"""``argusai mcp`` — manage MCP servers in settings.json."""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload, parse_kv_pairs
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="mcp",
    help="Manage MCP servers (stdio / HTTP / SSE).",
    rich_markup_mode="rich",
)


def _probe_status(row: dict[str, Any]) -> str:
    probe = row.get("last_probe") if isinstance(row.get("last_probe"), dict) else {}
    return str(probe.get("status") or ("on" if row.get("enabled") else "off"))


def _print_servers(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="MCP servers",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Transport")
    table.add_column("On")
    table.add_column("Probe")
    table.add_column("Target")
    for row in rows:
        transport = str(row.get("transport") or "stdio")
        target = (
            str(row.get("url") or "")
            if transport != "stdio"
            else " ".join(
                [str(row.get("command") or "")]
                + [str(a) for a in (row.get("args") or [])[:3]]
            )
        )
        table.add_row(
            str(row.get("id") or "")[:24],
            str(row.get("name") or "")[:24],
            transport,
            "yes" if row.get("enabled", True) else "no",
            _probe_status(row)[:10],
            target[:40],
        )
    get_console().print(table)


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured MCP servers and last probe status."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_mcp_servers()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("servers") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload(
            {"servers": rows, "startup_probe": result.get("startup_probe")},
            json_out=True,
        )
        return
    if not rows:
        get_console().print("[hud.dim]No MCP servers configured.[/]")
        return
    _print_servers(rows)


@app.command("add")
def add_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Display name."),
    command: str = typer.Option("", "--command", "-c", help="stdio executable."),
    arg: Optional[list[str]] = typer.Option(
        None, "--arg", "-a", help="stdio argument (repeatable)."
    ),
    url: str = typer.Option("", "--url", help="HTTP/SSE endpoint URL."),
    transport: str = typer.Option(
        "",
        "--transport",
        help="stdio | streamable-http | sse (default: stdio if --command, else sse).",
    ),
    server_id: Optional[str] = typer.Option(None, "--id", help="Stable server id."),
    env: Optional[list[str]] = typer.Option(
        None, "--env", "-e", help="Environment KEY=VALUE (repeatable)."
    ),
    header: Optional[list[str]] = typer.Option(
        None, "--header", "-H", help="HTTP header KEY=VALUE (repeatable)."
    ),
    cwd: str = typer.Option("", "--cwd", help="Working directory (stdio)."),
    notes: str = typer.Option("", "--notes", help="Optional notes."),
    enable: bool = typer.Option(True, "--enable/--disable", help="Enabled flag."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Add or update an MCP server in settings.json."""
    set_json_mode(json_out)
    try:
        env_map = parse_kv_pairs(env, flag="--env")
        headers = parse_kv_pairs(header, flag="--header")
    except ValueError as exc:
        fail(str(exc), json_out=json_out)

    t = str(transport or "").strip().lower()
    if not t:
        t = "stdio" if str(command or "").strip() else "sse"
    payload: dict[str, Any] = {
        "name": name,
        "enabled": bool(enable),
        "transport": t,
        "command": command,
        "args": list(arg or []),
        "env": env_map,
        "cwd": cwd,
        "url": url,
        "headers": headers,
        "notes": notes,
    }
    if server_id:
        payload["id"] = server_id

    api = headless_api()
    try:
        result = api.save_mcp_server(payload)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "save failed"), json_out=json_out)
    server = result.get("server") if isinstance(result.get("server"), dict) else {}
    sid = str(server.get("id") or "")
    if json_out:
        ok_payload({"server": server}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]saved[/] {sid}  {name}")


@app.command("remove")
def remove_cmd(
    server_id: str = typer.Argument(..., help="MCP server id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delete an MCP server from settings."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.delete_mcp_server(server_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    if json_out:
        ok_payload({"deleted": server_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]removed[/] {server_id}")


@app.command("enable")
def enable_cmd(
    server_id: str = typer.Argument(..., help="MCP server id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Enable an MCP server."""
    _set_enabled(server_id, True, json_out=json_out)


@app.command("disable")
def disable_cmd(
    server_id: str = typer.Argument(..., help="MCP server id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Disable an MCP server without deleting it."""
    _set_enabled(server_id, False, json_out=json_out)


def _set_enabled(server_id: str, enabled: bool, *, json_out: bool) -> None:
    set_json_mode(json_out)
    api = headless_api()
    try:
        listed = api.list_mcp_servers()
        rows = [r for r in list((listed or {}).get("servers") or []) if isinstance(r, dict)]
        row = next((r for r in rows if str(r.get("id") or "") == server_id), None)
        if not row:
            fail(f"MCP server not found: {server_id}", json_out=json_out)
        payload = {**row, "enabled": bool(enabled)}
        result = api.save_mcp_server(payload)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "update failed"), json_out=json_out)
    verb = "enabled" if enabled else "disabled"
    if json_out:
        ok_payload({"server": result.get("server"), "enabled": enabled}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]{verb}[/] {server_id}")


@app.command("test")
def test_cmd(
    server_id: str = typer.Argument(..., help="MCP server id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Handshake / health-check one MCP server (blocking)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.test_mcp_server(server_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict):
        fail("test failed", json_out=json_out)
    if json_out:
        ok_payload(result if result.get("ok") else {**result, "ok": False}, json_out=True)
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if not result.get("ok"):
        fail(str(result.get("error") or result.get("status") or "dead"), json_out=False)
    get_console().print(
        f"[ok]alive[/] {server_id}  "
        f"transport={result.get('transport')}  "
        f"{result.get('elapsed_ms')}ms"
    )


@app.command("probe")
def probe_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Kick a background probe of every enabled MCP server."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.probe_mcp_servers()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "probe failed"), json_out=json_out)
    if json_out:
        ok_payload({"started": True}, json_out=True)
        return
    get_console().print(
        "[ok]probe started[/]  poll with [meta]argusai mcp list[/]"
    )
