"""capa and YARA CLI bridges for native binary triage."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from argusai.core.reverse.android_toolchain import _now_iso, _run_cli, _tool_executable


def run_capa_analyze(
    sample_path: Path,
    *,
    format_hint: str = "",
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run ``capa`` against a sample and return JSON summary + preview."""
    exe, info = _tool_executable("capa")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("capa", info=info)
    if not sample_path.is_file():
        return {"ok": False, "error": f"sample not found: {sample_path}"}

    with tempfile.NamedTemporaryFile(suffix=".capa.json", delete=False) as tmp:
        out_path = Path(tmp.name)
    cmd = [exe, str(sample_path), "-j", "-o", str(out_path)]
    fmt = str(format_hint or "").strip().lower()
    if fmt:
        cmd.extend(["-f", fmt])

    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds)
    report: dict[str, Any] = {}
    preview = ""
    if out_path.is_file():
        try:
            raw = out_path.read_text(encoding="utf-8")
            report = json.loads(raw) if raw.strip() else {}
            preview = raw[:8000]
        except (OSError, json.JSONDecodeError) as exc:
            run["ok"] = False
            run["error"] = str(exc)
        finally:
            out_path.unlink(missing_ok=True)

    rules = report.get("rules") if isinstance(report.get("rules"), dict) else {}
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    return {
        **run,
        "tool": "capa",
        "input": str(sample_path.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "rule_count": len(rules),
        "format": meta.get("format") or fmt or "",
        "report_preview": preview,
        "report": report if len(preview) < 4000 else {"meta": meta, "rules": list(rules.keys())[:40]},
    }


def run_yara_scan(
    sample_path: Path,
    rules_path: Path,
    *,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run ``yara`` with a rule file against a sample."""
    exe, info = _tool_executable("yara")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("yara", info=info)
    if not sample_path.is_file():
        return {"ok": False, "error": f"sample not found: {sample_path}"}
    if not rules_path.is_file():
        return {"ok": False, "error": f"rules file not found: {rules_path}"}

    cmd = [exe, "-s", str(rules_path), str(sample_path)]
    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds)
    text = (run.get("stdout") or "") + ("\n" + run.get("stderr") if run.get("stderr") else "")
    matches = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("warning")]
    return {
        **run,
        "tool": "yara",
        "input": str(sample_path.resolve()),
        "rules": str(rules_path.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "match_lines": matches[:200],
        "match_count": len(matches),
    }
