"""Assistant-facing offline IoT/firmware tool executors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import *


def _tool_paths(paths_json: str = "", paths: dict[str, str] | None = None) -> dict[str, str]:
    if isinstance(paths, dict):
        return {str(k): str(v) for k, v in paths.items()}
    try:
        parsed = json.loads(paths_json or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def iot_firmware_preflight(
    api: Any,
    paths_json: str = "",
    platform_name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Probe Binwalk/EMBA/FirmAE/YARA/capa without running or installing them."""
    from argusai.core.security.firmware_tools import firmware_toolchain_preflight

    return firmware_toolchain_preflight(
        paths=_tool_paths(paths_json, kwargs.get("paths")),
        platform_name=platform_name,
    )


firmware_preflight = iot_firmware_preflight


def firmware_path_validate(
    api: Any,
    path: str = "",
    workspace_root: str = "",
    kind: str = "any",
    must_exist: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Validate a firmware/rootfs path against an explicit workspace boundary."""
    from argusai.core.security.firmware_tools import firmware_path_status

    try:
        root = (
            Path(_resolve_session_input_path(api, kwargs, workspace_root))
            if workspace_root
            else Path(_session_sandbox_root(api, kwargs))
        )
    except ValueError as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}
    return firmware_path_status(root, path, must_exist=bool(must_exist), kind=kind)


def firmware_rootfs_grep(
    api: Any,
    rootfs_path: str = "",
    pattern: str = "",
    glob: str = "",
    regex: bool = True,
    case_sensitive: bool = False,
    max_matches: int = 200,
    max_files: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Search bounded text/config files in an already-extracted rootfs."""
    from argusai.core.security.firmware_tools import rootfs_grep

    try:
        root = _resolve_session_input_path(api, kwargs, rootfs_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return rootfs_grep(
        root,
        pattern,
        glob=glob,
        regex=bool(regex),
        case_sensitive=bool(case_sensitive),
        max_matches=_safe_int(max_matches, 200, 1, 2000),
        max_files=_safe_int(max_files, 5000, 1, 20000),
    )


rootfs_grep = firmware_rootfs_grep


def firmware_service_map(
    api: Any,
    rootfs_path: str = "",
    max_services: int = 500,
    **kwargs: Any,
) -> dict[str, Any]:
    """Map systemd/SysV/OpenRC/xinetd service definitions offline."""
    from argusai.core.security.firmware_tools import map_rootfs_services

    try:
        root = _resolve_session_input_path(api, kwargs, rootfs_path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return map_rootfs_services(
        root,
        max_services=_safe_int(max_services, 500, 1, 2000),
    )


rootfs_service_map = firmware_service_map


def firmware_analysis_plan(
    api: Any,
    path: str = "",
    workspace_root: str = "",
    job_id: str = "",
    rules_path: str = "",
    paths_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a contained firmware→rootfs→ReverseJob/YARA/capa workflow."""
    from argusai.core.security.firmware_tools import build_firmware_analysis_plan

    try:
        firmware = Path(_resolve_session_input_path(api, kwargs, path))
        if workspace_root:
            root = Path(_resolve_session_input_path(api, kwargs, workspace_root))
            firmware_arg: str | Path = firmware
        else:
            root = firmware.parent
            firmware_arg = firmware.name
        rules_arg: str | Path = ""
        if rules_path:
            rules = Path(_resolve_session_input_path(api, kwargs, rules_path))
            if rules == root or root in rules.parents:
                rules_arg = rules
            else:
                return {
                    "ok": False,
                    "blocked": True,
                    "blocked_reason": "path_containment",
                    "error": "rules_path must be inside workspace_root",
                }
    except ValueError as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}
    return build_firmware_analysis_plan(
        firmware_arg,
        workspace_root=root,
        job_id=job_id,
        rules_path=rules_arg,
        paths=_tool_paths(paths_json, kwargs.get("paths")),
    )


iot_firmware_analysis_plan = firmware_analysis_plan


__all__ = [
    "firmware_analysis_plan",
    "firmware_path_validate",
    "firmware_preflight",
    "firmware_rootfs_grep",
    "firmware_service_map",
    "iot_firmware_analysis_plan",
    "iot_firmware_preflight",
    "rootfs_grep",
    "rootfs_service_map",
]
