"""Tests for the AI UI Protocol v2 normalization layer."""

from __future__ import annotations

from argusai.core.assistant.ui_protocol import (
    ACTION_TYPES,
    BLOCK_SOURCES,
    BLOCK_VERSION_DEFAULT,
    PROTOCOL_VERSION,
    SELECTION_REFERENCE_KINDS,
    block_payload_view,
    derive_selection_reference_from_content,
    infer_block_source,
    normalize_action,
    normalize_attachment,
    normalize_block,
    normalize_blocks,
    normalize_message,
    normalize_selection_reference,
    normalize_stream_event,
    selection_reference_to_prompt_text,
    strip_selection_reference_from_content,
)


# ---------------------------------------------------------------------------
# block normalization
# ---------------------------------------------------------------------------

def test_normalize_block_adds_missing_metadata() -> None:
    raw = {"type": "data_table", "title": "result"}

    b = normalize_block(raw)

    assert b["type"] == "data_table"
    assert b["title"] == "result"  # business field preserved
    assert isinstance(b["id"], str) and b["id"].startswith("blk_")
    assert b["version"] == BLOCK_VERSION_DEFAULT
    assert b["source"] in BLOCK_SOURCES


def test_normalize_block_is_idempotent() -> None:
    raw = {"type": "todo_list", "items": []}

    once = normalize_block(raw)
    twice = normalize_block(once)

    assert once["id"] == twice["id"]
    assert once["version"] == twice["version"]
    assert once["source"] == twice["source"]


def test_normalize_block_preserves_existing_id_and_source() -> None:
    raw = {
        "type": "data_table",
        "id": "blk_pinned",
        "source": "model",
        "version": 7,
    }

    b = normalize_block(raw)

    assert b["id"] == "blk_pinned"
    assert b["source"] == "model"
    assert b["version"] == 7


def test_normalize_block_does_not_mutate_input() -> None:
    raw = {"type": "todo_list"}

    normalize_block(raw)

    assert "id" not in raw
    assert "version" not in raw


def test_normalize_block_skips_non_dict() -> None:
    assert normalize_block(None) is None
    assert normalize_block("oops") == "oops"
    assert normalize_block(42) == 42


def test_normalize_blocks_returns_empty_for_falsy_input() -> None:
    assert normalize_blocks(None) == []
    assert normalize_blocks([]) == []


def test_normalize_block_recovers_invalid_source() -> None:
    raw = {"type": "task_progress", "source": "nonsense"}

    b = normalize_block(raw)

    assert b["source"] == "runtime"


def test_block_source_inference_table() -> None:
    assert infer_block_source("task_progress") == "runtime"
    assert infer_block_source("data_table") == "model"
    assert infer_block_source("probe_result") == "tool"
    assert infer_block_source("unknown_block_kind") == "tool"
    assert infer_block_source("") == "tool"


# ---------------------------------------------------------------------------
# action normalization
# ---------------------------------------------------------------------------

def test_normalize_action_from_legacy_value() -> None:
    a = normalize_action({"label": "Send", "value": "hello AI"})

    assert a["type"] == "send_text"
    assert a["payload"] == {"value": "hello AI"}
    assert a["label"] == "Send"
    assert a["id"].startswith("act_")


def test_normalize_action_from_route_href() -> None:
    a = normalize_action({"label": "Open", "href": "#/tasks/abc"})

    assert a["type"] == "open_route"
    assert a["payload"] == {"href": "#/tasks/abc"}


def test_normalize_action_from_external_href() -> None:
    a = normalize_action({"label": "Docs", "href": "https://example.com"})

    assert a["type"] == "open_external"
    assert a["payload"] == {"href": "https://example.com"}


def test_normalize_action_from_confirm_value() -> None:
    a = normalize_action({
        "label": "总是允许",
        "confirm_value": "always",
        "permission_key": "sandbox:run:python",
        "confirm_id": "c_42",
    })

    assert a["type"] == "tool_decision"
    assert a["payload"]["decision"] == "always"
    assert a["payload"]["permission_key"] == "sandbox:run:python"
    assert a["payload"]["confirm_id"] == "c_42"


def test_normalize_action_preserves_already_typed_action() -> None:
    raw = {
        "id": "act_keep",
        "type": "open_media",
        "payload": {"url": "sandbox://x.png"},
    }

    a = normalize_action(raw)

    assert a["id"] == "act_keep"
    assert a["type"] == "open_media"
    assert a["payload"] == {"url": "sandbox://x.png"}


def test_normalize_action_falls_back_to_custom() -> None:
    a = normalize_action({"label": "weird"})

    assert a["type"] == "custom"
    assert a["payload"] == {}


def test_normalize_action_types_are_known() -> None:
    legacy_examples = [
        {"value": "x"},
        {"href": "#/foo"},
        {"href": "https://x"},
        {"confirm_value": "allow"},
        {"cancel_value": "deny"},
    ]
    for raw in legacy_examples:
        a = normalize_action(raw)
        assert a["type"] in ACTION_TYPES


# ---------------------------------------------------------------------------
# attachment normalization
# ---------------------------------------------------------------------------

def test_normalize_attachment_kind_from_mime() -> None:
    a = normalize_attachment({"name": "x", "mime": "image/png"})

    assert a["kind"] == "image"
    assert a["id"].startswith("att_")
    assert a["source"] == "user"


def test_normalize_attachment_kind_from_filename() -> None:
    assert normalize_attachment({"name": "foo.MP4"})["kind"] == "video"
    assert normalize_attachment({"name": "foo.bin"})["kind"] == "file"


def test_normalize_attachment_maps_legacy_url_field() -> None:
    a = normalize_attachment({"name": "x", "media_url": "sandbox://x.png"})

    assert a["url"] == "sandbox://x.png"


def test_normalize_attachment_maps_legacy_data_url_field() -> None:
    a = normalize_attachment({"name": "x", "dataUrl": "data:image/png;base64,xxx"})

    assert a["data_url"] == "data:image/png;base64,xxx"


# ---------------------------------------------------------------------------
# block actions inside payload
# ---------------------------------------------------------------------------

def test_normalize_block_normalizes_top_level_actions() -> None:
    raw = {
        "type": "structured_card",
        "actions": [
            {"label": "Go", "value": "do it"},
            {"label": "Docs", "href": "https://x"},
        ],
    }

    b = normalize_block(raw)

    assert b["actions"][0]["type"] == "send_text"
    assert b["actions"][1]["type"] == "open_external"


def test_normalize_block_derives_confirm_payload() -> None:
    raw = {
        "type": "confirm",
        "title": "AI wants to run xx",
        "name": "sandbox_run_python",
        "confirm_id": "c_1",
        "permission_key": "sandbox:run:python",
        "confirm_value": "yes",
        "cancel_value": "no",
        "actions": [{"label": "auto allow", "value": "allow_session", "permission_key": "sandbox:run:python"}],
    }

    b = normalize_block(raw)

    payload = b["payload"]
    assert payload["tool_name"] == "sandbox_run_python"
    assert payload["confirm_id"] == "c_1"
    assert payload["permission_key"] == "sandbox:run:python"
    decisions = [a["payload"]["decision"] for a in payload["actions"] if a["type"] == "tool_decision"]
    assert "allow_once" in decisions
    assert "deny" in decisions
    # Legacy top-level fields untouched.
    assert b["confirm_value"] == "yes"
    assert b["cancel_value"] == "no"


def test_normalize_block_does_not_overwrite_existing_payload() -> None:
    raw = {
        "type": "confirm",
        "confirm_value": "yes",
        "payload": {"custom": True},
    }

    b = normalize_block(raw)

    assert b["payload"] == {"custom": True}


def test_normalize_block_normalizes_payload_actions() -> None:
    raw = {
        "type": "tool_confirm",
        "payload": {
            "actions": [
                {"label": "允许", "confirm_value": "allow_once", "confirm_id": "c1"},
            ],
        },
    }

    b = normalize_block(raw)

    assert b["payload"]["actions"][0]["type"] == "tool_decision"
    assert b["payload"]["actions"][0]["payload"]["decision"] == "allow_once"


# ---------------------------------------------------------------------------
# message / stream
# ---------------------------------------------------------------------------

def test_normalize_message_normalizes_blocks_and_attachments() -> None:
    msg = {
        "role": "assistant",
        "content": "hi",
        "blocks": [{"type": "todo_list"}],
        "attachments": [{"name": "a.png", "mime": "image/png"}],
    }

    out = normalize_message(msg)

    assert out["_protocol_version"] == PROTOCOL_VERSION
    assert out["blocks"][0]["id"].startswith("blk_")
    assert out["attachments"][0]["kind"] == "image"


def test_normalize_stream_event_block_upsert() -> None:
    raw = {
        "kind": "block_upsert",
        "data": {"block": {"type": "task_progress"}},
    }

    ev = normalize_stream_event(raw)

    assert ev["kind"] == "block_upsert"
    assert ev["data"]["block"]["id"].startswith("blk_")
    assert "ts" in ev


def test_normalize_stream_event_defaults_to_text_chunk() -> None:
    ev = normalize_stream_event({"data": {"text": "hello"}})

    assert ev["kind"] == "text_chunk"


# ---------------------------------------------------------------------------
# payload view
# ---------------------------------------------------------------------------

def test_block_payload_view_flattens_nested_payload() -> None:
    block = {"type": "data_table", "id": "blk_a", "payload": {"title": "foo"}}

    view = block_payload_view(block)

    assert view["title"] == "foo"
    assert view["type"] == "data_table"
    assert view["id"] == "blk_a"
    assert "payload" not in view


def test_block_payload_view_handles_flat_block() -> None:
    block = {"type": "data_table", "title": "foo"}

    view = block_payload_view(block)

    assert view["title"] == "foo"


def test_block_payload_view_handles_non_dict() -> None:
    assert block_payload_view(None) == {}
    assert block_payload_view("oops") == {}


# ---------------------------------------------------------------------------
# selection reference
# ---------------------------------------------------------------------------

def test_selection_reference_kinds_constant() -> None:
    assert set(SELECTION_REFERENCE_KINDS) == {"image_attachment", "message", "file_selection"}


def test_normalize_selection_reference_passthrough() -> None:
    ref = {"kind": "image_attachment", "attachment_name": "x.png"}

    out = normalize_selection_reference(ref)

    assert out == ref


def test_normalize_selection_reference_infers_image() -> None:
    out = normalize_selection_reference({"attachment_id": "att_1", "attachment_name": "x.png"})

    assert out["kind"] == "image_attachment"


def test_normalize_selection_reference_infers_message() -> None:
    out = normalize_selection_reference({"role": "user", "text": "hello"})

    assert out["kind"] == "message"


def test_normalize_selection_reference_infers_file_selection() -> None:
    out = normalize_selection_reference({"file_path": "/x/y.py", "start_line": 1})

    assert out["kind"] == "file_selection"


def test_normalize_selection_reference_rejects_non_dict() -> None:
    assert normalize_selection_reference(None) is None
    assert normalize_selection_reference("oops") is None
    assert normalize_selection_reference({"garbage": True}) is None


def test_derive_selection_reference_from_image_content() -> None:
    body = (
        "请帮我看看这张图\n\n"
        "[图片引用上下文]\n"
        "来源: 用户消息#3\n"
        "图片: screenshot.png\n"
        "类型: image/png\n"
        "说明: 用户引用了上述消息中已展示的图片。"
    )

    ref = derive_selection_reference_from_content(body)

    assert ref["kind"] == "image_attachment"
    assert ref["role"] == "user"
    assert ref["message_index"] == 2
    assert ref["attachment_name"] == "screenshot.png"
    assert ref["attachment_mime"] == "image/png"


def test_derive_selection_reference_from_message_content() -> None:
    body = (
        "解释这段\n\n"
        "[对话引用上下文]\n"
        "来源: assistant\n"
        "内容:\n"
        "```text\n"
        "原文片段\n"
        "```"
    )

    ref = derive_selection_reference_from_content(body)

    assert ref["kind"] == "message"
    assert ref["role"] == "assistant"
    assert ref["text"] == "原文片段"


def test_derive_selection_reference_from_file_content() -> None:
    body = (
        "看一下这段代码\n\n"
        "[选区上下文]\n"
        "文件: /sandbox/foo.py\n"
        "范围: L12:C4 - L14:C0\n"
        "内容:\n"
        "```text\n"
        "def foo():\n"
        "    pass\n"
        "```"
    )

    ref = derive_selection_reference_from_content(body)

    assert ref["kind"] == "file_selection"
    assert ref["file_path"] == "/sandbox/foo.py"
    assert ref["start_line"] == 12
    assert ref["start_column"] == 4
    assert ref["end_line"] == 14
    assert ref["end_column"] == 0
    assert "def foo():" in ref["text"]


def test_derive_selection_reference_returns_none_for_plain_text() -> None:
    assert derive_selection_reference_from_content("just a regular question") is None
    assert derive_selection_reference_from_content("") is None
    assert derive_selection_reference_from_content(None) is None


def test_strip_selection_reference_removes_tail_only() -> None:
    body = "前缀文字\n\n[选区上下文]\n文件: a.py\n范围: L1:C0 - L2:C0"

    cleaned = strip_selection_reference_from_content(body)

    assert cleaned == "前缀文字"


def test_strip_selection_reference_noop_when_absent() -> None:
    assert strip_selection_reference_from_content("hello") == "hello"


def test_normalize_message_migrates_legacy_v1_user_content() -> None:
    """Legacy v1 sessions persisted ``[图片引用上下文]`` inside ``content``.

    On load we want the structured field lifted out so the UI renders a chip
    and the prompt block isn't double-injected when chat() walks history."""
    msg = {
        "role": "user",
        "content": (
            "请分析这张图\n\n"
            "[图片引用上下文]\n"
            "来源: 用户消息#1\n"
            "图片: scan.png\n"
            "类型: image/png\n"
            "说明: 用户引用了上述消息中已展示的图片。"
        ),
    }

    out = normalize_message(msg)

    assert out["selection_reference"]["kind"] == "image_attachment"
    assert out["selection_reference"]["attachment_name"] == "scan.png"
    # Tail dump must be stripped so the visible bubble keeps the actual question.
    assert "[图片引用上下文]" not in out["content"]
    assert out["content"].strip() == "请分析这张图"


def test_normalize_message_keeps_explicit_selection_reference() -> None:
    """When the producer already supplied a structured reference, don't touch content."""
    msg = {
        "role": "user",
        "content": "请分析这张图",
        "selection_reference": {
            "kind": "image_attachment",
            "attachment_name": "scan.png",
        },
    }

    out = normalize_message(msg)

    assert out["selection_reference"]["attachment_name"] == "scan.png"
    assert out["content"] == "请分析这张图"


def test_selection_reference_to_prompt_text_image_round_trip() -> None:
    ref = {
        "kind": "image_attachment",
        "role": "user",
        "message_index": 0,
        "attachment_name": "scan.png",
        "attachment_mime": "image/png",
    }

    text = selection_reference_to_prompt_text(ref)

    assert "[图片引用上下文]" in text
    assert "scan.png" in text
    # The rendered prompt is the same shape the v1 parser recognises, so
    # round-tripping back through `derive_*` yields the original kind.
    parsed = derive_selection_reference_from_content("body\n\n" + text)
    assert parsed["kind"] == "image_attachment"
    assert parsed["attachment_name"] == "scan.png"


def test_selection_reference_to_prompt_text_message_kind() -> None:
    ref = {"kind": "message", "role": "assistant", "text": "原文"}

    text = selection_reference_to_prompt_text(ref)

    assert "[对话引用上下文]" in text
    assert "来源: assistant" in text
    assert "原文" in text


def test_selection_reference_to_prompt_text_file_selection_kind() -> None:
    ref = {
        "kind": "file_selection",
        "file_path": "/x.py",
        "start_line": 1,
        "start_column": 0,
        "end_line": 2,
        "end_column": 0,
        "text": "def foo():\n    pass",
    }

    text = selection_reference_to_prompt_text(ref)

    assert "[选区上下文]" in text
    assert "/x.py" in text
    assert "L1:C0 - L2:C0" in text


def test_selection_reference_to_prompt_text_unknown_kind_returns_empty() -> None:
    assert selection_reference_to_prompt_text(None) == ""
    assert selection_reference_to_prompt_text({}) == ""
    assert selection_reference_to_prompt_text({"kind": "bogus"}) == ""
