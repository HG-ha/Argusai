"""OpenAI-compatible explicit prompt cache (Qwen / OpenAI 5.6+ / OpenRouter Gemini)."""

from __future__ import annotations

import asyncio
import json

import httpx

from argusai.core.ai import (
    AIConfig,
    OpenAICompatibleProvider,
    _apply_openai_explicit_cache_control,
    _apply_openai_explicit_cache_markers,
    _openai_explicit_cache_enabled,
    _openai_explicit_cache_flavor,
    _openai_model_supports_prompt_cache_breakpoint,
    _CACHE_FLAVOR_CONTROL,
    _CACHE_FLAVOR_OPENAI,
)


def test_model_supports_prompt_cache_breakpoint():
    assert _openai_model_supports_prompt_cache_breakpoint("gpt-5.6")
    assert _openai_model_supports_prompt_cache_breakpoint("openai/gpt-5.6-pro")
    assert _openai_model_supports_prompt_cache_breakpoint("gpt-6")
    assert not _openai_model_supports_prompt_cache_breakpoint("gpt-5.5")
    assert not _openai_model_supports_prompt_cache_breakpoint("gpt-4o")


def test_flavor_auto_qwen_openai_gemini_openrouter():
    assert _openai_explicit_cache_flavor(
        AIConfig(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3.8-max")
    ) == _CACHE_FLAVOR_CONTROL
    assert _openai_explicit_cache_flavor(
        AIConfig(base_url="https://api.openai.com/v1", model="gpt-5.6")
    ) == _CACHE_FLAVOR_OPENAI
    assert _openai_explicit_cache_flavor(
        AIConfig(base_url="https://openrouter.ai/api/v1", model="google/gemini-3.6-flash")
    ) == _CACHE_FLAVOR_CONTROL
    # Google native Gemini: implicit only — do not inject markers that may 400.
    assert _openai_explicit_cache_flavor(
        AIConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.6-flash",
        )
    ) is None
    assert _openai_explicit_cache_flavor(
        AIConfig(base_url="https://api.openai.com/v1", model="gpt-5.5")
    ) is None


def test_flavor_extra_body_override():
    assert _openai_explicit_cache_flavor(
        AIConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.8-max",
            extra_body={"explicit_cache": False},
        )
    ) is None
    assert _openai_explicit_cache_flavor(
        AIConfig(
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            extra_body={"explicit_cache": "openai"},
        )
    ) == _CACHE_FLAVOR_OPENAI
    assert _openai_explicit_cache_flavor(
        AIConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model="gemini-3.6-flash",
            extra_body={"explicit_cache": "cache_control"},
        )
    ) == _CACHE_FLAVOR_CONTROL
    assert _openai_explicit_cache_enabled(
        AIConfig(base_url="https://api.openai.com/v1", model="gpt-5.6")
    )


def test_apply_marks_system_and_tail_cache_control():
    messages = [
        {"role": "system", "content": "You are ArgusAI."},
        {"role": "user", "content": "step 1"},
        {"role": "assistant", "content": "doing 1"},
        {"role": "user", "content": "step 2"},
        {"role": "assistant", "content": "doing 2"},
        {"role": "user", "content": "live"},
    ]
    marked = _apply_openai_explicit_cache_control(messages, max_breakpoints=4)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    marked_idx = [
        i for i, m in enumerate(marked)
        if isinstance(m.get("content"), list) and "cache_control" in m["content"][-1]
    ]
    assert marked_idx == [0, 3, 4, 5]
    assert isinstance(messages[0]["content"], str)


def test_apply_openai_prompt_cache_breakpoint_shape():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    marked = _apply_openai_explicit_cache_markers(
        messages, flavor=_CACHE_FLAVOR_OPENAI, max_breakpoints=4
    )
    assert marked[0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "cache_control" not in marked[0]["content"][0]


def _capture(bodies: list[dict], *, usage: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": usage or {
                    "prompt_tokens": 2000,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {
                        "cached_tokens": 1600,
                        "cache_write_tokens": 40,
                    },
                },
            },
        )
    return handler


def test_chat_injects_cache_control_for_dashscope():
    captured: list[dict] = []

    async def run():
        provider = OpenAICompatibleProvider(
            AIConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen3.8-max-preview",
                max_tokens=1024,
                extra_body={"explicit_cache": True, "replay_reasoning_content": True},
            )
        )
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture(captured)),
        )
        try:
            data = await provider.chat([
                {"role": "system", "content": "sys " * 200},
                {"role": "user", "content": "hi"},
            ])
        finally:
            await provider.close()
        return data

    data = asyncio.run(run())
    body = captured[0]
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "prompt_cache_options" not in body
    assert "explicit_cache" not in body
    assert "replay_reasoning_content" not in body
    assert data["_cache_usage"]["cache_hit_tokens"] == 1600
    assert data["_cache_usage"]["cache_write_tokens"] == 40


def test_chat_injects_prompt_cache_breakpoint_for_gpt56():
    captured: list[dict] = []

    async def run():
        provider = OpenAICompatibleProvider(
            AIConfig(
                base_url="https://api.openai.com/v1",
                model="gpt-5.6",
                max_tokens=1024,
            )
        )
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture(captured)),
        )
        try:
            await provider.chat([
                {"role": "system", "content": "sys " * 200},
                {"role": "user", "content": "hi"},
            ])
        finally:
            await provider.close()

    asyncio.run(run())
    body = captured[0]
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert body["messages"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "cache_control" not in body["messages"][0]["content"][0]


def test_google_extra_body_nests_google_block():
    captured: list[dict] = []

    async def run():
        provider = OpenAICompatibleProvider(
            AIConfig(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                model="gemini-3.6-flash",
                max_tokens=1024,
                extra_body={
                    "google": {
                        "cached_content": "cachedContents/abc123",
                    }
                },
            )
        )
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture(captured)),
        )
        try:
            await provider.chat([{"role": "user", "content": "hi"}])
        finally:
            await provider.close()

    asyncio.run(run())
    body = captured[0]
    assert "google" not in body
    assert body["extra_body"]["google"]["cached_content"] == "cachedContents/abc123"
    # No accidental cache_control on Google native.
    content = body["messages"][0]["content"]
    assert isinstance(content, str) or "cache_control" not in (content[0] if content else {})


def test_chat_skips_cache_control_when_disabled():
    captured: list[dict] = []

    async def run():
        provider = OpenAICompatibleProvider(
            AIConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen3.8-max",
                max_tokens=1024,
                extra_body={"explicit_cache": False},
            )
        )
        provider._client = httpx.AsyncClient(
            base_url="https://api.test",
            transport=httpx.MockTransport(_capture(captured)),
        )
        try:
            await provider.chat([
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ])
        finally:
            await provider.close()

    asyncio.run(run())
    content = captured[0]["messages"][0]["content"]
    assert isinstance(content, str) or (
        isinstance(content, list) and "cache_control" not in content[0]
    )
