"""Rich panels for ops-console command results and interactive choices."""

from __future__ import annotations

from typing import Any, Iterable

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from argusai.cli.locale import get_locale, label
from argusai.cli.ui import OPS_BORDER, OPS_MUTED, get_console


# Card types the CLI already handles through dedicated interactive flows
# (confirm / decision / EXEC line). ``print_ui_card`` skips them so we don't
# double-render the same block once as an inline card and once as a prompt.
_CLI_CARD_HANDLED_ELSEWHERE = frozenset(
    {
        "confirm",
        "tool_confirm",
        "tool_confirmation",
        "batch_confirmation",
        "command_plan",
        "user_decision",
        "user_decision_batch",
        "tool_trace",
        "tool_call",
        "tool_activity",
        "tool_result",
    }
)


_VARIANT_COLOR = {
    "neutral": OPS_BORDER,
    "info": "#7eb8c9",
    "success": "#6b9e78",
    "warning": "#e6a23c",
    "danger": "#e06c75",
    "critical": "#e06c75",
    "high": "#e06c75",
    "medium": "#e6a23c",
    "low": "#7eb8c9",
}


def _variant_color(variant: str | None) -> str:
    return _VARIANT_COLOR.get(str(variant or "").lower(), OPS_BORDER)


def _clip(text: Any, width: int = 240) -> str:
    if text is None:
        return ""
    value = str(text).replace("\r", "")
    if len(value) <= width:
        return value
    return value[: max(0, width - 1)] + "…"


def _summary_bits(pairs: Iterable[tuple[str, Any]]) -> Text:
    """Render ``key: value`` pairs as a compact single-line summary."""
    body = Text()
    first = True
    for key, value in pairs:
        text = _clip(value, 120)
        if not text:
            continue
        if not first:
            body.append("  ·  ", style=OPS_MUTED)
        first = False
        body.append(f"{key} ", style=OPS_MUTED)
        body.append(text, style="bold")
    return body


def print_cmd_panel(
    lines: list[str],
    *,
    title: str | None = None,
    console: Any | None = None,
) -> None:
    cons = console or get_console()
    # Join first so markup spans across lines stay valid; fall back to plain text.
    joined = "\n".join(str(line) for line in lines)
    try:
        body: Any = Text.from_markup(joined)
    except Exception:
        body = Text(joined)
    cons.print(
        Panel(
            body,
            title=title or f"[amber]▸ {label('tag_cmd')}[/]",
            border_style=OPS_BORDER,
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    cons.print()


def print_decision_panel(
    block: dict[str, Any],
    *,
    console: Any | None = None,
) -> list[str]:
    """Render a user_decision card; return option labels in display order."""
    cons = console or get_console()
    question = str(block.get("question") or block.get("title") or "").strip()
    context = str(block.get("context") or "").strip()
    options = [
        str(item).strip()
        for item in list(block.get("options") or [])
        if str(item).strip()
    ]
    if not options:
        options = [label("decision_continue"), label("decision_cancel")]

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("n", style="bold #e6a23c", width=4)
    table.add_column("opt")
    for i, opt in enumerate(options, start=1):
        table.add_row(f"[{i}]", opt)

    parts: list[Any] = []
    if question:
        parts.append(Text(question, style="bold"))
    if context:
        parts.append(Text(context, style=OPS_MUTED))
    parts.append(Text(label("decision_pick_hint"), style="#7eb8c9"))
    parts.append(table)

    cons.print(
        Panel(
            Group(*parts),
            title=f"[amber]▸ {label('tag_choice')}[/]",
            border_style="#e6a23c",
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    cons.print()
    return options


def print_confirm_panel(
    item: dict[str, Any],
    *,
    high_risk: bool = False,
    console: Any | None = None,
) -> None:
    cons = console or get_console()
    name = str(
        item.get("title")
        or item.get("name")
        or item.get("tool")
        or item.get("question")
        or "confirm"
    )
    detail = str(
        item.get("detail")
        or item.get("description")
        or item.get("summary")
        or ""
    ).strip()
    risk = str(item.get("risk") or item.get("risk_level") or "").strip()
    rows = [Text(name, style="bold")]
    if detail:
        rows.append(Text(detail[:400], style=OPS_MUTED))
    if risk:
        rows.append(Text(f"{label('tools_risk', risk=risk)}", style="yellow"))
    if high_risk:
        rows.append(Text(label("auth_high_risk_note"), style="bold bright_red"))

    cons.print(
        Panel(
            Group(*rows),
            title=f"[amber]▸ {label('tag_auth')}[/]",
            border_style="#e6a23c" if not high_risk else "#e06c75",
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    cons.print()


def print_batch_decision_panel(
    block: dict[str, Any],
    *,
    console: Any | None = None,
) -> list[dict[str, Any]]:
    """Render user_decision_batch; return normalized question rows."""
    cons = console or get_console()
    title = str(block.get("title") or label("tag_choice")).strip()
    context = str(block.get("context") or "").strip()
    questions: list[dict[str, Any]] = []
    for raw in list(block.get("questions") or [])[:8]:
        if not isinstance(raw, dict):
            continue
        qid = str(raw.get("id") or raw.get("question") or len(questions) + 1)
        qtext = str(raw.get("question") or "").strip()
        opts = [
            str(item).strip()
            for item in list(raw.get("options") or [])
            if str(item).strip()
        ]
        if not qtext or not opts:
            continue
        questions.append({"id": qid, "question": qtext, "options": opts})

    parts: list[Any] = [Text(title, style="bold")]
    if context:
        parts.append(Text(context, style=OPS_MUTED))
    for qi, q in enumerate(questions, start=1):
        parts.append(Text(f"\n{qi}. {q['question']}", style="bold #e6a23c"))
        for oi, opt in enumerate(q["options"], start=1):
            parts.append(Text(f"   [{oi}] {opt}"))

    cons.print(
        Panel(
            Group(*parts),
            title=f"[amber]▸ {label('tag_choice')}[/]",
            border_style="#e6a23c",
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    cons.print()
    return questions


def _card_title(block: dict[str, Any], fallback: str) -> str:
    return _clip(block.get("title") or fallback, 100).strip() or fallback


def _print_card(
    body: Any,
    *,
    title: str,
    tag_key: str = "tag_reply",
    border: str | None = None,
    console: Any | None = None,
) -> None:
    cons = console or get_console()
    header = f"[amber]▸ {label(tag_key)}[/]  {title}"
    cons.print(
        Panel(
            body,
            title=header,
            border_style=border or OPS_BORDER,
            box=box.SQUARE,
            padding=(0, 1),
        )
    )
    cons.print()


def _render_data_table(block: dict[str, Any]) -> Table:
    columns = [c for c in list(block.get("columns") or []) if isinstance(c, dict)]
    rows = [r for r in list(block.get("rows") or []) if isinstance(r, dict)]
    table = Table(
        show_header=True,
        header_style="bold #e6a23c",
        box=box.SIMPLE_HEAD,
        padding=(0, 1),
        expand=True,
    )
    if not columns:
        table.add_column("value")
        for row in rows:
            cells = row.get("cells") if isinstance(row.get("cells"), dict) else row
            if not isinstance(cells, dict):
                continue
            table.add_row(Text(_clip(next(iter(cells.values()), ""), 220)))
        return table

    for col in columns[:6]:
        table.add_column(_clip(col.get("label") or col.get("key"), 40) or "-")
    for row in rows[:32]:
        cells = row.get("cells") if isinstance(row.get("cells"), dict) else row
        values = [Text(_clip(cells.get(c.get("key")), 160)) for c in columns[:6]]
        table.add_row(*values)
    return table


def _render_structured_card(block: dict[str, Any]) -> Group:
    parts: list[Any] = []
    subtitle = _clip(block.get("subtitle"), 200)
    if subtitle:
        parts.append(Text(subtitle, style=OPS_MUTED))

    for section in list(block.get("sections") or [])[:12]:
        if not isinstance(section, dict):
            continue
        stype = str(section.get("type") or "").strip().lower()
        stitle = _clip(section.get("title"), 100)
        if stitle:
            parts.append(Text(stitle, style="bold #e6a23c"))

        if stype == "text":
            body = _clip(section.get("text") or section.get("body"), 1200)
            if body:
                parts.append(Text(body))
        elif stype in {"kv", "metrics"}:
            table = Table(show_header=False, box=None, padding=(0, 1))
            table.add_column("k", style=OPS_MUTED, no_wrap=True)
            table.add_column("v")
            for item in list(section.get("items") or [])[:12]:
                if not isinstance(item, dict):
                    continue
                key = _clip(item.get("label") or item.get("key"), 60) or "—"
                value = _clip(item.get("value"), 220) or "—"
                note = _clip(item.get("note") or item.get("trend"), 60)
                cell = Text(value)
                if note:
                    cell.append(f"  ({note})", style=OPS_MUTED)
                table.add_row(Text(key), cell)
            parts.append(table)
        elif stype in {"list", "badges"}:
            for item in list(section.get("items") or [])[:24]:
                if isinstance(item, dict):
                    text = _clip(item.get("label") or item.get("text"), 160)
                else:
                    text = _clip(item, 160)
                if text:
                    parts.append(Text(f"  • {text}"))
        elif stype == "steps":
            for index, item in enumerate(list(section.get("items") or [])[:12], start=1):
                if isinstance(item, dict):
                    step_title = _clip(item.get("title") or item.get("label"), 120)
                    body = _clip(item.get("body") or item.get("text"), 320)
                    status = _clip(item.get("status"), 40)
                else:
                    step_title = _clip(item, 200)
                    body = ""
                    status = ""
                head = f"  {index}. {step_title}"
                if status:
                    head = f"{head}  ({status})"
                parts.append(Text(head, style="bold"))
                if body:
                    parts.append(Text(f"     {body}", style=OPS_MUTED))
        elif stype == "actions":
            for action in list(section.get("actions") or section.get("items") or [])[:8]:
                if not isinstance(action, dict):
                    continue
                label_text = _clip(action.get("label"), 60)
                target = _clip(action.get("href") or action.get("value"), 200)
                if label_text or target:
                    parts.append(
                        Text(f"  → {label_text or target}  {target}", style="#7eb8c9")
                    )
        else:
            fallback = _clip(section.get("text") or section.get("body"), 800)
            if fallback:
                parts.append(Text(fallback))

    return Group(*(parts or [Text(_clip(block.get("empty_text") or "—", 120), style=OPS_MUTED)]))


def _render_todo_list(block: dict[str, Any]) -> Group:
    todos = [entry for entry in list(block.get("todos") or []) if isinstance(entry, dict)]
    if not todos:
        return Group(Text(label("ops_todo_empty"), style=OPS_MUTED))
    marks = {"completed": "[x]", "in_progress": "[~]", "blocked": "[!]", "cancelled": "[-]"}
    lines: list[Text] = []

    def _walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for todo in nodes[:64]:
            if not isinstance(todo, dict):
                continue
            status = str(todo.get("status") or "pending").lower()
            marker = marks.get(status, "[ ]")
            title = _clip(todo.get("title") or todo.get("content") or todo.get("id"), 200)
            line = Text("  " * depth + f"{marker} {title}")
            if status == "in_progress":
                line.stylize("bold #e6a23c")
            elif status == "completed":
                line.stylize("#6b9e78")
            elif status in {"blocked", "cancelled"}:
                line.stylize(OPS_MUTED)
            lines.append(line)
            children = todo.get("children")
            if isinstance(children, list):
                _walk(children, depth + 1)

    _walk(todos, 0)
    return Group(*lines)


def _render_findings_summary(block: dict[str, Any]) -> Group:
    parts: list[Any] = []
    counts = block.get("counts") if isinstance(block.get("counts"), dict) else {}
    if counts:
        summary = _summary_bits(
            (level.upper(), counts.get(level, 0))
            for level in ("critical", "high", "medium", "low", "info")
            if counts.get(level)
        )
        if summary.plain.strip():
            parts.append(summary)
    for finding in list(block.get("top_findings") or [])[:6]:
        if not isinstance(finding, dict):
            continue
        sev = str(finding.get("severity") or "").upper() or "-"
        title = _clip(finding.get("title") or finding.get("id"), 160)
        parts.append(Text(f"  {sev:<8} {title}"))
    if not parts:
        parts.append(Text(_clip(block.get("empty_text") or "—", 120), style=OPS_MUTED))
    return Group(*parts)


def _render_scan_plan(block: dict[str, Any]) -> Group:
    args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
    checks = args.get("checks") or block.get("checks") or []
    scope = args.get("scope") or block.get("scope") or []
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style=OPS_MUTED, no_wrap=True)
    table.add_column("v")
    for key, value in (
        ("target", args.get("url") or block.get("target_url") or "-"),
        ("scope", ", ".join(str(s) for s in scope) if scope else "-"),
        ("risk", block.get("risk") or "-"),
        ("checks", ", ".join(str(c) for c in checks) if checks else "-"),
    ):
        table.add_row(Text(key), Text(_clip(value, 200)))
    return Group(table)


def _render_task_progress(block: dict[str, Any]) -> Group:
    task_id = _clip(block.get("task_id"), 60) or "-"
    status = _clip(block.get("status"), 60) or "running"
    percent = block.get("progress")
    try:
        pct_txt = f"{float(percent) * 100:.0f}%" if percent is not None else "-"
    except (TypeError, ValueError):
        pct_txt = str(percent or "-")
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style=OPS_MUTED, no_wrap=True)
    table.add_column("v")
    table.add_row(Text("task"), Text(task_id))
    table.add_row(Text("status"), Text(status))
    table.add_row(Text("progress"), Text(pct_txt))
    return Group(table)


def _render_command_result(block: dict[str, Any]) -> Group:
    command = _clip(block.get("command"), 120) or "-"
    args = " ".join(str(a) for a in list(block.get("args") or []))
    exit_code = block.get("exit_code")
    duration = block.get("duration_ms")
    summary = _summary_bits(
        (
            ("cmd", f"{command} {args}".strip()),
            ("exit", exit_code if exit_code is not None else "-"),
            ("ms", duration if duration is not None else "-"),
        )
    )
    parts: list[Any] = [summary]
    for label_key, key in (("stdout", "stdout_excerpt"), ("stderr", "stderr_excerpt"), ("error", "error")):
        text = _clip(block.get(key), 1200)
        if text:
            parts.append(Rule(label_key, style=OPS_MUTED))
            parts.append(Text(text))
    return Group(*parts)


def _render_sandbox_run(block: dict[str, Any]) -> Group:
    exit_code = block.get("exit_code")
    duration = block.get("duration_ms")
    summary = _summary_bits(
        (
            ("title", block.get("title") or "-"),
            ("exit", exit_code if exit_code is not None else "-"),
            ("ms", duration if duration is not None else "-"),
            ("timed_out", "yes" if block.get("timed_out") else ""),
        )
    )
    parts: list[Any] = [summary]
    for label_key, key in (("stdout", "stdout_excerpt"), ("stderr", "stderr_excerpt"), ("error", "error")):
        text = _clip(block.get(key), 1200)
        if text:
            parts.append(Rule(label_key, style=OPS_MUTED))
            parts.append(Text(text))
    return Group(*parts)


def _render_sandbox_file(block: dict[str, Any]) -> Group:
    summary = _summary_bits(
        (
            ("path", block.get("relative_path") or block.get("directory") or "-"),
            ("size", block.get("size") if block.get("size") is not None else "-"),
            ("status", "failed" if block.get("ok") is False else "ok"),
        )
    )
    parts: list[Any] = [summary]
    preview = _clip(block.get("preview") or block.get("excerpt"), 1200)
    if preview:
        parts.append(Rule("preview", style=OPS_MUTED))
        parts.append(Text(preview))
    return Group(*parts)


def _render_channel_delivery(block: dict[str, Any]) -> Group:
    status = "failed" if block.get("ok") is False else ("queued" if block.get("queued") else "sent")
    file_info = block.get("file") if isinstance(block.get("file"), dict) else {}
    summary = _summary_bits(
        (
            ("provider", block.get("provider") or block.get("channel_id") or "-"),
            ("status", status),
            ("file", file_info.get("name") or file_info.get("relative_path") or ""),
            ("id", block.get("delivery_id") or ""),
        )
    )
    parts: list[Any] = [summary]
    error = _clip(block.get("error"), 400)
    if error:
        parts.append(Text(error, style="#e06c75"))
    return Group(*parts)


def _render_kb_updated(block: dict[str, Any]) -> Group:
    summary = _summary_bits(
        (
            ("action", block.get("action") or "-"),
            ("count", block.get("count") if block.get("count") is not None else "-"),
        )
    )
    parts: list[Any] = [summary]
    for entry in list(block.get("entries") or [])[:8]:
        if not isinstance(entry, dict):
            continue
        parts.append(
            Text(f"  • {_clip(entry.get('title') or entry.get('id'), 200)}")
        )
    return Group(*parts)


def _render_tool_skipped(block: dict[str, Any]) -> Group:
    reason = str(block.get("reason") or "").strip()
    summary = _summary_bits(
        (
            ("tool", block.get("tool_name") or block.get("title") or block.get("name") or "-"),
            ("reason", reason or "-"),
        )
    )
    parts: list[Any] = [summary]
    note = block.get("note") or block.get("detail") or block.get("summary")
    if note:
        parts.append(Text(_clip(note, 400), style=OPS_MUTED))
    return Group(*parts)


def _render_generic(block: dict[str, Any]) -> Group:
    parts: list[Any] = []
    subtitle = _clip(block.get("subtitle") or block.get("description") or block.get("summary"), 240)
    if subtitle:
        parts.append(Text(subtitle, style=OPS_MUTED))
    skip = {"type", "id", "version", "source", "title", "subtitle"}
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style=OPS_MUTED, no_wrap=True)
    table.add_column("v")
    for key, value in block.items():
        if key in skip or key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            text = _clip(str(value), 300)
        else:
            text = _clip(value, 300)
        if not text:
            continue
        table.add_row(Text(str(key)), Text(text))
        if len(table.rows) >= 12:
            break
    if table.rows:
        parts.append(table)
    if not parts:
        parts.append(Text("—", style=OPS_MUTED))
    return Group(*parts)


_CARD_RENDERERS: dict[str, Any] = {
    "data_table": _render_data_table,
    "structured_card": _render_structured_card,
    "todo_list": _render_todo_list,
    "findings_summary": _render_findings_summary,
    "scan_plan": _render_scan_plan,
    "task_progress": _render_task_progress,
    "command_result": _render_command_result,
    "sandbox_run": _render_sandbox_run,
    "sandbox_file": _render_sandbox_file,
    "channel_delivery": _render_channel_delivery,
    "kb_updated": _render_kb_updated,
    "tool_skipped": _render_tool_skipped,
}

_CARD_TITLE_FALLBACKS: dict[str, str] = {
    "data_table": "Data table",
    "structured_card": "Structured card",
    "todo_list": "Todo list",
    "findings_summary": "Findings",
    "scan_plan": "Scan plan",
    "task_progress": "Task progress",
    "command_result": "Command result",
    "sandbox_run": "Sandbox run",
    "sandbox_file": "Sandbox file",
    "channel_delivery": "Channel delivery",
    "kb_updated": "Knowledge base",
    "tool_skipped": "Tool skipped",
}


def print_ui_card(
    block: dict[str, Any],
    *,
    console: Any | None = None,
) -> bool:
    """Render an assistant UI card block into the CLI transcript.

    Returns ``True`` when a card was drawn, ``False`` when the block type is
    handled elsewhere (interactive decision/confirm, tool trace lines) or is
    empty/unknown-and-uninteresting.
    """
    if not isinstance(block, dict):
        return False
    btype = str(block.get("type") or "").strip()
    if not btype or btype in _CLI_CARD_HANDLED_ELSEWHERE:
        return False
    renderer = _CARD_RENDERERS.get(btype)
    if renderer is None:
        # Only surface unknown card types that came from a model-authored /
        # runtime source with a title or subtitle worth reading. Random tool
        # scratch payloads without any human-readable fields are skipped.
        title = str(block.get("title") or "").strip()
        subtitle = str(block.get("subtitle") or block.get("description") or "").strip()
        if not (title or subtitle):
            return False
        body = _render_generic(block)
    else:
        body = renderer(block)

    title = _card_title(block, _CARD_TITLE_FALLBACKS.get(btype, btype))
    _print_card(
        body,
        title=title,
        tag_key="tag_reply",
        border=_variant_color(block.get("variant") or block.get("severity")),
        console=console,
    )
    return True


def locale_tag() -> str:
    return get_locale()
