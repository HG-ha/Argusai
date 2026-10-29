"""Tests for self-healing the Anthropic ``max_tokens`` output cap.

When a user (or a stale config) requests more output tokens than the model
allows, the endpoint replies with HTTP 400 and tells us the real cap. We parse
that cap and retry once instead of bubbling the error up to the user.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from argusai.core.ai import (
    AIConfig,
    AnthropicProvider,
    OpenAICompatibleProvider,
    _extract_output_token_cap,
)


def test_extract_output_token_cap_parses_anthropic_400() -> None:
    body = (
        "max_tokens: 131072 > 128000, which is the maximum allowed number of "
        "output tokens for claude-opus-4-7"
    )
    assert _extract_output_token_cap(body, 131072) == 128000


def test_extract_output_token_cap_parses_openai_prose() -> None:
    assert (
        _extract_output_token_cap(
            "max_tokens is too large: 131072. This model supports at most "
            "128000 completion tokens",
            131072,
        )
        == 128000
    )
    assert (
        _extract_output_token_cap(
            "This model's maximum context length is 128000 tokens, however you "
            "requested 131072 tokens",
            131072,
        )
        is None
    )
    assert (
        _extract_output_token_cap("maximum output tokens is 8192", 16384) == 8192
    )


def test_extract_output_token_cap_ignores_non_matching_text() -> None:
    assert _extract_output_token_cap("rate limit exceeded", 16384) is None
    assert _extract_output_token_cap("", 16384) is None
    # Requested already within the cap → nothing to clamp.
    assert _extract_output_token_cap("max_tokens: 8000 > 0", 8000) is None
    # Prose patterns need a requested value to compare against.
    assert _extract_output_token_cap("supports at most 8192 tokens") is None
    # Context overflow must not be treated as an output cap.
    assert (
        _extract_output_token_cap(
            "This model's maximum context length is 8000 tokens",
            16384,
        )
        is None
    )


def test_chat_retries_clamped_after_max_tokens_400() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(int(payload["max_tokens"]))
        if payload["max_tokens"] > 128000:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": (
                            "max_tokens: 131072 > 128000, which is the maximum "
                            "allowed number of output tokens for claude-opus-4-7"
                        ),
                    }
                },
            )
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}]},
        )

    async def run() -> dict:
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=131072))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test", transport=httpx.MockTransport(handler)
        )
        try:
            return await provider.chat([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    result = asyncio.run(run())

    assert seen == [131072, 128000]
    assert result["choices"][0]["message"]["content"] == "ok"


def test_chat_does_not_retry_on_unrelated_400() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            400,
            json={"error": {"message": "bad request: model not found"}},
        )

    async def run() -> None:
        provider = AnthropicProvider(AIConfig(protocol="anthropic", max_tokens=16384))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test", transport=httpx.MockTransport(handler)
        )
        try:
            await provider.chat([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())

    assert len(calls) == 1


def test_openai_chat_retries_clamped_after_max_tokens_400() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(int(payload["max_tokens"]))
        if payload["max_tokens"] > 128000:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "max_tokens is too large: 131072. This model supports "
                            "at most 128000 completion tokens"
                        )
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}}
                ]
            },
        )

    async def run() -> dict:
        provider = OpenAICompatibleProvider(AIConfig(protocol="openai", max_tokens=131072))
        provider._client = httpx.AsyncClient(
            base_url="https://api.test", transport=httpx.MockTransport(handler)
        )
        try:
            return await provider.chat([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    result = asyncio.run(run())

    assert seen == [131072, 128000]
    assert result["choices"][0]["message"]["content"] == "ok"
