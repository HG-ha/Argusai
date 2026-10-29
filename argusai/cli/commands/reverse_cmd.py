"""``argusai reverse`` — reverse-engineering jobs from the terminal."""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="reverse",
    help="Manage reverse-engineering jobs (list / create / export / toolchain).",
    rich_markup_mode="rich",
)


def _print_jobs(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="Reverse jobs",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID", style="meta", no_wrap=True)
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Scope")
    table.add_column("Target", style="hud.dim")
    for row in rows:
        table.add_row(
            str(row.get("id") or "")[:18],
            str(row.get("title") or "")[:36],
            str(row.get("status") or "")[:12],
            str(row.get("scope") or "")[:10],
            str(row.get("target_url") or row.get("platform") or "")[:36],
        )
    get_console().print(table)


@app.command("list")
def list_cmd(
    scope: str = typer.Option("", "--scope", help="Filter by scope."),
    status: str = typer.Option("", "--status", help="Filter by status."),
    include_trashed: bool = typer.Option(False, "--trash", help="Include trashed jobs."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List reverse jobs."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_job_list(scope, status, include_trashed)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("jobs") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"jobs": rows, "root": result.get("root"), "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No reverse jobs.[/]")
        return
    _print_jobs(rows)


@app.command("create")
def create_cmd(
    title: str = typer.Option(..., "--title", "-T", help="Job title."),
    target_url: str = typer.Option("", "--url", help="Target URL."),
    scope: str = typer.Option("", "--scope", help="Scope label (web/android/…)."),
    intent: str = typer.Option("", "--intent", help="Analysis intent."),
    platform: str = typer.Option("", "--platform", help="Platform hint."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Tag (repeatable)."),
    linked_task: str = typer.Option("", "--task", help="Linked scan task id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a reverse job."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_job_create(
            {
                "title": title,
                "target_url": target_url,
                "scope": scope,
                "intent": intent,
                "platform": platform,
                "tags": list(tag or []),
                "linked_task_id": linked_task,
            }
        )
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "create failed"), json_out=json_out)
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    jid = str(job.get("id") or "")
    if json_out:
        ok_payload({"job": job}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]created[/] {jid}  {title}")


@app.command("show")
def show_cmd(
    job_id: str = typer.Argument(..., help="Reverse job id."),
    include_trashed: bool = typer.Option(False, "--trash", help="Allow trashed jobs."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one reverse job summary in the terminal."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_job_get(job_id, include_trashed)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "not found"), json_out=json_out)
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    if json_out:
        ok_payload({"job": job}, json_out=True)
        return
    lines = [
        f"id={job.get('id')}",
        f"status={job.get('status') or '-'}",
        f"scope={job.get('scope') or '-'}",
        f"url={job.get('target_url') or '-'}",
        f"platform={job.get('platform') or '-'}",
        f"intent={job.get('intent') or '-'}",
        f"artifacts={len(job.get('artifacts') or [])}",
        f"findings={len(job.get('findings') or [])}",
        f"hooks={len(job.get('hooks') or [])}",
    ]
    get_console().print(
        Panel(
            "\n".join(lines),
            title=f"[brand]{job.get('title') or job_id}[/]",
            border_style=OPS_BORDER,
        )
    )


@app.command("trash")
def trash_cmd(
    job_id: str = typer.Argument(..., help="Reverse job id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Move a reverse job to trash."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_job_trash(job_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "trash failed"), json_out=json_out)
    if json_out:
        ok_payload({"id": job_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]trashed[/] {job_id}")


@app.command("restore")
def restore_cmd(
    job_id: str = typer.Argument(..., help="Reverse job id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Restore a reverse job from trash."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_job_restore(job_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "restore failed"), json_out=json_out)
    if json_out:
        ok_payload({"id": job_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]restored[/] {job_id}")


@app.command("purge")
def purge_cmd(
    job_id: str = typer.Argument(..., help="Reverse job id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Permanently delete a reverse job (including from trash)."""
    set_json_mode(json_out)
    if not yes and not json_out:
        fail("refusing to purge without --yes", json_out=json_out)
    api = headless_api()
    try:
        result = api.reverse_job_hard_delete(job_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "purge failed"), json_out=json_out)
    if json_out:
        ok_payload({"id": job_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]purged[/] {job_id}")


@app.command("export")
def export_cmd(
    job_id: str = typer.Argument(..., help="Reverse job id."),
    dest: str = typer.Option("", "--dest", "-d", help="Destination dir (default: store exports)."),
    include_decompiled: bool = typer.Option(
        False, "--decompiled", help="Include decompiled/ (can be large)."
    ),
    folder: bool = typer.Option(
        False, "--folder", help="Export as a folder instead of zip."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Export a job to zip/folder (no GUI file-manager popup)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        from argusai.core.reverse.reverse_job_export import export_reverse_job

        try:
            result = export_reverse_job(
                api.reverse_jobs,
                str(job_id),
                dest_dir=(dest or None),
                include_decompiled=bool(include_decompiled),
                as_zip=not bool(folder),
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            fail(str(exc), json_out=json_out)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok", True):
        fail(str((result or {}).get("error") or "export failed"), json_out=json_out)
    path = str(result.get("path") or "")
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]exported[/] {path}")


@app.command("toolchain")
def toolchain_cmd(
    job_id: str = typer.Option("", "--job", help="Optional job id to record scan on."),
    tool: Optional[list[str]] = typer.Option(
        None, "--tool", help="Limit scan to named tools (repeatable)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Scan PATH / settings for reverse toolchain binaries."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.reverse_toolchain_scan(job_id, list(tool) if tool else None)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict):
        fail("toolchain scan failed", json_out=json_out)
    if result.get("ok") is False:
        fail(str(result.get("error") or "toolchain scan failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    tools = result.get("tools") if isinstance(result.get("tools"), dict) else {}
    table = Table(
        title="Reverse toolchain",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("Tool")
    table.add_column("Available")
    table.add_column("Path", style="hud.dim")
    for name, info in tools.items():
        if not isinstance(info, dict):
            table.add_row(str(name), str(info), "")
            continue
        table.add_row(
            str(name),
            "yes" if info.get("available") or info.get("found") else "no",
            str(info.get("path") or info.get("resolved") or "")[:48],
        )
    if not tools:
        get_console().print(
            f"[hud.dim]available={len(result.get('available') or [])} "
            f"missing={len(result.get('missing') or [])}[/]"
        )
        return
    get_console().print(table)
