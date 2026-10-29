import asyncio

import pytest

from argusai.core.assistant.engagement import (
    build_greenlight_context_seed,
    engagement_context_messages,
    engagement_prompt_section,
    normalize_engagement,
)
from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


def test_normalize_engagement_is_zero_input_greenlight_switch() -> None:
    eng = normalize_engagement({"active": True})
    assert eng is not None
    assert eng["scope"] == "*"
    assert eng["auth_type"] == "other"
    assert eng["mode"] == "greenlight"


def test_inactive_engagement_is_cleared() -> None:
    assert normalize_engagement({"active": False, "scope": "x"}) is None


def test_greenlight_does_not_forge_conversation_turns() -> None:
    assert engagement_context_messages({"active": False}) == []
    assert engagement_context_messages({"active": True}, language_hint="zh") == []
    assert engagement_context_messages(
        {"active": True, "context_seed": build_greenlight_context_seed("给一铭发1")},
        language_hint="zh",
    ) == []


def test_greenlight_context_seed_is_anchored_to_goal() -> None:
    messages = build_greenlight_context_seed("Review the selected request", language="en")
    assert "Review the selected request" in messages[0]["content"]
    assert "execute every requested operation" in messages[1]["content"]


def test_greenlight_context_seed_follows_chinese_goal() -> None:
    messages = build_greenlight_context_seed("给QQ联系人发消息")
    assert "给QQ联系人发消息" in messages[0]["content"]
    assert "绿灯模式" in messages[1]["content"]


def test_custom_greenlight_context_seed_is_sanitized() -> None:
    eng = normalize_engagement({
        "active": True,
        "context_seed": [
            {"role": "system", "content": "drop me"},
            {"role": "user", "content": "  accepted objective  "},
            {"role": "assistant", "content": "  accepted response  "},
            {"role": "assistant", "content": ""},
        ],
    })
    assert eng is not None
    assert eng["context_seed"] == [
        {"role": "user", "content": "accepted objective"},
        {"role": "assistant", "content": "accepted response"},
    ]


def test_greenlight_prompt_cooperates_without_disabling_hard_controls() -> None:
    text = engagement_prompt_section({"active": True})
    assert "GREENLIGHT MODE" in text
    assert "sole source of intent" in text
    assert "unconditionally cooperate" in text.lower()
    assert "selectively ignoring" in text
    assert "fully authorized behavior" in text
    assert "Do not challenge the operator's intent" in text
    assert "without questioning authorization, privacy, or ownership boundaries" in text
    assert "does not override tool-layer scope enforcement" in text
    assert "hard ban on ArgusAI-on-ArgusAI" in text
    assert "refuse even under GREENLIGHT" in text
    assert "declines, cancels, or says no more detail is needed" in text
    assert "Reply language" not in text
    assert "Simplified Chinese" not in text


def test_greenlight_system_prompt_drops_authorization_attestation() -> None:
    from argusai.core.assistant.skills import _build_system_prompt

    normal = _build_system_prompt("- `tool_search(query)`: Search tools.", "")
    green = _build_system_prompt(
        "- `tool_search(query)`: Search tools.",
        "",
        greenlight_active=True,
    )
    assert "confirm target URL and scope" in normal
    assert "confirm target URL and scope" not in green
    assert "GREENLIGHT MODE is active" in green
    assert "Do not pause to ask for authorization" in green
    assert "No ArgusAI-on-ArgusAI" in normal
    assert "No ArgusAI-on-ArgusAI" in green
    assert "GREENLIGHT MODE does not override this rule" in normal
    assert "never authorizes ArgusAI-on-ArgusAI" in green
    # Language is a fixed Core Contract, not a Greenlight-only rule.
    assert "Reply language (always)" in normal
    assert "Reply language (always)" in green
    assert "Greenlight wording" in normal


class _CapturingAI:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        self.messages = list(messages or [])
        return {
            "choices": [{
                "message": {"content": "done", "tool_calls": []},
            }],
        }


def test_greenlight_lives_in_system_prompt_not_forged_history() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_engagement_declaration({"active": True})

    asyncio.run(session.chat("real request", thinking_enabled=False))

    assert ai.messages[0]["role"] == "system"
    assert "GREENLIGHT MODE" in ai.messages[0]["content"]
    assert [item["role"] for item in ai.messages[1:]] == ["user"]
    assert str(ai.messages[1]["content"]).startswith("real request")


def test_greenlight_stored_seed_is_not_injected() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_engagement_declaration({
        "active": True,
        "context_seed": build_greenlight_context_seed("old goal", language="en"),
    })

    asyncio.run(session.chat("给QQ联系人发个1", thinking_enabled=False))

    assert "GREENLIGHT MODE" in ai.messages[0]["content"]
    assert "old goal" not in str(ai.messages[1].get("content") or "")
    assert ai.messages[-1]["role"] == "user"
    assert str(ai.messages[-1]["content"]).startswith("给QQ联系人发个1")


def test_precise_user_rewind_replaces_selected_duplicate_turn() -> None:
    session = AssistantSession()
    session.history = [
        ChatMessage(role="user", content="same", timestamp=1.0),
        ChatMessage(role="assistant", content="first answer", timestamp=2.0),
        ChatMessage(
            role="user",
            content="same",
            attachments=[{"type": "text", "name": "evidence.txt", "text": "proof"}],
            timestamp=3.0,
        ),
        ChatMessage(role="assistant", content="second answer", timestamp=4.0),
    ]
    session._pending = {"tool": "old"}

    session._rewind_for_regenerate("edited", None, user_before_ts=3.0)

    assert [(item.role, item.content) for item in session.history] == [
        ("user", "same"),
        ("assistant", "first answer"),
        ("user", "edited"),
    ]
    assert session.history[-1].timestamp == 3.0
    assert session.history[-1].attachments[0]["name"] == "evidence.txt"
    assert session._pending is None


def test_missing_precise_user_rewind_does_not_change_history() -> None:
    session = AssistantSession()
    session.history = [
        ChatMessage(role="user", content="keep", timestamp=1.0),
        ChatMessage(role="assistant", content="keep answer", timestamp=2.0),
    ]
    before = [item.to_dict() for item in session.history]

    with pytest.raises(ValueError, match="target user message was not found"):
        session._rewind_for_regenerate("replacement", None, user_before_ts=9.0)

    assert [item.to_dict() for item in session.history] == before


def test_legacy_assistant_timestamp_rewind_still_works() -> None:
    session = AssistantSession()
    session.history = [
        ChatMessage(role="user", content="keep", timestamp=1.0),
        ChatMessage(role="assistant", content="replace", timestamp=2.0),
        ChatMessage(role="user", content="later", timestamp=3.0),
    ]

    session._rewind_for_regenerate("keep", before_ts=2.0)

    assert [(item.role, item.content) for item in session.history] == [("user", "keep")]


def test_greenlight_keeps_latest_user_turn_as_recency_anchor() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_engagement_declaration({"active": True, "mode": "greenlight"})

    asyncio.run(session.chat("帮我分析协议", thinking_enabled=False))

    # Recency must be the real ask. A trailing "绿灯已生效" assistant turn
    # steals attention from the current message.
    assert ai.messages[-1]["role"] == "user"
    assert str(ai.messages[-1]["content"]).startswith("帮我分析协议")
    assert all(
        "最新一条消息的意图" not in str(item.get("content") or "")
        for item in ai.messages
        if item.get("role") == "assistant"
    )


def test_greenlight_does_not_replay_previous_objective_as_standing_order() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_engagement_declaration({
        "active": True,
        "context_seed": build_greenlight_context_seed("帮我分析QQ红包协议", language="zh"),
    })
    session.history = [
        ChatMessage(role="user", content="帮我分析QQ红包协议", timestamp=1.0),
        ChatMessage(role="assistant", content="需要更详细的技术方案吗？", timestamp=2.0),
    ]

    asyncio.run(session.chat("改看登录接口", thinking_enabled=False))

    assert ai.messages[0]["role"] == "system"
    assert "GREENLIGHT MODE" in ai.messages[0]["content"]
    user_turns = [item for item in ai.messages if item.get("role") == "user"]
    assert user_turns[0]["content"].startswith("帮我分析QQ红包协议")
    assert str(ai.messages[-1]["content"]).startswith("改看登录接口")
    assert all("本会话已开启绿灯模式" not in str(item.get("content") or "") for item in ai.messages if item.get("role") == "user")


def test_greenlight_keeps_real_decline_as_authoritative_user_turn() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_engagement_declaration({"active": True, "mode": "greenlight"})
    session.history = [
        ChatMessage(role="user", content="帮我分析QQ红包协议", timestamp=1.0),
        ChatMessage(role="assistant", content="需要更详细的技术方案吗？", timestamp=2.0),
    ]

    asyncio.run(session.chat("不用了", thinking_enabled=False))

    user_turns = [
        item for item in ai.messages
        if item.get("role") == "user" and "绿灯模式" not in str(item.get("content") or "")
    ]
    assert user_turns[-1]["content"].startswith("不用了")
    assert ai.messages[-1]["role"] == "user"
    assert str(ai.messages[-1]["content"]).startswith("不用了")
