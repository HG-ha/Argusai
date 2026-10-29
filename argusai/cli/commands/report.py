"""``argusai report`` — regenerate reports from an existing task / run dir."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel

from argusai.cli.ui import emit_result, get_console, set_json_mode


def report(
    task_dir: Optional[Path] = typer.Option(
        None,
        "--task-dir",
        help="Path to a task or scan run directory containing findings.",
    ),
    task_id: Optional[str] = typer.Option(
        None,
        "--task-id",
        help="Task id under the configured tasks_dir.",
    ),
    fmt: str = typer.Option(
        "html,md,json",
        "--format",
        "-f",
        help="Comma-separated formats: html, md, json.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Rebuild HTML / Markdown / JSON reports from saved findings."""
    set_json_mode(json_out)
    root = task_dir
    if root is None and task_id:
        from argusai.core.settings import SettingsManager, default_tasks_dir

        settings = SettingsManager().get()
        base = Path(settings.tasks_dir or default_tasks_dir())
        root = base / task_id
    if root is None:
        raise typer.BadParameter("provide --task-dir or --task-id")
    root = Path(root)
    if not root.is_dir():
        raise typer.BadParameter(f"directory not found: {root}")

    findings_path = root / "findings.jsonl"
    report_json = root / "report.json"
    findings: list[dict] = []
    if findings_path.is_file():
        for line in findings_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                findings.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    elif report_json.is_file():
        data = json.loads(report_json.read_text(encoding="utf-8"))
        findings = list(data.get("findings") or [])
    else:
        raise typer.BadParameter(f"no findings.jsonl or report.json under {root}")

    # Reconstruct a minimal ScanResult for the renderers.
    from argusai.core.finding import Finding
    from argusai.core.runner import ScanResult
    from argusai.core.target import Target
    from argusai.report import render_html, render_json, render_markdown

    meta_path = root / "task.json"
    url = ""
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            url = str(meta.get("target_url") or meta.get("url") or "")
        except Exception:
            url = ""
    target = Target(url=url or "https://example.invalid", name=root.name)
    result = ScanResult(target=target, run_dir=root)
    for raw in findings:
        try:
            item = dict(raw)
            item.setdefault("id", item.get("id") or "cli.imported")
            item.setdefault("category", item.get("category") or "imported")
            item.setdefault("title", item.get("title") or "finding")
            item.setdefault("severity", item.get("severity") or "info")
            allowed = {
                "id", "category", "severity", "title", "url", "description",
                "evidence", "remediation", "cwe", "references", "target", "check",
                "domain", "kind", "confidence", "artifact_refs", "interaction_refs",
                "recommended_tools", "next_actions", "metadata", "uid", "created_at",
            }
            result.findings.append(Finding.from_dict({k: v for k, v in item.items() if k in allowed}))
        except Exception:
            continue

    formats = {p.strip().lower() for p in fmt.split(",") if p.strip()}
    paths: dict[str, str] = {}
    if "html" in formats:
        paths["html"] = str(render_html(result, root / "report.html"))
    if "md" in formats or "markdown" in formats:
        paths["md"] = str(render_markdown(result, root / "report.md"))
    if "json" in formats:
        paths["json"] = str(render_json(result, root / "report.json"))

    payload = {"ok": True, "task_dir": str(root), "findings": len(result.findings), "reports": paths}
    if json_out:
        emit_result(payload)
        return
    get_console().print(
        Panel(
            "\n".join(f"{k}: {v}" for k, v in paths.items()) or "(no formats)",
            title=f"[brand]report[/] · {len(result.findings)} findings",
            border_style="cyan",
        )
    )
