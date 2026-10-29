"""``argusai agent`` — manage local native/ACP agent runtimes."""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload, parse_kv_pairs
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="agent",
    help="Manage local Agent runtimes (built-in ACP presets + custom).",
    rich_markup_mode="rich",
)


def _flatten_runtime_row(row: dict[str, Any]) -> dict[str, Any]:
    probe = row.get("probe") if isinstance(row.get("probe"), dict) else {}
    return {
        "id": str(row.get("id") or row.get("runtime_id") or ""),
        "name": str(row.get("name") or ""),
        "backend": str(row.get("backend") or ""),
        "enabled": bool(row.get("enabled", True)),
        "available": bool(probe.get("available", row.get("available"))),
        "command": str(
            probe.get("resolved_command")
            or row.get("command")
            or probe.get("command")
            or ""
        ),
        "preset": bool(row.get("preset")),
        "notes": str(row.get("notes") or ""),
        "probe": probe,
    }


def _print_agents(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="Local Agents",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Backend")
    table.add_column("On")
    table.add_column("Available")
    table.add_column("Command")
    for raw in rows:
        row = _flatten_runtime_row(raw)
        table.add_row(
            row["id"],
            row["name"][:28],
            row["backend"],
            "yes" if row["enabled"] else "no",
            "yes" if row["available"] else "no",
            row["command"][:36],
        )
    get_console().print(table)
    get_console().print(
        "[hud.dim]Bind in ops: /agent · /new <id|name|#> · "
        "Add: argusai agent add --name … --command …[/]"
    )


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured local agents and PATH detection status."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_agent_runtimes()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("runtimes") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload(
            {
                "runtimes": [_flatten_runtime_row(r) for r in rows],
                "available": result.get("available") or [],
                "missing": result.get("missing") or [],
            },
            json_out=True,
        )
        return
    if not rows:
        get_console().print("[hud.dim]No agent runtimes configured.[/]")
        return
    _print_agents(rows)


@app.command("add")
def add_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Display name."),
    command: str = typer.Option(..., "--command", "-c", help="Executable (PATH or absolute)."),
    arg: Optional[list[str]] = typer.Option(
        None,
        "--arg",
        "-a",
        help="Command argument (repeatable), e.g. -a acp",
    ),
    runtime_id: Optional[str] = typer.Option(None, "--id", help="Stable runtime id."),
    env: Optional[list[str]] = typer.Option(
        None,
        "--env",
        "-e",
        help="Environment KEY=VALUE (repeatable).",
    ),
    cwd: str = typer.Option("", "--cwd", help="Working directory."),
    notes: str = typer.Option("", "--notes", help="Optional notes."),
    enable: bool = typer.Option(True, "--enable/--disable", help="Enabled flag."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Add or update a custom ACP agent runtime in settings.json."""
    set_json_mode(json_out)
    try:
        env_map = parse_kv_pairs(env, flag="--env")
    except ValueError as exc:
        fail(str(exc), json_out=json_out)

    payload: dict[str, Any] = {
        "name": name.strip(),
        "backend": "acp",
        "command": command.strip(),
        "args": list(arg or []),
        "env": env_map,
        "cwd": cwd.strip(),
        "enabled": bool(enable),
        "notes": notes.strip(),
        "preset": False,
    }
    if runtime_id:
        payload["id"] = runtime_id.strip().lower()

    api = headless_api()
    try:
        result = api.save_agent_runtime(payload)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "save failed"), json_out=json_out)
    runtime = dict(result.get("runtime") or {})
    rid = str(runtime.get("id") or "")
    if json_out:
        ok_payload({"runtime": runtime}, json_out=True)
        return
    get_console().print(
        f"[ok]saved[/]  [accent]{rid}[/]  {runtime.get('name')}  "
        f"cmd={runtime.get('command')}"
    )
    get_console().print(
        f"[hud.dim]Test: argusai agent test {rid} · "
        f"In ops: /new {rid}[/]"
    )


@app.command("enable")
def enable_cmd(
    runtime_id: str = typer.Argument(..., help="Runtime id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Enable a runtime (including built-in presets)."""
    _set_enabled(runtime_id, True, json_out=json_out)


@app.command("disable")
def disable_cmd(
    runtime_id: str = typer.Argument(..., help="Runtime id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Disable a runtime without deleting it."""
    _set_enabled(runtime_id, False, json_out=json_out)


def _set_enabled(runtime_id: str, enabled: bool, *, json_out: bool) -> None:
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.save_agent_runtime({"id": runtime_id.strip().lower(), "enabled": enabled})
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "update failed"), json_out=json_out)
    verb = "enabled" if enabled else "disabled"
    ok_payload(
        {"runtime": result.get("runtime")},
        json_out=json_out,
        human=f"[ok]{verb}[/]  {runtime_id}",
    )


@app.command("remove")
def remove_cmd(
    runtime_id: str = typer.Argument(..., help="Custom runtime id (not built-in)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Delete a custom runtime. Built-in presets can only be disabled."""
    set_json_mode(json_out)
    if not yes and not json_out:
        if not typer.confirm(f"Delete runtime {runtime_id!r}?", default=False):
            raise typer.Abort()
    api = headless_api()
    try:
        result = api.delete_agent_runtime(runtime_id)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "delete failed"), json_out=json_out)
    ok_payload(
        {"runtime_id": runtime_id},
        json_out=json_out,
        human=f"[ok]removed[/]  {runtime_id}",
    )


@app.command("test")
def test_cmd(
    runtime_id: str = typer.Argument(..., help="Runtime id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """ACP initialize handshake (or native capability check)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.test_agent_runtime(runtime_id)
    finally:
        api.shutdown_background_services()
    if json_out:
        from argusai.cli.ui import emit_result

        emit_result({"ok": bool(result.get("ok")), **{k: v for k, v in result.items() if k != "ok"}})
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if result.get("ok"):
        info = result.get("agent_info") if isinstance(result.get("agent_info"), dict) else {}
        title = info.get("title") or info.get("name") or runtime_id
        get_console().print(
            f"[ok]test ok[/]  {runtime_id}  "
            f"protocol={result.get('protocol_version') or '?'}  agent={title}"
        )
    else:
        fail(str(result.get("error") or "test failed"), json_out=False)
