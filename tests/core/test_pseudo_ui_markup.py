from __future__ import annotations

from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.output import (
    _rescue_pseudo_ui_markup,
    _sanitize_assistant_output,
    has_pseudo_ui_markup,
)


def test_pseudo_option_markup_becomes_user_decision_block() -> None:
    text = (
        "下一步操作\n"
        "请选择以下选项继续：\n"
        "<next_actions>\n"
        "- <option>了解详细技术方案（协议分析路线）</option>\n"
        "- <option>开始执行分析（需要确认目标QQ账号信息）</option>\n"
        "- <option>取消操作</option>\n"
        "</next_actions>"
    )
    new_text, blocks = _rescue_pseudo_ui_markup(text, [])

    assert "<option>" not in new_text
    assert "<next_actions>" not in new_text
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "user_decision"
    assert block["question"] == "请选择以下选项继续"
    assert block["options"] == [
        "了解详细技术方案（协议分析路线）",
        "开始执行分析（需要确认目标QQ账号信息）",
        "取消操作",
    ]


def test_chatmessage_rescues_text_only_pseudo_markup() -> None:
    msg = ChatMessage(
        role="assistant",
        content="要选哪个？\n<options><option>继续</option><option>停止</option></options>",
    )
    assert not msg.content.strip().endswith("</options>")
    assert "<option>" not in msg.content
    decision = [b for b in msg.blocks if b.get("type") == "user_decision"]
    assert len(decision) == 1
    assert decision[0]["options"] == ["继续", "停止"]


def test_option_markup_inside_code_is_preserved() -> None:
    text = "HTML 里可以写：\n```html\n<select><option>a</option><option>b</option></select>\n```"
    new_text, blocks = _rescue_pseudo_ui_markup(text, [])
    assert new_text == text
    assert blocks == []
    # And the ChatMessage path must not fabricate a card from a code example.
    msg = ChatMessage(role="assistant", content=text)
    assert all(b.get("type") != "user_decision" for b in msg.blocks)


def test_existing_decision_block_is_not_duplicated() -> None:
    text = "选一个：<option>甲</option><option>乙</option>"
    existing = [{"type": "user_decision", "question": "已存在", "options": ["甲", "乙"]}]
    new_text, blocks = _rescue_pseudo_ui_markup(text, existing)
    assert "<option>" not in new_text
    assert len([b for b in blocks if b.get("type") == "user_decision"]) == 1


def test_plain_prose_is_untouched() -> None:
    text = "这是一段普通回答，没有任何选项标记。"
    assert not has_pseudo_ui_markup(text)
    new_text, blocks = _sanitize_assistant_output(text, [])
    assert new_text == text
    assert blocks == []
