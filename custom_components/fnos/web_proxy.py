"""fnOS Web 同源反向代理：HTTP 全方法透传 + WebSocket 透传 + HTML 改写。

为什么需要它：设备页「访问」按钮按《HA集成-远程访问地址跟随浏览器地址栏方案》
必须给出**浏览器当前实际可达**的地址。端口转发/反代场景下浏览器到不了 fnOS
内网 IP，唯一出路是把 fnOS Web 整站经 HA 反代成同源页面（悬浮窗 iframe 内嵌，
同时规避上游可能的 X-Frame-Options）。

鉴权模型（fnOS SPA 的请求集无界，无法逐 URL 签 authSig）：
  * 设备链接指向的 ``/fnos/web`` 页面走 HA 标准 authSig（GET，中间件放行）；
  * 页面渲染时签发 24h 随机**代理票据**，注入改写后的 HTML；
  * 页面内补丁脚本给 fetch/XHR/WebSocket/媒体元素自动追加票据，
    ``/fnos/proxy/**`` 校验票据放行（``/assets/``、``/static/`` 静态资源豁免）。
票据只授予访问已配好凭据的那台 fnOS，风险面与 esxi_stats 的 24h 签名链接一致。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import secrets
import time

import aiohttp
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DATA_KEY,
    DOMAIN,
    EMBED_JS_URL,
    PROXY_PREFIX,
    WEB_PAGE_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_EMBED_JS = os.path.join(_HERE, "frontend", "fnos-embed.js")

TICKET_PARAM = "fnosTicket"
TICKET_TTL = 24 * 3600.0
_TICKET_RENEW_THRESHOLD = 12 * 3600.0

#: 静态资源前缀：无票据放行（公开的 SPA 骨架文件，不含任何数据）。
#: /vm/static/ 是虚拟机 App SPA 骨架（noVNC 页面的 JS/CSS），模块脚本加载
#: 早于页内补丁的 MutationObserver，无法补票据 → 必须免票，否则 VNC 白屏。
_STATIC_EXEMPT = ("/assets/", "/static/", "/vm/static/", "/favicon")

_PROXY_DATA_KEY = f"{DOMAIN}_proxy"

_CHUNK = 64 * 1024

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "x-frame-options", "content-security-policy",
    "content-length", "content-encoding",
    "accept-encoding", "host",
}


def _proxy_store(hass):
    return hass.data.setdefault(_PROXY_DATA_KEY, {})


# --------------------------------------------------------------------------- #
# 虚拟机远程控制总开关（noVNC 控制台）
# --------------------------------------------------------------------------- #
#: 开关为 False 时：拒绝新的控制台页面 / VNC WebSocket，并立即切断已建立的
#: 连接（``switch.fnos_remote_control`` 关闭时调用 async_set_remote_control）。
_REMOTE_STATE_KEY = "remote_control"

#: 开关变化广播：多条目共用同一全局开关，其余条目的开关实体即时刷新显示
REMOTE_CONTROL_SIGNAL = f"{DOMAIN}_remote_control_changed"


def notify_remote_control(hass) -> None:
    """广播远程控制状态变化（线程安全）。

    dispatcher 是在**发送方线程**里同步执行回调的，一旦这里不是事件循环
    线程，实体回调里的 ``async_write_ha_state`` 就会在别的线程写状态
    （HA 会直接报 RuntimeError）。因此非循环线程一律调度回循环再发。
    """
    try:
        in_loop = asyncio.get_running_loop() is hass.loop
    except RuntimeError:  # 纯线程里没有运行中的循环
        in_loop = False
    if in_loop:
        async_dispatcher_send(hass, REMOTE_CONTROL_SIGNAL)
        return
    try:
        hass.loop.call_soon_threadsafe(
            async_dispatcher_send, hass, REMOTE_CONTROL_SIGNAL)
    except RuntimeError:  # 事件循环已关闭（集成卸载中）
        _LOGGER.debug("事件循环已关闭，跳过远程控制状态广播")


def _remote_state(hass):
    store = _proxy_store(hass)
    state = store.get(_REMOTE_STATE_KEY)
    if not isinstance(state, dict):
        state = {"enabled": True, "sessions": {}}
        store[_REMOTE_STATE_KEY] = state
    return state


def remote_control_enabled(hass) -> bool:
    """远程控制是否开启（未初始化按开启处理）。"""
    return bool(_remote_state(hass)["enabled"])


def remote_control_session_count(hass) -> int:
    """当前活动控制台连接数。"""
    return len(_remote_state(hass)["sessions"])


def _is_vm_remote_path(path: str) -> bool:
    """是否属于虚拟机远程控制通道。

    * ``/vm/vnc/<名>``、``target=vmconsole``：控制台页面入口；
    * ``/vm/api/v1/vnc/<名>``：noVNC 数据 WebSocket；
    * ``/websocket``：fnOS SPA（控制台页）用 location.host 拼的实时通道，
      HA 自渲染的悬浮窗页面不走它 —— 集成里经代理的 SPA 只有 VM 控制台，
      故远程控制关闭时一并拒绝。
    """
    text = "/" + str(path or "").lstrip("/").lower()
    if "/vm/vnc/" in text or "target=vmconsole" in text:
        return True
    if text.startswith("/vm/api/") and "vnc" in text:
        return True
    return text in ("/websocket", "/websocket/")


async def async_set_remote_control(hass, enabled: bool) -> int:
    """设置远程控制总开关。

    关闭时立即断开所有已建立的 noVNC 连接（不等浏览器自愈），
    返回被切断的连接数。
    """
    state = _remote_state(hass)
    state["enabled"] = bool(enabled)
    if enabled:
        notify_remote_control(hass)
        return 0
    sessions = list(state["sessions"].values())
    state["sessions"].clear()
    for ws_server, ws_client in sessions:
        for ws in (ws_client, ws_server):
            try:
                await ws.close(code=web.WSCloseCode.POLICY_VIOLATION,
                               message=b"remote control disabled")
            except Exception:  # pylint: disable=broad-except
                pass
    if sessions:
        _LOGGER.info("fnOS 远程控制已关闭，切断 %s 个控制台连接", len(sessions))
    notify_remote_control(hass)
    return len(sessions)


async def _get_session(hass):
    """共享的上游 ClientSession（事件循环单线程内懒创建）。"""
    store = _proxy_store(hass)
    session = store.get("session")
    if session is None or session.closed:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                          sock_read=300),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        store["session"] = session
        async def _on_stop(_event):
            await session.close()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    return session


def _upstream_base(hass, entry_id):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    data = entry.data
    scheme = "https" if data.get("ssl") else "http"
    return f"{scheme}://{data['host']}:{data.get('port', 5666)}"


def _upstream_ssl(hass):
    """忽略自签证书的 SSL context（懒创建、全局复用）。"""
    import ssl as ssl_mod

    store = _proxy_store(hass)
    if store.get("ssl_ctx") is None:
        ctx = ssl_mod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_mod.CERT_NONE
        store["ssl_ctx"] = ctx
    return store["ssl_ctx"]


def _upstream_token(hass, entry_id):
    """集成侧 WS 登录持有的 fnos-token（VM App REST / noVNC 用它做 cookie 鉴权）。"""
    store = hass.data.get(DOMAIN) or {}
    coord = (store.get(entry_id) or {}).get("coordinator")
    client = getattr(coord, "client", None)
    return getattr(client, "token", None) or ""


def _with_fnos_cookie(headers, token):
    """把 fnos-token 合并进转发请求的 Cookie 头（保留浏览器既有 cookie）。"""
    if not token:
        return headers
    parts = []
    for seg in str(headers.get("Cookie") or "").split(";"):
        seg = seg.strip()
        if seg and not seg.startswith("fnos-token="):
            parts.append(seg)
    parts.append(f"fnos-token={token}")
    headers["Cookie"] = "; ".join(parts)
    return headers


# --------------------------------------------------------------------------- #
# 代理票据
# --------------------------------------------------------------------------- #
def _tickets(hass):
    return _proxy_store(hass).setdefault("tickets", {})


def _issue_ticket(hass, entry_id):
    token = secrets.token_hex(20)
    _tickets(hass)[token] = {
        "entry": entry_id,
        "exp": time.monotonic() + TICKET_TTL,
    }
    # 顺手清理过期票据
    now = time.monotonic()
    for key in [k for k, v in _tickets(hass).items() if v["exp"] < now]:
        _tickets(hass).pop(key, None)
    return token


def _check_ticket(hass, entry_id, request):
    """校验请求携带的代理票据；剩余不足 12h 时滑动续期。"""
    token = (request.query.get(TICKET_PARAM)
             or request.headers.get("X-Fnos-Ticket") or "")
    record = _tickets(hass).get(token)
    if record is None or record["entry"] != entry_id:
        return False
    now = time.monotonic()
    if record["exp"] < now:
        _tickets(hass).pop(token, None)
        return False
    if record["exp"] - now < _TICKET_RENEW_THRESHOLD:
        record["exp"] = now + TICKET_TTL
    return True


def _is_exempt(path: str):
    return path.startswith(_STATIC_EXEMPT)


# --------------------------------------------------------------------------- #
# HTML 改写：<base> 前缀 + 运行时补丁（fetch/XHR/WS/EventSource/媒体元素）
# --------------------------------------------------------------------------- #
_PATCH_TEMPLATE = """
<script>
(function () {
  "use strict";
  var P = "__FNOS_PREFIX__";
  var T = "__FNOS_TICKET__";
  var PARAM = "fnosTicket";
  try {
    // VM 控制台 SPA 从浏览器 cookie 读 fnos-token 做鉴权（vdt()→authToken）；
    // 仅在 /vm/ 路径可读可发送，作用域限制在控制台页
    var __TOK = "__FNOS_TOKEN__";
    if (__TOK) {
      document.cookie =
        "fnos-token=" + __TOK + "; path=/vm/; SameSite=Lax";
    }
  } catch (e) {}
  function prefixed(pathname) {
    if (pathname === P || pathname.indexOf(P + "/") === 0) { return true; }
    return false;
  }
  function fix(u) {
    try {
      var x = new URL(u, location.href);
      // ws:/wss: 的 URL.origin 与页面 http(s) origin 永不相等（协议不同），
      // 不能走 origin 比较，否则 VM 控制台的 ws://host/websocket 不会被改写
      // （noVNC 连不上 HA 侧代理的根因）；同 host 即视为同源。
      var isWs = x.protocol === "ws:" || x.protocol === "wss:";
      var sameOrigin = isWs ? x.host === location.host
                            : x.origin === location.origin;
      if (!sameOrigin) { return u; }
      if (!prefixed(x.pathname)) {
        if (x.pathname.charAt(0) !== "/") { return u; }
        x.pathname = P + x.pathname;
      }
      if (!x.searchParams.get(PARAM)) { x.searchParams.set(PARAM, T); }
      if (isWs) {
        x.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      }
      return x.href;
    } catch (e) { return u; }
  }
  // WebSocket（fnOS SPA 用 location.host 拼绝对路径 /websocket）
  var OW = window.WebSocket;
  if (OW) {
    var NW = function (u, p) {
      u = fix(u);
      return p === undefined ? new OW(u) : new OW(u, p);
    };
    NW.prototype = OW.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) {
      try { NW[k] = OW[k]; } catch (e) {}
    });
    window.WebSocket = NW;
  }
  // fetch
  var OF = window.fetch;
  if (OF) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string") { input = fix(input); }
        else if (input && input.url) { input = new Request(fix(input.url), input); }
      } catch (e) {}
      return OF.call(this, input, init);
    };
  }
  // XMLHttpRequest（axios 浏览器端走 XHR）
  var OX = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, u) {
    var rest = Array.prototype.slice.call(arguments, 2);
    return OX.apply(this, [method, fix(u)].concat(rest));
  };
  // EventSource（SSE）
  var OE = window.EventSource;
  if (OE) {
    window.EventSource = function (u, conf) {
      return new OE(fix(u), conf);
    };
    window.EventSource.prototype = OE.prototype;
  }
  // DOM 媒体元素（img/video/source/audio 的 src、下载链接 href、海报 poster）
  function retag(node) {
    try {
      if (!node || node.nodeType !== 1 || !node.getAttribute) { return; }
      ["src", "href", "poster"].forEach(function (at) {
        var v = node.getAttribute(at);
        if (v && v.charAt(0) === "/") {
          var x = new URL(v, location.href);
          if (!prefixed(x.pathname)) { x.pathname = P + x.pathname; }
          if (!x.searchParams.get(PARAM)) { x.searchParams.set(PARAM, T); }
          node.setAttribute(at, x.href);
        }
      });
    } catch (e) {}
  }
  var mo = new MutationObserver(function (records) {
    records.forEach(function (rec) {
      if (rec.type === "attributes") { retag(rec.target); }
      (rec.addedNodes || []).forEach(function (n) {
        retag(n);
        if (n.querySelectorAll) {
          n.querySelectorAll("[src],[href],[poster]").forEach(retag);
        }
      });
    });
  });
  mo.observe(document.documentElement, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ["src", "href", "poster"]
  });
  // ---- 桌面 App 自动打开 ----
  // fnOS 内置应用（文件管理/设置/应用中心）没有独立 URL，只能经桌面 SPA 窗口
  // 打开。桌面图标渲染为 /static/app/icons/<appId>/icon.png，定位图标后沿
  // React fiber 找最近的 onDoubleClick/onClick 处理器直接调用（与真实点击
  // 同一条代码路径），失败再退化成原生 dblclick 事件。
  function __fnosOpenApp(appId) {
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (tries > 80) { clearInterval(timer); return; }
      var img = document.querySelector(
        'img[src*="/static/app/icons/' + appId + '/icon.png"],'
        + 'img[src*="/app-center-static/icon/' + appId + '/icon.png"]');
      if (!img) { return; }
      clearInterval(timer);
      var done = false;
      try {
        var fkey = Object.keys(img).find(function (k) {
          return k.indexOf("__reactFiber$") === 0;
        });
        var fiber = fkey ? img[fkey] : null;
        while (fiber && !done) {
          var props = fiber.memoizedProps;
          if (props) {
            var ev = {
              stopPropagation: function () {},
              preventDefault: function () {},
              nativeEvent: new MouseEvent("dblclick"),
              target: img, currentTarget: fiber.stateNode
            };
            if (typeof props.onDoubleClick === "function") {
              props.onDoubleClick(ev); done = true;
            } else if (typeof props.onClick === "function") {
              props.onClick(ev); done = true;
            }
          }
          fiber = fiber.return;
        }
      } catch (e) {}
      if (!done) {
        try {
          img.dispatchEvent(new MouseEvent("dblclick",
            { bubbles: true, cancelable: true }));
          img.dispatchEvent(new MouseEvent("click",
            { bubbles: true, cancelable: true }));
        } catch (e) {}
      }
    }, 300);
  }
  try {
    var __qs = new URLSearchParams(location.search);
    var __openApp = __qs.get("fnosopen");
    if (!__openApp && __qs.get("target") === "file") {
      __openApp = "trim.file-manager";
    }
    if (__openApp) { __fnosOpenApp(__openApp); }
  } catch (e) {}
})();
</script>
"""


def _rewrite_html(hass, entry_id, html_text, ticket, fnos_token=""):
    """注入 base 前缀 + 补丁脚本，并把 HTML 里的绝对资源引改到代理前缀下。

    fnos_token 非空时（VM 控制台页）注入 fnos-token cookie，供 SPA 自鉴权。
    """
    prefix = f"{PROXY_PREFIX}/{entry_id}"
    patch = (_PATCH_TEMPLATE
             .replace("__FNOS_PREFIX__", prefix)
             .replace("__FNOS_TICKET__", ticket)
             .replace("__FNOS_TOKEN__", fnos_token or ""))
    text = html_text
    # fnOS 各页面（桌面 / 相册 /p / 影视 /v / apps/* / vm）的静态资源均为
    # 站点绝对路径，统一改到代理前缀下（协议相对 // 除外）。
    # 运行期动态 URL 由补丁脚本 fix() 兜底。
    text = re.sub(r'(=(["\']))(/(?!/)[^"\']*)\2',
                  rf"\1{prefix}\3\2", text)
    inject = f'<base href="{prefix}/">' + patch
    if re.search(r"<head[^>]*>", text, re.IGNORECASE):
        text = re.sub(r"<head[^>]*>",
                      lambda m: m.group(0) + inject, text, count=1,
                      flags=re.IGNORECASE)
    else:
        text = inject + text
    return text


def _rewrite_location(hass, entry_id, location):
    """上游 302 的 Location 改到代理前缀下（相对化，天然跟随地址栏）。"""
    if not location:
        return location
    try:
        from yarl import URL as YarlURL

        parsed = YarlURL(location)
        if parsed.scheme in ("http", "https"):
            path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
            return f"{PROXY_PREFIX}/{entry_id}{path}"
    except ValueError:
        pass
    return location


def _clean_headers(headers):
    lower = {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS}
    return lower


# --------------------------------------------------------------------------- #
# 悬浮窗内容页：主机启动器 / Docker 容器链接页（HA 生成，ticket 鉴权）
# --------------------------------------------------------------------------- #
_PAGE_CSS = """
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: linear-gradient(160deg, #0f141c 0%, #16202e 100%);
    color: #e7edf5; min-height: 100vh; padding: 0;
  }
  /* 悬浮窗 iframe 满宽布局：内容撑满整个窗口，不留左右空白 */
  .card {
    width: 100%; max-width: 100%; min-height: 100vh; background: #1b222d;
    border: 0; border-radius: 0; box-shadow: none; padding: 22px 26px;
  }
  h1 { font-size: 17px; font-weight: 600; margin-bottom: 4px; }
  .sub { color: #8b98a9; font-size: 12.5px; margin-bottom: 18px; }
  .grid { display: grid; gap: 10px; margin-bottom: 6px; }
  .grid.c2 { grid-template-columns: 1fr 1fr; }
  .grid.c4 { grid-template-columns: repeat(4, 1fr); }
  a.tile {
    display: flex; flex-direction: column; gap: 8px; align-items: flex-start;
    background: #202a38; border: 1px solid #2f3c4e; border-radius: 10px;
    color: #e7edf5; text-decoration: none; padding: 13px 14px;
    transition: border-color .15s, background .15s;
  }
  a.tile:hover { border-color: #2f9bff; background: #243146; }
  a.tile .ico { font-size: 20px; line-height: 1; }
  a.tile .nm { font-size: 13.5px; font-weight: 600; }
  a.tile .ds { font-size: 11px; color: #8b98a9; }
  .urlbox {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: #11151c; border: 1px solid #2f3c4e; border-radius: 10px;
    padding: 13px 14px; margin: 14px 0;
  }
  .urlbox code {
    font-family: Consolas, monospace; font-size: 13.5px; color: #6fd3ff;
    word-break: break-all; flex: 1;
  }
  .urlbox a, .btn {
    background: #2f9bff; color: #fff; text-decoration: none; border: 0;
    font-size: 12.5px; font-weight: 600; border-radius: 8px;
    padding: 8px 16px; cursor: pointer; white-space: nowrap;
  }
  .ports { font-size: 12px; color: #8b98a9; line-height: 1.9; }
  .ports b { color: #cdd9e5; font-weight: 600; }
  .hint { font-size: 11.5px; color: #6b7787; margin-top: 12px; line-height: 1.7; }
  .plist { display: flex; flex-direction: column; gap: 8px; margin: 14px 0; }
  a.prow {
    display: flex; align-items: center; gap: 12px;
    background: #11151c; border: 1px solid #2f3c4e; border-radius: 10px;
    padding: 12px 14px; text-decoration: none;
    transition: border-color .15s, background .15s;
  }
  a.prow:hover { border-color: #2f9bff; background: #1a2433; }
  a.prow .purl {
    font-family: Consolas, monospace; font-size: 13.5px; color: #6fd3ff;
    flex: 1; word-break: break-all;
  }
  a.prow .pmeta { font-size: 11.5px; color: #8b98a9; white-space: nowrap; }
  .pc {
    background: #2f9bff; color: #fff; border: 0; font-size: 11.5px;
    font-weight: 600; border-radius: 7px; padding: 6px 12px; cursor: pointer;
    white-space: nowrap;
  }
  .pc:hover { background: #57b0ff; }
  /* ---- 仪表盘（主机页）---- */
  .sec { margin: 18px 0 8px; font-size: 13px; font-weight: 600;
         color: #9fb2c8; letter-spacing: .04em; }
  .kvs { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
         gap: 8px; }
  .kv { background: #11151c; border: 1px solid #2f3c4e; border-radius: 10px;
        padding: 10px 12px; }
  .kv .k { font-size: 11px; color: #8b98a9; margin-bottom: 4px; }
  .kv .v { font-size: 14.5px; font-weight: 600; color: #e7edf5;
           word-break: break-all; }
  .kv .v small { font-size: 11px; color: #8b98a9; font-weight: 400; }
  .badge { display: inline-block; font-size: 10.5px; font-weight: 600;
           border-radius: 6px; padding: 2px 8px; margin-left: 6px;
           vertical-align: 1px; }
  .b-ok { background: #17311f; color: #6fe08a; }
  .b-off { background: #31201a; color: #ff9d7a; }
  .b-warn { background: #332b17; color: #ffd479; }
  .b-idle { background: #1d2735; color: #8b98a9; }
  /* ---- 文件列表 ---- */
  .crumbs { display: flex; flex-wrap: wrap; gap: 2px 4px; align-items: center;
            font-size: 12.5px; margin: 12px 0; color: #8b98a9; }
  .crumbs a { color: #6fd3ff; text-decoration: none; padding: 2px 4px;
              border-radius: 6px; }
  .crumbs a:hover { background: #1a2433; }
  .crumbs .sep { color: #4a586c; }
  .flist { display: flex; flex-direction: column; gap: 6px; }
  .frow { display: flex; align-items: center; gap: 10px; text-align: left;
          background: #11151c; border: 1px solid #2f3c4e; border-radius: 9px;
          padding: 9px 12px; text-decoration: none; color: #e7edf5;
          transition: border-color .15s, background .15s; }
  a.frow:hover { border-color: #2f9bff; background: #1a2433; }
  span.frow { opacity: .96; }
  .frow .fico { width: 20px; text-align: center; flex: none; }
  .frow .fname { flex: 1; font-size: 13px; word-break: break-all; }
  .frow .fmeta { font-size: 11px; color: #8b98a9; white-space: nowrap; }
  /* ---- 应用网格 ---- */
  .apps { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
          gap: 10px; }
  .app { background: #11151c; border: 1px solid #2f3c4e; border-radius: 10px;
         padding: 12px 8px; display: flex; flex-direction: column;
         align-items: center; gap: 7px; text-align: center; }
  .app img { width: 38px; height: 38px; border-radius: 9px; object-fit: contain;
             background: #1b222d; }
  .app .an { font-size: 12px; font-weight: 600; word-break: break-all; }
  .app .av { font-size: 10px; color: #8b98a9; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block;
         margin-right: 4px; }
  .dot.on { background: #6fe08a; }
  .dot.off { background: #6b7787; }
  /* ---- 统一滚动条（替代浏览器默认灰色宽条，与深色卡片风格一致）---- */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #2f3c4e; border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #46607f; }
  * { scrollbar-width: thin; scrollbar-color: #2f3c4e transparent; }
  /* ---- 文件/相册页工具栏（上传）---- */
  .toolbar { display: flex; align-items: center; gap: 10px; margin: 0 0 12px; }
  .upstat { font-size: 11.5px; color: #8b98a9; word-break: break-all; }
  .frow .dlbox { background: #1d2735; border-radius: 6px; padding: 3px 9px;
                 color: #6fd3ff; font-weight: 600; flex: none; }
  a.frow:hover .dlbox { background: #2f9bff; color: #fff; }
  /* ---- 主机页应用启停菜单 ---- */
  .app[data-app] { cursor: pointer; }
  .app[data-app]:hover { border-color: #2f9bff; background: #1a2433; }
  .amenu { position: fixed; z-index: 60; display: none; flex-direction: column;
           min-width: 130px; padding: 6px; background: #202a38;
           border: 1px solid #3d4f66; border-radius: 10px;
           box-shadow: 0 10px 28px rgba(0, 0, 0, .5); }
  .amenu .mi { display: block; width: 100%; text-align: left; border: 0;
               background: transparent; color: #e7edf5; font-size: 12.5px;
               font-weight: 600; padding: 8px 10px; border-radius: 7px;
               cursor: pointer; }
  .amenu .mi:hover { background: #2f9bff; color: #fff; }
  .amenu .mi:disabled { opacity: .55; cursor: default; }
  .amenu .mstop:hover { background: #e5484d; color: #fff; }
  .amstat { font-size: 11px; color: #8b98a9; padding: 4px 6px 2px;
            min-height: 16px; word-break: break-all; }
  /* ---- 相册照片墙 + 灯箱 ---- */
  .pwall { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
           gap: 8px; margin: 12px 0; }
  .pitem { position: relative; aspect-ratio: 1 / 1; border-radius: 9px;
           overflow: hidden; background: #11151c; border: 1px solid #2f3c4e;
           cursor: zoom-in; padding: 0; }
  .pitem:hover { border-color: #2f9bff; }
  .pitem img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .pitem .pm { position: absolute; left: 0; right: 0; bottom: 0;
               font-size: 10px; color: #dfe8f2; text-align: left;
               background: rgba(10, 14, 20, .62); padding: 3px 6px;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .lbox { position: fixed; inset: 0; z-index: 80; display: none;
          align-items: center; justify-content: center;
          background: rgba(5, 8, 12, .88); padding: 26px; }
  .lbox .lbig { max-width: 92%; max-height: 76vh; border-radius: 10px;
                box-shadow: 0 12px 40px rgba(0, 0, 0, .6); }
  .lbox .lbar { position: absolute; top: 12px; right: 14px; display: flex;
                gap: 8px; }
  .lbox .lname { position: absolute; left: 16px; bottom: 12px; font-size: 12px;
                 color: #cdd9e5; max-width: 70%; word-break: break-all; }
  /* ---- 虚拟机详情 ---- */
  table.vtab { width: 100%; border-collapse: collapse; font-size: 12px;
               margin: 8px 0; }
  table.vtab th, table.vtab td { border: 1px solid #2f3c4e; padding: 7px 9px;
                                 text-align: left; word-break: break-all; }
  table.vtab th { color: #8b98a9; font-weight: 600; background: #11151c; }
  table.vtab td { color: #e7edf5; }
  .vbtns { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
  .vbtns .pc.off { background: #1d2735; color: #8b98a9; cursor: default; }
  .vmstat { font-size: 11.5px; color: #8b98a9; word-break: break-all;
            align-self: center; }
"""

_PAGE_JS = """
  function copyUrl(btn, text) {
    navigator.clipboard.writeText(text).then(function () {
      var old = btn.textContent; btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = old; }, 1500);
    }).catch(function () {});
  }
"""


# --------------------------------------------------------------------------- #
# 悬浮窗内容页公共工具（数据全部由集成服务端调 fnOS API 获取，无透传）
# --------------------------------------------------------------------------- #
def _esc(v):
    return str(v if v is not None else "").replace("&", "&amp;").replace(
        "<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _js_encode_uri(s):
    """模拟 JS encodeURI（fnOS /upload 的 Trim-Path / Trim-Sign 均基于它）。"""
    keep = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
               "0123456789;/?:@&=+$-_.!~*'()#,")
    out = []
    for ch in str(s):
        if ch in keep:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def _trim_sign(client, text):
    """fnOS /upload 的 Trim-Sign：HMAC-SHA256(text, sign_key) → base64。

    与桌面 SPA 的 mz() 一致：密钥即 WS 登录 secret 解出的 base64 签名密钥。
    """
    import base64 as b64mod
    import hashlib as hlmod
    import hmac as hmacmod

    key = getattr(client, "_sign_key", None)
    if not key:
        raise RuntimeError("fnOS 签名密钥未就绪（尚未登录）")
    return b64mod.b64encode(hmacmod.new(
        b64mod.b64decode(key), text.encode(), hlmod.sha256).digest()).decode()


def _download_url(entry_id, path, name, ticket):
    """下载链接：路径式传 path（HA security_filter 会拦截 ?x=/path 形态查询串，
    故 path 并入路径段；aiohttp match_info 自动解码）。"""
    from urllib.parse import quote

    return (f"{WEB_PAGE_PREFIX}/dl{quote(str(path), safe='/')}"
            f"?entry={entry_id}"
            f"&{TICKET_PARAM}={ticket}"
            f"&name={quote(str(name), safe='')}")


def _img_url(entry_id, upstream_path, ticket):
    """相册图片流代理链接（/p/api/v1/stream/*，HA 带 token 代取）。

    流路径并入路径段而非 ?u= 查询串——security_filter 会把 "?u=/p/..."
    递归解码后按 "x=/path" 形态整批拦截（相册墙全军覆没的根因）。
    """
    return (f"{WEB_PAGE_PREFIX}/img{str(upstream_path or '')}"
            f"?entry={entry_id}"
            f"&{TICKET_PARAM}={ticket}")


def _fmt_phototime(value):
    """相册时间串 "2026:09:18 02:08:39" → "2026-09-18 02:08"。"""
    text = str(value or "")
    date, _, tm = text.partition(" ")
    if not date:
        return ""
    shown = date.replace(":", "-")
    if tm:
        shown += f" {tm[:5]}"
    return shown


def _fmt_size(num):
    if not isinstance(num, (int, float)):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return "—"


def _fmt_speed(num):
    if not isinstance(num, (int, float)) or num <= 0:
        return "0 KB/s"
    return _fmt_size(num) + "/s"


def _fmt_ts(ts):
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "—"
    try:
        from datetime import datetime

        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return "—"


def _fmt_uptime(seconds):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "—"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {mins} 分"
    return f"{mins} 分钟"


def _snap(hass, entry_id):
    """集成快照（30s 刷新一轮）；未就绪返回 {}。"""
    store = hass.data.get(DOMAIN) or {}
    coord = (store.get(entry_id) or {}).get("coordinator")
    return (coord.data if coord is not None else None) or {}


def _client(hass, entry_id):
    store = hass.data.get(DOMAIN) or {}
    return ((store.get(entry_id) or {}).get("coordinator")) and \
        (store.get(entry_id) or {}).get("coordinator").client


def _page_client(hass, entry_id):
    """悬浮窗专用 fnOS 客户端（独立 WS 连接，按 entry_id 缓存）。

    页面取数若与快照轮询共用集成主客户端，两条路径会在同一把 RLock 上互相
    等待（文件悬浮窗卡顿的根因）；这里给悬浮窗单独一条连接。凭据变化时
    自动重建。建连/登录是阻塞网络 IO，只能在 executor 线程调用。
    """
    store = _proxy_store(hass)
    clients = store.setdefault("page_clients", {})
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    data = entry.data
    sig = (str(data["host"]), int(data.get("port", 5666)),
           str(data["username"]), str(data["password"]),
           bool(data.get("ssl", False)))
    client = clients.get(entry_id)
    if client is not None and getattr(client, "cred_sig", None) != sig:
        try:
            client.close()
        except Exception:  # pylint: disable=broad-except
            pass
        client = None
    if client is None:
        from .api import FnOsClient

        client = FnOsClient(host=sig[0], port=sig[1], username=sig[2],
                            password=sig[3], ssl=sig[4])
        client.cred_sig = sig
        clients[entry_id] = client
    return client


def close_page_client(hass, entry_id):
    """关闭并移除某条目的悬浮窗专用客户端（entry 卸载 / HA 停止时调用）。"""
    clients = _proxy_store(hass).get("page_clients") or {}
    client = clients.pop(entry_id, None)
    if client is not None:
        try:
            client.close()
        except Exception:  # pylint: disable=broad-except
            pass


def _nav_url(entry_id, target, extra=None, ticket=""):
    """生成悬浮窗页内导航链接（票据鉴权）。

    不再用 authSig 签名：authSig 对含中文/百分号编码的 query 校验存在兼容
    问题（文件窗无法进入子目录、相册页 401 的根因），页内导航统一走
    /fnos/web/nav 并由代理票据保护（票据仅由鉴权通过的页面签发）。
    """
    from urllib.parse import quote

    query = f"entry={entry_id}&target={target}&{TICKET_PARAM}={ticket}"
    for key, value in (extra or {}).items():
        if value is not None:
            query += f"&{key}={quote(str(value), safe='')}"
    return f"{WEB_PAGE_PREFIX}/nav?{query}"


def _page_shell(title, subtitle, body, js=""):
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head>
<body><div class="card">
<h1>{_esc(title)}</h1>
<div class="sub">{_esc(subtitle)}</div>
{body}
</div><script>{_PAGE_JS}{js}</script></body></html>"""


def _not_ready(entry_id):
    return _page_shell(
        "飞牛 fnOS", "数据尚未就绪",
        '<div class="urlbox"><code>集成快照尚未完成首轮刷新，'
        "请稍后重新打开悬浮窗；若持续出现请在 HA 中检查 fnOS 集成状态。</code></div>")


# --------------------------------------------------------------------------- #
# 主机页：系统仪表盘 + 已装应用（全部来自集成快照 / 应用中心 API）
# --------------------------------------------------------------------------- #
def _kv(label, value, badge=""):
    return (f'<div class="kv"><div class="k">{_esc(label)}</div>'
            f'<div class="v">{_esc(value)}{badge}</div></div>')


def _host_page_html(hass, entry_id, ticket):
    """主机设备悬浮窗：fnOS 系统总览 + 服务状态 + 已装应用网格（手机 App 同款信息架构）。"""
    snap = _snap(hass, entry_id)
    if not snap:
        return _not_ready(entry_id)
    prefix = f"{PROXY_PREFIX}/{entry_id}"

    host = snap.get("host") or {}
    rm = snap.get("resmon") or {}
    st = snap.get("storage") or {}
    svc = snap.get("services") or {}
    docker = snap.get("docker") or {}
    dl = snap.get("download") or {}
    ext = snap.get("external") or {}
    apps = snap.get("apps") or {}
    ver = host.get("version") or "?"

    # --- 头部徽章 ---
    badges = ""
    upd = snap.get("update") or {}
    if upd.get("available"):
        badges += '<span class="badge b-warn">有系统更新</span>'
    else:
        badges += '<span class="badge b-ok">已是最新</span>'
    fn_conn = (ext.get("fnconnect") or {})
    fn_state = str(fn_conn.get("state") or fn_conn.get("status") or "")
    if fn_state:
        ok = fn_state.lower() in ("open", "opened", "connected", "running")
        badges += (f'<span class="badge {"b-ok" if ok else "b-idle"}">'
                   f'FN Connect {fn_state}</span>')

    # --- 资源监控 ---
    mem_pct = rm.get("mem_pct")
    mem_txt = "—"
    if isinstance(rm.get("mem_used"), (int, float)) and isinstance(
            rm.get("mem_total"), (int, float)) and rm["mem_total"]:
        mem_txt = (f"{_fmt_size(rm['mem_used'])} / {_fmt_size(rm['mem_total'])}"
                   f"（{mem_pct:.1f}%）" if mem_pct is not None else
                   f"{_fmt_size(rm['mem_used'])} / {_fmt_size(rm['mem_total'])}")
    cpu_pct = rm.get("cpu_pct")
    res_kvs = "".join([
        _kv("CPU 使用率",
            f"{cpu_pct:.1f}%" if isinstance(cpu_pct, (int, float)) else "—",
            f'<small>{_esc(rm.get("cpu_name") or "")}</small>'),
        _kv("内存", mem_txt),
        _kv("GPU 使用率",
            f"{rm['gpu_pct']:.0f}%" if isinstance(rm.get("gpu_pct"),
                                                  (int, float)) else "—"),
        _kv("网络 ↑", _fmt_speed(rm.get("net_up"))),
        _kv("网络 ↓", _fmt_speed(rm.get("net_down"))),
        _kv("磁盘读取", _fmt_speed(rm.get("disk_read"))),
        _kv("磁盘写入", _fmt_speed(rm.get("disk_write"))),
        _kv("风扇转速", (f"{rm['fan_rpm']:.0f} RPM"
                    if isinstance(rm.get("fan_rpm"), (int, float)) else "—")),
    ])

    # --- 存储 ---
    st_bad = ""
    if st.get("disk_bad"):
        st_bad += (f'<span class="badge b-off">'
                   f'{st["disk_bad"]} 块硬盘异常</span>')
    stor_kvs = "".join([
        _kv("总容量", _fmt_size(st.get("total"))),
        _kv("已用", _fmt_size(st.get("used"))),
        _kv("可用", _fmt_size(st.get("free"))),
        _kv("硬盘数量", str(st.get("disk_count") or "—"), st_bad),
        _kv("存储池", str(st.get("pool_count") or "—")),
    ])

    # --- 服务开关 ---
    svc_names = [("ssh", "SSH"), ("smb", "SMB"), ("nfs", "NFS"),
                 ("ftp", "FTP"), ("webdav", "WebDAV"), ("dlna", "DLNA"),
                 ("firewall", "防火墙")]
    svc_kvs = "".join(
        _kv(label, "开启" if svc.get(key) else "关闭",
            f'<span class="badge {"b-ok" if svc.get(key) else "b-off"}">'
            f'{"运行中" if svc.get(key) else "已停止"}</span>')
        for key, label in svc_names)

    # --- Docker / 下载中心 ---
    containers = docker.get("containers") or []
    running_n = sum(1 for c in containers
                    if str(c.get("state") or "").lower() == "running")
    docker_installed = docker.get("installed")
    docker_kvs = "".join([
        _kv("Docker 容器",
            (f"{running_n} 运行 / {len(containers)} 总数"
             if docker_installed else "未安装")),
        _kv("下载中心",
            ("活跃任务 " + str(dl.get("active_tasks"))
             if dl.get("installed") and dl.get("active_tasks") is not None
             else ("已安装" if dl.get("installed") else "未安装"))),
        _kv("下载 ↑", _fmt_speed(dl.get("speed_up"))),
        _kv("下载 ↓", _fmt_speed(dl.get("speed_down"))),
    ])

    # --- 外部访问 / 会话 ---
    domains = ext.get("domains") or []
    misc_kvs = "".join([
        _kv("未读通知", str(snap.get("notify_unread") or 0)),
        _kv("登录设备", str(len(snap.get("login_devices") or []))),
        _kv("外部访问域名", str(len(domains))),
        _kv("证书数量", str(ext.get("cert_count") or 0)),
        _kv("虚拟机", str(len((snap.get("vms") or {}).get("list") or []))),
    ])

    # --- 已装应用网格（应用中心 API；点击图标弹出启动/停止菜单）---
    app_cards = []
    for app in snap.get("apps_installed") or []:
        icon = app.get("icon") or ""
        if icon.startswith("/"):
            icon_url = f"{prefix}{icon}?{TICKET_PARAM}={ticket}"
        else:
            icon_url = f"{prefix}/static/app/icons/icon.png"
        running = str(app.get("status") or "").lower() == "running"
        app_cards.append(
            f'<div class="app" data-app="{_esc(app.get("app"))}" '
            f'data-running="{"1" if running else "0"}" '
            f'title="{_esc(app.get("name"))} {_esc(app.get("version"))} · '
            f'点击操作">'
            f'<img src="{_esc(icon_url)}" alt="" loading="lazy">'
            f'<div class="an"><span class="dot {"on" if running else "off"}">'
            f'</span>{_esc(app.get("name"))}</div>'
            f'<div class="av">{_esc(app.get("version"))}'
            f'{" · 运行中" if running else ""}</div></div>')
    apps_html = (f'<div class="apps">{"".join(app_cards)}</div>'
                 if app_cards else
                 '<div class="hint">暂时无法获取应用列表'
                 "（应用中心服务未响应）。</div>")
    appctl_url = (f"{WEB_PAGE_PREFIX}/appctl?entry={entry_id}"
                  f"&{TICKET_PARAM}={ticket}")
    app_menu = (f'<div class="amenu" id="amenu" data-act="{_esc(appctl_url)}">'
                f'<button class="mi mstart" type="button">▶ 启动应用</button>'
                f'<button class="mi mstop" type="button">■ 停止应用</button>'
                f'<div class="amstat" id="amstat"></div></div>')
    photo_ok = apps.get("photo")
    video_ok = apps.get("video")

    body = f"""
<div>{_esc(host.get("hostname") or "")} · fnOS {_esc(ver)}
{_esc(host.get("edition") or "")}{badges}</div>
<div class="sec">运行时长 {_esc(_fmt_uptime(host.get("uptime_seconds")))}
 · 系统版本 {_esc(ver)}</div>
<div class="sec">资源监控</div>
<div class="kvs">{res_kvs}</div>
<div class="sec">存储</div>
<div class="kvs">{stor_kvs}</div>
<div class="sec">文件服务</div>
<div class="kvs">{svc_kvs}</div>
<div class="sec">Docker 与下载</div>
<div class="kvs">{docker_kvs}</div>
<div class="sec">访问与通知</div>
<div class="kvs">{misc_kvs}</div>
<div class="sec">已装应用（{len(app_cards)}）<span class="sub">点击图标可启动/停止</span></div>
{apps_html}
{app_menu}
<div class="hint">相册 App：{"可用" if photo_ok else "不可用"} ·
影视 App：{"可用" if video_ok else "不可用"}。
数据由 Home Assistant 每 30 秒从 fnOS API 拉取。</div>"""
    return _page_shell("飞牛主机 · 系统总览",
                       "系统状态 · 服务 · 已装应用", body, js=_APP_MENU_JS)


# --------------------------------------------------------------------------- #
# 文件页 / 相册页：WS file.ls 目录浏览 + 最近文件（集成服务端取数）
# --------------------------------------------------------------------------- #
_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
            ".tiff", ".tif", ".svg", ".avif")
_VID_EXT = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m2ts")
_DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".md")


def _fetch_photo_payload(hass, entry_id):
    """executor：相册「最近添加」照片（recent_photos，authx 签名实测通签）。

    返回 {"photos": [...]} 或 {"error": "..."}。
    """
    client = _page_client(hass, entry_id)
    if client is None:
        return {"error": "fnOS 集成客户端不可用，请重载集成后重试。"}
    try:
        return {"photos": client.photo_recent(offset=0, limit=60)}
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"fnOS 相册 API 调用失败：{err}"}


def _file_icon(name, is_dir):
    if is_dir:
        return "📁"
    low = str(name).lower()
    if low.endswith(_IMG_EXT):
        return "🖼️"
    if low.endswith(_VID_EXT):
        return "🎬"
    if low.endswith((".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a")):
        return "🎵"
    if low.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "🗜️"
    if low.endswith(_DOC_EXT):
        return "📄"
    if low.endswith((".iso", ".img")):
        return "💽"
    return "📄"


def _fetch_browse_payload(hass, entry_id, target, path):
    """executor 里调 fnOS 文件 WS API 取目录数据（绝不能在事件循环里同步调用）。

    返回 {"rows": [...], "recent": [...](仅 file 根目录), "path": 实际路径}
    或 {"error": "..."}。
    """
    client = _page_client(hass, entry_id)
    if client is None:
        return {"error": "fnOS 集成客户端不可用，请重载集成后重试。"}

    def ls(p):
        resp = client.request("file.ls", timeout=15, path=p)
        errno = resp.get("errno")
        if errno not in (None, 0):
            return None, errno
        files = resp.get("files")
        if not isinstance(files, list):
            data = resp.get("data") if isinstance(resp.get("data"), dict) \
                else {}
            files = data.get("files") if isinstance(data.get("files"),
                                                    list) else []
        rows = [f for f in files if isinstance(f, dict)]
        rows.sort(key=lambda f: (not f.get("dir"), str(f.get("name") or "")))
        return rows, None

    resolved = path
    try:
        rows, errno = ls(resolved)
        if errno == 4100:
            return {"error": "目录不存在（可能已被删除或移动）。"}
        if errno == 4352:
            return {"error": "没有权限访问该目录。"}
        if errno:
            return {"error": f"fnOS 返回错误（errno={errno}）。"}
        payload = {"rows": rows or [], "path": resolved}
        if target == "file" and not path:
            try:
                rec = client.request("file.recent.list", timeout=12)
                payload["recent"] = (rec.get("recent")
                                     if isinstance(rec.get("recent"), list)
                                     else [])
            except Exception:  # pylint: disable=broad-except
                payload["recent"] = []
        return payload
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"fnOS 文件 API 调用失败：{err}"}


def _crumbs_html(entry_id, target, path, ticket):
    root_label = "个人空间" if target == "file" else "相册空间"
    parts = [p for p in str(path or "").split("/") if p]
    crumbs = [f'<a href="{_esc(_nav_url(entry_id, target, ticket=ticket))}">'
              f'{root_label}</a>']
    acc = ""
    for part in parts:
        acc = f"{acc}/{part}" if acc else part
        href = _nav_url(entry_id, target, {"path": acc}, ticket=ticket)
        crumbs.append(f'<a href="{_esc(href)}">{_esc(part)}</a>')
    sep = '<span class="sep">/</span>'
    return f'<div class="crumbs">{sep.join(crumbs)}</div>'


def _row_child_path(path, row):
    """行条目的完整路径。

    fnOS file.ls 子目录必须用 /vol{卷id}/{uid}/xxx 全路径（裸名字报 errno
    4101）；根目录行带 v/uid 字段，据此拼出全路径，深层直接拼接。
    """
    name = str(row.get("name") or "")
    if path:
        return f"{str(path).rstrip('/')}/{name}"
    if row.get("v") is not None and row.get("uid") is not None:
        return f"/vol{row.get('v')}/{row.get('uid')}/{name}"
    return name


def _file_rows_html(entry_id, target, path, rows, ticket):
    rows_html = []
    for row in rows:
        name = str(row.get("name") or "")
        is_dir = bool(row.get("dir"))
        child = _row_child_path(path, row)
        if is_dir:
            href = _nav_url(entry_id, target, {"path": child}, ticket=ticket)
            rows_html.append(
                f'<a class="frow" href="{_esc(href)}">'
                f'<span class="fico">📁</span>'
                f'<span class="fname">{_esc(name)}</span>'
                f'<span class="fmeta">目录</span></a>')
        else:
            meta = _fmt_size(row.get("size"))
            mtim = row.get("mtim")
            if isinstance(mtim, (int, float)) and mtim > 0:
                meta += f" · {_fmt_ts(mtim)}"
            href = _download_url(entry_id, child, name, ticket)
            rows_html.append(
                f'<a class="frow fdl" href="{_esc(href)}" '
                f'title="点击下载（由 Home Assistant 从 fnOS 取回）">'
                f'<span class="fico">{_file_icon(name, False)}</span>'
                f'<span class="fname">{_esc(name)}</span>'
                f'<span class="fmeta">{_esc(meta)}'
                f'<span class="dlbox">⬇ 下载</span></span></a>')
    return f'<div class="flist">{"".join(rows_html)}</div>' if rows_html else \
        '<div class="hint">该目录为空。</div>'


def _recent_rows(recent, limit=8):
    rows = []
    for item in (recent or [])[:limit]:
        if not isinstance(item, dict):
            continue
        rpath = str(item.get("path") or "")
        if not rpath:
            continue
        name = rpath.rstrip("/").rsplit("/", 1)[-1]
        meta = _fmt_size(item.get("size"))
        dt = item.get("datetime")
        if isinstance(dt, (int, float)) and dt > 0:
            meta += f" · {_fmt_ts(dt)}"
        rows.append(
            f'<span class="frow">'
            f'<span class="fico">{_file_icon(name, False)}</span>'
            f'<span class="fname">{_esc(name)}</span>'
            f'<span class="fmeta">{_esc(meta)}</span></span>')
    if not rows:
        return ""
    return ('<div class="sec">最近文件</div>'
            f'<div class="flist">{"".join(rows)}</div>')


def _upload_bar_html(entry_id, path, ticket):
    """目录页顶部工具栏：上传按钮（选择文件后经 HA 转存到 fnOS）。"""
    from urllib.parse import quote

    up = (f"{WEB_PAGE_PREFIX}/up?entry={entry_id}"
          f"&{TICKET_PARAM}={ticket}&path={quote(str(path or ''), safe='')}")
    return (f'<div class="toolbar">'
            f'<button class="pc" type="button" id="upbtn" data-up="{_esc(up)}">'
            f'⬆ 上传文件</button>'
            f'<span class="upstat" id="upstat"></span>'
            f'<input type="file" id="upinput" multiple style="display:none">'
            f'</div>')


_UPLOAD_JS = """
  (function () {
    var btn = document.getElementById("upbtn");
    var input = document.getElementById("upinput");
    var stat = document.getElementById("upstat");
    if (!btn || !input) { return; }
    btn.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", async function () {
      var files = Array.prototype.slice.call(input.files || []);
      if (!files.length) { return; }
      btn.disabled = true;
      for (var i = 0; i < files.length; i++) {
        stat.textContent = "正在上传 " + (i + 1) + "/" + files.length +
          "：" + files[i].name;
        var fd = new FormData();
        fd.append("file", files[i]);
        try {
          var r = await fetch(btn.dataset.up, {method: "POST", body: fd});
          var j = await r.json();
          if (!j.ok) { stat.textContent = "上传失败：" + (j.error || r.status);
            btn.disabled = false; return; }
        } catch (e) {
          stat.textContent = "上传失败：" + e;
          btn.disabled = false; return;
        }
      }
      stat.textContent = "上传完成，正在刷新…";
      setTimeout(function () { location.reload(); }, 600);
    });
  })();
"""


_PHOTO_JS = """
  (function () {
    var box = document.getElementById("lbox");
    if (!box) { return; }
    var big = document.getElementById("lbig");
    var nm = document.getElementById("lname");
    var dl = document.getElementById("ldl");
    document.querySelectorAll(".pitem").forEach(function (it) {
      it.addEventListener("click", function () {
        big.src = it.dataset.big;
        nm.textContent = it.dataset.name || "";
        dl.href = it.dataset.orig;
        dl.setAttribute("download", it.dataset.name || "photo.jpg");
        box.style.display = "flex";
      });
    });
    box.addEventListener("click", function () {
      box.style.display = "none";
      big.src = "";
    });
  })();
"""


_VM_JS = """
  function vmact(action, btn) {
    var st = document.getElementById("vmstat");
    btn.disabled = true;
    if (st) { st.textContent = action === "start" ? "正在开机…" : "正在执行…"; }
    var fd = new FormData();
    fd.append("name", btn.dataset.name);
    fd.append("action", action);
    fetch(btn.dataset.act, {method: "POST", body: fd})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j && j.ok) {
          if (st) { st.textContent = "操作成功，正在刷新…"; }
          setTimeout(function () { location.reload(); }, 900);
        } else {
          if (st) { st.textContent = "失败：" + ((j && j.error) || r.status); }
          btn.disabled = false;
        }
      })
      .catch(function (e) {
        if (st) { st.textContent = "失败：" + e; }
        btn.disabled = false;
      });
  }
  document.querySelectorAll(".vact").forEach(function (b) {
    b.addEventListener("click", function () { vmact(b.dataset.action, b); });
  });
"""


_APP_MENU_JS = """
  (function () {
    var menu = document.getElementById("amenu");
    if (!menu) { return; }
    var stat = document.getElementById("amstat");
    document.querySelectorAll(".app[data-app]").forEach(function (card) {
      card.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var reopen = menu.style.display === "block" &&
          menu.dataset.app === card.dataset.app;
        if (reopen) { menu.style.display = "none"; return; }
        menu.dataset.app = card.dataset.app;
        menu.dataset.running = card.dataset.running;
        menu.querySelector(".mstart").style.display =
          card.dataset.running === "1" ? "none" : "block";
        menu.querySelector(".mstop").style.display =
          card.dataset.running === "1" ? "block" : "none";
        stat.textContent = "";
        menu.style.visibility = "hidden";
        menu.style.display = "block";
        var r = card.getBoundingClientRect();
        var mh = menu.offsetHeight, mw = menu.offsetWidth;
        var left = Math.min(r.left, window.innerWidth - mw - 8);
        var top = r.bottom + 6;
        if (top + mh > window.innerHeight - 8) { top = r.top - mh - 6; }
        menu.style.left = Math.max(8, left) + "px";
        menu.style.top = Math.max(8, top) + "px";
        menu.style.visibility = "visible";
      });
    });
    menu.addEventListener("click", function (ev) { ev.stopPropagation(); });
    document.addEventListener("click", function () {
      menu.style.display = "none";
    });
    function act(action, btn) {
      btn.disabled = true;
      stat.textContent = action === "start" ? "正在启动…" : "正在停止…";
      fetch(menu.dataset.act, {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "app=" + encodeURIComponent(menu.dataset.app) +
              "&action=" + action
      }).then(function (r) { return r.json(); }).then(function (j) {
        if (j && j.ok) {
          stat.textContent = "操作成功，正在刷新…";
          setTimeout(function () { location.reload(); }, 700);
        } else {
          stat.textContent = "失败：" + ((j && j.error) || r.status);
          btn.disabled = false;
        }
      }).catch(function (e) {
        stat.textContent = "失败：" + e;
        btn.disabled = false;
      });
    }
    menu.querySelector(".mstart").addEventListener("click", function () {
      act("start", this);
    });
    menu.querySelector(".mstop").addEventListener("click", function () {
      act("stop", this);
    });
  })();
"""


def _file_page_html(hass, entry_id, req_path, payload, ticket):
    """文件设备悬浮窗：文件管理器（目录浏览 + 下载 + 上传 + 最近文件）。"""
    if not _snap(hass, entry_id):
        return _not_ready(entry_id)
    show_path = payload.get("path", req_path)
    if payload.get("error"):
        body = (f'{_crumbs_html(entry_id, "file", show_path, ticket)}'
                f'<div class="urlbox"><code>{_esc(payload["error"])}</code></div>')
        js = ""
    else:
        body = (f'{_crumbs_html(entry_id, "file", show_path, ticket)}'
                f'{_upload_bar_html(entry_id, show_path, ticket)}'
                f'{_file_rows_html(entry_id, "file", show_path,
                                   payload.get("rows") or [], ticket)}'
                f'{_recent_rows(payload.get("recent"))}'
                '<div class="hint">点击目录进入下一层；点击文件由 Home '
                "Assistant 从 fnOS 取回并下载。目录数据实时调用 fnOS 文件 "
                "WS API 获取。</div>")
        js = _UPLOAD_JS
    return _page_shell("飞牛文件 · 文件管理", "个人空间文件管理", body, js=js)


def _photo_page_html(hass, entry_id, payload, ticket):
    """相册设备悬浮窗：「最近添加」照片墙（recent_photos，签名实测通签）。

    缩略图/大图经 /p/api/v1/stream/*（实测免签名，仅需 fnos-token），
    由 HA 的 /fnos/web/img 视图代取转发；点击缩略图开灯箱看大图与下载原图。
    """
    if not _snap(hass, entry_id):
        return _not_ready(entry_id)
    if payload.get("error"):
        body = (f'<div class="urlbox">'
                f'<code>{_esc(payload["error"])}</code></div>')
        js = ""
    else:
        items = []
        for p in payload.get("photos") or []:
            if not isinstance(p, dict):
                continue
            thumb = (p.get("additional") or {}).get("thumbnail") or {}
            small = thumb.get("sUrl") or thumb.get("xsUrl") \
                or thumb.get("xxsUrl") or ""
            if not small:
                continue
            big = thumb.get("mUrl") or thumb.get("originalUrl") or small
            orig = thumb.get("originalUrl") or big
            name = str(p.get("fileName") or "")
            when = _fmt_phototime(
                p.get("dateTime") or p.get("createdAt") or "")
            items.append(
                f'<button class="pitem" type="button" '
                f'data-big="{_esc(_img_url(entry_id, big, ticket))}" '
                f'data-orig="{_esc(_img_url(entry_id, orig, ticket))}" '
                f'data-name="{_esc(name)}" title="{_esc(name)}">'
                f'<img loading="lazy" alt="" '
                f'src="{_esc(_img_url(entry_id, small, ticket))}">'
                f'<span class="pm">{_esc(when)}</span></button>')
        if items:
            head = f'<div class="sec">最近添加（{len(items)} 张）</div>'
            wall = f'<div class="pwall">{"".join(items)}</div>'
            lbox = f"""
<div class="lbox" id="lbox">
  <div class="lbar">
    <a class="pc" id="ldl" href="#" download>⬇ 下载原图</a>
    <button class="pc" type="button" id="lcls">关闭</button>
  </div>
  <img class="lbig" id="lbig" alt="">
  <div class="lname" id="lname"></div>
</div>"""
            hint = ('<div class="hint">数据实时调用 fnOS 相册 App '
                    "recent_photos 接口（authx 签名）；缩略图/大图由 Home "
                    "Assistant 从 fnOS 代取。点击照片放大，可下载原图。</div>")
            body = f"{head}{wall}{lbox}{hint}"
            js = _PHOTO_JS
        else:
            body = ('<div class="urlbox"><code>相册里暂时没有可显示的照片'
                    '（或缩略图尚未生成）。</code></div>')
            js = ""
    return _page_shell("飞牛相册 · 最近添加", "相册 App 最近添加的照片",
                       body, js=js)


# --------------------------------------------------------------------------- #
# 虚拟机详情页：/domain/info + /snapshot/list（实测 2026-10-18，参数用 name）
# --------------------------------------------------------------------------- #
def _fetch_vm_payload(hass, entry_id, name):
    """executor：虚拟机详情（domain/info + snapshot/list + 运行时 status）。"""
    client = _page_client(hass, entry_id)
    if client is None:
        return {"error": "fnOS 集成客户端不可用，请重载集成后重试。"}
    if not name:
        return {"error": "缺少虚拟机名。"}
    try:
        info = client.vm_info(name)
    except Exception as err:  # pylint: disable=broad-except
        return {"error": f"获取虚拟机配置失败：{err}"}
    try:
        snaps = client.vm_snapshots(name)
    except Exception:  # pylint: disable=broad-except
        snaps = []
    usage = None
    try:
        usage = client.vm_status_row(name)
    except Exception:  # pylint: disable=broad-except
        pass
    return {"info": info, "snapshots": snaps, "usage": usage}


def _fetch_vm_state(hass, entry_id, name):
    """executor：查虚拟机电源状态（vmconsole 页判断是否可直连 noVNC）。"""
    client = _page_client(hass, entry_id)
    if client is None:
        return None
    try:
        return client.vm_state(name)
    except Exception:  # pylint: disable=broad-except
        return None


def _vm_state_of(hass, entry_id, name, usage):
    """虚拟机电源状态：优先集成快照（domain/list），其次运行时行。"""
    for item in ((_snap(hass, entry_id).get("vms") or {}).get("list") or []):
        if isinstance(item, dict) and \
                str(item.get("name") or "") == str(name):
            st = str(item.get("state") or "").upper()
            if st:
                return st
    if usage:
        return "RUNNING"
    return "UNKNOWN"


def _vm_usage_vals(usage):
    """从 /domain/status 行里防御式提取 CPU/内存使用率（字段未公开）。"""
    cpu = mem = None
    for key, val in (usage or {}).items():
        low = str(key).lower()
        num = None
        if isinstance(val, (int, float)):
            num = val
        elif isinstance(val, dict):
            for sub in ("usage", "percent", "pct", "value", "val"):
                if isinstance(val.get(sub), (int, float)):
                    num = val[sub]
                    break
        if num is None:
            continue
        if cpu is None and "cpu" in low:
            cpu = num
        elif mem is None and ("mem" in low or "ram" in low):
            mem = num
    return cpu, mem


def _vm_pct(val):
    if not isinstance(val, (int, float)):
        return "—"
    return f"{val:.0f}%" if val <= 100 else f"{val:.1f}"


def _vm_disk_cap(disk):
    cap = disk.get("capacityInBytes")
    if isinstance(cap, (int, float)) and cap > 0:
        return _fmt_size(cap)
    unit = str(disk.get("capacityUnit") or "").strip()
    val = disk.get("capacity")
    if isinstance(val, (int, float)):
        return f"{val:g} {unit}".strip()
    return "—"


def _vm_disk_table(info):
    rows = []
    for i, d in enumerate(info.get("disks") or [], 1):
        if not isinstance(d, dict):
            continue
        pool = str(d.get("poolName") or d.get("pool") or "—")
        path = str(d.get("volumePath") or d.get("path") or "—")
        rows.append(
            f'<tr><td>磁盘 {i}</td><td>{_esc(str(d.get("busType") or "—"))}'
            f'</td><td>{_esc(_vm_disk_cap(d))}</td><td>{_esc(pool)}</td>'
            f'<td>{_esc(path)}</td></tr>')
    for c in info.get("cdroms") or []:
        if not isinstance(c, dict):
            continue
        path = str(c.get("volumePath") or c.get("isoPath")
                   or c.get("path") or "—")
        rows.append(
            f'<tr><td>光驱</td><td>{_esc(str(c.get("busType") or "—"))}'
            f'</td><td>—</td><td>—</td><td>{_esc(path)}</td></tr>')
    if not rows:
        return '<div class="hint">未配置磁盘。</div>'
    return ('<table class="vtab"><tr><th>设备</th><th>总线</th><th>容量</th>'
            '<th>存储池</th><th>卷路径</th></tr>' + "".join(rows) + "</table>")


def _vm_net_table(info):
    rows = []
    for n in info.get("networks") or []:
        if not isinstance(n, dict):
            continue
        rows.append(
            f'<tr><td>{_esc(str(n.get("type") or "—"))}</td>'
            f'<td>{_esc(str(n.get("source") or "—"))}</td>'
            f'<td>{_esc(str(n.get("nicModel") or "—"))}</td>'
            f'<td><code>{_esc(str(n.get("macAddr") or "—"))}</code></td></tr>')
    if not rows:
        return '<div class="hint">未配置网卡。</div>'
    return ('<table class="vtab"><tr><th>类型</th><th>桥接来源</th>'
            '<th>网卡型号</th><th>MAC</th></tr>' + "".join(rows) + "</table>")


def _vm_snap_table(snaps):
    if not snaps:
        return '<div class="hint">暂无快照。</div>'
    rows = []
    for s in snaps[:30]:
        nm = str(s.get("snapshotName") or s.get("name") or "—")
        desc = str(s.get("snapshotDesc") or s.get("desc")
                   or s.get("description") or "—")
        t = s.get("time") or s.get("ctime") or s.get("createTime") \
            or s.get("createdAt")
        t_s = _fmt_ts(t) if isinstance(t, (int, float)) else str(t or "—")
        rows.append(f"<tr><td>{_esc(nm)}</td><td>{_esc(desc)}</td>"
                    f"<td>{_esc(t_s)}</td></tr>")
    return ('<table class="vtab"><tr><th>快照</th><th>描述</th><th>时间</th>'
            '</tr>' + "".join(rows) + "</table>")


def _vm_page_html(hass, entry_id, name, payload, ticket):
    """虚拟机设备悬浮窗：基本信息/硬件配置/磁盘/网络/CPU使用率/快照 + 电源操作。

    数据实时调用 fnOS 虚拟机 App REST（/domain/info?name= 等）；CPU/内存
    使用率仅运行时可采（/domain/status 只列运行中），关机时显示"—"。
    """
    if not _snap(hass, entry_id):
        return _not_ready(entry_id)
    if payload.get("error"):
        body = (f'<div class="urlbox">'
                f'<code>{_esc(payload["error"])}</code></div>')
        return _page_shell("飞牛虚拟机", "虚拟机详情", body)
    info = payload.get("info") or {}
    usage = payload.get("usage")
    state = _vm_state_of(hass, entry_id, name, usage)
    running = state == "RUNNING"
    badge = {
        "RUNNING": '<span class="badge b-ok">运行中</span>',
        "SHUTOFF": '<span class="badge b-off">已关机</span>',
        "PAUSED": '<span class="badge b-warn">已暂停</span>',
    }.get(state, '<span class="badge b-idle">状态未知</span>')

    # 操作按钮：控制台仅运行时可开；电源操作按状态显隐
    vmctl = (f"{WEB_PAGE_PREFIX}/vmctl?entry={entry_id}"
             f"&{TICKET_PARAM}={ticket}")
    # 控制台直开专属视图（/vm/vnc/<名>，TanStack basepath=/vm 才能匹配路由）
    from urllib.parse import quote as _qq

    console_href = (f"/vm/vnc/{_qq(name, safe='')}"
                    f"?entry={entry_id}&{TICKET_PARAM}={ticket}")

    def _act(action, label):
        return (f'<button class="pc vact" type="button" data-act="{vmctl}" '
                f'data-name="{_esc(name)}" data-action="{action}">'
                f'{label}</button>')

    if running and remote_control_enabled(hass):
        console_btn = (f'<a class="btn" href="{_esc(console_href)}">'
                       f'🖥 打开控制台</a>')
        power_btns = _act("shutdown", "关机") + _act("reboot", "重启") \
            + _act("forceshutdown", "强制关机")
    elif running:
        console_btn = ('<span class="btn" style="opacity:.55" '
                       'title="远程控制开关已关闭">🖥 打开控制台</span>')
        power_btns = _act("shutdown", "关机") + _act("reboot", "重启") \
            + _act("forceshutdown", "强制关机")
    else:
        console_btn = ('<span class="btn" style="opacity:.55" '
                       'title="虚拟机未开机，无法打开控制台">🖥 打开控制台'
                       '</span>')
        power_btns = _act("start", "开机") if state != "UNKNOWN" else ""
    buttons = f'<div class="vbtns">{console_btn}{power_btns}' \
              f'<span class="vmstat" id="vmstat"></span></div>'

    os_txt = f'{info.get("osType") or "—"} {info.get("osVersion") or ""}'.strip()
    mem = info.get("memCapacityInBytes")
    cpu_cores = info.get("cpuCoreCnt")
    basics = "".join([
        _kv("状态", {"RUNNING": "运行中", "SHUTOFF": "已关机",
                     "PAUSED": "已暂停"}.get(state, "未知"), badge),
        _kv("操作系统", os_txt),
        _kv("CPU", f"{cpu_cores} 核 · {info.get('cpuMode') or '—'}"
            if cpu_cores else "—"),
        _kv("内存", _fmt_size(mem) if isinstance(mem, (int, float)) else "—"),
        _kv("芯片组", str(info.get("chipset") or "—")),
        _kv("固件", str(info.get("firmware") or "—").upper()),
        _kv("显卡", str(info.get("videoType") or "—").upper()),
        _kv("自启动", "是" if info.get("autostart") else "否"),
        _kv("Hypervisor", str(info.get("hypervisor") or "—")),
        _kv("UUID", str(info.get("uuid") or "—")),
    ])
    cpu_u, mem_u = _vm_usage_vals(usage)
    if running:
        res_kv = _kv("CPU 使用率", _vm_pct(cpu_u)) + \
            _kv("内存使用率", _vm_pct(mem_u))
        res_note = ""
    else:
        res_kv = _kv("CPU 使用率", "—") + _kv("内存使用率", "—")
        res_note = '<div class="hint">虚拟机未运行，fnOS 不采集使用率。</div>'
    n_pci = len([x for x in info.get("hostpcis") or [] if isinstance(x, dict)])
    n_usb = len([x for x in info.get("hostusbs") or [] if isinstance(x, dict)])
    if n_pci or n_usb:
        basics += _kv("PCI 直通", f"{n_pci} 项") + \
            _kv("USB 直通", f"{n_usb} 项")

    body = f"""
{buttons}
<div class="sec">基本信息与硬件配置</div>
<div class="kvs">{basics}</div>
<div class="sec">资源使用率</div>
<div class="kvs">{res_kv}</div>
{res_note}
<div class="sec">磁盘 / 光驱</div>
{_vm_disk_table(info)}
<div class="sec">网络</div>
{_vm_net_table(info)}
<div class="sec">快照</div>
{_vm_snap_table(payload.get("snapshots") or [])}
<div class="hint">数据实时调用 fnOS 虚拟机 App REST API（domain/info、
snapshot/list、domain/status）。点击「打开控制台」进入 noVNC 实时画面
（需虚拟机处于运行状态）。</div>"""
    title = str(info.get("title") or name or "虚拟机")
    return _page_shell(f"飞牛虚拟机 · {title}", "虚拟机详情", body, js=_VM_JS)


def _vm_off_html(hass, entry_id, name, ticket):
    """P3：控制台入口在虚拟机关机时的明确提示页（替代无画面的 noVNC）。"""
    vmctl = (f"{WEB_PAGE_PREFIX}/vmctl?entry={entry_id}"
             f"&{TICKET_PARAM}={ticket}")
    detail_href = _nav_url(entry_id, "vm", {"name": name}, ticket=ticket)
    body = f"""
<div class="urlbox"><code>虚拟机「{_esc(name)}」当前处于关机状态，
noVNC 控制台无画面可连接。</code></div>
<div class="vbtns">
  <button class="pc vact" type="button" data-act="{vmctl}"
          data-name="{_esc(name)}" data-action="start">▶ 开机</button>
  <a class="btn" href="{_esc(detail_href)}">查看虚拟机详情</a>
  <span class="vmstat" id="vmstat"></span>
</div>
<div class="hint">点击「开机」通过 Home Assistant 调用 fnOS 虚拟机 App 的
start 接口；开机约需数十秒，完成后回到本页刷新，再打开控制台。</div>"""
    return _page_shell("飞牛虚拟机 · 控制台", "虚拟机未开机", body, js=_VM_JS)


# --------------------------------------------------------------------------- #
# 影视页：App 状态检测 + 明确提示（App 未运行时无可取数据）
# --------------------------------------------------------------------------- #
def _video_page_html(hass, entry_id):
    """影视设备悬浮窗：影视 App 状态页（数据来自集成快照探测结果）。"""
    snap = _snap(hass, entry_id)
    if not snap:
        return _not_ready(entry_id)
    apps = snap.get("apps") or {}
    installed = any(
        str(a.get("app") or "") in ("trim.media", "trim.movie", "trim.video")
        for a in snap.get("apps_installed") or [])
    available = bool(apps.get("video"))
    if available:
        tip = ("飞牛影视 App 正在运行。影视 App 的媒体库 API 未公开"
               "（App 内部接口），悬浮窗暂不支持浏览媒体库；"
               "请在 fnOS 桌面或飞牛 TV/手机端使用影视。")
        state_badge = '<span class="badge b-ok">运行中</span>'
    elif installed:
        tip = ("飞牛影视 App 已安装但当前未运行，无法获取媒体库数据。"
               "请在 fnOS 桌面打开一次影视 App（或到应用中心启动），"
               "然后重新打开本悬浮窗。")
        state_badge = '<span class="badge b-off">已安装 · 未运行</span>'
    else:
        tip = ("未检测到飞牛影视 App。请到 fnOS 应用中心安装「飞牛影视」后"
               "重新打开本悬浮窗。")
        state_badge = '<span class="badge b-off">未安装</span>'
    body = f"""
<div class="urlbox"><code>飞牛影视 {state_badge}</code></div>
<div class="ports">状态检测方式：Home Assistant 定期探测 fnOS 影视服务端口，
并核对应用中心已装应用列表。</div>
<div class="hint">{_esc(tip)}</div>"""
    return _page_shell("飞牛影视", "应用状态", body)



def _docker_landing_html(hass, entry_id, ticket, container_name):
    """Docker 容器设备悬浮窗：该容器映射端口组成的访问链接。"""
    prefix = f"{PROXY_PREFIX}/{entry_id}"
    t = f"{TICKET_PARAM}={ticket}"
    host_ip = None
    store = hass.data.get(DOMAIN) or {}
    coord = (store.get(entry_id) or {}).get("coordinator")
    if coord is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        host_ip = entry.data.get("host") if entry else None
        rows = ((coord.data or {}).get("docker") or {}).get("containers") or []
        row = next((c for c in rows if c.get("name") == container_name), None)
    else:
        row = None

    ports = []
    if row:
        for p in row.get("ports") or []:
            if isinstance(p, dict) and p.get("PublicPort"):
                ports.append((p.get("PublicPort"), p.get("PrivatePort"),
                              p.get("Type") or "tcp"))
    running = str((row or {}).get("state") or "").lower() == "running"

    def _esc(v):
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(
            '"', "&quot;")

    name = _esc(container_name)
    docker_page = f"{prefix}/apps/docker?{t}"
    if ports:
        # 列表形式：每个映射端口一行，点击整行打开该 ip:port 链接
        links = []
        for pub_, priv_, typ_ in ports:
            u = f"http://{host_ip}:{pub_}"
            links.append(
                f'<a class="prow" href="{_esc(u)}" target="_blank" '
                f'rel="noopener">'
                f'<span class="purl">{_esc(u)}</span>'
                f'<span class="pmeta">{typ_.upper()} · 主机 {pub_} → 容器 {priv_}</span>'
                f'<button class="pc" type="button" data-url="{_esc(u)}">复制</button>'
                f'</a>')
        body = f"""
<div class="plist">{"".join(links)}</div>
<div class="hint">链接由该容器映射到 fnOS 主机（{_esc(host_ip)}）的端口组成，
与 fnOS Docker 应用中配置一致。点击整行在新标签页打开对应服务；容器应用若禁止被网页内嵌，
请用浏览器直接访问。点击「复制」可复制该行链接。</div>"""
        copy_js = (
            'document.querySelectorAll(".pc").forEach(function(b){'
            'b.addEventListener("click",function(ev){'
            'ev.preventDefault();ev.stopPropagation();copyUrl(b,b.dataset.url);});})')
    else:
        state_txt = "运行中" if running else "未运行"
        body = f"""
<div class="urlbox"><code>该容器没有映射到主机的端口，无法生成访问链接。</code></div>
<div class="ports">当前状态：<b>{state_txt}</b>。请在 fnOS「Docker 管理」中为容器
配置端口映射后重试。</div>
<div class="hint">也可以直接到 Docker 管理页查看该容器：</div>
<div class="urlbox"><a href="{docker_page}" target="_blank" rel="noopener">打开 Docker 管理</a></div>"""
        copy_js = ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Docker · {name}</title><style>{_PAGE_CSS}</style></head>
<body><div class="card">
<h1>Docker · {name}</h1>
<div class="sub">{_esc((row or {}).get('image') or '容器')}</div>
{body}
</div><script>{_PAGE_JS}{copy_js}</script></body></html>"""


# --------------------------------------------------------------------------- #
# 悬浮窗内容页渲染（Web 入口页与票据导航页共用）
# --------------------------------------------------------------------------- #
async def _render_target_page(hass, entry_id, target, query):
    """按设备类型渲染悬浮窗内容页（数据全部来自 fnOS API / 集成快照）：

    * target=host → 系统总览仪表盘 + 已装应用网格（集成快照 + 应用中心 API）；
    * target=file → 文件管理器（file.ls / file.recent.list WS API）；
    * target=photo → 相册「最近添加」照片墙（recent_photos，authx 签名）；
    * target=video → 影视 App 状态页（快照探测结果）；
    * target=docker&name=<容器> → 该容器 ip:port 访问链接页；
    * target=vm&name=<虚拟机> → 虚拟机详情页（domain/info + snapshot/list）；
    * target=vmconsole&name=<虚拟机> → noVNC 实时控制台（唯一回源页，
      关机时改出提示页，避免"连接失败"的无画面黑屏）。
    """
    from urllib.parse import unquote

    def html_page(body):
        return web.Response(text=body, content_type="text/html",
                            charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    ticket = _issue_ticket(hass, entry_id)

    # ---- HA 渲染内容页 ----
    if target == "host":
        return html_page(_host_page_html(hass, entry_id, ticket))
    if target == "video":
        return html_page(_video_page_html(hass, entry_id))
    if target == "file":
        req_path = unquote(query.get("path") or "") or None
        payload = await hass.async_add_executor_job(
            _fetch_browse_payload, hass, entry_id, target, req_path)
        return html_page(
            _file_page_html(hass, entry_id, req_path, payload, ticket))
    if target == "photo":
        payload = await hass.async_add_executor_job(
            _fetch_photo_payload, hass, entry_id)
        return html_page(_photo_page_html(hass, entry_id, payload, ticket))
    if target == "docker":
        name = unquote(query.get("name") or "")
        if not name:
            return web.Response(
                text="缺少容器名参数。", status=400,
                content_type="text/plain", charset="utf-8")
        return html_page(
            _docker_landing_html(hass, entry_id, ticket, name))
    if target not in ("vm", "vmconsole"):
        return web.Response(
            text=f"未知的悬浮窗类型：{target}", status=400,
            content_type="text/plain", charset="utf-8")

    if target == "vmconsole" and not remote_control_enabled(hass):
        return html_page(_page_shell(
            "飞牛虚拟机 · 远程控制已关闭", "远程控制已关闭",
            '<div class="urlbox"><code>集成中的「远程控制」开关处于关闭状态，'
            '虚拟机控制台（noVNC）已停用。<br>请在 Home Assistant 中打开'
            '「远程控制」开关后重试。</code></div>'))

    # ---- VM：详情页（HA 渲染）/ noVNC 控制台（唯一回源页）----
    vm_name = unquote(query.get("name") or "")
    if not vm_name:
        return web.Response(
            text="缺少虚拟机名参数。", status=400,
            content_type="text/plain", charset="utf-8")
    if target == "vm":
        payload = await hass.async_add_executor_job(
            _fetch_vm_payload, hass, entry_id, vm_name)
        return html_page(
            _vm_page_html(hass, entry_id, vm_name, payload, ticket))

    # vmconsole：关机状态下 noVNC 必然连不上，先给提示页（可一键开机）；
    # 运行中 302 到专属控制台视图（/vm/vnc/<名>，SPA 路由 basepath=/vm）
    state = await hass.async_add_executor_job(
        _fetch_vm_state, hass, entry_id, vm_name)
    if state == "SHUTOFF":
        return html_page(_vm_off_html(hass, entry_id, vm_name, ticket))
    from urllib.parse import quote as _q

    raise web.HTTPFound(
        f"/vm/vnc/{_q(vm_name, safe='')}"
        f"?entry={entry_id}&{TICKET_PARAM}={ticket}")


# --------------------------------------------------------------------------- #
# View 1：设备「访问」入口页（authSig 保护）/ 页内票据导航页
# --------------------------------------------------------------------------- #
class FnOsWebPageView(HomeAssistantView):
    """悬浮窗 iframe 实际加载的入口页（设备链接入口，HA 标准 authSig 保护）。"""

    requires_auth = True
    url = WEB_PAGE_PREFIX
    name = "fnos:web"

    async def get(self, request):
        hass = request.app["hass"]
        entry_id = request.query.get("entry", "")
        if not hass.config_entries.async_get_entry(entry_id):
            return web.Response(text="fnOS 集成条目不存在或已卸载。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        target = request.query.get("target") or "host"
        return await _render_target_page(hass, entry_id, target,
                                         request.query)


class FnOsNavView(HomeAssistantView):
    """悬浮窗页内导航（文件/相册子目录跳转等）。

    ``requires_auth=False``：authSig 对含中文/百分号编码的 query 校验存在
    兼容问题（文件窗无法进入子目录、相册页 401 的根因），页内导航改用
    代理票据鉴权——票据仅由鉴权通过的内容页签发，风险面与 /fnos/proxy 一致。
    """

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/nav"
    name = "fnos:web_nav"

    async def get(self, request):
        hass = request.app["hass"]
        entry_id = request.query.get("entry", "")
        # 先校验票据再查条目：不向未授权请求泄露条目是否存在
        if not _check_ticket(hass, entry_id, request):
            return web.Response(
                text="fnOS 悬浮窗票据缺失或已失效，请从设备页重新打开。",
                status=401, content_type="text/plain", charset="utf-8")
        if not hass.config_entries.async_get_entry(entry_id):
            return web.Response(text="fnOS 集成条目不存在或已卸载。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        target = request.query.get("target") or "host"
        return await _render_target_page(hass, entry_id, target,
                                         request.query)


# --------------------------------------------------------------------------- #
# View 2：反向代理（全方法 + WebSocket 升级）
# --------------------------------------------------------------------------- #
class FnOsProxyView(HomeAssistantView):
    """fnOS Web 全站反代：``/fnos/proxy/<entry_id>/{path}``。

    ``requires_auth=False``：页面内补丁无法给每个请求带 Bearer，鉴权由
    ``_check_ticket`` 自行校验（与 esxi_stats 光盘接口自行验签同一思路）。
    """

    requires_auth = False
    url = PROXY_PREFIX + "/{entry_id}" + "/{path:.*}"
    name = "fnos:proxy"

    async def _handle(self, request, entry_id, path):
        hass = request.app["hass"]
        upstream = _upstream_base(hass, entry_id)
        if upstream is None:
            return web.json_response({"error": "fnOS 集成条目不存在"}, status=404)

        if (request.headers.get("Upgrade", "").lower() == "websocket"):
            return await self._handle_ws(hass, request, entry_id, upstream, path)

        # 远程控制总开关关闭时：虚拟机控制台通道一律拒绝（含 VNC REST 调用）
        if _is_vm_remote_path(path) and not remote_control_enabled(hass):
            return web.json_response(
                {"error": "fnOS 远程控制已关闭，请打开「远程控制」开关"}, status=403)

        if not _is_exempt(f"/{path}") and not _check_ticket(hass, entry_id, request):
            return web.json_response(
                {"error": "fnOS 代理票据缺失或已失效，请从设备页重新打开"},
                status=401)

        session = await _get_session(hass)
        headers = _with_fnos_cookie(
            _clean_headers(request.headers), _upstream_token(hass, entry_id))
        query = request.query.copy()
        query.pop(TICKET_PARAM, None)
        url = f"{upstream}/{path}"
        if query:
            from urllib.parse import urlencode

            url += "?" + urlencode(list(query.items()))
        ssl_arg = _upstream_ssl(hass) if upstream.startswith("https") else None

        try:
            async with session.request(
                request.method, url, headers=headers,
                data=request.content if request.can_read_body else None,
                ssl=ssl_arg, allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400 and resp.headers.get("Location"):
                    headers_out = _clean_headers(resp.headers)
                    headers_out["Location"] = _rewrite_location(
                        hass, entry_id, resp.headers["Location"])
                    return web.Response(status=resp.status,
                                        headers=headers_out)
                if "text/html" in (resp.headers.get("Content-Type") or ""):
                    # 经反代命中的 HTML 只可能是补丁外的整页加载：
                    # 没有有效票据一律拒绝，绝不为未授权请求签发新票据
                    if not _check_ticket(hass, entry_id, request):
                        return web.json_response(
                            {"error": "fnOS 代理票据缺失或已失效，"
                                      "请从设备页重新打开"},
                            status=401)
                    html_text = await resp.text(errors="replace")
                    # Content-Type 必须从透传头中剔除：web.Response 同时给
                    # content_type 参数与 headers 里的 Content-Type 会让
                    # aiohttp 抛 ValueError → 500（磁贴整页跳转白屏的根因）
                    headers_out = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in _HOP_HEADERS
                        and k.lower() != "content-type"}
                    return web.Response(
                        status=resp.status,
                        headers=headers_out,
                        text=_rewrite_html(
                            hass, entry_id, html_text,
                            request.query.get(TICKET_PARAM, "")),
                        content_type="text/html", charset="utf-8")
                stream = web.StreamResponse(
                    status=resp.status, headers=_clean_headers(resp.headers))
                await stream.prepare(request)
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    await stream.write(chunk)
                await stream.write_eof()
                return stream
        except aiohttp.ClientError as err:
            return web.json_response(
                {"error": f"fnOS 连接失败：{err}"}, status=502)
        except ConnectionResetError:
            return web.Response(status=499)

    async def _handle_ws(self, hass, request, entry_id, upstream, path):
        """WebSocket 双向透传（三级兼容读取协议头；关闭消息用 bytes）。"""
        if not _is_exempt(f"/{path}") and not _check_ticket(hass, entry_id, request):
            return web.json_response({"error": "fnOS 代理票据无效"}, status=401)

        # 远程控制关断后的新连接一律不建立（noVNC 走 /vm/api/v1/vnc/…）
        remote_channel = _is_vm_remote_path(path)
        if remote_channel and not remote_control_enabled(hass):
            return web.json_response(
                {"error": "fnOS 远程控制已关闭，请打开「远程控制」开关"}, status=403)

        ws_server = web.WebSocketResponse(autoping=True)
        prepared = await ws_server.prepare(request)
        # aiohttp 3.13 的 prepare() 返回 WebSocketWriter：其 .protocol 是底层
        # RequestHandler（连接处理器）而非子协议——绝不能取！子协议在
        # ws_protocol；且仅接受 str，其他一律视为未协商
        subprotocol = getattr(prepared, "ws_protocol", None)
        if not isinstance(subprotocol, str):
            subprotocol = None
        protocols = [subprotocol] if subprotocol else None

        query = request.query.copy()
        query.pop(TICKET_PARAM, None)
        scheme = "wss" if upstream.startswith("https") else "ws"
        url = f"{scheme}://{upstream.split('://', 1)[1]}/{path}"
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())

        session = await _get_session(hass)
        # noVNC 的 WS（/vm/api/v1/vnc/<name>）同样需要 fnos-token cookie
        ws_headers = _with_fnos_cookie({}, _upstream_token(hass, entry_id))
        try:
            ws_client = await session.ws_connect(
                url, protocols=protocols,
                ssl=_upstream_ssl(hass) if scheme == "wss" else None,
                autoping=True, heartbeat=None,
                headers=ws_headers if ws_headers.get("Cookie") else None,
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("fnOS WS 上游连接失败：%s（url=%s, protocols=%r）",
                            err, url, protocols, exc_info=True)
            await ws_server.close(code=web.WSCloseCode.INTERNAL_ERROR,
                                  message=b"upstream unavailable")
            return ws_server

        # 登记活动控制台连接：开关关闭时按此表立即切断
        remote_state = None
        remote_key = str(id(ws_server))
        if remote_channel:
            remote_state = _remote_state(hass)
            remote_state["sessions"][remote_key] = (ws_server, ws_client)
            notify_remote_control(hass)
            # 竞态兜底：开关正好在握手期间被关闭
            if not remote_state["enabled"]:
                remote_state["sessions"].pop(remote_key, None)
                await ws_client.close()
                await ws_server.close(code=web.WSCloseCode.POLICY_VIOLATION,
                                      message=b"remote control disabled")
                return ws_server

        async def _pump(src, dst):
            try:
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PING:
                        await dst.ping(msg.data)
                    elif msg.type == aiohttp.WSMsgType.PONG:
                        await dst.pong(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                      aiohttp.WSMsgType.CLOSING,
                                      aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.ERROR):
                        break
            except (ConnectionResetError, asyncio.CancelledError,
                    RuntimeError):
                pass

        try:
            await asyncio.gather(
                _pump(ws_client, ws_server),
                _pump(ws_server, ws_client),
                return_exceptions=True,
            )
        finally:
            if remote_state is not None:
                remote_state["sessions"].pop(remote_key, None)
                notify_remote_control(hass)
            await ws_client.close()
            await ws_server.close(code=web.WSCloseCode.OK, message=b"")
        return ws_server

    async def get(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def post(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def put(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def delete(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def patch(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def head(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)

    async def options(self, request, entry_id, path=""):
        return await self._handle(request, entry_id, path)


class FnOsProxyRootView(FnOsProxyView):
    """``/fnos/proxy/<entry_id>``（无尾随路径）。"""

    url = PROXY_PREFIX + "/{entry_id}"
    name = "fnos:proxy_root"

    async def get(self, request, entry_id):
        return await self._handle(request, entry_id, "")


# --------------------------------------------------------------------------- #
# View 3：悬浮窗前端模块（add_extra_js_url 注入）
# --------------------------------------------------------------------------- #
class FnOsEmbedView(HomeAssistantView):
    """提供 fnos-embed.js（不含敏感信息，允许缓存）。"""

    requires_auth = False
    url = EMBED_JS_URL
    name = "fnos:embed_js"

    async def get(self, request):
        hass = request.app["hass"]
        try:
            body = await hass.async_add_executor_job(_read_embed)
        except OSError:
            return web.Response(status=404, text="// fnos-embed.js missing")
        return web.Response(
            body=body.encode("utf-8"),
            content_type="application/javascript",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


def _read_embed():
    with open(_EMBED_JS, "r", encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- #
# View 4-6：文件下载 / 文件上传 / 应用启停（票据鉴权，HA 服务端经公开 API 转发）
# --------------------------------------------------------------------------- #
def _ticket_guard(hass, request):
    """公共票据校验；通过返回 None，否则返回 401 响应。"""
    entry_id = request.query.get("entry", "")
    if _check_ticket(hass, entry_id, request):
        return None
    return web.Response(
        text="fnOS 悬浮窗票据缺失或已失效，请从设备页重新打开。",
        status=401, content_type="text/plain", charset="utf-8")


async def _img_guard(hass, request):
    """相册图片流鉴权：悬浮窗票据 **或** HA 官方签名链接（二选一）。

    「最近添加」传感器属性里的原图地址用 authSig 签名（24h、传感器轮询
    自动重签），没有短期票据，故此视图额外接受官方签名校验。
    """
    entry_id = request.query.get("entry", "")
    if _check_ticket(hass, entry_id, request):
        return None
    try:
        from homeassistant.components.http.auth import (
            async_validate_signed_path)

        signed = request.path
        if request.query_string:
            signed += "?" + request.query_string
        result = async_validate_signed_path(hass, signed)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            return None
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("fnOS 图片流签名校验异常", exc_info=True)
    return web.Response(
        text="fnOS 图片流鉴权失败，请从设备页重新打开。",
        status=401, content_type="text/plain", charset="utf-8")


class FnOsDownloadView(HomeAssistantView):
    """文件下载：HA 调 fnOS /multiple-download 取回文件流转发给浏览器。

    实测协议（2026-10-18）：POST /multiple-download
    {token, paths:[全路径], downloadLimit:2, noZipWhenSingleFile:true,
    zipFileName} → 200 {token:<dl-token>} → GET /multiple-download?token=…
    返回文件流（单文件原样，多文件/目录自动 zip）。免 Trim-Sign。
    """

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/dl{fpath:/.+}"
    name = "fnos:web_dl"

    # HA 注入 **match_info（helpers/http.py handle），路径组必须出现在签名里
    async def get(self, request, fpath=""):
        hass = request.app["hass"]
        resp = _ticket_guard(hass, request)
        if resp is not None:
            return resp
        entry_id = request.query.get("entry", "")
        from urllib.parse import unquote, quote as urlquote

        # 文件路径走路径段（?path=/xxx 会被 security_filter 过滤）
        path = request.match_info.get("fpath") or ""
        name = unquote(request.query.get("name") or "download.bin")
        if not path:
            return web.Response(text="缺少 path 参数。", status=400,
                                content_type="text/plain", charset="utf-8")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return web.Response(text="fnOS 集成条目不存在或已卸载。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        upstream = _upstream_base(hass, entry_id)
        ssl_arg = _upstream_ssl(hass) if upstream.startswith("https") else None
        session = await _get_session(hass)

        try:
            async with session.post(
                f"{upstream}/multiple-download", ssl=ssl_arg,
                json={"token": _upstream_token(hass, entry_id),
                      "paths": [path], "downloadLimit": 2,
                      "noZipWhenSingleFile": True, "zipFileName": name},
                cookies={"fnos-token": _upstream_token(hass, entry_id)},
            ) as resp:
                apply_data = await resp.json(content_type=None)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("申请 fnOS 下载失败：%s", path)
            return web.Response(text="连接 fnOS 失败。", status=502,
                                content_type="text/plain", charset="utf-8")
        dl_token = (apply_data or {}).get("token")
        if not dl_token:
            return web.Response(
                text=f"fnOS 拒绝下载请求：{_esc(apply_data)[:200]}",
                status=502, content_type="text/plain", charset="utf-8")
        try:
            upstream_resp = await session.get(
                f"{upstream}/multiple-download",
                params={"token": dl_token}, ssl=ssl_arg,
                cookies={"fnos-token": _upstream_token(hass, entry_id)})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("拉取 fnOS 文件失败：%s", path)
            return web.Response(text="连接 fnOS 失败。", status=502,
                                content_type="text/plain", charset="utf-8")
        if upstream_resp.status != 200:
            upstream_resp.release()
            return web.Response(
                text=f"fnOS 返回 {upstream_resp.status}，下载失败。",
                status=502, content_type="text/plain", charset="utf-8")

        out = web.StreamResponse(status=200)
        out.headers["Content-Type"] = upstream_resp.headers.get(
            "Content-Type", "application/octet-stream")
        out.headers["Content-Disposition"] = (
            "attachment; filename*=UTF-8''" + urlquote(str(name)))
        length = upstream_resp.headers.get("Content-Length")
        if length:
            out.headers["Content-Length"] = length
        await out.prepare(request)
        try:
            async for chunk in upstream_resp.content.iter_chunked(_CHUNK):
                await out.write(chunk)
            await out.write_eof()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("转发 fnOS 文件中断：%s", path)
        finally:
            upstream_resp.release()
        return out


class FnOsVncConsoleView(HomeAssistantView):
    """虚拟机 noVNC 控制台页：/vm/vnc/{name}。

    必须从 /vm 前缀的路径伺服：控制台 SPA 用 TanStack Router（basepath=/vm），
    只有 pathname 以 /vm 开头才能匹配 /vnc/$vncName 路由，否则页内渲染 404。
    ``requires_auth=False``，票据自校验；改写时注入 fnos-token cookie
    （SPA 的 vdt() 从 cookie 读 token 走 authToken 鉴权，无 cookie 会跳 /login）。
    """

    requires_auth = False
    url = "/vm/vnc/{name}"
    name = "fnos:web_vnc"

    # HA 注入 **match_info（helpers/http.py handle），name 组必须出现在签名里
    async def get(self, request, name=""):
        hass = request.app["hass"]
        resp = _ticket_guard(hass, request)
        if resp is not None:
            return resp
        entry_id = request.query.get("entry", "")
        from urllib.parse import quote

        name = request.match_info.get("name", "")
        if not name:
            return web.Response(text="缺少虚拟机名参数。", status=400,
                                content_type="text/plain", charset="utf-8")
        if not hass.config_entries.async_get_entry(entry_id):
            return web.Response(text="fnOS 集成条目不存在或已卸载。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        if not remote_control_enabled(hass):
            return web.Response(
                text="fnOS 远程控制已关闭，请先打开「远程控制」开关。",
                status=403, content_type="text/plain", charset="utf-8")
        upstream = _upstream_base(hass, entry_id)
        if upstream is None:
            return web.Response(text="fnOS 集成条目配置缺失。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        upstream_path = f"/vm/vnc/{quote(name, safe='')}"
        try:
            session = await _get_session(hass)
            async with session.get(
                f"{upstream}{upstream_path}", ssl=_upstream_ssl(hass)
                if upstream.startswith("https") else None,
                headers={"Accept-Encoding": "identity",
                         "Cookie": _with_fnos_cookie({}, _upstream_token(
                             hass, entry_id)).get("Cookie", "")},
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return web.Response(
                        text="无法打开虚拟机控制台：请确认虚拟机 App 已安装、"
                             "虚拟机存在且 fnOS 凭据有效。",
                        status=502, content_type="text/plain",
                        charset="utf-8")
                html_text = await resp.text(errors="replace")
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("拉取 fnOS 页面失败：%s%s", upstream,
                              upstream_path)
            return web.Response(
                text="无法连接 fnOS Web，请确认 fnOS 主机在线。",
                status=502, content_type="text/plain", charset="utf-8")
        return web.Response(
            text=_rewrite_html(hass, entry_id, html_text,
                               request.query.get(TICKET_PARAM, ""),
                               fnos_token=_upstream_token(hass, entry_id)),
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )


class FnOsImageView(HomeAssistantView):
    """相册缩略图/原图流代理：/fnos/web/img/p/api/v1/stream/...

    流路径取自路径段（security_filter 拦截 ?u=/path 查询形态）。
    缩略图流端点实测免签名（2026-10-18，仅需 fnos-token cookie），
    HA 带 token 代取；票据校验与下载视图同一套 _ticket_guard。
    """

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/img{u_path:.+}"
    name = "fnos:web_img"

    # HA 注入 **match_info（helpers/http.py handle），u_path 组必须出现在签名里
    async def get(self, request, u_path=""):
        hass = request.app["hass"]
        resp = await _img_guard(hass, request)
        if resp is not None:
            return resp
        entry_id = request.query.get("entry", "")

        upstream_path = request.match_info.get("u_path") or ""
        if not upstream_path.startswith("/p/api/v1/stream/"):
            return web.Response(text="仅允许转发 fnOS 相册流。",
                                status=400, content_type="text/plain",
                                charset="utf-8")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return web.Response(text="fnOS 集成条目不存在或已卸载。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        upstream = _upstream_base(hass, entry_id)
        if upstream is None:
            return web.Response(text="fnOS 集成条目配置缺失。",
                                status=404, content_type="text/plain",
                                charset="utf-8")
        session = await _get_session(hass)
        try:
            upstream_resp = await session.get(
                f"{upstream}{upstream_path}", ssl=_upstream_ssl(hass)
                if upstream.startswith("https") else None,
                cookies={"fnos-token": _upstream_token(hass, entry_id)},
                timeout=aiohttp.ClientTimeout(total=30))
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("拉取 fnOS 相册流失败：%s", upstream_path)
            return web.Response(status=502)
        ctype = upstream_resp.headers.get("Content-Type", "")
        if upstream_resp.status != 200 or "text/html" in ctype:
            # 上游 404/重定向到登录页等：一律按失败处理，别把 HTML 当图片回
            upstream_resp.release()
            return web.Response(status=502)
        out = web.StreamResponse(status=200)
        out.headers["Content-Type"] = ctype or "image/jpeg"
        length = upstream_resp.headers.get("Content-Length")
        if length:
            out.headers["Content-Length"] = length
        out.headers["Cache-Control"] = "private, max-age=3600"
        await out.prepare(request)
        try:
            async for chunk in upstream_resp.content.iter_chunked(_CHUNK):
                await out.write(chunk)
            await out.write_eof()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("转发 fnOS 相册流中断：%s", upstream_path)
        finally:
            upstream_resp.release()
        return out


class FnOsUploadView(HomeAssistantView):
    """文件上传：浏览器 multipart → HA → fnOS file.checkUpload + /upload。

    实测协议（2026-10-18）：
      1) WS file.checkUpload {size, path:<完整目标路径含文件名>, overwrite:true}
         → {uploadName:"xxx.~#N", result:"succ"}（服务端传完自动改名）；
      2) POST /upload multipart 字段 trim-upload-file，headers：
         Trim-Path=encodeURI(<dir>/<uploadName> 临时路径)、Trim-From:0、
         Trim-Overwrite、Trim-Mtim(秒)、Trim-Token、
         Trim-Sign=HMAC-SHA256(Trim-Path 文本, sign_key)→base64。
    """

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/up"
    name = "fnos:web_up"

    async def post(self, request):
        hass = request.app["hass"]
        resp = _ticket_guard(hass, request)
        if resp is not None:
            return web.json_response({"ok": False,
                                      "error": resp.text}, status=401)
        entry_id = request.query.get("entry", "")
        from urllib.parse import unquote

        dir_path = (unquote(request.query.get("path") or "") or "").rstrip("/")
        if not dir_path:
            return web.json_response({"ok": False, "error": "缺少 path 参数"},
                                     status=400)
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return web.json_response({"ok": False, "error": "集成条目不存在"},
                                     status=404)
        coord = ((hass.data.get(DOMAIN) or {}).get(entry_id) or {}).get(
            "coordinator")
        client = getattr(coord, "client", None)
        if client is None:
            return web.json_response({"ok": False, "error": "fnOS 未连接"},
                                     status=503)

        reader = await request.multipart()
        field = None
        while field is None:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                field = part
        if field is None:
            return web.json_response({"ok": False, "error": "缺少 file 字段"},
                                     status=400)
        fname = field.filename or "upload.bin"
        data = await field.read(decode=False)

        # 1) checkUpload 拿临时名（阻塞 WS，放 executor）
        def _check():
            return client.payload(
                "file.checkUpload", size=len(data),
                path=f"{dir_path}/{fname}", overwrite=True)

        try:
            chk = await hass.async_add_executor_job(_check)
        except Exception as err:  # pylint: disable=broad-except
            return web.json_response({"ok": False, "error": str(err)},
                                     status=502)
        upload_name = (chk or {}).get("uploadName")
        if not upload_name:
            return web.json_response(
                {"ok": False,
                 "error": f"fnOS 拒绝上传（checkUpload 无 uploadName）"},
                status=502)

        # 2) 转发到 fnOS /upload（Trim-Sign 与桌面 SPA 的 mz() 一致）
        trim_path = _js_encode_uri(f"{dir_path}/{upload_name}")
        token = _upstream_token(hass, entry_id)
        form = aiohttp.FormData()
        form.add_field("trim-upload-file", data, filename=upload_name,
                       content_type="application/octet-stream")
        headers = {
            "Trim-Path": trim_path,
            "Trim-From": "0",
            "Trim-Overwrite": "true",
            "Trim-Mtim": str(int(time.time())),
            "Trim-Token": token or "",
            "Trim-Sign": _trim_sign(client, trim_path),
        }
        upstream = _upstream_base(hass, entry_id)
        ssl_arg = _upstream_ssl(hass) if upstream.startswith("https") else None
        session = await _get_session(hass)

        def _cleanup_tmp():
            # 传失败时 fnOS 不会删 .~#N 临时占位文件（实测残留），
            # 这里直接 file.rm 硬删（不进回收站）
            try:
                client.payload("file.rm",
                               files=[f"{dir_path}/{upload_name}"],
                               moveToTrashbin=False)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.warning("清理 fnOS 上传临时文件失败：%s/%s",
                                dir_path, upload_name, exc_info=True)

        try:
            async with session.post(f"{upstream}/upload", data=form,
                                    headers=headers, ssl=ssl_arg) as resp:
                body_text = await resp.text(errors="replace")
                if resp.status == 200:
                    return web.json_response({"ok": True})
                await hass.async_add_executor_job(_cleanup_tmp)
                return web.json_response(
                    {"ok": False,
                     "error": f"fnOS HTTP {resp.status}: {body_text[:160]}"},
                    status=502)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("转发 fnOS 上传失败：%s/%s", dir_path, fname)
            await hass.async_add_executor_job(_cleanup_tmp)
            return web.json_response({"ok": False, "error": str(err)},
                                     status=502)


class FnOsAppCtlView(HomeAssistantView):
    """应用启停：POST app=<应用标识>&action=start|stop → client.app_action。"""

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/appctl"
    name = "fnos:web_appctl"

    async def post(self, request):
        hass = request.app["hass"]
        resp = _ticket_guard(hass, request)
        if resp is not None:
            return web.json_response({"ok": False, "error": resp.text},
                                     status=401)
        entry_id = request.query.get("entry", "")
        form = await request.post()
        app_name = str(form.get("app") or "")
        action = str(form.get("action") or "")
        if not app_name or action not in ("start", "stop"):
            return web.json_response({"ok": False, "error": "参数错误"},
                                     status=400)
        if not hass.config_entries.async_get_entry(entry_id):
            return web.json_response({"ok": False, "error": "集成条目不存在"},
                                     status=404)
        client = _client(hass, entry_id)
        if client is None:
            return web.json_response({"ok": False, "error": "fnOS 未连接"},
                                     status=503)
        try:
            await hass.async_add_executor_job(
                client.app_action, app_name, action)
        except Exception as err:  # pylint: disable=broad-except
            return web.json_response({"ok": False, "error": str(err)},
                                     status=502)
        return web.json_response({"ok": True})


class FnOsVmCtlView(HomeAssistantView):
    """虚拟机电源操作：POST name=<虚拟机名>&action=start|shutdown|
    forceshutdown|reboot → client.vm_action（REST，实测 2026-10-18）。"""

    requires_auth = False
    url = WEB_PAGE_PREFIX + "/vmctl"
    name = "fnos:web_vmctl"

    async def post(self, request):
        hass = request.app["hass"]
        resp = _ticket_guard(hass, request)
        if resp is not None:
            return web.json_response({"ok": False, "error": resp.text},
                                     status=401)
        entry_id = request.query.get("entry", "")
        form = await request.post()
        vm_name = str(form.get("name") or "")
        action = str(form.get("action") or "")
        if not vm_name or action not in ("start", "shutdown",
                                         "forceshutdown", "reboot"):
            return web.json_response({"ok": False, "error": "参数错误"},
                                     status=400)
        if not hass.config_entries.async_get_entry(entry_id):
            return web.json_response({"ok": False, "error": "集成条目不存在"},
                                     status=404)
        client = _client(hass, entry_id)
        if client is None:
            return web.json_response({"ok": False, "error": "fnOS 未连接"},
                                     status=503)
        try:
            await hass.async_add_executor_job(
                client.vm_action, vm_name, action)
        except Exception as err:  # pylint: disable=broad-except
            return web.json_response({"ok": False, "error": str(err)},
                                     status=502)
        return web.json_response({"ok": True})


async def async_setup_proxy(hass):
    """全局注册一次反代相关视图 + 前端模块注入。

    组件级 setup 与 entry setup 都可能调用（幂等）；重复注册同一 resource
    会触发 aiohttp_cors 的 OPTIONS 冲突，逐个 register 包 ValueError 兜底
    （冲突意味着视图已在，功能不受影响）。
    """
    store = _proxy_store(hass)
    if store.get("views_registered"):
        return
    store["views_registered"] = True

    for view in (FnOsWebPageView, FnOsNavView, FnOsProxyView,
                 FnOsProxyRootView, FnOsEmbedView,
                 FnOsDownloadView, FnOsImageView, FnOsUploadView,
                 FnOsAppCtlView, FnOsVmCtlView, FnOsVncConsoleView):
        try:
            hass.http.register_view(view)
        except ValueError:
            # resource 已注册过（entry 重试 setup），直接复用现有视图
            _LOGGER.debug("fnOS 视图 %s 已注册，跳过", type(view).__name__)

    try:
        from homeassistant.components import frontend

        frontend.add_extra_js_url(hass, EMBED_JS_URL)
        store["embed_url"] = EMBED_JS_URL
    except Exception:  # pylint: disable=broad-except
        _LOGGER.warning("注册 fnOS 悬浮窗前端模块失败（不影响独立页面访问）",
                        exc_info=True)

    async def _on_stop(_event):
        if store.get("embed_url"):
            try:
                from homeassistant.components import frontend

                frontend.remove_extra_js_url(hass, store["embed_url"])
            except Exception:  # pylint: disable=broad-except
                pass
        session = store.get("session")
        if session is not None and not session.closed:
            await session.close()
        for client in (store.get("page_clients") or {}).values():
            try:
                client.close()
            except Exception:  # pylint: disable=broad-except
                pass

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    _LOGGER.debug("fnOS 反代视图已注册")
