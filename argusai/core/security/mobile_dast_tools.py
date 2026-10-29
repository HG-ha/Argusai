"""Conservative mobile DAST readiness probes and execution plans.

This module does not create Appium sessions, install/uninstall applications,
grant permissions, reset devices, or automate destructive UI actions.  It
connects the existing adb/proxy concepts into an observation-first plan.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

_PLATFORMS = frozenset({"android", "ios"})
_SAFE_CAPABILITIES = frozenset({
    "platformName",
    "appium:automationName",
    "appium:deviceName",
    "appium:udid",
    "appium:app",
    "appium:appPackage",
    "appium:appActivity",
    "appium:bundleId",
    "appium:noReset",
    "appium:newCommandTimeout",
    "appium:autoWebview",
})
_FORBIDDEN_CAPABILITY_FRAGMENTS = (
    "fullreset",
    "autograntpermissions",
    "otherapps",
    "uninstall",
    "unlock",
    "skipunlock",
    "chromedriverexecutable",
    "adbexec",
)


def _blocked(reason: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "tool": "appium",
        "reason": reason,
        "error": error,
        "do_not_retry": reason in {"invalid_input", "unsafe_operation", "tool_unavailable"},
        **extra,
    }


def mobile_dast_tool_inventory() -> dict[str, Any]:
    """Probe executable availability without starting a server or device."""
    tools = {}
    for name in ("appium", "adb", "idevice_id"):
        path = str(shutil.which(name) or "")
        tools[name] = {"installed": bool(path), "path": path}
    return {
        "ok": True,
        "tools": tools,
        "capability_mode": "plan-only",
        "automatic_actions": False,
        "forbidden": [
            "application install/uninstall/reset",
            "permission grants",
            "device unlock",
            "destructive UI actions",
        ],
    }


def _server_url(raw: str, *, allow_remote: bool) -> tuple[str, dict[str, Any] | None]:
    value = str(raw or "http://127.0.0.1:4723").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "", _blocked("invalid_input", "Appium server URL must be http(s)")
    if parsed.username or parsed.password:
        return "", _blocked("invalid_input", "credentials in the Appium server URL are forbidden")
    if not allow_remote:
        host = parsed.hostname.lower()
        local = host == "localhost"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not local:
            return "", _blocked(
                "unsafe_operation",
                "remote Appium status probes require allow_remote=true",
                server=value,
            )
    return value, None


def parse_appium_status_response(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Normalize Appium 1/2 ``GET /status`` responses."""
    try:
        if isinstance(payload, dict):
            data = payload
        else:
            text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload or "")
            data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid Appium status JSON: {exc}", "ready": False}
    value = data.get("value") if isinstance(data.get("value"), dict) else data
    build = value.get("build") if isinstance(value.get("build"), dict) else {}
    ready = value.get("ready")
    if ready is None:
        ready = int(data.get("status", 1) or 1) == 0
    return {
        "ok": True,
        "ready": bool(ready),
        "message": str(value.get("message") or ""),
        "version": str(build.get("version") or value.get("version") or ""),
        "build": {
            key: build.get(key)
            for key in ("version", "revision", "built")
            if build.get(key) is not None
        },
    }


async def appium_status(
    server_url: str = "http://127.0.0.1:4723",
    *,
    probe_server: bool = False,
    allow_remote: bool = False,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Report Appium/adb readiness; optionally perform only ``GET /status``."""
    inventory = mobile_dast_tool_inventory()
    endpoint, error = _server_url(server_url, allow_remote=allow_remote)
    if error:
        return error
    if not probe_server:
        if not inventory["tools"]["appium"]["installed"]:
            return _blocked(
                "tool_unavailable",
                "appium is not installed on PATH",
                external_tool_required="appium",
                can_install=False,
                inventory=inventory,
                server=endpoint,
                suggested_fix="Install Appium on the host or point to an already running authorized server and set probe_server=true.",
            )
        return {
            "ok": True,
            "tool": "appium",
            "installed": True,
            "server": endpoint,
            "server_probed": False,
            "ready": None,
            "inventory": inventory,
        }
    try:
        async with httpx.AsyncClient(
            timeout=max(1.0, min(float(timeout_seconds or 5.0), 15.0)),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"{endpoint}/status")
    except (httpx.HTTPError, OSError) as exc:
        return _blocked(
            "environment_unavailable",
            f"Appium status endpoint is unavailable: {type(exc).__name__}",
            server=endpoint,
            inventory=inventory,
        )
    parsed = parse_appium_status_response(response.content)
    if response.status_code < 200 or response.status_code >= 300 or not parsed.get("ok"):
        return _blocked(
            "environment_unavailable",
            f"Appium status probe returned HTTP {response.status_code}",
            server=endpoint,
            status_code=response.status_code,
            parsed=parsed,
            inventory=inventory,
        )
    return {
        "ok": bool(parsed.get("ready")),
        "blocked": not bool(parsed.get("ready")),
        "tool": "appium",
        "reason": "" if parsed.get("ready") else "environment_unavailable",
        "server": endpoint,
        "server_probed": True,
        "status_code": response.status_code,
        "ready": bool(parsed.get("ready")),
        "status": parsed,
        "inventory": inventory,
    }


def _safe_proxy_url(proxy_url: str) -> tuple[str, dict[str, Any] | None]:
    raw = str(proxy_url or "").strip()
    if not raw:
        return "", None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        return "", _blocked("invalid_input", "proxy_url must include http(s), host, and port")
    if parsed.username or parsed.password:
        return "", _blocked("invalid_input", "proxy credentials must not be embedded in a DAST plan")
    return raw.rstrip("/"), None


def _normalize_capabilities(
    capabilities: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized: dict[str, Any] = {}
    for key, value in (capabilities or {}).items():
        name = str(key or "").strip()
        lowered = name.lower().replace("-", "").replace("_", "")
        if any(fragment in lowered for fragment in _FORBIDDEN_CAPABILITY_FRAGMENTS):
            return {}, _blocked("unsafe_operation", f"destructive Appium capability is forbidden: {name}")
        if name not in _SAFE_CAPABILITIES:
            return {}, _blocked("invalid_input", f"Appium capability is not allowlisted: {name}")
        normalized[name] = value
    if normalized.get("appium:noReset") is False:
        return {}, _blocked("unsafe_operation", "appium:noReset=false is forbidden by the non-reset policy")
    normalized["appium:noReset"] = True
    return normalized, None


def mobile_dast_plan(
    platform: str,
    *,
    app_id: str = "",
    app_path: str | Path = "",
    device_serial: str = "",
    server_url: str = "http://127.0.0.1:4723",
    proxy_url: str = "",
    capabilities: dict[str, Any] | None = None,
    allow_remote_appium: bool = False,
) -> dict[str, Any]:
    """Create an observation-only mobile DAST plan; performs no device action."""
    selected = str(platform or "").strip().lower()
    if selected not in _PLATFORMS:
        return _blocked("invalid_input", f"unsupported platform: {selected}")
    identifier = str(app_id or "").strip()
    source = str(app_path or "").strip()
    if not identifier and not source:
        return _blocked("invalid_input", "app_id or app_path is required")
    if source:
        resolved = Path(source).expanduser().resolve()
        if not resolved.is_file():
            return _blocked("environment_unavailable", f"application package not found: {resolved}")
        source = str(resolved)
    endpoint, server_error = _server_url(server_url, allow_remote=allow_remote_appium)
    if server_error:
        return server_error
    proxy, proxy_error = _safe_proxy_url(proxy_url)
    if proxy_error:
        return proxy_error
    caps, capability_error = _normalize_capabilities(capabilities)
    if capability_error:
        return capability_error

    caps.setdefault("platformName", "Android" if selected == "android" else "iOS")
    caps.setdefault("appium:automationName", "UiAutomator2" if selected == "android" else "XCUITest")
    if device_serial:
        caps.setdefault("appium:udid", str(device_serial).strip())
    if source:
        caps.setdefault("appium:app", source)
    if identifier:
        caps.setdefault("appium:appPackage" if selected == "android" else "appium:bundleId", identifier)

    device_tool = "adb_devices" if selected == "android" else "frida_list_devices"
    return {
        "ok": True,
        "type": "mobile-dast-plan",
        "platform": selected,
        "app_id": identifier,
        "app_path": source,
        "server": endpoint,
        "proxy": proxy,
        "capabilities": caps,
        "risk": "low",
        "read_only_plan": True,
        "automatic_actions": False,
        "steps": [
            {
                "id": "environment",
                "tool": "appium_status",
                "arguments": {"server_url": endpoint, "probe_server": True},
                "action": "Verify the already running Appium server.",
            },
            {
                "id": "device",
                "tool": device_tool,
                "arguments": {"serial": str(device_serial).strip()} if selected == "android" else {},
                "action": "List the already connected test device; do not unlock, reset, or enroll it.",
            },
            {
                "id": "traffic",
                "tool": "start_proxy" if proxy else "",
                "action": (
                    "Reuse the configured ArgusAI proxy and configure the lab device manually."
                    if proxy
                    else "Choose an existing authorized proxy capture before testing network behavior."
                ),
                "proxy": proxy,
            },
            {
                "id": "manual-session",
                "action": "Create and drive the Appium session manually from the approved lab harness.",
                "implemented": False,
                "reason": "ArgusAI does not automate application state changes or destructive UI actions.",
            },
            {
                "id": "observe",
                "tools": ["get_proxy_traffic", "adb_logcat"] if selected == "android" else ["get_proxy_traffic"],
                "action": "Collect bounded traffic/log evidence and correlate it with manual interactions.",
            },
            {
                "id": "report",
                "action": "Report evidence, affected flow, confidence, and remediation; do not claim unobserved behavior.",
            },
        ],
        "forbidden": [
            "install/uninstall/reset",
            "automatic permission grants",
            "unlock or passcode automation",
            "destructive taps, purchases, submissions, or data deletion",
        ],
    }


build_mobile_dast_plan = mobile_dast_plan
