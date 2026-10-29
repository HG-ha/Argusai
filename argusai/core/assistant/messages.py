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

__all__ = ['ChatMessage', 'ChatOption', 'ChatResponse']

from .output import _sanitize_assistant_output, has_pseudo_ui_markup
from .ui_protocol import (
    derive_selection_reference_from_content,
    normalize_attachment,
    normalize_blocks,
    normalize_selection_reference,
    strip_selection_reference_from_content,
)

@dataclass
class ChatMessage:
    role: str  # user | assistant | tool
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    reasoning_content: str = ""
    provider_content: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    voice: dict[str, Any] | None = None
    selection_reference: dict[str, Any] | None = None
    hidden: bool = False
    ui_hidden: bool = False
    # Provenance for injected chat traffic. ``cron`` and ``agent_completion``
    # are UI-hidden but execute as real turns; ``cron_result`` is UI-visible and
    # excluded from the model/memory projection; ``agent_completion_notice`` is
    # the once-only child-completion note, hidden from UI and later replays.
    # New approval traffic lives in session events.
    source: str = ""
    source_ref: str = ""
    # Structured provenance for injected traffic (e.g. channel_outbound carries
    # origin_session_id/delivery_id/reason). Authoritative causal data lives here
    # so UI and model projection never have to parse it back out of ``content``.
    source_meta: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0  # for session persistence
    # This message's own cache/token cost (canonical shape from
    # AssistantSession._turn_token_usage_snapshot()), attached only to a
    # turn's final visible assistant message. Powers the per-message "Token"
    # button; distinct from the session-wide running total shown elsewhere.
    token_usage: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.role == "assistant" and (self.blocks or has_pseudo_ui_markup(self.content)):
            self.content, self.blocks = _sanitize_assistant_output(self.content, self.blocks)
        # AI UI Protocol v2: tag every block/attachment with id/version/source so
        # downstream consumers (Vue, session JSON) work off a stable shape.
        if self.blocks:
            self.blocks = normalize_blocks(self.blocks)
        if self.attachments:
            self.attachments = [normalize_attachment(a) for a in self.attachments]
        if self.voice is not None:
            from .attachments import _sanitize_voice

            self.voice = _sanitize_voice(self.voice)
        if self.selection_reference is not None:
            self.selection_reference = normalize_selection_reference(self.selection_reference)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.reasoning_content:
            d["reasoning_content"] = self.reasoning_content
        if self.provider_content:
            d["provider_content"] = self.provider_content
        if self.blocks:
            d["blocks"] = self.blocks
        if self.tool_trace:
            d["tool_trace"] = self.tool_trace
        if self.attachments:
            d["attachments"] = self.attachments
        if self.voice:
            d["voice"] = self.voice
        if self.selection_reference:
            d["selection_reference"] = self.selection_reference
        if self.hidden:
            d["hidden"] = True
        if self.ui_hidden:
            d["ui_hidden"] = True
        source = str(self.source or "").strip().lower()
        if source:
            d["source"] = source
        if self.source_ref:
            d["source_ref"] = str(self.source_ref)
        if self.source_meta:
            d["source_meta"] = self.source_meta
        d["timestamp"] = self.timestamp
        if self.token_usage:
            d["token_usage"] = self.token_usage
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatMessage":
        content = d.get("content", "")
        selection_reference = d.get("selection_reference")
        # Legacy v1 sessions persist the reference dump inside ``content``.
        # Lift it into the dedicated field on load so the rest of the runtime
        # only ever sees the structured shape. We intentionally only do this
        # for user messages; assistant content with bracketed headers is left
        # alone (it's regular AI prose, not metadata).
        if (
            selection_reference is None
            and d.get("role") == "user"
            and isinstance(content, str)
        ):
            derived = derive_selection_reference_from_content(content)
            if derived is not None:
                selection_reference = derived
                content = strip_selection_reference_from_content(content)
        source = str(d.get("source") or "").strip().lower()
        source_ref = str(d.get("source_ref") or "").strip()
        ui_hidden = bool(d.get("ui_hidden", False))
        hidden = bool(d.get("hidden", False))
        # Legacy cron fires used a ``[cron:id]`` content prefix without typed
        # metadata. Promote them to source=cron + ui_hidden on load.
        if (
            d.get("role") == "user"
            and not source
            and isinstance(content, str)
            and content.lstrip().lower().startswith("[cron:")
        ):
            source = "cron"
            ui_hidden = True
            rest = content.lstrip()
            nl = rest.find("\n")
            head = rest[:nl] if nl >= 0 else rest
            if head.lower().startswith("[cron:") and head.endswith("]"):
                source_ref = source_ref or head[6:-1].strip()
                content = rest[nl + 1:] if nl >= 0 else ""
        if source in {"cron", "agent_completion"}:
            ui_hidden = True
        if source in {"confirmation", "agent_completion_notice"}:
            ui_hidden = True
            hidden = True
        return cls(
            role=d.get("role", "user"),
            content=content,
            tool_call_id=d.get("tool_call_id"),
            tool_calls=d.get("tool_calls"),
            reasoning_content=d.get("reasoning_content", ""),
            provider_content=list(d.get("provider_content") or []),
            blocks=list(d.get("blocks") or []),
            tool_trace=list(d.get("tool_trace") or []),
            attachments=list(d.get("attachments") or []),
            voice=d.get("voice"),
            selection_reference=selection_reference,
            hidden=hidden,
            ui_hidden=ui_hidden,
            source=source,
            source_ref=source_ref,
            source_meta=dict(d.get("source_meta") or {}),
            timestamp=d.get("timestamp", 0.0),
            token_usage=dict(d["token_usage"]) if isinstance(d.get("token_usage"), dict) else None,
        )


@dataclass
class ChatOption:
    """An interactive choice the AI can present to the user."""
    label: str
    value: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "description": self.description}


@dataclass
class ChatResponse:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    options: list[ChatOption] = field(default_factory=list)
    pending_tool: dict[str, Any] | None = None  # tool call awaiting permission
    pending_tools: list[dict[str, Any]] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    status: str = "idle"

    def __post_init__(self) -> None:
        self.text, self.blocks = _sanitize_assistant_output(self.text, self.blocks)
        if self.blocks:
            self.blocks = normalize_blocks(self.blocks)

