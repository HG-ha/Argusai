"""macOS system-proxy helpers (mocked; no live networksetup)."""

from __future__ import annotations

from argusai.core.security import system_proxy as sp


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_SERVICES = (
    "An asterisk (*) denotes that a network service is disabled.\n"
    "Wi-Fi\n"
    "Thunderbolt Bridge\n"
    "iPhone USB\n"
)
_HARDWARE = (
    "Hardware Port: Wi-Fi\n"
    "Device: en0\n"
    "Ethernet Address: aa:bb:cc:dd:ee:ff\n"
    "\n"
    "Hardware Port: Thunderbolt Bridge\n"
    "Device: bridge0\n"
    "Ethernet Address: 11:22:33:44:55:66\n"
)
_ROUTE = "    interface: en0\n"
_PROXY_OFF = "Enabled: No\nServer:\nPort: 0\n"
_PROXY_ON = "Enabled: Yes\nServer: 127.0.0.1\nPort: 8866\n"


def _fake_macos_network(monkeypatch, *, proxy_stdout: str = _PROXY_OFF, calls: list | None = None):
    recorded = calls if calls is not None else []

    def fake_run(cmd, **kwargs):
        recorded.append(list(cmd))
        if cmd[:4] == ["route", "-n", "get", "default"]:
            return _Proc(stdout=_ROUTE)
        if cmd[:2] == ["networksetup", "-listallnetworkservices"]:
            return _Proc(stdout=_SERVICES)
        if cmd[:2] == ["networksetup", "-listallhardwareports"]:
            return _Proc(stdout=_HARDWARE)
        if cmd[1] in {"-getwebproxy", "-getsecurewebproxy"}:
            return _Proc(stdout=proxy_stdout)
        return _Proc(0)

    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    return recorded


def test_macos_target_services_uses_default_route_only(monkeypatch) -> None:
    _fake_macos_network(monkeypatch)
    assert sp._macos_target_services() == ["Wi-Fi"]


def test_loopback_host_rewrites_wildcard() -> None:
    assert sp._loopback_host("0.0.0.0") == "127.0.0.1"
    assert sp._loopback_host("::") == "127.0.0.1"
    assert sp._loopback_host("127.0.0.1") == "127.0.0.1"


def test_enable_macos_proxy_skips_when_already_set(monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_macos_network(monkeypatch, proxy_stdout=_PROXY_ON, calls=calls)
    snapshot = sp.SystemProxySnapshot(platform="darwin")
    result = sp._enable_macos_proxy("127.0.0.1", 8866, snapshot)
    assert result["ok"] is True
    assert result["services"] == ["Wi-Fi"]
    assert not any(c[1].startswith("-set") for c in calls if c and c[0] == "networksetup")


def test_enable_macos_proxy_does_not_touch_unused_nics(monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_macos_network(monkeypatch, proxy_stdout=_PROXY_OFF, calls=calls)
    snapshot = sp.SystemProxySnapshot(platform="darwin")
    result = sp._enable_macos_proxy("127.0.0.1", 8866, snapshot)
    assert result["ok"] is True
    set_cmds = [c for c in calls if c[:2] == ["networksetup", "-setwebproxy"] or c[:2] == ["networksetup", "-setsecurewebproxy"]]
    assert set_cmds
    assert all(c[2] == "Wi-Fi" for c in set_cmds)
    assert not any("Thunderbolt" in c[2] or "iPhone" in c[2] for c in set_cmds if len(c) > 2)
    assert snapshot.macos_restore and snapshot.macos_restore[0]["service"] == "Wi-Fi"


def test_restore_macos_proxy_uses_snapshot_rows(monkeypatch) -> None:
    calls: list[list[str]] = []
    _fake_macos_network(monkeypatch, proxy_stdout=_PROXY_ON, calls=calls)
    snapshot = sp.SystemProxySnapshot(
        platform="darwin",
        macos_restore=[{
            "service": "Wi-Fi",
            "http_enabled": False,
            "http_server": "",
            "http_port": 0,
            "https_enabled": False,
            "https_server": "",
            "https_port": 0,
        }],
    )
    result = sp._restore_macos_proxy(snapshot)
    assert result["ok"] is True
    state_cmds = [
        c for c in calls
        if tuple(c[:2]) in {("networksetup", "-setwebproxystate"), ("networksetup", "-setsecurewebproxystate")}
    ]
    assert state_cmds
    assert all(c[2] == "Wi-Fi" for c in state_cmds)
