"""Child-Agent completion has to reach the parent Agent, not just the UI.

Covers both delivery layers: the once-only in-turn notice injected into the
parent's prompt, and the debounced idle wake that starts one parent turn per
finished batch (and refuses to start one while the user is being waited on).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from argusai.core.assistant.agent_threads import (
    AgentThreadManager,
    format_parent_agent_notice,
)
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
from argusai.core.agent_runtimes import SessionRuntimeBinding
from argusai.gui.assistant_sessions import AssistantSessionsMixin


class _Provider:
    async def close(self) -> None:
        return None


def _parent(tmp_path: Path, api_extra: dict | None = None) -> AssistantSession:
    settings = SimpleNamespace(
        sessions_dir=str(tmp_path),
        ai_enabled=True,
        ai_endpoints=[SimpleNamespace(enabled=True, name="test", model="test-model")],
    )
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(get=lambda: settings),
        **(api_extra or {}),
    )
    registry = ToolRegistry(api=api, session_id="parent-session")
    return AssistantSession(registry=registry, ai_provider=object())


def _manager(tmp_path: Path, monkeypatch, *, api_extra: dict | None = None) -> AgentThreadManager:
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path, api_extra))
    manager.wake_debounce_seconds = 0.1
    return manager


class _CapturingAI:
    """Records the exact message list handed to the provider each round."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[list[dict]] = []

    async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
        self.prompts.append([dict(m) for m in (messages or [])])
        index = min(len(self.prompts) - 1, len(self.replies) - 1)
        return {"choices": [{"message": {"content": self.replies[index], "tool_calls": []}}]}


class _StubManager:
    """Manager surface the session mixin talks to, without real child threads."""

    def __init__(self, notices: list[dict], active: int = 0) -> None:
        self.notices = list(notices)
        self.reported: list[str] = []
        self.active = active

    def pending_parent_notices(self) -> list[dict]:
        return [n for n in self.notices if n["agent_id"] not in self.reported]

    def mark_parent_notices_reported(self, agent_ids) -> int:  # noqa: ANN001
        fresh = [str(a) for a in agent_ids if str(a) not in self.reported]
        self.reported.extend(fresh)
        return len(fresh)

    def active_agent_count(self) -> int:
        return self.active


class _WakeHost(AssistantSessionsMixin):
    pass


def _host(
    tmp_path: Path,
    sessions: dict[str, AssistantSession],
    *,
    ai_enabled: bool = True,
    agent_runtimes: list[dict] | None = None,
):
    host = object.__new__(_WakeHost)
    host.settings_mgr = SimpleNamespace(
        get=lambda: SimpleNamespace(
            sessions_dir=str(tmp_path),
            data_root=str(tmp_path),
            ai_enabled=ai_enabled,
            ai_endpoints=[SimpleNamespace(name="test", model="test-model")] if ai_enabled else [],
            agent_runtimes=agent_runtimes or [],
        )
    )
    host._assistant_sessions = dict(sessions)
    host._assistant_streams = {}
    host._assistant_run_lock = threading.Lock()
    host._assistant_running_sessions = set()
    host.started: list[dict] = []
    host.refreshed: list[str] = []

    def _start(params):  # noqa: ANN001
        host.started.append(dict(params))
        return {"ok": True, "run_id": "run-1", "job_id": "job-1"}

    def _refresh(session_id, *, reason="", source_ref=""):  # noqa: ANN001
        host.refreshed.append(f"{session_id}:{reason}")

    host._start_assistant_stream = _start
    host._notify_assistant_session_updated = _refresh
    return host


def test_notice_is_bounded_and_caps_the_agent_list() -> None:
    notices = [
        {
            "agent_id": f"agent-{i}",
            "role": f"role-{i}",
            "status": "completed",
            "summary": "A" * 2000,
        }
        for i in range(7)
    ]

    text = format_parent_agent_notice(notices)

    assert "A" * 600 + "…" in text
    assert "A" * 601 not in text
    assert "agent-4" in text
    assert "agent-5" not in text
    assert "还有 2 个子 Agent" in text


def test_terminal_child_queues_exactly_one_parent_notice(tmp_path: Path, monkeypatch) -> None:
    async def _chat(self, message="", on_checkpoint=None, **_kwargs):  # noqa: ANN001
        return SimpleNamespace(text="child conclusion", status="done", tool_trace=[], tool_calls=[])

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = _manager(tmp_path, monkeypatch)
    agent_id = manager.spawn(role="reviewer", objective="inspect")["agent"]["agent_id"]
    assert manager.wait([agent_id], timeout_seconds=2)["completed"] is True

    pending = manager.pending_parent_notices()
    assert [item["agent_id"] for item in pending] == [agent_id]
    assert pending[0]["status"] == "completed"
    assert pending[0]["summary"] == "child conclusion"

    assert manager.mark_parent_notices_reported([agent_id]) == 1
    assert manager.pending_parent_notices() == []
    # Re-marking is a no-op, so a retry can never double-count a delivery.
    assert manager.mark_parent_notices_reported([agent_id]) == 0
    manager.shutdown(wait_timeout=0.5)


def test_batch_of_children_wakes_the_parent_once(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    woken: list[tuple[str, tuple[str, ...]]] = []
    wake_seen = threading.Event()

    async def _chat(self, message="", on_checkpoint=None, **_kwargs):  # noqa: ANN001
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SimpleNamespace(text="done", status="done", tool_trace=[], tool_calls=[])

    def _wake(session_id, *, agent_ids=()):  # noqa: ANN001
        woken.append((session_id, tuple(agent_ids)))
        wake_seen.set()
        return {"ok": True}

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = _manager(
        tmp_path,
        monkeypatch,
        api_extra={"_wake_assistant_after_agent_completion": _wake},
    )
    agent_ids = [
        manager.spawn(role=f"reviewer-{i}", objective="inspect")["agent"]["agent_id"]
        for i in range(3)
    ]
    release.set()
    assert manager.wait(agent_ids, timeout_seconds=5)["completed"] is True

    assert wake_seen.wait(timeout=5)
    # Let any competing debounce timer fire before asserting the batch invariant.
    manager.shutdown(wait_timeout=1)
    assert len(woken) == 1
    assert woken[0][0] == "parent-session"
    assert set(woken[0][1]) == set(agent_ids)


def test_cancel_active_closes_children_and_suppresses_the_wake(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    running = threading.Event()
    woken: list[tuple[str, ...]] = []

    async def _chat(self, message="", on_checkpoint=None, **_kwargs):  # noqa: ANN001
        running.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return SimpleNamespace(text="done", status="done", tool_trace=[], tool_calls=[])

    def _wake(session_id, *, agent_ids=()):  # noqa: ANN001
        woken.append(tuple(agent_ids))
        return {"ok": True}

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = _manager(
        tmp_path,
        monkeypatch,
        api_extra={"_wake_assistant_after_agent_completion": _wake},
    )
    agent_id = manager.spawn(role="reviewer", objective="inspect")["agent"]["agent_id"]
    assert running.wait(timeout=5)

    result = manager.cancel_active()
    assert agent_id in result["closed"]
    # The child must actually stop, even though its chat loop never returned on
    # its own; cancel_active pulls the cancel_event the worker polls.
    release.set()
    assert manager.wait([agent_id], timeout_seconds=5)["completed"] is True
    assert manager.get(agent_id)["agent"]["status"] == "closed"

    # A completion landing after the cancel must not wake the parent.
    manager.shutdown(wait_timeout=1)
    assert woken == []


def test_spawn_after_cancel_re_enables_the_wake(tmp_path: Path, monkeypatch) -> None:
    async def _chat(self, message="", on_checkpoint=None, **_kwargs):  # noqa: ANN001
        return SimpleNamespace(text="done", status="done", tool_trace=[], tool_calls=[])

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = _manager(tmp_path, monkeypatch)
    manager.cancel_active()
    assert manager._suppress_wake is True

    manager.spawn(role="reviewer", objective="inspect")
    assert manager._suppress_wake is False
    manager.shutdown(wait_timeout=1)


def test_repeated_terminal_transitions_schedule_one_dispatch(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager.wake_debounce_seconds = 0.3
    dispatched: list[float] = []
    monkeypatch.setattr(
        AgentThreadManager,
        "_dispatch_parent_wake",
        lambda self: dispatched.append(time.monotonic()),
    )

    with manager._lock:
        for index in range(3):
            agent_id = f"agent-{index}"
            record = {
                "agent_id": agent_id,
                "role": f"reviewer-{index}",
                "objective": "inspect",
                "status": "queued",
                "summary": f"summary-{index}",
                "events": [],
            }
            manager._records[agent_id] = record
            manager._set_status_locked(record, "completed", "done", event_type="agent_completed")

    time.sleep(0.6)

    assert len(dispatched) == 1
    assert len(manager.pending_parent_notices()) == 3


def test_busy_parent_is_woken_again_once_it_goes_idle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._PARENT_WAKE_RETRY_SECONDS",
        0.1,
    )
    attempts: list[tuple[str, ...]] = []
    second = threading.Event()

    def _wake(session_id, *, agent_ids=()):  # noqa: ANN001
        attempts.append(tuple(agent_ids))
        if len(attempts) == 1:
            return {"ok": False, "skipped": True, "reason": "running_target"}
        second.set()
        return {"ok": True}

    manager = _manager(
        tmp_path,
        monkeypatch,
        api_extra={"_wake_assistant_after_agent_completion": _wake},
    )
    with manager._lock:
        record = {
            "agent_id": "agent-busy",
            "role": "reviewer",
            "objective": "inspect",
            "status": "running",
            "summary": "child conclusion",
            "events": [],
        }
        manager._records["agent-busy"] = record
        manager._set_status_locked(record, "completed", "done", event_type="agent_completed")

    assert second.wait(timeout=5)
    manager.shutdown(wait_timeout=0.5)
    assert len(attempts) == 2
    assert attempts[1] == ("agent-busy",)


def test_reading_a_finished_child_through_a_tool_retires_its_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    parent = manager.parent
    parent._agent_thread_manager = manager
    with manager._lock:
        record = {
            "agent_id": "agent-read",
            "role": "reviewer",
            "objective": "inspect",
            "status": "running",
            "summary": "child conclusion",
            "events": [],
        }
        manager._records["agent-read"] = record
        manager._set_status_locked(record, "completed", "done", event_type="agent_completed")
    assert len(manager.pending_parent_notices()) == 1

    result = parent.wait_for_agents_tool(["agent-read"], timeout_seconds=0)

    assert result["completed"] is True
    # The tool result already carried status + summary, so no wake is owed.
    assert manager.pending_parent_notices() == []
    manager.shutdown(wait_timeout=0.5)


def test_parent_turn_receives_bounded_notice_exactly_once() -> None:
    stub = _StubManager([
        {
            "agent_id": "agent-1",
            "role": "reviewer",
            "status": "completed",
            "summary": "B" * 1200,
        }
    ])
    ai = _CapturingAI(["first reply", "second reply"])
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session._agent_thread_manager = stub

    asyncio.run(session.chat("看一下结果", thinking_enabled=False))
    first_prompt = "\n".join(str(m.get("content") or "") for m in ai.prompts[0])

    assert "子 Agent 已结束" in first_prompt
    assert "agent_id=agent-1" in first_prompt
    assert "B" * 600 + "…" in first_prompt
    assert "B" * 601 not in first_prompt
    assert stub.reported == ["agent-1"]

    asyncio.run(session.chat("还有别的吗", thinking_enabled=False))
    second_prompt = "\n".join(str(m.get("content") or "") for m in ai.prompts[1])

    assert "agent_id=agent-1" not in second_prompt
    # The notice never reaches the UI transcript either.
    assert all(
        item.hidden
        for item in session.history
        if item.source == "agent_completion_notice"
    )


def test_finishing_turn_admits_children_are_still_running() -> None:
    ai = _CapturingAI(["初步结论如上。"])
    session = AssistantSession(registry=ToolRegistry(), ai_provider=ai)
    session._agent_thread_manager = _StubManager([], active=2)

    response = asyncio.run(session.chat("先说结论", thinking_enabled=False))

    assert "仍有 2 个子 Agent 在运行" in response.text


def test_wake_is_refused_while_the_user_is_being_waited_on(tmp_path: Path) -> None:
    session = AssistantSession(registry=ToolRegistry())
    session._pending = {
        "pending_id": "pending-1",
        "version": 1,
        "confirmations": [{
            "name": "run_shell_snippet",
            "arguments": {"script": "echo ok"},
            "tool_call": {"id": "call-1"},
        }],
    }
    host = _host(tmp_path, {"parent-session": session})

    result = host._wake_assistant_after_agent_completion(
        "parent-session",
        agent_ids=["agent-1"],
    )

    assert result == {"ok": False, "skipped": True, "reason": "pending_confirmation"}
    assert host.started == []

    session._pending = None
    session._user_decision_requested = True
    assert host._agent_wake_block_reason("parent-session") == "pending_user_decision"

    session._user_decision_requested = False
    session._cancel_requested = True
    assert host._agent_wake_block_reason("parent-session") == "cancelled_by_user"

    session._cancel_requested = False
    host._assistant_running_sessions.add("parent-session")
    assert host._agent_wake_block_reason("parent-session") == "running_target"


def test_wake_drops_silently_for_a_closed_session(tmp_path: Path) -> None:
    host = _host(tmp_path, {})

    result = host._wake_assistant_after_agent_completion("gone-session", agent_ids=["agent-1"])

    assert result == {"ok": False, "skipped": True, "reason": "session_closed"}
    assert host.started == []


def test_idle_wake_starts_one_hidden_parent_turn(tmp_path: Path) -> None:
    session = AssistantSession(registry=ToolRegistry())
    host = _host(tmp_path, {"parent-session": session})

    result = host._wake_assistant_after_agent_completion(
        "parent-session",
        agent_ids=["agent-1", "agent-2"],
    )

    assert result["ok"] is True
    assert len(host.started) == 1
    params = host.started[0]
    assert params["session_id"] == "parent-session"
    assert params["message_source"] == "agent_completion"
    assert params["message_source_ref"] == "agent-1"
    assert params["hide_user_message"] is True
    assert "子 Agent 已结束" in params["message"]
    assert host.refreshed == ["parent-session:agent_completion"]


def test_acp_parent_wake_does_not_require_native_ai_endpoint(tmp_path: Path) -> None:
    session = AssistantSession(registry=ToolRegistry())
    session.runtime_binding = SessionRuntimeBinding(
        runtime_id="wake-acp",
        locked=True,
    )
    host = _host(
        tmp_path,
        {"parent-session": session},
        ai_enabled=False,
        agent_runtimes=[{
            "id": "wake-acp",
            "name": "Wake ACP",
            "backend": "acp",
            "command": "wake-acp",
            "enabled": True,
        }],
    )

    result = host._wake_assistant_after_agent_completion(
        "parent-session",
        agent_ids=["agent-1"],
    )

    assert result["ok"] is True
    assert len(host.started) == 1
