"""``argusai proxy`` — foreground passive MITM proxy and breakpoint control."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import typer
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from argusai.cli.commands._host import fail, ok_payload
from argusai.cli.ui import OPS_BORDER, emit_result, get_console, set_json_mode

app = typer.Typer(
    name="proxy",
    help="Run the local MITM proxy, or control breakpoints on the one already running.",
    rich_markup_mode="rich",
    invoke_without_command=True,
)
break_app = typer.Typer(
    name="break",
    help="Arm, list, forward, or drop interactive request breakpoints.",
    rich_markup_mode="rich",
)
app.add_typer(break_app, name="break")


def _paths():
    from argusai.core.paths import PathManager
    from argusai.core.settings import SettingsManager

    data_root = SettingsManager().get().data_root or None
    paths = PathManager(data_root=data_root)
    paths.ensure_base_dirs()
    return paths


def _control_info() -> dict[str, Any]:
    path = _paths().proxy_dir / "control.json"
    if not path.is_file():
        raise RuntimeError("proxy is not running (no breakpoint control file)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read proxy control file: {exc}") from exc
    if not isinstance(data, dict) or not data.get("port") or not data.get("token"):
        raise RuntimeError("proxy control file is incomplete")
    return data


def _control_call(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    info = _control_info()
    url = f"http://{info.get('host') or '127.0.0.1'}:{info['port']}{path}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "X-Argus-Token": str(info["token"]),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": detail or str(exc)}
        if isinstance(parsed, dict):
            return parsed
        return {"ok": False, "error": str(exc)}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"proxy control is not reachable: {exc.reason}") from exc
    data = json.loads(raw or "{}")
    return data if isinstance(data, dict) else {"ok": False, "error": "bad control response"}


def _run_proxy(host: str, port: int, system: bool, json_out: bool) -> None:
    from argusai.core.security.proxy import ProxyManager
    from argusai.core.security.proxy_flow import ProxyConfig

    paths = _paths()
    mgr = ProxyManager(ca_dir=paths.proxy_dir)
    config = ProxyConfig(listen_host=host, listen_port=port)
    started = mgr.start_proxy(config, use_system_proxy=system)
    console = get_console()
    if not started.get("ok"):
        emit_result({"ok": False, "error": started.get("error") or "proxy failed"})
        raise typer.Exit(1)

    control = started.get("control") if isinstance(started.get("control"), dict) else {}
    if json_out:
        emit_result({"ok": True, **{k: v for k, v in started.items() if k != "ok"}})
    else:
        control_line = ""
        if control.get("port"):
            control_line = f"\n[muted]breakpoints[/] 127.0.0.1:{control.get('port')}"
        console.print(
            Panel(
                f"[accent]{started.get('host')}:{started.get('port')}[/]\n"
                f"[muted]CA[/] {paths.proxy_dir}\n"
                f"[muted]system proxy[/] {'on' if system else 'off'}"
                f"{control_line}",
                title="[brand]proxy[/]",
                border_style="cyan",
            )
        )
        console.print("[muted]Ctrl+C to stop. Another terminal: argusai proxy break list[/]")

    try:
        with Live(
            Text("proxy · listening", style="accent"),
            console=console,
            refresh_per_second=2,
            transient=True,
        ) as live:
            ticks = 0
            while mgr.is_running:
                time.sleep(0.5)
                ticks += 1
                if not json_out and console.is_terminal:
                    flows = 0
                    try:
                        traffic = mgr.get_traffic(limit=1)
                        flows = int(
                            (traffic or {}).get("total")
                            or len((traffic or {}).get("flows") or [])
                            or 0
                        )
                    except Exception:
                        try:
                            flows = int((mgr.get_status() or {}).get("flow_count") or 0)
                        except Exception:
                            flows = 0
                    live.update(
                        Text(
                            f"proxy · {host}:{port} · up {ticks // 2}s · flows≈{flows}",
                            style="accent",
                        )
                    )
    except KeyboardInterrupt:
        if not json_out:
            console.print("[muted]Stopping proxy…[/]")
    finally:
        mgr.stop_proxy(force=True)


@app.callback(invoke_without_command=True)
def proxy_main(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", help="Listen host."),
    port: int = typer.Option(8866, "--port", "-p", help="Listen port."),
    system: bool = typer.Option(
        False,
        "--system",
        help="Also set the OS system proxy to this listener (use carefully).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Print start result as JSON."),
) -> None:
    """Start the passive proxy and keep it in the foreground."""
    if ctx.invoked_subcommand is not None:
        return
    set_json_mode(json_out)
    _run_proxy(host, port, system, json_out)


def _parse_headers(items: list[str] | None) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in items or []:
        if ":" not in raw:
            raise RuntimeError(f"header must be Name: Value, got {raw!r}")
        name, _, value = raw.partition(":")
        rows.append([name.strip(), value.strip()])
    return rows


@break_app.command("on")
def break_on(
    url: str = typer.Option("", "--url", help="Only park URLs containing this text."),
    host: str = typer.Option("", "--host", help="Only park hosts containing this text."),
    method: Optional[list[str]] = typer.Option(None, "--method", help="Only these methods. Repeatable."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Arm breakpoints on the running proxy. Matching requests wait for forward or drop."""
    set_json_mode(json_out)
    try:
        result = _control_call("POST", "/v1/breakpoints/config", {
            "enabled": True,
            "url_contains": url,
            "host_contains": host,
            "methods": [str(item).upper() for item in (method or []) if str(item).strip()],
        })
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    if not result.get("ok"):
        fail(str(result.get("error") or "arm failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload({}, json_out=False, human="[ok]breakpoints on[/]")


@break_app.command("off")
def break_off(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Disarm breakpoints. Parked requests are left for forward or drop."""
    set_json_mode(json_out)
    try:
        result = _control_call("POST", "/v1/breakpoints/config", {"enabled": False})
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    if not result.get("ok"):
        fail(str(result.get("error") or "disarm failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload({}, json_out=False, human="[ok]breakpoints off[/]")


@break_app.command("list")
def break_list(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List requests currently parked at a breakpoint."""
    set_json_mode(json_out)
    try:
        result = _control_call("GET", "/v1/breakpoints")
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    if not result.get("ok"):
        fail(str(result.get("error") or "list failed"), json_out=json_out)
    paused = [row for row in list(result.get("paused") or []) if isinstance(row, dict)]
    if json_out:
        ok_payload({"paused": paused, "config": result.get("config")}, json_out=True)
        return
    if not paused:
        get_console().print("[hud.dim]No parked requests.[/]")
        return
    table = Table(title="Parked requests", show_header=True, header_style="accent", border_style=OPS_BORDER, box=None)
    table.add_column("ID", style="meta")
    table.add_column("Method")
    table.add_column("URL")
    for row in paused:
        table.add_row(str(row.get("id") or ""), str(row.get("method") or ""), str(row.get("url") or "")[:80])
    get_console().print(table)


@break_app.command("forward")
def break_forward(
    pause_id: str = typer.Argument(..., help="Parked request id from `break list`."),
    method: str = typer.Option("", "--method", help="Replacement method."),
    url: str = typer.Option("", "--url", help="Replacement URL."),
    header: Optional[list[str]] = typer.Option(None, "--header", help="Replacement header, 'Name: Value'. Repeatable."),
    body: str = typer.Option("", "--body", help="Replacement body text."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Forward one parked request, optionally edited."""
    set_json_mode(json_out)
    edits: dict[str, Any] = {}
    if method:
        edits["method"] = method
    if url:
        edits["url"] = url
    if body:
        edits["body_text"] = body
    try:
        if header:
            edits["headers"] = _parse_headers(header)
        result = _control_call("POST", "/v1/breakpoints/resolve", {
            "id": pause_id,
            "action": "forward",
            "edits": edits,
        })
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    if not result.get("ok"):
        fail(str(result.get("error") or "forward failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]forwarded[/] {pause_id}")


@break_app.command("drop")
def break_drop(
    pause_id: str = typer.Argument(..., help="Parked request id from `break list`."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Drop one parked request."""
    set_json_mode(json_out)
    try:
        result = _control_call("POST", "/v1/breakpoints/resolve", {"id": pause_id, "action": "drop"})
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    if not result.get("ok"):
        fail(str(result.get("error") or "drop failed"), json_out=json_out)
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]dropped[/] {pause_id}")
