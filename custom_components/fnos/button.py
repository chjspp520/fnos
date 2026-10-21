"""飞牛 fnOS 按钮平台：电源操作 + Docker 容器重启 + 虚拟机重启/强制关机。"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FnOsEntity
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
    for row in (coordinator.data.get("docker") or {}).get("containers") or []:
        if row.get("name"):
            entities.append(FnOsDockerRestartButton(coordinator, entry,
                                                    row["name"]))
    for row in (coordinator.data.get("vms") or {}).get("list") or []:
        if row.get("name"):
            entities.append(FnOsVMButton(coordinator, entry, row["name"],
                                         "reboot", "重启"))
            entities.append(FnOsVMButton(coordinator, entry, row["name"],
                                         "forceshutdown", "强制关机"))
    async_add_entities(entities)

    from datetime import timedelta

    from homeassistant.helpers.event import async_track_time_interval

    known: set = {e._attr_unique_id for e in entities}

    def _track_new(_now=None):
        snap = coordinator.data or {}
        new = []
        for row in (snap.get("docker") or {}).get("containers") or []:
            name = row.get("name")
            if not name:
                continue
            uid = f"{entry.entry_id}-{KIND_DOCKER}-{name}-restart"
            if uid not in known:
                known.add(uid)
                new.append(FnOsDockerRestartButton(coordinator, entry, name))
        for row in (snap.get("vms") or {}).get("list") or []:
            name = row.get("name")
            if not name:
                continue
            for action in ("reboot", "forceshutdown"):
                uid = f"{entry.entry_id}-{KIND_VM}-{name}-{action}"
                if uid not in known:
                    known.add(uid)
                    label = "重启" if action == "reboot" else "强制关机"
                    new.append(FnOsVMButton(coordinator, entry, name,
                                            action, label))
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_track_time_interval(hass, _track_new, timedelta(seconds=60)))


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
        await self.coordinator.async_request_refresh()
