"""飞牛 fnOS WebSocket API 客户端（同步，供 executor 调用）。

协议要点（实测见《飞牛fnOS_API完整清单.md》）：
  * ws://host:port/websocket?type=main 一条连接承载全部请求；
  * 登录体 AES-256-CBC(PKCS7) 加密 → aes 字段，AES key 经 RSA(PKCS1_v1_5) 公钥
    加密 → rsa 字段，IV 随机；
  * 登录响应里的 secret 用登录 AES key+iv 解密 → HMAC-SHA256 签名密钥(b64)；
  * 之后每个请求：紧凑 JSON 计算 HMAC → b64 字符串**拼在 JSON 文本前面**发送；
  * errno=4224 表示未登录（自动重登一次）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import threading
import time
from urllib.parse import urlparse

import websocket
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

# VM App（trim.vm）REST API 走 HTTP + fnos-token cookie 鉴权（2026-10-18 实测）
# 注意：GET /vm/api/v1/dashboard/summary 是 SSE 流（data: 持续推送不结束），
#   普通 GET read() 会永久阻塞 —— 集成内禁止调用该端点，一律用 /domain/status
import urllib.error
import urllib.parse
import urllib.request

_VM_API_BASE = "/vm/api/v1"

# 相册 App（/p）REST 的 authx 签名参数（2026-10-18 从相册 SPA 逆向并实测通签）：
#   authx = "nonce=<6位随机>&timestamp=<毫秒>&sign=" + md5(
#       "_".join([盐, 签名路径, nonce, timestamp, md5(解码后参数串), apiKey]))
#   * 签名路径 = "/p" + 接口路径（如 /p/api/v1/explore/recent_photos）；
#   * 参数串 = 参数按 key 排序后以 encodeURIComponent 风格序列化（空格→%20），
#     md5 前先 decodeURIComponent 还原；
#   * apiKey 是主入口 gv 字节数组逐字节 XOR 119 得到的固定串。
_PHOTO_SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
_PHOTO_APIKEY = "EAECCF25-80A6-4666-A7C2-A76904A74AB6"
_PHOTO_RECENT_PATH = "/p/api/v1/explore/recent_photos"

_LOGGER = logging.getLogger(__name__)

ERR_NOT_LOGIN = 4224
ERR_BAD_CREDENTIAL = 131072
ERR_NO_ROUTE = 10000002
ERR_APP_NOT_INSTALLED = 266240

_SKIP_SIGN = {"encrypted", "util.crypto.getRSAPub", "util.getSI"}


class FnOsApiError(Exception):
    """fnOS API 调用失败。"""

    def __init__(self, message, errno=None):
        super().__init__(message)
        self.errno = errno


def _last_num(payload, depth=0):
    """从 resmon 返回的序列结构里捞最后一个数值（结构未公开，防御式解析）。"""
    if depth > 6:
        return None
    if isinstance(payload, (int, float)):
        return payload
    if isinstance(payload, str):
        try:
            return float(payload)
        except ValueError:
            return None
    if isinstance(payload, dict):
        best = None
        for key in ("value", "val", "util", "percent", "used", "in", "rx",
                    "read", "up", "out", "tx", "write", "rpm", "speed"):
            if key in payload:
                got = _last_num(payload[key], depth + 1)
                if got is not None:
                    best = got
        if best is not None:
            return best
        for value in reversed(list(payload.values())):
            got = _last_num(value, depth + 1)
            if got is not None:
                return got
        return None
    if isinstance(payload, (list, tuple)):
        for value in reversed(payload):
            got = _last_num(value, depth + 1)
            if got is not None:
                return got
    return None


def _first_key(data, *keys, default=None):
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data[key]
    return default


#: 磁盘阵列状态口径（stor.listDisk → disk[].storage[].arrState）；
#: api / binary_sensor / sensor 三处共用，避免各算各的「异常盘」
DISK_GOOD_STATES = frozenset({
    "in_sync", "clean", "active", "ok", "normal", "optimal", "idle",
    "online", "good", "passed", "healthy",
})
DISK_WARN_STATES = frozenset({
    "degraded", "fault", "failed", "missing", "rebuild", "resync",
    "recovering", "error", "offline", "notpresent",
})


def disk_health_states(disk):
    """一块盘在阵列里的状态列表（disk[].storage[].arrState，小写）。"""
    if not isinstance(disk, dict):
        return []
    states = []
    for row in disk.get("storage") or []:
        if isinstance(row, dict):
            state = str(row.get("arrState") or "").strip().lower()
            if state:
                states.append(state)
    return states


def disk_is_bad(states):
    """阵列状态判坏：命中告警态，或不含任何已知正常态。"""
    if not states:
        return False
    return (any(state in DISK_WARN_STATES for state in states)
            or not any(state in DISK_GOOD_STATES for state in states))


def _photo_qs(params):
    """相册签名参数串：key 排序 + URLSearchParams 风格编码（空格→%20）。"""
    parts = []
    for key in sorted(params):
        if params[key] is None:
            continue
        parts.append(
            urllib.parse.quote(str(key), safe="-.*_") + "="
            + urllib.parse.quote(str(params[key]), safe="-.*_"))
    return "&".join(parts)


class FnOsClient:
    """fnOS WS 客户端：线程安全、断线自动重连重登。"""

    def __init__(self, host, port, username, password, ssl=False):
        parsed = urlparse(host if "://" in str(host) else f"//{host}")
        self.host = parsed.hostname or str(host)
        self.port = int(port or 5666)
        self.ssl = bool(ssl)
        self.username = username
        self.password = password
        scheme = "wss" if self.ssl else "ws"
        self.ws_url = f"{scheme}://{self.host}:{self.port}/websocket?type=main"
        self.http_base = f"{'https' if self.ssl else 'http'}://{self.host}:{self.port}"

        self._ws = None
        self._lock = threading.RLock()
        self._pending = {}
        self._pending_cond = threading.Condition()
        self._recv_thread = None
        self._req_seq = 0
        self._back_id = "0000000000000000"
        self._rsa_pub = None
        self._si = None
        self._sign_key = None
        self._aes_key = None
        self._aes_iv = None
        self._logged_in = False
        self._closed = False
        self.token = None
        # VM App REST 客户端：懒创建（build_opener 内部 load_default_certs
        # 是阻塞调用，绝不能在事件循环里做——首次使用发生在 executor 线程）
        self._http_opener = None

    # ------------------------------------------------------------------ #
    # 连接 / 登录
    # ------------------------------------------------------------------ #
    def _ensure_token(self):
        """REST 调用前的登录保障。

        request()/payload() 走 WS 且有 4224 自动重连重登；REST（_http_json /
        photo_recent）没有这层兜底——悬浮窗专用客户端在 HA 重启后是全新实例，
        token 为空，直接调 REST 必报「未登录，缺少 fnos-token」。
        """
        if not self.token:
            self._connect()  # 建连 + getRSAPub + 登录（阻塞 IO，仅 executor 线程调）
        return self.token

    def _connect(self):
        self._close_ws()
        timeout = 15
        self._ws = websocket.create_connection(
            self.ws_url, timeout=timeout,
            sslopt={"cert_reqs": 0} if self.ssl else None,
        )
        self._logged_in = False
        self._pending = {}
        if self._recv_thread is None or not self._recv_thread.is_alive():
            self._recv_thread = threading.Thread(
                target=self._recv_loop, name="fnos-ws-recv", daemon=True
            )
            self._recv_thread.start()
        # getRSAPub 是免签接口；这里必须内联发送，不能走 self.request()——
        # request() 发现 _logged_in=False 会再次调 _connect()，互相递归爆栈
        rid = self._reqid()
        body = json.dumps(
            {"req": "util.crypto.getRSAPub", "reqid": rid},
            separators=(",", ":"),
        )
        r = self._raw_request(rid, body, timeout=timeout)
        # 实测响应为平铺结构：pub/si 直接在顶层（部分版本可能包在 data 里，兼容取）
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        data = {**r, **data}
        if r.get("errno") not in (None, 0) or not data.get("pub"):
            raise FnOsApiError(
                f"获取 fnOS RSA 公钥失败：errno={r.get('errno')}")
        self._rsa_pub = data.get("pub")
        self._si = data.get("si")
        self._login()

    def _close_ws(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # pylint: disable=broad-except
                pass

    def _recv_loop(self):
        while True:
            ws = self._ws
            if ws is None:
                break
            try:
                raw = ws.recv()
            except Exception:  # pylint: disable=broad-except
                break
            if not raw:
                break
            try:
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
                start = text.find("{")
                if start < 0:
                    continue
                msg = json.loads(text[start:])
            except Exception:  # pylint: disable=broad-except
                continue
            rid = msg.get("reqid")
            with self._pending_cond:
                if rid in self._pending:
                    self._pending[rid].append(msg)
                    self._pending_cond.notify_all()

    def _reqid(self):
        self._req_seq = (self._req_seq + 1) & 0xFFFF
        return (f"{format(int(time.time()), 'x').zfill(8)}"
                f"{self._back_id}{format(self._req_seq, 'x').zfill(4)}")

    def _login(self):
        self._aes_key = "".join(random.choice("0123456789abcdef") for _ in range(32))
        self._aes_iv = os.urandom(16)
        body = json.dumps(
            {
                "req": "user.login", "reqid": self._reqid(),
                "user": self.username, "password": self.password,
                "deviceType": "HomeAssistant", "deviceName": "Home Assistant",
                "stay": True, "si": self._si,
            },
            separators=(",", ":"),
        )
        rsa_b64 = base64.b64encode(
            PKCS1_v1_5.new(RSA.import_key(self._rsa_pub)).encrypt(
                self._aes_key.encode())
        ).decode()
        aes_b64 = base64.b64encode(
            AES.new(self._aes_key.encode(), AES.MODE_CBC, self._aes_iv).encrypt(
                pad(body.encode(), AES.block_size))
        ).decode()
        frame = json.dumps(
            {"req": "encrypted", "iv": base64.b64encode(self._aes_iv).decode(),
             "rsa": rsa_b64, "aes": aes_b64},
            separators=(",", ":"),
        )
        rid = json.loads(body)["reqid"]
        resp = self._raw_request(rid, frame, timeout=20)
        # 实测登录响应可能把字段平铺在顶层（getRSAPub 即如此），data 与顶层合并兼容
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        data = {**resp, **data}
        if resp.get("errno") not in (None, 0) or not data.get("secret"):
            errno = resp.get("errno")
            if errno == ERR_BAD_CREDENTIAL:
                raise FnOsApiError("用户名或密码错误", errno)
            raise FnOsApiError(f"fnOS 登录失败：errno={errno}", errno)
        secret = base64.b64decode(data["secret"])
        plain = unpad(
            AES.new(self._aes_key.encode(), AES.MODE_CBC, self._aes_iv).decrypt(
                secret),
            AES.block_size,
        )
        self._sign_key = base64.b64encode(plain).decode()
        self._back_id = data.get("backId") or self._back_id
        # token 同时用于 VM App REST API 的 fnos-token cookie 鉴权
        self.token = data.get("token")
        self._logged_in = True
        _LOGGER.info("fnOS 登录成功：%s", self.host)

    def _sign(self, body_text):
        key = base64.b64decode(self._sign_key)
        sig = base64.b64encode(
            hmac.new(key, body_text.encode(), hashlib.sha256).digest()
        ).decode()
        return sig + body_text

    def _raw_request(self, rid, frame_text, timeout=30):
        if self._ws is None:
            raise FnOsApiError("fnOS WebSocket 未连接")
        with self._pending_cond:
            self._pending[rid] = []
        self._ws.send(frame_text)
        deadline = time.monotonic() + timeout
        with self._pending_cond:
            while True:
                got = self._pending.get(rid)
                if got:
                    return got.pop(0)
                remain = deadline - time.monotonic()
                if remain <= 0:
                    self._pending.pop(rid, None)
                    raise FnOsApiError(f"fnOS 请求超时（{timeout}s）：等待响应")
                self._pending_cond.wait(remain)

    def request(self, req, timeout=30, retry_login=True, **params):
        """发起一次 WS 请求；连接失效自动重连重登一次。"""
        with self._lock:
            if self._ws is None or not self._logged_in:
                self._connect()
            rid = self._reqid()
            body = json.dumps(
                {"req": req, "reqid": rid, **params}, separators=(",", ":")
            )
            if self._sign_key and req not in _SKIP_SIGN:
                frame = self._sign(body)
            else:
                frame = body
            try:
                resp = self._raw_request(rid, frame, timeout)
            except (FnOsApiError, OSError, websocket.WebSocketException):
                self._close_ws()
                if not retry_login:
                    raise
                self._connect()
                rid = self._reqid()
                body = json.dumps(
                    {"req": req, "reqid": rid, **params}, separators=(",", ":")
                )
                frame = self._sign(body) if (
                    self._sign_key and req not in _SKIP_SIGN) else body
                resp = self._raw_request(rid, frame, timeout)
            errno = resp.get("errno")
            if errno in (None, 0):
                return resp
            if errno == ERR_NOT_LOGIN and retry_login:
                self._close_ws()
                self._connect()
                return self.request(req, timeout, retry_login=False, **params)
            return resp

    def call(self, req, default=None, **params):
        """容错版 request：任何失败返回 default（快照采集用，单接口失败不拖垮整轮）。"""
        try:
            resp = self.request(req, **params)
        except FnOsApiError as err:
            _LOGGER.debug("fnOS %s 调用失败：%s", req, err)
            return default
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS %s 调用异常", req, exc_info=True)
            return default
        if resp.get("errno") not in (None, 0):
            _LOGGER.debug("fnOS %s 返回 errno=%s", req, resp.get("errno"))
            return default
        return resp.get("data") if "data" in resp else {}

    def payload(self, req, timeout=30, **params):
        """容错取数：优先 data 字段；实测部分接口把业务字段平铺在顶层
        （stor.calcSpace / user.listLoginDevice / notify.unreadTotal 等），
        此时剥离协议字段后返回顶层。任何失败返回 {}。"""
        try:
            resp = self.request(req, timeout=timeout, **params)
        except FnOsApiError as err:
            _LOGGER.debug("fnOS %s 调用失败：%s", req, err)
            return {}
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS %s 调用异常", req, exc_info=True)
            return {}
        if resp.get("errno") not in (None, 0):
            _LOGGER.debug("fnOS %s 返回 errno=%s", req, resp.get("errno"))
            return {}
        if "data" in resp:
            return resp["data"] if resp["data"] is not None else {}
        return {k: v for k, v in resp.items()
                if k not in ("reqid", "result", "rev", "req", "errno",
                             "errmsg")}

    def _stream_call(self, method, timeout=25, **params):
        """subscribe 型接口（docker 系）：响应以多帧 rsp 片段推送，
        参数必须顶层平铺（如 all=True），聚合到 result=succ/fail 终态。
        实测：appcgi.dockermgr.containerList/stats 均为此模式。"""
        if self._ws is None or not self._logged_in:
            self._connect()
        rid = self._reqid()
        body = json.dumps({"req": method, "reqid": rid, **params},
                          separators=(",", ":"))
        frame = self._sign(body) if (
            self._sign_key and method not in _SKIP_SIGN) else body
        with self._pending_cond:
            self._pending[rid] = []
        self._ws.send(frame)
        deadline = time.monotonic() + timeout
        rows, result, cursor = [], None, 0
        while time.monotonic() < deadline:
            # 只能用 get+游标读取；pop 会删掉 _pending[rid] 键，
            # 之后到达的帧会被 _on_message 判 "rid 不在 pending" 而丢弃
            with self._pending_cond:
                frames = (self._pending.get(rid) or [])[cursor:]
                cursor += len(frames)
            for f in frames:
                result = f.get("result")
                if f.get("errno") not in (None, 0):
                    _LOGGER.debug("fnOS %s 返回 errno=%s",
                                  method, f.get("errno"))
                    return None
                part = f.get("rsp")
                if isinstance(part, list):
                    rows.extend(part)
                elif part is not None:
                    rows.append(part)
            if result in ("succ", "fail"):
                break
            time.sleep(0.15)
        if result != "succ":
            _LOGGER.debug("fnOS %s 流式响应未完成（result=%s）",
                          method, result)
        with self._pending_cond:
            self._pending.pop(rid, None)
        return rows if result == "succ" else None

    def must(self, req, **params):
        """严格版 request：errno 非成功时抛 FnOsApiError（控制类操作用）。"""
        resp = self.request(req, **params)
        errno = resp.get("errno")
        if errno not in (None, 0):
            raise FnOsApiError(
                f"{req} 失败：errno={errno} {resp.get('errmsg') or ''}".strip(),
                errno,
            )
        return resp.get("data") if "data" in resp else {}

    def close(self):
        self._closed = True
        self._close_ws()

    # ------------------------------------------------------------------ #
    # 数据快照
    # ------------------------------------------------------------------ #
    def fetch_all(self):
        """拉一轮全量快照；单接口失败不影响其余字段。"""
        snap: dict = {}
        snap["host"] = self._fetch_host()
        snap["resmon"] = self._fetch_resmon()
        snap["storage"] = self._fetch_storage()
        snap["net"] = self._fetch_net()
        snap["services"] = self._fetch_services()
        snap["update"] = self._fetch_update()
        snap["notify_unread"] = self.payload(
            "notify.unreadTotal").get("unreadTotal") or 0
        login = self.payload("user.listLoginDevice")
        login_rows = login.get("loginDevice") if isinstance(
            login.get("loginDevice"), list) else []
        snap["login_devices"] = login_rows
        snap["docker"] = self._fetch_docker()
        snap["download"] = self._fetch_download()
        snap["file"] = self._fetch_file()
        snap["external"] = self._fetch_external()
        snap["vms"] = self._fetch_vms()
        snap["apps"] = self._probe_apps()
        snap["apps_installed"] = self.installed_apps()
        return snap

    def _fetch_host(self):
        host = self.call("appcgi.sysinfo.getHostName", default={}) or {}
        ver = self.call("appcgi.sysinfo.getTrimVersion", default={}) or {}
        uptime = self.call("appcgi.sysinfo.getUptime", default={}) or {}
        machine = self.call("appcgi.sysinfo.getMachineId", default={}) or {}
        fan = self.call("appcgi.sysinfo.getFanMode", default={}) or {}
        plans = self.call("appcgi.sysinfo.listPowerPlan", default=[]) or []
        # 硬件信息（免登录接口）：CPU/内存条/GPU/BIOS/主板/系统盘/虚拟化能力。
        # 返回结构官方未公开，这边原样保留，sensor.py 做防御式解析。
        hardware = self.payload("appcgi.sysinfo.getHardwareInfo")
        return {
            "hostname": _first_key(host, "hostname", "hostName", "name",
                                  default=self.host),
            "version": _first_key(ver, "version", "trimVersion"),
            "edition": _first_key(ver, "edition"),
            "platform": _first_key(ver, "platform"),
            "machine_id": _first_key(machine, "machineId", "machine_id", "id"),
            "uptime_seconds": _last_num(uptime),
            "fan_mode": _first_key(fan, "mode", "fanMode", "level", "value"),
            "power_plans": plans if isinstance(plans, list) else [],
            "hardware": hardware if isinstance(hardware, dict) else {},
        }

    def _fetch_resmon(self):
        # 全部按 2026-10-18 实测结构解析：
        #   mem:  data.mem.{used,total,...}（字节）、data.swap.{...}
        #   cpu:  data.cpu.busy.all（%）、data.cpu.temp[]、loadavg、name/core/thread
        #   gpu:  data.gpu[].busy（%）、device 名称、ram
        #   net:  data.ifs[].{receive,transmit}（B/s，多网卡求和）
        #   disk: data.disk[].{read,write}（B/s，多盘求和）
        #   sysFan: {isTrim:bool} —— 非 trim 硬件无风扇转速
        mem_p = self.payload("appcgi.resmon.mem")
        mem = mem_p.get("mem") if isinstance(mem_p.get("mem"), dict) else {}
        swap = mem_p.get("swap") if isinstance(mem_p.get("swap"), dict) else {}
        mem_used = _first_key(mem, "used", "memUsed", default=None)
        mem_total = _first_key(mem, "total", "memTotal", default=None)
        mem_pct = None
        if isinstance(mem_used, (int, float)) and isinstance(
                mem_total, (int, float)) and mem_total:
            mem_pct = round(mem_used * 100.0 / mem_total, 1)

        cpu_p = self.payload("appcgi.resmon.cpu")
        cpu = cpu_p.get("cpu") if isinstance(cpu_p.get("cpu"), dict) else {}
        busy = cpu.get("busy") if isinstance(cpu.get("busy"), dict) else {}
        temps = cpu.get("temp") if isinstance(cpu.get("temp"), list) else []
        loadavg = cpu.get("loadavg") if isinstance(cpu.get("loadavg"),
                                                   dict) else {}

        gpu_p = self.payload("appcgi.resmon.gpu")
        gpus = gpu_p.get("gpu") if isinstance(gpu_p.get("gpu"), list) else []
        gpu0 = gpus[0] if gpus and isinstance(gpus[0], dict) else {}
        gpu_ram = gpu0.get("ram") if isinstance(gpu0.get("ram"), dict) else {}

        net_p = self.payload("appcgi.resmon.net")
        ifs = net_p.get("ifs") if isinstance(net_p.get("ifs"), list) else []
        net_down = net_up = None
        if ifs:
            net_down = round(sum(float(i.get("receive") or 0)
                                 for i in ifs if isinstance(i, dict)), 1)
            net_up = round(sum(float(i.get("transmit") or 0)
                               for i in ifs if isinstance(i, dict)), 1)

        disk_p = self.payload("appcgi.resmon.disk")
        dsk_rows = (disk_p.get("disk")
                    if isinstance(disk_p.get("disk"), list) else [])
        disk_read = disk_write = None
        if dsk_rows:
            disk_read = round(sum(float(d.get("read") or 0)
                                  for d in dsk_rows if isinstance(d, dict)), 1)
            disk_write = round(sum(float(d.get("write") or 0)
                                   for d in dsk_rows if isinstance(d, dict)), 1)

        fan_p = self.payload("appcgi.resmon.sysFan")
        fan_rpm = _last_num(fan_p) if fan_p.get("isTrim") else None

        return {
            "cpu_pct": _first_key(busy, "all", "total", "busy"),
            "cpu_temp": temps[0] if temps else None,
            "cpu_name": cpu.get("name"),
            "cpu_core": cpu.get("core"),
            "cpu_thread": cpu.get("thread"),
            "load_1min": loadavg.get("avg1min"),
            "mem_used": mem_used,
            "mem_total": mem_total,
            "mem_available": mem.get("available"),
            "mem_pct": mem_pct,
            "swap_used": swap.get("used"),
            "swap_total": swap.get("total"),
            "net_up": net_up,
            "net_down": net_down,
            "disk_read": disk_read,
            "disk_write": disk_write,
            "disk_busy": max(
                (d.get("busy") or 0 for d in dsk_rows if isinstance(d, dict)),
                default=None),
            "gpu_pct": gpu0.get("busy"),
            "gpu_name": gpu0.get("device") or gpu0.get("model"),
            "gpu_ram_used": gpu_ram.get("used"),
            "gpu_ram_total": gpu_ram.get("total"),
            "fan_rpm": fan_rpm,
        }

    def _fetch_storage(self):
        # 实测（2026-10-18）：stor.calcSpace 顶层平铺
        #   {fssizeSys, frsizeSys, sysDiskType, storTotal, fssizeStor,
        #    frsizeStor, HDD, SSD, USB}（字节）；stor.state2 → data.state[]
        #   stor.listDisk → data.disk[]（无健康字段，健康看 storage[].arrState）
        calc = self.payload("stor.calcSpace")
        if not isinstance(calc, dict):
            calc = {}
        pools_p = self.payload("stor.state2")
        pools = (pools_p.get("state")
                 if isinstance(pools_p.get("state"), list) else [])
        disks_p = self.payload("stor.listDisk")
        disks = (disks_p.get("disk")
                 if isinstance(disks_p.get("disk"), list) else [])
        total = _first_key(calc, "fssizeStor", "total", "totalSize")
        free = _first_key(calc, "frsizeStor", "free", "freeSize")
        used = None
        if isinstance(total, (int, float)) and isinstance(free, (int, float)):
            used = max(total - free, 0)
        hdd = _first_key(calc, "HDD", "hdd", default=0) or 0
        ssd = _first_key(calc, "SSD", "ssd", default=0) or 0
        usb = _first_key(calc, "USB", "usb", default=0) or 0
        bad = 0
        for disk in disks:
            if disk_is_bad(disk_health_states(disk)):
                bad += 1
        return {
            "total": total,
            "free": free,
            "used": used,
            "disk_count": len(disks) or (hdd + ssd + usb) or None,
            "hdd_count": hdd,
            "ssd_count": ssd,
            "usb_count": usb,
            "pool_count": len(pools) or _first_key(
                calc, "storTotal", default=None),
            "pools": pools,
            "disks": disks,
            "disk_bad": bad,
        }

    def _fetch_net(self):
        # 实测：data.net.ifs[]（name/ipv4/speed/mac/mtu 等完整网卡信息）
        data = self.payload("appcgi.network.net.list")
        if isinstance(data, dict):
            net = data.get("net") if isinstance(data.get("net"), dict) else {}
            data = net.get("ifs") or data.get("ifs") or []
        return {"interfaces": data if isinstance(data, list) else []}

    def _fetch_services(self):
        # 实测：ssh → data.enable；smb → data.smbEnable（其余服务同构 *Enable）
        services = {}
        ssh = self.payload("appcgi.network.ssh.status")
        services["ssh"] = bool(_first_key(
            ssh, "enable", "enabled", "switch", "open", default=False))
        for key in ("smb", "nfs", "ftp", "webdav", "dlna"):
            opt = self.payload(f"appcgi.share.{key}.opt")
            services[key] = bool(opt.get(f"{key}Enable") or _first_key(
                opt, "switch", "enabled", "open", "run", "start",
                default=False))
        fw = self.payload("appcgi.security.firewall.getting")
        services["firewall"] = bool(_first_key(
            fw, "switch", "enable", "enabled", "open", "status",
            default=False))
        return services

    def _fetch_update(self):
        data = self.call("liveupdate.status", default={}) or {}
        available = bool(_first_key(data, "haveUpdate", "hasUpdate", "update",
                                    "upgrade", "available", default=False))
        return {
            "available": available,
            "version": _first_key(data, "version", "newVersion"),
            "status": _first_key(data, "status", "state"),
            "raw": data,
        }

    def _fetch_docker(self):
        # 实测：containerList/stats 是 subscribe 流式接口，参数顶层平铺
        rows = self._stream_call("appcgi.dockermgr.containerList", all=True)
        if rows is None:
            return {"installed": False, "containers": [], "stats": {}}
        stats_rows = self._stream_call("appcgi.dockermgr.stats")
        stats_map = {}
        for block in stats_rows or []:
            if isinstance(block, dict):
                stats_map.update(block)
        containers = [self._norm_container(row, stats_map)
                      for row in rows if isinstance(row, dict)]
        return {"installed": True, "containers": containers,
                "stats": stats_map}

    @staticmethod
    def _norm_container(row, stats_map=None):
        """按实测字段（docker ps 风格）归一化：
        Id/Command/Created/HostConfig.NetworkMode/Image/Names/State/Status/
        Project/Ports，stats 为 {容器Id: {cpuUsage,usedMem,networkRx,networkTx}}。
        """
        name = _first_key(row, "name", "Name", "Names")
        if isinstance(name, list) and name:
            name = name[0]
        cid = str(_first_key(row, "id", "ID", "Id", "containerId",
                             default="") or "")
        state = str(_first_key(row, "state", "State", default="") or "")
        state = state.lower() or "unknown"
        if state in ("stopped", "stop"):
            state = "exited"

        stat = (stats_map or {}).get(cid) or {}
        cpu = _last_num(_first_key(stat, "cpuUsage", "cpu", default=None))
        mem_used = _last_num(_first_key(stat, "usedMem", "mem",
                                        default=None))
        ports = row.get("Ports") or row.get("ports") or []
        host_config = row.get("HostConfig") or row.get("hostConfig") or {}
        return {
            "name": str(name or "").lstrip("/"),
            "id": cid,
            "state": state,
            "status": str(_first_key(row, "Status", "status", default="")
                          or ""),
            "image": _first_key(row, "image", "Image", "imageName"),
            "project": _first_key(row, "Project", "project"),
            "command": _first_key(row, "Command", "command"),
            "created": _first_key(row, "Created", "created"),
            "network_mode": _first_key(host_config, "NetworkMode",
                                       "networkMode"),
            "ports": ports,
            "cpu_pct": (round(cpu * 100.0, 2)
                        if isinstance(cpu, (int, float)) else None),
            "mem_mb": (round(mem_used / 1048576.0, 2)
                       if isinstance(mem_used, (int, float)) else None),
            "net_rx": _last_num(_first_key(stat, "networkRx", "rx",
                                           default=None)),
            "net_tx": _last_num(_first_key(stat, "networkTx", "tx",
                                           default=None)),
            "raw": row,
        }

    def _fetch_download(self):
        # 实测（2026-10-18）：
        #   task.query → data.block.data 为 JSON 字符串：
        #     {"stats":{"all":..,"downloading":..,"active":..,...},
        #      "stats_speed_size":{"dl_speed":..,"up_speed":..,...}}
        #   stat.all → data.rsp.{dl_speed,up_speed,...}
        stat = self.payload("appcgi.downloadcenter.stat.all")
        rsp = stat.get("rsp") if isinstance(stat.get("rsp"), dict) else {}
        active = None
        speed_down = _first_key(rsp, "dl_speed", "downloadSpeed")
        speed_up = _first_key(rsp, "up_speed", "uploadSpeed")
        tasks = self.payload("appcgi.downloadcenter.task.query",
                             start=0, limit=5)
        block = tasks.get("block") if isinstance(tasks.get("block"),
                                                  dict) else {}
        try:
            blob = json.loads(block.get("data") or "{}")
        except (TypeError, ValueError):
            blob = {}
        stats = blob.get("stats") if isinstance(blob.get("stats"),
                                                 dict) else {}
        speeds = (blob.get("stats_speed_size")
                  if isinstance(blob.get("stats_speed_size"), dict) else {})
        if stats:
            active = _first_key(stats, "active", "downloading",
                                default=stats.get("all"))
        if speed_down is None:
            speed_down = speeds.get("dl_speed")
        if speed_up is None:
            speed_up = speeds.get("up_speed")
        installed = bool(stats or rsp or block)
        if not installed:
            return {"installed": False, "active_tasks": None,
                    "speed_down": None, "speed_up": None}
        return {
            "installed": True,
            "active_tasks": active,
            "speed_down": speed_down,
            "speed_up": speed_up,
            "stats": stats,
        }

    def _fetch_file(self):
        def count(req, list_keys=("list", "data", "rows")):
            data = self.call(req, default=None)
            if data is None:
                return None
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                for key in list_keys:
                    if isinstance(data.get(key), list):
                        return len(data[key])
                total = data.get("total")
                if isinstance(total, int):
                    return total
            return None

        return {
            "recent_n": count("file.recent.list"),
            "fav_n": count("file.fav.list"),
            "trash_n": count("file.trash.list"),
            "share_n": count("file.share.list"),
        }

    def _fetch_external(self):
        certs = self.call("appcgi.netsvr.cert.list", default=[]) or []
        if isinstance(certs, dict):
            certs = certs.get("list") or []
        conn = self.call("appcgi.netsvr.conn.status", default={}) or {}
        domains = self.call("appcgi.netsvr.domain.list", default=[]) or []
        if isinstance(domains, dict):
            domains = domains.get("list") or []
        return {
            "cert_count": len(certs) if isinstance(certs, list) else 0,
            "fnconnect": conn,
            "domains": domains if isinstance(domains, list) else [],
        }

    def _http_json(self, method, path, payload=None, timeout=15,
                   base=_VM_API_BASE):
        """调 fnOS REST API（默认 /vm/api/v1，可指定 /app-center/v1 等基路径）。

        返回 data 字段；业务码非 0 / HTTP 非 200 / 非 JSON 抛 FnOsApiError。
        """
        if not self._ensure_token():
            raise FnOsApiError("未登录，缺少 fnos-token")
        if self._http_opener is None:
            # 禁用系统代理（内网直连）；在 executor 线程里创建才不会阻塞事件循环
            self._http_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        url = f"{self.http_base}{base}{path}"
        body = None
        headers = {
            "Cookie": f"fnos-token={self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=body, method=method, headers=headers)
        try:
            with self._http_opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as err:
            raise FnOsApiError(
                f"fnOS VM REST {method} {path} HTTP {err.code}") from err
        except (OSError, urllib.error.URLError) as err:
            raise FnOsApiError(
                f"fnOS VM REST {method} {path} 失败：{err}") from err
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as err:
            # App 未安装时该路径会 fallback 返回 SPA 的 HTML（200）
            raise FnOsApiError(
                f"fnOS VM REST {path} 返回非 JSON（App 未安装？）") from err
        code = obj.get("code")
        if code not in (0, None):
            raise FnOsApiError(
                f"fnOS VM REST {path} code={code}", errno=code)
        return obj.get("data")

    def _fetch_vms(self):
        # trim.vm App REST（实测 2026-10-18）：/domain/list 返回全部虚拟机
        #   （name/uuid/title/state/osType…）；/domain/status 虽 code=0 但
        #   data 为空数组（不可靠，仅作回退）。
        #   未装 App → 端点 fallback 返回 SPA 的 HTML（非 JSON → 抛错）。
        #   禁用 GET /dashboard/summary —— 那是 SSE 流，read() 永久阻塞。
        try:
            rows = self._http_json("GET", "/domain/list")
        except FnOsApiError:
            try:
                rows = self._http_json("GET", "/domain/status")
            except FnOsApiError as err:
                _LOGGER.debug("fnOS VM App 不可用：%s", err)
                return {"installed": False, "list": []}
        if isinstance(rows, dict):
            rows = rows.get("list") or rows.get("domains") or []
        vms = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(_first_key(
                row, "name", "domainName", "vmName", default="") or "")
            vms.append({
                "id": str(_first_key(
                    row, "uuid", "id", "ID", "vmId", default=name)),
                "name": name,
                "title": str(_first_key(
                    row, "title", "displayName", default=name) or ""),
                "state": str(_first_key(
                    row, "state", "status", "power",
                    default="unknown") or ""),
                "cpu": _first_key(row, "vcpuCount", "cpuCount", "vcpus"),
                "memory": _first_key(
                    row, "memoryCapacity", "memory", "memMb"),
                "os": _first_key(row, "osType", "os", "guestOs"),
                "ip": _first_key(row, "ip", "ipAddress", "ipv4"),
            })
        return {"installed": True, "list": vms}

    def photo_recent(self, offset=0, limit=60, timeout=15):
        """相册「最近添加」照片列表（实测通签 2026-10-18）。

        GET /p/api/v1/explore/recent_photos，参数 offset/limit/start_time/
        end_time（时间串格式 ``YYYY:MM:DD HH:MM:SS``，范围放大以覆盖全部）。
        返回 data.list（dict 列表，含 fileName/filePath/dateTime 与
        additional.thumbnail.{sUrl,mUrl,originalUrl} 等缩略图流地址）。
        """
        if not self._ensure_token():
            raise FnOsApiError("未登录，缺少 fnos-token")
        if self._http_opener is None:
            self._http_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        params = {"offset": offset, "limit": limit,
                  "start_time": "2000:01:01 00:00:00",
                  "end_time": "2030:01:01 23:59:59"}
        qs = _photo_qs(params)
        # yk：decodeURIComponent 后再 md5（与 SPA 的 yk() 一致）
        u = hashlib.md5(urllib.parse.unquote(qs).encode()).hexdigest()
        nonce = str(random.randint(100000, 999999))
        ts = str(int(time.time() * 1000))
        sign = hashlib.md5("_".join(
            [_PHOTO_SALT, _PHOTO_RECENT_PATH, nonce, ts, u,
             _PHOTO_APIKEY]).encode()).hexdigest()
        url = (f"{self.http_base}/p/api/v1/explore/recent_photos?{qs}")
        req = urllib.request.Request(url, headers={
            "Cookie": f"fnos-token={self.token}",
            "accessToken": self.token,
            "authx": f"nonce={nonce}&timestamp={ts}&sign={sign}",
            "Accept": "application/json",
        })
        try:
            with self._http_opener.open(req, timeout=timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as err:
            raise FnOsApiError(
                f"fnOS 相册 recent_photos HTTP {err.code}") from err
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as err:
            raise FnOsApiError(f"fnOS 相册 recent_photos 失败：{err}") from err
        code = obj.get("code")
        if code not in (0, None):
            raise FnOsApiError(
                f"fnOS 相册 recent_photos code={code} ({obj.get('msg')})",
                errno=code)
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        rows = data.get("list")
        return [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []

    def vm_info(self, name):
        """虚拟机完整硬件配置（GET /domain/info?name=，实测 2026-10-18）。

        含 chipset/firmware/cpuMode/cpuCoreCnt/memCapacityInBytes/videoType/
        networks/disks/cdroms/hostpcis/hostusbs/autostart 等；参数用 name
        （传 uuid 返回 code 12021）。
        """
        qname = urllib.parse.quote(str(name or ""), safe="")
        return self._http_json("GET", f"/domain/info?name={qname}") or {}

    def vm_snapshots(self, name):
        """虚拟机快照列表（GET /snapshot/list?name=，实测 2026-10-18）。"""
        qname = urllib.parse.quote(str(name or ""), safe="")
        data = self._http_json("GET", f"/snapshot/list?name={qname}")
        if isinstance(data, dict):
            data = data.get("list") or data.get("snapshots") or []
        return [s for s in data if isinstance(s, dict)] \
            if isinstance(data, list) else []

    def vm_status_row(self, name):
        """运行中虚拟机的实时状态行（/domain/status 只列运行中，关机为空）。"""
        rows = self._http_json("GET", "/domain/status")
        if isinstance(rows, dict):
            rows = rows.get("list") or []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and \
                    str(row.get("name") or "") == str(name):
                return row
        return None

    def vm_state(self, name):
        """虚拟机电源状态（/domain/list 按 name 匹配；未知返回 None）。"""
        rows = self._http_json("GET", "/domain/list")
        if isinstance(rows, dict):
            rows = rows.get("list") or []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and \
                    str(row.get("name") or "") == str(name):
                return str(row.get("state") or "").upper() or None
        return None

    def installed_apps(self):
        """已安装应用列表（应用中心 REST，实测 2026-10-18）：
        GET /app-center/v1/app/installed + ``Authorization: trim <token>``。
        返回 [{app,name,version,status,icon,docker}]，失败返回 []。"""
        if not self.token:
            return []
        if self._http_opener is None:
            self._http_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
        url = f"{self.http_base}/app-center/v1/app/installed?language=zh-CN"
        req = urllib.request.Request(url, headers={
            "Cookie": f"fnos-token={self.token}",
            "Authorization": f"trim {self.token}",
            "Accept": "application/json",
        })
        try:
            with self._http_opener.open(req, timeout=15) as resp:
                obj = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS 应用中心 installed 列表获取失败", exc_info=True)
            return []
        if obj.get("code") not in (0, None):
            return []
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        apps = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            apps.append({
                "app": str(row.get("appName") or ""),
                "name": str(row.get("name") or row.get("appName") or ""),
                "version": str(row.get("version") or ""),
                "status": str(row.get("status") or ""),
                "icon": str(row.get("icon") or ""),
                "docker": bool(row.get("docker")),
            })
        return apps

    def _probe_apps(self):
        """相册 / 影视 App 可用性探测（未登录 HTTP GET，App 存在即返回页面）。"""
        result = {}
        for key, path in (("photo", "/p"), ("video", "/v")):
            try:
                req = urllib.request.Request(
                    f"{self.http_base}{path}", method="HEAD",
                    headers={"User-Agent": "HomeAssistant-fnos"})
                resp = urllib.request.urlopen(req, timeout=6)
                result[key] = resp.status < 400
            except Exception:  # pylint: disable=broad-except
                result[key] = False
        return result

    # ------------------------------------------------------------------ #
    # 控制操作
    # ------------------------------------------------------------------ #
    def app_action(self, app_name, action):
        """应用中心应用 启动/停止（实测端点 2026-10-18）：

        POST /app-center/v1/start/start | /app-center/v1/stop/start
        body {appName, ignoreDependencies:true}；code=0 成功。
        """
        if action not in ("start", "stop"):
            raise FnOsApiError(f"不支持的应用操作：{action}")
        if not app_name:
            raise FnOsApiError("appName 为空")
        return self._http_json(
            "POST", f"/{action}/start",
            payload={"appName": app_name, "ignoreDependencies": True},
            base="/app-center/v1")

    def docker_container_action(self, name, cid, action):
        """容器电源操作（实测：参数平铺 containerId=<64位Id>，勿传 name）。"""
        if action not in ("start", "stop", "restart"):
            raise FnOsApiError(f"不支持的容器操作：{action}")
        target = str(cid or "").strip()
        if not target:
            raise FnOsApiError(f"容器 {name} 缺少 Id，无法执行 {action}")
        return self.must(f"appcgi.dockermgr.container{action.capitalize()}",
                         containerId=target)

    def vm_action(self, name, action):
        """虚拟机电源操作（实测 REST 端点）：start/shutdown/forceshutdown/reboot/pause/resume。"""
        allowed = ("start", "shutdown", "forceshutdown",
                   "reboot", "pause", "resume")
        if action not in allowed:
            raise FnOsApiError(f"不支持的虚拟机操作：{action}")
        if not name:
            raise FnOsApiError("虚拟机 name 为空")
        self._http_json("POST", f"/domain/{action}", payload={"name": name})

    def set_service(self, key, enabled):
        """切换 SSH / SMB / NFS / FTP / WebDAV / DLNA / 防火墙。"""
        if key == "ssh":
            self.must("appcgi.network.ssh.switch", switch=bool(enabled))
            return
        if key == "firewall":
            self.must("appcgi.security.firewall.setting", switch=bool(enabled))
            return
        prefix = {"smb": "smb", "nfs": "nfs", "ftp": "ftp",
                  "webdav": "webdav", "dlna": "dlna"}.get(key)
        if prefix is None:
            raise FnOsApiError(f"未知服务：{key}")
        opt = self.call(f"appcgi.share.{prefix}.opt", default={}) or {}
        field = next(
            (k for k in ("switch", "enabled", "open", "status", "run", "start")
             if isinstance(opt.get(k), bool)),
            "switch",
        )
        self.must(f"appcgi.share.{prefix}.set", **{field: bool(enabled)})

    def power(self, action):
        if action not in ("reboot", "poweroff"):
            raise FnOsApiError(f"不支持的电源操作：{action}")
        return self.must(f"power.{action}")

    def set_fan_mode(self, mode):
        self.must("appcgi.sysinfo.setFanMode", mode=mode)

    def notify_read_all(self):
        return self.must("notify.setReadAll")
