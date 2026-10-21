"""「远程访问地址跟随浏览器地址栏」实现（模式移植自 esxi_stats）。

要点：
  * 设备页「访问」按钮 = 设备注册表 ``configuration_url``，指向 HA 侧签名页面
    ``/fnos/web?entry=..&target=..``；「远程地址」传感器读同一个字段，两处永远一致；
  * 基地址优先级：集成选项显式地址 → 浏览器心跳学到的 origin → HA 外部 URL →
    内部 URL → 自动探测；
  * 学习值 Store 落盘（private），重启不丢；变化时防抖落盘(2s) + 防抖重签(1.5s)；
  * authSig 只签 path+query 不签 host，换 origin 后签名依然有效。
"""
from __future__ import annotations

import asyncio
import inspect
import logging

from yarl import URL as YarlURL

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    BASE_STORE_KEY,
    BASE_STORE_VERSION,
    CONF_WEB_BASE_URL,
    DOMAIN,
    KIND_TO_TARGET,
    ORIGIN_PREFIX,
    WEB_PAGE_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.components.http.auth import async_sign_path
except ImportError:  # pragma: no cover
    async_sign_path = None

_SIGN_EXPIRY_HOURS = 24
_RESIGN_INTERVAL_HOURS = 12

_DATA_KEY = f"{DOMAIN}_origin"
_LINK_RETRY_INTERVAL = 10
_LINK_MAX_ATTEMPTS = 25


def _sign(hass, path):
    """给路径签 authSig；老版本 HA 没这能力时原样返回。"""
    if async_sign_path is None:
        return path
    try:
        from datetime import timedelta

        return async_sign_path(hass, path, timedelta(hours=_SIGN_EXPIRY_HOURS))
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("fnOS 设备路径签名失败: %s", path)
        return path


def _normalize_base(value):
    """规范成 ``scheme://host[:port]``；非法返回空串。"""
    if not value:
        return ""
    try:
        parsed = YarlURL(str(value).strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return ""
    return str(parsed.origin()).rstrip("/")


def _store(hass):
    return hass.data.setdefault(_DATA_KEY, {})


async def async_load_learned_bases(hass):
    """（幂等）加载历史学到的访问基地址，并给无主记忆做一次过继。"""
    store_data = _store(hass)
    if "learned_bases" in store_data:
        return
    base_store = Store(hass, BASE_STORE_VERSION, BASE_STORE_KEY, private=True)
    try:
        data = await base_store.async_load() or {}
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("加载 fnOS 访问地址记忆失败，本轮先使用自动探测地址")
        data = {}
    store_data["base_store"] = base_store
    learned = dict(data.get("bases") or {})

    live_ids = {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}
    missing = [
        e.entry_id for e in hass.config_entries.async_entries(DOMAIN)
        if not _normalize_base(learned.get(e.entry_id))
    ]
    orphan_values = {
        _normalize_base(v) for k, v in learned.items()
        if k not in live_ids and _normalize_base(v)
    }
    if missing and len(orphan_values) == 1:
        inherited = orphan_values.pop()
        for entry_id in missing:
            learned[entry_id] = inherited
        _LOGGER.info("fnOS 访问地址从历史记忆过继给新集成条目：%s", inherited)
        try:
            await base_store.async_save({"bases": dict(learned)})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("过继 fnOS 访问地址后落盘失败")

    store_data["learned_bases"] = learned


async def _persist_learned_bases(hass):
    """防抖落盘（2 秒内的多次学习只写一次）。"""
    store_data = _store(hass)
    if store_data.get("base_persist_task") is not None:
        return

    async def _do_save():
        try:
            await asyncio.sleep(2)
            store = store_data.get("base_store")
            if store is not None:
                await store.async_save(
                    {"bases": dict(store_data.get("learned_bases") or {})})
        finally:
            store_data["base_persist_task"] = None

    store_data["base_persist_task"] = hass.async_create_task(_do_save())


def _configured_base(hass, entry_id):
    """设备链接要用的绝对基地址（设备注册表要求绝对 URL）。"""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None:
        explicit = _normalize_base(entry.options.get(CONF_WEB_BASE_URL))
        if explicit:
            return explicit
    learned = _normalize_base(
        _store(hass).get("learned_bases", {}).get(entry_id))
    if learned:
        return learned
    configured = hass.config.external_url or hass.config.internal_url or ""
    normalized = _normalize_base(configured)
    if normalized:
        return normalized
    try:
        from homeassistant.helpers.network import get_url

        detected = get_url(hass, allow_ip=True, allow_cloud=False)
    except Exception:  # pylint: disable=broad-except
        detected = ""
    return _normalize_base(detected) or detected.rstrip("/")


def _schedule_relink(hass, entry_id, delay=1.5):
    """基地址变化后防抖重签该条目的设备链接。"""
    store_data = _store(hass)
    tasks = store_data.setdefault("relink_tasks", {})
    old = tasks.get(entry_id)
    if old is not None and not old.done():
        return

    async def _do_relink():
        await asyncio.sleep(delay)
        try:
            count = await link_one_entry(hass, entry_id)
            if count:
                _LOGGER.debug("fnOS 设备链接已按新访问地址刷新：%s 台设备", count)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("访问地址变化后重签条目 %s 的链接失败", entry_id)

    tasks[entry_id] = hass.async_create_task(_do_relink())


async def remember_origin_for_all_entries(hass, request):
    """前端心跳：用浏览器当前地址栏来源更新所有未显式配置的条目。"""
    origin = _normalize_base(str(request.url.origin()))
    if not origin:
        return False
    store_data = _store(hass)
    learned = store_data.setdefault("learned_bases", {})
    changed = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if _normalize_base(entry.options.get(CONF_WEB_BASE_URL)):
            continue
        if learned.get(entry.entry_id) == origin:
            continue
        learned[entry.entry_id] = origin
        changed.append(entry.entry_id)
    if not changed:
        return False
    await _persist_learned_bases(hass)
    for entry_id in changed:
        _schedule_relink(hass, entry_id)
    _LOGGER.info("fnOS 访问地址跟随浏览器地址栏更新为 %s（%s 个条目）",
                 origin, len(changed))
    return True


def _device_target(identifier: str):
    """由设备 identifier（``<entry_id>:<kind>[:<key>]``）解析 (target, key)。"""
    parts = identifier.split(":", 2)
    kind = parts[1] if len(parts) > 1 else ""
    key = parts[2] if len(parts) > 2 else None
    return KIND_TO_TARGET.get(kind), key


async def link_one_entry(hass, entry_id):
    """给该条目下全部 fnOS 设备写 configuration_url，返回签上链接的设备数。"""
    if not hass.config_entries.async_get_entry(entry_id):
        return 0
    base_url = _configured_base(hass, entry_id)
    dev_registry = dr.async_get(hass)
    if inspect.isawaitable(dev_registry):
        dev_registry = await dev_registry
    linked = 0
    for device in dr.async_entries_for_config_entry(dev_registry, entry_id):
        identifier = next(
            (ident[1] for ident in device.identifiers if ident[0] == DOMAIN),
            None,
        )
        if identifier is None:
            continue
        target, key = _device_target(identifier)
        if target is None:
            continue
        page_path = f"{WEB_PAGE_PREFIX}?entry={entry_id}&target={target}"
        if key and target in ("vm", "docker"):
            # 虚拟机 → noVNC 控制台；Docker 容器 → 该容器 ip:port 链接页
            from urllib.parse import quote
            page_path += f"&name={quote(key, safe='')}"
        signed = _sign(hass, page_path)
        configuration_url = f"{base_url}{signed}" if base_url else signed
        if device.configuration_url != configuration_url:
            dev_registry.async_update_device(
                device.id, configuration_url=configuration_url
            )
        linked += 1
    return linked


async def _initial_link_loop(hass, entry_id):
    """启动后反复重签，直到条目下全部设备都挂上本次进程的签名。

    HA 每次重启会换掉 authSig 的内存签名密钥，旧链接在重签完成前打开一律 401。
    """
    for _attempt in range(_LINK_MAX_ATTEMPTS):
        if not hass.config_entries.async_get_entry(entry_id):
            return
        try:
            count = await link_one_entry(hass, entry_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("fnOS 设备链接重签失败（条目 %s）", entry_id)
            count = 0
        expected = len(dr.async_entries_for_config_entry(
            dr.async_get(hass), entry_id))
        if count >= expected and expected > 0:
            return
        await asyncio.sleep(_LINK_RETRY_INTERVAL)


def schedule_initial_link(hass, entry_id):
    """幂等启动重签轮询（重复调用不会叠任务）。"""
    store_data = _store(hass)
    tasks = store_data.setdefault("link_tasks", {})
    old = tasks.get(entry_id)
    if old is not None and not old.done():
        return
    tasks[entry_id] = hass.async_create_task(_initial_link_loop(hass, entry_id))


class FnOsOriginView(HomeAssistantView):
    """前端心跳：上报浏览器地址栏来源（必须走 HA 标准鉴权）。"""

    requires_auth = True
    url = ORIGIN_PREFIX
    name = "fnos:origin"

    async def get(self, request):
        hass = request.app["hass"]
        try:
            changed = await remember_origin_for_all_entries(hass, request)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("fnOS 来源地址心跳处理失败")
            from aiohttp import web

            return web.json_response({"ok": False}, status=500)
        from aiohttp import web

        return web.json_response({"ok": True, "changed": bool(changed)})


async def async_setup_origin(hass):
    """全局注册一次：origin 视图、12 小时兜底重签、停止清理。"""
    store_data = _store(hass)
    if store_data.get("views_registered"):
        return
    store_data["views_registered"] = True

    hass.http.register_view(FnOsOriginView)

    async def _on_interval(_now):
        for entry in hass.config_entries.async_entries(DOMAIN):
            try:
                await link_one_entry(hass, entry.entry_id)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("fnOS 定时重签链接失败（%s）", entry.entry_id)

    from datetime import timedelta

    unsub = async_track_time_interval(hass, _on_interval,
                                      timedelta(hours=_RESIGN_INTERVAL_HOURS))

    async def _on_stop(_event):
        unsub()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
