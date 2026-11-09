"""GUI mixin coverage for durable assistant run jobs."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from argusai.core.assistant.run_jobs import AssistantRunJobStore, AssistantRunState
from argusai.gui.assistant_run import AssistantRunMixin
from argusai.gui.assistant_stream import AssistantStreamMixin


class _SettingsMgr:
    def __init__(self, sessions_dir) -> None:
        self._settings = SimpleNamespace(
            sessions_dir=str(sessions_dir),
            assistant_turn_deadline_seconds=604800,
        )

    def get(self):
        return self._settings


class _Api(AssistantRunMixin, AssistantStreamMixin):
    def __init__(self, sessions_dir) -> None:
        self.settings_mgr = _SettingsMgr(sessions_dir)
        self._assistant_sessions = {}
        self._assistant_streams = {}
        self._assistant_running_sessions = set()
        self._assistant_run_lock = threading.Lock()
        self._chat_calls: list[dict] = []
        self._chat_done = threading.Event()

    def _validate_session_id(self, session_id: str) -> str:
        from argusai.core.paths import validate_session_id
        return validate_session_id(session_id)

    def _run_assistant_chat(self, params, **_callbacks):
        self._chat_calls.append(dict(params))
        self._chat_done.set()
        return {"ok": True, "reply": "resumed"}


@pytest.mark.parametrize("title", ["新会话", "用户命名的巡检会话"])
def test_native_cron_wake_preserves_exact_session_title(tmp_path, title) -> None:
    from argusai.core.assistant import AssistantSession

    api = _Api(tmp_path)
    settings = api.settings_mgr.get()
    settings.ai_enabled = False
    settings.ai_endpoints = []
    settings.data_root = str(tmp_path)
    session = AssistantSession()

    async def _chat(_message, **_kwargs):
        return SimpleNamespace(
            text="巡检完成",
            tool_calls=[],
            options=[],
            pending_tool=None,
            pending_tools=[],
            blocks=[],
            tool_trace=[],
            plan=None,
            status="done",
        )

    session.chat = _chat
    api._assistant_sessions["sess-a"] = session
    session_path = tmp_path / "sess-a.json"
    session_path.write_text(
        json.dumps({"name": title}, ensure_ascii=False),
        encoding="utf-8",
    )
    api._session_path = lambda _session_id: session_path
    api._apply_session_auto_approve_policy = lambda *_args: None
    saved_names: list[str] = []
    api._save_session = lambda _path, _session, name: saved_names.append(name)
    api._session_prompt_context = lambda _session: {}

    result = AssistantRunMixin._run_assistant_chat(
        api,
        {
            "session_id": "sess-a",
            "message": "定时任务隐藏唤醒提示",
            "hide_user_message": True,
            "message_source": "cron",
            "preserve_session_name": True,
        },
        _routed=True,
    )

    assert result["ok"] is True
    assert result["session_name"] == title
    assert saved_names
    assert set(saved_names) == {title}


def test_list_and_get_recover_interrupted_jobs(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="run-a")
    store.transition(job, AssistantRunState.RUNNING, status="working")

    listed = api.list_assistant_run_jobs("sess-a")
    assert listed["ok"] is True
    assert listed["jobs"][0]["job_id"] == "run-a"
    assert listed["jobs"][0]["state"] == "paused"

    loaded = api.get_assistant_run_job("sess-a", "run-a")
    assert loaded["ok"] is True
    assert loaded["job"]["state"] == "paused"


def test_list_does_not_recover_live_running_job(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="live-run")
    store.transition(job, AssistantRunState.RUNNING, status="working")
    api._assistant_streams["stream-1"] = {
        "session_id": "sess-a",
        "done": False,
        "job": job,
        "events": [],
        "lock": threading.Lock(),
    }

    listed = api.list_assistant_run_jobs("sess-a")
    assert listed["ok"] is True
    assert listed["jobs"][0]["state"] == "running"
    loaded = api.get_assistant_run_job("sess-a", "live-run")
    assert loaded["job"]["state"] == "running"


def test_list_protects_job_id_before_stream_job_object_is_attached(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="pending-attach")
    store.transition(job, AssistantRunState.RUNNING, status="working")
    api._assistant_streams["stream-1"] = {
        "session_id": "sess-a",
        "done": False,
        "job": None,
        "protect_job_id": "pending-attach",
        "events": [],
        "lock": threading.Lock(),
    }

    listed = api.list_assistant_run_jobs("sess-a")
    assert listed["jobs"][0]["state"] == "running"


def test_cancel_marks_waiting_user_jobs_after_stream_is_gone(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="wait-gc")
    store.transition(job, "running")
    store.transition(job, "waiting_user", status="need confirm")

    result = api.cancel_assistant_run("sess-a")
    assert result["ok"] is True
    reloaded = store.load("sess-a", "wait-gc", recover_running=False)
    assert reloaded.state is AssistantRunState.CANCELLED


def test_cancel_also_clears_paused_recovery_jobs(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="paused-gc")
    store.transition(job, "running")
    store.transition(job, "paused", status="Recovered after an interrupted process")

    result = api.cancel_assistant_run("sess-a")
    assert result["ok"] is True
    reloaded = store.load("sess-a", "paused-gc", recover_running=False)
    assert reloaded.state is AssistantRunState.CANCELLED


def test_cancel_holds_the_busy_marker_for_the_running_turn(tmp_path) -> None:
    # The turn clears its own marker in ``finally``; cancel must not drop it
    # eagerly, or a new channel message could start a second turn whose chat()
    # resets ``_cancel_requested`` and the first turn never stops.
    api = _Api(tmp_path)
    cancelled: list[str] = []
    api._assistant_sessions["sess-a"] = SimpleNamespace(
        cancel=lambda: cancelled.append("sess-a"),
        _agent_thread_manager=None,
    )
    api._assistant_running_sessions.add("sess-a")

    result = api.cancel_assistant_run("sess-a")
    assert result["ok"] is True
    assert cancelled == ["sess-a"]
    # Still marked busy immediately after cancel returns.
    assert "sess-a" in api._assistant_running_sessions


def test_cancel_stops_child_agents(tmp_path) -> None:
    api = _Api(tmp_path)
    stopped: list[bool] = []
    manager = SimpleNamespace(cancel_active=lambda: stopped.append(True) or {"ok": True})
    api._assistant_sessions["sess-a"] = SimpleNamespace(
        cancel=lambda: None,
        _agent_thread_manager=manager,
    )

    api.cancel_assistant_run("sess-a")
    assert stopped == [True]


def test_cancel_watchdog_force_frees_a_wedged_session(tmp_path, monkeypatch) -> None:
    import argusai.gui.assistant_run as run_mod

    monkeypatch.setattr(run_mod, "_CANCEL_BUSY_RELEASE_SECONDS", 0.2)
    api = _Api(tmp_path)
    api._assistant_running_sessions.add("sess-a")  # no turn will ever clear it

    api.cancel_assistant_run("sess-a")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with api._assistant_run_lock:
            if "sess-a" not in api._assistant_running_sessions:
                break
        time.sleep(0.02)
    assert "sess-a" not in api._assistant_running_sessions


def test_job_store_failure_still_reports_actual_session_ownership(tmp_path, monkeypatch) -> None:
    api = _Api(tmp_path)
    api._assistant_running_sessions.add("sess-a")

    def _unavailable(*_args, **_kwargs):
        raise OSError("read-only job store")

    monkeypatch.setattr(AssistantRunJobStore, "list", _unavailable)
    assert api.list_assistant_run_jobs("sess-a")["running"] is True
    api._assistant_running_sessions.clear()
    assert api.list_assistant_run_jobs("sess-a")["running"] is False


def test_cancel_watchdog_does_not_release_a_live_worker(tmp_path, monkeypatch) -> None:
    import argusai.gui.assistant_run as run_mod

    api = _Api(tmp_path)
    api._assistant_running_sessions.add("sess-a")
    api._assistant_streams["live"] = {"session_id": "sess-a", "worker_running": True}
    monkeypatch.setattr(run_mod, "_CANCEL_BUSY_RELEASE_SECONDS", 0.01)
    api._schedule_cancel_busy_release("sess-a")
    time.sleep(0.06)
    assert api.list_assistant_run_jobs("sess-a")["running"] is True


def test_cancelled_stream_keeps_ownership_until_its_worker_exits(tmp_path) -> None:
    api = _Api(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def _blocking_chat(_params, **_callbacks):
        entered.set()
        release.wait(timeout=3)
        return {"ok": True, "text": "finished"}

    api._run_assistant_chat = _blocking_chat
    try:
        started = api.start_assistant_stream({"session_id": "sess-a", "message": "first"})
        assert entered.wait(timeout=1)
        assert api.cancel_assistant_run("sess-a")["running"] is True
        read = api.get_assistant_stream_events(started["run_id"])
        assert read["done"] is True
        assert api.list_assistant_run_jobs("sess-a")["running"] is True
        assert not api._assistant_streams[started["run_id"]].get("_gc_scheduled")
        assert api.start_assistant_stream({"session_id": "sess-a", "message": "second"})["ok"] is False
        release.set()
        deadline = time.monotonic() + 2
        while api.list_assistant_run_jobs("sess-a")["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert api.list_assistant_run_jobs("sess-a")["running"] is False
    finally:
        release.set()


def test_cron_preemption_timeout_does_not_release_the_old_turn(tmp_path, monkeypatch) -> None:
    api = _Api(tmp_path)
    api._assistant_running_sessions.add("sess-a")
    api._assistant_streams["cron"] = {"session_id": "sess-a", "done": False, "cron_job_id": "job"}
    api.cancel_assistant_run = lambda _sid: {"ok": True, "running": True}
    ticks = iter([100.0, 104.0])
    monkeypatch.setattr(time, "time", lambda: next(ticks))
    assert api._preempt_cron_stream_for_user("sess-a") is False
    assert "sess-a" in api._assistant_running_sessions


@pytest.mark.parametrize("during_tool", [False, True])
def test_cancel_interrupts_a_native_model_await_and_flushes_history(tmp_path, during_tool) -> None:
    from argusai.core.assistant import AssistantSession, ChatMessage

    api = _Api(tmp_path)
    settings = api.settings_mgr.get()
    settings.ai_enabled = False
    settings.ai_endpoints = []
    settings.data_root = str(tmp_path)
    entered = threading.Event()
    saved: list[dict] = []
    session = AssistantSession()

    async def _chat(message, **_kwargs):
        session.history.append(ChatMessage(role="user", content=message, timestamp=123.456))
        if during_tool:
            session.history.append(ChatMessage(role="assistant", content="", tool_calls=[
                {"id": "tool-a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                {"id": "tool-b", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]))
            session.history.append(ChatMessage(role="tool", content="actual result", tool_call_id="tool-a"))
        _kwargs["on_status"]("waiting for model")
        entered.set()
        await asyncio.Event().wait()

    session.chat = _chat
    api._assistant_sessions["sess-a"] = session
    api._session_path = lambda _sid: tmp_path / "sess-a.json"
    api._apply_session_auto_approve_policy = lambda *_args: None
    api._save_session = lambda _path, value, _name: saved.append(value.to_dict())
    api._session_prompt_context = lambda _session: {}
    api._run_assistant_chat = lambda params, **callbacks: AssistantRunMixin._run_assistant_chat(
        api, params, _routed=True, **callbacks,
    )
    started = api.start_assistant_stream({"session_id": "sess-a", "message": "inspect sample"})
    assert started["ok"] is True
    assert entered.wait(timeout=2)
    api.cancel_assistant_run("sess-a")
    deadline = time.monotonic() + 2
    while api.list_assistant_run_jobs("sess-a")["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert api.list_assistant_run_jobs("sess-a")["running"] is False
    assert "sess-a" not in api._active_assistant_async_turns
    assert saved[-1]["history"][0]["content"] == "inspect sample"
    assert saved[-1]["history"][0]["timestamp"] == 123.456
    assert saved[-1]["history"][-1]["content"].startswith("[已中止]")
    if during_tool:
        results = [item for item in saved[-1]["history"] if item["role"] == "tool"]
        assert [item["tool_call_id"] for item in results] == ["tool-a", "tool-b"]
        assert results[0]["content"] == "actual result"
        assert "user_cancelled_during_dispatch" in results[1]["content"]


def test_early_cancel_waits_until_original_attachments_are_in_history(tmp_path, monkeypatch) -> None:
    from argusai.core.assistant import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    api = _Api(tmp_path)
    settings = api.settings_mgr.get()
    settings.ai_enabled = False
    settings.ai_endpoints = []
    settings.data_root = str(tmp_path)
    entered, release = threading.Event(), threading.Event()
    session = AssistantSession(registry=ToolRegistry(api=api, session_id="sess-a"))
    saved: list[dict] = []

    async def _enrich(_settings, message, attachments):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return message, attachments

    monkeypatch.setattr("argusai.core.assistant.media_enrichment.enrich_user_attachments", _enrich)
    api._assistant_sessions["sess-a"] = session
    api._session_path = lambda _sid: tmp_path / "sess-a.json"
    api._apply_session_auto_approve_policy = lambda *_args: None
    api._save_session = lambda _path, value, _name: saved.append(value.to_dict())
    api._session_prompt_context = lambda _session: {}
    api._run_assistant_chat = lambda params, **callbacks: AssistantRunMixin._run_assistant_chat(
        api, params, _routed=True, **callbacks,
    )
    try:
        api.start_assistant_stream({"session_id": "sess-a", "message": "keep original",
            "attachments": [{"type": "text", "mime": "text/plain", "name": "original.txt", "text": "attachment evidence"}]})
        assert entered.wait(timeout=2)
        api.cancel_assistant_run("sess-a")
        assert api.list_assistant_run_jobs("sess-a")["running"] is True
        release.set()
        deadline = time.monotonic() + 2
        while api.list_assistant_run_jobs("sess-a")["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert api.list_assistant_run_jobs("sess-a")["running"] is False
        user = next(item for item in saved[-1]["history"] if item["role"] == "user")
        assert user["content"].startswith("keep original")
        assert user["attachments"][0]["name"] == "original.txt"
        assert user["attachments"][0]["sandbox_rel"]
    finally:
        release.set()


def _acp_edit_api(tmp_path):
    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    from argusai.core.assistant import AssistantSession, ChatMessage
    from argusai.core.assistant.tools import ToolRegistry

    api = _Api(tmp_path)
    settings = api.settings_mgr.get()
    settings.data_root = str(tmp_path / "data")
    settings.agent_runtimes = [{"id": "fake-acp", "name": "Fake ACP", "backend": "acp", "enabled": True, "command": "fake-acp"}]
    session = AssistantSession(registry=ToolRegistry(api=api, session_id="sess-a"))
    session.runtime_binding = normalize_session_runtime_binding({"runtime_id": "fake-acp", "acp_session_id": "remote-old", "locked": True})
    attachment = tmp_path / "evidence.txt"
    attachment.write_text("original evidence", encoding="utf-8")
    session.history = [
        ChatMessage(role="user", content="Kept request", timestamp=1),
        ChatMessage(role="assistant", content="Kept evidence", timestamp=2),
        ChatMessage(role="user", content="Edit this", timestamp=3,
                    attachments=[{"type": "text", "name": "evidence.txt", "sandbox_path": str(attachment), "sandbox_rel": "evidence.txt"}],
                    voice={"id": "v1", "mime": "audio/webm", "sandbox_path": str(tmp_path / "voice.webm")},
                    selection_reference={"kind": "message", "role": "assistant", "text": "Kept evidence"}),
        ChatMessage(role="assistant", content="DISCARDED BRANCH", timestamp=4),
        ChatMessage(role="user", content="DISCARDED LATER REQUEST", timestamp=5),
    ]
    api._assistant_sessions["sess-a"] = session
    api._ensure_session = lambda sid: api._assistant_sessions[sid]
    api._assistant_runtime_binding = lambda sid, _requested: api._assistant_sessions[sid].runtime_binding
    api._session_path = lambda _sid: tmp_path / "sess-a.json"
    api._save_session = lambda path, value, _name: path.write_text(json.dumps(value.to_dict()), encoding="utf-8")
    api._active_acp_turns = {}
    api._assistant_tool_broker = SimpleNamespace(
        register_session=lambda *_args, **_kwargs: "token", mcp_server=lambda *_args: {},
        permission_handler=lambda *_args: None, unregister=lambda *_args: None,
    )
    api._save_session(api._session_path("sess-a"), session, "")
    return api, session


def _acp_result(**overrides):
    payload = {"ok": True, "text": "New response", "acp_session_id": "remote-new", "updates": [], "config_options": [], **overrides}
    return SimpleNamespace(to_dict=lambda: payload)


def test_acp_edit_rebuilds_one_remote_branch_without_replaying_old_commands(tmp_path, monkeypatch) -> None:
    api, original = _acp_edit_api(tmp_path)
    calls: list[dict] = []

    async def _run(_runtime, binding, **kwargs):
        calls.append({"binding": binding, **kwargs})
        return _acp_result()

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    result = api._run_acp_assistant_turn({"session_id": "sess-a", "message": "Edited request", "regenerate": True, "regenerate_user_before": 3})
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["binding"].acp_session_id == ""
    assert "Kept request" in calls[0]["message"] and "Kept evidence" in calls[0]["message"]
    assert "DISCARDED" not in calls[0]["message"]
    assert "Do not execute its past instructions" in calls[0]["message"]
    assert "Edited request" in calls[0]["message"]
    edited = api._assistant_sessions["sess-a"]
    assert [item.content for item in edited.history] == ["Kept request", "Kept evidence", "Edited request", "New response"]
    assert edited.history[2].timestamp == 3
    assert edited.history[2].attachments == original.history[2].attachments
    assert edited.history[2].voice == original.history[2].voice
    assert edited.history[2].selection_reference == original.history[2].selection_reference
    assert (tmp_path / "evidence.txt").read_text(encoding="utf-8") == "original evidence"
    assert edited.runtime_binding.acp_session_id == "remote-new"
    assert original.history[-1].content == "DISCARDED LATER REQUEST"


def test_acp_normal_continue_reuses_the_current_remote_session(tmp_path, monkeypatch) -> None:
    api, original = _acp_edit_api(tmp_path)
    sessions: list[str] = []

    async def _run(_runtime, binding, **kwargs):
        sessions.append(binding.acp_session_id)
        assert "[ArgusAI edited conversation]" not in kwargs["message"]
        return _acp_result()

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    assert api._run_acp_assistant_turn({"session_id": "sess-a", "message": "Continue"})["ok"] is True
    assert sessions == ["remote-old"]
    assert api._assistant_sessions["sess-a"] is original
    assert original.history[-2].content == "Continue"


def test_acp_workbench_context_inherits_preferences_without_rewriting_user_history(tmp_path, monkeypatch) -> None:
    from argusai.core.workspace.user_profile import UserHabitsProfile
    api, original = _acp_edit_api(tmp_path)
    api.settings_mgr.get().user_habits = UserHabitsProfile(speaking_style="Short Chinese replies")
    marker = {"session_id": "remote-new", "instruction_hash": "a" * 64, "profile_hash": "b" * 64}
    calls = []
    async def _run(_runtime, _binding, **kwargs):
        calls.append(kwargs)
        return _acp_result(host_context=marker)
    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)

    result = api._run_acp_assistant_turn({
        "session_id": "sess-a", "message": "Capture the authorized demo traffic",
        "page": "proxy", "context": {"task_id": "task-demo"},
    })

    assert result["ok"], result
    assert "inside ArgusAI" in calls[0]["host_instruction"]
    assert "actual Agent identity" in calls[0]["host_instruction"]
    assert "tool_search" in calls[0]["host_instruction"]
    assert "argusai_invoke_tool" in calls[0]["host_instruction"]
    assert "get_app_snapshot" in calls[0]["host_instruction"]
    assert "Short Chinese replies" in calls[0]["user_habits"]
    assert '"page": "proxy"' in calls[0]["message"]
    assert '"task_id": "task-demo"' in calls[0]["message"]
    assert original.history[-2].content == "Capture the authorized demo traffic"
    assert original.runtime_binding.acp_host_context == marker


@pytest.mark.parametrize("failure", ["result", "exception", "metadata_then_result"])
def test_acp_edit_startup_failure_preserves_original_history_and_binding(tmp_path, monkeypatch, failure) -> None:
    api, original = _acp_edit_api(tmp_path)
    before = api._session_path("sess-a").read_text(encoding="utf-8")
    original_tool = original.registry.get("list_todos").fn

    async def _run(_runtime, _binding, **kwargs):
        if failure == "exception":
            raise OSError("runtime unavailable")
        if failure == "metadata_then_result":
            kwargs["on_update"]({"sessionUpdate": "available_commands_update", "availableCommands": []})
        return _acp_result(ok=False, text="", error="runtime rejected startup", error_code="acp_connect_failed")

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    result = api._run_acp_assistant_turn({"session_id": "sess-a", "message": "Edited", "regenerate": True, "regenerate_user_before": 3})
    assert result["ok"] is False and result["regenerate_rolled_back"] is True
    assert api._assistant_sessions["sess-a"] is original
    assert api._session_path("sess-a").read_text(encoding="utf-8") == before
    assert original.runtime_binding.acp_session_id == "remote-old"
    assert original.registry.get("list_todos").fn is original_tool


@pytest.mark.parametrize("target", [None, 999])
def test_acp_edit_unknown_target_never_starts_a_runtime_or_changes_history(tmp_path, monkeypatch, target) -> None:
    api, original = _acp_edit_api(tmp_path)
    before = original.to_dict()

    async def _run(*_args, **_kwargs):
        pytest.fail("an invalid edit must not start a runtime")

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    result = api._run_acp_assistant_turn({"session_id": "sess-a", "message": "Edited", "regenerate": True, "regenerate_user_before": target})
    assert result["ok"] is False
    assert api._assistant_sessions["sess-a"] is original
    assert original.to_dict() == before


def test_acp_edit_cancelled_while_preparing_media_never_starts_or_commits(tmp_path, monkeypatch) -> None:
    api, original = _acp_edit_api(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    results: list[dict] = []
    before = api._session_path("sess-a").read_text(encoding="utf-8")
    api._assistant_running_sessions.add("sess-a")
    api._assistant_streams["prep"] = {
        "session_id": "sess-a", "done": False,
        "lock": threading.Lock(), "events": [],
    }

    def _voice(voice, _root):
        entered.set()
        assert release.wait(timeout=3)
        return dict(voice) if voice else None

    async def _run(*_args, **_kwargs):
        calls.append("started")
        return _acp_result()

    monkeypatch.setattr("argusai.core.assistant.attachments._materialize_voice", _voice)
    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    worker = threading.Thread(target=lambda: results.append(api._run_acp_assistant_turn({
        "session_id": "sess-a", "message": "Edited after cancel", "regenerate": True,
        "regenerate_user_before": 3, "_assistant_stream_run_id": "prep",
    })))
    try:
        worker.start()
        assert entered.wait(timeout=2)
        api.cancel_assistant_run("sess-a")
        release.set()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert calls == []
        assert results[0]["status"] == "cancelled"
        assert results[0]["regenerate_rolled_back"] is True
        assert api._assistant_sessions["sess-a"] is original
        assert api._session_path("sess-a").read_text(encoding="utf-8") == before
    finally:
        release.set()
        worker.join(timeout=3)
        api._assistant_running_sessions.clear()


def test_acp_empty_chunk_does_not_commit_an_edit_before_startup_failure(tmp_path, monkeypatch) -> None:
    api, original = _acp_edit_api(tmp_path)
    before = api._session_path("sess-a").read_text(encoding="utf-8")

    async def _run(_runtime, _binding, **kwargs):
        kwargs["on_update"]({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ""}})
        return _acp_result(ok=False, text="", error="startup failed", error_code="acp_connect_failed")

    monkeypatch.setattr("argusai.core.agent_runtimes.run_acp_turn", _run)
    result = api._run_acp_assistant_turn({"session_id": "sess-a", "message": "Edited", "regenerate": True, "regenerate_user_before": 3})
    assert result["ok"] is False
    assert result.get("regenerate_rolled_back") is True
    assert api._assistant_sessions["sess-a"] is original
    assert api._session_path("sess-a").read_text(encoding="utf-8") == before


def test_dismiss_assistant_run_job_clears_paused_banner(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="paused-dismiss")
    store.transition(job, "running")
    store.transition(job, "paused", status="Recovered after an interrupted process")

    result = api.dismiss_assistant_run_job("sess-a", "paused-dismiss")
    assert result["ok"] is True
    assert result["job"]["state"] == "cancelled"
    reloaded = store.load("sess-a", "paused-dismiss", recover_running=False)
    assert reloaded.state is AssistantRunState.CANCELLED


def test_resume_rejects_when_tool_confirmation_pending(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="wait-a")
    store.transition(job, "running")
    store.transition(job, "waiting_user")
    api._assistant_sessions["sess-a"] = SimpleNamespace(_pending={"name": "run_shell_snippet"})

    denied = api.resume_assistant_run_job({
        "session_id": "sess-a",
        "job_id": "wait-a",
        "message": "继续",
    })
    assert denied["ok"] is False
    assert "pending tool confirmation" in denied["error"]


def test_resume_requires_user_message(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="run-a")
    store.transition(job, "running")
    store.load("sess-a", "run-a")  # crash-recover to paused

    denied = api.resume_assistant_run_job({"session_id": "sess-a", "job_id": "run-a", "message": "  "})
    assert denied["ok"] is False
    assert "continuation message" in denied["error"]


def test_resume_starts_stream_with_same_job_id_and_new_epoch(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="run-a")
    store.transition(job, "running")
    store.load("sess-a", "run-a")

    started = api.resume_assistant_run_job({
        "session_id": "sess-a",
        "job_id": "run-a",
        "message": "继续上次未完成的分析",
        "page": "assistant",
    })
    assert started["ok"] is True
    assert started["job_id"] == "run-a"
    assert started["job"]["job_id"] == "run-a"
    assert started["job"]["epoch"] == 2
    assert started["run_id"] != "run-a"

    events = api.get_assistant_stream_events(started["run_id"], 0)
    assert events["ok"] is True
    assert events["job"]["job_id"] == "run-a"
    assert events["job"]["epoch"] == 2
    assert api._chat_done.wait(2.0)
    assert api._chat_calls[0]["message"] == "继续上次未完成的分析"


def test_new_stream_closes_previous_waiting_jobs(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    waiting = store.create("sess-a", job_id="old-wait")
    store.transition(waiting, "running")
    store.transition(waiting, "waiting_user", status="need confirm")

    started = api.start_assistant_stream({
        "session_id": "sess-a",
        "message": "继续",
        "page": "assistant",
    })
    assert started["ok"] is True
    assert started["job_id"] != "old-wait"
    assert store.load("sess-a", "old-wait", recover_running=False).state is AssistantRunState.COMPLETED
    assert api._chat_done.wait(2.0)


def test_confirmation_uses_dedicated_typed_action_stream(tmp_path) -> None:
    api = _Api(tmp_path)
    denied = api.start_assistant_stream({
        "session_id": "sess-a",
        "message": "",
        "confirmation": {"type": "confirmation_decision", "verdict": "allow"},
        "confirmation_action": True,
    })
    assert denied["ok"] is False
    assert "start_assistant_action_stream" in denied["error"]

    store = AssistantRunJobStore(tmp_path)
    pending_job = store.create("sess-a", job_id="approval-turn")
    store.transition(pending_job, "running")
    store.transition(pending_job, "waiting_user")
    started = api.start_assistant_action_stream({
        "session_id": "sess-a",
        "job_id": "approval-turn",
        "action": {
            "type": "confirmation_decision",
            "pending_id": "pending-a",
            "version": 1,
            "call_id": "call-a",
            "verdict": "allow",
        },
    })
    assert started["ok"] is True
    assert started["job_id"] == "approval-turn"
    assert api._chat_done.wait(2.0)
    params = api._chat_calls[-1]
    assert params["message"] == ""
    assert params["confirmation"]["pending_id"] == "pending-a"
    assert params["_typed_action_rpc"] is True


def test_resume_accepts_waiting_user_jobs(tmp_path) -> None:
    api = _Api(tmp_path)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess-a", job_id="wait-a")
    store.transition(job, "running")
    store.transition(job, "waiting_user")

    started = api.resume_assistant_run_job({
        "session_id": "sess-a",
        "job_id": "wait-a",
        "message": "允许并继续",
    })
    assert started["ok"] is True
    assert started["job_id"] == "wait-a"
    assert started["job"]["epoch"] == 2
    assert api._chat_done.wait(2.0)
