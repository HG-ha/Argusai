"""Provider-neutral channel cards and deterministic platform payloads."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from argusai.core.assistant.ui_protocol import normalize_block

from .i18n import DEFAULT_LOCALE, channel_text
from .markdown import (
    DINGTALK,
    DISCORD,
    FEISHU,
    PLAIN,
    SLACK,
    TELEGRAM_HTML,
    WECOM,
    QQ,
    feishu_markdown_parts,
    markdown_table_parts,
    render_markdown,
    render_markdown_chunks,
)
from .models import ChannelReply

ActionIssuer = Callable[[dict[str, Any]], str]
BatchGroupCreator = Callable[[str, list[dict[str, Any]]], str]


def _text(value: Any, limit: int = 1000) -> str:
    return str(value or "").strip()[:limit]


def _options(value: Any) -> list[str]:
    return list(dict.fromkeys(
        _text(item, 160)
        for item in (value if isinstance(value, list) else [])
        if _text(item, 160)
    ))[:8]


def project_channel_cards(
    blocks: Iterable[Any] | None,
    *,
    issue_action: ActionIssuer,
    create_batch_group: BatchGroupCreator | None = None,
    locale: str = DEFAULT_LOCALE,
) -> tuple[dict[str, Any], ...]:
    """Project assistant UI blocks into the small card contract channels support."""
    cards: list[dict[str, Any]] = []
    todo_card_index: int | None = None
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        block = normalize_block(raw)
        block_type = str(block.get("type") or "")
        if block_type in {"confirm", "command_plan"}:
            card = _tool_card(block, issue_action, locale)
        elif block_type == "user_decision":
            card = _single_decision_card(block, issue_action)
        elif block_type == "user_decision_batch":
            card = _batch_decision_card(block, issue_action, create_batch_group, locale)
        elif block_type == "structured_card":
            card = _structured_card_info(block, issue_action)
        elif block_type == "data_table":
            card = _data_table_info(block, issue_action)
        elif block_type == "file_resource_view":
            card = _file_resource_info(block)
        elif block_type == "todo_list":
            # Streaming turns emit one todo_list per set/update_todo call. The
            # GUI collapses those to a singleton; channels must do the same or
            # the final reply repeats the whole plan three times.
            if not _todo_worth_projecting(block):
                if todo_card_index is not None:
                    cards.pop(todo_card_index)
                    todo_card_index = None
                continue
            card = _todo_card(block, locale)
            if card is None:
                continue
            if todo_card_index is not None:
                cards[todo_card_index] = card
                continue
            todo_card_index = len(cards)
            cards.append(card)
            continue
        else:
            card = None
        if card is not None:
            cards.append(card)
    return tuple(cards)


def _value_action(
    action: dict[str, Any],
    issue_action: ActionIssuer,
) -> dict[str, Any] | None:
    """Turn a model-authored ``value`` action into a channel button."""
    label = _text(action.get("label"), 160)
    value = _text(action.get("value"), 320)
    if not label or not value:
        return None
    return {
        "label": label,
        "token": issue_action({
            "kind": "user_decision",
            "response": value,
        }),
        "style": "default",
    }


def _structured_card_info(
    block: dict[str, Any],
    issue_action: ActionIssuer,
) -> dict[str, Any] | None:
    """Project a GUI structured_card into a channel ``info`` card."""
    title = _text(block.get("title"), 240)
    body = _text(block.get("subtitle"), 1000)
    sections: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for raw in block.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        stype = _text(raw.get("type"), 40).lower()
        stitle = _text(raw.get("title"), 160)
        if stype == "text":
            text = _text(raw.get("text") or raw.get("body"), 1200)
            if text:
                sections.append({"title": stitle, "rows": [text]})
            continue
        if stype in {"kv", "metrics"}:
            rows: list[str] = []
            table_rows: list[dict[str, str]] = []
            for item in raw.get("items") or []:
                if not isinstance(item, dict):
                    continue
                label = _text(item.get("label") or item.get("key"), 80)
                value = _text(item.get("value"), 240)
                if not label and not value:
                    continue
                note = _text(item.get("note") or item.get("trend"), 80)
                line = f"- **{label or '—'}**: {value or '—'}"
                if note:
                    line = f"{line} ({note})"
                rows.append(line)
                shown = value or "—"
                if note:
                    shown = f"{shown} ({note})"
                table_rows.append({"label": label or "—", "value": shown})
            if rows:
                section: dict[str, Any] = {"title": stitle, "rows": rows}
                if table_rows:
                    section["table"] = {
                        "columns": [
                            {"key": "label", "label": stitle or " "},
                            {"key": "value", "label": " "},
                        ],
                        "rows": table_rows,
                    }
                sections.append(section)
            continue
        if stype in {"list", "badges"}:
            rows = []
            for item in raw.get("items") or []:
                if isinstance(item, dict):
                    label = _text(item.get("label") or item.get("text"), 140)
                else:
                    label = _text(item, 140)
                if label:
                    rows.append(f"- {label}")
            if rows:
                sections.append({"title": stitle, "rows": rows})
            continue
        if stype == "steps":
            rows = []
            for index, item in enumerate(raw.get("items") or [], start=1):
                if isinstance(item, dict):
                    step = _text(item.get("title") or item.get("label"), 120) or f"Step {index}"
                    detail = _text(item.get("body") or item.get("text"), 360)
                    status = _text(item.get("status"), 40)
                    line = f"{index}. **{step}**"
                    if detail:
                        line = f"{line} — {detail}"
                    if status:
                        line = f"{line} ({status})"
                    rows.append(line)
                else:
                    text = _text(item, 240)
                    if text:
                        rows.append(f"{index}. {text}")
            if rows:
                sections.append({"title": stitle, "rows": rows})
            continue
        if stype == "actions":
            for action in raw.get("actions") or raw.get("items") or []:
                if not isinstance(action, dict):
                    continue
                projected = _value_action(action, issue_action)
                if projected is not None:
                    actions.append(projected)
    if not title and not body and not sections and not actions:
        return None
    return {
        "type": "info",
        "title": title,
        "body": body,
        "sections": sections,
        "actions": actions,
    }


def _data_table_info(
    block: dict[str, Any],
    issue_action: ActionIssuer,
) -> dict[str, Any] | None:
    """Project a GUI data_table into a compact channel ``info`` card."""
    title = _text(block.get("title"), 240)
    body = _text(block.get("subtitle"), 1000)
    columns = [
        {
            "key": _text(col.get("key"), 64),
            "label": _text(col.get("label") or col.get("key"), 80),
        }
        for col in block.get("columns") or []
        if isinstance(col, dict) and _text(col.get("key"), 64)
    ][:6]
    rows_out: list[str] = []
    table_rows: list[dict[str, str]] = []
    actions: list[dict[str, Any]] = []
    inferred_columns: list[dict[str, str]] = list(columns)
    for raw in list(block.get("rows") or [])[:24]:
        if not isinstance(raw, dict):
            continue
        cells = raw.get("cells") if isinstance(raw.get("cells"), dict) else raw
        parts: list[str] = []
        cell_map: dict[str, str] = {}
        if columns:
            for col in columns:
                value = _text(cells.get(col["key"]) if isinstance(cells, dict) else "", 240)
                cell_map[col["key"]] = value
                if value:
                    parts.append(f"{col['label']}: {value}")
        elif isinstance(cells, dict):
            for key, value in list(cells.items())[:6]:
                text = _text(value, 240)
                if text:
                    parts.append(f"{_text(key, 64)}: {text}")
                    cell_map[_text(key, 64)] = text
            if cell_map and not inferred_columns:
                inferred_columns = [
                    {"key": key, "label": key}
                    for key in cell_map
                ]
        line = " · ".join(parts)
        if line:
            rows_out.append(f"- {line}")
        if cell_map and any(cell_map.values()):
            table_rows.append(cell_map)
        for action in list(raw.get("actions") or [])[:2]:
            if not isinstance(action, dict):
                continue
            projected = _value_action(action, issue_action)
            if projected is not None and len(actions) < 8:
                actions.append(projected)
        single = raw.get("action")
        if isinstance(single, dict) and len(actions) < 8:
            projected = _value_action(single, issue_action)
            if projected is not None:
                actions.append(projected)
    if not rows_out:
        empty = _text(block.get("empty_text"), 120)
        if empty:
            rows_out = [empty]
    if not title and not body and not rows_out and not actions:
        return None
    card: dict[str, Any] = {
        "type": "info",
        "title": title,
        "body": body,
        "sections": [{"title": "", "rows": rows_out}] if rows_out else [],
        "actions": actions,
    }
    if table_rows and inferred_columns:
        card["table"] = {
            "columns": inferred_columns[:6],
            "rows": table_rows,
        }
    return card


def _file_resource_info(block: dict[str, Any]) -> dict[str, Any] | None:
    """Project a GUI file_resource_view into a channel ``info`` card."""
    title = _text(block.get("title"), 240)
    body_parts = [
        part
        for part in (
            _text(block.get("subtitle"), 180),
            _text(block.get("root_label"), 120),
        )
        if part
    ]
    body = " · ".join(body_parts)
    rows: list[str] = []
    for raw in list(block.get("items") or [])[:40]:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"), 120)
        path = _text(raw.get("relative_path") or raw.get("path"), 240)
        kind = _text(raw.get("kind"), 20).lower() or "file"
        icon = "📁" if kind == "dir" else "📄"
        label = name or path
        if not label:
            continue
        if path and path != name:
            rows.append(f"- {icon} {label} (`{path}`)")
        else:
            rows.append(f"- {icon} {label}")
    if not rows:
        empty = _text(block.get("empty_text"), 120)
        if empty:
            rows = [empty]
    if not title and not body and not rows:
        return None
    return {
        "type": "info",
        "title": title,
        "body": body,
        "sections": [{"title": "", "rows": rows}] if rows else [],
        "actions": [],
    }


def _todo_worth_projecting(block: dict[str, Any]) -> bool:
    """Keep live plans; drop fully settled snapshots from chat spam.

    A stale ``plan_pending`` flag must not resurrect an all-completed list
    beside a freshly ``set_todos`` plan in the same channel reply.
    """
    items = list(block.get("flat") or block.get("todos") or [])
    statuses = {
        _todo_status_key(raw.get("status"))
        for raw in items
        if isinstance(raw, dict)
    }
    if not statuses:
        return False
    if statuses.issubset({"completed", "cancelled"}):
        return False
    return True


def _tool_card(
    block: dict[str, Any],
    issue_action: ActionIssuer,
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any] | None:
    payload = dict(block.get("payload") or {})
    tool_name = _text(payload.get("tool_name") or block.get("name"), 160)
    call_id = _text(payload.get("confirm_id") or block.get("confirm_id"), 200)
    pending_id = _text(block.get("pending_id"), 200)
    try:
        version = max(1, int(block.get("pending_version") or block.get("version") or 1))
    except (TypeError, ValueError):
        version = 1
    if not tool_name or not call_id or not pending_id:
        return None
    actions: list[dict[str, Any]] = []
    token = issue_action({
        "kind": "tool_confirmation",
        "pending_id": pending_id,
        "version": version,
        "call_id": call_id,
        "verdict": "allow",
    })
    actions.append({
        "label": channel_text("card_allow_once", locale),
        "token": token,
        "style": "primary",
    })
    deny_token = issue_action({
        "kind": "tool_confirmation",
        "pending_id": pending_id,
        "version": version,
        "call_id": call_id,
        "verdict": "deny",
    })
    actions.append({
        "label": channel_text("card_deny", locale),
        "token": deny_token,
        "style": "danger",
    })
    return {
        "type": "tool_confirm",
        "title": _text(
            block.get("title") or channel_text("card_run_tool", locale, name=tool_name),
            240,
        ),
        "body": _text(block.get("body") or payload.get("description"), 1400),
        "tool_name": tool_name,
        "risk": _text(payload.get("risk") or block.get("risk"), 40),
        "arguments": dict(payload.get("arguments") or block.get("arguments") or {}),
        "requires_local_confirmation": False,
        "actions": actions,
    }


def _single_decision_card(
    block: dict[str, Any],
    issue_action: ActionIssuer,
) -> dict[str, Any] | None:
    question = _text(block.get("question") or block.get("title"), 300)
    options = _options(block.get("options"))
    if not question:
        return None
    actions = [
        {
            "label": option,
            "token": issue_action({
                "kind": "user_decision",
                "response": option,
                "decision_kind": _text(block.get("decision_kind"), 80),
            }),
            "style": "default",
        }
        for option in options
    ]
    return {
        "type": "user_decision",
        "title": question,
        "body": _text(block.get("context"), 1000),
        "input_required": bool(block.get("input_required")),
        "actions": actions,
    }


def _batch_decision_card(
    block: dict[str, Any],
    issue_action: ActionIssuer,
    create_batch_group: BatchGroupCreator | None,
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any] | None:
    title = _text(block.get("title") or channel_text("card_choose_options", locale), 240)
    questions: list[dict[str, Any]] = []
    for index, raw in enumerate(block.get("questions") or []):
        if not isinstance(raw, dict):
            continue
        question_id = _text(raw.get("id") or f"q{index + 1}", 80)
        question = _text(raw.get("question") or raw.get("title"), 300)
        options = _options(raw.get("options"))
        if question_id and question and options:
            questions.append({
                "id": question_id,
                "question": question,
                "options": options,
                "required": bool(raw.get("required", True)),
            })
    if not questions or create_batch_group is None:
        return None
    group_id = create_batch_group(title, questions)
    return project_batch_group_card(
        {
            "id": group_id,
            "title": title,
            "questions": questions,
            "answers": {},
            "context": _text(block.get("context"), 1000),
        },
        issue_action=issue_action,
        locale=locale,
    )


def project_batch_group_card(
    group: dict[str, Any],
    *,
    issue_action: ActionIssuer,
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any]:
    group_id = _text(group.get("id"), 80)
    answers = {
        _text(key, 80): _text(value, 160)
        for key, value in dict(group.get("answers") or {}).items()
    }
    questions = []
    for raw in group.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        question_id = _text(raw.get("id"), 80)
        options = _options(raw.get("options"))
        question_actions = []
        for option in options:
            question_actions.append({
                "label": option,
                "token": issue_action({
                    "kind": "batch_select",
                    "group_id": group_id,
                    "question_id": question_id,
                    "value": option,
                }),
                "style": "primary" if answers.get(question_id) == option else "default",
                "selected": answers.get(question_id) == option,
            })
        questions.append({
            "id": question_id,
            "question": _text(raw.get("question"), 300),
            "required": bool(raw.get("required", True)),
            "selected": answers.get(question_id, ""),
            "actions": question_actions,
        })
    submit = {
        "label": channel_text("card_submit", locale),
        "token": issue_action({"kind": "batch_submit", "group_id": group_id}),
        "style": "primary",
    }
    return {
        "type": "user_decision_batch",
        "title": _text(group.get("title") or channel_text("card_choose_options", locale), 240),
        "body": _text(group.get("context"), 1000),
        "questions": questions,
        "actions": [submit],
    }


def _todo_card(
    block: dict[str, Any],
    locale: str = DEFAULT_LOCALE,
) -> dict[str, Any] | None:
    items = list(block.get("flat") or block.get("todos") or [])
    rows = []
    for raw in items[:24]:
        if not isinstance(raw, dict):
            continue
        title = _text(raw.get("content") or raw.get("title") or raw.get("name"), 240)
        if title:
            rows.append({
                "title": title,
                "status": _text(raw.get("status") or "pending", 40),
            })
    if not rows:
        return None
    done = sum(1 for row in rows if _todo_status_key(row.get("status")) == "completed")
    pending = sum(1 for row in rows if _todo_status_key(row.get("status")) == "pending")
    return {
        "type": "todo_plan",
        "title": channel_text("card_plan", locale),
        "body": channel_text(
            "card_plan_progress",
            locale,
            done=done,
            total=len(rows),
            pending=pending,
        ),
        "items": rows,
        "actions": [],
    }


def _todo_status_key(value: Any) -> str:
    status = _text(value, 40).lower().replace("-", "_").replace(" ", "_")
    if status in {"completed", "done", "complete"}:
        return "completed"
    if status in {"in_progress", "running", "active", "doing"}:
        return "in_progress"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"blocked"}:
        return "blocked"
    return "pending"


def _todo_item_line(item: dict[str, Any], locale: str = DEFAULT_LOCALE) -> str:
    """Pretty one-line todo for channel cards (emoji + title, no raw [pending])."""
    title = _text(item.get("title"), 240)
    kind = _todo_status_key(item.get("status"))
    icon = {
        "completed": "✅",
        "in_progress": "🔵",
        "cancelled": "➖",
        "blocked": "⛔",
        "pending": "⬜",
    }[kind]
    if kind == "completed":
        return f"- {icon} ~~{title}~~"
    if kind == "in_progress":
        return f"- {icon} **{title}**"
    if kind in {"blocked", "cancelled"}:
        # Strikethrough and a red icon read the same across these two, so the
        # word is what tells the user whether the item can still move.
        label = channel_text(f"todo_status_{kind}", locale, inline=True)
        return f"- {icon} {title} ({label})"
    return f"- {icon} {title}"


def _pipe_table(spec: dict[str, Any]) -> str:
    """Turn a structured channel table into CommonMark so every dialect can parse it."""
    columns = [column for column in list(spec.get("columns") or []) if isinstance(column, dict)][:8]
    rows = [row for row in list(spec.get("rows") or []) if isinstance(row, dict)][:50]
    if not columns or not rows:
        return ""
    keys = [_text(column.get("key"), 64) for column in columns]
    headers = [
        _text(column.get("label") or column.get("key"), 40).replace("|", "/")
        for column in columns
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [
            _text(row.get(key), 80).replace("|", "/") or " "
            for key in keys
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def card_text(
    text: str,
    cards: Iterable[dict[str, Any]],
    *,
    include_tokens: bool = False,
    include_actions: bool = True,
    include_titles: bool = True,
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Return a deterministic text representation used by every provider."""
    base = _text(text, 8000)
    if base.startswith("[Waiting for your decision]"):
        base = base.removeprefix("[Waiting for your decision]").strip()
    sections = [base] if base else []
    for card in cards:
        title = _text(card.get("title"), 300)
        # Roomy enough for a whole side-question answer; providers chunk anyway.
        body = _text(card.get("body"), 4000)
        # Emphasis markers may not span lines, so only single-line titles are bold.
        emphasized = card.get("type") in {"info", "todo_plan"} and "\n" not in title
        lines = [f"**{title}**" if emphasized else title] if title else []
        if not include_titles:
            lines = []
        if body and body != title:
            lines.append(body)
        card_table = card.get("table") if isinstance(card.get("table"), dict) else None
        card_pipe = _pipe_table(card_table) if card_table else ""
        if card_pipe:
            lines.append(card_pipe)
        for group in card.get("sections") or []:
            if not isinstance(group, dict):
                continue
            group_title = _text(group.get("title"), 160)
            group_table = group.get("table") if isinstance(group.get("table"), dict) else None
            group_pipe = _pipe_table(group_table) if group_table else ""
            if card_pipe and not group_pipe:
                continue
            rows = [_text(row, 400) for row in group.get("rows") or []]
            rows = [row for row in rows if row]
            if group_pipe:
                rows = [group_pipe]
            if not rows and not group_title:
                continue
            if lines:
                lines.append("")
            if group_title:
                lines.append(f"**{group_title}**")
            lines.extend(rows)
        if card.get("requires_local_confirmation"):
            lines.append(channel_text("card_local_confirmation", locale))
        for item in card.get("items") or []:
            if isinstance(item, dict):
                lines.append(_todo_item_line(item, locale))
        for question in card.get("questions") or []:
            selected = _text(question.get("selected"), 160)
            line = _text(question.get("question"), 300)
            suffix = (
                channel_text("card_selected", locale, value=selected)
                if selected
                else ""
            )
            lines.append(f"{line}{suffix}")
            if include_actions:
                for action in question.get("actions") or []:
                    suffix = f" — /act {_text(action.get('token'), 64)}" if include_tokens else ""
                    lines.append(f"  • {_text(action.get('label'), 160)}{suffix}")
        if include_actions:
            for action in card.get("actions") or []:
                suffix = f" — /act {_text(action.get('token'), 64)}" if include_tokens else ""
                lines.append(f"- {_text(action.get('label'), 160)}{suffix}")
        footer = _text(card.get("footer"), 600)
        if footer:
            if lines:
                lines.append("")
            lines.append(footer)
        if lines:
            sections.append("\n".join(lines))
    # Keep empty replies empty. Callers that want a visible ack must pass text
    # or cards explicitly — never invent "Done." / "完成。" for live/silent turns.
    return "\n\n".join(section for section in sections if section).strip()


def _actions(cards: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card in cards:
        for question in card.get("questions") or []:
            rows.extend(item for item in question.get("actions") or [] if isinstance(item, dict))
        rows.extend(item for item in card.get("actions") or [] if isinstance(item, dict))
    return rows


def _telegram_button_width(value: Any) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in _text(value, 48))


def _truncate_bytes(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


#: How many actions each provider can turn into buttons before the rest are
#: dropped by its own API limits.
_BUTTON_BUDGET = {
    "qq": 10,
    "telegram": 40,
    "discord": 25,
    "slack": 25,
    "feishu": 20,
    "wecom": 6,
}

#: Max characters a provider accepts on a button label. Longer labels are
#: truncated on the button; the full text stays in the card body.
_BUTTON_LABEL_LIMIT = {
    "qq": 30,
    "telegram": 48,
    "discord": 80,
    "slack": 75,
    "feishu": 30,
    "wecom": 18,
}


def _promoted_title(text: str, cards: tuple[dict[str, Any], ...]) -> str:
    """Return the title a provider can render in its own card header."""
    if text.strip() or len(cards) != 1:
        return ""
    # Card headers are single-line plain text on every provider.
    return " ".join(_text(cards[0].get("title"), 100).split())


def _wechat_hard_breaks(text: str) -> str:
    """Space every line so WeChat keeps the breaks the layout depends on.

    An iLink bubble folds a lone newline into a space the way Markdown folds a
    soft wrap, so a card title runs into its body and a bullet list arrives as
    one sentence. Only a blank line survives, and the assistant separates real
    paragraphs that way already, so nothing readable is lost by widening the
    rest to match.
    """
    lines = (line.rstrip() for line in str(text or "").split("\n"))
    return "\n\n".join(line for line in lines if line.strip())


def _button_label(action: dict[str, Any], provider: str) -> str:
    limit = _BUTTON_LABEL_LIMIT.get(provider, 80)
    return _text(action.get("label"), limit)


_FEISHU_TABLE_BUDGET = 5
_FEISHU_MARKDOWN_LIMIT = 4000


def feishu_card_elements(content: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return visual elements from a Feishu 1.0 or 2.0 card payload."""
    if not isinstance(content, dict):
        return []
    body = content.get("body")
    if isinstance(body, dict) and isinstance(body.get("elements"), list):
        source = body.get("elements") or []
    else:
        source = content.get("elements") or []
    return [item for item in source if isinstance(item, dict)]


def feishu_card_has_body(content: dict[str, Any] | None) -> bool:
    """True when the card would show text, a table, or buttons."""
    for item in feishu_card_elements(content):
        tag = str(item.get("tag") or "")
        if tag == "markdown" and str(item.get("content") or "").strip():
            return True
        if tag == "table" and (item.get("rows") or item.get("columns")):
            return True
        if tag in {"action", "button", "column_set"}:
            return True
    return False


def _feishu_col_name(key: str, index: int, used: set[str]) -> str:
    cleaned = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in str(key or "")
    ).strip("_")[:32]
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"c{index}" if not cleaned else f"c{index}_{cleaned}"
    name = cleaned or f"c{index}"
    suffix = 2
    while name in used:
        name = f"{cleaned}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _feishu_table_element(
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    used: set[str] = set()
    mapping: list[tuple[str, str, str]] = []
    for index, column in enumerate(columns[:8]):
        orig = _text(column.get("key"), 64) or f"c{index}"
        name = _feishu_col_name(orig, index, used)
        mapping.append((orig, name, _text(column.get("label") or orig, 40) or f"C{index + 1}"))
    col_defs = [
        {
            "name": name,
            "display_name": label,
            "data_type": "text",
            "width": "auto",
            "vertical_align": "center",
            "horizontal_align": "left",
        }
        for _orig, name, label in mapping
    ]
    table_rows = []
    for raw in rows[:50]:
        if not isinstance(raw, dict):
            continue
        table_rows.append({
            name: _text(raw.get(orig) if orig in raw else raw.get(name), 120)
            for orig, name, _label in mapping
        })
    return {
        "tag": "table",
        "page_size": min(10, max(len(table_rows), 1)),
        "row_height": "low",
        "header_style": {
            "text_align": "left",
            "background_style": "grey",
            "bold": True,
            "lines": 1,
        },
        "columns": col_defs,
        "rows": table_rows,
    }


def _feishu_table_from_markdown_rows(rows: list[list[str]]) -> dict[str, Any]:
    header = list(rows[0]) if rows else []
    width = max((len(row) for row in rows), default=0)
    columns = [
        {
            "key": f"c{index}",
            "label": _text(header[index] if index < len(header) else "", 40) or f"C{index + 1}",
        }
        for index in range(width)
    ]
    body = rows[1:] if len(rows) > 1 else []
    mapped = [
        {
            f"c{index}": _text(row[index] if index < len(row) else "", 120)
            for index in range(width)
        }
        for row in body
    ]
    if not mapped and header:
        mapped = [{f"c{index}": "" for index in range(width)}]
    return _feishu_table_element(columns, mapped)


def _feishu_markdown_element(content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
    }


def _flush_feishu_markdown(elements: list[dict[str, Any]], lines: list[str]) -> None:
    content = "\n\n".join(line for line in lines if str(line).strip()).strip()
    lines.clear()
    if not content:
        return
    remaining = content
    while remaining:
        chunk = remaining[:_FEISHU_MARKDOWN_LIMIT]
        remaining = remaining[_FEISHU_MARKDOWN_LIMIT:]
        elements.append(_feishu_markdown_element(chunk))


def _feishu_append_table(
    elements: list[dict[str, Any]],
    spec: dict[str, Any],
    slots: list[int],
) -> None:
    columns = [
        column
        for column in list(spec.get("columns") or [])
        if isinstance(column, dict)
    ]
    rows = [row for row in list(spec.get("rows") or []) if isinstance(row, dict)]
    if not columns or not rows:
        return
    if slots[0] <= 0:
        preview = [
            " | ".join(_text(row.get(column.get("key")), 40) for column in columns)
            for row in rows[:8]
        ]
        header = " | ".join(_text(column.get("label") or column.get("key"), 40) for column in columns)
        elements.append(_feishu_markdown_element(
            "```\n" + "\n".join([header, *preview]) + "\n```"
        ))
        return
    elements.append(_feishu_table_element(columns, rows))
    slots[0] -= 1


def _feishu_button(action: dict[str, Any]) -> dict[str, Any]:
    token = _text(action.get("token"), 64)
    style = _text(action.get("style"), 20)
    button_type = (
        "danger" if style == "danger" else "primary" if style == "primary" else "default"
    )
    value = {"argusai_action": token}
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": _button_label(action, "feishu")},
        "type": button_type,
        "width": "default",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _feishu_action_elements(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for start in range(0, min(len(actions), 20), 5):
        chunk = actions[start:start + 5]
        elements.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "background_style": "default",
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [_feishu_button(action)],
                }
                for action in chunk
            ],
        })
    return elements


def _feishu_header_template(cards: tuple[dict[str, Any], ...]) -> str:
    if len(cards) != 1:
        return "blue"
    kind = str(cards[0].get("type") or "")
    if kind == "tool_confirm":
        risk = str(cards[0].get("risk") or "").lower()
        if risk in {"high", "danger", "critical"}:
            return "red"
        if risk in {"medium", "warn", "warning"}:
            return "orange"
        return "yellow"
    if kind == "user_decision_batch":
        return "wathet"
    if kind == "todo_plan":
        return "turquoise"
    return "blue"


def _feishu_append_parts(
    elements: list[dict[str, Any]],
    text: str,
    slots: list[int],
) -> None:
    for part in feishu_markdown_parts(text):
        kind = str(part.get("kind") or "")
        if kind == "table":
            rows = [list(row) for row in list(part.get("rows") or [])]
            if not rows:
                continue
            if slots[0] <= 0:
                preview = [" | ".join(_text(cell, 40) for cell in row) for row in rows[:8]]
                elements.append(_feishu_markdown_element("```\n" + "\n".join(preview) + "\n```"))
                continue
            elements.append(_feishu_table_from_markdown_rows(rows))
            slots[0] -= 1
            continue
        content = str(part.get("content") or "").strip()
        if content:
            remaining = content
            while remaining:
                elements.append(_feishu_markdown_element(remaining[:_FEISHU_MARKDOWN_LIMIT]))
                remaining = remaining[_FEISHU_MARKDOWN_LIMIT:]


def _feishu_append_card(
    elements: list[dict[str, Any]],
    card: dict[str, Any],
    *,
    include_title: bool,
    include_actions: bool,
    include_tokens: bool,
    locale: str,
    slots: list[int],
) -> None:
    lines: list[str] = []
    title = _text(card.get("title"), 300)
    if include_title and title:
        lines.append(f"**{title}**" if "\n" not in title else title)
    body = _text(card.get("body"), 4000)
    if body and body != title:
        lines.append(body)
    card_table = card.get("table") if isinstance(card.get("table"), dict) else None
    if card_table:
        _flush_feishu_markdown(elements, lines)
        _feishu_append_table(elements, card_table, slots)
    for group in card.get("sections") or []:
        if not isinstance(group, dict):
            continue
        group_title = _text(group.get("title"), 160)
        group_table = group.get("table") if isinstance(group.get("table"), dict) else None
        if group_table:
            if group_title:
                lines.append(f"**{group_title}**")
            _flush_feishu_markdown(elements, lines)
            _feishu_append_table(elements, group_table, slots)
            continue
        if card_table:
            continue
        rows = [_text(row, 400) for row in group.get("rows") or []]
        rows = [row for row in rows if row]
        if not rows and not group_title:
            continue
        if group_title:
            lines.append(f"**{group_title}**")
        lines.extend(rows)
    if card.get("requires_local_confirmation"):
        lines.append(channel_text("card_local_confirmation", locale))
    for item in card.get("items") or []:
        if isinstance(item, dict):
            lines.append(_todo_item_line(item, locale))
    arguments = card.get("arguments")
    if isinstance(arguments, dict) and arguments:
        arg_rows = [
            {"label": _text(key, 64), "value": _text(value, 120)}
            for key, value in list(arguments.items())[:8]
            if _text(key, 64) or _text(value, 120)
        ]
        if arg_rows:
            _flush_feishu_markdown(elements, lines)
            _feishu_append_table(
                elements,
                {
                    "columns": [
                        {"key": "label", "label": " "},
                        {"key": "value", "label": " "},
                    ],
                    "rows": arg_rows,
                },
                slots,
            )
    for question in card.get("questions") or []:
        selected = _text(question.get("selected"), 160)
        line = _text(question.get("question"), 300)
        suffix = (
            channel_text("card_selected", locale, value=selected)
            if selected
            else ""
        )
        lines.append(f"{line}{suffix}")
        if include_actions:
            for action in question.get("actions") or []:
                suffix = f" — /act {_text(action.get('token'), 64)}" if include_tokens else ""
                lines.append(f"  • {_text(action.get('label'), 160)}{suffix}")
    if include_actions:
        for action in card.get("actions") or []:
            suffix = f" — /act {_text(action.get('token'), 64)}" if include_tokens else ""
            lines.append(f"- {_text(action.get('label'), 160)}{suffix}")
    footer = _text(card.get("footer"), 600)
    if footer:
        lines.append(footer)
    _flush_feishu_markdown(elements, lines)


def _render_feishu_card(
    reply: ChannelReply,
    cards: tuple[dict[str, Any], ...],
    actions: list[dict[str, Any]],
    *,
    promoted_title: str,
    include_actions: bool,
    labels_truncated: bool,
) -> dict[str, Any]:
    locale = getattr(reply, "locale", DEFAULT_LOCALE)
    slots = [_FEISHU_TABLE_BUDGET]
    elements: list[dict[str, Any]] = []
    _feishu_append_parts(elements, str(reply.text or ""), slots)
    overflow_actions = bool(
        include_actions
        and actions
        and (len(actions) > _BUTTON_BUDGET["feishu"] or labels_truncated)
    )
    for index, card in enumerate(cards):
        if elements and index == 0 and (reply.text or "").strip():
            elements.append({"tag": "hr"})
        elif elements and index > 0:
            elements.append({"tag": "hr"})
        _feishu_append_card(
            elements,
            card,
            include_title=not (promoted_title and index == 0),
            include_actions=overflow_actions,
            include_tokens=False,
            locale=locale,
            slots=slots,
        )
    elements.extend(_feishu_action_elements(actions[:20]))
    header_title = promoted_title[:100] or "ArgusAI"
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
        },
        "header": {
            "template": _feishu_header_template(cards),
            "title": {"tag": "plain_text", "content": header_title},
        },
        "body": {"elements": elements},
    }


def _slack_table_block(rows: list[list[str]]) -> dict[str, Any]:
    width = min(max((len(row) for row in rows), default=0), 20)
    slack_rows = []
    for row in rows[:100]:
        slack_rows.append([
            {
                "type": "raw_text",
                "text": _text(row[index] if index < len(row) else "", 200) or " ",
            }
            for index in range(width)
        ])
    return {
        "type": "table",
        "column_settings": [{"is_wrapped": True} for _ in range(width)],
        "rows": slack_rows,
    }


def _render_slack_blocks(
    source: str,
    actions: list[dict[str, Any]],
    *,
    promoted_title: str,
) -> tuple[str, list[dict[str, Any]]]:
    mrkdwn = render_markdown(source, SLACK)
    blocks: list[dict[str, Any]] = []
    if promoted_title:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": promoted_title[:150]},
        })
    for part in markdown_table_parts(source, SLACK):
        kind = str(part.get("kind") or "")
        if kind == "table":
            rows = [list(row) for row in list(part.get("rows") or [])]
            if rows:
                blocks.append(_slack_table_block(rows))
            continue
        content = str(part.get("content") or "").strip()
        if content:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": content[:2900]},
            })
    if not any(block.get("type") != "header" for block in blocks) and mrkdwn:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": mrkdwn[:2900]},
        })
    for start in range(0, min(len(actions), 25), 5):
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": _button_label(action, "slack"),
                    },
                    "action_id": "argusai_action",
                    "value": _text(action.get("token"), 64),
                    **({"style": action["style"]} if action.get("style") in {"primary", "danger"} else {}),
                }
                for action in actions[start:start + 5]
            ],
        })
    return mrkdwn, blocks


def render_channel_reply(provider: str, reply: ChannelReply) -> dict[str, Any]:
    """Render one reply into a provider payload without performing I/O."""
    provider_key = str(provider or "webhook").lower()
    cards = tuple(dict(item) for item in reply.blocks if isinstance(item, dict))
    actions = _actions(cards)
    promoted_title = _promoted_title(str(reply.text or ""), cards)
    interactive = provider_key in {
        "qq",
        "telegram",
        "discord",
        "slack",
        "feishu",
        "wecom",
        "webhook",
    }
    # Options are listed in the body only when buttons cannot carry them all,
    # otherwise every choice would appear twice in the same bubble.
    budget = _BUTTON_BUDGET.get(provider_key, 0)
    label_limit = _BUTTON_LABEL_LIMIT.get(provider_key, 0)
    labels_truncated = bool(
        label_limit
        and any(len(_text(action.get("label"), 300)) > label_limit for action in actions)
    )

    def _body(include_titles: bool = True) -> str:
        return card_text(
            reply.text,
            cards,
            include_tokens=not interactive,
            include_actions=(
                not actions
                or len(actions) > budget
                or labels_truncated
            ),
            include_titles=include_titles,
            locale=getattr(reply, "locale", DEFAULT_LOCALE),
        )

    text = _body()
    # Providers with a real card header show the title there instead of
    # repeating it as the first line of the body.
    headed = _body(include_titles=False) if promoted_title else text

    if provider_key == "qq":
        chunks = render_markdown_chunks(text, QQ)
        # QQ's official inline keyboard accepts callback actions. Two buttons
        # per row stay readable on mobile; excess actions remain in the body.
        keyboard_rows = [
            [
                {
                    "id": f"argusai-{start + offset}",
                    "label": _button_label(action, provider_key),
                    "visited_label": _button_label(action, provider_key),
                    "style": (
                        1 if action.get("style") in {"primary", "danger"} else 0
                    ),
                    "data": "act:" + _text(action.get("token"), 64),
                    "group_id": "argusai",
                }
                for offset, action in enumerate(actions[start:start + 2])
            ]
            for start in range(0, min(len(actions), _BUTTON_BUDGET["qq"]), 2)
        ]
        fallback = card_text(
            reply.text,
            cards,
            include_tokens=True,
            include_actions=True,
            locale=getattr(reply, "locale", DEFAULT_LOCALE),
        )
        return {
            "text": "\n\n".join(chunks),
            "text_chunks": chunks,
            "markdown": True,
            "keyboard": {"rows": keyboard_rows} if keyboard_rows else None,
            "plain_fallback_chunks": render_markdown_chunks(fallback, PLAIN),
        }

    if provider_key == "telegram":
        row_size = (
            1
            if any(_telegram_button_width(action.get("label")) > 18 for action in actions)
            else 2
        )
        keyboard = [
            [
                {
                    "text": _button_label(action, provider_key),
                    "callback_data": "act:" + _text(action.get("token"), 64),
                }
                for action in actions[start:start + row_size]
            ]
            for start in range(0, min(len(actions), 16), row_size)
        ]
        chunks = render_markdown_chunks(text, TELEGRAM_HTML)
        return {
            "text": "\n\n".join(chunks),
            "text_chunks": chunks,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        }
    if provider_key == "discord":
        components = []
        for start in range(0, min(len(actions), 25), 5):
            components.append({
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 4 if action.get("style") == "danger" else 1
                        if action.get("style") == "primary" else 2,
                        "label": _button_label(action, provider_key),
                        "custom_id": "act:" + _text(action.get("token"), 64),
                    }
                    for action in actions[start:start + 5]
                ],
            })
        content_chunks = render_markdown_chunks(text, DISCORD)
        return {
            "content": "\n\n".join(content_chunks),
            "content_chunks": content_chunks,
            "components": components,
        }
    if provider_key == "slack":
        mrkdwn, blocks = _render_slack_blocks(
            headed,
            actions,
            promoted_title=promoted_title,
        )
        return {"text": mrkdwn, "blocks": blocks}
    if provider_key == "feishu":
        return {
            "msg_type": "interactive",
            "content": _render_feishu_card(
                reply,
                cards,
                actions,
                promoted_title=promoted_title,
                include_actions=(
                    not actions
                    or len(actions) > budget
                    or labels_truncated
                ),
                labels_truncated=labels_truncated,
            ),
        }
    if provider_key == "wecom":
        if not actions:
            # App messages only accept the v1 subset; markdown_v2 is webhook-only.
            return {
                "msgtype": "markdown",
                "markdown": {"content": _truncate_bytes(render_markdown(text, WECOM), 2000)},
            }
        plain = render_markdown(text, PLAIN)
        return {
            "msgtype": "template_card",
            "template_card": {
                "card_type": "button_interaction",
                "source": {"desc": "ArgusAI"},
                "main_title": {"title": "ArgusAI", "desc": plain[:80]},
                "sub_title_text": plain[:1000],
                "button_list": [
                    {
                        "text": _button_label(action, provider_key),
                        "style": 2 if action.get("style") == "danger" else 1,
                        "key": "act:" + _text(action.get("token"), 64),
                    }
                    for action in actions[:6]
                ],
            },
        }
    if provider_key == "wechat":
        # WeChat message bubbles render no markup at all, so anything left as
        # markdown would reach the user as literal asterisks and backticks.
        return {"text": _wechat_hard_breaks(render_markdown(text, PLAIN))}
    if provider_key == "dingtalk":
        return {
            "msgtype": "actionCard",
            "actionCard": {
                "title": promoted_title[:60] or "ArgusAI",
                "text": render_markdown(text, DINGTALK),
                "btnOrientation": "0",
                "btns": [],
            },
        }
    return {
        "text": text,
        "chat_id": reply.external_chat_id,
        "thread_id": reply.external_thread_id,
        "blocks": list(cards),
        "actions": [
            {
                "label": _text(action.get("label"), 160),
                "token": _text(action.get("token"), 64),
                "style": _text(action.get("style"), 20),
            }
            for action in actions
        ],
    }


def batch_response_text(group: dict[str, Any]) -> str:
    answers = dict(group.get("answers") or {})
    return json.dumps(
        {
            "type": "user_decision_batch_response",
            "title": _text(group.get("title"), 240),
            "answers": answers,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
