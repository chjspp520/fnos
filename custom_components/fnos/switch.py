"""飞牛 fnOS 开关平台：系统服务开关 + Docker 容器电源 + 虚拟机电源。"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from . import FnOsEntity
from .api import FnOsApiError
from .const import (
    DOMAIN,
    KIND_DOCKER,
    KIND_HOST,
    KIND_VM,
    SERVICE_SWITCHES,
)
from .web_proxy import (
    REMOTE_CONTROL_SIGNAL,
    async_set_remote_control,
    remote_control_enabled,
    remote_control_session_count,
)

_LOGGER = logging.getLogger(__name__)

#: 电源操作后乐观状态的保持时长（秒）。虚拟机 start/shutdown 在 fnOS 侧要几秒
#: 才反映到状态里，期间若按快照写状态，开关会先弹回再跳过去。真实状态一旦等于
#: 目标值就立刻让位（见 ``_handle_coordinator_update``），这里只是兜底上限。
VM_OPTIMISTIC_TTL = 60.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback):
    """安装开关。"""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list = [
        # 虚拟机远程控制总开关（关闭 = 立即切断所有 noVNC 控制台连接）
        FnOsRemoteControlSwitch(coordinator, entry),
    ]
    for key, (label, _prefix) in SERVICE_SWITCHES.items():
        entities.append(FnOsServiceSwitch(coordinator, entry, key, label))
    entities.append(FnOsServiceSwitch(coordinator, entry, "firewall",
                                      "防火墙"))
    for row in (coordinator.data.get("docker") or {}).get("containers") or []:
        if row.get("name"):
            entities.append(FnOsDockerSwitch(coordinator, entry, row["name"]))
    for row in (coordinator.data.get("vms") or {}).get("list") or []:
        if row.get("name"):
            entities.append(FnOsVMSwitch(coordinator, entry, row["name"]))
    async_add_entities(entities)

    # 新容器跟随（60 秒一查，只补缺）
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
            uid = f"{entry.entry_id}-{KIND_DOCKER}-{name}-power"
            if uid not in known:
                known.add(uid)
                new.append(FnOsDockerSwitch(coordinator, entry, name))
        for row in (snap.get("vms") or {}).get("list") or []:
            name = row.get("name")
            if not name:
                continue
            uid = f"{entry.entry_id}-{KIND_VM}-{name}-power"
            if uid not in known:
                known.add(uid)
                new.append(FnOsVMSwitch(coordinator, entry, name))
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_track_time_interval(hass, _track_new, timedelta(seconds=60)))


class FnOsServiceSwitch(FnOsEntity, SwitchEntity):
    """系统服务开关（SSH / SMB / NFS / FTP / WebDAV / DLNA / 防火墙）。

    key 固定为空串（identifier 只到 ``<entry>:host``），service_key 放进
    suffix —— 绝不产生多余设备。
    """

    _forced_domain = "switch"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, service_key, label):
        super().__init__(coordinator, entry, KIND_HOST, "",
                         f"svc-{service_key}", id_suffix=service_key)
        self._service_key = service_key
        self._attr_name = f"{label} 服务"

    @property
    def is_on(self):
        return bool((self.snap.get("services") or {}).get(self._service_key))

    @property
    def icon(self):
        return {
            "ssh": "mdi:console",
            "smb": "mdi:folder-network",
            "nfs": "mdi:nas",
            "ftp": "mdi:file-upload",
            "webdav": "mdi:web",
            "dlna": "mdi:cast-audio",
            "firewall": "mdi:shield-lock",
        }.get(self._service_key, "mdi:toggle-switch")

    async def _set(self, enabled):
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.set_service, self._service_key, enabled)
        except FnOsApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class FnOsDockerSwitch(FnOsEntity, SwitchEntity):
    """Docker 容器电源开关（start / stop）。"""

    _forced_domain = "switch"

    def __init__(self, coordinator, entry, name):
        super().__init__(coordinator, entry, KIND_DOCKER, name, "power")
        self._name = name
        self._attr_name = "容器电源"
        self._attr_icon = "mdi:power"

    def _container(self):
        for row in (self.snap.get("docker") or {}).get("containers") or []:
            if row.get("name") == self._name:
                return row
        return {}

    @property
    def is_on(self):
        return (self._container().get("state") or "").lower() in (
            "running", "up", "active", "started")

    async def _action(self, action):
        row = self._container()
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.docker_container_action,
                self._name, row.get("id"), action)
        except FnOsApiError as err:
            raise HomeAssistantError(f"容器 {action} 失败：{err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._action("start")

    async def async_turn_off(self, **kwargs):
        await self._action("stop")


class FnOsVMSwitch(FnOsEntity, SwitchEntity):
    """虚拟机电源开关（start / shutdown，实测 /domain/start|shutdown）。"""

    _forced_domain = "switch"

    def __init__(self, coordinator, entry, name):
        # 实体 ID 用虚拟机标题（Windows 7）而不是内部 name（tf2vf3bx）
        title = ""
        for row in (coordinator.data.get("vms") or {}).get("list") or []:
            if str(row.get("name")) == name:
                title = str(row.get("title") or "")
                break
        super().__init__(coordinator, entry, KIND_VM, name, "power",
                         device_title=title)
        self._name = name
        self._attr_name = "电源"
        self._attr_icon = "mdi:power"
        # 乐观状态：按下后先显示目标值，等 fnOS 真正切过去（或超时）再让位
        self._optimistic: bool | None = None
        self._optimistic_cancel = None

    def _vm(self):
        for row in (self.snap.get("vms") or {}).get("list") or []:
            if row.get("name") == self._name:
                return row
        return {}

    def _real_is_on(self) -> bool:
        # libvirt 风格状态：RUNNING / PAUSED / SHUTOFF（前端 JS 以 RUNNING 判断）
        return (self._vm().get("state") or "").upper() in (
            "RUNNING", "RUN", "ACTIVE", "STARTED")

    @property
    def is_on(self):
        if self._optimistic is not None:
            return self._optimistic
        return self._real_is_on()

    @property
    def extra_state_attributes(self):
        row = self._vm()
        attrs = {}
        for key in ("title", "cpu", "memory", "os", "ip"):
            if row.get(key) not in (None, ""):
                attrs[key] = row[key]
        if self._optimistic is not None:
            attrs["操作中"] = "开机" if self._optimistic else "关机"
        return attrs

    def _set_optimistic(self, value: bool) -> None:
        """立刻写成目标状态，避免接口生效前的中间态把开关弹回去。"""
        self._optimistic = value
        if self._optimistic_cancel is not None:
            self._optimistic_cancel()
        self._optimistic_cancel = async_call_later(
            self.hass, VM_OPTIMISTIC_TTL, self._optimistic_timeout)
        self.async_write_ha_state()

    def _clear_optimistic(self, write: bool = True) -> None:
        """放弃乐观状态，回到快照里的真实值。"""
        if self._optimistic is None and self._optimistic_cancel is None:
            return
        self._optimistic = None
        if self._optimistic_cancel is not None:
            self._optimistic_cancel()
            self._optimistic_cancel = None
        if write:
            self.async_write_ha_state()

    @callback
    def _optimistic_timeout(self, _now) -> None:
        """超时仍未等到目标状态（虚拟机启动失败/卡住），交还给真实值。"""
        self._optimistic_cancel = None
        self._optimistic = None
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        # 真实状态已到目标值即让位；仍相反（VM 还没起来）则继续乐观显示
        if self._optimistic is not None and self._real_is_on() == self._optimistic:
            self._clear_optimistic(write=False)
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self):
        self._clear_optimistic(write=False)
        await super().async_will_remove_from_hass()

    async def _action(self, action, optimistic=None):
        if optimistic is not None:
            self._set_optimistic(optimistic)
        try:
            await self.hass.async_add_executor_job(
                self.coordinator.client.vm_action, self._name, action)
        except FnOsApiError as err:
            self._clear_optimistic()
            raise HomeAssistantError(f"虚拟机 {action} 失败：{err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._action("start", optimistic=True)

    async def async_turn_off(self, **kwargs):
        # 软关机优先；对无响应系统用户可用强制关机按钮
        await self._action("shutdown", optimistic=False)


class FnOsRemoteControlSwitch(FnOsEntity, RestoreEntity, SwitchEntity):
    """虚拟机远程控制总开关（所有虚拟机共用）。

    关闭后：
      * 拒绝新的控制台页面 / noVNC WebSocket 握手；
      * **立即**切断所有已建立的控制台连接（不等浏览器超时）；
      * 设备「控制台地址」传感器不再给出控制台链接。
    状态经 RestoreEntity 持久化，HA 重启后保持上次选择。
    """

    _forced_domain = "switch"
    _attr_icon = "mdi:remote-desktop"

    def __init__(self, coordinator, entry):
        # 实体 ID 固定为 switch.fnos_remote_control
        super().__init__(coordinator, entry, KIND_HOST, "", "remote-control")
        self._attr_name = "远程控制"

    @property
    def available(self):
        # 纯本地开关：NAS 离线时也要能操作（切断残留连接）
        return True

    @property
    def is_on(self):
        return remote_control_enabled(self.hass)

    @property
    def extra_state_attributes(self):
        return {
            "活动控制台连接": remote_control_session_count(self.hass),
            "说明": "关闭后所有虚拟机控制台（noVNC）连接立即断开",
        }

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, REMOTE_CONTROL_SIGNAL, self._handle_remote_change))
        last = await self.async_get_last_state()
        if last is not None:
            # 默认开启；只有恢复出明确的「关」才关闭（含 HA 重启后保持关）
            await async_set_remote_control(
                self.hass, last.state != STATE_OFF)

    @callback
    def _handle_remote_change(self) -> None:
        """其它条目/其它途径改动全局开关时同步显示（含活动连接数）。

        dispatcher 回调在发送方线程里同步执行，兜一层线程检查：万一不是事件
        循环线程，调度回循环再写状态（``async_write_ha_state`` 只能循环线程调）。
        """
        try:
            in_loop = asyncio.get_running_loop() is self.hass.loop
        except RuntimeError:
            in_loop = False
        if in_loop:
            self.async_write_ha_state()
            return
        try:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
        except RuntimeError:  # 事件循环已关闭（卸载中）
            pass

    async def async_turn_on(self, **kwargs):
        await async_set_remote_control(self.hass, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await async_set_remote_control(self.hass, False)
        self.async_write_ha_state()
