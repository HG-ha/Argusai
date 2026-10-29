"""Anthropic API-level refusals must surface as visible assistant text."""

from __future__ import annotations

import asyncio
import json

import httpx

from argusai.core.ai import AIConfig, AnthropicProvider, _format_anthropic_refusal


def test_format_anthropic_refusal_message() -> None:
    text = _format_anthropic_refusal(
        "refusal",
        {
            "type": "refusal",
            "category": "cyber",
            "explanation": "blocked under Anthropic's Usage Policy",
        },
    )
    assert "[模型拒绝回复]" in text
    assert "cyber" in text
    assert "Usage Policy" in text
    assert _format_anthropic_refusal("end_turn", None) == ""


def test_stream_chat_surfaces_refusal_without_content_blocks() -> None:
    sse = "\n".join(
        [
            "event: message_start",
            'data: {"type":"message_start","message":{"role":"assistant","content":[]}}',
            "",
            "event: message_delta",
            (
                "data: "
                + json.dumps(
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "refusal",
                            "stop_details": {
                                "type": "refusal",
                                "category": "cyber",
                                "explanation": "violative cyber content",
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            ),
            "",
            "event: message_stop",
            'data: {"type":"message_stop"}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse.encode("utf-8"),
        )

    deltas: list[str] = []

    async def run() -> dict:
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await provider.stream_chat(
                [{"role": "user", "content": "hi"}],
                on_delta=lambda t: deltas.append(t),
            )
        finally:
            await provider.close()

    result = asyncio.run(run())
    text = result["choices"][0]["message"]["content"]
    assert "[模型拒绝回复]" in text
    assert "cyber" in text
    assert deltas and "[模型拒绝回复]" in deltas[0]


def test_chat_surfaces_refusal_from_nonstream_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {
                    "type": "refusal",
                    "category": "cyber",
                    "explanation": "blocked",
                },
            },
        )

    async def run() -> dict:
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            return await provider.chat([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    result = asyncio.run(run())
    assert "cyber" in result["choices"][0]["message"]["content"]


def test_tool_history_without_thinking_disables_endpoint_default() -> None:
    provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
    body: dict = {}
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "check", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
        },
    ]

    provider._disable_thinking_for_tool_context(body, messages)

    assert body["thinking"] == {"type": "disabled"}
    asyncio.run(provider.close())


def test_tool_history_replays_complete_thinking_blocks() -> None:
    provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=1024))
    body: dict = {}
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "reason", "signature": "sig"},
                {"type": "tool_use", "id": "call-1", "name": "check", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
        },
    ]

    provider._disable_thinking_for_tool_context(body, messages)

    assert "thinking" not in body
    asyncio.run(provider.close())
