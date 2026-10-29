from __future__ import annotations

import asyncio

from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.side_question import (
    answer_side_question,
    build_side_messages,
    transcript_from_history,
)


def _history() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="scan example.com"),
        ChatMessage(
            role="assistant",
            content="Starting the scan.",
            tool_calls=[{
                "id": "call-1",
                "function": {"name": "http_request", "arguments": '{"url": "https://example.com"}'},
            }],
        ),
        ChatMessage(role="tool", content="200 OK", tool_call_id="call-1"),
        ChatMessage(role="user", content="continue", hidden=True),
        # A live turn is usually parked here: a call whose result has not landed.
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{
                "id": "call-2",
                "function": {"name": "browser_evaluate", "arguments": "{}"},
            }],
        ),
    ]


def test_transcript_keeps_the_shape_of_the_conversation() -> None:
    transcript = transcript_from_history(_history())
    assert "User: scan example.com" in transcript
    assert "Assistant: Starting the scan." in transcript
    assert "[tool call] http_request(" in transcript
    assert "[tool result] 200 OK" in transcript
    # Hidden auto-continue prompts are plumbing, not conversation.
    assert "continue" not in transcript
    # The dangling call is flattened to text, so an unmatched tool_use can never
    # reach a provider that would reject the whole request.
    assert "[tool call] browser_evaluate({})" in transcript


def test_transcript_keeps_the_newest_exchanges_within_budget() -> None:
    history = [
        ChatMessage(role="user", content=f"question {index}")
        for index in range(200)
    ]
    transcript = transcript_from_history(history, budget_chars=2000)
    assert "question 199" in transcript
    assert "question 0" not in transcript


def test_prompt_states_the_side_agent_has_no_tools() -> None:
    messages = build_side_messages(_history(), "what did the scan return?")
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "NO tools" in messages[0]["content"]
    assert "main agent is still running" in messages[0]["content"]
    assert "what did the scan return?" in messages[1]["content"]


def test_side_question_never_offers_tools_to_the_provider() -> None:
    seen: dict[str, object] = {}

    class Provider:
        async def chat(self, messages, **kwargs):
            seen["messages"] = messages
            seen.update(kwargs)
            return {"choices": [{"message": {"content": "It returned 200 OK."}}]}

    answer = asyncio.run(
        answer_side_question(Provider(), _history(), "what did the scan return?")
    )
    assert answer == "It returned 200 OK."
    assert seen["tools"] is None
