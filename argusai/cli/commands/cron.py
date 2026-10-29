"""``argusai cron`` — assistant scheduled jobs."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from argusai.cli.commands._host import fail, headless_api, ok_payload
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="cron",
    help="Manage assistant jobs (time, signed webhook, or file change).",
    rich_markup_mode="rich",
)


def _fmt_ts(value: Any) -> str:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(int(ts))


def _sched_cell(job: dict[str, Any]) -> str:
    sched = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    kind = str(sched.get("kind") or "interval")
    if kind == "cron":
        return f"cron {sched.get('expr') or ''}".strip()
    if kind == "webhook":
        return "webhook"
    if kind == "file_change":
        path = str(sched.get("path") or "?")
        return f"watch {path}"
    return f"every {sched.get('every_seconds') or '?'}s"


def _print_jobs(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="Cron jobs",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID", style="meta", no_wrap=True)
    table.add_column("Name")
    table.add_column("On")
    table.add_column("Schedule")
    table.add_column("Next", style="hud.dim")
    table.add_column("Last", style="hud.dim")
    for row in rows:
        table.add_row(
            str(row.get("id") or "")[:18],
            str(row.get("name") or "")[:28],
            "yes" if row.get("enabled", True) else "no",
            _sched_cell(row)[:22],
            _fmt_ts(row.get("next_run_at")),
            str(row.get("last_status") or "-")[:12],
        )
    get_console().print(table)
    get_console().print(
        "[hud.dim]Need a host: argusai serve run · serve install · "
        "or GUI / chat.[/]"
    )


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List assistant cron jobs."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_assistant_cron_jobs()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("jobs") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"jobs": rows, "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No cron jobs.[/]")
        return
    _print_jobs(rows)


@app.command("add")
def add_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Job display name."),
    prompt: str = typer.Option(..., "--prompt", "-p", help="Message sent to the assistant."),
    every: Optional[int] = typer.Option(
        None, "--every", help="Interval seconds (mutually exclusive with --cron)."
    ),
    cron_expr: Optional[str] = typer.Option(
        None, "--cron", help="5-field cron expr, e.g. '0 9 * * *'."
    ),
    webhook: bool = typer.Option(
        False, "--webhook", help="Trigger from the built-in signed webhook."
    ),
    watch_path: str = typer.Option(
        "", "--watch-path", help="Trigger when this file or directory changes."
    ),
    watch_glob: str = typer.Option(
        "*", "--glob", help="File glob used with --watch-path."
    ),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Recurse under watched directories."
    ),
    poll_seconds: int = typer.Option(
        2, "--poll", help="File watcher poll interval in seconds."
    ),
    debounce_seconds: int = typer.Option(
        2, "--debounce", help="Webhook/file trigger debounce seconds."
    ),
    job_id: Optional[str] = typer.Option(None, "--id", help="Stable job id."),
    destination: str = typer.Option(
        "new_session",
        "--destination",
        help="new_session | dedicated_session | session",
    ),
    session: str = typer.Option(
        "",
        "--session",
        help="Target session id when --destination=session.",
    ),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Tag (repeatable)."),
    enable: bool = typer.Option(True, "--enable/--disable", help="Enabled flag."),
    approve_high_risk: bool = typer.Option(
        False,
        "--approve-high-risk",
        help="Explicitly authorize unattended high-risk tools for this job.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create or update a scheduled assistant job."""
    set_json_mode(json_out)
    selected = sum([
        bool(every),
        bool(str(cron_expr or "").strip()),
        bool(webhook),
        bool(str(watch_path or "").strip()),
    ])
    if selected != 1:
        fail(
            "provide exactly one of --every, --cron, --webhook, or --watch-path",
            json_out=json_out,
        )
    if cron_expr:
        schedule: dict[str, Any] = {"kind": "cron", "expr": str(cron_expr).strip()}
    elif webhook:
        schedule = {
            "kind": "webhook",
            "debounce_seconds": max(0, int(debounce_seconds)),
        }
    elif watch_path:
        schedule = {
            "kind": "file_change",
            "path": str(watch_path).strip(),
            "glob": str(watch_glob or "*").strip() or "*",
            "recursive": bool(recursive),
            "poll_seconds": max(1, int(poll_seconds)),
            "debounce_seconds": max(0, int(debounce_seconds)),
        }
    else:
        schedule = {"kind": "interval", "every_seconds": int(every or 3600)}
    payload: dict[str, Any] = {
        "name": name,
        "prompt": prompt,
        "schedule": schedule,
        "destination": destination,
        "session_id": session,
        "tags": list(tag or []),
        "enabled": bool(enable),
        "approval": {
            "mode": "unattended_full",
            "high_risk_ack": bool(approve_high_risk),
            "grant_source": "cli" if approve_high_risk else "",
            "granted_at": time.time() if approve_high_risk else None,
        },
    }
    if job_id:
        payload["id"] = job_id
    api = headless_api()
    try:
        result = api.save_assistant_cron_job(payload)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "save failed"), json_out=json_out)
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    jid = str(job.get("id") or "")
    if json_out:
        ok_payload({"job": job, "webhook": result.get("webhook")}, json_out=True)
        return
    ok_payload(
        {},
        json_out=False,
        human=f"[ok]saved[/] {jid}  next={_fmt_ts(job.get('next_run_at'))}",
    )
    webhook_info = result.get("webhook") if isinstance(result.get("webhook"), dict) else {}
    if webhook_info:
        get_console().print(f"[hud]URL[/] {webhook_info.get('url') or '-'}")
        if webhook_info.get("signing_secret"):
            get_console().print(
                f"[warn]Signing secret (shown once):[/] "
                f"{webhook_info.get('signing_secret')}"
            )


@app.command("enable")
def enable_cmd(
    job_id: str = typer.Argument(..., help="Cron job id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Enable a cron job."""
    _set_enabled(job_id, True, json_out=json_out)


@app.command("disable")
def disable_cmd(
    job_id: str = typer.Argument(..., help="Cron job id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Disable a cron job."""
    _set_enabled(job_id, False, json_out=json_out)


def _set_enabled(job_id: str, enabled: bool, *, json_out: bool) -> None:
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.set_assistant_cron_enabled(job_id, enabled)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "update failed"), json_out=json_out)
    verb = "enabled" if enabled else "disabled"
    if json_out:
        ok_payload({"job": result.get("job")}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]{verb}[/] {job_id}")


@app.command("remove")
def remove_cmd(
    job_id: str = typer.Argument(..., help="Cron job id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delete a cron job."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.delete_assistant_cron_job(job_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    if json_out:
        ok_payload({"deleted": job_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]removed[/] {job_id}")


@app.command("authorize")
def authorize_cmd(
    job_id: str = typer.Argument(..., help="Cron job id."),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke high-risk approval."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the authorization change."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Grant or revoke job-scoped unattended high-risk execution."""
    set_json_mode(json_out)
    if not yes:
        fail(
            "high-risk authorization requires --yes",
            json_out=json_out,
        )
    api = headless_api()
    try:
        result = api.grant_assistant_cron_high_risk(
            job_id,
            not revoke,
            "cli",
            "",
        )
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "authorization failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    state = "granted" if not revoke else "revoked"
    ok_payload({}, json_out=False, human=f"[ok]{state}[/] {job_id}")


@app.command("webhook-secret")
def webhook_secret_cmd(
    job_id: str = typer.Argument(..., help="Webhook-triggered job id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm secret rotation."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Rotate and show a webhook signing secret once."""
    set_json_mode(json_out)
    if not yes:
        fail("secret rotation requires --yes", json_out=json_out)
    api = headless_api()
    try:
        result = api.rotate_assistant_cron_webhook_secret(job_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "secret rotation failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    get_console().print(f"[hud]URL[/] {result.get('url') or '-'}")
    get_console().print(
        f"[warn]Signing secret (shown once):[/] {result.get('signing_secret') or '-'}"
    )


@app.command("runs")
def runs_cmd(
    job_id: str = typer.Argument(..., help="Cron job id."),
    limit: int = typer.Option(20, "--limit", "-n", help="History rows."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show recent run history for a job."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_assistant_cron_runs(job_id, int(limit or 20))
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "runs failed"), json_out=json_out)
    rows = [r for r in list(result.get("runs") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"job_id": job_id, "runs": rows}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No runs yet.[/]")
        return
    table = Table(
        title=f"Cron runs · {job_id}",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("When")
    table.add_column("Status")
    table.add_column("Summary", style="hud.dim")
    for row in rows:
        table.add_row(
            _fmt_ts(row.get("finished_at") or row.get("started_at")),
            str(row.get("status") or "")[:14],
            str(row.get("summary") or row.get("error") or "")[:48],
        )
    get_console().print(table)


def run_cron_serve(*, json_out: bool = False) -> None:
    """Long-running scheduler host — no chat UI and no channel config required."""
    set_json_mode(json_out)
    from rich.live import Live

    from argusai.core.assistant.cron import get_cron_scheduler

    api = headless_api()
    console = get_console()
    try:
        listed = api.list_assistant_cron_jobs()
        jobs = [j for j in list((listed or {}).get("jobs") or []) if isinstance(j, dict)]
        enabled = sum(1 for j in jobs if j.get("enabled", True))
        try:
            api.start_assistant_cron_scheduler()
        except Exception as exc:
            fail(f"scheduler failed to start: {exc}", json_out=json_out)
        sched = get_cron_scheduler()
        active = bool(sched is not None and sched.is_active)
        if json_out:
            ok_payload(
                {
                    "jobs": len(jobs),
                    "enabled": enabled,
                    "scheduler_active": active,
                },
                json_out=True,
            )
        else:
            status = "holding lease" if active else "lease held by another process"
            console.print(
                Panel(
                    Text(
                        f"{enabled}/{len(jobs)} jobs enabled · {status}\n"
                        "No chat / channels required. Ctrl+C to stop.",
                        style="muted",
                    ),
                    title="[brand]cron serve[/]",
                    border_style=OPS_BORDER,
                )
            )
            if not active:
                console.print(
                    "[warn]Another ArgusAI host already runs the scheduler "
                    "(GUI / chat / serve). This process is idle standby.[/]"
                )

        with Live(
            Text("cron · scheduling", style="accent"),
            console=console,
            refresh_per_second=1,
            transient=True,
        ) as live:
            ticks = 0
            while True:
                time.sleep(1)
                ticks += 1
                if not json_out and console.is_terminal:
                    holding = bool(sched is not None and sched.is_active)
                    live.update(
                        Text(
                            f"cron · up {ticks}s · "
                            f"{'active' if holding else 'standby'} · "
                            f"{enabled} enabled",
                            style="accent",
                        )
                    )
    except KeyboardInterrupt:
        if not json_out:
            console.print("[muted]Stopping cron serve…[/]")
    finally:
        api.shutdown_background_services()


@app.command("serve")
def serve_cmd(
    json_out: bool = typer.Option(
        False, "--json", help="Print startup status as JSON then keep running."
    ),
) -> None:
    """Run the cron scheduler in the foreground (prefer ``argusai serve --cron-only``)."""
    run_cron_serve(json_out=json_out)
