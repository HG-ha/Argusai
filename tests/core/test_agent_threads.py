from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from argusai.core.assistant.agent_threads import (
    AgentThreadManager,
    _child_registry,
    set_team_room_hooks,
)
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


class _Provider:
    async def close(self) -> None:
        return None


def _parent(tmp_path: Path) -> AssistantSession:
    settings = SimpleNamespace(
        sessions_dir=str(tmp_path),
        ai_enabled=True,
        ai_endpoints=[SimpleNamespace(enabled=True, name="test", model="test-model")],
    )
    api = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))
    registry = ToolRegistry(api=api, session_id="parent-session")
    registry.register(
        "read_probe",
        "Read a probe",
        {},
        lambda: {"ok": True},
        risk="low",
        kind="idempotent",
    )
    registry.register(
        "write_probe",
        "Write a probe",
        {},
        lambda: {"ok": True},
        risk="medium",
        kind="mutating",
    )
    return AssistantSession(registry=registry, ai_provider=object())


def test_agent_thread_completes_persists_and_resumes(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    async def _chat(self, message="", on_checkpoint=None, **_kwargs):  # noqa: ANN001
        calls.append(message)
        if on_checkpoint is not None:
            result = on_checkpoint()
            if asyncio.iscoroutine(result):
                await result
        return SimpleNamespace(
            text=f"result-{len(calls)}",
            status="done",
            tool_trace=[],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))

    spawned = manager.spawn(
        role="reviewer",
        objective="inspect component",
        context="Only inspect the authentication module.",
    )
    assert spawned["ok"] is True
    agent_id = spawned["agent"]["agent_id"]
    waited = manager.wait([agent_id], timeout_seconds=2)

    assert waited["completed"] is True
    assert waited["agents"][0]["status"] == "completed"
    detail = manager.get(agent_id)["agent"]
    assert detail["summary"] == "result-1"
    assert detail["context"] == "Only inspect the authentication module."
    assert detail["history"] == []
    assert (tmp_path / "_agents" / "parent-session" / f"{agent_id}.json").is_file()

    resumed = manager.send_input(agent_id, "verify one more fact", resume=True)
    assert resumed["ok"] is True
    waited_again = manager.wait([agent_id], timeout_seconds=2)
    assert waited_again["agents"][0]["summary"] == "result-2"
    assert calls[-1] == "verify one more fact"

    removed = manager.remove(agent_id)
    assert removed["ok"] is True
    assert manager.get(agent_id)["ok"] is False
    assert not (tmp_path / "_agents" / "parent-session" / f"{agent_id}.json").exists()


def test_deferred_team_member_waits_for_command_and_receives_room_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    attachment_calls: list[list[dict]] = []
    published: list[tuple[str, str, str, str]] = []

    async def _chat(self, message="", **_kwargs):  # noqa: ANN001
        calls.append(message)
        attachment_calls.append(list(_kwargs.get("attachments") or []))
        return SimpleNamespace(text="member reply", status="done", tool_trace=[], tool_calls=[])

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    parent = _parent(tmp_path)
    parent.registry.session_id = "team-deadbeef"
    manager = AgentThreadManager(parent)
    set_team_room_hooks(
        context_provider=lambda parent_id, agent_id: "peer room context",
        output_publisher=lambda parent_id, agent_id, role, text: published.append(
            (parent_id, agent_id, role, text)
        ),
    )
    try:
        spawned = manager.spawn(
            role="reviewer",
            objective="inspect component",
            start_immediately=False,
        )
        agent_id = spawned["agent"]["agent_id"]
        assert spawned["agent"]["status"] == "paused"
        assert spawned["agent"]["deferred_start"] is True
        assert calls == []

        queued = manager.send_input(
            agent_id,
            "start now",
            resume=True,
            attachments=[{"name": "scope.txt", "data_base64": "aGVsbG8="}],
        )
        assert queued["ok"] is True
        waited = manager.wait([agent_id], timeout_seconds=2)
        assert waited["completed"] is True
        assert "peer room context" in calls[0]
        assert "inspect component" in calls[0]
        assert "start now" in calls[0]
        assert attachment_calls[0][0]["name"] == "scope.txt"
        assert published and published[0][0] == "team-deadbeef"
        assert published[0][3] == "member reply"
    finally:
        set_team_room_hooks()


def test_agent_thread_recovers_interrupted_snapshot_as_paused(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    root = tmp_path / "_agents" / "parent-session"
    root.mkdir(parents=True)
    path = root / "agent-recover.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_id": "agent-recover",
                "parent_session_id": "parent-session",
                "role": "reviewer",
                "objective": "recover",
                "status": "running",
                "created_at": 1,
                "updated_at": 1,
                "events": [],
                "pending_inputs": [],
            }
        ),
        encoding="utf-8",
    )

    manager = AgentThreadManager(parent)
    recovered = manager.get("agent-recover")["agent"]

    assert recovered["status"] == "paused"
    assert recovered["status_message"] == "Recovered after an interrupted process"
    assert recovered["events"][-1]["type"] == "crash_recovered"


def test_external_agent_spawn_persists_explicit_acp_backend(tmp_path: Path, monkeypatch) -> None:
    parent = _parent(tmp_path)
    settings = parent.registry.api.settings_mgr.get()
    settings.agent_runtimes = [{
        "id": "fake-acp",
        "name": "Fake ACP",
        "backend": "acp",
        "enabled": True,
        "command": "fake-acp",
    }]
    manager = AgentThreadManager(parent)
    monkeypatch.setattr(manager, "_start_locked", lambda _agent_id: None)

    spawned = manager.spawn_external(
        runtime_id="fake-acp",
        role="reviewer",
        objective="inspect one module",
    )

    assert spawned["ok"] is True
    assert spawned["agent"]["backend"] == "acp"
    assert spawned["agent"]["runtime_id"] == "fake-acp"
    assert spawned["agent"]["status"] == "queued"


def test_scoped_agent_inherits_exact_parent_allow_policy(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    parent.registry.register(
        "run_shell_snippet",
        "Run an authorized shell probe",
        {"script": {"type": "string"}},
        lambda script="": {"ok": True, "script": script},
        required=["script"],
        risk="high",
        kind="mutating",
    )
    parent.set_auto_approve_tool("run_shell_snippet", True, force=True)
    manager = AgentThreadManager(parent)
    registry = _child_registry(
        parent,
        permission="scoped",
        requested_tools=["run_shell_snippet"],
    )

    child = manager._restore_child(
        {"agent_id": "agent-scoped"},
        provider=None,
        child_registry=registry,
    )

    assert "run_shell_snippet" in child.registry._tools
    assert "run_shell_snippet" in child.auto_approve_tools()
    assert "read_probe" not in child.registry._tools
    assert "spawn_agent" not in child.registry._tools


def test_scoped_child_auto_approves_shell_without_parent_allow(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    parent.registry.register(
        "run_shell_snippet",
        "Run an authorized shell probe",
        {"script": {"type": "string"}},
        lambda script="": {"ok": True, "script": script},
        required=["script"],
        risk="high",
        kind="mutating",
    )
    manager = AgentThreadManager(parent)
    registry = _child_registry(
        parent,
        permission="scoped",
        requested_tools=["run_shell_snippet"],
    )
    child = manager._restore_child(
        {"agent_id": "agent-shell"},
        provider=None,
        child_registry=registry,
    )
    tool_call = {
        "id": "call-shell",
        "function": {
            "name": "run_shell_snippet",
            "arguments": '{"script": "echo hi"}',
        },
    }

    assert "run_shell_snippet" in child.auto_approve_tools()
    assert child._confirmation_requirements([tool_call]) == []
    assert parent._confirmation_requirements([tool_call])
    assert parent._confirmation_requirements([tool_call])[0]["name"] == "run_shell_snippet"


def test_manual_team_member_surfaces_tool_confirmation(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    parent.registry.register(
        "run_shell_snippet",
        "Run an authorized shell probe",
        {"script": {"type": "string"}},
        lambda script="": {"ok": True, "script": script},
        required=["script"],
        risk="high",
        kind="mutating",
    )
    manager = AgentThreadManager(parent)
    registry = _child_registry(
        parent,
        permission="scoped",
        requested_tools=["run_shell_snippet"],
    )
    child = manager._restore_child(
        {
            "agent_id": "agent-manual",
            "parent_session_id": "team-deadbeef",
            "approval_mode": "manual",
        },
        provider=None,
        child_registry=registry,
    )
    tool_call = {
        "id": "call-shell",
        "function": {
            "name": "run_shell_snippet",
            "arguments": '{"script": "echo hi"}',
        },
    }
    assert "run_shell_snippet" not in child.auto_approve_tools()
    reqs = child._confirmation_requirements([tool_call])
    assert reqs and reqs[0]["name"] == "run_shell_snippet"


def test_running_agent_pauses_at_safe_boundary(tmp_path: Path, monkeypatch) -> None:
    started = threading.Event()

    async def _chat(self, *_args, **_kwargs):  # noqa: ANN001
        started.set()
        while not self._pause_after_current_response:
            await asyncio.sleep(0.01)
        return SimpleNamespace(
            text="checkpoint",
            status="paused_for_interjection",
            tool_trace=[],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    spawned = manager.spawn(role="reviewer", objective="pause safely")
    agent_id = spawned["agent"]["agent_id"]
    assert started.wait(timeout=1)

    paused = manager.pause(agent_id)
    waited = manager.wait([agent_id], timeout_seconds=2)

    assert paused["ok"] is True
    assert waited["agents"][0]["status"] == "paused"
    assert "safe boundary" in waited["agents"][0]["status_message"]


def test_shutdown_interrupts_running_agent_and_persists_pause(tmp_path: Path, monkeypatch) -> None:
    started = threading.Event()
    stopped = threading.Event()

    async def _chat(self, *_args, **_kwargs):  # noqa: ANN001
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    spawned = manager.spawn(role="reviewer", objective="wait until shutdown")
    agent_id = spawned["agent"]["agent_id"]
    assert started.wait(timeout=1)

    manager.shutdown(wait_timeout=1)

    assert stopped.wait(timeout=1)
    record = manager.get(agent_id)["agent"]
    assert record["status"] == "paused"
    assert record["stopped_reason"] == "application_shutdown"
    persisted = json.loads(
        (tmp_path / "_agents" / "parent-session" / f"{agent_id}.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "paused"


def test_parent_ui_confirmation_resumes_child_agent(tmp_path: Path, monkeypatch) -> None:
    received: list[dict] = []

    async def _chat(self, *_args, confirmation_action=False, confirmation=None, **_kwargs):  # noqa: ANN001
        if confirmation_action:
            received.append(confirmation)
            self._pending = None
            return SimpleNamespace(
                text="approved result",
                status="done",
                tool_trace=[],
                tool_calls=[],
            )
        self._pending = {
            "confirmations": [{
                "name": "write_probe",
                "arguments": {"value": "safe"},
                "tool_call": {"id": "call-write"},
            }],
            "tool_calls": [],
            "messages": [],
            "assistant_response": {},
            "decisions": {},
            "auto_allow_names": [],
            "auto_allow_permissions": [],
        }
        return SimpleNamespace(
            text="",
            status="awaiting_confirmation",
            tool_trace=[],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    spawned = manager.spawn(
        role="operator",
        objective="perform scoped write",
        permission="scoped",
        scope_ref="case-1",
        requested_tools=["write_probe"],
    )
    agent_id = spawned["agent"]["agent_id"]
    paused = manager.wait([agent_id], timeout_seconds=2)["agents"][0]
    tool = paused["pending_confirmation"]["pending_tools"][0]

    resumed = manager.confirm(agent_id, {
        "type": "confirmation_decision",
        "pending_id": tool["pending_id"],
        "version": tool["version"],
        "call_id": tool["id"],
        "verdict": "allow",
    })
    completed = manager.wait([agent_id], timeout_seconds=2)["agents"][0]

    assert resumed["ok"] is True
    # Authorize must clear the card immediately — waiting for the resume turn
    # to finish left the buttons on screen for seconds.
    assert resumed["agent"]["pending_confirmation"] is None
    assert completed["status"] == "completed"
    assert completed["summary"] == "approved result"
    assert received[0]["call_id"] == "call-write"


def test_parent_auto_allow_sync_resolves_paused_child(tmp_path: Path, monkeypatch) -> None:
    received: list[dict] = []

    async def _chat(self, *_args, confirmation_action=False, confirmation=None, **_kwargs):  # noqa: ANN001
        if confirmation_action:
            received.append(confirmation)
            self._pending = None
            return SimpleNamespace(
                text="auto-resolved",
                status="done",
                tool_trace=[],
                tool_calls=[],
            )
        self._pending = {
            "confirmations": [{
                "name": "write_probe",
                "arguments": {"value": "safe"},
                "tool_call": {"id": "call-write"},
            }],
            "tool_calls": [],
            "messages": [],
            "assistant_response": {},
            "decisions": {},
            "auto_allow_names": [],
            "auto_allow_permissions": [],
        }
        return SimpleNamespace(
            text="",
            status="awaiting_confirmation",
            tool_trace=[],
            tool_calls=[],
        )

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    parent = _parent(tmp_path)
    manager = AgentThreadManager(parent)
    spawned = manager.spawn(
        role="operator",
        objective="write a file",
        permission="scoped",
        scope_ref="case-1",
        requested_tools=["write_probe"],
    )
    agent_id = spawned["agent"]["agent_id"]
    paused = manager.wait([agent_id], timeout_seconds=2)["agents"][0]
    assert paused["pending_confirmation"]["pending_tools"]

    parent.set_auto_approve_tool("write_probe", True)
    synced = manager.sync_auto_approve_from_parent()
    assert agent_id in synced["auto_resolved"]
    assert manager.get(agent_id)["agent"]["pending_confirmation"] is None

    completed = manager.wait([agent_id], timeout_seconds=2)["agents"][0]
    assert completed["status"] == "completed"
    assert completed["summary"] == "auto-resolved"
    assert received and received[0]["type"] == "batch_confirmation_decision"


def test_team_parallel_limit_is_configurable(tmp_path: Path) -> None:
    parent = _parent(tmp_path)
    settings = parent.registry.api.settings_mgr.get()
    settings.assistant_team_max_parallel = 1
    manager = AgentThreadManager(parent)
    manager._records["busy"] = {"agent_id": "busy", "status": "running"}

    blocked = manager.spawn(role="recon", objective="map", start_immediately=True)
    assert blocked["ok"] is False
    assert blocked["error_code"] == "subagent_concurrency_limit"
    assert blocked["max_concurrent"] == 1

    staged = manager.spawn(role="analyst", objective="wait", start_immediately=False)
    assert staged["ok"] is True
    assert staged["agent"]["status"] == "paused"

    refused = manager.send_input(staged["agent"]["agent_id"], "go", resume=True)
    assert refused["ok"] is False
    assert refused["max_concurrent"] == 1
    kept = manager.get(staged["agent"]["agent_id"])["agent"]
    assert kept["status"] == "paused"


def test_team_member_tool_session_override_replaces_lead_session() -> None:
    from argusai.core.assistant.tools import bind_tool_session_id, tool_session_override

    kwargs: dict[str, str] = {}
    bind_tool_session_id(kwargs, "team-abc")
    assert kwargs["_session_id"] == "team-abc"
    token = tool_session_override.set("member-1")
    try:
        bind_tool_session_id(kwargs, "team-abc")
    finally:
        tool_session_override.reset(token)
    assert kwargs["_session_id"] == "member-1"
