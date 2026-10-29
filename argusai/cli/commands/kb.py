"""``argusai kb`` — browse and manage the knowledge base in the terminal."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from argusai.cli.commands._host import fail, headless_api, ok_payload
from argusai.cli.ui import OPS_BORDER, get_console, set_json_mode

app = typer.Typer(
    name="kb",
    help="Browse and manage the local knowledge base (terminal-friendly).",
    rich_markup_mode="rich",
)


def _tags_cell(row: dict[str, Any]) -> str:
    tags = row.get("tags") or []
    if isinstance(tags, list):
        return ",".join(str(t) for t in tags[:6])
    return str(tags or "")


def _print_entries(rows: list[dict[str, Any]], *, title: str) -> None:
    table = Table(
        title=title,
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=None,
    )
    table.add_column("ID", style="meta", no_wrap=True)
    table.add_column("Title")
    table.add_column("Tags", style="hud.dim")
    table.add_column("Updated", style="hud.dim")
    for row in rows:
        table.add_row(
            str(row.get("id") or "")[:22],
            str(row.get("title") or "")[:48],
            _tags_cell(row)[:28],
            str(row.get("updated_at") or row.get("created_at") or "")[:19],
        )
    get_console().print(table)
    get_console().print(
        "[hud.dim]Show: argusai kb show <id> · Search: argusai kb search <query>[/]"
    )


def _print_entry(entry: dict[str, Any], *, max_chars: int) -> None:
    console = get_console()
    kid = str(entry.get("id") or "")
    title = str(entry.get("title") or kid or "(untitled)")
    meta_bits = [
        f"id={kid}",
        f"confidence={entry.get('confidence') or '-'}",
    ]
    tags = _tags_cell(entry)
    if tags:
        meta_bits.append(f"tags={tags}")
    if entry.get("trashed"):
        meta_bits.append("trashed")
    if entry.get("superseded_by"):
        meta_bits.append(f"superseded_by={entry.get('superseded_by')}")
    updated = str(entry.get("updated_at") or entry.get("created_at") or "")
    if updated:
        meta_bits.append(updated[:19])
    console.print(
        Panel(
            "  ·  ".join(meta_bits),
            title=f"[brand]{title}[/]",
            border_style=OPS_BORDER,
            padding=(0, 1),
        )
    )
    body = str(entry.get("body") or "").strip()
    if not body:
        console.print("[hud.dim](empty body)[/]")
        return
    truncated = False
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n…"
        truncated = True
    console.print(Markdown(body))
    if truncated:
        console.print(
            f"[hud.dim]Truncated to {max_chars} chars. "
            f"Use --max-chars 0 for full body.[/]"
        )


@app.command("list")
def list_cmd(
    include_trashed: bool = typer.Option(False, "--trash", help="Include trashed entries."),
    include_superseded: bool = typer.Option(
        False, "--superseded", help="Include superseded entries."
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows to show."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List knowledge entries (newest first)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.kb_list(
            include_superseded=include_superseded,
            include_trashed=include_trashed,
        )
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "list failed"), json_out=json_out)
    rows = [r for r in list(result.get("entries") or []) if isinstance(r, dict)]
    if limit > 0:
        rows = rows[:limit]
    if json_out:
        ok_payload({"entries": rows, "root": result.get("root"), "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]Knowledge base is empty.[/]")
        return
    _print_entries(rows, title=f"Knowledge · {len(rows)}")


@app.command("search")
def search_cmd(
    query: str = typer.Argument("", help="Keywords (title / tags / triggers)."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Filter by tag."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max matches."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Search the knowledge index (terminal browse)."""
    set_json_mode(json_out)
    if not str(query or "").strip() and not tag:
        fail("provide a query and/or --tag", json_out=json_out)
    api = headless_api()
    try:
        result = api.kb_search(query, list(tag or []), int(limit or 20), False)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "search failed"), json_out=json_out)
    rows = [r for r in list(result.get("entries") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload({"entries": rows, "query": query, "count": len(rows)}, json_out=True)
        return
    if not rows:
        get_console().print("[hud.dim]No matches.[/]")
        return
    _print_entries(rows, title=f"Knowledge search · {len(rows)}")


@app.command("show")
def show_cmd(
    entry_id: str = typer.Argument(..., help="Knowledge entry id."),
    max_chars: int = typer.Option(
        20000,
        "--max-chars",
        help="Truncate body for the terminal (0 = full).",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print one entry with markdown body in the terminal."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.kb_get(entry_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "not found"), json_out=json_out)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    if json_out:
        ok_payload({"entry": entry}, json_out=True)
        return
    _print_entry(entry, max_chars=int(max_chars))


@app.command("add")
def add_cmd(
    title: str = typer.Option(..., "--title", "-T", help="Entry title."),
    body: str = typer.Option("", "--body", "-b", help="Markdown / plain body."),
    file: Optional[Path] = typer.Option(
        None, "--file", "-f", help="Read body from a UTF-8 file."
    ),
    tag: Optional[list[str]] = typer.Option(None, "--tag", "-t", help="Tag (repeatable)."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Create a knowledge entry."""
    set_json_mode(json_out)
    text = body
    if file is not None:
        try:
            text = Path(file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read file: {exc}", json_out=json_out)
    api = headless_api()
    try:
        result = api.kb_save(
            {
                "title": title,
                "body": text,
                "tags": list(tag or []),
                "reason": "cli kb add",
            }
        )
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "save failed"), json_out=json_out)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    kid = str(entry.get("id") or "")
    if json_out:
        ok_payload({"entry": entry}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]saved[/] {kid}  {title}")


@app.command("delete")
def delete_cmd(
    entry_id: str = typer.Argument(..., help="Knowledge entry id."),
    reason: str = typer.Option("cli kb delete", "--reason", help="Trash reason."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Move an entry to trash (soft delete)."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.kb_delete(entry_id, reason)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "delete failed"), json_out=json_out)
    if json_out:
        ok_payload({"deleted": entry_id}, json_out=True)
        return
    ok_payload({}, json_out=False, human=f"[ok]trashed[/] {entry_id}")


@app.command("trash")
def trash_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List trashed knowledge entries."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.kb_list_trash()
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "trash list failed"), json_out=json_out)
    rows = [r for r in list(result.get("entries") or []) if isinstance(r, dict)]
    if json_out:
        ok_payload(
            {
                "entries": rows,
                "count": result.get("count", len(rows)),
                "total_bytes": result.get("total_bytes", 0),
            },
            json_out=True,
        )
        return
    if not rows:
        get_console().print("[hud.dim]Trash is empty.[/]")
        return
    freed = int(result.get("total_bytes") or 0)
    _print_entries(rows, title=f"Knowledge trash · {len(rows)} · {freed} bytes")
    get_console().print(
        "[hud.dim]Restore: argusai kb restore <id> · "
        "Purge: argusai kb purge --yes · argusai kb purge --id <id> --yes[/]"
    )


@app.command("restore")
def restore_cmd(
    entry_id: str = typer.Argument(..., help="Knowledge entry id."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Restore a knowledge entry from trash."""
    set_json_mode(json_out)
    api = headless_api()
    try:
        result = api.kb_restore(entry_id)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "restore failed"), json_out=json_out)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    if json_out:
        ok_payload({"entry": entry}, json_out=True)
        return
    ok_payload(
        {},
        json_out=False,
        human=f"[ok]restored[/] {entry.get('id') or entry_id}",
    )


@app.command("purge")
def purge_cmd(
    entry_id: Optional[list[str]] = typer.Option(
        None, "--id", help="Hard-delete specific trashed id(s) (repeatable)."
    ),
    older_days: Optional[int] = typer.Option(
        None,
        "--older-days",
        help="Hard-delete trashed entries older than N days (omit with --all).",
    ),
    purge_all: bool = typer.Option(
        False,
        "--all",
        help="Hard-delete everything currently in trash.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Permanently delete trashed knowledge entries."""
    set_json_mode(json_out)
    ids = [str(x).strip() for x in (entry_id or []) if str(x).strip()]
    if not ids and not purge_all and older_days is None:
        fail(
            "specify --id, --older-days N, or --all (plus --yes to confirm)",
            json_out=json_out,
        )
    if purge_all and older_days is not None:
        fail("use either --all or --older-days, not both", json_out=json_out)
    if not yes and not json_out:
        fail("refusing to purge without --yes", json_out=json_out)

    api = headless_api()
    try:
        if ids:
            result = api.kb_purge_trash(None, ids)
        elif purge_all:
            result = api.kb_purge_trash(0, None)
        else:
            result = api.kb_purge_trash(int(older_days or 0), None)
    finally:
        api.shutdown_background_services()
    if not isinstance(result, dict) or not result.get("ok"):
        fail(str((result or {}).get("error") or "purge failed"), json_out=json_out)
    removed = list(result.get("removed") or [])
    if json_out:
        ok_payload(result, json_out=True)
        return
    ok_payload(
        {},
        json_out=False,
        human=(
            f"[ok]purged[/] {len(removed)}  "
            f"freed={result.get('freed_bytes', 0)}B  "
            f"kept={result.get('kept', 0)}"
        ),
    )
