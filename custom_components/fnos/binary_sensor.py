"""飞牛 fnOS 二进制传感器：App 可用性 / 更新状态。"""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FnOsEntity
from .api import disk_health_states, disk_is_bad
from .const import (
    DOMAIN,
    KIND_HOST,
    KIND_PHOTO,
    KIND_VIDEO,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback):
    """安装二进制传感器。"""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        FnOsUpdateBinary(coordinator, entry),
        FnOsDiskBadBinary(coordinator, entry),
        FnOsAppBinary(coordinator, entry, "docker", "Docker App", KIND_HOST),
        FnOsAppBinary(coordinator, entry, "download", "下载中心 App", KIND_HOST),
        FnOsAppBinary(coordinator, entry, "vm", "虚拟机 App", KIND_HOST),
        FnOsAppBinary(coordinator, entry, "photo", "相册服务", KIND_PHOTO),
        FnOsAppBinary(coordinator, entry, "video", "影视服务", KIND_VIDEO),
    ])


class FnOsUpdateBinary(FnOsEntity, BinarySensorEntity):
    """系统更新可用。"""

    _forced_domain = "binary_sensor"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, KIND_HOST, "", "update_binary",
                         id_suffix="update")
        self._attr_name = "有可用系统更新"
        self._attr_device_class = BinarySensorDeviceClass.UPDATE
        self._attr_icon = "mdi:package-up"

    @property
    def is_on(self):
        return bool((self.snap.get("update") or {}).get("available"))


class FnOsDiskBadBinary(FnOsEntity, BinarySensorEntity):
    """存在健康异常硬盘（S.M.A.R.T. 非 healthy 均判异常）。"""

    _forced_domain = "binary_sensor"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, KIND_HOST, "", "disk_bad_binary",
                         id_suffix="disk_bad")
        self._attr_name = "硬盘健康异常"
        self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        self._attr_icon = "mdi:harddisk-remove"

    @property
    def is_on(self):
        storage = self.snap.get("storage") or {}
        return bool(storage.get("disk_bad"))

    @property
    def extra_state_attributes(self):
        disks = (self.snap.get("storage") or {}).get("disks") or []
        bad = []
        for disk in disks:
            if not isinstance(disk, dict):
                continue
            states = disk_health_states(disk)
            if not disk_is_bad(states):
                continue
            bad.append({
                "设备": disk.get("name") or disk.get("dev"),
                "型号": disk.get("model") or disk.get("modelName"),
                "健康": "/".join(states),
            })
        return {"bad_disks": bad}


class FnOsAppBinary(FnOsEntity, BinarySensorEntity):
    """App / 服务可用性。key 固定空串（source 进 suffix），不产生多余设备。"""

    _forced_domain = "binary_sensor"

    def __init__(self, coordinator, entry, source, label, kind):
        # 主机上的 App：binary_sensor.fnos_docker_available；
        # 相册/影视设备：binary_sensor.fnos_photo_available …
        id_suffix = f"{source}_available" if kind == KIND_HOST else "available"
        super().__init__(coordinator, entry, kind, "", f"app-{source}",
                         id_suffix=id_suffix)
        self._source = source
        self._attr_name = f"{label} 可用"
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_icon = "mdi:puzzle"

    def _value(self):
        snap = self.snap
        if self._source in ("docker",):
            return bool((snap.get("docker") or {}).get("installed"))
        if self._source == "download":
            return bool((snap.get("download") or {}).get("installed"))
        if self._source == "vm":
            return bool((snap.get("vms") or {}).get("installed"))
        return bool((snap.get("apps") or {}).get(self._source))

    @property
    def is_on(self):
        return self._value()
