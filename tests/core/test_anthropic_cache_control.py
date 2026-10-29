"""Anthropic multi-breakpoint prompt caching (Hermes Agent's ``system_and_3``
layout — https://github.com/NousResearch/hermes-agent).

Covers the P2 gap identified while auditing ArgusAI's cache-hit rate: the
Anthropic path only ever marked the system prompt with a ``cache_control``
breakpoint, leaving the other 3 of Anthropic's 4 allowed breakpoints unused —
so a growing multi-turn conversation never cached anything beyond the system
prefix. This only matters for Anthropic-format endpoints (native Claude, or
an Anthropic-compatible gateway); DeepSeek's OpenAI-compatible endpoint caches
automatically and ignores ``cache_control`` entirely.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from argusai.core.ai import (
    AIConfig,
    AnthropicProvider,
    _apply_anthropic_cache_control,
    _apply_anthropic_content_marker,
    _can_carry_anthropic_cache_marker,
)


def test_can_carry_marker_requires_nonempty_content():
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": "hi"}) is True
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": [{"type": "text", "text": "hi"}]}) is True
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": ""}) is False
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": None}) is False
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": []}) is False
    # A list whose last element isn't a dict has nowhere to attach the marker.
    assert _can_carry_anthropic_cache_marker({"role": "user", "content": ["raw-string-part"]}) is False


def test_apply_content_marker_on_string_wraps_as_text_block():
    msg = {"role": "user", "content": "hello"}
    _apply_anthropic_content_marker(msg, {"type": "ephemeral"})
    assert msg["content"] == [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]


def test_apply_content_marker_on_list_marks_last_block_only():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {}},
        ],
    }
    _apply_anthropic_content_marker(msg, {"type": "ephemeral"})
    assert "cache_control" not in msg["content"][0]
    assert msg["content"][1]["cache_control"] == {"type": "ephemeral"}


def test_cache_control_marks_only_the_last_n_messages():
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"} for i in range(8)]
    marked = _apply_anthropic_cache_control(messages, max_breakpoints=3)

    marked_indexes = [
        i for i, m in enumerate(marked)
        if isinstance(m.get("content"), list) and "cache_control" in m["content"][-1]
    ]
    assert marked_indexes == [5, 6, 7]


def test_cache_control_never_mutates_caller_messages():
    original = [{"role": "user", "content": "hi"}]
    marked = _apply_anthropic_cache_control(original)
    assert "cache_control" not in original[0].get("content", "")
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"]


def test_cache_control_respects_1h_ttl():
    marked = _apply_anthropic_cache_control([{"role": "user", "content": "hi"}], cache_ttl="1h")
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_cache_control_disabled_returns_input_unchanged():
    original = [{"role": "user", "content": "hi"}]
    assert _apply_anthropic_cache_control(original, max_breakpoints=0) is original
    assert _apply_anthropic_cache_control([]) == []


def _capture_request_body(monkeypatch_holder: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        monkeypatch_holder.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    return handler


def test_chat_request_carries_system_plus_tail_breakpoints():
    captured: list[dict] = []

    async def run():
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture_request_body(captured)),
        )
        try:
            await provider.chat([
                {"role": "system", "content": "You are ArgusAI."},
                {"role": "user", "content": "step 1"},
                {"role": "assistant", "content": "doing step 1"},
                {"role": "user", "content": "step 2"},
                {"role": "assistant", "content": "doing step 2"},
                {"role": "user", "content": "step 3 — the live turn"},
            ])
        finally:
            await provider.close()

    asyncio.run(run())
    body = captured[0]

    # System gets its own breakpoint.
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}

    # Exactly 3 of the non-system messages are marked (the most recent 3),
    # not the oldest ones — an unmarked earlier turn would mean the model's
    # first exchange is wastefully re-marked forever while the live tail
    # (which actually changes every request) isn't covered.
    sent = body["messages"]
    assert len(sent) == 5
    marked = [i for i, m in enumerate(sent) if "cache_control" in m["content"][-1]]
    assert marked == [2, 3, 4]
    assert "cache_control" not in sent[0]["content"][-1]
    assert "cache_control" not in sent[1]["content"][-1]


def test_stream_chat_request_also_carries_breakpoints():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        sse = (
            "event: message_start\n"
            'data: {"type":"message_start","message":{"role":"assistant","content":[],'
            '"usage":{"input_tokens":10,"cache_read_input_tokens":0,"cache_creation_input_tokens":5}}}\n\n'
            "event: message_delta\n"
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n\n'
            "event: message_stop\n"
            'data: {"type":"message_stop"}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode("utf-8"))

    async def run():
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await provider.stream_chat([
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ])
        finally:
            await provider.close()

    result = asyncio.run(run())
    body = captured[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # message_start + message_delta usage merges into one normalized record.
    assert result["_cache_usage"] == {
        "input_tokens": 15,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 10,
        "cache_write_tokens": 5,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "reply_tokens": 3,
    }


def test_tool_use_only_assistant_turn_can_still_carry_a_marker():
    """A round where the model only called a tool (no text) must not be
    skipped — Anthropic honors cache_control on any content block type, and
    this codebase always sends Anthropic's native block-list shape (never
    OpenRouter's top-level-marker envelope), so there is no format carve-out
    to worry about here."""
    captured: list[dict] = []

    async def run():
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture_request_body(captured)),
        )
        try:
            await provider.chat([
                {"role": "user", "content": "read the config file"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "t1", "content": "file contents here"},
            ])
        finally:
            await provider.close()

    asyncio.run(run())
    sent = captured[0]["messages"]
    # assistant(tool_use) + user(tool_result) merge is NOT expected here since
    # roles differ (assistant vs user); the last two eligible messages both
    # get marked within the 3-breakpoint budget.
    assert sent[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sent[-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
