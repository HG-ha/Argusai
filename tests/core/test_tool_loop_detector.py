"""Result-aware + taxonomy-driven loop detector (Loom22 §11 / §11.2).

The round-signature circuit breaker (``test_agent_loop_circuit.py``) only
catches ``generic_repeat`` — byte-identical rounds. These tests pin the
patterns it structurally cannot see:

- ``poll_no_progress``: same tool returning byte-identical *results* N times
  in a row (survives §11 dedup because polling tools are exempt, and survives
  the round breaker because reasoning text varies between calls).
- ``ping_pong``: the model alternates between exactly two (tool,args) states
  A-B-A-B…; every round differs so the round signature never collapses.
- ``same_tool_failure`` (§11.2): same tool fails N times with *varying*
  args/errors — result and round signatures never collapse.
- ``mutating_repetition`` (§11.2): a mutating tool succeeds with the *same*
  call signature N times — success payloads carry fresh ids so result hash
  also misses it.

All soft-first: a one-shot hidden hint at the warn threshold, a
user_decision stop only at 2x — so a legitimate long poll is nudged, not
killed.
"""

from __future__ import annotations

from argusai.core.assistant.session_loop_control import (
    _DEFAULT_MUTATING_REPETITION_COUNT,
    _DEFAULT_PING_PONG_COUNT,
    _DEFAULT_POLL_NO_PROGRESS_COUNT,
    _DEFAULT_SAME_TOOL_FAILURE_COUNT,
    _TOOL_OUTCOME_BUFFER_LIMIT,
    SessionLoopControlMixin,
    _tool_outcome_ok,
)


class _FakeSettings:
    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)


class _FakeSettingsMgr:
    def __init__(self, settings: _FakeSettings) -> None:
        self._settings = settings

    def get(self) -> _FakeSettings:
        return self._settings


class _FakeApi:
    def __init__(self, settings: _FakeSettings) -> None:
        self.settings_mgr = _FakeSettingsMgr(settings)


class _FakeRegistry:
    def __init__(self, settings: _FakeSettings) -> None:
        self.api = _FakeApi(settings)


def test_turn_deadline_uses_persisted_long_running_setting() -> None:
    harness = _DetectorHarness()
    harness.registry = _FakeRegistry(_FakeSettings(assistant_turn_deadline_seconds=172800))
    assert harness._turn_deadline_seconds() == 172800


class _DetectorHarness(SessionLoopControlMixin):
    """Minimal stand-in exercising only the loop-detector math + routing.

    The two heavy ``_commit_*`` paths are stubbed to capture the routing
    decision (warn-hint vs hard-stop vs generic-repeat) without standing up
    a real ``ChatResponse`` / blocks / settings registry. The light
    ``_inject_*`` hint methods run for real so we also verify the one-shot
    injection + per-turn guard flag.
    """

    def __init__(
        self,
        *,
        poll: int | None = None,
        ping: int | None = None,
        same_fail: int | None = None,
        mut_rep: int | None = None,
    ) -> None:
        self._recent_tool_outcomes: list[dict] = []
        self._recent_round_signatures: list[dict] = []
        self._current_round_index = 0
        self._current_turn_id = "test-turn"
        self._poll_no_progress_hint_injected = False
        self._ping_pong_hint_injected = False
        self._same_tool_failure_hint_injected = False
        self._mutating_repetition_hint_injected = False
        self.history: list = []
        self.calls: list = []
        kw: dict = {}
        if poll is not None:
            kw["assistant_poll_no_progress_stop"] = poll
        if ping is not None:
            kw["assistant_ping_pong_stop"] = ping
        if same_fail is not None:
            kw["assistant_same_tool_failure_stop"] = same_fail
        if mut_rep is not None:
            kw["assistant_mutating_repetition_stop"] = mut_rep
        self.registry = _FakeRegistry(_FakeSettings(**kw)) if kw else None

    def _commit_tool_loop_stop(self, *, pattern, detail, count, tool_calls_made, tc_list, messages):  # noqa: ANN001
        self.calls.append(("stop", pattern, count))
        return f"STOP:{pattern}:{count}"

    def _commit_agent_loop_circuit_breaker(self, *, tool_calls_made, tc_list, messages):  # noqa: ANN001
        self.calls.append(("generic_repeat",))
        return "STOP:generic_repeat"


def _record(h: _DetectorHarness, name: str, args: dict, result) -> None:  # noqa: ANN001
    h._record_tool_outcome(name, args, result)


# -- recording -----------------------------------------------------------


def test_record_skips_dedup_results():
    h = _DetectorHarness()
    _record(h, "read_status", {"job": 1}, {"status": "running"})
    _record(h, "read_status", {"job": 1}, {"skipped": True, "reason": "duplicate_tool_call_skipped"})
    _record(h, "read_status", {"job": 1}, {"ok": True, "skipped": True})
    assert len(h._recent_tool_outcomes) == 1, "dedup-skip results must not enter the no-progress window"


def test_record_ring_buffer_capped():
    h = _DetectorHarness()
    for i in range(_TOOL_OUTCOME_BUFFER_LIMIT + 10):
        _record(h, "t", {"i": i}, {"r": i})
    assert len(h._recent_tool_outcomes) == _TOOL_OUTCOME_BUFFER_LIMIT


# -- poll_no_progress run length ----------------------------------------


def test_poll_no_progress_counts_identical_results():
    h = _DetectorHarness()
    for _ in range(3):
        _record(h, "read_status", {"job": 1}, {"status": "running"})
    assert h._poll_no_progress_run() == 3


def test_poll_no_progress_resets_on_changed_result():
    h = _DetectorHarness()
    _record(h, "read_status", {"job": 1}, {"status": "running"})
    _record(h, "read_status", {"job": 1}, {"status": "running"})
    _record(h, "read_status", {"job": 1}, {"status": "done"})  # progress!
    assert h._poll_no_progress_run() == 1


def test_poll_no_progress_resets_on_different_tool():
    h = _DetectorHarness()
    _record(h, "read_status", {"job": 1}, {"status": "running"})
    _record(h, "read_status", {"job": 1}, {"status": "running"})
    _record(h, "list_files", {"dir": "/"}, {"status": "running"})  # same result body, diff tool
    assert h._poll_no_progress_run() == 1


# -- ping_pong run length ------------------------------------------------


def test_ping_pong_detects_alternation():
    h = _DetectorHarness()
    for _ in range(2):
        _record(h, "tool_a", {"x": 1}, {"r": "a"})
        _record(h, "tool_b", {"y": 2}, {"r": "b"})
    # sequence A B A B -> trailing alternation length 4
    assert h._ping_pong_run() == 4


def test_ping_pong_zero_when_repeated_state():
    h = _DetectorHarness()
    _record(h, "tool_a", {"x": 1}, {"r": "a"})
    _record(h, "tool_a", {"x": 1}, {"r": "a"})  # A A, not alternating
    assert h._ping_pong_run() == 0


def test_ping_pong_breaks_on_third_state():
    h = _DetectorHarness()
    _record(h, "tool_a", {"x": 1}, {"r": "a"})
    _record(h, "tool_c", {"z": 9}, {"r": "c"})
    _record(h, "tool_a", {"x": 1}, {"r": "a"})
    _record(h, "tool_b", {"y": 2}, {"r": "b"})  # tail B A ... A C breaks
    # reversed: B(exp A?) start expected=last=B -> B ok exp A; A ok exp B;
    # C != B -> stop. run == 2 (only B,A alternation tail)
    assert h._ping_pong_run() == 2


# -- thresholds ----------------------------------------------------------


def test_threshold_defaults_when_no_registry():
    h = _DetectorHarness()
    assert h._poll_no_progress_count() == _DEFAULT_POLL_NO_PROGRESS_COUNT
    assert h._ping_pong_count() == _DEFAULT_PING_PONG_COUNT
    assert h._same_tool_failure_count() == _DEFAULT_SAME_TOOL_FAILURE_COUNT
    assert h._mutating_repetition_count() == _DEFAULT_MUTATING_REPETITION_COUNT


def test_threshold_zero_disables_via_settings():
    h = _DetectorHarness(poll=0, ping=0, same_fail=0, mut_rep=0)
    assert h._poll_no_progress_count() == 0
    assert h._ping_pong_count() == 0
    assert h._same_tool_failure_count() == 0
    assert h._mutating_repetition_count() == 0


# -- guard routing (soft hint -> hard stop) ------------------------------


def test_guard_poll_injects_hint_at_warn():
    h = _DetectorHarness(poll=2, ping=0)
    for _ in range(2):
        _record(h, "read_status", {"job": 1}, {"status": "running"})
    messages: list = []
    out = h._tool_loop_guard(messages=messages, tool_calls_made=[], tc_list=[])
    assert out is None, "warn level must not stop"
    assert any(c[0] == "hint_poll" for c in _hint_calls(h)) or h._poll_no_progress_hint_injected
    assert messages and "没有任何进展" in messages[-1]["content"]


def test_guard_poll_stops_at_double_warn():
    h = _DetectorHarness(poll=2, ping=0)
    for _ in range(4):
        _record(h, "read_status", {"job": 1}, {"status": "running"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out == "STOP:poll_no_progress:4"
    assert ("stop", "poll_no_progress", 4) in h.calls


def test_guard_poll_disabled_never_fires():
    h = _DetectorHarness(poll=0, ping=0)
    for _ in range(10):
        _record(h, "read_status", {"job": 1}, {"status": "running"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out is None
    assert h.calls == []


def test_guard_ping_pong_stops_at_double_warn():
    h = _DetectorHarness(poll=0, ping=4)
    for _ in range(4):  # A B A B A B A B -> run 8 == 2*warn
        _record(h, "tool_a", {"x": 1}, {"r": "a"})
        _record(h, "tool_b", {"y": 2}, {"r": "b"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out == "STOP:ping_pong:8"
    assert ("stop", "ping_pong", 8) in h.calls


def test_guard_generic_repeat_takes_precedence():
    h = _DetectorHarness(poll=2, ping=0)
    # Make the round-signature breaker trip (4 identical empty rounds).
    for _ in range(4):
        h._current_round_index += 1
        h._record_round_signature("stuck", [])
    # Also queue a poll-no-progress condition.
    for _ in range(4):
        _record(h, "read_status", {"job": 1}, {"status": "running"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out == "STOP:generic_repeat"
    assert ("generic_repeat",) in h.calls


def _hint_calls(h: _DetectorHarness) -> list:
    return [c for c in h.calls if isinstance(c, tuple) and c and c[0] == "hint_poll"]


# -- tool outcome ok helper ---------------------------------------------


def test_tool_outcome_ok_true_for_plain_success():
    assert _tool_outcome_ok({"ok": True, "status": "running"}) is True
    assert _tool_outcome_ok({"status": "running"}) is True
    assert _tool_outcome_ok("plain text") is True


def test_tool_outcome_ok_false_on_error_or_ok_false():
    assert _tool_outcome_ok({"ok": False, "error": "boom"}) is False
    assert _tool_outcome_ok({"error": "boom"}) is False
    assert _tool_outcome_ok({"ok": False}) is False


# -- same_tool_failure (Loom22 §11.2) -----------------------------------


def test_same_tool_failure_counts_varying_args_and_errors():
    h = _DetectorHarness()
    _record(h, "http_probe", {"url": "a"}, {"ok": False, "error": "timeout"})
    _record(h, "http_probe", {"url": "b"}, {"error": "conn refused"})
    _record(h, "http_probe", {"url": "c"}, {"ok": False, "error": "403"})
    assert h._same_tool_failure_run() == 3
    # Result fingerprints differ — poll_no_progress must NOT see this.
    assert h._poll_no_progress_run() == 1


def test_same_tool_failure_resets_on_success():
    h = _DetectorHarness()
    _record(h, "http_probe", {"url": "a"}, {"ok": False, "error": "timeout"})
    _record(h, "http_probe", {"url": "a"}, {"ok": False, "error": "timeout"})
    _record(h, "http_probe", {"url": "a"}, {"ok": True, "status": 200})
    assert h._same_tool_failure_run() == 0


def test_same_tool_failure_resets_on_different_tool():
    h = _DetectorHarness()
    _record(h, "http_probe", {"url": "a"}, {"ok": False, "error": "timeout"})
    _record(h, "http_probe", {"url": "b"}, {"ok": False, "error": "timeout"})
    _record(h, "dns_lookup", {"host": "x"}, {"ok": False, "error": "nxdomain"})
    assert h._same_tool_failure_run() == 1


def test_guard_same_tool_failure_hint_at_warn():
    h = _DetectorHarness(poll=0, ping=0, same_fail=2, mut_rep=0)
    for i in range(2):
        _record(h, "http_probe", {"url": f"u{i}"}, {"ok": False, "error": f"e{i}"})
    messages: list = []
    out = h._tool_loop_guard(messages=messages, tool_calls_made=[], tc_list=[])
    assert out is None
    assert h._same_tool_failure_hint_injected
    assert messages and "撞同一堵墙" in messages[-1]["content"]


def test_guard_same_tool_failure_stops_at_double_warn():
    h = _DetectorHarness(poll=0, ping=0, same_fail=2, mut_rep=0)
    for i in range(4):
        _record(h, "http_probe", {"url": f"u{i}"}, {"ok": False, "error": f"e{i}"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out == "STOP:same_tool_failure:4"
    assert ("stop", "same_tool_failure", 4) in h.calls


def test_guard_same_tool_failure_disabled_never_fires():
    h = _DetectorHarness(poll=0, ping=0, same_fail=0, mut_rep=0)
    for i in range(10):
        _record(h, "http_probe", {"url": f"u{i}"}, {"ok": False, "error": f"e{i}"})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out is None
    assert h.calls == []


# -- mutating_repetition (Loom22 §11.2) ---------------------------------


def test_mutating_repetition_counts_same_sig_despite_fresh_ids():
    h = _DetectorHarness()
    # Same call signature; each success carries a fresh id (result hash differs).
    for i in range(3):
        _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": i})
    assert h._mutating_repetition_run() == 3
    assert h._poll_no_progress_run() == 1  # result fps differ → no poll trip


def test_mutating_repetition_ignores_idempotent_tools():
    h = _DetectorHarness()
    for i in range(3):
        _record(h, "skill_read", {"name": "sqli-workflow"}, {"ok": True, "body": f"chunk{i}"})
    assert h._mutating_repetition_run() == 0


def test_mutating_repetition_resets_on_changed_args():
    h = _DetectorHarness()
    _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": 1})
    _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": 2})
    _record(h, "sandbox_write_file", {"path": "b.py", "content": "x"}, {"ok": True, "id": 3})
    assert h._mutating_repetition_run() == 1


def test_mutating_repetition_resets_on_failure():
    h = _DetectorHarness()
    _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": 1})
    _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": 2})
    _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": False, "error": "denied"})
    assert h._mutating_repetition_run() == 0


def test_guard_mutating_repetition_hint_at_warn():
    h = _DetectorHarness(poll=0, ping=0, same_fail=0, mut_rep=2)
    for i in range(2):
        _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": i})
    messages: list = []
    out = h._tool_loop_guard(messages=messages, tool_calls_made=[], tc_list=[])
    assert out is None
    assert h._mutating_repetition_hint_injected
    assert messages and "有副作用" in messages[-1]["content"]


def test_guard_mutating_repetition_stops_at_double_warn():
    h = _DetectorHarness(poll=0, ping=0, same_fail=0, mut_rep=2)
    for i in range(4):
        _record(h, "sandbox_write_file", {"path": "a.py", "content": "x"}, {"ok": True, "id": i})
    out = h._tool_loop_guard(messages=[], tool_calls_made=[], tc_list=[])
    assert out == "STOP:mutating_repetition:4"
    assert ("stop", "mutating_repetition", 4) in h.calls


def test_record_stores_ok_flag():
    h = _DetectorHarness()
    _record(h, "http_probe", {"url": "a"}, {"ok": False, "error": "timeout"})
    _record(h, "sandbox_write_file", {"path": "a.py"}, {"ok": True, "id": 1})
    assert h._recent_tool_outcomes[0]["ok"] is False
    assert h._recent_tool_outcomes[1]["ok"] is True
