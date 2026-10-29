"""Offline-first IaC scanner plans, runners, and report parsers."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from argusai.core.infra.command import CommandRequest, CommandRunner
from argusai.core.security.cloud_tools import parse_trivy_json

_CHECKOV_FRAMEWORKS = frozenset({
    "terraform", "terraform_plan", "cloudformation", "kubernetes", "helm",
    "dockerfile", "arm", "bicep", "serverless", "github_actions",
})


def _blocked(tool: str, reason: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "tool": tool,
        "reason": reason,
        "error": error,
        "do_not_retry": reason in {"invalid_input", "unsafe_operation", "tool_unavailable"},
        **extra,
    }


def iac_tool_inventory() -> dict[str, Any]:
    tools = {}
    for name in ("trivy", "checkov"):
        path = str(shutil.which(name) or "")
        tools[name] = {"installed": bool(path), "path": path}
    return {
        "ok": True,
        "tools": tools,
        "mode": "local-files-read-only",
        "network_required": False,
    }


def _require_tool(tool: str) -> dict[str, Any] | None:
    if shutil.which(tool):
        return None
    return _blocked(
        tool,
        "tool_unavailable",
        f"{tool} is not installed on PATH",
        external_tool_required=tool,
        can_install=False,
        suggested_fix=f"Install {tool} on the host and ensure it is on PATH.",
    )


def _local_target(path: str | Path) -> tuple[Path | None, dict[str, Any] | None]:
    text = str(path or "").strip()
    if not text:
        return None, _blocked("iac", "invalid_input", "path is required")
    target = Path(text).expanduser().resolve()
    if not target.exists():
        return None, _blocked("iac", "environment_unavailable", f"IaC path not found: {target}")
    return target, None


def trivy_iac_plan(
    path: str | Path,
    *,
    severities: str | list[str] | None = "MEDIUM,HIGH,CRITICAL",
    offline: bool = True,
) -> dict[str, Any]:
    """Build ``trivy config`` for a local file/tree."""
    target, error = _local_target(path)
    if error:
        error["tool"] = "trivy"
        return error
    unavailable = _require_tool("trivy")
    if unavailable:
        return unavailable
    raw = severities if isinstance(severities, list) else str(severities or "").replace(" ", ",").split(",")
    allowed = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
    normalized = [value for value in dict.fromkeys(str(item).strip().upper() for item in raw) if value in allowed]
    if not normalized:
        return _blocked("trivy", "invalid_input", "at least one valid severity is required")
    args = [
        "config",
        "--format", "json",
        "--no-progress",
        "--severity", ",".join(normalized),
    ]
    if offline:
        args.append("--skip-check-update")
    args.append(str(target))
    return {
        "ok": True,
        "tool": "trivy",
        "command": "trivy",
        "args": args,
        "target": str(target),
        "risk": "low",
        "read_only": True,
        "offline": bool(offline),
    }


async def trivy_iac_scan(
    path: str | Path,
    *,
    severities: str | list[str] | None = "MEDIUM,HIGH,CRITICAL",
    offline: bool = True,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    plan = trivy_iac_plan(path, severities=severities, offline=offline)
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        "localhost",
        [],
        "low",
        float(max(30, min(int(timeout_seconds or 300), 1800))),
    ))
    result["plan"] = plan
    result["parsed"] = parse_trivy_json(str(result.get("stdout_excerpt") or ""))
    return result


def checkov_plan_scan(
    path: str | Path,
    *,
    framework: str = "",
    compact: bool = True,
) -> dict[str, Any]:
    """Build a local Checkov JSON scan with no external checks or downloads."""
    target, error = _local_target(path)
    if error:
        error["tool"] = "checkov"
        return error
    unavailable = _require_tool("checkov")
    if unavailable:
        return unavailable
    selected_framework = str(framework or "").strip().lower()
    if selected_framework and selected_framework not in _CHECKOV_FRAMEWORKS:
        return _blocked("checkov", "invalid_input", f"unsupported framework: {selected_framework}")
    args = ["--output", "json", "--quiet", "--skip-download"]
    args.extend(["--directory", str(target)] if target.is_dir() else ["--file", str(target)])
    if compact:
        args.append("--compact")
    if selected_framework:
        args.extend(["--framework", selected_framework])
    return {
        "ok": True,
        "tool": "checkov",
        "command": "checkov",
        "args": args,
        "target": str(target),
        "framework": selected_framework,
        "risk": "low",
        "read_only": True,
        "offline": True,
    }


def _checkov_sections(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("results"), dict):
        return [data]
    sections: list[dict[str, Any]] = []
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("results"), dict):
            sections.append({"check_type": key, **value})
    return sections


def parse_checkov_json(payload: str | bytes | dict[str, Any] | list[Any]) -> dict[str, Any]:
    """Normalize single- and multi-framework Checkov JSON output."""
    try:
        if isinstance(payload, (dict, list)):
            data = payload
        else:
            text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload or "")
            data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid Checkov JSON: {exc}", "findings": [], "failed_count": 0}

    findings: list[dict[str, Any]] = []
    passed_count = 0
    skipped_count = 0
    for section in _checkov_sections(data):
        check_type = str(section.get("check_type") or "")
        results = section.get("results") if isinstance(section.get("results"), dict) else {}
        passed = results.get("passed_checks") if isinstance(results.get("passed_checks"), list) else []
        skipped = results.get("skipped_checks") if isinstance(results.get("skipped_checks"), list) else []
        failed = results.get("failed_checks") if isinstance(results.get("failed_checks"), list) else []
        passed_count += len(passed)
        skipped_count += len(skipped)
        for row in failed:
            if not isinstance(row, dict):
                continue
            guideline = row.get("guideline")
            findings.append({
                "id": str(row.get("check_id") or row.get("bc_check_id") or ""),
                "name": str(row.get("check_name") or ""),
                "framework": check_type,
                "file": str(row.get("file_path") or row.get("repo_file_path") or ""),
                "resource": str(row.get("resource") or ""),
                "line_start": (row.get("file_line_range") or [None])[0],
                "line_end": (row.get("file_line_range") or [None, None])[-1],
                "guideline": str(guideline or ""),
                "severity": str(row.get("severity") or "unknown").lower(),
            })
    return {
        "ok": True,
        "format": "checkov-json",
        "failed_count": len(findings),
        "passed_count": passed_count,
        "skipped_count": skipped_count,
        "findings": findings[:500],
        "framework_counts": dict(Counter(item["framework"] or "unknown" for item in findings)),
        "truncated": len(findings) > 500,
    }


async def checkov_run_scan(
    path: str | Path,
    *,
    framework: str = "",
    compact: bool = True,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    plan = checkov_plan_scan(path, framework=framework, compact=compact)
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        "localhost",
        [],
        "low",
        float(max(30, min(int(timeout_seconds or 300), 1800))),
    ))
    result["plan"] = plan
    result["parsed"] = parse_checkov_json(str(result.get("stdout_excerpt") or ""))
    return result


trivy_config_plan = trivy_iac_plan
trivy_config_scan = trivy_iac_scan
checkov_scan_plan = checkov_plan_scan
