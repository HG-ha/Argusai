"""Shared Rich console, theme, and banner helpers for the ArgusAI CLI.

Visual language: night-ops console — charcoal chrome, amber signal,
steel meta. Green is reserved for success, not the whole UI.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# Night ops — amber brand, readable steel text, sage only for "ok".
# These foregrounds intentionally target a near-black terminal background:
# primary copy stays near-white while secondary copy remains above the WCAG
# 4.5:1 contrast floor instead of disappearing into Windows Terminal.
OPS_TEXT = "#e1e5ed"
OPS_HUD = "#cdd3de"
OPS_SECONDARY = "#aab3c2"
OPS_MUTED = "#9099aa"

ARGUS_THEME = Theme(
    {
        "info": OPS_SECONDARY,
        "warning": "bold #e6a23c",
        "error": "bold #e06c75",
        "success": "bold #6b9e78",
        "muted": OPS_MUTED,
        "accent": "bold #e6a23c",
        "brand": "bold #e6a23c",
        "meta": "#7eb8c9",
        "amber": "bold #e6a23c",
        "ok": "bold #6b9e78",
        "warn": "bold #e6a23c",
        "fail": "bold #e06c75",
        "critical": "bold #e06c75",
        "high": "bold #e06c75",
        "medium": "bold #e6a23c",
        "low": "#7eb8c9",
        "info_sev": OPS_MUTED,
        "hud": OPS_HUD,
        "hud.dim": OPS_MUTED,
        "prompt": "bold #e6a23c",
        "tool": "#d4a574",
        "reply": "bold #7eb8c9",
    }
)

# Shared chrome for panels / borders (not the loud matrix green).
OPS_BORDER = "#4b5361"

_console: Console | None = None
_json_mode = False


def apply_typer_theme() -> None:
    """Tune Typer's Rich help panel colors once at process start."""
    try:
        import typer.rich_utils as ru

        ru.STYLE_OPTIONS_PANEL_BORDER = OPS_BORDER
        ru.STYLE_COMMANDS_PANEL_BORDER = OPS_BORDER
        ru.STYLE_METAVAR = f"italic {OPS_MUTED}"
        ru.STYLE_OPTION_DEFAULT = "dim"
    except Exception:
        pass


def set_json_mode(enabled: bool) -> None:
    global _json_mode
    _json_mode = bool(enabled)


def json_mode() -> bool:
    return _json_mode or not sys.stdout.isatty() and os.environ.get("ARGUSAI_FORCE_RICH") != "1"


def get_console(*, force_terminal: bool | None = None) -> Console:
    global _console
    if _console is None:
        no_color = bool(os.environ.get("NO_COLOR"))
        _console = Console(
            theme=ARGUS_THEME,
            highlight=False,
            force_terminal=force_terminal,
            no_color=no_color,
        )
    return _console


def print_banner(version: str, *, subtitle: str = "") -> None:
    if json_mode():
        return
    from argusai.cli.locale import label

    console = get_console()
    title = Text()
    title.append("ARGUSAI", style="brand")
    title.append(f"  v{version}", style="muted")
    body = Text(subtitle or label("banner_subtitle"), style="muted")
    console.print(
        Panel(
            body,
            title=title,
            border_style=OPS_BORDER,
            box=box.SQUARE,
            padding=(0, 2),
        )
    )


def ops_hud_panel(
    version: str,
    *,
    session_id: str,
    mode: str = "AGENT",
) -> Panel:
    """Build the ops-console header panel (printable or capturable)."""
    host = platform.system().upper()
    now = datetime.now().strftime("%H:%M:%S")

    body = Text()
    body.append(session_id, style="meta")
    body.append("  ·  ", style="hud.dim")
    body.append(host, style="hud.dim")
    body.append("  ·  ", style="hud.dim")
    body.append(mode, style="amber")
    body.append(f"  ·  {now}", style="hud.dim")

    title = Text()
    title.append("ARGUSAI", style="brand")
    title.append("  ops", style="hud")
    title.append(f"  {version}", style="hud.dim")

    return Panel(
        body,
        title=title,
        border_style=OPS_BORDER,
        box=box.SQUARE,
        padding=(0, 2),
        subtitle="[hud.dim]/help · select to copy · PgUp/PgDn[/]",
        subtitle_align="right",
    )


def print_ops_hud(
    version: str,
    *,
    session_id: str,
    mode: str = "AGENT",
    console: Console | None = None,
) -> None:
    """Ops-console header panel for interactive chat."""
    if json_mode():
        return
    cons = console or get_console()
    cons.print(ops_hud_panel(version, session_id=session_id, mode=mode))
    cons.print()


def print_json(payload: Any) -> None:
    get_console().print_json(json.dumps(payload, ensure_ascii=False, default=str))


def emit_result(payload: dict[str, Any], *, human: str | None = None) -> None:
    if json_mode():
        print_json(payload)
        return
    if human:
        get_console().print(human)
    else:
        get_console().print(payload)


def status_table(title: str, rows: list[tuple[str, str, str]]) -> Table:
    """rows: (component, status, detail) where status is ok|warn|fail."""
    table = Table(
        title=title,
        show_header=True,
        header_style="accent",
        border_style=OPS_BORDER,
        box=box.SQUARE,
    )
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    style_map = {"ok": "ok", "warn": "warn", "fail": "fail", "error": "fail"}
    for name, status, detail in rows:
        key = str(status or "").lower()
        label = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "error": "FAIL"}.get(key, status.upper())
        table.add_row(name, f"[{style_map.get(key, 'muted')}]{label}[/]", detail)
    return table


def severity_style(sev: str) -> str:
    return {
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "info": "info_sev",
    }.get(str(sev or "").lower(), "muted")


def ops_rule(label: str = "") -> None:
    console = get_console()
    if label:
        console.print(Rule(label, style="hud.dim", characters="─"))
    else:
        console.print(Rule(style="hud.dim", characters="─"))
