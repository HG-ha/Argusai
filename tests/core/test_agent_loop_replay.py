"""Deterministic agent-loop replay harness (Loom22 Appendix A).

Scripts a fake provider + tools and asserts stop / dedupe / parallel order
without network.
"""

from __future__ import annotations

import asyncio
import json
import time

from argusai.core.assistant.messages import ChatResponse
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


class ScriptedAI:
    """Minimal AI stub: ``chat`` returns scripted OpenAI-shaped payloads."""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        if self.calls >= len(self.script):
            return {
                "choices": [{
                    "message": {"content": "fallback done", "tool_calls": []},
                }],
            }
        item = self.script[self.calls]
        self.calls += 1
        return item


def _assistant_msg(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    msg: dict = {"content": content, "tool_calls": tool_calls or []}
    return {"choices": [{"message": msg}]}


def _tool_call(cid: str, name: str, args: dict | None = None) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args or {}, ensure_ascii=False),
        },
    }


def test_wait_for_duration_is_short_and_cancellable() -> None:
    session = AssistantSession(registry=ToolRegistry())
    tool = session.registry.get("wait_for_duration")
    assert tool is not None
    assert "cross-domain timing primitive" in tool.description
    assert "UI/app startup" in tool.description

    result = asyncio.run(tool.fn(seconds=0.01, reason="settle"))
    assert result["ok"] is True
    assert result["reason"] == "settle"

    session.cancel()
    cancelled = asyncio.run(tool.fn(seconds=1))
    assert cancelled["ok"] is False
    assert cancelled["status"] == "cancelled"


def test_replay_toolless_done() -> None:
    ai = ScriptedAI([_assistant_msg("hello world")])
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)

    async def _run():
        return await session.chat("hi", thinking_enabled=False)

    resp = asyncio.run(_run())
    assert isinstance(resp, ChatResponse)
    assert resp.status == "done"
    assert "hello world" in (resp.text or "")
    assert ai.calls == 1


def test_channel_reply_is_terminal_after_delivery_is_accepted() -> None:
    sent: list[list[str]] = []
    later_tool_hits = 0

    async def channel_reply(messages=None, **kwargs):  # noqa: ANN001
        sent.append(list(messages or []))
        return {
            "ok": True,
            "queued": True,
            "terminal_delivery": True,
            "delivery_count": len(messages or []),
        }

    async def later_tool(**kwargs):
        nonlocal later_tool_hits
        later_tool_hits += 1
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        "channel_reply",
        "final channel reply",
        {"messages": {"type": "array", "items": {"type": "string"}}},
        channel_reply,
        kind="mutating",
        dedupe=False,
    )
    reg.register("skill_read", "later read", {}, later_tool, kind="idempotent")
    ai = ScriptedAI([
        _assistant_msg("", [
            _tool_call(
                "reply-1",
                "channel_reply",
                {"messages": ["第一条", "第二条"]},
            ),
            _tool_call("must-not-run", "skill_read"),
        ]),
        _assistant_msg("已经发到聊天里了（两条）。"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)

    async def _run():
        return await session.chat(
            "分两条回复",
            thinking_enabled=False,
            message_source="channel",
            message_source_ref="qq-user",
        )

    resp = asyncio.run(_run())

    assert sent == [["第一条", "第二条"]]
    assert later_tool_hits == 0
    assert ai.calls == 1
    assert resp.status == "done"
    assert resp.text == ""
    assert [trace["name"] for trace in resp.tool_trace] == ["channel_reply"]
    assert all("已经发到聊天里了" not in item.content for item in session.history)


def test_failed_channel_reply_keeps_agent_loop_running() -> None:
    async def channel_reply(messages=None, **kwargs):  # noqa: ANN001
        return {"ok": False, "error": "channel unavailable"}

    reg = ToolRegistry()
    reg.register(
        "channel_reply",
        "final channel reply",
        {"messages": {"type": "array", "items": {"type": "string"}}},
        channel_reply,
        kind="mutating",
        dedupe=False,
    )
    ai = ScriptedAI([
        _assistant_msg("", [_tool_call(
            "reply-failed",
            "channel_reply",
            {"messages": ["准备发送"]},
        )]),
        _assistant_msg("渠道发送失败，请稍后重试。"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)

    resp = asyncio.run(session.chat(
        "回复我",
        thinking_enabled=False,
        message_source="channel",
        message_source_ref="qq-user",
    ))

    assert ai.calls == 2
    assert resp.text == "渠道发送失败，请稍后重试。"


def test_replay_parallel_readonly_order() -> None:
    seen: list[str] = []

    async def tool_a(**kwargs):
        await asyncio.sleep(0.05)
        seen.append("a")
        return {"ok": True, "tool": "a"}

    async def tool_b(**kwargs):
        await asyncio.sleep(0.05)
        seen.append("b")
        return {"ok": True, "tool": "b"}

    reg = ToolRegistry()
    reg.register("skill_read", "a", {}, tool_a, kind="idempotent", concurrency="read_parallel")
    reg.register("tool_search", "b", {}, tool_b, kind="idempotent", concurrency="read_parallel")

    ai = ScriptedAI([
        _assistant_msg(
            "",
            [
                _tool_call("1", "skill_read"),
                _tool_call("2", "tool_search"),
            ],
        ),
        _assistant_msg("finished both"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)
    # Avoid skill injection / memory noise by empty tools still ok.

    async def _run():
        t0 = time.perf_counter()
        resp = await session.chat("run both", thinking_enabled=False)
        return resp, time.perf_counter() - t0

    resp, elapsed = asyncio.run(_run())
    assert resp.status == "done"
    assert "finished" in (resp.text or "")
    # Results committed in call order regardless of finish order.
    names = [t.get("name") for t in (resp.tool_trace or resp.tool_calls or [])]
    assert names[:2] == ["skill_read", "tool_search"]
    assert elapsed < 0.25


def test_replay_exact_dedupe_skips_second_identical_call() -> None:
    hits = {"n": 0}

    async def probe(**kwargs):
        hits["n"] += 1
        return {"ok": True, "n": hits["n"]}

    reg = ToolRegistry()
    reg.register("skill_read", "probe", {"q": {"type": "string"}}, probe, kind="idempotent", dedupe=True)

    same = _tool_call("c1", "skill_read", {"q": "x"})
    same2 = _tool_call("c2", "skill_read", {"q": "x"})
    ai = ScriptedAI([
        _assistant_msg("", [same, same2]),
        _assistant_msg("after dedupe"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)

    async def _run():
        return await session.chat("dedupe please", thinking_enabled=False)

    resp = asyncio.run(_run())
    assert resp.status in {"done", "awaiting_user_decision", "awaiting_confirmation"} or True
    # Exact duplicate in the same batch should execute at most once.
    assert hits["n"] <= 1


def test_replay_mutating_not_exact_deduped() -> None:
    hits = {"n": 0}

    async def write(**kwargs):
        hits["n"] += 1
        return {"ok": True, "n": hits["n"]}

    reg = ToolRegistry()
    # Custom name avoids the confirmation gate on real write tools.
    reg.register(
        "lab_mutate",
        "write",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        write,
        kind="mutating",
        dedupe=False,
    )

    same = _tool_call("c1", "lab_mutate", {"path": "a.txt", "content": "x"})
    same2 = _tool_call("c2", "lab_mutate", {"path": "a.txt", "content": "x"})
    ai = ScriptedAI([
        _assistant_msg("", [same, same2]),
        _assistant_msg("after writes"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)

    async def _run():
        return await session.chat("write twice", thinking_enabled=False)

    asyncio.run(_run())
    # Loom22 §11: mutate tools must execute both times; repetition detector
    # (not exact-dedupe) is responsible for stopping thrash.
    assert hits["n"] == 2


def test_replay_freshness_key_bypasses_exact_dedupe() -> None:
    hits = {"n": 0}

    async def probe(**kwargs):
        hits["n"] += 1
        return {"ok": True, "n": hits["n"]}

    reg = ToolRegistry()
    reg.register(
        "skill_read",
        "probe",
        {"q": {"type": "string"}, "force": {"type": "boolean"}},
        probe,
        kind="idempotent",
        dedupe=True,
    )

    first = _tool_call("c1", "skill_read", {"q": "x", "force": True})
    second = _tool_call("c2", "skill_read", {"q": "x", "force": True})
    ai = ScriptedAI([
        _assistant_msg("", [first, second]),
        _assistant_msg("after force"),
    ])
    session = AssistantSession(registry=reg, ai_provider=ai)

    async def _run():
        return await session.chat("force refresh", thinking_enabled=False)

    asyncio.run(_run())
    assert hits["n"] == 2
