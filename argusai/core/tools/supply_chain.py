"""Thin assistant-tool wrappers for offline SCA and SBOM operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import *

_MAX_SBOM_BYTES = 20_000_000


def _case_store(api: Any):
    from argusai.core.workspace.cases import CaseStore

    return CaseStore(Path(api.settings_mgr.get().data_root) / "cases")


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


def _resolve_source(
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


def _source_or_error(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    case_id: str,
    artifact_id: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        return _resolve_source(
            api,
            kwargs,
            path=path,
            case_id=case_id,
            artifact_id=artifact_id,
        ), None
    except (ValueError, FileNotFoundError, OSError) as exc:
        return None, {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_local_input",
            "error": str(exc),
        }


def _attach_result(
    api: Any,
    result: dict[str, Any],
    *,
    case_id: str,
    source_artifact_id: str,
    tool: str,
) -> dict[str, Any]:
    cid = str(case_id or "").strip()
    if not cid:
        return result
    try:
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if len(payload) > 300_000:
            payload = payload[:300_000] + "\n...[result truncated before case attachment]"
        artifact = _case_store(api).add_artifact(
            cid,
            kind="trace",
            title=f"{tool} offline result",
            content=payload,
            filename=f"{tool}-offline-result.json",
            domain="supply_chain",
            media_type="application/json",
            metadata={
                "tool": tool,
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


def sca_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report Syft, Trivy, and OSV-Scanner availability without installing anything."""
    from argusai.core.security.sca_tools import sca_tool_inventory as _tool

    return _tool()


def syft_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report local Syft availability."""
    from argusai.core.security.sca_tools import syft_status as _tool

    return _tool()


def trivy_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report local Trivy availability."""
    from argusai.core.security.sca_tools import trivy_status as _tool

    return _tool()


def osv_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report local OSV-Scanner availability."""
    from argusai.core.security.sca_tools import osv_status as _tool

    return _tool()


def osv_scanner_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for osv_status."""
    return osv_status(api, **kwargs)


def syft_plan_inventory(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    output_format: str = "cyclonedx-json",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an offline local-source Syft inventory/SBOM plan."""
    from argusai.core.security.sca_tools import syft_plan_inventory as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(str(source), output_format=output_format)


async def syft_run_inventory(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    output_format: str = "cyclonedx-json",
    timeout_seconds: int = 300,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Generate and parse a local SBOM; suitable for background registration."""
    from argusai.core.security.sca_tools import syft_run_inventory as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(source),
        output_format=output_format,
        timeout_seconds=_safe_int(timeout_seconds, 300, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            tool="syft",
        )
    return result


def trivy_plan_scan(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    mode: str = "filesystem",
    severities: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a Trivy plan with offline scan and all DB updates disabled."""
    from argusai.core.security.sca_tools import trivy_plan_scan as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(str(source), mode=mode, severities=severities)


async def trivy_run_scan(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    mode: str = "filesystem",
    severities: str = "",
    timeout_seconds: int = 300,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run and parse a strictly offline Trivy scan; suitable for background registration."""
    from argusai.core.security.sca_tools import trivy_run_scan as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(source),
        mode=mode,
        severities=severities,
        timeout_seconds=_safe_int(timeout_seconds, 300, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            tool="trivy",
        )
    return result


def osv_plan_scan(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    mode: str = "source",
    recursive: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an OSV-Scanner plan with explicit offline mode."""
    from argusai.core.security.sca_tools import osv_plan_scan as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    return _tool(str(source), mode=mode, recursive=bool(recursive))


async def osv_run_scan(
    api: Any,
    path: str = "",
    case_id: str = "",
    artifact_id: str = "",
    mode: str = "source",
    recursive: bool = True,
    timeout_seconds: int = 300,
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run and parse OSV-Scanner offline; suitable for background registration."""
    from argusai.core.security.sca_tools import osv_run_scan as _tool

    source, error = _source_or_error(
        api, kwargs, path=path, case_id=case_id, artifact_id=artifact_id
    )
    if error:
        return error
    result = await _tool(
        str(source),
        mode=mode,
        recursive=bool(recursive),
        timeout_seconds=_safe_int(timeout_seconds, 300, 10, 1800),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id=artifact_id,
            tool="osv-scanner",
        )
    return result


def _read_sbom_path(api: Any, kwargs: dict[str, Any], raw_path: str) -> str:
    path = Path(_resolve_session_input_path(api, kwargs, raw_path)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SBOM file not found: {path}")
    size = path.stat().st_size
    if size > _MAX_SBOM_BYTES:
        raise ValueError(f"SBOM exceeds {_MAX_SBOM_BYTES} byte limit")
    return path.read_text(encoding="utf-8", errors="replace")


def sbom_diff(
    api: Any,
    old_path: str = "",
    new_path: str = "",
    old_json: str = "",
    new_json: str = "",
    max_items: int = 200,
    case_id: str = "",
    attach_to_case: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Diff two local Syft/CycloneDX/SPDX SBOM files or supplied JSON objects."""
    from argusai.core.security.sca_tools import sbom_diff as _tool

    try:
        before = str(old_json or "") or _read_sbom_path(api, kwargs, old_path)
        after = str(new_json or "") or _read_sbom_path(api, kwargs, new_path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_local_input",
            "error": str(exc),
        }
    result = _tool(
        before,
        after,
        max_items=_safe_int(max_items, 200, 1, 1000),
    )
    if attach_to_case:
        _attach_result(
            api,
            result,
            case_id=case_id,
            source_artifact_id="",
            tool="sbom-diff",
        )
    return result


syft_plan_sbom = syft_plan_inventory
syft_generate_sbom = syft_run_inventory
