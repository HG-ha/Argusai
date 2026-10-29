"""FallbackProvider should pool sub-providers (and their httpx clients).

A single agent turn issues many LLM calls (one per round). Before pooling,
every call rebuilt the sub-provider and its httpx.AsyncClient, paying a fresh
TCP+TLS handshake each round. These tests lock in that:

1. multiple calls within one FallbackProvider reuse a single sub-provider, and
2. close() releases the cached sub-providers exactly once and clears the cache.
"""

import asyncio
from types import SimpleNamespace

from argusai.core.ai import AIProvider, FallbackProvider


class _StubProvider(AIProvider):
    def __init__(self) -> None:
        self.chat_calls = 0
        self.analyze_calls = 0
        self.close_calls = 0

    async def chat(self, messages, **_kw):
        self.chat_calls += 1
        return {"choices": [{"message": {"content": "ok"}}]}

    async def stream_chat(self, messages, **_kw):
        self.chat_calls += 1
        return {"choices": [{"message": {"content": "ok"}}]}

    async def analyze(self, system_prompt, user_content):
        self.analyze_calls += 1
        return "ok"

    async def close(self) -> None:
        self.close_calls += 1


def _endpoint() -> SimpleNamespace:
    return SimpleNamespace(enabled=True, priority=1, name="stub", base_url="http://x")


def test_subprovider_is_built_once_and_reused_across_calls():
    fp = FallbackProvider([_endpoint()])
    stub = _StubProvider()
    built = {"n": 0}

    def _make(_ep):
        built["n"] += 1
        return stub

    fp._make_provider = _make  # type: ignore[method-assign]

    async def run():
        await fp.chat([{"role": "user", "content": "1"}])
        await fp.chat([{"role": "user", "content": "2"}])
        await fp.analyze("sys", "3")
        await fp.close()

    asyncio.run(run())

    assert built["n"] == 1, "sub-provider should be built once and cached"
    assert stub.chat_calls == 2
    assert stub.analyze_calls == 1
    assert stub.close_calls == 1, "close() should release the cached sub-provider"


def test_close_is_idempotent_and_clears_cache():
    fp = FallbackProvider([_endpoint()])
    stub = _StubProvider()
    fp._make_provider = lambda _ep: stub  # type: ignore[method-assign]

    async def run():
        await fp.chat([{"role": "user", "content": "1"}])
        await fp.close()
        await fp.close()  # second close: cache already empty, must not re-close

    asyncio.run(run())

    assert stub.close_calls == 1
    assert fp._provider_cache == {}
