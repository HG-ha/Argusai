"""Assistant-facing offline/read-only hardware-security executors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import *


def _path_overrides(paths_json: str, paths: Any) -> dict[str, str]:
    if isinstance(paths, dict):
        return {str(k): str(v) for k, v in paths.items()}
    try:
        parsed = json.loads(paths_json or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def hardware_preflight(
    api: Any,
    paths_json: str = "",
    platform_name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Probe sigrok/CHIPSEC/OpenOCD and report unverified hardware prerequisites."""
    from argusai.core.security.hardware_tools import hardware_toolchain_preflight

    elevated = kwargs.get("elevated")
    return hardware_toolchain_preflight(
        paths=_path_overrides(paths_json, kwargs.get("paths")),
        platform_name=platform_name,
        elevated=None if elevated is None else bool(elevated),
    )


hardware_environment_preflight = hardware_preflight


def hardware_uart_parse(
    api: Any,
    text: str = "",
    path: str = "",
    max_lines: int = 5000,
    max_events: int = 500,
    **kwargs: Any,
) -> dict[str, Any]:
    """Parse supplied text or a bounded existing UART transcript offline."""
    from argusai.core.security.hardware_tools import parse_uart_log

    content = str(text or "")
    if path:
        try:
            capture = Path(_resolve_session_input_path(api, kwargs, path))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not capture.is_file():
            return {"ok": False, "error": f"UART capture not found: {capture}"}
        if capture.stat().st_size > 10 * 1024 * 1024:
            return {"ok": False, "blocked": True, "error": "UART capture exceeds the 10 MiB offline parser limit"}
        try:
            content = capture.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
    return parse_uart_log(
        content,
        max_lines=_safe_int(max_lines, 5000, 1, 50000),
        max_events=_safe_int(max_events, 500, 1, 5000),
    )


uart_log_parse = hardware_uart_parse


def hardware_flash_diff(
    api: Any,
    baseline_path: str = "",
    candidate_path: str = "",
    max_ranges: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """Byte-diff two existing flash images; never read from or write to a board."""
    from argusai.core.security.hardware_tools import diff_flash_images

    try:
        baseline = _resolve_session_input_path(api, kwargs, baseline_path)
        candidate = _resolve_session_input_path(api, kwargs, candidate_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return diff_flash_images(
        baseline,
        candidate,
        max_ranges=_safe_int(max_ranges, 200, 1, 5000),
    )


flash_image_diff = hardware_flash_diff


def hardware_sigrok_plan(
    api: Any,
    capture_path: str = "",
    decoder: str = "",
    channels: list[str] | str | None = None,
    decoder_options_json: str = "{}",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an offline sigrok-cli decode plan; no acquisition is started."""
    from argusai.core.security.hardware_tools import build_sigrok_offline_plan

    try:
        capture = _resolve_session_input_path(api, kwargs, capture_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        options = json.loads(decoder_options_json or "{}")
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"decoder_options_json must be an object: {exc}"}
    if not isinstance(options, dict):
        return {"ok": False, "error": "decoder_options_json must be an object"}
    return build_sigrok_offline_plan(
        capture,
        decoder=decoder,
        channels=channels,
        decoder_options=options,
    )


sigrok_offline_plan = hardware_sigrok_plan


def hardware_readonly_plan(
    api: Any,
    tool: str = "",
    action: str = "",
    paths_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Resolve exact CHIPSEC/OpenOCD/sigrok preflight actions; never execute."""
    from argusai.core.security.hardware_tools import build_readonly_hardware_plan

    elevated = kwargs.get("elevated")
    return build_readonly_hardware_plan(
        tool,
        action,
        paths=_path_overrides(paths_json, kwargs.get("paths")),
        elevated=None if elevated is None else bool(elevated),
    )


def hardware_action_check(
    api: Any,
    tool: str = "",
    action: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Check whether a hardware action is in the exact read-only allowlist."""
    from argusai.core.security.hardware_tools import hardware_action_policy

    return hardware_action_policy(tool, action)


__all__ = [
    "flash_image_diff",
    "hardware_action_check",
    "hardware_environment_preflight",
    "hardware_flash_diff",
    "hardware_preflight",
    "hardware_readonly_plan",
    "hardware_sigrok_plan",
    "hardware_uart_parse",
    "sigrok_offline_plan",
    "uart_log_parse",
]
