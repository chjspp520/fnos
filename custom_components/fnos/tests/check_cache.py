"""离线自检：api.py 的分层缓存（TTL / 失败沿用旧值 / invalidate）。

本地无需安装 Home Assistant / websocket-client / pycryptodome：
运行 ``python tests/check_cache.py`` 即可。
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


# 第三方依赖桩
mod("websocket", create_connection=lambda *a, **k: None,
    WebSocketException=type("WebSocketException", (Exception,), {}),
    WebSocketTimeoutException=type(
        "WebSocketTimeoutException", (Exception,), {}))
mod("Crypto")
mod("Crypto.Cipher", AES=object(), PKCS1_v1_5=object())
mod("Crypto.PublicKey", RSA=object())
mod("Crypto.Util")
mod("Crypto.Util.Padding", pad=lambda *a, **k: b"", unpad=lambda *a, **k: b"")

spec = importlib.util.spec_from_file_location("fnos_api", ROOT / "api.py")
api = importlib.util.module_from_spec(spec)
sys.modules["fnos_api"] = api
spec.loader.exec_module(api)


def main() -> None:
    client = api.FnOsClient("192.0.2.10", 5666, "user", "pass")
    calls = {}

    def fetcher(name, value):
        def inner():
            calls[name] = calls.get(name, 0) + 1
            return value
        return inner

    client._fetch_host = fetcher("host", {"hostname": "nas"})
    client._fetch_resmon = fetcher("resmon", {"cpu_pct": 7})
    client._fetch_storage = fetcher("storage", {"total": 100})
    client._fetch_net = fetcher("net", {"interfaces": []})
    client._fetch_services = fetcher("services", {"ssh": True})
    client._fetch_update = fetcher("update", {"available": False})
    client._fetch_notify = fetcher("notify", 0)
    client._fetch_login_devices = fetcher("login", [])
    client._fetch_docker = fetcher("docker", {"installed": True,
                                              "containers": []})
    client._fetch_download = fetcher("download", {"installed": True})
    client._fetch_file = fetcher("file", {"recent_n": 1})
    client._fetch_external = fetcher("external", {"cert_count": 0})
    client._fetch_vms = fetcher("vms", {"installed": True, "list": []})
    client._probe_apps = fetcher("apps", {"photo": True})
    client.installed_apps = fetcher("apps_installed", [])

    snap = client.fetch_all()
    assert {"host", "resmon", "storage", "services", "docker", "vms"} <= set(snap)
    first = dict(calls)

    snap2 = client.fetch_all()
    again = {key: calls[key] - first.get(key, 0) for key in calls}
    for fast in ("host", "resmon", "docker", "vms"):
        assert again.get(fast) == 1, f"{fast} 应每轮都取：{again}"
    for slow in ("storage", "net", "services", "update", "login", "file",
                 "external", "apps", "apps_installed", "notify"):
        assert again.get(slow, 0) == 0, f"{slow} 在 TTL 内不该再请求：{again}"
    assert snap2["notify_unread"] == 0, "0 条未读不能被当成取数失败"

    def boom():
        calls["storage"] = calls.get("storage", 0) + 1
        raise RuntimeError("nas busy")

    client._fetch_storage = boom
    snap3 = client.fetch_all()
    assert snap3["storage"] == {"total": 100}, snap3["storage"]

    client._fetch_storage = fetcher("storage", {"total": 200})
    client.invalidate("storage")
    calls["storage"] = 0
    snap4 = client.fetch_all()
    assert calls["storage"] == 1, calls
    assert snap4["storage"] == {"total": 200}, snap4["storage"]

    client._fetch_services = fetcher("services", {"ssh": True})
    client.invalidate("services")
    assert client.fetch_all()["services"] == {"ssh": True}
    client.BLOCK_TTL["services"] = 0
    client._fetch_services = fetcher("services", {})
    assert client.fetch_all()["services"] == {"ssh": True}, "空 dict 应回退旧值"
    print("check_cache: OK")


if __name__ == "__main__":
    main()
