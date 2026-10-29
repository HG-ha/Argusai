"""Assistant runtime helpers split from the former monolithic module."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

log = logging.getLogger("argusai.assistant")

__all__ = [
    '_data_table_signature',
    '_dedupe_blocks',
    '_rescue_pseudo_ui_markup',
    '_sanitize_assistant_output',
    'has_pseudo_ui_markup',
]

# Weaker / less instruction-adherent models sometimes improvise a clickable
# option list using markup borrowed from other agent products
# (``<next_actions><option>…</option></next_actions>``) instead of calling
# ``stop_for_user_decision``. Our renderer does not know those tags, so they
# leak into the chat as raw text. We rescue that near-miss into a real
# ``user_decision`` block and strip the stray tags — a model-independent
# guardrail, the same "trust the runtime, not the model output" philosophy as
# ``_dedupe_blocks``.
_PSEUDO_OPTION_RE = re.compile(r"<option\b[^>]*>(.*?)</option>", re.IGNORECASE | re.DOTALL)
_PSEUDO_CONTAINER_RE = re.compile(
    r"<(next_actions|nextactions|next-actions|options|actions|action_list|"
    r"suggestions|choices|decision)\b[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Fenced or inline code must never be rewritten: an answer may legitimately
# discuss HTML ``<option>`` elements.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_EMPTY_BULLET_RE = re.compile(r"(?m)^[ \t]*[-*][ \t]*$\n?")
_DECISION_BLOCK_TYPES = {"user_decision", "user_decision_batch"}
_MAX_PSEUDO_OPTIONS = 8


def has_pseudo_ui_markup(text: Any) -> bool:
    """Cheap gate: does the text plausibly contain pseudo option markup?"""
    return isinstance(text, str) and "<option" in text.lower()


def _clean_option_label(raw: str) -> str:
    label = re.sub(r"<[^>]+>", "", str(raw or ""))
    label = label.strip().lstrip("-*•").strip()
    # Collapse internal whitespace/newlines so a wrapped label stays one line.
    label = re.sub(r"\s+", " ", label)
    return label[:120].strip()


def _split_code_and_prose(text: str) -> list[tuple[str, bool]]:
    """Return ``(segment, is_code)`` parts, code spans kept verbatim."""
    parts: list[tuple[str, bool]] = []
    last = 0
    for match in _CODE_SPAN_RE.finditer(text):
        if match.start() > last:
            parts.append((text[last:match.start()], False))
        parts.append((match.group(0), True))
        last = match.end()
    if last < len(text):
        parts.append((text[last:], False))
    return parts


def _rescue_pseudo_ui_markup(
    text: str,
    blocks: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Convert stray ``<option>`` markup into a ``user_decision`` block.

    No-op unless the prose (outside code spans) actually carries the markup.
    Leftover/standalone option tags are degraded to plain text so nothing ever
    renders as a raw tag.
    """
    if not has_pseudo_ui_markup(text):
        return text, blocks
    segments = _split_code_and_prose(text)
    prose = "".join(seg for seg, is_code in segments if not is_code)
    if "<option" not in prose.lower():
        # Every option tag lived inside code; leave the answer untouched.
        return text, blocks

    options: list[str] = []
    seen: set[str] = set()
    for match in _PSEUDO_OPTION_RE.finditer(prose):
        label = _clean_option_label(match.group(1))
        key = label.lower()
        if label and key not in seen:
            seen.add(key)
            options.append(label)
        if len(options) >= _MAX_PSEUDO_OPTIONS:
            break

    out_blocks = list(blocks or [])
    has_decision = any(
        isinstance(b, dict) and b.get("type") in _DECISION_BLOCK_TYPES
        for b in out_blocks
    )
    if options and not has_decision:
        question = _pseudo_decision_question(prose)
        out_blocks.append({
            "type": "user_decision",
            "question": question,
            "options": options,
            "context": "",
            "decision_kind": "next_action",
        })

    # Clean only the prose segments: drop whole containers, degrade any leftover
    # standalone <option> to its inner text, then tidy emptied bullet lines.
    rebuilt: list[str] = []
    for seg, is_code in segments:
        if is_code:
            rebuilt.append(seg)
            continue
        cleaned = _PSEUDO_CONTAINER_RE.sub("", seg)
        cleaned = _PSEUDO_OPTION_RE.sub(lambda m: _clean_option_label(m.group(1)), cleaned)
        cleaned = re.sub(r"</?option\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = _EMPTY_BULLET_RE.sub("", cleaned)
        rebuilt.append(cleaned)
    new_text = "".join(rebuilt)
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
    return new_text, out_blocks


def _pseudo_decision_question(prose: str) -> str:
    """Derive the card question from the prose leading up to the options."""
    head = prose
    first_container = _PSEUDO_CONTAINER_RE.search(prose)
    first_option = _PSEUDO_OPTION_RE.search(prose)
    cut = min(
        (m.start() for m in (first_container, first_option) if m is not None),
        default=len(prose),
    )
    head = prose[:cut]
    for line in reversed(head.splitlines()):
        stripped = re.sub(r"<[^>]+>", "", line).strip().lstrip("#").strip()
        stripped = stripped.rstrip("：: ").strip()
        if stripped:
            return stripped[:200]
    return "请选择下一步操作"


def _data_table_signature(block: dict[str, Any]) -> str:
    """Return a stable signature for duplicate table detection."""
    if not isinstance(block, dict) or block.get("type") != "data_table":
        return ""
    rows = block.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return ""
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        cells = row.get("cells") if isinstance(row.get("cells"), dict) else {}
        cell_text = "|".join(f"{key}={cells.get(key)}" for key in sorted(cells))
        parts.append(f"{row_id}:{cell_text}")
    return "||".join(parts)


def _dedupe_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate or generic-empty data tables from one assistant output."""
    clean: list[dict[str, Any]] = []
    seen_tables: set[str] = set()
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "data_table":
            rows = block.get("rows") or []
            title = str(block.get("title") or "").strip().lower()
            if not rows and title in {"", "结果列表", "results", "result list"}:
                continue
            sig = _data_table_signature(block)
            if sig and sig in seen_tables:
                continue
            if sig:
                seen_tables.add(sig)
        clean.append(block)
    return clean


def _sanitize_assistant_output(text: str, blocks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Keep block-bearing assistant output canonical before persistence."""
    text, blocks = _rescue_pseudo_ui_markup(text, list(blocks or []))
    clean_blocks = _dedupe_blocks(list(blocks or []))
    return text, clean_blocks
