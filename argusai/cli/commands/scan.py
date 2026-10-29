"""``argusai scan`` — thin Runner / TaskManager wrapper with Rich progress."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from argusai.cli.ui import emit_result, get_console, severity_style, set_json_mode


def scan(
    url: str = typer.Argument(..., help="Target URL (e.g. https://example.com)."),
    checks: Optional[str] = typer.Option(
        None,
        "--checks",
        "-c",
        help="Comma-separated check ids (default: all registered).",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output directory for findings / reports.",
    ),
    fmt: str = typer.Option(
        "html,md,json",
        "--format",
        "-f",
        help="Comma-separated report formats: html, md, json.",
    ),
    name: Optional[str] = typer.Option(None, "--name", help="Friendly target / run name."),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable summary."),
) -> None:
    """Run built-in security checks against one target."""
    set_json_mode(json_out)
    import argusai.checks  # noqa: F401
    from argusai.core.events import EventBus
    from argusai.core.runner import Runner
    from argusai.core.target import Target
    from argusai.report import render_html, render_json, render_markdown

    check_ids = [c.strip() for c in (checks or "").split(",") if c.strip()] or None
    formats = {p.strip().lower() for p in fmt.split(",") if p.strip()}
    target = Target(url=url, name=name or "")
    work_dir = out or Path("runs")
    bus = EventBus()
    console = get_console()
    progress_msgs: list[str] = []

    def _on_event(event: object) -> None:
        try:
            name_ = str(getattr(event, "type", "") or "")
            data = getattr(event, "payload", None) or {}
            if name_ == "check.start":
                progress_msgs.append(f"check {data.get('check') or data.get('name')}")
            elif name_ == "check.done":
                progress_msgs.append(
                    f"done {data.get('check')} ({data.get('count', 0)} findings)"
                )
            elif name_ == "check.error":
                progress_msgs.append(f"error {data.get('check')}: {data.get('error')}")
        except Exception:
            pass

    bus.subscribe(_on_event)
    runner = Runner(bus=bus, work_dir=work_dir)

    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[accent]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=not json_out,
        disable=json_out or not console.is_terminal,
    ) as progress:
        task_id = progress.add_task(f"Scanning {url}", total=None)

        async def _run():
            return await runner.run(target, checks=check_ids)

        result = asyncio.run(_run())
        progress.update(task_id, description="Scan finished")

    run_dir = Path(result.run_dir) if result.run_dir else work_dir
    report_paths: dict[str, str] = {}
    if "html" in formats:
        report_paths["html"] = str(render_html(result, run_dir / "report.html"))
    if "md" in formats or "markdown" in formats:
        report_paths["md"] = str(render_markdown(result, run_dir / "report.md"))
    if "json" in formats:
        report_paths["json"] = str(render_json(result, run_dir / "report.json"))

    summary = {
        "ok": True,
        "url": url,
        "run_dir": str(run_dir),
        "findings": len(result.findings),
        "by_severity": result.by_severity(),
        "errors": result.errors,
        "reports": report_paths,
        "elapsed": round((result.finished_at or time.time()) - result.started_at, 2),
    }
    if json_out:
        emit_result(summary)
        return

    table = Table(title="Findings", border_style="cyan", header_style="accent")
    table.add_column("Severity", justify="center")
    table.add_column("Title")
    table.add_column("Check")
    for finding in result.findings[:50]:
        sev = getattr(finding.severity, "value", str(finding.severity))
        table.add_row(
            f"[{severity_style(sev)}]{sev}[/]",
            str(finding.title or "")[:80],
            str(finding.check or ""),
        )
    if not result.findings:
        console.print("[success]No findings.[/]")
    else:
        console.print(table)
        if len(result.findings) > 50:
            console.print(f"[muted]… and {len(result.findings) - 50} more[/]")

    paths_txt = "\n".join(f"{k}: {v}" for k, v in report_paths.items()) or "(none)"
    console.print(
        Panel(
            f"[muted]run_dir[/] {run_dir}\n"
            f"[muted]findings[/] {len(result.findings)} · "
            f"[muted]elapsed[/] {summary['elapsed']}s\n"
            f"{paths_txt}",
            title="[brand]scan complete[/]",
            border_style="cyan",
        )
    )
    if result.errors:
        raise typer.Exit(2)
