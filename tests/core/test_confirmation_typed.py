"""Loom22 §9: confirmation is a typed RPC payload, not chat-text protocol."""

from __future__ import annotations

import json
import asyncio

from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.streaming import (
    confirmation_decision_from_legacy_message,
    normalize_confirmation_decision,
)
from argusai.core.assistant.tools import ToolRegistry


def test_normalize_batch_confirmation_decision() -> None:
    out = normalize_confirmation_decision({
        "type": "batch_confirmation_decision",
        "decisions": {"call-a": "yes", "call-b": "allow_session"},
        "auto_allow_names": ["run_shell_snippet"],
    })
    assert out is not None
    assert out["type"] == "batch_confirmation_decision"
    assert out["decisions"] == {"call-a": "allow", "call-b": "allow_session"}
    assert out["auto_allow_names"] == ["run_shell_snippet"]


def test_normalize_single_confirmation_decision() -> None:
    out = normalize_confirmation_decision({
        "type": "confirmation_decision",
        "verdict": "deny",
    })
    assert out == {
        "type": "confirmation_decision",
        "verdict": "deny",
        "auto_allow_names": [],
        "auto_allow_permissions": [],
    }


def test_normalize_action_envelope_verdict_aliases() -> None:
    out = normalize_confirmation_decision({
        "type": "tool_decision",
        "confirm_id": "call-a",
        "decision": "allow_once",
    })
    assert out is not None
    assert out["call_id"] == "call-a"
    assert out["verdict"] == "allow"


def test_legacy_confirm_batch_migrates_to_typed() -> None:
    out = confirmation_decision_from_legacy_message(
        'confirm_batch:{"decisions":{"x":"allow"}}'
    )
    assert out is not None
    assert out["type"] == "batch_confirmation_decision"
    assert out["decisions"]["x"] == "allow"


def test_chat_message_keeps_ordinary_yes_content() -> None:
    msg = ChatMessage.from_dict({
        "role": "user",
        "content": "yes",
        "timestamp": 1.0,
    })
    assert msg.source == ""
    assert msg.ui_hidden is False
    assert msg.hidden is False
    assert msg.content == "yes"


def test_chat_message_keeps_typed_confirmation_source() -> None:
    msg = ChatMessage.from_dict({
        "role": "user",
        "content": "",
        "source": "confirmation",
        "hidden": True,
        "ui_hidden": True,
        "timestamp": 1.0,
    })
    assert msg.source == "confirmation"
    assert msg.to_dict()["source"] == "confirmation"


def _pending_session() -> AssistantSession:
    session = AssistantSession(registry=ToolRegistry())
    session._pending = {
        "confirmations": [{
            "name": "run_shell_snippet",
            "arguments": {"script": "echo ok"},
            "tool_call": {"id": "call-a"},
        }],
        "tool_calls": [],
        "messages": [],
        "assistant_response": {},
        "decisions": {},
        "auto_allow_names": [],
        "auto_allow_permissions": [],
    }
    return session


def test_confirmation_binding_rejects_stale_pending() -> None:
    session = _pending_session()
    pending_id, version = session._ensure_pending_identity()
    good = {
        "type": "confirmation_decision",
        "pending_id": pending_id,
        "version": version,
        "call_id": "call-a",
        "verdict": "allow",
    }
    assert session._validate_confirmation_binding(
        good,
        session._pending,
        allow_legacy_unbound=False,
    ) == ""
    stale = {**good, "pending_id": "pending_stale"}
    assert "过期" in session._validate_confirmation_binding(
        stale,
        session._pending,
        allow_legacy_unbound=False,
    )


def test_confirmation_decision_is_event_not_chat_message() -> None:
    session = _pending_session()
    pending_id, version = session._ensure_pending_identity()
    decision = {
        "type": "confirmation_decision",
        "pending_id": pending_id,
        "version": version,
        "call_id": "call-a",
        "verdict": "allow",
    }
    session._record_confirmation_decision(decision, session._pending, timestamp=1.0)
    assert session.history == []
    assert session.events[-1]["kind"] == "approval_decision"
    assert session.events[-1]["context_policy"] == "exclude"
    assert session.events[-1]["payload"]["call_id"] == "call-a"

    serialized = json.loads(json.dumps(session.to_dict()))
    restored = AssistantSession.from_dict(
        serialized,
        registry=ToolRegistry(),
    )
    assert restored.events[-1]["payload"]["verdict"] == "allow"
    assert restored._pending["pending_id"] == pending_id


def test_pending_projection_carries_binding_to_renderer() -> None:
    session = _pending_session()
    projection = session.pending_confirmation_projection()
    pending = projection["pending_tools"][0]
    block = projection["pending_blocks"][0]
    assert pending["pending_id"]
    assert pending["version"] == 1
    assert block["pending_id"] == pending["pending_id"]
    assert block["pending_version"] == pending["version"]
    assert session.events[-1]["kind"] == "approval_request"
    assert session.events[-1]["context_policy"] == "exclude"


def test_cron_source_never_abandons_pending_confirmation() -> None:
    session = _pending_session()
    pending_before = dict(session._pending or {})
    response = asyncio.run(session.chat(
        "scheduled tick",
        hide_user_message=True,
        message_source="cron",
        message_source_ref="cron-safe",
    ))
    assert response.status == "awaiting_confirmation"
    assert session._pending == pending_before
    assert session.history == []
    assert not any(event.get("kind") == "approval_abandoned" for event in session.events)


class _CapturingRuntimeAI:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.tools = None

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        self.messages = list(messages or [])
        self.tools = tools
        return {
            "choices": [{
                "message": {"content": "定时任务已完成。", "tool_calls": []},
            }],
        }


class _FailingRuntimeAI:
    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        raise RuntimeError("callback endpoint unavailable")


class _InspectingRuntimeAI:
    def __init__(self) -> None:
        self.calls = 0
        self.tools = None

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        self.calls += 1
        self.tools = tools
        if self.calls == 1:
            return {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "read-cron-result",
                            "type": "function",
                            "function": {
                                "name": "read_assistant_session",
                                "arguments": json.dumps({
                                    "session_id": "cron-run-123",
                                    "role": "assistant",
                                    "order": "desc",
                                }),
                            },
                        }],
                    },
                }],
            }
        return {
            "choices": [{
                "message": {"content": "核验原会话后，巡检完成且无异常。", "tool_calls": []},
            }],
        }


def test_cron_result_is_system_input_not_user_history() -> None:
    ai = _CapturingRuntimeAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)

    response = asyncio.run(session.chat(
        '{"event_type":"cron_result","status":"completed","summary":"done"}',
        hide_user_message=True,
        message_source="cron_result",
        message_source_ref="cron-1",
        thinking_enabled=False,
    ))

    assert response.text == "定时任务已完成。"
    assert ai.messages[-2]["role"] == "system"
    assert "typed scheduled-task result" in ai.messages[-2]["content"]
    assert ai.messages[-1] == {
        "role": "user",
        "content": "Present the typed runtime event above as the user-facing notification now.",
    }
    assert all(
        not (item["role"] == "user" and "cron_result" in str(item.get("content")))
        for item in ai.messages
    )
    assert [(item.role, item.content) for item in session.history] == [
        ("assistant", "定时任务已完成。"),
    ]


def test_cron_result_projection_excludes_unrelated_conversation_state() -> None:
    ai = _CapturingRuntimeAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.history.extend([
        ChatMessage(role="user", content="删除旧任务"),
        ChatMessage(role="assistant", content="已删除旧任务 cron-old"),
    ])

    response = asyncio.run(session.chat(
        '{"event_type":"cron_result","status":"completed","summary":"new patrol finished"}',
        hide_user_message=True,
        message_source="cron_result",
        message_source_ref="cron-new",
        thinking_enabled=False,
    ))

    serialized = json.dumps(ai.messages, ensure_ascii=False)
    assert response.text == "定时任务已完成。"
    assert len(ai.messages) == 3
    assert ai.tools is None
    assert "已删除旧任务" not in serialized
    assert "new patrol finished" in serialized
    assert "Simplified Chinese" in serialized
    assert session.history[-1].content == "定时任务已完成。"


def test_cron_result_may_read_only_linked_session_when_summary_is_insufficient() -> None:
    registry = ToolRegistry()
    registry.register(
        "read_assistant_session",
        "Read one assistant session",
        {"session_id": {"type": "string"}},
        lambda session_id="", **_kwargs: {
            "ok": True,
            "session_id": session_id,
            "messages": [{"role": "assistant", "content": "patrol completed; no findings"}],
        },
        required=["session_id"],
        risk="low",
    )
    ai = _InspectingRuntimeAI()
    session = AssistantSession(registry=registry, ai_provider=ai)

    response = asyncio.run(session.chat(
        json.dumps({
            "event_type": "cron_result",
            "status": "completed",
            "run_session_id": "cron-run-123",
            "summary": "patrol...",
            "summary_truncated": True,
        }),
        hide_user_message=True,
        message_source="cron_result",
        message_source_ref="cron-job-1",
        thinking_enabled=False,
    ))

    assert response.text == "核验原会话后，巡检完成且无异常。"
    assert ai.calls == 2
    assert ai.tools is not None
    assert [tool["function"]["name"] for tool in ai.tools] == ["read_assistant_session"]
    assert response.tool_trace[-1]["name"] == "read_assistant_session"


def test_cron_result_ai_failure_is_typed_and_not_persisted() -> None:
    session = AssistantSession(
        registry=ToolRegistry(),
        ai_provider=_FailingRuntimeAI(),
    )

    response = asyncio.run(session.chat(
        '{"event_type":"cron_result","status":"completed","summary":"patrol done"}',
        hide_user_message=True,
        message_source="cron_result",
        message_source_ref="cron-fallback",
        thinking_enabled=False,
    ))

    assert response.status == "ai_error"
    assert "callback endpoint unavailable" in response.text
    assert session.history == []


def test_cron_context_is_labeled_as_frozen_task_input() -> None:
    ai = _CapturingRuntimeAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)

    asyncio.run(session.chat(
        "inspect target",
        context={"target_url": "https://example.test"},
        hide_user_message=True,
        message_source="cron",
        message_source_ref="cron-context",
        thinking_enabled=False,
    ))

    serialized = json.dumps(ai.messages, ensure_ascii=False)
    assert "Scheduled Task Context" in serialized
    assert "explicit context snapshot stored with the cron job" in serialized
    assert "This is the screen the user is currently looking at" not in serialized
