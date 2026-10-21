"""飞牛 fnOS 集成入口。"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import FnOsApiError, FnOsClient
from .const import (
    CONF_UPDATE_INTERVAL,
    DATA_KEY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    KIND_DOCKER,
    KIND_FILE,
    KIND_HOST,
    KIND_PHOTO,
    KIND_VM,
    KIND_VIDEO,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    PLATFORMS,
)
from . import origin as origin_mod
from . import web_proxy
from .naming import ForcedEntityIdMixin, device_token, entry_token

_LOGGER = logging.getLogger(__name__)


def entry_slug(entry: ConfigEntry) -> str:
    return entry.entry_id


def device_identifier(entry: ConfigEntry, kind: str, key: str = "") -> str:
    """设备 identifier：``<entry_id>:<kind>[:<key>]``（与 origin.py 约定一致）。"""
    return f"{entry.entry_id}:{kind}:{key}" if key else f"{entry.entry_id}:{kind}"


def _interval_seconds(options: dict) -> int:
    try:
        value = int(options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_UPDATE_INTERVAL
    return max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, value))


class FnOsCoordinator(DataUpdateCoordinator):
    """中央数据协调器：executor 里跑同步 WS 客户端拉全量快照。"""

    def __init__(self, hass, entry, client: FnOsClient):
        self.entry = entry
        self.client = client
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(
                seconds=_interval_seconds(entry.options)),
        )

    async def _async_update_data(self):
        try:
            return await self.hass.async_add_executor_job(self.client.fetch_all)
        except FnOsApiError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:  # pylint: disable=broad-except
            raise UpdateFailed(f"fnOS 数据拉取失败：{err}") from err


def snapshot_devices(hass, entry, snap):
    """按快照增补设备注册表（幂等 upsert；容器/虚拟机增删自动跟随）。"""
    dev_reg = dr.async_get(hass)
    hostname = (snap.get("host") or {}).get("hostname") or entry.data.get("host")
    version = (snap.get("host") or {}).get("version") or ""

    def upsert(identifier, name, model):
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, identifier)},
            name=name,
            manufacturer="飞牛",
            model=model or "fnOS",
        )

    upsert(device_identifier(entry, KIND_HOST),
           f"飞牛主机 · {hostname}", f"fnOS {version}".strip())
    upsert(device_identifier(entry, KIND_FILE), "飞牛文件", "文件管理")
    upsert(device_identifier(entry, KIND_PHOTO), "飞牛相册", "相册")
    upsert(device_identifier(entry, KIND_VIDEO), "飞牛影视", "影视")

    live_dynamic = set()
    for container in (snap.get("docker") or {}).get("containers") or []:
        name = container.get("name")
        if name:
            ident = device_identifier(entry, KIND_DOCKER, name)
            live_dynamic.add(ident)
            upsert(ident, f"Docker · {name}",
                   container.get("image") or "Docker 容器")
    for vm in (snap.get("vms") or {}).get("list") or []:
        # key 用 name：domain 系列接口（/domain/start 等）与 noVNC 页
        # /vm/vnc/<name> 均以 name 为唯一标识（uuid 拿不到）
        vm_name = str(vm.get("name") or "")
        if vm_name:
            ident = device_identifier(entry, KIND_VM, vm_name)
            live_dynamic.add(ident)
            upsert(ident, f"虚拟机 · {vm.get('title') or vm_name}", "fnOS 虚拟机")

    _prune_devices(hass, entry, live_dynamic)


def _prune_devices(hass, entry, live_dynamic):
    """清掉历史遗留/幽灵设备：旧「设置」设备、实体挂错产生的无名设备、
    已删除容器/VM 的残留设备。每轮刷新调用（幂等，无变化零开销）。"""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    valid_static = {
        device_identifier(entry, kind)
        for kind in (KIND_HOST, KIND_FILE, KIND_PHOTO, KIND_VIDEO)
    }
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        identifier = next(
            (ident[1] for ident in device.identifiers if ident[0] == DOMAIN),
            None)
        if identifier is None:
            continue
        if identifier in valid_static or identifier in live_dynamic:
            continue
        # 还有实体挂在该设备上就不删（等下一轮实体迁移完后再清）
        if not er.async_entries_for_device(ent_reg, device.id):
            _LOGGER.info("fnOS 清理无效设备：%s", identifier)
            dev_reg.async_remove_device(device.id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """加载配置条目。"""
    hass.data.setdefault(DOMAIN, {})
    client = FnOsClient(
        host=entry.data["host"],
        port=entry.data.get("port", 5666),
        username=entry.data["username"],
        password=entry.data["password"],
        ssl=entry.data.get("ssl", False),
    )
    coordinator = FnOsCoordinator(hass, entry, client)
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        await hass.async_add_executor_job(client.close)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise ConfigEntryNotReady(str(err)) from err

    # 全局视图 / 悬浮窗模块 / 地址学习（幂等，只注册一次）
    await web_proxy.async_setup_proxy(hass)
    await origin_mod.async_setup_origin(hass)
    await origin_mod.async_load_learned_bases(hass)

    # 按首轮快照补齐设备，之后每轮刷新跟随容器/虚拟机增删
    snapshot_devices(hass, entry, coordinator.data)

    def _on_refreshed():
        snapshot_devices(hass, entry, coordinator.data)

    entry.async_on_unload(coordinator.async_add_listener(_on_refreshed))

    # 启动重签设备「访问」链接（学习地址已先行加载）
    origin_mod.schedule_initial_link(hass, entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """卸载配置条目。"""
    unload_ok = await hass.config_entries.async_forward_entry_unload(
        entry, PLATFORMS)
    if unload_ok:
        info = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if info:
            client = info.get("client")
            if client is not None:
                await hass.async_add_executor_job(client.close)
        # 悬浮窗专用客户端同步关闭
        await hass.async_add_executor_job(
            web_proxy.close_page_client, hass, entry.entry_id)
    return unload_ok


class FnOsEntity(ForcedEntityIdMixin, CoordinatorEntity):
    """fnOS 实体基类：挂接对应设备 + 读协调器快照。

    实体 ID 统一为 ``<domain>.fnos_[<条目>_]<设备>_<用途>``（短英文，见
    :mod:`naming`）；显示名仍是中文。``id_suffix`` 可单独指定 ID 里的用途段
    （默认用 ``suffix``），``device_title`` 用于虚拟机（标题比内部 name 友好）。
    """

    _attr_has_entity_name = True
    #: 子类填自己的平台域（sensor / switch / button / binary_sensor）
    _forced_domain = ""

    def __init__(self, coordinator, entry, kind, key="", suffix="",
                 *, id_suffix=None, device_title=""):
        super().__init__(coordinator)
        self.entry = entry
        self.kind = kind
        self.key = key
        self._attr_suffix = suffix
        self._attr_unique_id = "-".join(
            x for x in (entry.entry_id, kind, key, suffix) if x)
        self._setup_forced_entity_id(
            self._forced_domain,
            entry_token(coordinator.hass, entry),
            device_token(kind, key, device_title),
            suffix if id_suffix is None else id_suffix,
        )

    @property
    def identifier(self):
        return device_identifier(self.entry, self.kind, self.key)

    @property
    def device_info(self):
        return {"identifiers": {(DOMAIN, self.identifier)}}

    @property
    def snap(self):
        return self.coordinator.data or {}

    @property
    def available(self):
        return self.coordinator.last_update_success
