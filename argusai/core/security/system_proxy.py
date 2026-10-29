"""Optional OS-level HTTP/HTTPS proxy configuration.

Snapshots the previous user settings and restores them when the passive proxy
stops or the app exits. This is opt-in — ArgusAI does not silently hijack
system traffic.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("argusai.proxy.system")


@dataclass
class SystemProxySnapshot:
    enabled: bool = False
    server: str = ""
    auto_config_url: str = ""
    proxy_override: str = ""
    platform: str = ""
    # macOS: per-service HTTP/HTTPS state so restore does not touch unused NICs
    # and does not pop an admin-password dialog for every network service.
    macos_restore: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "enabled": self.enabled,
            "server": self.server,
            "auto_config_url": self.auto_config_url,
            "proxy_override": self.proxy_override,
            "platform": self.platform,
        }
        if self.macos_restore:
            payload["macos_restore"] = list(self.macos_restore)
        return payload


def read_system_proxy() -> SystemProxySnapshot:
    sys_name = platform.system().lower()
    if sys_name.startswith("win"):
        return _read_windows_proxy()
    if sys_name == "darwin":
        return _read_macos_proxy()
    return SystemProxySnapshot(platform=sys_name)


def enable_system_proxy(host: str, port: int) -> tuple[SystemProxySnapshot, dict[str, Any]]:
    snapshot = read_system_proxy()
    host = _loopback_host(host)
    server = f"{host}:{int(port)}"
    sys_name = platform.system().lower()
    if sys_name.startswith("win"):
        result = _set_windows_proxy(True, server, proxy_override=_WINDOWS_PROXY_BYPASS)
    elif sys_name == "darwin":
        result = _enable_macos_proxy(host, int(port), snapshot)
    else:
        return snapshot, {"ok": False, "error": "System proxy control is not supported on this OS"}
    if not result.get("ok"):
        return snapshot, result
    return snapshot, {"ok": True, "server": server, **{k: v for k, v in result.items() if k != "ok"}}


def restore_system_proxy(snapshot: SystemProxySnapshot | None) -> dict[str, Any]:
    if snapshot is None:
        return {"ok": True, "restored": False}
    sys_name = platform.system().lower()
    if sys_name.startswith("win"):
        return _set_windows_proxy(
            snapshot.enabled,
            snapshot.server,
            proxy_override=snapshot.proxy_override,
        )
    if sys_name == "darwin":
        return _restore_macos_proxy(snapshot)
    return {"ok": True, "restored": False}


def _split_host(server: str) -> tuple[str, int]:
    text = str(server or "").strip()
    if not text:
        return "127.0.0.1", 8866
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        try:
            return host or "127.0.0.1", int(port_s)
        except ValueError:
            pass
    return text, 8866


def _loopback_host(host: str) -> str:
    text = str(host or "").strip() or "127.0.0.1"
    if text in {"0.0.0.0", "::", "::0", "*"}:
        return "127.0.0.1"
    return text


_WINDOWS_PROXY_BYPASS = "<-loopback>;localhost;127.0.0.1;*.local"


def _read_windows_proxy() -> SystemProxySnapshot:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except OSError:
                server = ""
            try:
                auto = str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "")
            except OSError:
                auto = ""
            try:
                override = str(winreg.QueryValueEx(key, "ProxyOverride")[0] or "")
            except OSError:
                override = ""
        return SystemProxySnapshot(
            enabled=enabled,
            server=server,
            auto_config_url=auto,
            proxy_override=override,
            platform="windows",
        )
    except Exception as exc:
        log.debug("read windows proxy failed: %s", exc)
        return SystemProxySnapshot(platform="windows")


def _set_windows_proxy(
    enabled: bool,
    server: str,
    *,
    proxy_override: str | None = None,
) -> dict[str, Any]:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enabled else 0)
            if enabled and server:
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, server)
                winreg.SetValueEx(
                    key,
                    "ProxyOverride",
                    0,
                    winreg.REG_SZ,
                    proxy_override if proxy_override is not None else _WINDOWS_PROXY_BYPASS,
                )
            elif proxy_override is not None:
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, proxy_override)
        # Notify WinINET of the change
        try:
            import ctypes

            internet_set_option = ctypes.windll.wininet.InternetSetOptionW
            internet_set_option(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
            internet_set_option(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
        except Exception:
            pass
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _list_macos_services() -> list[str]:
    """Return active macOS network services (excludes disabled / placeholder lines)."""
    try:
        proc = subprocess.run(
            ["networksetup", "-listallnetworkservices"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        log.debug("list macos services failed: %s", exc)
        return []
    services: list[str] = []
    for line in (proc.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        # The first line is a header; disabled services are prefixed with "*".
        if text.startswith("An asterisk"):
            continue
        if text.startswith("*"):
            continue
        services.append(text)
    return services


def _run_networksetup(args: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["networksetup", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_macos_proxy_output(text: str) -> dict[str, Any]:
    enabled = False
    server = ""
    port = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Enabled:"):
            enabled = "yes" in stripped.split(":", 1)[-1].strip().lower()
        elif stripped.startswith("Server:"):
            server = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Port:"):
            try:
                port = int(stripped.split(":", 1)[1].strip())
            except ValueError:
                port = 0
    return {"enabled": enabled, "server": server, "port": port}


def _read_macos_service_proxy(service: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "service": service,
        "http_enabled": False,
        "http_server": "",
        "http_port": 0,
        "https_enabled": False,
        "https_server": "",
        "https_port": 0,
    }
    try:
        http = _parse_macos_proxy_output(_run_networksetup(["-getwebproxy", service]).stdout)
        https = _parse_macos_proxy_output(_run_networksetup(["-getsecurewebproxy", service]).stdout)
    except Exception as exc:
        log.debug("read macos proxy for %s failed: %s", service, exc)
        return row
    row["http_enabled"] = bool(http["enabled"])
    row["http_server"] = str(http["server"] or "")
    row["http_port"] = int(http["port"] or 0)
    row["https_enabled"] = bool(https["enabled"])
    row["https_server"] = str(https["server"] or "")
    row["https_port"] = int(https["port"] or 0)
    return row


def _macos_hardware_ports() -> dict[str, str]:
    """Map BSD device (en0) → Hardware Port name (Wi-Fi)."""
    try:
        proc = _run_networksetup(["-listallhardwareports"], timeout=10)
    except Exception as exc:
        log.debug("list macos hardware ports failed: %s", exc)
        return {}
    mapping: dict[str, str] = {}
    port_name = ""
    for line in (proc.stdout or "").splitlines():
        text = line.strip()
        if text.startswith("Hardware Port:"):
            port_name = text.split(":", 1)[1].strip()
        elif text.startswith("Device:") and port_name:
            mapping[text.split(":", 1)[1].strip()] = port_name
            port_name = ""
    return mapping


def _macos_default_device() -> str:
    try:
        proc = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        log.debug("macos default route failed: %s", exc)
        return ""
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("interface:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _macos_target_services() -> list[str]:
    """Active default-route service only — not every NIC.

    ``networksetup -setwebproxy`` on each service can pop a separate admin
    password dialog (Thunderbolt Bridge, iPhone USB, …). One NIC is enough.
    """
    available = _list_macos_services()
    if not available:
        return ["Wi-Fi"]
    device = _macos_default_device()
    ports = _macos_hardware_ports()
    name = ports.get(device, "")
    if name and name in available:
        return [name]
    for preferred in ("Wi-Fi", "Ethernet", "USB 10/100/1000 LAN", "USB Ethernet"):
        if preferred in available:
            return [preferred]
    return available[:1]


def _macos_service_matches(row: dict[str, Any], host: str, port: int) -> bool:
    port_i = int(port)
    return (
        bool(row.get("http_enabled"))
        and str(row.get("http_server") or "") == host
        and int(row.get("http_port") or 0) == port_i
        and bool(row.get("https_enabled"))
        and str(row.get("https_server") or "") == host
        and int(row.get("https_port") or 0) == port_i
    )


def _write_macos_service_proxy(
    service: str,
    *,
    enabled: bool,
    host: str = "127.0.0.1",
    port: int = 8866,
    http_enabled: bool | None = None,
    http_server: str = "",
    http_port: int = 0,
    https_enabled: bool | None = None,
    https_server: str = "",
    https_port: int = 0,
) -> tuple[bool, str]:
    """Apply HTTP/HTTPS proxy for one service; skip no-op writes (no extra auth)."""
    current = _read_macos_service_proxy(service)
    errors: list[str] = []

    def _apply_http(want_on: bool, want_host: str, want_port: int, kind: str) -> None:
        cur_on = bool(current.get(f"{kind}_enabled"))
        cur_host = str(current.get(f"{kind}_server") or "")
        cur_port = int(current.get(f"{kind}_port") or 0)
        flag = "-setwebproxy" if kind == "http" else "-setsecurewebproxy"
        state = "-setwebproxystate" if kind == "http" else "-setsecurewebproxystate"
        if want_on and want_host:
            if cur_on and cur_host == want_host and cur_port == int(want_port):
                return
            proc = _run_networksetup([flag, service, want_host, str(int(want_port))])
            if proc.returncode != 0:
                err = ((proc.stdout or "") + (proc.stderr or "")).strip()
                errors.append(err or flag)
            return
        if not cur_on:
            return
        proc = _run_networksetup([state, service, "off"])
        if proc.returncode != 0:
            err = ((proc.stdout or "") + (proc.stderr or "")).strip()
            errors.append(err or state)

    if enabled:
        _apply_http(True, host, int(port), "http")
        _apply_http(True, host, int(port), "https")
    else:
        http_on = bool(http_enabled) if http_enabled is not None else False
        https_on = bool(https_enabled) if https_enabled is not None else False
        _apply_http(http_on, http_server, int(http_port or 0), "http")
        _apply_http(https_on, https_server, int(https_port or 0), "https")
    if errors:
        return False, "; ".join(errors)
    return True, ""


def _read_macos_proxy() -> SystemProxySnapshot:
    for service in _macos_target_services():
        row = _read_macos_service_proxy(service)
        if row.get("http_enabled") or row.get("http_server"):
            server = str(row.get("http_server") or "")
            port = int(row.get("http_port") or 0)
            return SystemProxySnapshot(
                enabled=bool(row.get("http_enabled")),
                server=f"{server}:{port}" if server else "",
                platform="darwin",
            )
    return SystemProxySnapshot(platform="darwin")


def _enable_macos_proxy(host: str, port: int, snapshot: SystemProxySnapshot) -> dict[str, Any]:
    services = _macos_target_services()
    restore_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    succeeded = False
    for service in services:
        before = _read_macos_service_proxy(service)
        restore_rows.append(before)
        if _macos_service_matches(before, host, port):
            succeeded = True
            continue
        ok, err = _write_macos_service_proxy(service, enabled=True, host=host, port=port)
        if ok:
            succeeded = True
        elif err:
            errors.append(f"{service}: {err}")
    snapshot.macos_restore = restore_rows
    snapshot.platform = "darwin"
    if succeeded:
        return {
            "ok": True,
            "services": [row["service"] for row in restore_rows],
            "errors": errors or None,
        }
    for row in restore_rows:
        service = str(row.get("service") or "").strip()
        if not service:
            continue
        _write_macos_service_proxy(
            service,
            enabled=False,
            http_enabled=bool(row.get("http_enabled")),
            http_server=str(row.get("http_server") or ""),
            http_port=int(row.get("http_port") or 0),
            https_enabled=bool(row.get("https_enabled")),
            https_server=str(row.get("https_server") or ""),
            https_port=int(row.get("https_port") or 0),
        )
    return {"ok": False, "error": "; ".join(errors) or "networksetup failed"}


def _restore_macos_proxy(snapshot: SystemProxySnapshot) -> dict[str, Any]:
    rows = list(snapshot.macos_restore or [])
    if not rows:
        host, port = _split_host(snapshot.server)
        if snapshot.enabled and host:
            return _enable_macos_proxy(host, port, snapshot)
        errors: list[str] = []
        succeeded = False
        for service in _macos_target_services():
            ok, err = _write_macos_service_proxy(service, enabled=False)
            if ok:
                succeeded = True
            elif err:
                errors.append(f"{service}: {err}")
        if succeeded:
            return {"ok": True, "errors": errors or None}
        return {"ok": False, "error": "; ".join(errors) or "networksetup failed"}
    errors = []
    succeeded = False
    for row in rows:
        service = str(row.get("service") or "").strip()
        if not service:
            continue
        ok, err = _write_macos_service_proxy(
            service,
            enabled=False,
            http_enabled=bool(row.get("http_enabled")),
            http_server=str(row.get("http_server") or ""),
            http_port=int(row.get("http_port") or 0),
            https_enabled=bool(row.get("https_enabled")),
            https_server=str(row.get("https_server") or ""),
            https_port=int(row.get("https_port") or 0),
        )
        if ok:
            succeeded = True
        elif err:
            errors.append(f"{service}: {err}")
    if succeeded or not errors:
        return {"ok": True, "errors": errors or None}
    return {"ok": False, "error": "; ".join(errors) or "networksetup failed"}
