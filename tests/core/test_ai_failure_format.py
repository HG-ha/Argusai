"""User-facing rendering of AI provider failures (format_ai_failure)."""

from __future__ import annotations

import httpx
import pytest

from argusai.core.ai import format_ai_failure


def _http_status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")
    response = httpx.Response(status_code=status, request=request, content=body.encode("utf-8"))
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _wrap_fallback(status: int, body: str, *, endpoint_count: int = 1) -> RuntimeError:
    inner = _http_status_error(status, body)
    err = RuntimeError(f"All {endpoint_count} AI endpoints failed: HTTP {status}: {body}")
    err.__cause__ = inner
    return err


def test_format_402_insufficient_balance_is_actionable() -> None:
    exc = _wrap_fallback(402, '{"error":{"message":"Insufficient Balance","type":"unknown_error"}}')

    out = format_ai_failure(exc)

    assert "HTTP 402" in out
    assert "余额不足" in out
    assert "设置" in out
    assert "Insufficient Balance" in out
    assert "重新生成" in out
    # Tell the user that pure regenerate without topping up won't help.
    assert "充值" in out or "换端点" in out or "添加另一个" in out
    # Mention how many endpoints we tried — important so the user knows
    # fallback already kicked in (or didn't, because they only have 1).
    assert "1" in out


def test_format_401_unauthorized_points_at_settings() -> None:
    exc = _wrap_fallback(401, '{"error":{"message":"Invalid API Key"}}')
    out = format_ai_failure(exc)
    assert "401" in out
    assert "API key" in out or "鉴权" in out
    assert "设置" in out


def test_format_429_rate_limit_suggests_wait() -> None:
    exc = _wrap_fallback(429, '{"error":{"message":"rate_limited"}}')
    out = format_ai_failure(exc)
    assert "429" in out
    assert "限流" in out
    # We explicitly tell the user immediate regenerate won't help.
    assert "30" in out or "稍候" in out or "等" in out


def test_format_5xx_generic_server_error() -> None:
    exc = _wrap_fallback(503, "Service Unavailable")
    out = format_ai_failure(exc)
    assert "503" in out
    assert "服务端" in out or "服务" in out
    assert "设置" in out or "备用" in out


def test_format_connect_error_is_network_branch() -> None:
    exc = httpx.ConnectError("Cannot connect")
    out = format_ai_failure(exc)
    assert "无法连接" in out or "网络" in out
    assert "ConnectError" in out
    # Should not pretend HTTP status is involved.
    assert "HTTP " not in out


def test_format_none_returns_actionable_default() -> None:
    out = format_ai_failure(None)
    assert "设置" in out
    assert "未知" in out


def test_format_extracts_message_from_openai_style_body() -> None:
    body = '{"error":{"message":"Quota exceeded","code":"insufficient_quota"}}'
    exc = _wrap_fallback(429, body)
    out = format_ai_failure(exc)
    assert "Quota exceeded" in out
    # Should NOT dump the raw JSON wrapper.
    assert '"error":{' not in out
