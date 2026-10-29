"""Backward-compatible migration for ``job.json`` records."""

from __future__ import annotations

from typing import Any

from argusai.core.reverse.reverse_constants import (
    DEFAULT_ANALYSIS_SUMMARY,
    DEFAULT_RISK_SUMMARY,
    DEFAULT_TOOLCHAIN_STATUS,
    JOB_SCHEMA_VERSION,
    PLATFORMS,
    TARGET_TYPES,
)


def _copy_summary(default: dict[str, object]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(default)


def infer_target_type(data: dict[str, Any]) -> str:
    explicit = str(data.get("target_type") or "").strip().lower()
    if explicit in TARGET_TYPES:
        return explicit
    tags = {str(t).lower() for t in (data.get("tags") or [])}
    if "android" in tags:
        return "android"
    if "ios" in tags:
        return "ios"
    if "windows" in tags or "pe" in tags:
        return "windows"
    if data.get("target_url"):
        return "web_js"
    return "unknown"


def infer_platform(data: dict[str, Any]) -> str:
    explicit = str(data.get("platform") or "").strip().lower()
    if explicit in PLATFORMS:
        return explicit
    target_type = infer_target_type(data)
    if target_type == "web_js":
        return "web"
    if target_type in PLATFORMS:
        return target_type if target_type in PLATFORMS else "unknown"
    return "unknown"


def migrate_job_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return migrated job dict and whether schema changed."""
    out = dict(data)
    changed = False
    current = int(out.get("schema_version") or 1)

    if current < JOB_SCHEMA_VERSION:
        if "target_type" not in out:
            out["target_type"] = infer_target_type(out)
            changed = True
        if "platform" not in out:
            out["platform"] = infer_platform(out)
            changed = True
        if "target_meta" not in out or not isinstance(out.get("target_meta"), dict):
            out["target_meta"] = {}
            changed = True
        if "toolchain_status" not in out or not isinstance(out.get("toolchain_status"), dict):
            out["toolchain_status"] = _copy_summary(DEFAULT_TOOLCHAIN_STATUS)
            changed = True
        if "analysis_summary" not in out or not isinstance(out.get("analysis_summary"), dict):
            out["analysis_summary"] = _copy_summary(DEFAULT_ANALYSIS_SUMMARY)
            changed = True
        if "risk_summary" not in out or not isinstance(out.get("risk_summary"), dict):
            out["risk_summary"] = _copy_summary(DEFAULT_RISK_SUMMARY)
            changed = True
        for list_key in ("runtime_sessions", "evidence_refs", "replay_refs"):
            if list_key not in out or not isinstance(out.get(list_key), list):
                out[list_key] = []
                changed = True
        out["schema_version"] = JOB_SCHEMA_VERSION
        changed = True

    return out, changed
