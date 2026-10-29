"""``argusai channels`` — configure and run communication channels."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from argusai.cli.commands._host import fail, headless_api, ok_payload, parse_kv_pairs
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="channels",
    help="Manage and run communication channels (Telegram, Feishu, …).",
    rich_markup_mode="rich",
)


def _print_channels_table(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="Channels",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID")
    table.add_column("Provider")
    table.add_column("Name")
    table.add_column("On")
    table.add_column("Status")
    table.add_column("Secrets")
    for row in rows:
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        connected = bool(status.get("connected"))
        err = str(status.get("last_error") or "").strip()
        hosted_elsewhere = bool(status.get("hosted_elsewhere")) or (
            "already hosted" in err.lower()
        )
        if connected:
            st = "up"
        elif hosted_elsewhere:
            # Another process (usually ``serve``) owns the host lock — not a
            # channel fault. Show ``ext`` so operators do not chase a false err.
            host_pid = status.get("host_pid")
            st = f"ext:{host_pid}" if host_pid else "ext"
        elif err:
            st = "err"
        else:
            st = "down"
        secrets = row.get("configured_secrets") or {}
        nsec = len(secrets) if isinstance(secrets, dict) else 0
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("provider") or ""),
            str(row.get("name") or "")[:28],
            "yes" if row.get("enabled") else "no",
            st,
            str(nsec),
        )
    get_console().print(table)


@app.command("providers")
def providers_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List supported channel providers and required secrets."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_channel_providers()
    finally:
        api.shutdown_background_services()
    rows = [r for r in list(result.get("providers") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"providers": rows}, json_out=True)
        return
    table = Table(title="Providers", header_style="accent", border_style=OPS_BORDER, box=None)
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Transport")
    table.add_column("Secrets")
    for row in rows:
        secrets = row.get("secrets") or []
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("transport") or ""),
            ", ".join(str(s) for s in secrets),
        )
    get_console().print(table)
    get_console().print(
        "[hud.dim]Add with: argusai channels add <provider> -s KEY=VALUE …[/]"
    )


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List configured channels."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_channels()
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("channels") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"channels": rows}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No channels. See: argusai channels providers[/]")
        return
    _print_channels_table(rows)


@app.command("add")
def add_cmd(
    provider: str = typer.Argument(..., help="Provider id (telegram, feishu, …)."),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        "-n",
        help="Display name (what you see in the UI). Defaults to the provider label.",
    ),
    channel_id: Optional[str] = typer.Option(
        None,
        "--id",
        help="Optional internal id. Omit to auto-generate (recommended).",
    ),
    secret: Optional[list[str]] = typer.Option(
        None,
        "--secret",
        "-s",
        help="Credential KEY=VALUE (repeatable).",
    ),
    setting: Optional[list[str]] = typer.Option(
        None,
        "--setting",
        help="Settings KEY=VALUE (repeatable), e.g. proxy=…",
    ),
    host: Optional[str] = typer.Option(None, "--host", help="Listen host (wecom/webhook)."),
    port: Optional[int] = typer.Option(None, "--port", help="Listen port (wecom/webhook)."),
    path: Optional[str] = typer.Option(None, "--path", help="Callback path (wecom/webhook)."),
    enable: bool = typer.Option(False, "--enable", help="Enable immediately after save."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a channel by name; internal id is auto-generated unless --id is set."""
    set_json_mode(json_out)
    provider_key = provider.strip().lower()
    try:
        secrets = parse_kv_pairs(secret, flag="--secret")
        settings = parse_kv_pairs(setting, flag="--setting")
    except ValueError as exc:
        fail(str(exc), json_out=json_out)

    if host is not None:
        settings["host"] = host
    if port is not None:
        settings["port"] = int(port)
    if path is not None:
        settings["path"] = path

    api = headless_api()
    try:
        meta = api.list_channel_providers()
        known = {
            str(row.get("id") or ""): row
            for row in list(meta.get("providers") or [])
            if isinstance(row, dict)
        }
        if provider_key not in known:
            fail(
                f"unknown provider {provider_key!r}; see: argusai channels providers",
                json_out=json_out,
            )
        if provider_key == "wechat" and secrets.get("bot_token"):
            get_console().print(
                "[warn]WeChat bot_token normally comes from QR login "
                "(argusai channels login-wechat <id>); continuing anyway.[/]"
            )
        display_name = (name or "").strip() or str(
            known[provider_key].get("name") or provider_key
        )
        payload: dict[str, Any] = {
            "provider": provider_key,
            "name": display_name,
            "enabled": bool(enable),
            "transport": known[provider_key].get("transport") or "",
            "settings": settings,
            "secrets": secrets,
        }
        # Only pass id when the user explicitly wants a stable key / update.
        if channel_id:
            payload["id"] = channel_id.strip().lower()
        result = api.save_channel(payload)
    finally:
        api.shutdown_background_services()

    if not result.get("ok"):
        fail(str(result.get("error") or "save failed"), json_out=json_out)
    ch = dict(result.get("channel") or {})
    cid = str(ch.get("id") or "")
    cname = str(ch.get("name") or display_name)
    if json_out:
        ok_payload({"channel": ch}, json_out=True)
        return
    get_console().print(
        f"[ok]saved[/]  [accent]{cname}[/]  "
        f"[hud.dim]id={cid}[/]  ({ch.get('provider')})  "
        f"enabled={'yes' if ch.get('enabled') else 'no'}"
    )
    if provider_key == "wechat":
        get_console().print(
            f"[hud.dim]Next: argusai channels login-wechat {cid}[/]"
        )
    elif not enable:
        get_console().print(
            f"[hud.dim]Enable with: argusai channels enable {cid}[/]"
        )


@app.command("enable")
def enable_cmd(
    channel_id: str = typer.Argument(..., help="Channel id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Enable a channel."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.set_channel_enabled(channel_id, True)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "enable failed"), json_out=json_out)
    ok_payload(
        {"channel": result.get("channel")},
        json_out=json_out,
        human=f"[ok]enabled[/]  {channel_id}",
    )


@app.command("disable")
def disable_cmd(
    channel_id: str = typer.Argument(..., help="Channel id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Disable a channel."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.set_channel_enabled(channel_id, False)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "disable failed"), json_out=json_out)
    ok_payload(
        {"channel": result.get("channel")},
        json_out=json_out,
        human=f"[ok]disabled[/]  {channel_id}",
    )


@app.command("remove")
def remove_cmd(
    channel_id: str = typer.Argument(..., help="Channel id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Delete a channel and its encrypted secrets."""
    set_json_mode(json_out)
    if not yes and not json_out:
        confirm = typer.confirm(f"Delete channel {channel_id!r}?", default=False)
        if not confirm:
            raise typer.Abort()
    api = headless_api()
    try:
        result = api.delete_channel(channel_id)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "delete failed"), json_out=json_out)
    ok_payload(
        {"channel_id": channel_id},
        json_out=json_out,
        human=f"[ok]removed[/]  {channel_id}",
    )


@app.command("test")
def test_cmd(
    channel_id: str = typer.Argument(..., help="Channel id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Probe one channel adapter."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.test_channel(channel_id)
    finally:
        api.shutdown_background_services()
    if json_out:
        emit = {"ok": bool(result.get("ok")), **{k: v for k, v in result.items() if k != "ok"}}
        from argusai.cli.ui import emit_result

        emit_result(emit)
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if result.get("ok"):
        get_console().print(f"[ok]test ok[/]  {channel_id}  {result.get('detail') or ''}")
    else:
        fail(str(result.get("error") or "test failed"), json_out=False)


@app.command("pair")
def pair_cmd(
    channel_id: str = typer.Argument(..., help="Channel id."),
    session_id: str = typer.Option(
        "",
        "--session",
        help="Optional session to bind after pairing (e.g. cli-default).",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Create a pairing code for remote users to bind this channel."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.create_channel_pairing(channel_id, session_id=session_id)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "pair failed"), json_out=json_out)
    code = str(result.get("code") or result.get("pair_code") or "")
    expires = result.get("expires_at") or result.get("expires_in")
    if json_out:
        ok_payload(dict(result), json_out=True)
        return
    get_console().print(
        Panel(
            Text.from_markup(
                f"[accent]pair code[/]  [brand]{code}[/]\n"
                f"[hud.dim]channel[/] {channel_id}"
                + (f"\n[hud.dim]session[/] {session_id}" if session_id else "")
                + (f"\n[hud.dim]expires[/] {expires}" if expires else "")
                + "\n\nSend the pair command from a direct message "
                "(usually `/pair <code>`). Group chats are ignored."
            ),
            title="[brand]pairing[/]",
            border_style=OPS_BORDER,
        )
    )


_IDENTITY_PERM_CHOICES = (
    "chat",
    "status",
    "cancel",
    "confirm",
    "configure",
    "send",
)


def _resolve_identity_id(api: Any, selector: str) -> str:
    """Accept a full identity id or a unique prefix from ``channels identities``."""
    needle = str(selector or "").strip()
    if not needle:
        raise ValueError("identity id required")
    result = api.list_channel_identities("")
    rows = [r for r in list(result.get("identities") or []) if isinstance(r, dict)]
    exact = [str(r.get("id") or "") for r in rows if str(r.get("id") or "") == needle]
    if exact:
        return exact[0]
    prefix = [
        str(r.get("id") or "")
        for r in rows
        if str(r.get("id") or "").startswith(needle)
    ]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise ValueError(f"ambiguous identity prefix {needle!r}; use a longer id")
    raise ValueError(f"identity not found: {needle}")


@app.command("identities")
def identities_cmd(
    channel_id: str = typer.Argument("", help="Optional channel id filter."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List paired identities (and bound sessions)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.list_channel_identities(channel_id)
    finally:
        api.shutdown_background_services()
    rows = [r for r in list(result.get("identities") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"identities": rows}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No paired identities.[/]")
        return
    table = Table(title="Identities", header_style="accent", border_style=OPS_BORDER, box=None)
    table.add_column("ID")
    table.add_column("Channel")
    table.add_column("User")
    table.add_column("Session")
    table.add_column("Permissions")
    for row in rows:
        perms = row.get("permissions") or []
        if isinstance(perms, (list, tuple)):
            perm_text = ",".join(str(p) for p in perms) or "—"
        else:
            perm_text = str(perms) or "—"
        table.add_row(
            str(row.get("id") or "")[:16],
            str(row.get("channel_id") or ""),
            str(row.get("display_name") or row.get("external_user_id") or "")[:24],
            str(row.get("bound_session_id") or "")[:18] or "—",
            perm_text,
        )
    get_console().print(table)


@app.command("bind")
def bind_cmd(
    identity_id: str = typer.Argument(..., help="Identity id."),
    session_id: str = typer.Argument(..., help="Assistant session id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Bind a paired identity to an assistant session."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.bind_channel_identity(identity_id, session_id)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "bind failed"), json_out=json_out)
    ok_payload(
        {"identity": result.get("identity")},
        json_out=json_out,
        human=f"[ok]bound[/]  {identity_id} → {session_id}",
    )


@app.command("grant")
def grant_cmd(
    identity_id: str = typer.Argument(..., help="Identity id (or unique prefix)."),
    permissions: Optional[list[str]] = typer.Argument(
        None,
        help="Permission names: chat status cancel confirm configure send.",
    ),
    all_perms: bool = typer.Option(
        False,
        "--all",
        help="Grant every identity permission.",
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Set permissions for a paired channel identity."""
    set_json_mode(json_out)
    if all_perms:
        wanted = list(_IDENTITY_PERM_CHOICES)
    else:
        wanted = [str(item).strip().lower() for item in (permissions or []) if str(item).strip()]
    if not wanted:
        fail(
            "pass permission names, or --all "
            f"(choices: {', '.join(_IDENTITY_PERM_CHOICES)})",
            json_out=json_out,
        )
    unknown = sorted({name for name in wanted if name not in _IDENTITY_PERM_CHOICES})
    if unknown:
        fail(
            f"unknown permission(s): {', '.join(unknown)}; "
            f"choices: {', '.join(_IDENTITY_PERM_CHOICES)}",
            json_out=json_out,
        )
    api = headless_api()
    try:
        try:
            resolved = _resolve_identity_id(api, identity_id)
        except ValueError as exc:
            fail(str(exc), json_out=json_out)
        result = api.set_channel_identity_permissions(resolved, wanted)
    finally:
        api.shutdown_background_services()
    if not result.get("ok"):
        fail(str(result.get("error") or "grant failed"), json_out=json_out)
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    granted = ",".join(str(p) for p in list(identity.get("permissions") or wanted))
    ok_payload(
        {"identity": identity},
        json_out=json_out,
        human=f"[ok]granted[/]  {resolved} → {granted}",
    )


@app.command("login-wechat")
def login_wechat_cmd(
    channel_id: str = typer.Argument(..., help="Existing wechat channel id."),
    timeout: float = typer.Option(180.0, "--timeout", help="Seconds to wait for scan."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Complete WeChat iLink QR sign-in for a channel."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        started = api.start_wechat_login()
        if not started.get("ok"):
            fail(str(started.get("error") or "QR start failed"), json_out=json_out)
        qrcode = str(started.get("qrcode") or "")
        qrurl = str(started.get("qrcode_url") or started.get("url") or "")
        if not qrcode:
            fail("no qrcode returned", json_out=json_out)
        if not json_out:
            get_console().print(
                Panel(
                    Text.from_markup(
                        f"Scan with WeChat:\n[accent]{qrurl or qrcode}[/]\n\n"
                        f"channel [brand]{channel_id}[/] · waiting…"
                    ),
                    title="[brand]wechat login[/]",
                    border_style=OPS_BORDER,
                )
            )
        deadline = time.monotonic() + max(30.0, float(timeout))
        last_status = ""
        while time.monotonic() < deadline:
            polled = api.poll_wechat_login(qrcode, channel_id=channel_id)
            if not polled.get("ok"):
                fail(str(polled.get("error") or "poll failed"), json_out=json_out)
            status = str(polled.get("status") or "")
            if status == "confirmed":
                if json_out:
                    ok_payload(dict(polled), json_out=True)
                else:
                    get_console().print(f"[ok]WeChat signed in[/]  {channel_id}")
                return
            if status != last_status and not json_out:
                get_console().print(f"[hud.dim]status: {status or 'wait'}[/]")
                last_status = status
            time.sleep(2.0)
        fail("timed out waiting for WeChat scan", json_out=json_out)
    finally:
        api.shutdown_background_services()


def run_channels_serve(*, json_out: bool = False) -> None:
    """Run channel adapters without opening the desktop GUI."""
    set_json_mode(json_out)
    from rich.live import Live

    api = headless_api()
    console = get_console()
    try:
        try:
            api.start_assistant_cron_scheduler()
        except Exception:
            pass
        result = api._channels_runtime().refresh()
        items = list((result or {}).get("channels") or []) if isinstance(result, dict) else []
        hosted = [
            item for item in items
            if isinstance(item, dict) and (
                item.get("hosted_elsewhere")
                or "already hosted" in str(item.get("last_error") or "").lower()
            )
        ]
        if hosted:
            # Fail fast: a second serve must not sit idle while the first hosts.
            detail = str(hosted[0].get("last_error") or "already hosted")
            host_pid = hosted[0].get("host_pid")
            if host_pid and f"pid {host_pid}" not in detail:
                detail = f"{detail} (pid {host_pid})"
            fail(detail, json_out=json_out)
        if json_out:
            console.print_json(json.dumps(result, ensure_ascii=False, default=str))
        else:
            statuses = []
            for item in items:
                if isinstance(item, dict):
                    name = item.get("channel_id") or item.get("id") or item.get("provider") or "?"
                    if item.get("connected"):
                        st = "up"
                    else:
                        st = str(item.get("last_error") or item.get("status") or "down")[:40]
                    statuses.append(f"{name}={st}")
            summary = ", ".join(statuses) if statuses else str(result)[:200]
            console.print(
                Panel(
                    Text(summary or "runtime refreshed", style="muted"),
                    title="[brand]channels serve[/]",
                    border_style="cyan",
                )
            )
            console.print("[muted]Running headless… Ctrl+C to stop[/]")

        with Live(
            Text("channels · listening", style="accent"),
            console=console,
            refresh_per_second=1,
            transient=True,
        ) as live:
            ticks = 0
            while True:
                time.sleep(1)
                ticks += 1
                if not json_out and console.is_terminal:
                    live.update(Text(f"channels · up {ticks}s", style="accent"))
    except KeyboardInterrupt:
        if not json_out:
            console.print("[muted]Stopping channels…[/]")
    finally:
        api.shutdown_background_services()


@app.command("serve")
def serve(
    json_out: bool = typer.Option(False, "--json", help="Print startup status as JSON then keep running."),
) -> None:
    """Run channel adapters in the foreground (prefer ``argusai serve``)."""
    run_channels_serve(json_out=json_out)
