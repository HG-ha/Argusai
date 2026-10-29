"""OpenAI-compat thinking models must echo prior reasoning_content."""

from __future__ import annotations

import asyncio
import json

import httpx

from argusai.core.ai import (
    AIConfig,
    OpenAICompatibleProvider,
    _openai_should_replay_reasoning_content,
)


def _history() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "ok",
            "reasoning_content": "step by step",
        },
        {"role": "user", "content": "continue"},
    ]


def test_replay_when_thinking_enabled():
    assert _openai_should_replay_reasoning_content({}, True) is True
    provider = OpenAICompatibleProvider(AIConfig(base_url="http://x", model="qwen3-max"))
    converted = provider._convert_messages(_history(), thinking_enabled=True)
    assistant = next(m for m in converted if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "step by step"


def test_omit_when_thinking_disabled():
    assert _openai_should_replay_reasoning_content({}, False) is False
    provider = OpenAICompatibleProvider(AIConfig(base_url="http://x", model="qwen3-max"))
    converted = provider._convert_messages(_history(), thinking_enabled=False)
    assistant = next(m for m in converted if m["role"] == "assistant")
    assert "reasoning_content" not in assistant


def test_default_replays_when_thinking_unset():
    assert _openai_should_replay_reasoning_content({}, None) is True
    provider = OpenAICompatibleProvider(AIConfig(base_url="http://x", model="qwen3-max"))
    converted = provider._convert_messages(_history())
    assistant = next(m for m in converted if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "step by step"


def test_extra_body_can_opt_out_only_when_thinking_unset():
    extra = {"replay_reasoning_content": False}
    assert _openai_should_replay_reasoning_content(extra, None) is False
    assert _openai_should_replay_reasoning_content(extra, True) is True
    provider = OpenAICompatibleProvider(
        AIConfig(base_url="http://x", model="qwen3-max", extra_body=extra)
    )
    converted = provider._convert_messages(_history(), thinking_enabled=None)
    assistant = next(m for m in converted if m["role"] == "assistant")
    assert "reasoning_content" not in assistant
    converted_on = provider._convert_messages(_history(), thinking_enabled=True)
    assistant_on = next(m for m in converted_on if m["role"] == "assistant")
    assert assistant_on["reasoning_content"] == "step by step"


def test_chat_sends_reasoning_content_when_thinking_enabled():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    async def run():
        provider = OpenAICompatibleProvider(
            AIConfig(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3-max")
        )
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            await provider.chat(_history(), thinking_enabled=True)
        finally:
            await provider.close()

    asyncio.run(run())
    assistant = next(m for m in captured[0]["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "step by step"
    assert captured[0]["thinking"] == {"type": "enabled"}
