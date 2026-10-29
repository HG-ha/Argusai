"""``argusai team`` — one-shot standing-team commands.

Interactive use is ``argusai chat``: ``/team use <name>``, then keep typing.
The desktop app and this process must not drive the same team at once.
``send`` hosts the members in this process until they finish (Ctrl+C stops them).
"""

from __future__ import annotations

import time
from typing import Any, Optional

import typer
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="team",
    help="Manage standing Agent teams (list, chat, command, stop).",
    rich_markup_mode="rich",
)


def _teams(api: Any) -> list[dict[str, Any]]:
    result = api.list_teams()
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(str((result or {}).get("error") or "list failed"))
    return [row for row in list(result.get("teams") or []) if isinstance(row, dict)]


def _load_team(api: Any, token: str) -> dict[str, Any]:
    token = str(token or "").strip()
    rows = _teams(api)
    exact = [row for row in rows if str(row.get("team_id") or "") == token or str(row.get("name") or "") == token]
    if len(exact) == 1:
        chosen = exact[0]
    elif token.isdigit():
        index = int(token)
        if index < 1 or index > len(rows):
            raise RuntimeError(f"team number {token} is out of range (1-{len(rows) or 0})")
        chosen = rows[index - 1]
    else:
        prefix = [
            row for row in rows
            if str(row.get("team_id") or "").startswith(token)
            or str(row.get("name") or "").lower() == token.lower()
        ]
        if len(prefix) != 1:
            raise RuntimeError(f"team not found: {token}")
        chosen = prefix[0]
    result = api.get_team(str(chosen.get("team_id") or ""))
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(str((result or {}).get("error") or "team not found"))
    team = result.get("team")
    if not isinstance(team, dict):
        raise RuntimeError("team not found")
    return team


def _members(team: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in list(team.get("members") or []) if isinstance(row, dict)]


def _resolve_member(team: dict[str, Any], token: str) -> dict[str, Any]:
    token = str(token or "").strip()
    rows = _members(team)
    exact = [
        row for row in rows
        if str(row.get("agent_id") or "") == token or str(row.get("role") or "") == token
    ]
    if len(exact) == 1:
        return exact[0]
    folded = token.lower()
    fuzzy = [
        row for row in rows
        if str(row.get("agent_id") or "").startswith(token)
        or str(row.get("role") or "").lower() == folded
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    raise RuntimeError(f"member not found: {token}")


def _print_teams(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Teams", show_header=True, header_style="accent", border_style=OPS_BORDER, box=None)
    table.add_column("#", justify="right", style="accent")
    table.add_column("ID", style="meta")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Members", justify="right")
    for index, row in enumerate(rows, start=1):
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        table.add_row(
            str(index),
            str(row.get("team_id") or "")[:16],
            str(row.get("name") or "")[:28],
            str(status.get("overall") or ""),
            str(status.get("member_count") or 0),
        )
    get_console().print(table)


def _print_members(rows: list[dict[str, Any]]) -> None:
    table = Table(title="Members", show_header=True, header_style="accent", border_style=OPS_BORDER, box=None)
    table.add_column("ID", style="meta")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Summary")
    for row in rows:
        table.add_row(
            str(row.get("agent_id") or "")[:18],
            str(row.get("role") or "")[:24],
            str(row.get("status") or ""),
            str(row.get("summary") or row.get("objective") or "")[:42],
        )
    get_console().print(table)


def _visible_chat(messages: list[dict[str, Any]], member_id: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        visibility = str(item.get("visibility") or "group")
        if not member_id:
            if visibility == "private":
                continue
            rows.append(item)
            continue
        if visibility != "private":
            continue
        participants = [str(value) for value in (item.get("participant_agent_ids") or [])]
        if member_id in participants or str(item.get("agent_id") or "") == member_id:
            rows.append(item)
    return rows


def _print_chat(rows: list[dict[str, Any]]) -> None:
    console = get_console()
    if not rows:
        console.print("[hud.dim]No messages.[/]")
        return
    for item in rows:
        who = str(item.get("role_name") or item.get("sender_role") or item.get("sender") or item.get("role") or "")
        text = str(item.get("content") or "").strip().replace("\n", " ")
        if item.get("type") == "activity":
            tools = ", ".join(str(name) for name in (item.get("tool_names") or []))
            console.print(f"[hud.dim]{who}[/]  [meta]{tools}[/]")
            continue
        if len(text) > 240:
            text = text[:239] + "…"
        console.print(f"[accent]{who}[/]  {text}")


@app.command("list")
def list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List standing teams."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        rows = _teams(api)
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    finally:
        api.shutdown_background_services()
    if json_out:
        ok_payload({"teams": rows, "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No teams.[/]")
        return
    _print_teams(rows)


@app.command("create")
def create_cmd(
    name: str = typer.Argument(..., help="Team name."),
    objective: str = typer.Option("", "--objective", "-o", help="What this team is for."),
    preset: str = typer.Option("", "--preset", help="web_audit, recon_sweep, or reverse_triage."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a standing team. Preset members wait for the first command."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.create_team({"name": name, "objective": objective, "preset": preset})
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "create failed"), json_out=json_out)
    team = result.get("team") if isinstance(result.get("team"), dict) else {}
    if json_out:
        ok_payload({"team": team}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]created[/] {team.get('team_id')}  {team.get('name')}")


@app.command("show")
def show_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one team and its members."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
    finally:
        api.shutdown_background_services()
    if json_out:
        ok_payload({"team": loaded}, json_out=True)
        return
    status = loaded.get("status") if isinstance(loaded.get("status"), dict) else {}
    get_console().print(
        f"[accent]{loaded.get('name')}[/]  [meta]{loaded.get('team_id')}[/]  {status.get('overall')}"
    )
    if loaded.get("objective"):
        get_console().print(f"[hud.dim]{loaded.get('objective')}[/]")
    _print_members(_members(loaded))


@app.command("delete")
def delete_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without prompting."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delete a team and its members."""
    set_json_mode(json_out)
    if not yes and not json_out:
        typer.confirm(f"Delete team {team}?", abort=True)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
        result = api.delete_team(str(loaded.get("team_id") or ""))
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    if json_out:
        ok_payload({"team_id": loaded.get("team_id")}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]deleted[/] {loaded.get('team_id')}")


@app.command("add-member")
def add_member_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    role: str = typer.Option(..., "--role", "-r", help="Member role, used for @mentions."),
    objective: str = typer.Option(..., "--objective", "-o", help="This member's assignment."),
    permission: str = typer.Option("readonly", "--permission", help="readonly or scoped."),
    tool: Optional[list[str]] = typer.Option(None, "--tool", help="Tool whitelist when --permission scoped."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Add a member. They wait until the first team command."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
        result = api.add_team_member(str(loaded.get("team_id") or ""), {
            "role": role,
            "objective": objective,
            "permission": permission,
            "requested_tools": list(tool or []),
        })
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "add member failed"), json_out=json_out)
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else {}
    if json_out:
        ok_payload({"agent": agent}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]added[/] {agent.get('agent_id')}  {role}")


@app.command("remove-member")
def remove_member_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    member: str = typer.Argument(..., help="Member id or role."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Remove one member from the team."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
            who = _resolve_member(loaded, member)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
        result = api.delete_team_member(str(loaded.get("team_id") or ""), str(who.get("agent_id") or ""))
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "remove failed"), json_out=json_out)
    if json_out:
        ok_payload({"agent_id": who.get("agent_id")}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]removed[/] {who.get('role')}")


@app.command("chat")
def chat_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    member: str = typer.Option("", "--member", "-m", help="Show the private thread with this member."),
    limit: int = typer.Option(40, "--limit", "-n", help="How many recent messages to print."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print the group room, or one member's private thread."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
            member_id = ""
            if member:
                member_id = str(_resolve_member(loaded, member).get("agent_id") or "")
            result = api.get_team_chat(str(loaded.get("team_id") or ""))
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "chat failed"), json_out=json_out)
    messages = [row for row in list(result.get("messages") or []) if isinstance(row, dict)]
    visible = _visible_chat(messages, member_id)[-max(1, int(limit or 40)):]
    if json_out:
        ok_payload({"messages": visible, "count": len(visible)}, json_out=True)
        return
    _print_chat(visible)


@app.command("send")
def send_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    message: str = typer.Argument(..., help="Command to send."),
    member: str = typer.Option("", "--member", "-m", help="Send only to this member."),
    private: bool = typer.Option(False, "--private", help="Private to --member. Hidden from the group."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Send a command and host the members until they finish. Ctrl+C stops them."""
    set_json_mode(json_out)
    if private and not member:
        fail("--private requires --member", json_out=json_out)
    api = headless_api()
    team_id = ""
    try:
        loaded = _load_team(api, team)
        team_id = str(loaded.get("team_id") or "")
        agent_ids: list[str] = []
        if member:
            agent_ids = [str(_resolve_member(loaded, member).get("agent_id") or "")]
        result = api.send_team_message(
            team_id,
            message,
            agent_ids,
            "private" if private else "group",
        )
        if not isinstance(result, dict) or not result.get("ok"):
            fail(str((result or {}).get("error") or "send failed"), json_out=json_out)
        if json_out:
            ok_payload({
                "delivered": result.get("delivered") or [],
                "team_id": team_id,
            }, json_out=True)
        else:
            get_console().print(
                f"[ok]sent[/] to {len(result.get('delivered') or [])} member(s). "
                "[hud.dim]This process hosts them until they finish. Ctrl+C stops them.[/]"
            )
        while True:
            current = api.get_team(team_id)
            status = {}
            if isinstance(current, dict) and isinstance(current.get("team"), dict):
                raw = current["team"].get("status")
                status = raw if isinstance(raw, dict) else {}
            if int(status.get("active") or 0) <= 0:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        if team_id:
            api.stop_team_members(team_id)
        if not json_out:
            get_console().print("[muted]Stopped team members.[/]")
    except typer.Exit:
        raise
    except RuntimeError as exc:
        fail(str(exc), json_out=json_out)
    finally:
        api.shutdown_background_services()


@app.command("stop")
def stop_cmd(
    team: str = typer.Argument(..., help="Team id or name."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Stop every member hosted by this process and drop queued replies."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
        result = api.stop_team_members(str(loaded.get("team_id") or ""))
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "stop failed"), json_out=json_out)
    if json_out:
        ok_payload({"stopped": result.get("stopped") or [], "count": result.get("stopped_count") or 0}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]stopped[/] {result.get('stopped_count') or 0}")


@app.command("greenlight")
def greenlight_cmd(
    action: str = typer.Argument(..., help="on, off, or status."),
    team: str = typer.Argument(..., help="Team id or name."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Turn the team authorization shield on or off. Tool confirms stay."""
    from argusai.cli.locale import label

    set_json_mode(json_out)
    wanted = action.strip().lower()
    if wanted not in {"on", "off", "status"}:
        fail("action must be on, off, or status", json_out=json_out)
    api = headless_api()
    try:
        try:
            loaded = _load_team(api, team)
        except RuntimeError as exc:
            fail(str(exc), json_out=json_out)
        team_id = str(loaded.get("team_id") or "")
        if wanted == "status":
            enabled = bool(loaded.get("greenlight"))
        else:
            result = api.set_team_greenlight(team_id, wanted == "on")
            if not isinstance(result, dict) or not result.get("ok"):
                fail(str((result or {}).get("error") or "greenlight failed"), json_out=json_out)
            enabled = bool(result.get("greenlight"))
    finally:
        api.shutdown_background_services()
    if json_out:
        ok_payload({"team_id": team_id, "greenlight": enabled}, json_out=True)
        return
    key = "team_room_greenlight_on" if enabled else "team_room_greenlight_off"
    ok_payload({}, json_out=False, human=f"[ok]{label(key)}[/]")
