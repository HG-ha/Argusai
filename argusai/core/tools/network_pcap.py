"""Thin assistant-tool wrappers for offline PCAP analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import *


def _case_store(api: Any):
    from argusai.core.paths import PathManager
    from argusai.core.workspace.cases import CaseStore

    settings = api.settings_mgr.get()
    data_root = PathManager(data_root=settings.data_root).data_root
    return CaseStore(Path(data_root) / "cases")


def _resolve_case_artifact(api: Any, case_id: str, artifact_id: str) -> Path:
    store = _case_store(api)
    case = store.get_case(case_id)
    artifact = next(
        (item for item in case.get("artifacts") or [] if item.get("id") == artifact_id),
        None,
    )
    if artifact is None:
        raise FileNotFoundError(f"artifact not found: {artifact_id}")
    case_dir = (Path(store.root) / case_id).resolve()
    path = (case_dir / str(artifact.get("path") or "")).resolve()
    if path != case_dir and case_dir not in path.parents:
        raise ValueError("artifact path escapes case directory")
    if not path.is_file():
        raise FileNotFoundError(f"artifact file missing: {artifact_id}")
    return path


def _resolve_pcap_input(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    case_id: str,
    artifact_id: str,
) -> Path:
    cid = str(case_id or "").strip()
    aid = str(artifact_id or "").strip()
    if aid:
        if not cid:
            raise ValueError("case_id is required with artifact_id")
        return _resolve_case_artifact(api, cid, aid)
    return Path(_resolve_session_input_path(api, kwargs, path)).resolve()


def _input_or_error(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    case_id: str,
    artifact_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        resolved = _resolve_pcap_input(
            api,
            kwargs,
            path=path,
            case_id=case_id,
            artifact_id=artifact_id,
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        return None, {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_local_input",
            "error": str(exc),
        }
    return resolved, None


def _attach_result(
    api: Any,
    result: dict[str, Any],
    *,
    case_id: str,
    source_artifact_id: str,
    operation: str,
) -> dict[str, Any]:
    cid = str(case_id or "").strip()
    if not cid:
        return result
    try:
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if len(payload) > 200_000:
            payload = payload[:200_000] + "\n...[result truncated before case attachment]"
        artifact = _case_store(api).add_artifact(
            cid,
            kind="trace",
            title=f"PCAP {operation} result",
            content=payload,
            filename=f"pcap-{operation}.json",
            domain="network",
            media_type="application/json",
            metadata={
                "tool": f"pcap_{operation}",
                "source_artifact_id": str(source_artifact_id or ""),
                "offline": True,
            },
        )
        result["attached"] = {
            "case_id": cid,
            "artifact": artifact,
            "artifact_ref": artifact["id"],
        }
    except (FileNotFoundError, OSError, ValueError) as exc:
        result["attach_error"] = str(exc)
    return result


def pcap_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report TShark availability for offline packet-capture analysis."""
    from argusai.core.security.pcap_tools import pcap_tool_inventory as _tool

    return _tool()


def tshark_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report whether TShark is available; no installation is attempted."""
    from argusai.core.security.pcap_tools import tshark_status as _tool

    return _tool()


def pcap_summary_plan(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a bounded offline TShark summary plan without executing it."""
    from argusai.core.security.pcap_tools import pcap_summary_plan as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(str(capture), max_packets=_safe_int(max_packets, 5000, 1, 100_000))


async def pcap_summary(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    timeout_seconds: int = 120,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run bounded offline packet metadata extraction; suitable for background registration."""
    from argusai.core.security.pcap_tools import pcap_summary as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(capture),
        max_packets=_safe_int(max_packets, 5000, 1, 100_000),
        timeout_seconds=_safe_int(timeout_seconds, 120, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            operation="summary",
        )
    return result


def pcap_filter_plan(
    api: Any,
    path: str = "",
    display_filter: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a bounded offline TShark display-filter plan."""
    from argusai.core.security.pcap_tools import pcap_filter_plan as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(
        str(capture),
        display_filter,
        max_packets=_safe_int(max_packets, 5000, 1, 100_000),
    )


async def pcap_filter(
    api: Any,
    path: str = "",
    display_filter: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    timeout_seconds: int = 120,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a bounded offline TShark display filter; suitable for background registration."""
    from argusai.core.security.pcap_tools import pcap_filter as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(capture),
        display_filter,
        max_packets=_safe_int(max_packets, 5000, 1, 100_000),
        timeout_seconds=_safe_int(timeout_seconds, 120, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            operation="filter",
        )
    return result


def pcap_http_extract_plan(
    api: Any,
    path: str = "",
    display_filter: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a clear-text HTTP metadata extraction plan for a local capture."""
    from argusai.core.security.pcap_tools import pcap_http_extract_plan as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(
        str(capture),
        display_filter=display_filter,
        max_packets=_safe_int(max_packets, 5000, 1, 100_000),
    )


async def pcap_http_extract(
    api: Any,
    path: str = "",
    display_filter: str = "",
    case_id: str = "",
    artifact_id: str = "",
    max_packets: int = 5000,
    timeout_seconds: int = 120,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Extract bounded clear-text HTTP metadata; suitable for background registration."""
    from argusai.core.security.pcap_tools import pcap_http_extract as _tool

    capture, error = _input_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(capture),
        display_filter=display_filter,
        max_packets=_safe_int(max_packets, 5000, 1, 100_000),
        timeout_seconds=_safe_int(timeout_seconds, 120, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            operation="http-extract",
        )
    return result
