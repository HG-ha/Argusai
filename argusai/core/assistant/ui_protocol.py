"""AI UI Protocol v2 — normalization layer.

This module is the **single source of truth** for the shapes that flow between
``argusai.assistant`` (Python) and ``argusai/webui`` (Vue).

Schemas are documented in :doc:`/docs/AI-UI-PROTOCOL.md`. This file only
provides the runtime helpers needed to *upgrade* legacy dict payloads to the
v2 shape without losing data.

Design goals (Phase 1):

* **Pure functions, no side effects** — every ``normalize_*`` returns a new
  dict and never mutates its input.
* **Idempotent** — ``normalize_block(normalize_block(b))`` == ``normalize_block(b)``.
* **Backward-compatible** — legacy blocks with no ``id``/``version``/``source``
  load and render unchanged; we only *add* metadata.
* **No third-party deps** — pure stdlib, importable from any layer.

The functions intentionally accept ``Any`` and gracefully skip non-dict inputs
because production data (loaded from years of session JSON) contains the full
zoo.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Iterable

__all__ = [
    "PROTOCOL_VERSION",
    "BLOCK_VERSION_DEFAULT",
    "BLOCK_SOURCES",
    "ACTION_TYPES",
    "STREAM_EVENT_KINDS",
    "ATTACHMENT_KINDS",
    "SELECTION_REFERENCE_KINDS",
    "generate_id",
    "infer_block_source",
    "normalize_block",
    "normalize_blocks",
    "normalize_action",
    "normalize_attachment",
    "normalize_selection_reference",
    "derive_selection_reference_from_content",
    "selection_reference_to_prompt_text",
    "strip_selection_reference_from_content",
    "normalize_message",
    "normalize_stream_event",
    "block_payload_view",
]


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = 2
BLOCK_VERSION_DEFAULT = 1

BLOCK_SOURCES = ("tool", "model", "runtime", "system")
ACTION_TYPES = (
    "send_text",
    "open_route",
    "open_external",
    "open_attachment",
    "open_media",
    "tool_decision",
    "user_decision",
    "custom",
)
STREAM_EVENT_KINDS = (
    "text_chunk",
    "block_upsert",
    "tool_activity",
    "tool_complete",
    "agent_round_start",
    "run_done",
    "run_error",
    "stage",
    "ping",
)
ATTACHMENT_KINDS = ("image", "video", "audio", "file")
SELECTION_REFERENCE_KINDS = ("image_attachment", "message", "file_selection")


# Block type → default source. Anything not listed defaults to "tool".
_BLOCK_SOURCE_MAP: dict[str, str] = {
    # runtime-injected
    "task_progress": "runtime",
    "findings_summary": "runtime",
    "confirm": "runtime",
    "tool_confirm": "runtime",
    "user_decision": "runtime",
    "user_decision_batch": "runtime",
    "todo_list": "runtime",
    "kb_updated": "runtime",
    "tool_activity": "runtime",
    "tool_trace": "runtime",
    "tool_skipped": "runtime",
    # model-authored (via show_* tools)
    "scan_plan": "model",
    "command_plan": "model",
    "data_table": "model",
    "structured_card": "model",
    "file_resource_view": "model",
    # tool-produced
    "probe_result": "tool",
    "command_result": "tool",
    "reverse_result": "tool",
    "runtime_trace": "tool",
    "sandbox_file": "tool",
    "sandbox_run": "tool",
    "sandbox_env": "tool",
    "vulnerability_evidence": "tool",
    "attack_chain": "tool",
    "request_replay": "tool",
    "oob_event": "tool",
    # v2 additions
    "media_preview": "tool",
    "attachment_preview": "tool",
}


# ---------------------------------------------------------------------------
# id generation
# ---------------------------------------------------------------------------

def generate_id(prefix: str = "blk") -> str:
    """Return a short, URL-safe id like ``blk_3f1c2d``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def infer_block_source(block_type: str) -> str:
    """Return the default ``source`` for a given block ``type``.

    Unknown types default to ``"tool"`` because the vast majority of legacy
    ad-hoc blocks were produced by tool dispatchers.
    """
    if not isinstance(block_type, str) or not block_type:
        return "tool"
    return _BLOCK_SOURCE_MAP.get(block_type, "tool")


# ---------------------------------------------------------------------------
# block normalization
# ---------------------------------------------------------------------------

def normalize_block(block: Any) -> Any:
    """Upgrade *any* block dict to v2 shape.

    * Non-dict input is returned unchanged (callers commonly pass raw JSON).
    * ``id``, ``version``, ``source`` are filled in if missing.
    * Existing fields are **never** overwritten.
    """
    if not isinstance(block, dict):
        return block
    out = dict(block)
    block_type = out.get("type") or ""
    if not out.get("id"):
        out["id"] = generate_id("blk")
    if "version" not in out or not isinstance(out["version"], int):
        out["version"] = BLOCK_VERSION_DEFAULT
    if not out.get("source"):
        out["source"] = infer_block_source(block_type)
    elif out["source"] not in BLOCK_SOURCES:
        # invalid value → recompute
        out["source"] = infer_block_source(block_type)
    actions = out.get("actions")
    if isinstance(actions, list):
        out["actions"] = [normalize_action(a) for a in actions if a is not None]
    payload = out.get("payload")
    if isinstance(payload, dict):
        nested_actions = payload.get("actions")
        if isinstance(nested_actions, list):
            payload = dict(payload)
            payload["actions"] = [normalize_action(a) for a in nested_actions if a is not None]
            out["payload"] = payload
    # AI UI Protocol v2: derive a typed `payload` for legacy confirm blocks
    # so v2-aware UIs can read a single shape. We do NOT touch the legacy
    # top-level fields the existing Vue card reads, so this is purely
    # additive.
    if block_type == "confirm" and not isinstance(out.get("payload"), dict):
        out["payload"] = _derive_confirm_payload(out)
    return out


def _derive_confirm_payload(block: dict[str, Any]) -> dict[str, Any]:
    """Build a v2 ``tool_confirm``-style payload from a legacy confirm block.

    Legacy shape (still emitted by ``session_blocks._blocks_for_pending_tool``)::

        {
            "type": "confirm",
            "title": "...",
            "name": "...",
            "confirm_id": "...",
            "permission_key": "...",
            "confirm_value": "yes",
            "cancel_value": "no",
            "actions": [{"label": "...", "value": "allow_session", ...}],  # optional
        }

    v2 payload pulls the implicit allow/deny buttons out of ``confirm_value`` /
    ``cancel_value`` into an explicit ``actions: ActionEnvelope[]`` list. The
    legacy ``actions`` array (auto-allow button) is appended unchanged after
    normalization.
    """
    payload: dict[str, Any] = {
        "title": block.get("title"),
        "description": block.get("body") or block.get("description"),
        "tool_name": block.get("name"),
        "confirm_id": block.get("confirm_id"),
        "permission_key": block.get("permission_key"),
        "risk": block.get("risk"),
        "arguments": block.get("arguments"),
    }
    actions: list[dict[str, Any]] = []
    confirm_value = block.get("confirm_value")
    if isinstance(confirm_value, str) and confirm_value:
        actions.append({
            "id": generate_id("act"),
            "type": "tool_decision",
            "label": "允许一次",
            "variant": "primary",
            "payload": {
                "decision": "allow_once",
                "value": confirm_value,
                "confirm_id": block.get("confirm_id"),
            },
        })
    for extra in block.get("actions") or []:
        if not isinstance(extra, dict):
            continue
        actions.append(normalize_action(extra))
    cancel_value = block.get("cancel_value")
    if isinstance(cancel_value, str) and cancel_value:
        actions.append({
            "id": generate_id("act"),
            "type": "tool_decision",
            "label": "拒绝",
            "variant": "danger",
            "payload": {
                "decision": "deny",
                "value": cancel_value,
                "confirm_id": block.get("confirm_id"),
            },
        })
    payload["actions"] = actions
    return {k: v for k, v in payload.items() if v is not None}


def normalize_blocks(blocks: Iterable[Any] | None) -> list[Any]:
    """Normalize a list of blocks. Non-list input returns ``[]``."""
    if not blocks:
        return []
    try:
        return [normalize_block(b) for b in blocks]
    except TypeError:
        return []


# ---------------------------------------------------------------------------
# action normalization
# ---------------------------------------------------------------------------

def _coerce_action_type(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Infer ``(type, payload)`` for a legacy/flat action dict.

    Legacy actions used one or more of these top-level fields:

    * ``value`` — text to send back to the AI
    * ``href`` — route or external link
    * ``confirm_value`` / ``cancel_value`` — old tool confirm
    * ``permission_key`` — tool confirm with permanent allow

    We map these into the unified ``ActionEnvelope`` form. Existing
    ``type`` / ``payload`` win without re-inference.
    """
    existing_type = raw.get("type")
    payload = raw.get("payload")
    if isinstance(existing_type, str) and existing_type in ACTION_TYPES:
        if isinstance(payload, dict):
            return existing_type, dict(payload)
        return existing_type, {}

    # confirm_value/cancel_value → tool_decision
    confirm = raw.get("confirm_value")
    cancel = raw.get("cancel_value")
    if isinstance(confirm, str) and confirm:
        decision_payload: dict[str, Any] = {"decision": confirm}
        if raw.get("permission_key"):
            decision_payload["permission_key"] = raw["permission_key"]
        if raw.get("confirm_id"):
            decision_payload["confirm_id"] = raw["confirm_id"]
        return "tool_decision", decision_payload
    if isinstance(cancel, str) and cancel:
        decision_payload = {"decision": cancel}
        if raw.get("confirm_id"):
            decision_payload["confirm_id"] = raw["confirm_id"]
        return "tool_decision", decision_payload

    # href → route / external
    href = raw.get("href")
    if isinstance(href, str) and href:
        if href.startswith("#/") or href.startswith("/"):
            return "open_route", {"href": href}
        return "open_external", {"href": href}

    # value → send_text
    value = raw.get("value")
    if isinstance(value, str) and value:
        return "send_text", {"value": value}

    return "custom", dict(payload) if isinstance(payload, dict) else {}


def normalize_action(action: Any) -> Any:
    """Upgrade *any* action dict to ``ActionEnvelope`` v2 shape."""
    if not isinstance(action, dict):
        return action
    out = dict(action)
    action_type, payload = _coerce_action_type(out)
    if not out.get("id"):
        out["id"] = generate_id("act")
    out["type"] = action_type
    if payload or not isinstance(out.get("payload"), dict):
        out["payload"] = payload
    # Keep legacy flat fields for backward-compatible Vue readers.
    return out


# ---------------------------------------------------------------------------
# attachment normalization
# ---------------------------------------------------------------------------

_ATTACHMENT_MIME_KIND: dict[str, str] = {}


def _infer_attachment_kind(att: dict[str, Any]) -> str:
    raw_kind = att.get("kind")
    if isinstance(raw_kind, str) and raw_kind in ATTACHMENT_KINDS:
        return raw_kind
    mime = att.get("mime") or att.get("content_type") or ""
    if isinstance(mime, str):
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
    name = att.get("name") or ""
    if isinstance(name, str):
        lower = name.lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")):
            return "image"
        if lower.endswith((".mp4", ".mov", ".webm", ".mkv")):
            return "video"
        if lower.endswith((".mp3", ".wav", ".ogg", ".flac")):
            return "audio"
    return "file"


def normalize_attachment(att: Any) -> Any:
    """Upgrade an attachment dict to ``AttachmentRef`` v2 shape."""
    if not isinstance(att, dict):
        return att
    out = dict(att)
    if not out.get("id"):
        out["id"] = generate_id("att")
    if not out.get("kind") or out.get("kind") not in ATTACHMENT_KINDS:
        out["kind"] = _infer_attachment_kind(out)
    # url <- media_url / path (only when missing)
    if not out.get("url"):
        for fallback in ("media_url", "path", "uri"):
            v = out.get(fallback)
            if isinstance(v, str) and v:
                out["url"] = v
                break
    # data_url <- dataUrl / base64
    if not out.get("data_url"):
        for fallback in ("dataUrl", "base64", "data"):
            v = out.get(fallback)
            if isinstance(v, str) and v.startswith("data:"):
                out["data_url"] = v
                break
    if not out.get("source"):
        out["source"] = "user"
    return out


# ---------------------------------------------------------------------------
# message / stream
# ---------------------------------------------------------------------------

def normalize_message(msg: Any) -> Any:
    """Normalize a message dict (post ``ChatMessage.to_dict()`` shape)."""
    if not isinstance(msg, dict):
        return msg
    out = dict(msg)
    if isinstance(out.get("blocks"), list):
        out["blocks"] = normalize_blocks(out["blocks"])
    if isinstance(out.get("attachments"), list):
        out["attachments"] = [normalize_attachment(a) for a in out["attachments"]]
    if out.get("voice") is not None:
        from .attachments import _sanitize_voice

        out["voice"] = _sanitize_voice(out["voice"])
    if out.get("selection_reference") is not None:
        out["selection_reference"] = normalize_selection_reference(out["selection_reference"])
    elif out.get("role") == "user" and isinstance(out.get("content"), str):
        # Legacy session files persist the reference dump inside `content`.
        # Migrate it once at load time so the rest of the stack only ever
        # sees the structured form.
        derived = derive_selection_reference_from_content(out["content"])
        if derived is not None:
            out["selection_reference"] = derived
            out["content"] = strip_selection_reference_from_content(out["content"])
    out.setdefault("_protocol_version", PROTOCOL_VERSION)
    return out


# ---------------------------------------------------------------------------
# selection reference
# ---------------------------------------------------------------------------

# Headers `selection_reference_to_prompt_text` may emit. Keep in sync with
# `_PROMPT_HEADER_BY_KIND` below — the regex on this list is the only place
# that recognises legacy v1 dumps in user content.
_REFERENCE_HEADERS = ("\u9009\u533a\u4e0a\u4e0b\u6587", "\u56fe\u7247\u5f15\u7528\u4e0a\u4e0b\u6587", "\u5bf9\u8bdd\u5f15\u7528\u4e0a\u4e0b\u6587")
_REFERENCE_TAIL_RE = re.compile(
    r"\n{0,2}\[(?:" + "|".join(_REFERENCE_HEADERS) + r")\][\s\S]*$"
)


_PROMPT_HEADER_BY_KIND: dict[str, str] = {
    "file_selection": "[\u9009\u533a\u4e0a\u4e0b\u6587]",
    "image_attachment": "[\u56fe\u7247\u5f15\u7528\u4e0a\u4e0b\u6587]",
    "message": "[\u5bf9\u8bdd\u5f15\u7528\u4e0a\u4e0b\u6587]",
}


def normalize_selection_reference(ref: Any) -> Any:
    """Coerce a selection-reference dict into the v2 shape.

    * Drops obviously bogus values (non-dict, missing ``kind``) by returning
      ``None`` so the caller can treat the field as absent.
    * Leaves recognised but unknown fields untouched (forward-compat).
    """
    if not isinstance(ref, dict):
        return None
    out = dict(ref)
    kind = out.get("kind")
    if not isinstance(kind, str) or kind not in SELECTION_REFERENCE_KINDS:
        # Try to infer kind from legacy field set.
        if "attachment_name" in out or "attachment_id" in out:
            out["kind"] = "image_attachment"
        elif "message_index" in out or out.get("role") in ("user", "assistant"):
            out["kind"] = "message"
        elif "file_path" in out or "file_name" in out or "start_line" in out:
            out["kind"] = "file_selection"
        else:
            return None
    return out


def derive_selection_reference_from_content(content: str) -> dict[str, Any] | None:
    """Reverse-parse a legacy v1 user-content tail into a SelectionReference.

    Returns ``None`` if no recognised tail is present. The caller is
    responsible for stripping the tail from ``content`` (see
    :func:`strip_selection_reference_from_content`).

    Recognised v1 formats (emitted historically by
    ``buildPreviewSelectionContext`` in the frontend):

    * ``[\u9009\u533a\u4e0a\u4e0b\u6587]`` — file / sandbox-preview selection
    * ``[\u56fe\u7247\u5f15\u7528\u4e0a\u4e0b\u6587]`` — image attachment reference
    * ``[\u5bf9\u8bdd\u5f15\u7528\u4e0a\u4e0b\u6587]`` — chat-text selection
    """
    if not isinstance(content, str):
        return None
    raw = content
    if "[\u56fe\u7247\u5f15\u7528\u4e0a\u4e0b\u6587]" in raw:
        ref: dict[str, Any] = {"kind": "image_attachment"}
        m = re.search(r"^\u6765\u6e90:\s*(.+)$", raw, re.MULTILINE)
        if m:
            source = m.group(1).strip()
            if source.startswith("AI"):
                ref["role"] = "assistant"
            elif source.startswith("\u7528\u6237"):
                ref["role"] = "user"
            ordinal = re.search(r"#(\d+)$", source)
            if ordinal:
                ref["message_index"] = max(0, int(ordinal.group(1)) - 1)
        m = re.search(r"^\u56fe\u7247:\s*(.+)$", raw, re.MULTILINE)
        if m:
            ref["attachment_name"] = m.group(1).strip()
        m = re.search(r"^\u7c7b\u578b:\s*(.+)$", raw, re.MULTILINE)
        if m:
            ref["attachment_mime"] = m.group(1).strip()
        return ref
    if "[\u5bf9\u8bdd\u5f15\u7528\u4e0a\u4e0b\u6587]" in raw:
        ref = {"kind": "message"}
        m = re.search(r"^\u6765\u6e90:\s*(.+)$", raw, re.MULTILINE)
        if m:
            ref["role"] = "assistant" if m.group(1).strip() == "assistant" else "user"
        body = re.search(r"```text\n([\s\S]*?)\n```", raw)
        if body:
            ref["text"] = body.group(1)
        return ref
    if "[\u9009\u533a\u4e0a\u4e0b\u6587]" in raw:
        ref = {"kind": "file_selection"}
        m = re.search(r"^\u6587\u4ef6:\s*(.+)$", raw, re.MULTILINE)
        if m:
            ref["file_path"] = m.group(1).strip()
        m = re.search(r"^\u8303\u56f4:\s*L(\d+):C(\d+) - L(\d+):C(\d+)$", raw, re.MULTILINE)
        if m:
            ref["start_line"] = int(m.group(1))
            ref["start_column"] = int(m.group(2))
            ref["end_line"] = int(m.group(3))
            ref["end_column"] = int(m.group(4))
        body = re.search(r"```text\n([\s\S]*?)\n```", raw)
        if body:
            ref["text"] = body.group(1)
        return ref
    return None


def strip_selection_reference_from_content(content: str) -> str:
    """Drop the trailing v1 reference dump from a user message body.

    Safe to call unconditionally — strings without a recognised tail are
    returned unchanged.
    """
    if not isinstance(content, str):
        return content
    return _REFERENCE_TAIL_RE.sub("", content).rstrip()


def selection_reference_to_prompt_text(ref: Any) -> str:
    """Render a SelectionReference into the legacy v1 prompt block.

    This is the **single** place we serialise references for LLM consumption.
    Keeping the format identical to the v1 dump lets us swap the persistence
    representation without retraining the AI on a new dialect.
    """
    if not isinstance(ref, dict):
        return ""
    kind = ref.get("kind") or ""
    header = _PROMPT_HEADER_BY_KIND.get(kind)
    if not header:
        return ""
    lines: list[str] = [header]
    if kind == "image_attachment":
        role = ref.get("role")
        role_label = "AI \u56de\u590d" if role == "assistant" else "\u7528\u6237\u6d88\u606f"
        idx = ref.get("message_index")
        ordinal = f"#{int(idx) + 1}" if isinstance(idx, int) and idx >= 0 else ""
        lines.append(f"\u6765\u6e90: {role_label}{ordinal}".rstrip())
        name = ref.get("attachment_name")
        if name:
            lines.append(f"\u56fe\u7247: {name}")
        mime = ref.get("attachment_mime")
        if mime:
            lines.append(f"\u7c7b\u578b: {mime}")
        lines.append(
            "\u8bf4\u660e: \u7528\u6237\u5f15\u7528\u4e86\u4e0a\u8ff0\u6d88\u606f\u4e2d\u5df2\u5c55\u793a\u7684\u56fe\u7247\u3002"
            "\u8bf7\u7ed3\u5408\u6b64\u524d\u5bf9\u8bdd\u91cc\u770b\u5230\u7684\u56fe\u50cf\u5185\u5bb9\u56de\u7b54\uff0c"
            "\u4e0d\u9700\u8981\u518d\u6b21\u8bf7\u6c42\u56fe\u7247\u3002"
        )
    elif kind == "message":
        role = ref.get("role") or "user"
        lines.append(f"\u6765\u6e90: {role}")
        text = ref.get("text") or ""
        if text:
            lines.append("\u5185\u5bb9:")
            lines.append("```text")
            lines.append(str(text)[:4000])
            lines.append("```")
    elif kind == "file_selection":
        files = ref.get("files")
        if isinstance(files, list) and files:
            lines.append("文件列表:")
            for item in files[:50]:
                if not isinstance(item, dict):
                    continue
                path = item.get("file_path") or item.get("path") or item.get("file_name") or ""
                if path:
                    lines.append(f"- {path}")
        file_path = ref.get("file_path") or ref.get("file_name") or ""
        if file_path:
            lines.append(f"\u6587\u4ef6: {file_path}")
        if ref.get("start_line"):
            lines.append(
                f"\u8303\u56f4: L{ref.get('start_line')}:C{ref.get('start_column')}"
                f" - L{ref.get('end_line')}:C{ref.get('end_column')}"
            )
        text = ref.get("text") or ""
        if text:
            lines.append("\u5185\u5bb9:")
            lines.append("```text")
            lines.append(str(text)[:4000])
            lines.append("```")
    return "\n".join(lines)


def normalize_stream_event(event: Any) -> Any:
    """Normalize a streaming event dict."""
    if not isinstance(event, dict):
        return event
    out = dict(event)
    if not out.get("kind"):
        # legacy text events: assume text_chunk
        out["kind"] = "text_chunk"
    if "ts" not in out:
        out["ts"] = time.time()
    data = out.get("data")
    if out["kind"] == "block_upsert" and isinstance(data, dict):
        block = data.get("block")
        if isinstance(block, dict):
            new_data = dict(data)
            new_data["block"] = normalize_block(block)
            out["data"] = new_data
    return out


# ---------------------------------------------------------------------------
# payload view (for v2 readers)
# ---------------------------------------------------------------------------

def block_payload_view(block: Any) -> dict[str, Any]:
    """Return a flat dict that merges ``block.payload`` into top-level keys.

    Lets v2-aware code read ``view["title"]`` regardless of whether the block
    stores fields flat (legacy) or nested under ``payload`` (v2).
    """
    if not isinstance(block, dict):
        return {}
    payload = block.get("payload")
    if isinstance(payload, dict):
        merged = dict(block)
        merged.pop("payload", None)
        merged.update(payload)
        return merged
    return dict(block)
