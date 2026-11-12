"""离线自检：新增容器/虚拟机时实体补建（sensor.py 的 _dynamic_sensors）。

运行 ``python tests/check_dynamic.py``（无需安装 Home Assistant）。
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def mod(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def callback(func):
    return func


# ---------------- homeassistant 桩 ----------------
mod("homeassistant")
mod("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}),
    callback=callback)
mod("homeassistant.const", EVENT_STATE_CHANGED="state_changed", PERCENTAGE="%")
mod("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))


class _SensorEntity:
    pass


class _DeviceClass:
    TIMESTAMP = "timestamp"


class _StateClass:
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class _Category:
    DIAGNOSTIC = "diagnostic"


mod("homeassistant.components")
mod("homeassistant.components.sensor",
    SensorDeviceClass=_DeviceClass, SensorEntity=_SensorEntity,
    SensorStateClass=_StateClass)
mod("homeassistant.helpers")
mod("homeassistant.helpers.entity", EntityCategory=_Category)
mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
mod("homeassistant.helpers.event",
    async_call_later=lambda *a, **k: (lambda: None),
    async_track_time_interval=lambda *a, **k: (lambda: None))
mod("homeassistant.helpers.dispatcher",
    async_dispatcher_connect=lambda *a, **k: (lambda: None),
    async_dispatcher_send=lambda *a, **k: None)
mod("homeassistant.helpers.network", get_url=lambda *a, **k: "")


class _Reg:
    def __init__(self):
        self.entities = {}

    def async_get(self, *a, **k):
        return None

    def async_get_device(self, *a, **k):
        return None


mod("homeassistant.helpers.device_registry", async_get=lambda hass: _Reg(),
    EVENT_DEVICE_REGISTRY_UPDATED="device_registry_updated")
mod("homeassistant.helpers.entity_registry", async_get=lambda hass: _Reg(),
    EVENT_ENTITY_REGISTRY_UPDATED="entity_registry_updated")

# ---------------- fnos 包桩 ----------------
pkg = mod("fnos")
pkg.__path__ = [str(ROOT)]


def device_identifier(entry, kind, key=""):
    return f"{entry.entry_id}:{kind}:{key}" if key else f"{entry.entry_id}:{kind}"


def live_dynamic_keys(snap):
    keys = set()
    for row in (snap.get("docker") or {}).get("containers") or []:
        if row.get("name"):
            keys.add(("docker", str(row["name"])))
    for vm in (snap.get("vms") or {}).get("list") or []:
        name = str(vm.get("name") or "")
        if name:
            keys.add(("vm", name))
    return keys


def purge_dynamic_entities(hass, entry, domain, keys, owned, known):
    return 0


class FnOsEntity:
    def __init__(self, coordinator, entry, kind, key="", suffix="",
                 *, id_suffix=None, device_title=""):
        self.coordinator = coordinator
        self.entry = entry
        self.kind = kind
        self.key = key
        self._attr_suffix = suffix
        self._attr_unique_id = "-".join(
            x for x in (entry.entry_id, kind, key, suffix) if x)
        self._setup_forced_entity_id(
            getattr(self, "_forced_domain", ""),
            entry_token(coordinator.hass, entry),
            device_token(kind, key, device_title),
            suffix if id_suffix is None else id_suffix,
        )

    def _setup_forced_entity_id(self, platform_domain, *parts):
        return naming.ForcedEntityIdMixin._setup_forced_entity_id(
            self, platform_domain, *parts)

    @property
    def snap(self):
        return self.coordinator.data or {}


pkg.FnOsEntity = FnOsEntity
pkg.device_identifier = device_identifier
pkg.live_dynamic_keys = live_dynamic_keys
pkg.purge_dynamic_entities = purge_dynamic_entities

mod("fnos.api", DISK_WARN_STATES=set(),
    _first_key=lambda d, *k, default=None: default,
    disk_health_states=lambda d: [], disk_is_bad=lambda s: False)
mod("fnos.web_proxy", remote_control_enabled=lambda hass: True,
    REMOTE_CONTROL_SIGNAL="fnos_remote_control_changed",
    PHOTO_REFRESH_SIGNAL="fnos_photo_refresh")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("fnos.const", "const.py")
naming = _load("fnos.naming", "naming.py")
entry_token = naming.entry_token
device_token = naming.device_token
sensor = _load("fnos.sensor", "sensor.py")


class _ConfigEntries:
    def __init__(self, entry):
        self._entry = entry

    def async_entries(self, domain):
        return [self._entry]


class _Hass:
    def __init__(self, entry):
        self.config_entries = _ConfigEntries(entry)
        self.data = {}
        self.states = {}
        self.loop = None
        self.bus = types.SimpleNamespace(async_listen=lambda *a, **k: (lambda: None))


class _Entry:
    entry_id = "E1"
    data = {}
    options = {}


class _Coordinator:
    def __init__(self, hass):
        self.hass = hass
        self.data = {"docker": {"containers": []},
                     "vms": {"installed": True, "list": []}}
        self.last_update_success = True


def main() -> None:
    entry = _Entry()
    hass = _Hass(entry)
    coordinator = _Coordinator(hass)

    assert sensor._dynamic_sensors(
        hass, coordinator, entry, coordinator.data) == []

    coordinator.data = {
        "docker": {"containers": [{"name": "nginx", "state": "running"}]},
        "vms": {"installed": True,
                "list": [{"name": "vm1", "title": "Windows 7"}]},
    }
    new = sensor._dynamic_sensors(hass, coordinator, entry, coordinator.data)
    uids = [e._attr_unique_id for e in new]
    assert any(u.endswith("-vm-vm1-state") for u in uids), "缺虚拟机电源状态"
    assert any("remote-E1:vm:vm1" in u for u in uids), "缺虚拟机远程地址"
    assert any("console-E1:vm:vm1" in u for u in uids), "缺虚拟机控制台地址"
    assert any(u.endswith("-docker-nginx-state") for u in uids), "缺容器状态"
    assert any("remote-E1:docker:nginx" in u for u in uids), "缺容器远程地址"
    assert len(uids) == len(set(uids)), "有重复 unique_id"

    again = sensor._dynamic_sensors(hass, coordinator, entry, coordinator.data,
                                    set(uids))
    assert again == [], [e._attr_unique_id for e in again]

    coordinator.data["vms"]["list"].append({"name": "vm2", "title": "Ubuntu"})
    new2 = sensor._dynamic_sensors(hass, coordinator, entry, coordinator.data,
                                   set(uids))
    uids2 = [e._attr_unique_id for e in new2]
    assert all("vm2" in u for u in uids2 if "-vm-" in u), uids2
    assert not any("nginx" in u or "-vm-vm1-" in u for u in uids2), uids2
    # 单位固定（避免 HA 报「单位已改变」）
    assert sensor.SPEED_UNIT == "KB/s"
    assert sensor.DISK_SPEED_UNIT == "MB/s"
    assert sensor.CAPACITY_UNIT == "GB"
    assert sensor._gb(1073741824) == 1.0
    print("check_dynamic: OK")


if __name__ == "__main__":
    main()
