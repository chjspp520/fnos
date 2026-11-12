"""飞牛 fnOS 集成诊断信息。

只导出「结构 + 计数」，不包含密码、token、签名链接或 URL——用户可以在
「设备与服务 → 飞牛 fnOS → 下载诊断」一键导出后直接贴给维护者。
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

#: 需要脱敏的字段（配置条目 data 里）
_SECRET_KEYS = {"password", "token", "auth", "authsig"}


def _block_summary(value):
    """数据块摘要：只给类型与规模，不带具体内容。"""
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(k) for k in value)}
    if isinstance(value, (list, tuple)):
        return {"type": "list", "count": len(value)}
    if value is None:
        return {"type": "none"}
    return {"type": type(value).__name__}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """返回该配置条目的诊断数据。"""
    store = hass.data.get(DOMAIN) or {}
    info = store.get(entry.entry_id) or {}
    coordinator = info.get("coordinator")
    client = info.get("client")

    data = getattr(coordinator, "data", None) or {}
    interval = getattr(coordinator, "update_interval", None)

    entry_data = {
        key: ("**已隐藏**" if str(key).lower() in _SECRET_KEYS else value)
        for key, value in dict(entry.data).items()
    }

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "version": entry.version,
            "state": str(entry.state),
            "data": entry_data,
            "options": dict(entry.options),
        },
        "connection": {
            "connected": bool(getattr(client, "connected", False)),
            "host": getattr(client, "host", None),
            "port": getattr(client, "port", None),
            "ssl": getattr(client, "ssl", None),
            "logged_in": bool(getattr(client, "_logged_in", False)),
            "has_token": bool(getattr(client, "token", None)),
        },
        "coordinator": {
            "last_update_success": bool(
                getattr(coordinator, "last_update_success", False)),
            "update_interval_seconds": (
                int(interval.total_seconds()) if interval else None),
            "block_ttl_seconds": dict(getattr(client, "BLOCK_TTL", {}) or {}),
            "cached_blocks": sorted((getattr(client, "_cache", {}) or {})),
            "blocks": {
                key: _block_summary(value) for key, value in data.items()
            },
        },
        "registry": {
            "devices": len(dr.async_entries_for_config_entry(
                device_registry, entry.entry_id)),
            "entities": len(er.async_entries_for_config_entry(
                entity_registry, entry.entry_id)),
        },
    }
