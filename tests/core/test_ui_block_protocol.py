from argusai.core.assistant import (
    AssistantSession,
    ChatMessage,
    ChatResponse,
    _SKILL_DOCS,
    _select_skill_text,
)


def _table(rows):
    return {
        "type": "data_table",
        "title": "知识库搜索结果",
        "columns": [{"key": "title", "label": "标题"}],
        "rows": [{"id": row, "cells": {"title": row}} for row in rows],
    }


def test_ui_block_skill_is_shortlisted_for_table_requests() -> None:
    assert any(skill.name == "ui-block-protocol" for skill in _SKILL_DOCS)

    # Progressive disclosure: the per-turn shortlist points at the playbook by
    # name (so the model can skill_read it) — it never injects the body.
    text = _select_skill_text("用 show_data_table 展示知识库结果", {"kb_search"})

    assert "ui-block-protocol" in text
    assert "UI Block Protocol" not in text  # body title is NOT pushed


def test_ui_block_skill_is_shortlisted_for_mindmap_requests() -> None:
    text = _select_skill_text("画一张认证链路的思维导图", set())
    assert "ui-block-protocol" in text


def test_chat_response_preserves_model_text_when_data_table_exists() -> None:
    response = ChatResponse(
        text=(
            "找到 2 条记录。\n\n"
            "| 标题 | 可信度 |\n"
            "| --- | --- |\n"
            "| A | confirmed |\n"
            "| B | confirmed |\n\n"
            "下一步可以点查看。"
        ),
        blocks=[_table(["A", "B"])],
    )

    assert "| 标题 |" in response.text
    assert "找到 2 条记录。" in response.text
    assert "下一步可以点查看。" in response.text


def test_chat_message_dedupes_duplicate_data_tables() -> None:
    message = ChatMessage(
        role="assistant",
        content="done",
        blocks=[_table(["A"]), _table(["A"])],
    )

    assert len(message.blocks) == 1


def test_search_tools_do_not_auto_render_data_tables() -> None:
    session = AssistantSession()
    blocks = session._blocks_for_tool_trace([{
        "name": "kb_search",
        "arguments": {"query": "csrf"},
        "result": {
            "ok": True,
            "entries": [{
                "id": "kb-1",
                "title": "CSRF note",
                "tags": ["csrf"],
                "confidence": "confirmed",
            }],
        },
    }])

    assert blocks == []


def test_show_data_table_renders_explicit_data_table_block() -> None:
    session = AssistantSession()
    result = session.show_data_table(
        "知识库搜索结果",
        "1 条",
        [{"key": "title", "label": "标题"}],
        [{"id": "kb-1", "cells": {"title": "CSRF note"}, "action": {"label": "查看", "href": "#/knowledge/kb-1"}}],
    )
    blocks = session._blocks_for_tool_trace([{
        "name": "show_data_table",
        "arguments": {},
        "result": result,
    }])

    assert len(blocks) == 1
    assert blocks[0]["type"] == "data_table"
    assert blocks[0]["render_policy"] == "model_authored"


def test_show_data_table_supports_multiple_row_actions() -> None:
    session = AssistantSession()
    result = session.show_data_table(
        "知识库搜索结果",
        "1 条",
        [{"key": "title", "label": "标题"}],
        [{
            "id": "kb-1",
            "cells": {"title": "CSRF note"},
            "actions": [
                {"label": "查看", "href": "#/knowledge/kb-1"},
                {"label": "使用", "value": "基于 kb-1 生成测试计划"},
                {"label": "外链", "href": "https://example.test"},
                {"label": "对比", "value": "对比 kb-1"},
                {"label": "过量", "value": "不应保留"},
            ],
        }],
    )
    block = result["block"]

    assert block["rows"][0]["actions"] == [
        {"label": "查看", "href": "#/knowledge/kb-1"},
        {"label": "使用", "value": "基于 kb-1 生成测试计划"},
        {"label": "对比", "value": "对比 kb-1"},
    ]
    assert block["rows"][0]["action"] == {"label": "查看", "href": "#/knowledge/kb-1"}


def test_show_structured_card_renders_sanitized_block() -> None:
    session = AssistantSession()
    result = session.show_structured_card(
        "审计概览",
        "当前目标摘要",
        "info",
        [
            {"type": "metrics", "items": [{"label": "发现", "value": "3", "note": "待确认"}]},
            {"type": "kv", "items": [{"key": "目标", "value": "https://example.test"}]},
            {"type": "badges", "items": [{"label": "SQLi", "variant": "warning"}, {"label": "bad", "variant": "unknown"}]},
            {"type": "actions", "actions": [{"label": "查看知识", "href": "#/knowledge/kb-1"}, {"label": "外链", "href": "https://example.test"}]},
            {"type": "html", "html": "<script>alert(1)</script>"},
        ],
    )
    blocks = session._blocks_for_tool_trace([{
        "name": "show_structured_card",
        "arguments": {},
        "result": result,
    }])

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "structured_card"
    assert block["render_policy"] == "model_authored"
    assert block["variant"] == "info"
    assert [section["type"] for section in block["sections"]] == ["metrics", "kv", "badges", "actions"]
    assert block["sections"][1]["items"][0] == {"label": "目标", "value": "https://example.test"}
    assert block["sections"][3]["actions"] == [{"label": "查看知识", "href": "#/knowledge/kb-1"}]


def test_show_structured_card_dedupes_within_turn() -> None:
    session = AssistantSession()
    # Simulate a turn boundary (chat() sets this each turn).
    session._emitted_card_signatures = set()
    args = (
        "审计概览",
        "当前目标摘要",
        "info",
        [{"type": "metrics", "items": [{"label": "发现", "value": "3"}]}],
    )

    first = session.show_structured_card(*args)
    second = session.show_structured_card(*args)

    assert "block" in first and not first.get("duplicate")
    assert second.get("duplicate") is True and "block" not in second

    blocks = session._blocks_for_tool_trace([
        {"name": "show_structured_card", "arguments": {}, "result": first},
        {"name": "show_structured_card", "arguments": {}, "result": second},
    ])
    structured = [b for b in blocks if b.get("type") == "structured_card"]
    assert len(structured) == 1


def test_show_structured_card_repeats_across_turns() -> None:
    session = AssistantSession()
    args = (
        "审计概览",
        "当前目标摘要",
        "info",
        [{"type": "metrics", "items": [{"label": "发现", "value": "3"}]}],
    )

    session._emitted_card_signatures = set()
    first = session.show_structured_card(*args)
    # New turn → registry reset → identical card is allowed again.
    session._emitted_card_signatures = set()
    second = session.show_structured_card(*args)

    assert "block" in first and "block" in second
    assert not second.get("duplicate")


def test_presentation_tool_result_is_compact_for_model() -> None:
    session = AssistantSession()
    session._emitted_card_signatures = set()
    result = session.show_structured_card(
        "审计概览",
        "当前目标摘要",
        "info",
        [{"type": "metrics", "items": [{"label": "发现", "value": "3"}]}],
    )

    prompt = session._tool_result_to_prompt("show_structured_card", result)

    # The model gets a tiny ack, not the echoed card body.
    assert '"shown": true' in prompt
    assert "结论" not in prompt and "metrics" not in prompt
    assert len(prompt) < 300
    # But the UI block is still produced from the trace.
    blocks = session._blocks_for_tool_trace([
        {"name": "show_structured_card", "arguments": {}, "result": result},
    ])
    assert any(b.get("type") == "structured_card" for b in blocks)


def test_toolless_probe_suppressed_after_presentation_tool() -> None:
    session = AssistantSession()

    # A non-presentation tool round leaves the probe enabled.
    info_traces = [{"name": "tool_search", "arguments": {}, "result": {"ok": True}}]
    assert session._should_probe_toolless_continuation("分析完成。", info_traces) is True

    # Any presentation tool used this turn means the model concluded by
    # rendering its answer — the probe must not nudge another round.
    from argusai.core.assistant.streaming import _PRESENTATION_TOOLS

    for name in _PRESENTATION_TOOLS:
        traces = [{"name": name, "arguments": {}, "result": {"ok": True}}]
        assert session._should_probe_toolless_continuation("分析完成。", traces) is False


def test_completed_todo_plan_gets_one_final_answer_probe() -> None:
    session = AssistantSession()
    session._todos_touched_this_turn = True
    traces = [{"name": "update_todo", "arguments": {}, "result": {"ok": True}}]

    session.todos = [{"id": "verify", "title": "验证", "status": "pending"}]
    assert session._should_probe_toolless_continuation("进展汇报。", traces) is False

    session.todos[0]["status"] = "completed"
    assert session._should_probe_toolless_continuation("进展汇报。", traces) is True


def test_show_file_resource_view_renders_sanitized_block() -> None:
    session = AssistantSession()
    result = session.show_file_resource_view(
        "沙盒产物",
        "生成的脚本和报告",
        "session sandbox",
        [
            {
                "name": "exploit.py",
                "relative_path": "poc/exploit.py",
                "kind": "file",
                "size": 1234,
                "mime": "text/x-python",
                "actions": [{"label": "基于此生成报告", "value": "基于 poc/exploit.py 生成报告"}],
            },
            {"name": "bad.exe", "relative_path": "bin/bad.exe", "kind": "binary", "size": "invalid"},
        ],
    )
    blocks = session._blocks_for_tool_trace([{
        "name": "show_file_resource_view",
        "arguments": {},
        "result": result,
    }])

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "file_resource_view"
    assert block["render_policy"] == "model_authored"
    assert block["items"][0]["relative_path"] == "poc/exploit.py"
    assert block["items"][0]["actions"] == [{"label": "基于此生成报告", "value": "基于 poc/exploit.py 生成报告"}]
    assert block["items"][1]["kind"] == "file"
    assert "size" not in block["items"][1]


def test_chat_message_roundtrip_preserves_selection_reference() -> None:
    msg = ChatMessage(
        role="user",
        content="解释一下",
        selection_reference={
            "kind": "image_attachment",
            "attachment_name": "scan.png",
            "attachment_mime": "image/png",
        },
        timestamp=1.0,
    )

    d = msg.to_dict()
    assert d["selection_reference"]["attachment_name"] == "scan.png"

    restored = ChatMessage.from_dict(d)
    assert restored.selection_reference["kind"] == "image_attachment"
    assert restored.content == "解释一下"


def test_chat_message_from_dict_migrates_legacy_v1_user_content() -> None:
    """v1 sessions persisted ``[图片引用上下文]`` inside content.

    Loading must lift the structured form into selection_reference so the
    rest of the runtime never sees the mixed-up shape again."""
    d = {
        "role": "user",
        "content": (
            "看看这张图\n\n"
            "[图片引用上下文]\n"
            "来源: 用户消息#1\n"
            "图片: scan.png\n"
            "类型: image/png\n"
            "说明: dummy"
        ),
        "timestamp": 1.0,
    }

    msg = ChatMessage.from_dict(d)

    assert msg.selection_reference is not None
    assert msg.selection_reference["kind"] == "image_attachment"
    assert msg.selection_reference["attachment_name"] == "scan.png"
    assert "[图片引用上下文]" not in msg.content
    assert msg.content.strip() == "看看这张图"
