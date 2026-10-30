"""Persistent per-session context supplied from the Agent tab editor."""

from __future__ import annotations

import asyncio

from argusai.core.assistant import AssistantSession
from argusai.core.assistant.session import MAX_CONTEXT_NOTES_CHARS
from argusai.core.assistant.tools import ToolRegistry


class _CapturingAI:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        self.messages = list(messages or [])
        return {"choices": [{"message": {"content": "done", "tool_calls": []}}]}


def test_context_notes_serialize_with_a_bounded_length() -> None:
    session = AssistantSession()
    session.set_context_notes("x" * (MAX_CONTEXT_NOTES_CHARS + 20))

    dumped = session.to_dict()
    restored = AssistantSession.from_dict(dumped, session.registry)

    assert len(dumped["context_notes"]) == MAX_CONTEXT_NOTES_CHARS
    assert restored.context_notes == dumped["context_notes"]


def test_context_notes_are_attached_as_user_context_not_system_authority() -> None:
    ai = _CapturingAI()
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session.set_context_notes("Use staging.example and retain the baseline ID 42.")

    asyncio.run(session.chat("Continue the review", thinking_enabled=False))

    assert ai.messages[0]["role"] == "system"
    assert "baseline ID 42" not in str(ai.messages[0]["content"])
    assert ai.messages[-1]["role"] == "user"
    assert "Continue the review" in str(ai.messages[-1]["content"])
    assert "Persistent Session Context (user-provided)" in str(ai.messages[-1]["content"])
    assert "baseline ID 42" in str(ai.messages[-1]["content"])
    assert "does not expand authorization" in str(ai.messages[-1]["content"])
