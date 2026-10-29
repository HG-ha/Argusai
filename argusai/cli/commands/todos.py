"""``argusai todos`` — user-level persistent todos (cross-session)."""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="todos",
    help="Manage user-level todos (cross-session, not assessment plan).",
    rich_markup_mode="rich",
)


def _print_todos(rows: list[dict[str, Any]]) -> None:
    table = Table(
        title="Todos",
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID", style="meta", no_wrap=True)
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Due", style="hud.dim")
    table.add_column("Tags", style="hud.dim")
    for row in rows:
        tags = row.get("tags") or []
        tag_s = ",".join(str(t) for t in tags[:4]) if isinstance(tags, list) else ""
        table.add_row(
            str(row.get("id") or "")[:18],
            str(row.get("status") or "")[:12],
            str(row.get("title") or "")[:40],
            str(row.get("due_at") or "")[:19],
            tag_s[:24],
        )
    get_console().print(table)


@app.command("list")
def list_cmd(
    status: str = typer.Option("", "--status", "-s", help="Filter: pending/in_progress/completed/cancelled."),
    tag: str = typer.Option("", "--tag", "-t", help="Filter by tag."),
    limit: int = typer.Option(100, "--limit", "-n", help="Max rows."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List user todos."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.user_todo_list(status, tag, int(limit or 100))
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("items") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"items": rows, "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No todos.[/]")
        return
    _print_todos(rows)


@app.command("add")
def add_cmd(
    title: str = typer.Argument(..., help="Todo title."),
    body: str = typer.Option("", "--body", "-b", help="Optional details."),
    due: str = typer.Option("", "--due", help="Due timestamp / date string."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Tag (repeatable)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a todo."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.user_todo_create(
            {
                "title": title,
                "body": body,
                "due_at": due or None,
                "tags": list(tag or []),
                "status": "pending",
            }
        )
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "create failed"), json_out=json_out)
    todo = result.get("todo") if isinstance(result.get("todo"), dict) else {}
    tid = str(todo.get("id") or "")
    if json_out:
        ok_payload({"todo": todo}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]added[/] {tid}  {title}")


@app.command("show")
def show_cmd(
    todo_id: str = typer.Argument(..., help="Todo id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show one todo."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.user_todo_list("", "", 500)
    finally:
        api.shutdown_background_services()
    rows = [r for r in list((result or {}).get("items") or []) if isinstance(r, dict)]
    todo = next((r for r in rows if str(r.get("id") or "") == todo_id), None)
    if not todo:
        fail(f"todo not found: {todo_id}", json_out=json_out)
    if json_out:
        ok_payload({"todo": todo}, json_out=True)
        return
    body = str(todo.get("body") or "").strip()
    lines = [
        f"id={todo.get('id')}",
        f"status={todo.get('status')}",
        f"due={todo.get('due_at') or '-'}",
        f"tags={','.join(todo.get('tags') or []) or '-'}",
    ]
    if body:
        lines.append("")
        lines.append(body)
    get_console().print(
        Panel(
            "\n".join(lines),
            title=f"[brand]{todo.get('title') or todo_id}[/]",
            border_style=OPS_BORDER,
        )
    )


@app.command("done")
def done_cmd(
    todo_id: str = typer.Argument(..., help="Todo id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Mark a todo completed."""
    _set_status(todo_id, "completed", json_out=json_out)


@app.command("status")
def status_cmd(
    todo_id: str = typer.Argument(..., help="Todo id."),
    status: str = typer.Argument(..., help="pending|in_progress|completed|cancelled"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Set todo status."""
    _set_status(todo_id, status, json_out=json_out)


def _set_status(todo_id: str, status: str, *, json_out: bool) -> None:
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.user_todo_update(todo_id, {"status": status})
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "update failed"), json_out=json_out)
    todo = result.get("todo") if isinstance(result.get("todo"), dict) else {}
    if json_out:
        ok_payload({"todo": todo}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]{status}[/] {todo_id}")


@app.command("delete")
def delete_cmd(
    todo_id: str = typer.Argument(..., help="Todo id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delete a todo."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.user_todo_delete(todo_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    if json_out:
        ok_payload({"deleted": todo_id, "removed": result.get("deleted")}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]deleted[/] {todo_id}")
