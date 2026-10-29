"""Import HAR / Burp proxy logs via GUI or Agent tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .browser_reverse import _resolve_reverse_store
from .common import _resolve_session_input_path


def _resolve_import_path(api: Any, kwargs: dict[str, Any], path: str) -> Path:
    text = str(path or "").strip()
    if not text:
        raise ValueError("path is required")
    candidate = Path(text).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    return Path(_resolve_session_input_path(api, kwargs, text)).resolve()


def _attach_import_to_job(
    store: Any,
    job_id: str,
    *,
    tool: str,
    analysis: dict[str, Any],
    raw_bytes: bytes,
    filename: str,
    kind: str,
) -> dict[str, Any]:
    from argusai.core.reverse.reverse_artifacts import artifact_ref

    if not job_id or not analysis.get("ok"):
        return {}
    har_rec = store.add_artifact(
        job_id,
        content=raw_bytes,
        kind=kind,
        filename=filename,
        tool=tool,
        note=f"imported {kind} from {analysis.get('filename') or filename}",
    )
    summary = json.dumps(analysis, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    trace_rec = store.add_artifact(
        job_id,
        content=summary,
        kind="trace",
        filename=f"{tool}-analysis.json",
        tool=tool,
        note=f"{tool} summary ({analysis.get('entry_count', 0)} entries)",
    )
    evidence = store.link_evidence_ref(
        job_id,
        ref_type=kind,
        ref_id=str(har_rec.get("id") or ""),
        note=str(analysis.get("filename") or filename)[:500],
    )
    return {
        "artifact": har_rec,
        "artifact_ref": artifact_ref(job_id, har_rec["id"]),
        "analysis_artifact": trace_rec,
        "analysis_artifact_ref": artifact_ref(job_id, trace_rec["id"]),
        "evidence_ref": evidence,
        "job_id": job_id,
    }


def proxy_import_har(
    api: Any,
    path: str = "",
    job_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a HAR JSON file from disk and analyze captured requests."""
    from argusai.core.security.proxy_import import analyze_har_path

    try:
        src = _resolve_import_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    result = analyze_har_path(src)
    if not result.get("ok"):
        return result

    result["summary"] = f"Imported HAR · {result.get('entry_count', 0)} entries · {src.name}"
    result["next_step"] = "Use web_request_replay or api_inventory_build on interesting URLs."

    if attach_to_job and str(job_id or "").strip():
        store = _resolve_reverse_store(api)
        if store is None:
            result["attach_error"] = "reverse-job store unavailable"
        elif store.get(str(job_id).strip()) is None:
            result["attach_error"] = f"job not found: {job_id}"
        else:
            try:
                raw = src.read_bytes()
            except OSError as exc:
                result["attach_error"] = str(exc)
            else:
                result["attached"] = _attach_import_to_job(
                    store,
                    str(job_id).strip(),
                    tool="proxy_import_har",
                    analysis=result,
                    raw_bytes=raw,
                    filename=src.name,
                    kind="har",
                )
                result["job_id"] = str(job_id).strip()
    return result


def proxy_import_burp_log(
    api: Any,
    path: str = "",
    job_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Parse a Burp Suite plain-text proxy log and extract HTTP requests."""
    from argusai.core.security.proxy_import import analyze_burp_log_path, burp_log_to_har_json

    try:
        src = _resolve_import_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    result = analyze_burp_log_path(src)
    if not result.get("ok"):
        return result

    result["summary"] = f"Imported Burp log · {result.get('entry_count', 0)} requests · {src.name}"
    result["next_step"] = "Use web_request_replay or har_import_analyze on stored HAR artifact."

    if attach_to_job and str(job_id or "").strip():
        store = _resolve_reverse_store(api)
        if store is None:
            result["attach_error"] = "reverse-job store unavailable"
        elif store.get(str(job_id).strip()) is None:
            result["attach_error"] = f"job not found: {job_id}"
        else:
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
                har_wrap = burp_log_to_har_json(text)
                raw = json.dumps(har_wrap.get("har") or {}, ensure_ascii=False, indent=2).encode("utf-8")
            except OSError as exc:
                result["attach_error"] = str(exc)
            else:
                result["attached"] = _attach_import_to_job(
                    store,
                    str(job_id).strip(),
                    tool="proxy_import_burp_log",
                    analysis=result,
                    raw_bytes=raw,
                    filename=f"{src.stem}.har.json",
                    kind="har",
                )
                result["job_id"] = str(job_id).strip()
    return result
