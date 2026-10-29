"""Readonly parallel tool batch (Loom22 §22.6)."""

from __future__ import annotations

import asyncio
import time

from argusai.core.assistant.session_loop_control import SessionLoopControlMixin
from argusai.core.assistant.tools import ToolRegistry


class _Harness(SessionLoopControlMixin):
    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self.history: list = []
        self._cancel_requested = False
        self._user_decision_requested = False
        self._last_partial_text = ""
        self._current_turn_id = "t"
        self._duplicate_skip_hint_injected = False
        self._recent_tool_outcomes = []
        self._auto_approved: set[str] = set()
        self.hits = {"decision": 0}

        async def slow_a(**kwargs):
            await asyncio.sleep(0.15)
            return {"ok": True, "tool": "a"}

        async def slow_b(**kwargs):
            await asyncio.sleep(0.15)
            return {"ok": True, "tool": "b"}

        async def request_decision(**kwargs):
            self.hits["decision"] += 1
            self._user_decision_requested = True
            return {
                "ok": True,
                "plan_confirmation": {
                    "question": "开始执行？",
                    "options": ["开始执行", "修改计划"],
                },
            }

        self.registry.register("skill_read", "read", {}, slow_a)
        self.registry.register("tool_search", "search", {}, slow_b)
        self.registry.register("set_todos", "plan", {}, request_decision)

    def _tool_call_key(self, name, args):  # noqa: ANN001
        import json
        return f"{name}:{json.dumps(args or {}, sort_keys=True, default=str)}"

    def _should_dedupe_tool_call(self, name, args):  # noqa: ANN001
        from argusai.core.assistant.streaming import should_dedupe_tool_call

        return should_dedupe_tool_call(self.registry.get(name), args, name=name)

    def _remember_executed_tool_key(self, executed_keys, name, args):  # noqa: ANN001
        if self._should_dedupe_tool_call(name, args):
            executed_keys.add(self._tool_call_key(name, args))

    def _handle_duplicate_tool_call(self, *a, **k):  # noqa: ANN001
        async def _noop():
            return None
        return _noop()

    def _record_tool_outcome(self, *a, **k):  # noqa: ANN001
        return None

    def _tool_auto_approved(self, name, args):  # noqa: ANN001
        return True

    async def _dispatch_tool(self, name, args, *, user_approved=False):  # noqa: ANN001
        tool = self.registry.get(name)
        return await tool.fn(**args)

    def _auto_archive_tool_result(self, name, args, result):  # noqa: ANN001
        return result

    def _pop_tool_attachments(self, result):  # noqa: ANN001
        return []

    def _tool_result_to_prompt(self, name, result):  # noqa: ANN001
        return str(result)

    def _trace(self, name, args, result):  # noqa: ANN001
        return {"name": name, "arguments": args, "result": result}

    async def _emit_trace_blocks(self, on_block, trace):  # noqa: ANN001
        return None

    async def _emit_tool_start(self, on_status, name, args):  # noqa: ANN001
        return None

    async def _emit_checkpoint(self):
        return None

    async def _emit_status(self, on_status, text):  # noqa: ANN001
        return None

    def _log_tool_dispatch_start(self, name, args, tool_calls_made):  # noqa: ANN001
        return None

    def _fill_missing_tool_results(self, *a, **k):  # noqa: ANN001
        return None

    def _commit_cancelled_assistant(self, *a, **k):  # noqa: ANN001
        return "CANCELLED"

    def _commit_user_decision_stop(self, *a, **k):  # noqa: ANN001
        return "WAITING"


def test_parallel_readonly_batch_is_faster_than_serial() -> None:
    h = _Harness()
    tc_list = [
        {"id": "1", "function": {"name": "skill_read", "arguments": "{}"}},
        {"id": "2", "function": {"name": "tool_search", "arguments": "{}"}},
    ]
    messages: list = []
    tool_calls_made: list = []
    executed: set[str] = set()

    async def _run():
        return await h._execute_tool_calls_batch(
            tc_list,
            messages=messages,
            tool_calls_made=tool_calls_made,
            executed_keys=executed,
        )

    t0 = time.perf_counter()
    out = asyncio.run(_run())
    elapsed = time.perf_counter() - t0
    assert out is None
    assert len(tool_calls_made) == 2
    assert [t["name"] for t in tool_calls_made] == ["skill_read", "tool_search"]
    # Parallel should finish near max(0.15,0.15) not sum(0.30).
    assert elapsed < 0.28, f"expected parallel speedup, got {elapsed:.3f}s"


def test_user_decision_stops_remaining_batch_tools() -> None:
    h = _Harness()
    tc_list = [
        {"id": "1", "function": {"name": "set_todos", "arguments": "{}"}},
        {"id": "2", "function": {"name": "skill_read", "arguments": "{}"}},
    ]
    messages: list = []
    traces: list = []

    async def _run():
        return await h._execute_tool_calls_batch(
            tc_list,
            messages=messages,
            tool_calls_made=traces,
            executed_keys=set(),
        )

    out = asyncio.run(_run())

    assert out == "WAITING"
    assert h.hits["decision"] == 1
    assert [trace["name"] for trace in traces] == ["set_todos"]
