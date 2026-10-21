"""飞牛 fnOS 传感器平台。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EVENT_STATE_CHANGED, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_interval,
)

from . import FnOsEntity, device_identifier
from .api import (
    DISK_WARN_STATES,
    _first_key,
    disk_health_states,
    disk_is_bad,
)
from .naming import (
    ForcedEntityIdMixin,
    device_token,
    entry_token,
)
from .web_proxy import remote_control_enabled
from .const import (
    DOMAIN,
    KIND_DOCKER,
    KIND_FILE,
    KIND_HOST,
    KIND_PHOTO,
    KIND_VM,
    KIND_VIDEO,
)

_LOGGER = logging.getLogger(__name__)


def _device_by_identifier(hass, entry, identifier):
    """按 ``(DOMAIN, identifier)`` 取设备（兼容新旧 HA 设备注册表接口）。

    HA 2026.8 起 ``device_registry.async_get_device`` 已弃用（identifier 不再
    跨配置条目唯一，2027.8 停用），替代品是
    ``async_get_device_by_identifier(identifier, config_entry_id)``；本集成的
    identifier 里已拼了 entry_id，但新版接口要求显式带上配置条目，否则可能
    取到别的条目的同名设备。老版本 HA 没有新接口，退回旧写法。
    """
    dev_registry = dr.async_get(hass)
    lookup = getattr(dev_registry, "async_get_device_by_identifier", None)
    if callable(lookup):
        try:
            return lookup((DOMAIN, identifier),
                          getattr(entry, "entry_id", None))
        except TypeError:
            _LOGGER.debug("async_get_device_by_identifier 签名不兼容，"
                          "退回旧接口", exc_info=True)
    return dev_registry.async_get_device(identifiers={(DOMAIN, identifier)})


def _num(value, digits=1):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits)


def _human_bytes(value):
    """防御式容量展示：按最大单位换算（fnOS 未公开字段单位）。"""
    value = _num(value)
    if value is None:
        return None
    for unit, factor in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if abs(value) >= factor:
            return round(value / factor, 2), unit
    return round(value / 1024, 2), "KB"


class FnOsSnapshotSensor(FnOsEntity, SensorEntity):
    """从快照取值的通用传感器；value_fn 返回 (值, 单位) 元组时动态换单位。"""

    _forced_domain = "sensor"

    def __init__(self, coordinator, entry, kind, key, suffix, name,
                 *, unit=None, device_class=None, state_class=None,
                 entity_category=None, icon=None, attrs_fn=None,
                 value_fn=None, device_title=""):
        super().__init__(coordinator, entry, kind, key, suffix,
                         device_title=device_title)
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = entity_category
        self._attr_icon = icon
        self._attrs_fn = attrs_fn
        self._value_fn = value_fn or (lambda snap: None)

    def _value(self):
        try:
            return self._value_fn(self.snap)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS 传感器 %s 取值异常", self._attr_unique_id,
                          exc_info=True)
            return None

    @property
    def native_value(self):
        value = self._value()
        if isinstance(value, tuple) and len(value) == 2:
            return value[0]
        return value

    @property
    def native_unit_of_measurement(self):
        value = self._value()
        if isinstance(value, tuple) and len(value) == 2:
            return value[1]
        return self._attr_native_unit_of_measurement

    @property
    def extra_state_attributes(self):
        if self._attrs_fn is not None:
            try:
                return self._attrs_fn(self.snap)
            except Exception:  # pylint: disable=broad-except
                return None
        return None


class FnOsRemoteAddressSensor(ForcedEntityIdMixin, SensorEntity):
    """设备远程访问地址：state = 短基地址，属性 url = 完整签名链接。

    直接读设备注册表 ``configuration_url``（与设备页「访问」按钮同一字段），
    绝不自己另签，两处永远一致。
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = "mdi:link-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, entry, identifier, kind="", key="", title=""):
        self.hass = hass
        self.entry = entry
        self._identifier = identifier
        self._attr_unique_id = f"{entry.entry_id}-remote-{identifier}"
        self._attr_name = "远程地址"
        self._url = None
        self._base = None
        self._setup_forced_entity_id(
            "sensor", entry_token(hass, entry),
            device_token(kind, key, title) or "host", "remote")

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._identifier)}}

    @property
    def available(self):
        return bool(self._url)

    @property
    def native_value(self):
        return self._base or "未就绪"

    @property
    def extra_state_attributes(self):
        return {"url": self._url} if self._url else {}

    async def async_update(self):
        device = _device_by_identifier(self.hass, self.entry, self._identifier)
        self._url = getattr(device, "configuration_url", None) if device else None
        self._base = None
        if self._url:
            try:
                from urllib.parse import urlsplit

                parsed = urlsplit(self._url)
                if parsed.scheme and parsed.netloc:
                    self._base = f"{parsed.scheme}://{parsed.netloc}"
            except ValueError:
                self._base = None
            if not self._base:
                self._base = "已就绪"


class FnOsConsoleAddressSensor(ForcedEntityIdMixin, SensorEntity):
    """虚拟机控制台地址：从远程地址派生（target=vmconsole 重签）。

    state = 短基地址，属性 url = 完整签名控制台链接——HA state 超 255 字符
    会回退 unknown，故完整链接只放属性（与远程地址传感器同款结构）。
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = "mdi:console"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, entry, identifier, name="", title=""):
        self.hass = hass
        self.entry = entry
        self._identifier = identifier
        self._attr_unique_id = f"{entry.entry_id}-console-{identifier}"
        self._attr_name = "控制台地址"
        self._url = None
        self._base = None
        self._setup_forced_entity_id(
            "sensor", entry_token(hass, entry),
            device_token(KIND_VM, name, title), "console")

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._identifier)}}

    @property
    def available(self):
        return bool(self._url)

    @property
    def native_value(self):
        if not remote_control_enabled(self.hass):
            return "远程控制已关闭"
        return self._base or "未就绪"

    @property
    def extra_state_attributes(self):
        # 远程控制关闭时不外泄控制台链接
        if not remote_control_enabled(self.hass):
            return {}
        return {"url": self._url} if self._url else {}

    async def async_update(self):
        if not remote_control_enabled(self.hass):
            return  # 关闭期间不重签；重新开启后下一轮自动恢复
        device = _device_by_identifier(self.hass, self.entry, self._identifier)
        url = getattr(device, "configuration_url", None) if device else None
        self._url = None
        self._base = None
        if not url:
            return
        try:
            from urllib.parse import parse_qsl, urlencode, urlsplit

            from .origin import _sign

            parsed = urlsplit(url)
            query = [(k, "vmconsole" if k == "target" else v)
                     for k, v in parse_qsl(parsed.query)
                     if k != "authSig"]
            path_q = parsed.path + ("?" + urlencode(query) if query else "")
            signed = _sign(self.hass, path_q)
            full = (f"{parsed.scheme}://{parsed.netloc}{signed}"
                    if parsed.scheme and parsed.netloc else signed)
            self._url = full
            self._base = f"{parsed.scheme}://{parsed.netloc}" if full else "已就绪"
        except Exception:  # pylint: disable=broad-except
            self._url = None
            self._base = None


class FnOsRecentPhotosSensor(ForcedEntityIdMixin, SensorEntity):
    """相册「最近添加」：state = 最新一张的拍摄时间，
    属性「原图下载地址」为最近 60 张的原图代理链接。

    链接走 HA 的 /fnos/web/img 流代理并附官方 authSig 签名（24h 有效，
    本传感器轮询时自动重签），长期可点开；相册原图不对外暴露 fnOS 地址。
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = "mdi:image-multiple"

    def __init__(self, hass, entry, identifier):
        self.hass = hass
        self.entry = entry
        self._identifier = identifier
        self._attr_unique_id = f"{entry.entry_id}-photo-recent"
        self._attr_name = "最近添加"
        self._time = None
        self._urls = None
        self._setup_forced_entity_id(
            "sensor", entry_token(hass, entry), "photo", "recent")

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._identifier)}}

    @property
    def available(self):
        return bool(self._urls)

    @property
    def native_value(self):
        return self._time or "未就绪"

    @property
    def extra_state_attributes(self):
        if not self._urls:
            return None
        return {"数量": len(self._urls), "原图下载地址": self._urls}

    async def async_update(self):
        store = self.hass.data.get(DOMAIN) or {}
        coord = (store.get(self.entry.entry_id) or {}).get("coordinator")
        client = getattr(coord, "client", None)
        if client is None:
            return
        try:
            photos = await self.hass.async_add_executor_job(
                client.photo_recent, 0, 60)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS 最近添加获取失败", exc_info=True)
            return  # 失败保留上次结果，避免属性闪空
        if not photos:
            self._time = None
            self._urls = None
            return
        from urllib.parse import quote

        from .origin import _sign

        base = ""
        try:
            from homeassistant.helpers.network import get_url

            base = get_url(self.hass, prefer_external_if_no_cert=True)
        except Exception:  # pylint: disable=broad-except
            base = ""
        urls = []
        latest = None
        for row in photos:
            ts = str(row.get("dateTime") or "")
            if latest is None and ts:
                latest = ts
            thumb = (row.get("additional") or {}).get("thumbnail") or {}
            orig = (thumb.get("originalUrl") or thumb.get("mUrl")
                    or thumb.get("sUrl"))
            if not orig:
                continue
            # 原图流路径直接透传（已是 /p/api/v1/stream/... 形态）
            path = f"/fnos/web/img{quote(str(orig), safe='/')}"
            signed = _sign(self.hass, f"{path}?entry={self.entry.entry_id}")
            urls.append(f"{base}{signed}" if base else signed)
        if not urls:
            return
        self._urls = urls
        date, _, tm = latest.partition(" ")
        self._time = (f"{date.replace(':', '-')} {tm[:5]}"
                      if tm else (date or None))


# 设备 kind → (分组排序, 中文名)；未列出的 kind 归入「其他」
_GROUP_META = {
    KIND_HOST: (0, "主机"),
    KIND_FILE: (1, "文件"),
    KIND_PHOTO: (2, "相册"),
    KIND_VIDEO: (3, "影视"),
    KIND_DOCKER: (4, "Docker 容器"),
    KIND_VM: (5, "虚拟机"),
}
_GROUP_RANK_OTHER = 6


class FnOsAllEntitiesSensor(ForcedEntityIdMixin, SensorEntity):
    """所有实体：把本集成全部实体按「设备」分组汇总到一个实体里。

    * state = 实体总数（不含自身与禁用实体）；
    * 属性 ``groups`` = 设备节点列表，**一台设备一个节点**：
      ``{key, name, kind, label, count, entities}``

      - ``key`` 取设备标识 ``<entry>:<kind>[:<key>]`` 的 ``kind[:key]`` 部分，
        如 ``host`` / ``docker:nginx`` / ``vm:win10``；
      - ``name`` 是设备在 HA 里的显示名（容器/虚拟机名就在这里）；
      - ``kind`` / ``label`` 是设备类型（英文 / 中文）；
      - ``entities`` 每项 ``{entity_id, name, state, icon}``：``name`` 是
        实体自身名称（不含设备名前缀，如 ``系统更新``），设备名看分组
        ``name``；``state`` 是采集时刻的值，实时值请直接读该实体。
    * 其余属性：``total`` / ``listed`` / ``classes`` / ``truncated`` / ``omitted``。

    全部从实体/设备注册表实时重建，所以新增 Docker 容器、虚拟机等设备会自动
    出现在列表里：注册表变化 1 秒防抖刷新、实体状态变化 5 秒合并刷新、
    30 秒轮询兜底（防止漏事件）。

    整包属性不写 recorder（``_unrecorded_attributes``）：状态机 / 模板 / 前端
    照常可读，同时绕开 recorder 16384 字节的单实体属性上限。想连状态值也不
    进历史库，可在 ``configuration.yaml`` 里加：

        recorder:
          exclude:
            entities:
              - sensor.fnos_all_entities
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_icon = "mdi:format-list-bulleted-square"
    # 整包属性不写数据库（状态机/模板仍然可读）
    _unrecorded_attributes = frozenset(
        {"total", "listed", "classes", "truncated", "omitted", "groups"})

    # 兜底体积上限（属性不进库，正常情况下远小于它）
    _ATTR_BUDGET = 60000
    # 状态变化后的合并刷新间隔（秒），避免每次状态变化都重写这份大属性
    _FLUSH_DELAY = 5.0

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}-all-entities"
        self._attr_name = "所有实体"
        self._payload = None
        self._ids: set = set()
        self._pending = None
        self._refresh_handle = None
        # 固定英文实体 ID：sensor.fnos_all_entities
        # （多条目时为 sensor.fnos_<主机>_all_entities，避免抢号）
        self._setup_forced_entity_id(
            "sensor", entry_token(hass, entry), "all_entities")

    @property
    def device_info(self):
        return {"identifiers": {
            (DOMAIN, device_identifier(self.entry, KIND_HOST))}}

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self._ensure_payload().get("total")

    @property
    def extra_state_attributes(self):
        return self._ensure_payload()

    # -- 刷新时机 -------------------------------------------------------- #
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._handle_state_changed))
        self.async_on_remove(self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_event))
        self.async_on_remove(self.hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, self._handle_device_event))
        self.async_on_remove(self._cancel_timers)
        self._payload = self._build_payload()  # 首次先把 _ids 建出来

    def _cancel_timers(self):
        for timer in (self._pending, self._refresh_handle):
            if timer is not None:
                timer()
        self._pending = None
        self._refresh_handle = None

    async def _flush_soon(self, _now=None):
        self._pending = None
        self._payload = None
        self.async_write_ha_state()

    async def _refresh_now(self, _now=None):
        self._refresh_handle = None
        self._payload = None
        self.async_write_ha_state()

    def _handle_state_changed(self, event):
        """只关心本集成的实体；合并 5 秒刷新一次，别把大属性反复写。"""
        entity_id = (event.data or {}).get("entity_id")
        if (not entity_id or entity_id == self.entity_id
                or entity_id not in self._ids):
            return
        if self._pending is not None:
            return
        self._pending = async_call_later(
            self.hass, self._FLUSH_DELAY, self._flush_soon)

    def _schedule_refresh(self):
        if self._refresh_handle is not None:
            return
        self._refresh_handle = async_call_later(
            self.hass, 1.0, self._refresh_now)

    def _handle_registry_event(self, event):
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return
        if entity_id in self._ids:
            # 已知实体的改名/改图标/删除（删除后注册表里查不到了）
            self._schedule_refresh()
            return
        entity = er.async_get(self.hass).async_get(entity_id)
        if entity is not None and entity.config_entry_id == self.entry.entry_id:
            self._schedule_refresh()

    def _handle_device_event(self, event):
        device_id = event.data.get("device_id")
        if not device_id:
            return
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            # 删除事件：设备已经不在注册表里，无法判断归属 → 廉价重建一次
            self._schedule_refresh()
            return
        if self.entry.entry_id in device.config_entries:
            self._schedule_refresh()

    async def async_update(self):
        """30 秒轮询兜底：只清缓存，重建发生在写状态时（懒加载）。"""
        self._payload = None

    # -- 分类与汇总 ------------------------------------------------------ #
    def _classify(self, device):
        """设备标识 ``<entry>:<kind>[:<key>]`` → (key, kind, 设备名)。"""
        if device is None:
            return None
        name = device.name_by_user or device.name or device.id
        for domain, identifier in device.identifiers or ():
            if domain != DOMAIN:
                continue
            parts = identifier.split(":", 2)
            if len(parts) < 2:
                continue
            kind = parts[1]
            sub = parts[2] if len(parts) > 2 else ""
            return (f"{kind}:{sub}" if sub else kind), kind, name
        return None

    @staticmethod
    def _size(obj) -> int:
        return len(json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _short_name(entry, attributes, device_name, entity_id):
        """实体自身名称，去掉设备名前缀（``… NUCfnOS 系统更新`` → ``系统更新``）。

        ``friendly_name`` 在 HA 里是「设备名 + 实体名」，而本属性已按设备分组，
        前缀只是重复。短名优先取注册表里的重命名（``name``）/ 代码名
        （``original_name``），最后才退到 ``friendly_name``；每个候选都再剥一次
        设备名前缀，兼容早期把完整名写进注册表的情况。
        """
        prefix = f"{device_name} " if device_name else ""

        def _clean(value):
            text = str(value)
            if prefix and text.startswith(prefix):
                return text[len(prefix):]
            return text

        for value in (getattr(entry, "name", None),
                      getattr(entry, "original_name", None),
                      attributes.get("friendly_name")):
            if value:
                return _clean(value)
        return entity_id

    def _build_payload(self) -> dict:
        try:
            registry = er.async_get(self.hass)
            dev_reg = dr.async_get(self.hass)
        except Exception:  # pylint: disable=broad-except
            return {"total": 0, "listed": 0, "classes": 0,
                    "truncated": False, "omitted": 0, "groups": []}

        groups: dict = {}
        ids: set = set()
        for entry in er.async_entries_for_config_entry(
                registry, self.entry.entry_id):
            if entry.disabled_by:
                # 禁用条目在状态机里并不存在，列出来只是噪音
                continue
            entity_id = entry.entity_id
            if entity_id == self.entity_id:
                continue  # 不把自己算进去（否则自己写状态会自激）
            device = (dev_reg.async_get(entry.device_id)
                      if entry.device_id else None)
            classified = self._classify(device)
            if classified is None:
                continue
            key, kind, device_name = classified
            state = self.hass.states.get(entity_id)
            attributes = state.attributes if state is not None else {}
            group = groups.setdefault(key, {
                "key": key,
                "name": device_name,
                "kind": kind,
                "label": _GROUP_META.get(kind, (_GROUP_RANK_OTHER, "其他"))[1],
                "count": 0,
                "entities": [],
            })
            group["entities"].append({
                "entity_id": entity_id,
                # 只留实体自身名称：分组已带设备名，避免「飞牛主机 · X 系统更新」
                "name": self._short_name(entry, attributes, device_name,
                                         entity_id),
                "state": state.state if state is not None else None,
                # 图标优先级：状态里的 icon → 注册表手工改的 → 代码声明的
                "icon": (attributes.get("icon")
                         or getattr(entry, "icon", None)
                         or getattr(entry, "original_icon", None)),
            })
            group["count"] += 1
            ids.add(entity_id)

        # 分组顺序：主机 → 文件 → 相册 → 影视 → Docker → 虚拟机 → 其他（同名按名称）
        ordered = sorted(
            groups.values(),
            key=lambda g: (_GROUP_META.get(
                g["kind"], (_GROUP_RANK_OTHER, ""))[0], g["name"]))

        total = len(ids)
        payload = {"total": total, "listed": total, "classes": len(ordered),
                   "truncated": False, "omitted": 0, "groups": ordered}
        omitted = 0
        current = self._size(payload)
        while current > self._ATTR_BUDGET and any(g["entities"] for g in ordered):
            # 轮着从各设备尾部裁，保证每台设备都还在，计数仍是真实的
            for group in ordered:
                if current <= self._ATTR_BUDGET:
                    break
                if not group["entities"]:
                    continue
                item = group["entities"].pop()
                current -= self._size(item) + 1
                group["count"] -= 1
                omitted += 1
        payload["listed"] = total - omitted
        if omitted:
            payload["truncated"] = True
            payload["omitted"] = omitted
            _LOGGER.debug(
                "fnOS 所有实体属性超过 %d 字节，已裁剪 %d 个实体（total=%d）",
                self._ATTR_BUDGET, omitted, total)
        self._ids = ids
        return payload

    def _ensure_payload(self) -> dict:
        if self._payload is None:
            self._payload = self._build_payload()
        return self._payload


def _container(snap, key):
    for row in (snap.get("docker") or {}).get("containers") or []:
        if row.get("name") == key:
            return row
    return {}


# --------------------------------------------------------------------------- #
# 虚拟机配置传感器（数据源 /domain/info 详情接口）
# --------------------------------------------------------------------------- #
# 短 TTL 缓存：一台虚拟机的配置被 7 个传感器共享，一轮只拉一次详情
_VM_INFO_CACHE: dict = {}


async def _vm_info_cached(hass, coordinator, name, ttl=60.0):
    """带 TTL 的 vm_info；失败时回退上次结果。"""
    now = time.monotonic()
    hit = _VM_INFO_CACHE.get(name)
    if hit and now - hit[0] < ttl:
        return hit[1]
    client = getattr(coordinator, "client", None)
    if client is None:
        return hit[1] if hit else None
    try:
        info = await hass.async_add_executor_job(client.vm_info, name)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("fnOS vm_info 获取失败: %s", name, exc_info=True)
        return hit[1] if hit else None
    info = info or {}
    _VM_INFO_CACHE[name] = (now, info)
    return info


class FnOsVmInfoSensor(ForcedEntityIdMixin, SensorEntity):
    """虚拟机配置传感器：从 domain/info 取值。

    parse_fn(info) → (值, 单位, 属性)；值为 None 时显示「未知」。
    """

    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, coordinator, entry, kind, vm_name,
                 key, name, icon, parse_fn, title=""):
        self.hass = hass
        self._coordinator = coordinator
        self.entry = entry
        self._identifier = device_identifier(entry, kind, vm_name)
        self._vm = vm_name
        self._parse = parse_fn
        self._attr_unique_id = f"{entry.entry_id}-{kind}-{vm_name}-{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._value = None
        self._unit = None
        self._attrs = None
        self._setup_forced_entity_id(
            "sensor", entry_token(hass, entry),
            device_token(kind, vm_name, title), key)

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self._identifier)}}

    @property
    def native_value(self):
        return self._value if self._value is not None else "未知"

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def extra_state_attributes(self):
        return self._attrs

    async def async_update(self):
        info = await _vm_info_cached(
            self.hass, self._coordinator, self._vm)
        if not info:
            self._value = None
            self._attrs = None
            return
        try:
            value, unit, attrs = self._parse(info)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("fnOS vm_info 解析异常: %s", self._vm,
                          exc_info=True)
            return
        self._value = value
        self._unit = unit
        self._attrs = attrs


def _vm_mem_parse(info):
    raw = info.get("memCapacityInBytes")
    val = _human_bytes(raw)
    if val is None:
        return None, None, {"raw": raw}
    return val[0], val[1], {"raw": raw}


def _vm_disk_parse(info):
    disks = info.get("disks") or []
    total = 0
    detail = []
    for d in disks:
        try:
            cap = int(d.get("capacityInBytes") or 0)
        except (TypeError, ValueError):
            cap = 0
        total += cap
        unit = _human_bytes(cap)
        detail.append({
            "池": d.get("poolName"),
            "总线": d.get("busType"),
            "容量": f"{unit[0]} {unit[1]}" if unit else None,
        })
    if not total:
        return None, None, None
    val = _human_bytes(total)
    return val[0], val[1], {"磁盘数": len(disks), "明细": detail}


def _vm_mac_parse(info):
    nets = info.get("networks") or []
    macs = [str(n.get("macAddr") or "") for n in nets]
    mac = next((m for m in macs if m), None)
    attrs = None
    if nets:
        n0 = nets[0]
        attrs = {
            "网络类型": n0.get("type"),
            "来源": n0.get("source"),
            "型号": n0.get("nicModel"),
            "全部MAC": macs,
        }
    return mac, None, attrs


# --------------------------------------------------------------------------- #
# 硬件信息（appcgi.sysinfo.getHardwareInfo + 快照里已有的资源/存储/网卡数据）
# --------------------------------------------------------------------------- #
def _hw_norm(key) -> str:
    """键名归一化：忽略大小写与 _ - . 等分隔符（结构未公开，防御式匹配）。"""
    return "".join(ch for ch in str(key or "").lower() if ch.isalnum())


def _hw_find(data, names, depth=0):
    """递归找第一个键名匹配的节点（dict / list / 标量都返回），找不到 None。

    先在同一层精确匹配键名，再往子层递归 —— 这样 ``{"cpu": {...}}`` 和
    ``{"data": {"cpu": {...}}}`` 两种包装都能取到。
    """
    if depth > 6:
        return None
    targets = {_hw_norm(name) for name in names}
    if isinstance(data, dict):
        for key, value in data.items():
            if _hw_norm(key) in targets and value not in (None, "", [], {}):
                return value
        for value in data.values():
            found = _hw_find(value, names, depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _hw_find(item, names, depth + 1)
            if found is not None:
                return found
    return None


def _hw_size(value):
    """字节数 → '8.0 GB' 这种可读字符串（属性里不要放 tuple）。"""
    result = _human_bytes(value)
    if isinstance(result, tuple) and len(result) == 2:
        return f"{result[0]} {result[1]}"
    return None


def _hw_text(*parts):
    """把若干片段（None 自动跳过）拼成一行。"""
    return " ".join(str(part) for part in parts if part) or None


def _disk_get(disk, *keys):
    """listDisk 行内取第一个非空字段（只看当前层，避免串进 storage[]）。"""
    for key in keys:
        value = disk.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _disk_row(disk):
    """一块物理盘的摘要（硬件信息 / 硬盘健康两个实体共用）。"""
    if not isinstance(disk, dict):
        return {}
    states = disk_health_states(disk)
    health = "/".join(states) or _hw_text(
        _hw_find(disk, ("health", "smartStatus", "smart")))
    if states:
        status = "异常" if disk_is_bad(states) else "正常"
    else:
        text = str(health or "").strip().lower()
        if not text:
            status = "未知"
        elif text in DISK_WARN_STATES or any(
                word in text for word in ("fail", "bad", "error", "degrad")):
            status = "异常"
        else:
            status = "正常"

    pools = []
    for storage in disk.get("storage") or []:
        if not isinstance(storage, dict):
            continue
        pool = (storage.get("poolName") or storage.get("storName")
                or storage.get("name") or storage.get("pool"))
        if pool and str(pool) not in pools:
            pools.append(str(pool))

    partitions = _disk_get(disk, "partition", "partitions", "part")
    system = _disk_get(disk, "system", "isSystem", "systemDisk", "boot",
                       "isBoot")
    system_flag = None
    if system is not None:
        system_flag = str(system).strip().lower() not in ("0", "false", "no", "")
    dtype = _disk_get(disk, "type", "diskType", "mediaType", "rota")
    if not dtype:
        rotational = _disk_get(disk, "rotational", "isRotational")
        if rotational is not None:
            dtype = "HDD" if str(rotational).strip().lower() in (
                "1", "true", "yes") else "SSD"

    row = {
        "设备": _disk_get(disk, "name", "dev", "devName", "device"),
        "型号": _disk_get(disk, "model", "modelName"),
        "序列号": _disk_get(disk, "serial", "serialNumber", "sn"),
        "容量": _hw_size(_disk_get(disk, "size", "capacity", "total")),
        "类型": dtype,
        "温度": _hw_find(disk, ("temp", "temperature", "temperatureC")),
        "通电时间": _hw_find(disk, ("powerOnHours", "poweron", "powerOnTime",
                                    "powerOn", "hours", "runningTime")),
        "健康": health,
        "状态": status,
        "系统盘": system_flag,
        "存储池": "/".join(pools) or None,
        "分区数": len(partitions) if isinstance(partitions, list) else None,
    }
    return {key: val for key, val in row.items()
            if val not in (None, "", [], {})}


def _disk_summary(snap):
    """硬盘健康摘要：(state, attributes)。"""
    rows = [_disk_row(disk)
            for disk in (snap.get("storage") or {}).get("disks") or []]
    rows = [row for row in rows if row]
    bad = [row for row in rows if row.get("状态") == "异常"]
    good = [row for row in rows if row.get("状态") == "正常"]
    if not rows or (not good and not bad):
        state = "未知"  # 有盘但健康状态全都判定不出来
    elif bad:
        state = "异常"
    else:
        state = "正常"
    return state, {
        "硬盘数量": len(rows),
        "正常": len(good),
        "异常": len(bad),
        "未知": len(rows) - len(good) - len(bad),
        "硬盘": rows,
    }


#: 存储池状态关键词（stor.state2 的池状态字段，口径未公开，防御式归并）
_POOL_OK_WORDS = ("normal", "healthy", "online", "active", "clean", "good",
                  "ok", "ready", "in_sync", "mounted", "inuse")
_POOL_BAD_WORDS = ("degrad", "fail", "error", "offline", "missing",
                   "abnormal", "unmount", "damage", "rebuild", "recover",
                   "stop", "readonly")


def _pool_status(raw):
    """池状态归并：正常 / 异常 / 未知。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "未知"
    if any(word in text for word in _POOL_BAD_WORDS):
        return "异常"
    if any(word in text for word in _POOL_OK_WORDS):
        return "正常"
    return "未知"


def _pool_row(pool):
    """一个存储池的摘要（stor.state2 → data.state[]）。"""
    if not isinstance(pool, dict):
        return {}

    # 状态：优先阵列成员 arrState（与硬盘同口径），否则取池自身的状态字段
    states = disk_health_states(pool)
    if states:
        raw_status = "/".join(states)
        health = "异常" if disk_is_bad(states) else "正常"
    else:
        raw_status = _disk_get(pool, "status", "state", "health", "poolState")
        if raw_status is None:
            raw_status = _hw_text(_hw_find(pool, ("status", "state", "health")))
        health = _pool_status(raw_status)

    total = _disk_get(pool, "size", "total", "capacity", "totalSize",
                      "sizeTotal")
    free = _disk_get(pool, "free", "avail", "available", "freeSize",
                     "sizeFree")
    used = _disk_get(pool, "used", "usedSize", "sizeUsed")
    if used is None and isinstance(total, (int, float)) and isinstance(
            free, (int, float)):
        used = max(total - free, 0)
    usage = None
    if isinstance(total, (int, float)) and total and isinstance(
            used, (int, float)):
        usage = round(used * 100.0 / total, 1)

    members = _disk_get(pool, "disks", "disk", "members", "diskList",
                       "devices")
    if isinstance(members, list):
        disk_count = len(members)
    else:
        disk_count = _disk_get(pool, "diskCount", "diskNum", "numDisks",
                               "ndisks")

    row = {
        "名称": _disk_get(pool, "name", "poolName", "storName", "label",
                        "title"),
        "状态": raw_status,
        "健康": health,
        "类型": _disk_get(pool, "type", "raidType", "level", "fsType", "fs",
                        "poolType"),
        "总容量": _hw_size(total),
        "已用": _hw_size(used),
        "可用": _hw_size(free),
        "使用率": usage,
        "磁盘数": disk_count,
        "挂载点": _disk_get(pool, "mountPoint", "mount", "mountPath", "path"),
        "设备": _disk_get(pool, "dev", "device", "dm", "dmName"),
    }
    return {key: val for key, val in row.items()
            if val not in (None, "", [], {})}


def _pool_summary(snap):
    """存储池摘要：(state, attributes)；state = 存储池数量。"""
    pools = (snap.get("storage") or {}).get("pools") or []
    rows = [_pool_row(pool) for pool in pools]
    rows = [row for row in rows if row]
    bad = [row for row in rows if row.get("健康") == "异常"]
    good = [row for row in rows if row.get("健康") == "正常"]
    attrs = {
        "存储池数量": len(rows),
        "正常": len(good),
        "异常": len(bad),
        "未知": len(rows) - len(good) - len(bad),
        "存储池": rows,
    }
    # 原始返回保留，方便核对字段名（不写 recorder，见类上的配置）
    raw = [pool for pool in pools if isinstance(pool, dict)]
    if raw:
        attrs["原始数据"] = raw
    return len(rows), attrs


def _hw_attributes(snap):
    """硬件信息实体的属性：getHardwareInfo 为主，快照里已有数据补齐。"""
    hw = snap.get("hardware")
    hw = hw if isinstance(hw, dict) else {}
    resmon = snap.get("resmon") or {}
    host = snap.get("host") or {}
    attrs = {}

    # ---- CPU ----
    cpu = _hw_find(hw, ("cpu", "processor"))
    cpu = cpu if isinstance(cpu, dict) else {}
    cpu_model = (resmon.get("cpu_name")
                 or _hw_find(cpu, ("model", "name", "brand", "product",
                                   "modelName")))
    if cpu_model:
        attrs["CPU"] = cpu_model
    cores = resmon.get("cpu_core") or _hw_find(cpu, ("core", "cores", "coreCnt",
                                                     "cpuCoreCnt"))
    threads = (resmon.get("cpu_thread")
               or _hw_find(cpu, ("thread", "threads", "threadCnt")))
    if cores:
        attrs["CPU 核心"] = cores
    if threads:
        attrs["CPU 线程"] = threads
    if resmon.get("cpu_temp") is not None:
        attrs["CPU 温度"] = resmon.get("cpu_temp")

    # ---- 内存 ----
    mem = _hw_find(hw, ("mem", "memory", "ram"))
    mem = mem if isinstance(mem, dict) else {}
    mem_total = (resmon.get("mem_total")
                 or _hw_find(mem, ("total", "totalSize", "size", "capacity")))
    mem_text = _hw_size(mem_total)
    if mem_text:
        attrs["内存总量"] = mem_text
    modules = _hw_find(mem, ("list", "dimms", "modules", "items", "slots",
                             "rams"))
    if isinstance(modules, list) and modules:
        rows = []
        for module in modules:
            if not isinstance(module, dict):
                continue
            rows.append({key: val for key, val in (
                ("容量", _hw_size(_hw_find(module, ("size", "capacity",
                                                    "total")))),
                ("频率", _hw_find(module, ("speed", "frequency", "clock"))),
                ("类型", _hw_find(module, ("type", "dramType", "memType"))),
                ("厂商", _hw_find(module, ("manufacturer", "vendor", "brand"))),
                ("槽位", _hw_find(module, ("slot", "locator", "name"))),
            ) if val not in (None, "", [], {})})
        if rows:
            attrs["内存条"] = rows
            attrs["内存条数"] = len(rows)

    # ---- GPU ----
    gpu = _hw_find(hw, ("gpu", "graphics", "display"))
    gpu_name = resmon.get("gpu_name")
    if not gpu_name and gpu is not None:
        gpu_name = (gpu if isinstance(gpu, str)
                    else _hw_find(gpu, ("model", "name", "device", "deviceName")))
    if gpu_name:
        attrs["GPU"] = gpu_name
    gpu_ram = resmon.get("gpu_ram_total")
    if not gpu_ram and gpu is not None:
        gpu_ram = _hw_find(gpu, ("ram", "mem", "memory"))
        if isinstance(gpu_ram, dict):
            gpu_ram = _hw_find(gpu_ram, ("total", "size"))
    gpu_ram_text = _hw_size(gpu_ram)
    if gpu_ram_text:
        attrs["GPU 显存"] = gpu_ram_text

    # ---- 主板 / BIOS / 整机 ----
    board = _hw_find(hw, ("board", "motherboard", "mainboard", "baseboard"))
    if isinstance(board, dict):
        board_text = _hw_text(
            _hw_find(board, ("manufacturer", "vendor", "brand")),
            _hw_find(board, ("product", "name", "model")))
    else:
        board_text = board if isinstance(board, str) else None
    if board_text:
        attrs["主板"] = board_text

    bios = _hw_find(hw, ("bios",))
    if isinstance(bios, dict):
        bios_text = _hw_text(
            _hw_find(bios, ("vendor", "manufacturer")),
            _hw_find(bios, ("version",)),
            _hw_find(bios, ("date", "releaseDate", "release")))
        if bios_text:
            attrs["BIOS"] = bios_text

    machine = _hw_find(hw, ("machine", "system", "product"))
    if isinstance(machine, dict):
        machine_text = _hw_text(
            _hw_find(machine, ("manufacturer", "vendor", "brand")),
            _hw_find(machine, ("product", "model", "name")))
        serial = _hw_find(machine, ("serial", "serialNumber", "sn"))
        if machine_text:
            attrs["机型"] = machine_text
        if serial:
            attrs["序列号"] = serial
    elif isinstance(machine, str) and machine.strip():
        attrs["机型"] = machine.strip()

    virt = _hw_find(hw, ("vm", "virtualization", "virt", "vmm"))
    if isinstance(virt, dict):
        flag = _hw_find(virt, ("enable", "enabled", "support", "supported",
                              "available", "vt", "vmx"))
        if flag is not None:
            attrs["支持虚拟化"] = bool(flag)
    elif isinstance(virt, bool):
        attrs["支持虚拟化"] = virt

    # ---- 系统 ----
    version = _hw_text(host.get("version"), host.get("edition"))
    if version:
        attrs["系统版本"] = version
    if host.get("platform"):
        attrs["平台"] = host.get("platform")

    # ---- 硬盘（stor.listDisk，型号/序列号/容量/健康） ----
    disk_rows = [_disk_row(disk)
                 for disk in (snap.get("storage") or {}).get("disks") or []]
    disk_rows = [row for row in disk_rows if row]
    if disk_rows:
        attrs["硬盘"] = disk_rows

    # ---- 网卡（appcgi.network.net.list） ----
    nic_rows = []
    for nic in (snap.get("net") or {}).get("interfaces") or []:
        if not isinstance(nic, dict):
            continue
        row = {
            "名称": nic.get("name") or nic.get("ifname") or nic.get("dev"),
            "IP": nic.get("ipv4") or nic.get("ip") or nic.get("ipv4Addr"),
            "MAC": nic.get("mac") or nic.get("macAddr") or nic.get("hwaddr"),
            "速率": nic.get("speed"),
            "MTU": nic.get("mtu"),
        }
        nic_rows.append({k: v for k, v in row.items()
                         if v not in (None, "", [], {})})
    if nic_rows:
        attrs["网卡"] = nic_rows

    # 原始返回原样保留：字段名后续可据此精确映射
    if hw:
        attrs["原始数据"] = hw
    return attrs


def _hw_state(snap):
    """硬件信息实体的 state：CPU 型号（取不到退回主机名）。"""
    attrs = _hw_attributes(snap)
    return attrs.get("CPU") or (snap.get("host") or {}).get("hostname") or "未知"


class FnOsHardwareSensor(FnOsEntity, SensorEntity):
    """硬件信息：CPU / 内存条 / GPU / 主板 / BIOS / 硬盘 / 网卡 / 虚拟化能力。

    数据源：
      * ``appcgi.sysinfo.getHardwareInfo``（免登录）——CPU/内存/GPU/BIOS/主板；
      * ``appcgi.resmon.cpu`` / ``.mem`` / ``.gpu`` —— 型号、核心线程、内存与显存；
      * ``stor.listDisk`` —— 硬盘型号/序列号/容量/阵列健康；
      * ``appcgi.network.net.list`` —— 网卡 IP/MAC/速率。

    state = CPU 型号；其余全部放属性。原始返回原样保留在「原始数据」里。
    """

    _forced_domain = "sensor"
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, KIND_HOST, "", "hardware")
        self._attr_name = "硬件信息"

    @property
    def native_value(self):
        return _hw_state(self.snap)

    @property
    def extra_state_attributes(self):
        return _hw_attributes(self.snap)


class FnOsDiskHealthSensor(FnOsEntity, SensorEntity):
    """硬盘健康状态：state = 正常 / 异常 / 未知，属性存每块硬盘的详细数据。

    数据源 ``stor.listDisk``（型号、序列号、容量、分区、系统盘标记）+
    ``disk[].storage[].arrState``（阵列健康）；每块盘一条：

        {设备, 型号, 序列号, 容量, 类型, 温度, 通电时间,
         健康, 状态, 系统盘, 存储池, 分区数}

    「健康」为原始 arrState（如 ``in_sync``）或盘自带的 health 字段；
    「状态」是按统一口径（api.DISK_GOOD_STATES / DISK_WARN_STATES）
    归并出的 正常 / 异常 / 未知，与 ``binary_sensor.fnos_disk_bad`` 同源。
    """

    _forced_domain = "sensor"
    _attr_icon = "mdi:harddisk"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # 原始 listDisk 字段只给前端核对（不写 recorder，避免属性超限）
    _unrecorded_attributes = frozenset({"原始数据"})

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, KIND_HOST, "", "disk_health")
        self._attr_name = "硬盘健康状态"

    @property
    def native_value(self):
        return _disk_summary(self.snap)[0]

    @property
    def extra_state_attributes(self):
        state, attrs = _disk_summary(self.snap)
        raw = [disk for disk in (self.snap.get("storage") or {}).get("disks") or []
               if isinstance(disk, dict)]
        if raw:
            attrs["原始数据"] = raw
        return attrs


class FnOsStoragePoolSensor(FnOsEntity, SensorEntity):
    """存储池：state = 存储池数量，属性存每个存储池的详细信息。

    数据源 ``stor.state2`` → ``storage.pools[]``；每个池一条：

        {名称, 状态, 健康, 类型, 总容量, 已用, 可用, 使用率,
         磁盘数, 挂载点, 设备}

    * 「状态」为池自身的原始状态（或成员 ``arrState`` 拼接）；
    * 「健康」按统一口径归并为 正常 / 异常 / 未知 —— 池带阵列成员时
      与 ``sensor.fnos_disk_health`` 同源，不会各算各的；
    * 容量口径与「存储总容量/可用容量」一致（字节 → 可读单位）。
    """

    _forced_domain = "sensor"
    _attr_icon = "mdi:database"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # 原始池字段只给前端/模板看，不写 recorder（避免属性超限）
    _unrecorded_attributes = frozenset({"原始数据"})

    def __init__(self, coordinator, entry):
        # unique_id 沿用旧的 pool_count（旧实体平滑改名为 sensor.fnos_pools）
        super().__init__(coordinator, entry, KIND_HOST, "", "pool_count",
                         id_suffix="pools")
        self._attr_name = "存储池"

    @property
    def native_value(self):
        return _pool_summary(self.snap)[0]

    @property
    def extra_state_attributes(self):
        return _pool_summary(self.snap)[1]


# --------------------------------------------------------------------------- #
# 平台装配
# --------------------------------------------------------------------------- #
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback):
    """安装传感器（含容器/虚拟机动态跟随）。"""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list = []
    entities.extend(_host_sensors(coordinator, entry))
    entities.extend(_file_sensors(coordinator, entry))
    entities.extend(_docker_sensors(coordinator, entry))
    entities.extend(_vm_sensors(hass, coordinator, entry))

    remote_specs = [
        (device_identifier(entry, KIND_HOST), KIND_HOST, "", ""),
        (device_identifier(entry, KIND_FILE), KIND_FILE, "", ""),
        (device_identifier(entry, KIND_PHOTO), KIND_PHOTO, "", ""),
        (device_identifier(entry, KIND_VIDEO), KIND_VIDEO, "", ""),
    ]
    vm_specs = []
    for row in (coordinator.data.get("docker") or {}).get("containers") or []:
        if row.get("name"):
            remote_specs.append((device_identifier(entry, KIND_DOCKER,
                                                   row["name"]),
                                 KIND_DOCKER, row["name"], ""))
    for vm in (coordinator.data.get("vms") or {}).get("list") or []:
        vm_name = str(vm.get("name") or "")
        if vm_name:
            vm_title = str(vm.get("title") or "")
            vm_ident = device_identifier(entry, KIND_VM, vm_name)
            remote_specs.append((vm_ident, KIND_VM, vm_name, vm_title))
            vm_specs.append((vm_ident, vm_name, vm_title))
    entities.extend(
        FnOsRemoteAddressSensor(hass, entry, ident, kind, key, title)
        for ident, kind, key, title in remote_specs
    )
    # 虚拟机设备额外提供「控制台地址」传感器（target=vmconsole 链接）
    entities.extend(
        FnOsConsoleAddressSensor(hass, entry, ident, name, title)
        for ident, name, title in vm_specs
    )
    # 相册设备提供「最近添加」传感器（60 张原图下载地址 + 最新时间）
    entities.append(
        FnOsRecentPhotosSensor(hass, entry,
                               device_identifier(entry, KIND_PHOTO)))
    # 全部实体总览（按设备分组，新增设备自动跟随）
    entities.append(FnOsAllEntitiesSensor(hass, entry))

    async_add_entities(entities)

    # 容器/虚拟机增删跟随：闭包记录已建 unique_id，60 秒一查，只补缺
    known: set = {e._attr_unique_id for e in entities}

    def _track_new(_now=None):
        snap = coordinator.data or {}
        new_entities = []
        for row in (snap.get("docker") or {}).get("containers") or []:
            if row.get("name"):
                new_entities.extend(_docker_sensors(
                    coordinator, entry, only_name=row["name"], known=known))
        for vm in (snap.get("vms") or {}).get("list") or []:
            vm_name = str(vm.get("name") or "")
            if vm_name:
                new_entities.extend(_vm_sensors(
                    hass, coordinator, entry,
                    only_name=vm_name, known=known))
        known.update(e._attr_unique_id for e in new_entities)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        async_track_time_interval(hass, _track_new, timedelta(seconds=60)))


# --------------------------------------------------------------------------- #
# 主机设备传感器
# --------------------------------------------------------------------------- #
def _host_sensors(coordinator, entry):
    kind = KIND_HOST
    host = lambda s: s.get("host") or {}  # noqa: E731
    resmon = lambda s: s.get("resmon") or {}  # noqa: E731
    storage = lambda s: s.get("storage") or {}  # noqa: E731
    download = lambda s: s.get("download") or {}  # noqa: E731
    external = lambda s: s.get("external") or {}  # noqa: E731

    return [
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "cpu", "CPU 使用率",
            unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:cpu-64",
            value_fn=lambda s: _num(resmon(s).get("cpu_pct"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "mem_pct", "内存使用率",
            unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:memory",
            value_fn=lambda s: _num(resmon(s).get("mem_pct")),
            attrs_fn=lambda s: {
                "available": _human_bytes(resmon(s).get("mem_available")),
                "swap_used": _human_bytes(resmon(s).get("swap_used")),
                "swap_total": _human_bytes(resmon(s).get("swap_total"))}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "mem_used", "内存已用",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:memory",
            value_fn=lambda s: _human_bytes(resmon(s).get("mem_used"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "mem_total", "内存总量",
            icon="mdi:memory", entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda s: _human_bytes(resmon(s).get("mem_total"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "gpu", "GPU 使用率",
            unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:expansion-card",
            value_fn=lambda s: _num(resmon(s).get("gpu_pct")),
            attrs_fn=lambda s: {"gpu": resmon(s).get("gpu_name")}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "net_down", "网络下载速率",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:download-network",
            value_fn=lambda s: _human_speed(resmon(s).get("net_down"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "net_up", "网络上传速率",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:upload-network",
            value_fn=lambda s: _human_speed(resmon(s).get("net_up"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "disk_read", "磁盘读取速率",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:read",
            value_fn=lambda s: _human_speed(resmon(s).get("disk_read"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "disk_write", "磁盘写入速率",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:pencil",
            value_fn=lambda s: _human_speed(resmon(s).get("disk_write"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "fan", "风扇转速",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:fan",
            value_fn=lambda s: _num(resmon(s).get("fan_rpm"), 0),
            attrs_fn=lambda s: {
                "fan_mode": host(s).get("fan_mode"),
                "cpu_temp": resmon(s).get("cpu_temp")}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "uptime", "本次运行开始时间",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:clock-start",
            value_fn=lambda s: _uptime_dt(host(s).get("uptime_seconds"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "storage_total", "存储总容量",
            icon="mdi:database",
            value_fn=lambda s: _human_bytes(storage(s).get("total"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "storage_free", "存储可用容量",
            icon="mdi:database-check",
            value_fn=lambda s: _human_bytes(storage(s).get("free"))),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "disk_count", "硬盘数量",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:harddisk",
            value_fn=lambda s: _num(
                storage(s).get("disk_count")
                or len(storage(s).get("disks") or []), 0),
            attrs_fn=lambda s: {
                "disks": [
                    {"name": d.get("name"), "model": d.get("modelName"),
                     "type": d.get("type"), "size": d.get("size")}
                    for d in storage(s).get("disks") or []],
                "pools": storage(s).get("pools") or [],
                "ssd_cache": storage(s).get("ssd_count"),
                "hdd": storage(s).get("hdd_count"),
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "disk_bad", "异常硬盘数量",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:harddisk-remove",
            value_fn=lambda s: _num(storage(s).get("disk_bad"), 0)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "notify", "未读通知",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:bell-badge",
            value_fn=lambda s: _num(s.get("notify_unread"), 0)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "dl_active", "下载中心活动任务",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:download",
            value_fn=lambda s: (
                _num(download(s).get("active_tasks"), 0)
                if download(s).get("installed") else None),
            attrs_fn=lambda s: {
                "installed": download(s).get("installed"),
                "speed_down": download(s).get("speed_down"),
                "speed_up": download(s).get("speed_up"),
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "docker_summary", "Docker 容器",
            icon="mdi:docker",
            value_fn=lambda s: _num(len((s.get("docker") or {}).get(
                "containers") or []), 0),
            attrs_fn=lambda s: {
                "installed": (s.get("docker") or {}).get("installed"),
                "containers": [
                    {"name": c.get("name"), "state": c.get("state"),
                     "image": c.get("image")}
                    for c in (s.get("docker") or {}).get("containers") or []
                ],
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "login_devices", "登录设备",
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:account-multiple",
            value_fn=lambda s: _num(len(s.get("login_devices") or []), 0),
            attrs_fn=lambda s: {"devices": s.get("login_devices") or []}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "net", "网卡",
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:ethernet",
            value_fn=lambda s: _num(len((s.get("net") or {}).get(
                "interfaces") or []), 0),
            attrs_fn=lambda s: {"interfaces": (s.get("net") or {}).get(
                "interfaces") or []}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "version", "系统版本",
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:tag",
            value_fn=lambda s: host(s).get("version"),
            attrs_fn=lambda s: {
                "edition": host(s).get("edition"),
                "platform": host(s).get("platform"),
                "machine_id": host(s).get("machine_id"),
                "hostname": host(s).get("hostname"),
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "update", "系统更新",
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:cloud-upload",
            value_fn=lambda s: (
                "有可用更新" if (s.get("update") or {}).get("available")
                else "已是最新"),
            attrs_fn=lambda s: {
                "new_version": (s.get("update") or {}).get("version"),
                "raw": (s.get("update") or {}).get("raw"),
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "vm_summary", "虚拟机",
            icon="mdi:monitor",
            value_fn=lambda s: (
                _num(len((s.get("vms") or {}).get("list") or []), 0)
                if (s.get("vms") or {}).get("installed")
                else "App 未安装"),
            attrs_fn=lambda s: {
                "installed": (s.get("vms") or {}).get("installed"),
                "vms": (s.get("vms") or {}).get("list") or [],
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "apps", "应用状态",
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:apps",
            value_fn=lambda s: _num(
                sum(1 for v in (s.get("apps") or {}).values() if v), 0),
            attrs_fn=lambda s: dict(s.get("apps") or {})),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "certs_host", "证书数量",
            entity_category=EntityCategory.DIAGNOSTIC,
            icon="mdi:certificate",
            value_fn=lambda s: _num(external(s).get("cert_count"), 0)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "uptime_h", "运行时长",
            state_class=SensorStateClass.TOTAL_INCREASING, icon="mdi:timer",
            unit="h",
            value_fn=lambda s: _num(
                (host(s).get("uptime_seconds") or 0) / 3600.0, 1),
            attrs_fn=lambda s: {"boot_time": _uptime_dt(
                host(s).get("uptime_seconds"))}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "storage_used", "存储已用容量",
            icon="mdi:database-arrow-right",
            value_fn=lambda s: _human_bytes(storage(s).get("used")),
            attrs_fn=lambda s: {"raw": storage(s).get("used")}),
        FnOsStoragePoolSensor(coordinator, entry),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "disk_types", "HDD/SSD/USB 数量",
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:harddisk",
            value_fn=lambda s: _num(
                (storage(s).get("hdd_count") or 0)
                + (storage(s).get("ssd_count") or 0)
                + (storage(s).get("usb_count") or 0), 0),
            attrs_fn=lambda s: {
                "hdd": storage(s).get("hdd_count"),
                "ssd": storage(s).get("ssd_count"),
                "usb": storage(s).get("usb_count"),
            }),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "dl_down", "下载中心下行速度",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:download-network",
            value_fn=lambda s: (_human_speed(download(s).get("speed_down"))
                                if download(s).get("installed") else None)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "dl_up", "下载中心上行速度",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:upload-network",
            value_fn=lambda s: (_human_speed(download(s).get("speed_up"))
                                if download(s).get("installed") else None)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "containers_running", "运行中容器",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:play-circle",
            value_fn=lambda s: _num(sum(
                1 for c in (s.get("docker") or {}).get("containers") or []
                if str(c.get("state") or "").lower() == "running"), 0)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "vms_running", "运行中虚拟机",
            state_class=SensorStateClass.MEASUREMENT, icon="mdi:monitor-play",
            value_fn=lambda s: (
                _num(sum(1 for v in (s.get("vms") or {}).get("list") or []
                         if str(v.get("state") or "").upper() == "RUNNING"), 0)
                if (s.get("vms") or {}).get("installed") else None)),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "ext_domains", "外部访问域名",
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:dns",
            value_fn=lambda s: _num(len(external(s).get("domains") or []), 0),
            attrs_fn=lambda s: {"domains": external(s).get("domains") or []}),
        FnOsSnapshotSensor(
            coordinator, entry, kind, "", "fnconnect", "FN Connect 远程连接",
            entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:earth",
            value_fn=lambda s: str(_first_key(
                external(s).get("fnconnect") or {},
                "status", "state", "enable", default="未知")),
            attrs_fn=lambda s: {"raw": external(s).get("fnconnect") or {}}),
        FnOsHardwareSensor(coordinator, entry),
        FnOsDiskHealthSensor(coordinator, entry),
    ]


def _uptime_dt(seconds):
    """开机时间点（UTC datetime 对象；TIMESTAMP device_class 只认 datetime）。"""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return None
    if not 0 < seconds < 10 ** 10:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _human_speed(value):
    """速率展示：_human_bytes 结果追加 /s 单位。"""
    result = _human_bytes(value)
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1] + "/s"
    return None


def _human_mb(value):
    """以 MB 为语义输入的容量展示。"""
    value = _num(value)
    if value is None:
        return None
    value *= 1024.0  # 转成 KB 语义复用 _human_bytes
    for unit, factor in (("TB", 1024 ** 4), ("GB", 1024 ** 3),
                         ("MB", 1024 ** 2)):
        if abs(value) >= factor:
            return round(value / factor, 2), unit
    return round(value, 2), "KB"


# --------------------------------------------------------------------------- #
# 文件 / 设置设备传感器
# --------------------------------------------------------------------------- #
def _file_sensors(coordinator, entry):
    kind = KIND_FILE

    def count_sensor(suffix, name, field):
        return FnOsSnapshotSensor(
            coordinator, entry, kind, "", suffix, name,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:file-multiple",
            value_fn=lambda s, f=field: _num((s.get("file") or {}).get(f), 0),
            attrs_fn=lambda s, f=field: {"raw": (s.get("file") or {}).get(f)})

    return [
        count_sensor("recent", "最近文件数", "recent_n"),
        count_sensor("fav", "收藏文件数", "fav_n"),
        count_sensor("trash", "回收站文件数", "trash_n"),
        count_sensor("share", "分享链接数", "share_n"),
    ]


# --------------------------------------------------------------------------- #
# Docker 容器 / 虚拟机传感器
# --------------------------------------------------------------------------- #
def _docker_sensors(coordinator, entry, only_name=None, known=None):
    kind = KIND_DOCKER
    sensors = []
    for row in (coordinator.data.get("docker") or {}).get("containers") or []:
        name = row.get("name")
        if not name or (only_name is not None and name != only_name):
            continue
        uid_base = f"{entry.entry_id}-{kind}-{name}"
        if known is not None and f"{uid_base}-state" in known:
            continue

        def get_row(s, n=name):
            return _container(s, n)

        sensors.extend([
            FnOsSnapshotSensor(
                coordinator, entry, kind, name, "state", "运行状态",
                icon="mdi:container-outline",
                value_fn=lambda s, f=get_row: (f(s).get("state") or "unknown"),
                attrs_fn=lambda s, f=get_row: {
                    "image": f(s).get("image"),
                    "id": f(s).get("id"),
                    "status": f(s).get("status"),
                    "ports": f(s).get("ports") or [],
                    "project": f(s).get("project"),
                    "command": f(s).get("command"),
                    "network_mode": f(s).get("network_mode"),
                    "created": f(s).get("created"),
                }),
            FnOsSnapshotSensor(
                coordinator, entry, kind, name, "cpu", "CPU 使用率",
                unit=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:cpu-32",
                value_fn=lambda s, f=get_row: _num(f(s).get("cpu_pct"))),
            FnOsSnapshotSensor(
                coordinator, entry, kind, name, "mem", "内存用量",
                state_class=SensorStateClass.MEASUREMENT, icon="mdi:memory",
                value_fn=lambda s, f=get_row: _human_mb(f(s).get("mem_mb"))),
            FnOsSnapshotSensor(
                coordinator, entry, kind, name, "net", "网络收发",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:transfer",
                value_fn=lambda s, f=get_row: _human_speed(
                    f(s).get("net_rx")),
                attrs_fn=lambda s, f=get_row: {
                    "rx": f(s).get("net_rx"),
                    "tx": f(s).get("net_tx"),
                }),
        ])
    return sensors


def _vm_sensors(hass, coordinator, entry, only_name=None, known=None):
    """每台虚拟机一组传感器；key 与设备注册一致，统一用 name。"""
    kind = KIND_VM
    sensors = []
    for vm in (coordinator.data.get("vms") or {}).get("list") or []:
        vm_name = str(vm.get("name") or "")
        if not vm_name or (only_name is not None and vm_name != only_name):
            continue
        vm_title = str(vm.get("title") or "")
        uid_base = f"{entry.entry_id}-{kind}-{vm_name}"
        if known is not None and f"{uid_base}-state" in known:
            continue

        def get_vm(s, n=vm_name):
            for row in (s.get("vms") or {}).get("list") or []:
                if str(row.get("name")) == n:
                    return row
            return {}

        # 配置类传感器统一走 domain/info 详情（60 秒共享缓存）；
        # unique_id 沿用旧 key（cpu_count/memory），平滑替换旧实体
        def vm_info_sensor(key, name, icon, parse_fn, k=vm_name,
                           t=vm_title):
            return FnOsVmInfoSensor(
                hass, coordinator, entry, kind, k, key, name, icon, parse_fn,
                title=t)

        sensors.extend([
            FnOsSnapshotSensor(
                coordinator, entry, kind, vm_name, "state", "电源状态",
                device_title=vm_title, icon="mdi:monitor",
                value_fn=lambda s, f=get_vm: (f(s).get("state") or "unknown"),
                attrs_fn=lambda s, f=get_vm: {
                    "name": f(s).get("name"),
                    "title": f(s).get("title"),
                    "cpu": f(s).get("cpu"),
                    "memory": f(s).get("memory"),
                    "os": f(s).get("os"),
                    "ip": f(s).get("ip"),
                }),
            FnOsSnapshotSensor(
                coordinator, entry, kind, vm_name, "ip", "IP 地址",
                device_title=vm_title,
                entity_category=EntityCategory.DIAGNOSTIC,
                icon="mdi:ip-network",
                value_fn=lambda s, f=get_vm: (f(s).get("ip") or "未获取")),
            vm_info_sensor(
                "cpu_count", "vCPU 数量", "mdi:cpu-64-bit",
                lambda i: (_num(i.get("cpuCoreCnt"), 0), None, None)),
            vm_info_sensor(
                "memory", "内存容量", "mdi:memory", _vm_mem_parse),
            vm_info_sensor(
                "chipset", "芯片组", "mdi:chip",
                lambda i: (i.get("chipset") or None, None, None)),
            vm_info_sensor(
                "firmware", "固件", "mdi:application-cog",
                lambda i: (str(i.get("firmware") or "").upper() or None,
                           None, None)),
            vm_info_sensor(
                "video", "显卡", "mdi:monitor-shimmer",
                lambda i: (i.get("videoType") or None, None, None)),
            vm_info_sensor(
                "disk", "磁盘容量", "mdi:harddisk", _vm_disk_parse),
            vm_info_sensor(
                "mac", "MAC 地址", "mdi:network-outline", _vm_mac_parse),
            FnOsSnapshotSensor(
                coordinator, entry, kind, vm_name, "os", "操作系统",
                device_title=vm_title,
                entity_category=EntityCategory.DIAGNOSTIC, icon="mdi:microsoft",
                value_fn=lambda s, f=get_vm: (f(s).get("os") or "未知")),
        ])
    return sensors
