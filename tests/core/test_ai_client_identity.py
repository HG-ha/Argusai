"""Upstream LLM requests identify ArgusAI instead of leaking python-httpx."""

from __future__ import annotations

from argusai import __version__
from argusai.core.ai import (
    AIConfig,
    AnthropicProvider,
    OpenAICompatibleProvider,
    default_ai_client_headers,
    merge_ai_headers,
)


def test_default_headers_identify_argusai() -> None:
    headers = default_ai_client_headers()
    ua = headers["User-Agent"]
    assert ua.startswith(f"ArgusAI/{__version__} ")
    assert "Python/" in ua
    assert "python-httpx" not in ua.lower()
    assert headers["HTTP-Referer"] == "https://argusai.local"
    assert headers["X-Title"] == "ArgusAI"
    assert headers["X-OpenRouter-Title"] == "ArgusAI"


def test_merge_ai_headers_is_case_insensitive_and_later_wins() -> None:
    merged = merge_ai_headers(
        {"User-Agent": "ArgusAI/0", "X-Title": "ArgusAI"},
        {"user-agent": "custom-ua", "X-Debug": "1"},
    )
    assert merged["user-agent"] == "custom-ua"
    assert "User-Agent" not in merged
    assert merged["X-Title"] == "ArgusAI"
    assert merged["X-Debug"] == "1"


def test_openai_provider_sends_client_identity() -> None:
    provider = OpenAICompatibleProvider(AIConfig(api_key="sk-test"))
    headers = provider._client.headers
    assert str(headers.get("user-agent") or "").startswith("ArgusAI/")
    assert headers.get("x-title") == "ArgusAI"
    assert headers.get("http-referer") == "https://argusai.local"
    assert headers.get("authorization") == "Bearer sk-test"


def test_anthropic_provider_sends_client_identity() -> None:
    provider = AnthropicProvider(AIConfig(protocol="anthropic", api_key="sk-ant"))
    headers = provider._client.headers
    assert str(headers.get("user-agent") or "").startswith("ArgusAI/")
    assert headers.get("x-api-key") == "sk-ant"
    assert headers.get("anthropic-version")


def test_extra_headers_override_client_identity() -> None:
    provider = OpenAICompatibleProvider(
        AIConfig(
            extra_headers={
                "User-Agent": "custom-agent/1",
                "X-Title": "Mine",
            }
        )
    )
    headers = provider._client.headers
    assert headers.get("user-agent") == "custom-agent/1"
    assert headers.get("x-title") == "Mine"
    assert headers.get("http-referer") == "https://argusai.local"
    assert headers.get("x-openrouter-title") == "ArgusAI"
