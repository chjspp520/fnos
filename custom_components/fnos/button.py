"""飞牛 fnOS 按钮平台：电源操作 + Docker 容器重启 + 虚拟机重启/强制关机。"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import (
    FnOsEntity,
    live_dynamic_keys,
    purge_dynamic_entities,
)
from .api import FnOsApiError
from .const import DOMAIN, KIND_DOCKER, KIND_HOST, KIND_VM

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback):
    """安装按钮。"""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list = [
        FnOsPowerButton(coordinator, entry, "reboot", "重启"),
        FnOsPowerButton(coordinator, entry, "poweroff", "关机"),
    ]
    owned: dict = {}
    for row in (coordinator.data.get("docker") or {}).get("containers") or []:
        name = row.get("name")
        if name:
            entity = FnOsDockerRestartButton(coordinator, entry, name)
            entities.append(entity)
            owned[entity._attr_unique_id] = (KIND_DOCKER, str(name))
    for row in (coordinator.data.get("vms") or {}).get("list") or []:
        name = row.get("name")
        if not name:
            continue
        for action, label in (("reboot", "重启"),
                              ("forceshutdown", "强制关机")):
            entity = FnOsVMButton(coordinator, entry, name, action, label)
            entities.append(entity)
            owned[entity._attr_unique_id] = (KIND_VM, str(name))
    async_add_entities(entities)

    # 新容器/虚拟机跟随：协调器刷新后立即补建 / 删除后同轮清理
    known: set = {e._attr_unique_id for e in entities}
    seen: set = live_dynamic_keys(coordinator.data or {})

    @callback
    def _sync_dynamic(_now=None):
        snap = coordinator.data or {}
        live = live_dynamic_keys(snap)
        vanished = seen - live
        if vanished:
            removed = purge_dynamic_entities(
                hass, entry, "button", vanished, owned, known)
            seen.difference_update(vanished)
            _LOGGER.info("fnOS 设备已移除（%s），清理按钮实体 %s 个",
                         ", ".join(key for _kind, key in vanished), removed)
        new = []
        try:
            for row in (snap.get("docker") or {}).get("containers") or []:
                name = row.get("name")
                if not name:
                    continue
                uid = f"{entry.entry_id}-{KIND_DOCKER}-{name}-restart"
                if uid not in known:
                    known.add(uid)
                    owned[uid] = (KIND_DOCKER, str(name))
                    new.append(FnOsDockerRestartButton(coordinator, entry,
                                                       name))
            for row in (snap.get("vms") or {}).get("list") or []:
                name = row.get("name")
                if not name:
                    continue
                for action, label in (("reboot", "重启"),
                                      ("forceshutdown", "强制关机")):
                    uid = f"{entry.entry_id}-{KIND_VM}-{name}-{action}"
                    if uid not in known:
                        known.add(uid)
                        owned[uid] = (KIND_VM, str(name))
                        new.append(FnOsVMButton(coordinator, entry, name,
                                                action, label))
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("fnOS 新增设备按钮补建失败")
            return
        seen.update(live)
        if new:
            _LOGGER.info("fnOS 新增设备按钮 %s 个（容器/虚拟机）", len(new))
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_sync_dynamic))
    entry.async_on_unload(
        async_track_time_interval(hass, _sync_dynamic, timedelta(seconds=300)))


class FnOsPowerButton(FnOsEntity, ButtonEntity):
    """主机电源按钮（重启 / 关机）。key 为空串，不产生多余设备。"""

    _forced_domain = "button"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, action, label):
        super().__init__(coordinator, entry, KIND_HOST, "", f"btn-{action}",
                         id_suffix=action)
        self._action = action
        self._attr_name = label
        self._attr_icon = ("mdi:restart" if action == "reboot"
                           else "mdi:power")

    async def async_press(self):
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.power, self._action)
        except FnOsApiError as err:
            raise HomeAssistantError(str(err)) from err


class FnOsDockerRestartButton(FnOsEntity, ButtonEntity):
    """Docker 容器重启按钮。"""

    _forced_domain = "button"

    def __init__(self, coordinator, entry, name):
        super().__init__(coordinator, entry, KIND_DOCKER, name, "restart")
        self._name = name
        self._attr_name = "重启容器"
        self._attr_icon = "mdi:restart"

    async def async_press(self):
        row = {}
        for item in (self.snap.get("docker") or {}).get("containers") or []:
            if item.get("name") == self._name:
                row = item
                break
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.docker_container_action,
                self._name, row.get("id"), "restart")
        except FnOsApiError as err:
            raise HomeAssistantError(f"容器重启失败：{err}") from err
        self.coordinator.client.invalidate("docker")
        await self.coordinator.async_request_refresh()


class FnOsVMButton(FnOsEntity, ButtonEntity):
    """虚拟机重启 / 强制关机按钮（实测 /domain/reboot|forceshutdown）。"""

    _forced_domain = "button"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, name, action, label):
        # 实体 ID 用虚拟机标题（Windows 7）而不是内部 name（tf2vf3bx）
        title = ""
        for row in (coordinator.data.get("vms") or {}).get("list") or []:
            if str(row.get("name")) == name:
                title = str(row.get("title") or "")
                break
        super().__init__(coordinator, entry, KIND_VM, name, action,
                         id_suffix=("force_shutdown"
                                    if action == "forceshutdown" else action),
                         device_title=title)
        self._name = name
        self._action = action
        self._attr_name = label
        self._attr_icon = ("mdi:restart" if action == "reboot"
                           else "mdi:power-off")

    async def async_press(self):
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.vm_action, self._name, self._action)
        except FnOsApiError as err:
            raise HomeAssistantError(f"虚拟机 {self._action} 失败：{err}") from err
        self.coordinator.client.invalidate("vms")
        await self.coordinator.async_request_refresh()
