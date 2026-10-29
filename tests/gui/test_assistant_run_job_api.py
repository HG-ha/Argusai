"""GUI mixin coverage for durable assistant run jobs."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

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
