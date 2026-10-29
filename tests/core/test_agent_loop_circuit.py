"""Loom22 §10/§11 — stall/deadlock detection.

These tests pin down the *new* circuit-breaker contract:

- Loom22 §11 dedup (``_handle_duplicate_tool_call``) is *防抖 only*; it
  must never on its own escalate into a stop.
- The framework-owned round-signature deadlock detector fires **iff** the
  model has emitted the same ``(content_fp, sorted([tool:args_fp]))`` for
  four rounds in a row. Persisted legacy tuning values cannot weaken or
  make this safety boundary over-eager.
- A turn where the assistant content is empty but every tool call has
  a *different* ``args_hash`` is silent progress, not a stall.
"""

from __future__ import annotations

import itertools
import json

from argusai.core.assistant.session_blocks import (
    SessionBlocksMixin,
    is_duplicate_tool_skip_result,
)
from argusai.core.assistant.session_loop_control import (
    _DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT,
    SessionLoopControlMixin,
)
from argusai.core.assistant.session_tool_methods import SessionToolMethodsMixin


class _BlocksHarness(SessionBlocksMixin):
    pass


class _ToolHarness(SessionToolMethodsMixin):
    def __init__(self) -> None:
        self._user_decision_requested = False


class _LoopHarness(SessionLoopControlMixin):
    """Minimal stand-in providing only what the deadlock detector reads.

    We deliberately do *not* mix in the full ``AssistantSession``: those
    methods own networking, history persistence and provider plumbing
    that are irrelevant to fingerprint / signature math. Keeping the
    harness small means a regression here can only blame the detector.

    ``hard_stop`` emulates a value persisted by an older release. The
    framework-owned detector must ignore it while continuing to accept the
    legacy settings shape.
    """

    def __init__(self, *, hard_stop: int | None = None) -> None:
        self._recent_round_signatures: list[dict] = []
        self._current_round_index = 0
        self._current_turn_id = "test-turn"
        self._duplicate_skip_hint_injected = False
        if hard_stop is None:
            self.registry = None
        else:
            self.registry = _FakeRegistry(hard_stop)


class _FakeRegistry:
    """Minimal ``registry.api.settings_mgr.get()`` chain for the tests."""

    def __init__(self, hard_stop: int) -> None:
        self.api = _FakeApi(hard_stop)


class _FakeApi:
    def __init__(self, hard_stop: int) -> None:
        self.settings_mgr = _FakeSettingsMgr(hard_stop)


class _FakeSettingsMgr:
    def __init__(self, hard_stop: int) -> None:
        self._settings = _FakeSettings(hard_stop)

    def get(self) -> "_FakeSettings":
        return self._settings


class _FakeSettings:
    def __init__(self, hard_stop: int) -> None:
        self.assistant_round_signature_hard_stop = hard_stop


def _make_tc(name: str, args: dict) -> dict:
    return {
        "id": f"call_{name}_{json.dumps(args, sort_keys=True)}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_is_duplicate_tool_skip_result():
    assert is_duplicate_tool_skip_result({
        "ok": True,
        "skipped": True,
        "reason": "duplicate_tool_call_skipped",
    })
    assert not is_duplicate_tool_skip_result({"ok": True, "url": "http://x"})
    assert not is_duplicate_tool_skip_result({"ok": False, "error": "fail"})


def test_blocks_for_duplicate_skip_uses_tool_skipped_not_probe_result():
    harness = _BlocksHarness()
    traces = [{
        "name": "browser_evaluate",
        "arguments": {"url": "https://example.com/login", "expression": "() => 1"},
        "result": {
            "ok": True,
            "skipped": True,
            "reason": "duplicate_tool_call_skipped",
            "message": "Skipped repeat",
        },
    }]
    blocks = harness._blocks_for_tool_trace(traces)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_skipped"
    assert blocks[0]["tool_name"] == "browser_evaluate"
    assert "probe_result" not in {b.get("type") for b in blocks}


def test_blocks_for_user_decision_batch():
    harness = _BlocksHarness()
    blocks = harness._blocks_for_tool_trace([{
        "name": "stop_for_user_decision_batch",
        "args": {
            "title": "确认执行策略",
            "questions": [],
        },
        "result": {
            "ok": True,
            "stopped": True,
            "title": "确认执行策略",
            "context": "需要一起确认运行环境和网络策略。",
            "questions": [
                {
                    "id": "runtime",
                    "question": "JS 运行环境用哪个？",
                    "options": ["Node vm", "Playwright"],
                    "required": True,
                    "context": "Playwright 更接近浏览器。",
                },
                {
                    "id": "network",
                    "question": "是否允许网络验证？",
                    "options": ["禁网", "走被动代理"],
                    "required": False,
                    "context": "",
                },
            ],
        },
    }])

    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "user_decision_batch"
    assert block["title"] == "确认执行策略"
    assert block["context"] == "需要一起确认运行环境和网络策略。"
    assert [q["id"] for q in block["questions"]] == ["runtime", "network"]
    assert block["questions"][0]["options"] == ["Node vm", "Playwright"]
    assert block["questions"][1]["required"] is False


def test_blocks_collapse_repeated_todo_and_pending_decision_projections():
    harness = _BlocksHarness()
    traces = [
        {
            "name": "set_todos",
            "result": {
                "ok": True,
                "todos": [{"id": "one", "status": "pending"}],
                "plan_confirmation": {
                    "question": "旧问题",
                    "options": ["开始", "取消"],
                },
            },
        },
        {
            "name": "update_todo",
            "result": {
                "ok": True,
                "todos": [{"id": "one", "status": "completed"}],
                "plan_confirmation": {
                    "question": "新问题",
                    "options": ["继续", "停止"],
                },
            },
        },
    ]

    blocks = harness._blocks_for_tool_trace(traces)

    todo_blocks = [block for block in blocks if block.get("type") == "todo_list"]
    decision_blocks = [
        block for block in blocks if block.get("type") == "user_decision"
    ]
    assert len(todo_blocks) == 1
    assert todo_blocks[0]["todos"] == [{"id": "one", "status": "completed"}]
    assert len(decision_blocks) == 1
    assert decision_blocks[0]["question"] == "新问题"


def test_stop_for_user_decision_batch_sanitizes_payload():
    harness = _ToolHarness()
    result = harness.stop_for_user_decision_batch(
        "确认执行策略",
        [
            {
                "id": "Runtime Choice!",
                "question": "JS 运行环境用哪个？",
                "options": ["Node vm", "Playwright", "", "  "],
                "required": True,
                "context": "Playwright 更接近浏览器。",
            },
            {
                "id": "Runtime Choice!",
                "question": "重复 id 会自动改名？",
                "options": ["是", "否"],
                "required": False,
            },
            {"id": "bad", "question": "", "options": ["ignored"]},
        ],
        "需要一起确认运行环境和网络策略。",
    )

    assert result["ok"] is True
    assert result["stopped"] is True
    assert harness._user_decision_requested is True
    assert result["title"] == "确认执行策略"
    assert result["questions"][0]["id"] == "runtime_choice"
    assert result["questions"][1]["id"] == "runtime_choice_2"
    assert result["questions"][0]["options"] == ["Node vm", "Playwright"]
    assert result["questions"][1]["required"] is False
    assert len(result["questions"]) == 2


# -- Round-signature math ----------------------------------------------------


def test_round_signature_differs_when_tool_args_differ():
    """Same empty text + different tool args ⇒ different signatures."""
    h = _LoopHarness()
    sig_a = h._round_signature("", [_make_tc("js_runtime_eval", {"expression": "1+1"})])
    sig_b = h._round_signature("", [_make_tc("js_runtime_eval", {"expression": "2+2"})])
    assert sig_a != sig_b


def test_round_signature_stable_for_identical_round():
    """Same text + same (sorted) tool calls ⇒ identical signatures."""
    h = _LoopHarness()
    tcs = [
        _make_tc("a", {"x": 1}),
        _make_tc("b", {"y": 2}),
    ]
    sig_first = h._round_signature("hello", tcs)
    sig_second = h._round_signature("hello", list(reversed(tcs)))
    assert sig_first == sig_second


def test_round_signature_ignores_tool_id():
    """tool_call id varies per invocation but must not affect signature."""
    h = _LoopHarness()
    a = {"id": "call_1", "type": "function",
         "function": {"name": "x", "arguments": json.dumps({"k": 1})}}
    b = {"id": "call_2", "type": "function",
         "function": {"name": "x", "arguments": json.dumps({"k": 1})}}
    assert h._round_signature("", [a]) == h._round_signature("", [b])


# -- Circuit breaker behaviour ----------------------------------------------


def test_circuit_breaker_not_tripped_by_silent_progress():
    """Replay the real-world #333-#341 GeeTest reverse-engineering tail.

    Five rounds with empty content where every js_runtime_* call has a
    fresh ``args_hash`` — the model is silently rebuilding a sandbox
    runtime, not deadlocked. The old content-only fingerprint mistook
    this for a stall; the round-signature detector must let it through.
    """
    h = _LoopHarness()
    sequence = [
        ("", [_make_tc("js_runtime_create",
                       {"env": "browser", "session_id": "final"})]),
        ("", [_make_tc("js_runtime_eval",
                       {"expression": "window.Math = Math; 'ok'",
                        "session_id": "final"})]),
        ("", [_make_tc("js_runtime_eval",
                       {"expression": "window.Math = Math",
                        "session_id": "final"})]),
        ("", [_make_tc("js_runtime_load",
                       {"path": "gt4.js", "session_id": "final"})]),
        ("", [_make_tc("js_runtime_patch",
                       {"mode": "regex",
                        "pattern": "var head = document\\.getElementsByTagName",
                        "session_id": "final"})]),
    ]
    for content, tcs in sequence:
        h._current_round_index += 1
        h._record_round_signature(content, tcs)

    assert not h._agent_loop_circuit_breaker_exceeded(), (
        "Silent-progress GeeTest tail was falsely identified as a stall. "
        f"buffer={h._recent_round_signatures}"
    )
    # And no two signatures in the buffer should collide.
    sigs = [e["sig"] for e in h._recent_round_signatures]
    assert len(set(sigs)) == len(sigs), f"signatures collided: {sigs}"


def test_circuit_breaker_trips_on_full_repeat_with_tools():
    """4 rounds emitting the exact same (text, tool, args) ⇒ stop."""
    h = _LoopHarness()
    tcs = [_make_tc("browser_evaluate",
                    {"url": "https://x", "expression": "() => 1"})]
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT):
        h._current_round_index += 1
        h._record_round_signature("retrying...", tcs)
    assert h._agent_loop_circuit_breaker_exceeded()


def test_circuit_breaker_trips_on_text_only_repeat():
    """4 rounds emitting the same text with no tool calls ⇒ stop.

    This is the classic 'model is talking in circles, has given up on
    tool use' scenario — fingerprint-only repetition is still a valid
    deadlock signal when there is genuinely no tool progress to inspect.
    """
    h = _LoopHarness()
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT):
        h._current_round_index += 1
        h._record_round_signature("我不知道下一步该怎么办。", [])
    assert h._agent_loop_circuit_breaker_exceeded()


def test_circuit_breaker_quiet_below_threshold():
    """One short below threshold ⇒ not yet a stall."""
    h = _LoopHarness()
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT - 1):
        h._current_round_index += 1
        h._record_round_signature("stuck", [])
    assert not h._agent_loop_circuit_breaker_exceeded()


def test_circuit_breaker_resets_when_progress_resumes():
    """A single different signature inside the tail must break the streak.

    Loom22 says the detector exists to catch *hard* deadlocks, not
    near-misses. Any honest sign of progress within the most recent
    threshold-sized window means we are not stuck.
    """
    h = _LoopHarness()
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT - 1):
        h._current_round_index += 1
        h._record_round_signature("stuck", [])
    # One progress round (different signature) erases the threat.
    h._current_round_index += 1
    h._record_round_signature("trying a new approach", [
        _make_tc("read_text_file", {"path": "/tmp/notes.md"}),
    ])
    assert not h._agent_loop_circuit_breaker_exceeded()


# -- Framework-owned threshold / legacy compatibility ----------------------


def test_legacy_hard_stop_zero_cannot_disable_detector():
    h = _LoopHarness(hard_stop=0)
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT):
        h._current_round_index += 1
        h._record_round_signature("identical", [])
    assert h._agent_loop_circuit_breaker_exceeded()


def test_legacy_higher_threshold_does_not_weaken_internal_boundary():
    h = _LoopHarness(hard_stop=6)
    for i in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT - 1):
        h._current_round_index += 1
        h._record_round_signature("identical", [])
        assert not h._agent_loop_circuit_breaker_exceeded(), (
            f"tripped at internal round {i + 1}"
        )
    h._current_round_index += 1
    h._record_round_signature("identical", [])
    assert h._agent_loop_circuit_breaker_exceeded()


def test_legacy_lower_threshold_does_not_make_detector_overeager():
    h = _LoopHarness(hard_stop=2)
    for _ in range(_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT - 1):
        h._current_round_index += 1
        h._record_round_signature("stuck", [])
        assert not h._agent_loop_circuit_breaker_exceeded()
    h._current_round_index += 1
    h._record_round_signature("stuck", [])
    assert h._agent_loop_circuit_breaker_exceeded()


class _RuntimePolicyHarness(SessionLoopControlMixin):
    def __init__(self, context_window: int = 131072) -> None:
        self.context_window = context_window
        self._turn_deadline_at = None

    def _context_window_tokens(self) -> int:
        return self.context_window


def test_agent_round_iterator_has_no_fixed_round_cap():
    h = _RuntimePolicyHarness()
    assert list(itertools.islice(h._agent_round_iter(), 503, 506)) == [503, 504, 505]


def test_todo_continuation_is_state_guarded_not_count_capped():
    h = _RuntimePolicyHarness()
    assert h._auto_continue_allowed(10_000)
    assert not h._auto_continue_allowed(10_001)
    assert h._auto_continue_exhausted(10_001)
    h._recent_tool_outcomes = [{
        "name": "read_text_file",
        "result_fp": "new-result",
        "ok": True,
    }]
    assert h._auto_continue_allowed(10_002)
    assert not h._auto_continue_exhausted(10_002)


def test_context_retention_is_derived_from_active_context_window():
    expected = [
        (32 * 1024, 6, 300),
        (64 * 1024, 10, 400),
        (128 * 1024, 15, 500),
        (256 * 1024, 20, 500),
    ]
    for context_window, keep_last, summary_cap in expected:
        h = _RuntimePolicyHarness(context_window)
        assert h._tool_result_keep_last() == keep_last
        assert h._tool_result_summary_cap() == summary_cap
