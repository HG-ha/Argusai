"""Assistant-facing mobile DAST readiness and observation planning tools."""

from __future__ import annotations

import json
from typing import Any

from argusai.core.security import mobile_dast_tools as _mobile


def mobile_dast_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Probe Appium, adb and iOS device tooling without starting anything."""
    return _mobile.mobile_dast_tool_inventory()


async def appium_status(
    api: Any,
    server_url: str = "http://127.0.0.1:4723",
    probe_server: bool = False,
    allow_remote: bool = False,
    timeout_seconds: int = 5,
    **kwargs: Any,
) -> dict[str, Any]:
    """Check Appium availability; network probing is opt-in and bounded."""
    return await _mobile.appium_status(
        server_url,
        probe_server=probe_server,
        allow_remote=allow_remote,
        timeout_seconds=timeout_seconds,
    )


def mobile_dast_plan(
    api: Any,
    platform: str = "",
    app_id: str = "",
    app_path: str = "",
    device_serial: str = "",
    server_url: str = "http://127.0.0.1:4723",
    proxy_url: str = "",
    capabilities_json: str = "{}",
    allow_remote_appium: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an observation-only Appium/ADB/proxy plan without touching a device."""
    try:
        capabilities = json.loads(capabilities_json or "{}")
        if not isinstance(capabilities, dict):
            raise ValueError("capabilities_json must decode to an object")
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "ok": False,
            "blocked": True,
            "reason": "invalid_input",
            "error": str(exc),
            "do_not_retry": True,
        }
    return _mobile.mobile_dast_plan(
        platform,
        app_id=app_id,
        app_path=app_path,
        device_serial=device_serial,
        server_url=server_url,
        proxy_url=proxy_url,
        capabilities=capabilities,
        allow_remote_appium=allow_remote_appium,
    )
