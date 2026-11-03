from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.core.assistant.agent_threads import (
    AgentThreadManager,
    _child_registry,
    set_team_room_hooks,
)
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.messages import ChatResponse
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


@pytest.mark.parametrize(
    ("response_status", "expected_state"),
    [
        ("done", "completed"),
        ("ok", "completed"),
        ("ai_error", "failed"),
        ("error", "failed"),
        ("stream_repetition", "paused"),
        ("paused_by_user", "paused"),
        ("paused_for_interjection", "paused"),
        ("cancelled", "paused"),
        ("awaiting_confirmation", "paused"),
        ("awaiting_user_decision", "paused"),
        ("unexpected_status", "failed"),
    ],
)
def test_child_response_status_is_not_mistaken_for_success(
    tmp_path: Path, monkeypatch, response_status: str, expected_state: str,
) -> None:
    async def _chat(self, *_args, **_kwargs):  # noqa: ANN001
        return ChatResponse(text="mock response", status=response_status)

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="reviewer", objective="inspect sample")["agent"]["agent_id"]
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert result["status"] == expected_state
        assert result["stopped_reason"] == response_status
    finally:
        manager.shutdown()


@pytest.mark.parametrize("provider_error", [False, True])
def test_real_child_chat_propagates_provider_outcome(
    tmp_path: Path, monkeypatch, provider_error: bool,
) -> None:
    class _ResultProvider(_Provider):
        async def chat(self, **_kwargs):
            if provider_error:
                raise RuntimeError("mock endpoint unavailable")
            return {"choices": [{"message": {"content": "Sample reviewed.", "tool_calls": []}}]}

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _ResultProvider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="reviewer", objective="inspect provided sample")["agent"]["agent_id"]
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert result["status"] == ("failed" if provider_error else "completed")
        assert result["stopped_reason"] == ("ai_error" if provider_error else "done")
    finally:
        manager.shutdown()


@pytest.mark.parametrize("stop_action", [None, "pause", "close"])
def test_publisher_queued_input_is_drained_unless_parent_stops(
    tmp_path: Path, monkeypatch, stop_action: str | None,
) -> None:
    calls: list[str] = []
    receipts: list[dict] = []

    async def _chat(self, message="", **_kwargs):  # noqa: ANN001
        calls.append(message)
        return ChatResponse(text=f"result-{len(calls)}", status="done")

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    parent = _parent(tmp_path)
    parent.registry.session_id = "team-audit"
    manager = AgentThreadManager(parent)

    def _publish(_parent_id, aid, _role, _text, *_args):
        if len(calls) == 1:
            receipts.append(manager.send_input(aid, "next concrete task", resume=True))
            if stop_action:
                assert getattr(manager, stop_action)(aid)["ok"] is True

    set_team_room_hooks(output_publisher=_publish)
    try:
        aid = manager.spawn(role="reviewer", objective="inspect sample")["agent"]["agent_id"]
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert receipts and receipts[0]["ok"] is True
        if stop_action:
            assert len(calls) == 1
            assert result["status"] == ("paused" if stop_action == "pause" else "closed")
        else:
            assert len(calls) == 2
            assert calls[1] == "next concrete task"
            assert result["status"] == "completed"
            assert result["pending_input_count"] == 0
    finally:
        set_team_room_hooks()
        manager.shutdown()


@pytest.mark.parametrize("stop_action", [None, "pause", "close", "halt", "cancel_active", "shutdown"])
def test_continuation_waits_until_retiring_worker_releases_provider(
    tmp_path: Path, monkeypatch, stop_action: str | None,
) -> None:
    closing = threading.Event()
    release = threading.Event()
    providers: list[_Provider] = []
    calls: list[str] = []

    class _ClosingProvider(_Provider):
        async def close(self):
            if self is providers[0]:
                closing.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)

    def _provider(_parent, model=""):
        provider = _ClosingProvider()
        providers.append(provider)
        return provider

    async def _chat(self, message="", **_kwargs):  # noqa: ANN001
        calls.append(message)
        return ChatResponse(text=f"result-{len(calls)}", status="done")

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", _provider,
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="reviewer", objective="inspect sample")["agent"]["agent_id"]
        assert closing.wait(timeout=2)
        retiring = manager._runtimes[aid]
        receipt = manager.send_input(aid, "continue after cleanup", resume=True)
        assert receipt["ok"] is True
        assert manager._runtimes[aid] is retiring
        assert len(providers) == 1
        if stop_action == "shutdown":
            manager.shutdown(wait_timeout=0)
        elif stop_action == "cancel_active":
            assert aid in manager.cancel_active()["closed"]
        elif stop_action:
            assert getattr(manager, stop_action)(aid)["ok"] is True
        release.set()
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        if stop_action:
            assert result["status"] == ("closed" if stop_action in {"close", "cancel_active"} else "paused")
            assert len(calls) == len(providers) == 1
            assert aid not in manager._runtimes
            assert result["pending_input_count"] == (0 if stop_action in {"halt", "cancel_active"} else 1)
            if stop_action == "shutdown":
                assert result["stopped_reason"] == "application_shutdown"
        else:
            assert result["status"] == "completed"
            assert result["pending_input_count"] == 0
            assert len(calls) == 2
            assert calls[1] == "continue after cleanup"
            assert len(providers) == 2
    finally:
        release.set()
        manager.shutdown()


def test_pending_confirmation_precedes_already_queued_team_input(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    receipts: list[dict] = []

    async def _chat(self, message="", *, confirmation_action=False, **_kwargs):  # noqa: ANN001
        if confirmation_action:
            calls.append("confirmation")
            self._pending = None
            return ChatResponse(text="approved result", status="done")
        if not calls:
            calls.append("initial")
            receipts.append(manager.send_input(aid, "queued follow-up", resume=True))
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
            return ChatResponse(text="approval required", status="awaiting_confirmation")
        calls.append(message)
        return ChatResponse(text="follow-up result", status="done")

    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    monkeypatch.setattr(AssistantSession, "chat", _chat)
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(
            role="reviewer", objective="inspect sample", start_immediately=False,
        )["agent"]["agent_id"]
        assert manager.send_input(aid, "start", resume=True)["ok"] is True
        paused = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert receipts[0]["ok"] is True
        assert calls == ["initial"]
        assert paused["status"] == "paused"
        assert paused["pending_input_count"] == 1
        pending = paused["pending_confirmation"]["pending_tools"][0]
        resumed = manager.confirm(aid, {
            "type": "confirmation_decision",
            "pending_id": pending["pending_id"],
            "version": pending["version"],
            "call_id": pending["id"],
            "verdict": "allow",
        })
        assert resumed["ok"] is True
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert calls == ["initial", "confirmation", "queued follow-up"]
    finally:
        manager.shutdown()


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
                "waiting_for_slot": True,
            }
        ),
        encoding="utf-8",
    )

    manager = AgentThreadManager(parent)
    recovered = manager.get("agent-recover")["agent"]

    assert recovered["status"] == "paused"
    assert recovered["status_message"] == "Recovered after an interrupted process"
    assert recovered["events"][-1]["type"] == "crash_recovered"
    assert manager._records["agent-recover"]["peer_resume_blocked"] is True
    assert not manager._records["agent-recover"].get("waiting_for_slot")


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


def test_external_child_inherits_host_guidance_and_preserves_bootstrap_on_resume(tmp_path: Path, monkeypatch) -> None:
    import hashlib
    from argusai.core.agent_runtimes.acp_turn import ACPTurnResult
    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    from argusai.core.workspace.user_profile import UserHabitsProfile

    parent = _parent(tmp_path)
    settings = parent.registry.api.settings_mgr.get()
    settings.data_root = str(tmp_path / "data")
    settings.user_habits = UserHabitsProfile(speaking_style="Concise Chinese")
    settings.agent_runtimes = [{
        "id": "fake-acp", "name": "Fake ACP", "backend": "acp",
        "enabled": True, "command": "fake-acp",
    }]
    parent.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "fake-acp", "acp_config": {"model": "chosen-by-parent"},
    })
    delegated_tools = []
    def register(_sid, child, *_args, **_kwargs):
        delegated_tools.append(set(child.registry._tools))
        return "mock-token"
    parent.registry.api._assistant_tool_broker = SimpleNamespace(
        register_session=register, mcp_server=lambda *_args: {},
        permission_handler=lambda *_args: None, unregister=lambda *_args: None,
    )
    calls = []
    marker = {}
    options = [{"id": "model", "name": "Model", "type": "select", "category": "model",
                "current_value": "chosen-by-parent", "options": [{"value": "chosen-by-parent", "name": "Chosen"}]}]
    capabilities = {"image": True, "audio": False, "embedded_context": True}
    async def fake_turn(_runtime, binding, **kwargs):
        calls.append((binding, kwargs))
        instruction = kwargs.get("host_instruction", "")
        profile = kwargs.get("user_habits", "")
        marker.update({
            "session_id": "remote-child", "instruction_hash": hashlib.sha256(instruction.encode()).hexdigest(),
            "profile_hash": hashlib.sha256(profile.encode()).hexdigest(),
        })
        return ACPTurnResult(
            ok=True, runtime_id="fake-acp", acp_session_id="remote-child", text="Reviewed",
            stop_reason="end_turn", config_options=options, prompt_capabilities=capabilities,
            host_context=dict(marker),
        )
    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", fake_turn)
    manager = AgentThreadManager(parent)
    try:
        spawned = manager.spawn_external(runtime_id="fake-acp", role="reviewer", objective="inspect one sample")
        aid = spawned["agent"]["agent_id"]
        assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
        assert "[ArgusAI host context]" in calls[0][1]["host_instruction"]
        assert "delegated child Agent" in calls[0][1]["host_instruction"]
        assert "not the main" in calls[0][1]["host_instruction"]
        assert "Concise Chinese" in calls[0][1]["user_habits"]
        assert manager.send_input(aid, "review the next sample", resume=True)["ok"]
        assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
        previous, _kwargs = calls[1]
        assert previous.acp_session_id == "remote-child"
        assert previous.acp_host_context == marker
        assert previous.acp_prompt_capabilities == capabilities
        assert previous.acp_config_options == tuple(options)
        assert previous.acp_config == {"model": "chosen-by-parent"}
        saved = manager._records[aid]["child_session"]["runtime"]
        assert saved["acp_host_context"] == marker
        assert saved["acp_prompt_capabilities"] == capabilities
        assert all("read_probe" in names and "write_probe" not in names for names in delegated_tools)
    finally:
        manager.shutdown()


@pytest.mark.parametrize("stop_action", [None, "pause", "close"])
def test_external_agent_accepts_input_during_final_handoff(
    tmp_path: Path, monkeypatch, stop_action: str | None,
) -> None:
    calls: list[str] = []
    receipts: list[dict] = []
    parent = _parent(tmp_path)
    settings = parent.registry.api.settings_mgr.get()
    settings.data_root = str(tmp_path / "data")
    settings.agent_runtimes = [{
        "id": "fake-acp", "name": "Fake ACP", "backend": "acp",
        "enabled": True, "command": "fake-acp",
    }]
    parent.registry.api._assistant_tool_broker = SimpleNamespace(
        register_session=lambda *_args, **_kwargs: "mock-token",
        mcp_server=lambda *_args: {},
        permission_handler=lambda *_args: None,
        unregister=lambda *_args: None,
    )

    async def _run_acp_turn(_config, _binding, *, message, **_kwargs):
        calls.append(message)
        return SimpleNamespace(
            ok=True, text="mock ACP result", error="", error_code="",
            stop_reason="end_turn", acp_session_id="mock-acp-session",
            config_options=[], updates=[],
        )

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run_acp_turn)
    manager = AgentThreadManager(parent)
    with monkeypatch.context() as staging:
        staging.setattr(manager, "_start_locked", lambda _agent_id: None)
        aid = manager.spawn_external(
            runtime_id="fake-acp", role="reviewer", objective="inspect sample",
        )["agent"]["agent_id"]

    original_run = asyncio.run

    def _run_at_handoff(coro, **kwargs):
        is_turn = getattr(getattr(coro, "cr_code", None), "co_name", "") == "_run_worker"
        result = original_run(coro, **kwargs)
        # Model the exact cross-thread boundary after the final empty-queue
        # check and before the worker acquires its terminal-state lock.
        if is_turn and not receipts:
            receipts.append(manager.send_input(aid, "late ACP follow-up", resume=True))
            if stop_action:
                assert getattr(manager, stop_action)(aid)["ok"] is True
        return result

    monkeypatch.setattr(asyncio, "run", _run_at_handoff)
    try:
        with manager._lock:
            manager._start_locked(aid)
        result = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert receipts and receipts[0]["ok"] is True
        if stop_action:
            assert len(calls) == 1
            assert result["status"] == ("paused" if stop_action == "pause" else "closed")
            assert result["pending_input_count"] == 1
        else:
            assert result["status"] == "completed"
            assert result["pending_input_count"] == 0
            assert len(calls) == 2
            assert "late ACP follow-up" in calls[1]
    finally:
        manager.shutdown()


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


@pytest.mark.parametrize("backend", ["native", "acp"])
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("coalesce", [False, True])
def test_peer_delivery_queues_exact_message_and_deduplicates_durably(
    tmp_path: Path, monkeypatch, backend: str, interrupt: bool, coalesce: bool,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[tuple[str, list]] = []
    parent = _parent(tmp_path)

    async def _turn(message, attachments=None):
        calls.append((message, list(attachments or [])))
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)

    if backend == "native":
        async def _chat(self, message="", attachments=None, **_kwargs):
            await _turn(message, attachments)
            return ChatResponse(text="result", status="done")

        monkeypatch.setattr(AssistantSession, "chat", _chat)
        monkeypatch.setattr(
            "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
            lambda *_args, **_kwargs: _Provider(),
        )
    else:
        settings = parent.registry.api.settings_mgr.get()
        settings.data_root = str(tmp_path / "data")
        settings.agent_runtimes = [{
            "id": "fake-acp", "backend": "acp", "enabled": True, "command": "fake-acp",
        }]
        parent.registry.api._assistant_tool_broker = SimpleNamespace(
            register_session=lambda *_args, **_kwargs: "mock-token", mcp_server=lambda *_args: {},
            permission_handler=lambda *_args: None, unregister=lambda *_args: None,
        )

        async def _acp(_config, _binding, *, message, attachments=None, **_kwargs):
            await _turn(message, attachments)
            return SimpleNamespace(
                ok=True, text="ACP result", error="", error_code="", stop_reason="end_turn",
                acp_session_id="mock-acp", config_options=[], updates=[],
            )

        monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _acp)

    manager = AgentThreadManager(parent)
    try:
        if backend == "native":
            aid = manager.spawn(role="A", objective="finish original work")["agent"]["agent_id"]
        else:
            aid = manager.spawn_external(runtime_id="fake-acp", role="A", objective="finish original work")["agent"]["agent_id"]
        assert started.wait(timeout=2)
        # Preserve the full 24k room body even with its additional typed wrapper.
        body = "[Team Room event]\n" + "e" * 300 + "\n" + "x" * 23980 + "UNIQUE_PEER_TAIL"
        attachment = {"name": "original.txt", "data_base64": "aGk="}
        receipt = manager.send_input(
            aid, body, resume=True, interrupt=interrupt, input_ref="room-message-1",
            room_visibility="group", attachments=[attachment],
            coalesce_key="team-room" if coalesce else "",
        )
        assert receipt["ok"] is True
        assert manager._runtimes[aid]["child"]._pause_after_current_response is interrupt
        duplicate = manager.send_input(aid, body, interrupt=False, input_ref="room-message-1")
        assert duplicate["ok"] is True and duplicate["duplicate"] is True
        attachment["name"] = "mutated.txt"
        assert manager.send_input(
            aid, "separate room request", interrupt=False, input_ref="room-message-2", room_visibility="group",
            coalesce_key="team-room" if coalesce else "",
        )["ok"] is True
        stored = json.loads(manager._path(aid).read_text(encoding="utf-8"))
        merged = coalesce and not interrupt
        assert stored["pending_input_count"] == (1 if merged else 2)
        assert stored["pending_inputs"][0]["input_refs"] == (
            ["room-message-1", "room-message-2"] if merged else ["room-message-1"]
        )
        assert stored["pending_inputs"][0]["room_visibility"] == "group"
        assert stored["pending_inputs"][0]["attachments"][0]["name"] == "original.txt"
        release.set()
        done = manager.wait([aid], timeout_seconds=3)["agents"][0]
        assert done["status"] == "completed" and done["pending_input_count"] == 0
        assert len(calls) == (2 if merged else 3)
        expected_body = body if not interrupt else body[:24000]
        if merged:
            from argusai.core.assistant.agent_threads import _COALESCED_INPUT_SEPARATOR
            expected_body += _COALESCED_INPUT_SEPARATOR + "separate room request"
        assert calls[1][0] == expected_body
        assert calls[1][1][0]["name"] == "original.txt"
        if not merged:
            assert calls[2][0] == "separate room request"
        assert manager.send_input(aid, "retry", interrupt=False, input_ref="room-message-1")["duplicate"] is True
        restored = AgentThreadManager(parent)
        try:
            assert restored.send_input(aid, "retry", interrupt=False, input_ref="room-message-1")["duplicate"] is True
            assert not restored._runtimes
        finally:
            restored.shutdown()
    finally:
        release.set()
        manager.shutdown()


def test_peer_input_waits_behind_existing_confirmation_without_clearing_it(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    async def _chat(self, message="", *, confirmation_action=False, **_kwargs):
        if confirmation_action:
            calls.append("confirmation")
            self._pending = None
            return ChatResponse(text="approved", status="done")
        if not calls:
            calls.append("original")
            self._pending = {
                "confirmations": [{"name": "write_probe", "arguments": {}, "tool_call": {"id": "write-1"}}],
                "tool_calls": [], "messages": [], "assistant_response": {}, "decisions": {},
            }
            return ChatResponse(text="authorize first", status="awaiting_confirmation")
        calls.append(message)
        return ChatResponse(text="peer processed", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="A", objective="original work")["agent"]["agent_id"]
        pending = manager.wait([aid], timeout_seconds=2)["agents"][0]
        confirmation = pending["pending_confirmation"]
        receipt = manager.send_input(aid, "@A follow-up", interrupt=False, input_ref="room-waiting")
        assert receipt["ok"] is True
        assert receipt["agent"]["status"] == "paused"
        assert receipt["agent"]["pending_confirmation"] == confirmation
        assert calls == ["original"]
        assert manager.send_input(aid, "ordinary human input")["error_code"] == "agent_confirmation_required"
        tool = confirmation["pending_tools"][0]
        assert manager.confirm(aid, {
            "type": "confirmation_decision", "pending_id": tool["pending_id"],
            "version": tool["version"], "call_id": tool["id"], "verdict": "allow",
        })["ok"] is True
        done = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert done["status"] == "completed"
        assert calls == ["original", "confirmation", "@A follow-up"]
    finally:
        manager.shutdown()


@pytest.mark.parametrize("stop_action", ["pause", "close", "halt"])
def test_parent_stop_takes_priority_over_accepted_peer_queue(tmp_path: Path, monkeypatch, stop_action: str) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return ChatResponse(text="original result", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="A", objective="original work")["agent"]["agent_id"]
        assert started.wait(timeout=2)
        assert manager.send_input(aid, "peer request", interrupt=False, input_ref="room-stop")["ok"] is True
        assert getattr(manager, stop_action)(aid)["ok"] is True
        release.set()
        stopped = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert stopped["status"] == ("closed" if stop_action == "close" else "paused")
        assert stopped["pending_input_count"] == (0 if stop_action == "halt" else 1)
        assert manager.send_input(aid, "retry old request", interrupt=False, input_ref="room-stop")["duplicate"] is True
        assert len(calls) == 1
    finally:
        release.set()
        manager.shutdown()


@pytest.mark.parametrize("stop_waiter", [None, "pause", "close"])
def test_peer_capacity_waiters_resume_only_when_a_slot_retires(
    tmp_path: Path, monkeypatch, stop_waiter: str | None,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return ChatResponse(text="finished", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    parent = _parent(tmp_path)
    parent.registry.api.settings_mgr.get().assistant_team_max_parallel = 1
    manager = AgentThreadManager(parent)
    try:
        ids = [manager.spawn(role=role, objective=f"work-{role}", start_immediately=False)["agent"]["agent_id"] for role in ("A", "B", "C")]
        assert manager.send_input(ids[0], "start original")["ok"] is True
        assert started.wait(timeout=2)
        for aid, text in zip(ids[1:], ("peer-B", "peer-C")):
            result = manager.send_input(aid, text, interrupt=False, input_ref=text)
            assert result["ok"] is True and result["agent"]["waiting_for_slot"] is True
            assert aid not in manager._runtimes
        assert manager.active_agent_count() == 1
        assert manager.send_input(ids[1], "retry", interrupt=False, input_ref="peer-B")["duplicate"] is True
        if stop_waiter:
            assert getattr(manager, stop_waiter)(ids[1])["ok"] is True
        release.set()
        results = manager.wait(ids, timeout_seconds=3)["agents"]
        assert results[2]["status"] == "completed"
        assert any("peer-C" in text for text in calls)
        assert sum("peer-B" in text for text in calls) == (0 if stop_waiter else 1)
        assert results[1]["status"] == ({"pause": "paused", "close": "closed"}.get(stop_waiter, "completed"))
    finally:
        release.set()
        manager.shutdown()


def test_peer_ref_history_is_bounded_but_pending_refs_still_deduplicate(tmp_path: Path) -> None:
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="A", objective="wait", start_immediately=False)["agent"]["agent_id"]
        assert manager.pause(aid)["ok"] is True
        for index in range(260):
            assert manager.send_input(aid, "queued peer", interrupt=False, input_ref=f"room-{index}")["ok"] is True
        record = manager._records[aid]
        assert len(record["accepted_input_refs"]) == 256
        assert "room-0" not in record["accepted_input_refs"]
        assert manager.send_input(aid, "retry pending", interrupt=False, input_ref="room-0")["duplicate"] is True
        assert manager.get(aid)["agent"]["status"] == "paused"
        assert not manager._runtimes
        manager.halt(aid)
        assert manager.send_input(aid, "retry stopped", interrupt=False, input_ref="room-259")["duplicate"] is True
        assert manager.get(aid)["agent"]["pending_input_count"] == 0
    finally:
        manager.shutdown()


def test_ordinary_peer_batches_respect_text_budget_and_explicit_request_order(tmp_path: Path, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return ChatResponse(text="finished", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="A", objective="finish current work")["agent"]["agent_id"]
        assert started.wait(timeout=2)
        messages = [
            ("a", "A" * 20000, "team-room"),
            ("b", "B" * 6000, "team-room"),
            ("c", "ordinary-tail-C", "team-room"),
            ("at", "@A explicit request", ""),
            ("d", "ordinary-D", "team-room"),
            ("e", "ordinary-E", "team-room"),
        ]
        for ref, text, key in messages:
            assert manager.send_input(
                aid, text, interrupt=False, input_ref=ref, coalesce_key=key, room_visibility="group",
            )["ok"] is True
        pending = manager._records[aid]["pending_inputs"]
        assert [item["input_refs"] for item in pending] == [["a"], ["b", "c"], ["at"], ["d", "e"]]
        assert all(len(item["text"]) <= 26000 for item in pending)
        assert manager._runtimes[aid]["child"]._pause_after_current_response is False
        release.set()
        assert manager.wait([aid], timeout_seconds=3)["agents"][0]["status"] == "completed"
        assert len(calls) == 5
        assert calls[1] == "A" * 20000
        assert calls[2].startswith("B" * 6000) and calls[2].endswith("ordinary-tail-C")
        assert calls[3] == "@A explicit request"
        assert calls[4].startswith("ordinary-D") and calls[4].endswith("ordinary-E")
        assert sum(text.count("ordinary-tail-C") for text in calls) == 1
    finally:
        release.set()
        manager.shutdown()


def test_coalesced_refs_stay_deduplicated_while_batch_is_active(tmp_path: Path, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[str] = []

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
        return ChatResponse(text="finished", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    try:
        aid = manager.spawn(role="A", objective="wait", start_immediately=False)["agent"]["agent_id"]
        manager.pause(aid)
        for index in range(260):
            assert manager.send_input(
                aid, f"observation-{index}", interrupt=False, input_ref=f"room-{index}",
                coalesce_key="team-room", room_visibility="group",
            )["ok"] is True
        assert manager._records[aid]["pending_input_count"] == 1
        assert "room-0" not in manager._records[aid]["accepted_input_refs"]
        assert manager.send_input(aid, "pending retry", interrupt=False, input_ref="room-0")["duplicate"] is True
        assert manager.send_input(aid, "resume explicitly")["ok"] is True
        assert started.wait(timeout=2)
        stored = json.loads(manager._path(aid).read_text(encoding="utf-8"))
        assert len(stored["active_input_refs"]) == 260
        assert not any("room-0" in item.get("input_refs", []) for item in stored["pending_inputs"] if isinstance(item, dict))
        assert manager.send_input(aid, "active retry", interrupt=False, input_ref="room-0")["duplicate"] is True
        release.set()
        assert manager.wait([aid], timeout_seconds=3)["agents"][0]["status"] == "completed"
        assert len(calls) == 2
        assert manager.send_input(aid, "recent retry", interrupt=False, input_ref="room-259")["duplicate"] is True
        assert manager.send_input(
            aid, "fresh observation", interrupt=False, input_ref="room-new", coalesce_key="team-room", room_visibility="group",
        )["ok"] is True
        assert manager.wait([aid], timeout_seconds=3)["agents"][0]["status"] == "completed"
        assert calls[-1] == "fresh observation" and len(calls) == 3
    finally:
        release.set()
        manager.shutdown()


@pytest.mark.parametrize("automatic", [False, True])
def test_private_confirmation_keeps_its_own_room_visibility(tmp_path: Path, monkeypatch, automatic: bool) -> None:
    published: list[tuple[str, str | None]] = []

    async def _chat(self, message="", *, confirmation_action=False, **_kwargs):
        if confirmation_action:
            self._pending = None
            return ChatResponse(text="private final evidence", status="done")
        self._pending = {
            "confirmations": [{"name": "write_probe", "arguments": {}, "tool_call": {"id": "write-1"}}],
            "tool_calls": [], "messages": [], "assistant_response": {}, "decisions": {},
        }
        return ChatResponse(text="private authorization needed", status="awaiting_confirmation")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    parent = _parent(tmp_path)
    parent.registry.session_id = "team-private-confirm"
    manager = AgentThreadManager(parent)
    set_team_room_hooks(output_publisher=lambda _sid, _aid, _role, text, visibility: published.append((text, visibility)))
    try:
        aid = manager.spawn(role="A", objective="private work", start_immediately=False)["agent"]["agent_id"]
        assert manager.send_input(aid, "private instruction", room_visibility="private")["ok"] is True
        pending = manager.wait([aid], timeout_seconds=2)["agents"][0]
        assert manager._records[aid]["pending_room_visibility"] == "private"
        stored = json.loads(manager._path(aid).read_text(encoding="utf-8"))
        assert stored["pending_room_visibility"] == "private"
        if automatic:
            parent.set_auto_approve_tool("write_probe", True)
            assert aid in manager.sync_auto_approve_from_parent()["auto_resolved"]
        else:
            tool = pending["pending_confirmation"]["pending_tools"][0]
            assert manager.confirm(aid, {
                "type": "confirmation_decision", "pending_id": tool["pending_id"],
                "version": tool["version"], "call_id": tool["id"], "verdict": "allow",
            })["ok"] is True
        assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
        assert published == [
            ("private authorization needed", "private"), ("private final evidence", "private"),
        ]
        assert "pending_room_visibility" not in manager._records[aid]
    finally:
        set_team_room_hooks()
        manager.shutdown()


def test_room_context_provider_receives_only_current_batch_refs(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    context_calls: list[list[str]] = []
    rows = [("leader", "earlier leader background"), ("ordinary", "ordinary input"), ("at", "@A future request")]
    cursor = 0

    def _context(_sid, _aid, *, input_refs=None):
        nonlocal cursor
        refs = list(input_refs or [])
        context_calls.append(refs)
        limit = max((i + 1 for i, (ref, _) in enumerate(rows) if ref in refs), default=len(rows))
        result = "\n".join(text for index, (ref, text) in enumerate(rows) if cursor < index + 1 <= limit and ref not in refs)
        cursor = max(cursor, limit)
        return result

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        return ChatResponse(text="done", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    set_team_room_hooks(context_provider=_context)
    try:
        aid = manager.spawn(role="A", objective="work", start_immediately=False)["agent"]["agent_id"]
        manager.pause(aid)
        manager.send_input(aid, "ordinary input", interrupt=False, input_ref="ordinary", coalesce_key="team-room")
        manager.send_input(aid, "@A future request", interrupt=False, input_ref="at")
        manager.send_input(aid, "resume explicitly")
        assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
        assert context_calls == [["ordinary"], ["at"], []]
        assert "earlier leader background" in calls[0]
        assert calls[0].count("ordinary input") == 1
        assert "future request" not in calls[0]
        assert calls[1].count("@A future request") == 1
    finally:
        set_team_room_hooks()
        manager.shutdown()


def test_context_provider_internal_type_error_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    reads: list[list[str]] = []

    def _context(_sid, _aid, *, input_refs=None):
        reads.append(list(input_refs or []))
        raise TypeError("provider failed after consuming its cursor")

    async def _chat(self, *_args, **_kwargs):
        return ChatResponse(text="done", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    set_team_room_hooks(context_provider=_context)
    try:
        aid = manager.spawn(role="A", objective="work", start_immediately=False)["agent"]["agent_id"]
        manager.send_input(aid, "peer input", interrupt=False, input_ref="peer-1")
        assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
        assert reads == [["peer-1"]]
    finally:
        set_team_room_hooks()
        manager.shutdown()


@pytest.mark.parametrize("stop_action", ["pause", "halt", "close", "shutdown"])
def test_native_stop_during_context_preparation_does_not_start_chat(
    tmp_path: Path, monkeypatch, stop_action: str,
) -> None:
    calls: list[str] = []
    reads = 0

    def _context(_sid, target, *, input_refs=None):
        nonlocal reads
        reads += 1
        if reads == 1:
            if stop_action == "shutdown":
                manager.shutdown(wait_timeout=0)
            else:
                assert getattr(manager, stop_action)(target)["ok"] is True
            return "already consumed leader background"
        return ""

    async def _chat(self, message="", **_kwargs):
        calls.append(message)
        return ChatResponse(text="done", status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider", lambda *_args, **_kwargs: _Provider(),
    )
    manager = AgentThreadManager(_parent(tmp_path))
    set_team_room_hooks(context_provider=_context)
    try:
        aid = manager.spawn(role="A", objective="work", start_immediately=False)["agent"]["agent_id"]
        manager.send_input(aid, "peer input", interrupt=False, input_ref="prepare-1", coalesce_key="team-room")
        stopped = manager.wait([aid], timeout_seconds=2)["agents"][0]
        future = manager._runtimes.get(aid, {}).get("future")
        if future:
            future.result(timeout=2)
        assert calls == []
        assert stopped["status"] == ("closed" if stop_action == "close" else "paused")
        assert stopped["pending_input_count"] == (1 if stop_action in {"pause", "shutdown"} else 0)
        assert not manager._records[aid].get("active_input_refs")
        if stop_action == "pause":
            pending_text = manager._records[aid]["pending_inputs"][0]["text"]
            assert "already consumed leader background" in pending_text
            assert manager.send_input(
                aid, "new peer observation", interrupt=False,
                input_ref="prepare-2", coalesce_key="team-room",
            )["ok"] is True
            # Frozen context belongs to the old prepared item. New traffic
            # needs its own context read/ack, rather than joining that snapshot.
            assert manager._records[aid]["pending_input_count"] == 2
            assert manager.send_input(aid, "resume explicitly")["ok"] is True
            assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
            assert calls[0].count("already consumed leader background") == 1
            assert calls[0].count("peer input") == 1
            assert calls[1] == "new peer observation"
            assert reads == 3  # Frozen item is not reread; fresh peer/resume are.
    finally:
        set_team_room_hooks()
        manager.shutdown()


@pytest.mark.parametrize("stop_action", ["pause", "halt", "close"])
def test_acp_stop_during_permission_preparation_does_not_start_turn(
    tmp_path: Path, monkeypatch, stop_action: str,
) -> None:
    calls: list[str] = []
    prepared = False
    parent = _parent(tmp_path)
    settings = parent.registry.api.settings_mgr.get()
    settings.data_root = str(tmp_path / "data")
    settings.agent_runtimes = [{"id": "fake-acp", "backend": "acp", "enabled": True, "command": "fake-acp"}]

    def _permission_handler(_token):
        nonlocal prepared
        if not prepared:
            prepared = True
            assert getattr(manager, stop_action)(aid)["ok"] is True
        return None

    parent.registry.api._assistant_tool_broker = SimpleNamespace(
        register_session=lambda *_args, **_kwargs: "mock-token", mcp_server=lambda *_args: {},
        permission_handler=_permission_handler, unregister=lambda *_args: None,
    )

    async def _acp(_config, _binding, *, message, **_kwargs):
        calls.append(message)
        return SimpleNamespace(
            ok=True, text="result", error="", error_code="", stop_reason="end_turn",
            acp_session_id="mock-acp", config_options=[], updates=[],
        )

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _acp)
    manager = AgentThreadManager(parent)
    try:
        with monkeypatch.context() as staging:
            staging.setattr(manager, "_start_locked", lambda _aid: None)
            aid = manager.spawn_external(runtime_id="fake-acp", role="A", objective="ACP work")["agent"]["agent_id"]
        with manager._lock:
            manager._start_locked(aid)
        stopped = manager.wait([aid], timeout_seconds=2)["agents"][0]
        future = manager._runtimes.get(aid, {}).get("future")
        if future:
            future.result(timeout=2)
        assert calls == []
        assert stopped["status"] == ("closed" if stop_action == "close" else "paused")
        assert stopped["pending_input_count"] == (1 if stop_action == "pause" else 0)
        if stop_action == "pause":
            assert manager.send_input(aid, "resume ACP")["ok"] is True
            assert manager.wait([aid], timeout_seconds=2)["agents"][0]["status"] == "completed"
            assert len(calls) == 2
            assert "ACP work" in calls[0] and calls[1] == "resume ACP"
    finally:
        manager.shutdown()
