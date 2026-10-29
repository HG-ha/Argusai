"""Same-endpoint retry before multi-endpoint failover."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from argusai.core import ai as ai_mod
from argusai.core.ai import AIProvider, FallbackProvider, _is_retryable_ai_error


class _FlakyProvider(AIProvider):
    def __init__(self, errors: list[BaseException], result: dict | None = None) -> None:
        self.errors = list(errors)
        self.chat_calls = 0
        self.stream_calls = 0
        self.result = result or {"choices": [{"message": {"content": "ok"}}]}

    async def chat(self, messages, **_kw):
        self.chat_calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result

    async def stream_chat(self, messages, **_kw):
        self.stream_calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result

    async def analyze(self, system_prompt, user_content):
        return (await self.chat([{"role": "user", "content": user_content}]))["choices"][0]["message"]["content"]

    async def close(self) -> None:
        return None


def _endpoint(name: str = "primary", priority: int = 10) -> SimpleNamespace:
    return SimpleNamespace(enabled=True, priority=priority, name=name, base_url=f"http://{name}")


def _http_status(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://example/v1/chat/completions")
    response = httpx.Response(code, request=request, text=f'{{"error":"status {code}"}}')
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (httpx.ReadTimeout("slow", request=httpx.Request("POST", "http://x")), True),
        (httpx.ConnectError("down", request=httpx.Request("POST", "http://x")), True),
        (_http_status(429), True),
        (_http_status(503), True),
        (_http_status(401), False),
        (_http_status(402), False),
        (_http_status(404), False),
        (_http_status(400), False),
    ],
)
def test_is_retryable_ai_error(exc: BaseException, retryable: bool) -> None:
    assert _is_retryable_ai_error(exc) is retryable


def test_same_endpoint_retries_timeout_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _fake_sleep)

    stub = _FlakyProvider([
        httpx.ReadTimeout("t1", request=httpx.Request("POST", "http://x")),
        httpx.ReadTimeout("t2", request=httpx.Request("POST", "http://x")),
    ])
    fp = FallbackProvider([_endpoint()])
    fp._make_provider = lambda _ep: stub  # type: ignore[method-assign]

    async def run():
        return await fp.chat([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert stub.chat_calls == 3
    assert sleeps == [0.5, 1.0]


def test_permanent_4xx_skips_same_endpoint_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _fake_sleep)

    primary = _FlakyProvider([_http_status(401)])
    secondary = _FlakyProvider([])
    eps = [_endpoint("a", 10), _endpoint("b", 1)]
    providers = {"a": primary, "b": secondary}
    fp = FallbackProvider(eps)
    fp._make_provider = lambda ep: providers[ep.name]  # type: ignore[method-assign]

    async def run():
        return await fp.chat([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert primary.chat_calls == 1
    assert secondary.chat_calls == 1
    assert sleeps == []


def test_exhausted_retries_fail_over_to_next_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _fake_sleep)

    timeout = httpx.ReadTimeout("t", request=httpx.Request("POST", "http://x"))
    primary = _FlakyProvider([timeout, timeout, timeout])
    secondary = _FlakyProvider([])
    eps = [_endpoint("a", 10), _endpoint("b", 1)]
    providers = {"a": primary, "b": secondary}
    fp = FallbackProvider(eps)
    fp._make_provider = lambda ep: providers[ep.name]  # type: ignore[method-assign]

    async def run():
        return await fp.chat([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert primary.chat_calls == 3
    assert secondary.chat_calls == 1


def test_stream_retries_transient_before_nonstream_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_mod.asyncio, "sleep", _fake_sleep)

    class _StreamFlaky(_FlakyProvider):
        async def stream_chat(self, messages, **_kw):
            self.stream_calls += 1
            if self.stream_calls < 3:
                raise httpx.ReadTimeout("slow", request=httpx.Request("POST", "http://x"))
            return self.result

        async def chat(self, messages, **_kw):
            self.chat_calls += 1
            raise AssertionError("non-stream fallback should not run when stream recovers")

    stub = _StreamFlaky([])
    fp = FallbackProvider([_endpoint()])
    fp._make_provider = lambda _ep: stub  # type: ignore[method-assign]

    async def run():
        return await fp.stream_chat([{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert stub.stream_calls == 3
    assert stub.chat_calls == 0
