"""离线自检：删除容器/虚拟机后的实体与设备清理。

运行 ``python tests/check_purge.py``（无需安装 Home Assistant）。
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
mod("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
mod("homeassistant.exceptions",
    ConfigEntryNotReady=type("ConfigEntryNotReady", (Exception,), {}))
mod("homeassistant.const")
mod("homeassistant.helpers")
mod("homeassistant.helpers.update_coordinator",
    CoordinatorEntity=type("CoordinatorEntity", (), {}),
    DataUpdateCoordinator=type("DataUpdateCoordinator", (), {}),
    UpdateFailed=type("UpdateFailed", (Exception,), {}))


class _Ent:
    def __init__(self, entity_id, unique_id, device_id=None):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.device_id = device_id
        self.platform = "fnos"


class _Dev:
    def __init__(self, dev_id, identifiers, name):
        self.id = dev_id
        self.identifiers = identifiers
        self.name = name
        self.name_by_user = None


class _EntReg:
    def __init__(self):
        self.entries = {}

    @property
    def entities(self):
        return self.entries

    def async_get_entity_id(self, domain, platform, unique_id):
        for ent in self.entries.values():
            if ent.unique_id == unique_id:
                return ent.entity_id
        return None

    def async_remove(self, entity_id):
        self.entries.pop(entity_id, None)

    async def async_get(self, entity_id):
        return self.entries.get(entity_id)


class _DevReg:
    def __init__(self):
        self.devices = {}

    def async_get(self, dev_id):
        return self.devices.get(dev_id)

    def async_remove_device(self, dev_id):
        self.devices.pop(dev_id, None)


ENT = _EntReg()
DEV = _DevReg()

mod("homeassistant.helpers.entity_registry",
    async_get=lambda hass: ENT,
    async_entries_for_config_entry=lambda reg, entry_id: list(reg.entities.values()),
    async_entries_for_device=lambda reg, dev_id: [
        e for e in reg.entities.values() if e.device_id == dev_id],
    EVENT_ENTITY_REGISTRY_UPDATED="entity_registry_updated")
mod("homeassistant.helpers.device_registry",
    async_get=lambda hass: DEV,
    async_entries_for_config_entry=lambda reg, entry_id: list(reg.devices.values()),
    async_get_or_create=lambda **kw: None,
    EVENT_DEVICE_REGISTRY_UPDATED="device_registry_updated")

# ---------------- fnos 包桩 ----------------
pkg = mod("fnos")
pkg.__path__ = [str(ROOT)]

mod("fnos.api", FnOsApiError=type("FnOsApiError", (Exception,), {}),
    FnOsClient=type("FnOsClient", (), {}))
mod("fnos.web_proxy", async_setup_proxy=None, close_page_client=None)
mod("fnos.origin", async_setup_origin=None, async_load_learned_bases=None,
    schedule_initial_link=None)


def _load(name, filename, into=None):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = into or importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


_load("fnos.const", "const.py")
_load("fnos.naming", "naming.py")
init = _load("fnos", "__init__.py", into=sys.modules["fnos"])


class _Hass:
    def __init__(self):
        self.data = {}
        self.loop = None


class _Entry:
    entry_id = "E1"
    data = {}
    options = {}


def main() -> None:
    hass = _Hass()
    entry = _Entry()
    snap = {
        "docker": {"containers": [{"name": "nginx"}]},
        "vms": {"installed": True,
                "list": [{"name": "vm1", "title": "Windows 7"}]},
    }

    assert init.live_dynamic_keys(snap) == {("docker", "nginx"), ("vm", "vm1")}

    DEV.devices["D_VM1"] = _Dev("D_VM1", {("fnos", "E1:vm:vm1")}, "虚拟机 · VM1")
    DEV.devices["D_NGINX"] = _Dev(
        "D_NGINX", {("fnos", "E1:docker:nginx")}, "Docker · nginx")
    for uid, eid in (
        ("E1-vm-vm1-state", "sensor.fnos_vm_windows_7_state"),
        ("E1-vm-vm1-ip", "sensor.fnos_vm_windows_7_ip"),
        ("E1-vm-vm1-power", "switch.fnos_vm_windows_7_power"),
        ("E1-vm-vm1-reboot", "button.fnos_vm_windows_7_reboot"),
        ("E1-vm-vm1-forceshutdown", "button.fnos_vm_windows_7_force_shutdown"),
        ("E1-remote-E1:vm:vm1", "sensor.fnos_vm_windows_7_remote"),
        ("E1-console-E1:vm:vm1", "sensor.fnos_vm_windows_7_console"),
    ):
        ENT.entries[eid] = _Ent(eid, uid, "D_VM1")
    ENT.entries["sensor.fnos_docker_nginx_state"] = _Ent(
        "sensor.fnos_docker_nginx_state", "E1-docker-nginx-state", "D_NGINX")

    owned = {
        "E1-vm-vm1-state": ("vm", "vm1"),
        "E1-vm-vm1-ip": ("vm", "vm1"),
        "E1-remote-E1:vm:vm1": ("vm", "vm1"),
        "E1-docker-nginx-state": ("docker", "nginx"),
    }
    known = set(owned)
    removed = init.purge_dynamic_entities(
        hass, entry, "sensor", {("vm", "vm1")}, owned, known)
    assert removed == 3, removed
    assert "E1-docker-nginx-state" in known, "不该动还活着的容器实体"
    assert "sensor.fnos_docker_nginx_state" in ENT.entries
    assert "sensor.fnos_vm_windows_7_state" not in ENT.entries
    assert "D_VM1" in DEV.devices, "还有 switch/button 实体，设备先别删"

    assert init.forget_dynamic_device(hass, entry, "vm", "vm1") is False
    for eid in ("switch.fnos_vm_windows_7_power",
                "button.fnos_vm_windows_7_reboot",
                "button.fnos_vm_windows_7_force_shutdown",
                "sensor.fnos_vm_windows_7_console"):
        ENT.async_remove(eid)
    assert init.forget_dynamic_device(hass, entry, "vm", "vm1") is True
    assert "D_VM1" not in DEV.devices

    # 启动兜底：HA 停机期间删除的虚拟机
    DEV.devices["D_VM2"] = _Dev("D_VM2", {("fnos", "E1:vm:vm2")}, "虚拟机 · VM2")
    ENT.entries["sensor.fnos_vm_vm2_state"] = _Ent(
        "sensor.fnos_vm_vm2_state", "E1-vm-vm2-state", "D_VM2")
    assert init._purge_stale_dynamic_entities(hass, entry, snap) == 1
    assert "sensor.fnos_vm_vm2_state" not in ENT.entries
    assert "sensor.fnos_docker_nginx_state" in ENT.entries
    print("check_purge: OK")


if __name__ == "__main__":
    main()
