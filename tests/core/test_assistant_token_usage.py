"""Session-level cache/token-usage accumulation (Token 消耗明细 panel)."""

from __future__ import annotations

from argusai.core.assistant import AssistantSession
from argusai.core.assistant.session import _TOKEN_USAGE_FIELDS
from argusai.core.assistant.tools import ToolRegistry


def _usage(**over):
    base = {
        "input_tokens": 100,
        "cache_hit_tokens": 80,
        "cache_miss_tokens": 20,
        "cache_write_tokens": 0,
        "output_tokens": 30,
        "reasoning_tokens": 5,
        "reply_tokens": 25,
    }
    base.update(over)
    return base


def test_record_token_usage_accumulates_across_calls():
    session = AssistantSession()
    assert session.token_usage_total == dict.fromkeys(_TOKEN_USAGE_FIELDS, 0)
    assert session.cache_hit_rate() is None

    session._record_token_usage(_usage())
    session._record_token_usage(_usage(input_tokens=200, cache_hit_tokens=190, cache_miss_tokens=10))

    assert session.token_usage_total["input_tokens"] == 300
    assert session.token_usage_total["cache_hit_tokens"] == 270
    assert session.token_usage_total["cache_miss_tokens"] == 30
    assert session.token_usage_total["output_tokens"] == 60
    assert session.token_usage_total["calls"] == 2
    assert session.last_token_usage["input_tokens"] == 200
    assert session.cache_hit_rate() == 90.0


def test_record_token_usage_ignores_none():
    session = AssistantSession()
    session._record_token_usage(None)
    assert session.token_usage_total == dict.fromkeys(_TOKEN_USAGE_FIELDS, 0)
    assert session.last_token_usage is None


def test_token_usage_total_survives_round_trip():
    registry = ToolRegistry()
    session = AssistantSession(registry=registry)
    session._record_token_usage(_usage())
    session._record_token_usage(_usage())

    restored = AssistantSession.from_dict(session.to_dict(), registry)
    assert restored.token_usage_total == session.token_usage_total
    assert restored.cache_hit_rate() == session.cache_hit_rate()
    # Persisted so a reopened session can show its last context usage.
    assert restored.last_token_usage == session.last_token_usage


def test_turn_token_usage_accumulates_then_resets_per_turn():
    """The per-message 'Token' button needs THIS turn's cost, not the whole
    session's running total — _turn_token_usage must reset each ``chat()``
    call and _turn_token_usage_snapshot() must reflect only what happened
    since that reset."""
    session = AssistantSession()
    assert session._turn_token_usage_snapshot() is None

    session._record_token_usage(_usage(input_tokens=100))
    session._record_token_usage(_usage(input_tokens=50))
    snap = session._turn_token_usage_snapshot()
    assert snap["input_tokens"] == 150
    assert snap["calls"] == 2
    # Session-wide total keeps accumulating independently of the per-turn one.
    assert session.token_usage_total["input_tokens"] == 150

    # Simulate the reset chat() does at the top of every turn.
    session._turn_token_usage = {}
    assert session._turn_token_usage_snapshot() is None
    session._record_token_usage(_usage(input_tokens=10))
    assert session._turn_token_usage_snapshot()["input_tokens"] == 10
    # Prior turns' cost is untouched in the session-wide total.
    assert session.token_usage_total["input_tokens"] == 160


def test_chat_message_token_usage_round_trips():
    from argusai.core.assistant.messages import ChatMessage

    msg = ChatMessage(role="assistant", content="done", token_usage=_usage())
    restored = ChatMessage.from_dict(msg.to_dict())
    assert restored.token_usage == _usage()

    # A message with no usage attached must not fabricate one on reload.
    plain = ChatMessage.from_dict(ChatMessage(role="assistant", content="hi").to_dict())
    assert plain.token_usage is None


class _FakeAIWithCacheUsage:
    """Minimal AI stub whose ``chat()`` return matches ai.py's real shape:
    ``choices`` plus a top-level ``_cache_usage`` the providers attach."""

    def __init__(self, usage: dict) -> None:
        self._usage = usage

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        return {
            "choices": [{"message": {"content": "done", "tool_calls": []}}],
            "_cache_usage": dict(self._usage),
        }


def test_chat_attaches_turn_usage_to_the_final_reply(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from argusai.core.assistant.tools import ToolRegistry

    settings = SimpleNamespace(
        sessions_dir=str(tmp_path),
        ai_enabled=True,
        ai_endpoints=[SimpleNamespace(enabled=True, name="test", model="test-model")],
    )
    api = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))
    registry = ToolRegistry(api=api, session_id="s1")
    usage = _usage(input_tokens=42)
    session = AssistantSession(registry=registry, ai_provider=_FakeAIWithCacheUsage(usage))

    resp = asyncio.run(session.chat("你好"))

    assert resp.text == "done"
    reply = session.history[-1]
    assert reply.role == "assistant"
    assert reply.token_usage == {**usage, "calls": 1}
    # The session-wide running total picked it up too.
    assert session.token_usage_total["input_tokens"] == 42

