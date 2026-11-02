"""GUI cron wiring keeps busy/result protocol typed."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
from argusai.gui.assistant_cron import AssistantCronMixin
from argusai.gui.assistant_sessions import AssistantSessionsMixin


class _SettingsMgr:
    def __init__(self, root: Path) -> None:
        self._settings = SimpleNamespace(
            data_root=str(root),
            sessions_dir=str(root / "sessions"),
            assistant_desktop_notifications=False,
        )

    def get(self):
        return self._settings


class _Api(AssistantCronMixin):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.settings_mgr = _SettingsMgr(root)
        self._assistant_sessions: dict[str, AssistantSession] = {}
        self._assistant_streams: dict[str, dict] = {}
        self._assistant_running_sessions: set[str] = set()
        self._assistant_run_lock = threading.Lock()
        self.next_result = {"ok": True, "status": "completed", "text": "done"}
        self.last_action_params: dict | None = None
        self.last_stream_params: dict | None = None

    def _ensure_session(self, session_id: str) -> AssistantSession:
        session = self._assistant_sessions.get(session_id)
        if session is None:
            registry = ToolRegistry()
            registry.register(
                "run_python_snippet",
                "Run Python",
                {"code": {"type": "string"}},
                lambda **_kwargs: {"ok": True},
                risk="high",
            )
            # Confirmable but auto-approvable — typical cron patrol tool.
            registry.register(
                "http_curl",
                "HTTP curl",
                {"url": {"type": "string"}},
                lambda **_kwargs: {"ok": True},
                risk="medium",
            )
            session = AssistantSession(registry=registry)
            self._assistant_sessions[session_id] = session
        return session

    def _session_path(self, session_id: str) -> Path:
        path = self.root / "sessions" / f"{session_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _save_session(self, path: Path, session: AssistantSession, name: str) -> None:
        payload = session.to_dict()
        payload["name"] = name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _start_assistant_stream(self, params: dict) -> dict:
        self.last_stream_params = dict(params)
        run_id = "stream-cron"
        self._assistant_streams[run_id] = {
            "session_id": params["session_id"],
            "done": True,
            "events": [{"type": "done", "result": dict(self.next_result)}],
        }
        return {"ok": True, "run_id": run_id, "job_id": "durable-cron"}

    def start_assistant_action_stream(self, params: dict) -> dict:
        self.last_action_params = dict(params)
        return self._start_assistant_stream(params)


def test_busy_reason_uses_pending_not_stale_waiting_job(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    session = api._ensure_session("session-a")
    assert api._assistant_cron_is_busy("session-a") == ""
    # Pending confirmations no longer block the cron scheduler — ticks force-allow.
    session._pending = {"name": "run_shell_snippet"}
    assert api._assistant_cron_is_busy("session-a") == ""
    session._pending = None
    api._assistant_running_sessions.add("session-a")
    assert api._assistant_cron_is_busy("session-a") == "running_target"
    assert api._assistant_cron_is_busy("unrelated") == ""


def test_cron_recovers_its_plan_but_not_an_interactive_plan(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    cron_session = api._ensure_session("cron-target")
    cron_session.set_todos([{"id": "tick", "title": "巡检"}])
    assert cron_session._todos_plan_approved is False

    assert api._assistant_cron_is_busy(
        "cron-target",
        {"id": "cron-plan", "destination": "new_session"},
    ) == ""
    assert cron_session._todos_plan_approved is True

    # destination=session also auto-approves under full unattended cron policy.
    interactive = api._ensure_session("interactive")
    interactive.set_todos([{"id": "manual", "title": "手工计划"}])
    assert api._assistant_cron_is_busy(
        "interactive",
        {"id": "cron-thread", "destination": "session"},
    ) == ""
    assert interactive._todos_plan_approved is True


def test_cron_fire_force_allows_waiting_confirmation(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    # First stream pauses; force-allow resume completes.
    states = iter([
        {"ok": True, "status": "awaiting_confirmation", "text": "confirm?"},
        {"ok": True, "status": "completed", "text": "done after allow"},
    ])

    def _stream(params: dict) -> dict:
        api.last_stream_params = dict(params)
        run_id = f"stream-{len(api._assistant_streams)+1}"
        result = next(states)
        api._assistant_streams[run_id] = {
            "session_id": params["session_id"],
            "done": True,
            "events": [{"type": "done", "result": dict(result)}],
        }
        # Seed a pending confirmation so the force-allow action can be built.
        session = api._ensure_session(params["session_id"])
        if result.get("status") == "awaiting_confirmation":
            session._pending = {
                "confirmations": [{
                    "name": "run_python_snippet",
                    "arguments": {"code": "print(1)"},
                    "tool_call": {"id": "call-1"},
                }],
                "tool_calls": [],
                "messages": [],
                "assistant_response": {},
                "decisions": {},
            }
        else:
            session._pending = None
        return {"ok": True, "run_id": run_id, "job_id": "durable-cron"}

    api._start_assistant_stream = _stream
    api.start_assistant_action_stream = _stream
    result = api._fire_assistant_cron_job({
        "id": "cron-status",
        "name": "Status check",
        "prompt": "run",
        "destination": "new_session",
        "approval": {"high_risk_ack": True},
        "notify": {"report_to_origin": False},
    })
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert "done after allow" in result["summary"]
    session = api._assistant_sessions[result["session_id"]]
    assert sum(event["kind"] == "cron_wakeup" for event in session.events) == 1


def test_cron_fire_injects_explicit_context_snapshot(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    result = api._fire_assistant_cron_job({
        "id": "cron-context",
        "name": "Context check",
        "prompt": "Inspect the frozen target",
        "context": {
            "target_url": "https://example.test",
            "baseline_ref": "kb-123",
        },
        "destination": "new_session",
        "notify": {"report_to_origin": False},
    })

    assert result["status"] == "completed"
    assert api.last_stream_params is not None
    assert api.last_stream_params["context"] == {
        "target_url": "https://example.test",
        "baseline_ref": "kb-123",
    }


def test_cron_report_is_typed_and_excluded_from_model_context(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    assert api._report_cron_result_to_origin(
        {
            "id": "cron-report",
            "name": "Daily patrol",
            "origin_session_id": "origin",
            "notify": {"report_to_origin": True},
        },
        status="completed",
        summary="No change.",
        fire_session_id="cron-session",
    )
    session = api._assistant_sessions["origin"]
    assert session.events[-1]["kind"] == "cron_result"
    assert session.events[-1]["context_policy"] == "exclude"
    assert session.events[-1]["payload"]["summary_truncated"] is False
    assert session.events[-1]["payload"]["detail_ref"] == {
        "kind": "assistant_session",
        "session_id": "cron-session",
    }
    assert session.history[-1].source == "cron_result"
    assert "[cron result" not in session.history[-1].content.lower()


def test_cron_report_runs_as_system_input_and_refreshes_live_ui(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    captured: dict = {}

    def _run(params: dict) -> dict:
        captured.update(params)
        session = api._ensure_session(params["session_id"])
        session.history.append(ChatMessage(role="assistant", content="巡检完成，无异常。"))
        return {"ok": True, "status": "completed", "text": "巡检完成，无异常。"}

    class _Window:
        script = ""

        def evaluate_js(self, script: str) -> None:
            self.script = script

    api._run_assistant_chat = _run
    api._window = _Window()
    assert api._report_cron_result_to_origin(
        {
            "id": "cron-live",
            "name": "Live patrol",
            "origin_session_id": "origin",
            "notify": {"report_to_origin": True},
        },
        status="completed",
        summary="No findings.",
        fire_session_id="cron-session",
    )

    session = api._assistant_sessions["origin"]
    assert captured["message_source"] == "cron_result"
    assert captured["hide_user_message"] is True
    assert captured["thinking_enabled"] is False
    assert session.history[-1].content == "巡检完成，无异常。"
    assert session.history[-1].source == ""
    assert session.events[-1]["payload"]["delivery"] == "agent_processed"
    assert "argusai:assistant-session-updated" in api._window.script


def test_cron_report_marks_truncated_summary_and_links_exact_run(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    captured: dict = {}

    def _run(params: dict) -> dict:
        captured.update(json.loads(params["message"]))
        return {"ok": True, "status": "completed", "text": "收到。"}

    api._run_assistant_chat = _run
    assert api._report_cron_result_to_origin(
        {
            "id": "cron-long",
            "name": "Long patrol",
            "origin_session_id": "origin",
            "notify": {"report_to_origin": True},
        },
        status="completed",
        summary="x" * 1900,
        fire_session_id="cron-run-exact",
    )

    assert captured["summary_truncated"] is True
    assert len(captured["summary"]) == 1800
    assert captured["detail_ref"] == {
        "kind": "assistant_session",
        "session_id": "cron-run-exact",
    }


def test_cron_report_falls_back_when_agent_projection_fails(tmp_path: Path) -> None:
    api = _Api(tmp_path)

    def _run(_params: dict) -> dict:
        return {
            "ok": True,
            "status": "ai_error",
            "text": "AI endpoint request failed",
        }

    api._run_assistant_chat = _run
    assert api._report_cron_result_to_origin(
        {
            "id": "cron-fallback",
            "name": "Fallback patrol",
            "origin_session_id": "origin",
            "notify": {"report_to_origin": True},
        },
        status="completed",
        summary="No findings.",
        fire_session_id="cron-session",
    )

    session = api._assistant_sessions["origin"]
    assert session.history[-1].source == "cron_result"
    assert "No findings." in session.history[-1].content
    assert session.events[-1]["payload"]["delivery"] == "projected_fallback"


def test_new_session_cron_seeds_all_approvals_including_high_risk(tmp_path: Path) -> None:
    """The explicit job-scoped acknowledgement unlocks high-risk tools."""
    api = _Api(tmp_path)
    target = api._ensure_session("cron-seed")
    job = {
        "id": "cron-seed",
        "name": "Seeded task",
        "destination": "new_session",
        "origin_session_id": "",
        "approval": {"high_risk_ack": True},
    }

    receipt = api._sync_cron_session_permissions(job, "cron-seed", target)
    assert receipt["changed"] is True
    assert "http_curl" in target.auto_approve_tools()
    assert "run_python_snippet" in target.auto_approve_tools()

    target._pending = {
        "confirmations": [{
            "name": "run_python_snippet",
            "arguments": {"code": "print(1)"},
            "tool_call": {"id": "call-py"},
        }],
        "tool_calls": [],
        "messages": [],
        "assistant_response": {},
        "decisions": {},
    }
    action = api._cron_inherited_approval_action(target, job)
    assert action is not None
    assert action["decisions"] == {"call-py": "allow"}
    assert api._assistant_cron_is_busy("cron-seed", job) == ""


def test_cron_without_high_risk_grant_fails_fast_not_pending(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    target = api._ensure_session("cron-standard")
    job = {
        "id": "cron-standard",
        "name": "Standard only",
        "destination": "session",
        "session_id": "cron-standard",
        "approval": {"high_risk_ack": False},
        "notify": {"report_to_origin": False},
    }
    api._sync_cron_session_permissions(job, "cron-standard", target)
    assert "http_curl" in target.auto_approve_tools()
    assert "run_python_snippet" not in target.auto_approve_tools()
    target._pending = {
        "confirmations": [{
            "name": "run_python_snippet",
            "arguments": {"code": "print(1)"},
            "tool_call": {"id": "call-blocked"},
        }],
        "tool_calls": [],
        "messages": [],
        "assistant_response": {},
        "decisions": {},
    }

    result = api._fire_assistant_cron_job(job)

    assert result["status"] == "blocked"
    assert "run_python_snippet" in result["error"]
    assert target._pending is None
    assert not any(event["kind"] == "cron_wakeup" for event in target.events)


def test_gui_grant_persists_job_scoped_high_risk_receipt(tmp_path: Path) -> None:
    from argusai.core.assistant.cron import CronStore

    api = _Api(tmp_path)
    job = CronStore(tmp_path).upsert_job({
        "name": "Grant",
        "prompt": "run",
        "schedule": {"kind": "interval", "every_seconds": 60},
    })
    result = api.grant_assistant_cron_high_risk(
        job["id"],
        True,
        source="gui",
        session_id="origin",
    )
    assert result["ok"] is True
    assert result["approval"]["high_risk_ack"] is True
    stored = CronStore(tmp_path).get_job(job["id"])
    assert stored is not None
    assert stored["approval"]["grant_session_id"] == "origin"


def test_gui_webhook_secret_is_shown_once_and_rotatable(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    payload = {
        "name": "Hook",
        "prompt": "inspect",
        "schedule": {"kind": "webhook"},
    }
    created = api.save_assistant_cron_job(payload)
    assert created["ok"] is True
    assert created["webhook"]["secret_created"] is True
    assert created["webhook"]["signing_secret"]
    job_id = created["job"]["id"]

    saved_again = api.save_assistant_cron_job({
        **payload,
        "id": job_id,
    })
    assert saved_again["webhook"]["secret_created"] is False
    assert saved_again["webhook"]["signing_secret"] == ""

    rotated = api.rotate_assistant_cron_webhook_secret(job_id)
    assert rotated["ok"] is True
    assert rotated["signing_secret"]
    assert rotated["signing_secret"] != created["webhook"]["signing_secret"]
    listed = api.list_assistant_cron_jobs()
    row = next(item for item in listed["jobs"] if item["id"] == job_id)
    assert row["webhook"]["url"].endswith(job_id)
    assert "signing_secret" not in row["webhook"]


def test_event_payload_is_injected_as_typed_context(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    result = api._fire_assistant_cron_job({
        "id": "cron-event",
        "name": "Event",
        "prompt": "inspect",
        "context": {"static": "value"},
        "_trigger_event": {
            "event_id": "deploy-1",
            "source": "webhook",
            "created_at": 1.0,
            "payload": {"body": {"ref": "main"}},
        },
        "destination": "new_session",
        "notify": {"report_to_origin": False},
    })
    assert result["status"] == "completed"
    assert api.last_stream_params is not None
    assert api.last_stream_params["context"]["static"] == "value"
    assert api.last_stream_params["context"]["trigger_event"]["event_id"] == "deploy-1"
    assert api.last_stream_params["context"]["trigger_event"]["payload"] == {
        "body": {"ref": "main"},
    }


def test_new_session_cron_inherits_origin_approval_state(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    origin = api._ensure_session("origin")
    origin._auto_approve_tools = {"run_python_snippet"}
    origin._auto_approve_permissions = {"mcp:email:send_email"}
    target = api._ensure_session("cron-target")
    job = {
        "id": "cron-inherit",
        "name": "Inherited task",
        "destination": "new_session",
        "origin_session_id": "origin",
        "approval": {"high_risk_ack": True},
    }

    receipt = api._sync_cron_session_permissions(job, "cron-target", target)
    assert receipt["changed"] is True
    assert "run_python_snippet" in target.auto_approve_tools()
    assert "http_curl" in target.auto_approve_tools()
    assert target.auto_approve_permissions() == ["mcp:email:send_email"]
    assert target.events[-1]["kind"] == "permission_state_inherited"

    target._pending = {
        "confirmations": [{
            "name": "run_python_snippet",
            "arguments": {"code": "print(1)"},
            "tool_call": {"id": "call-auto"},
        }],
        "tool_calls": [],
        "messages": [],
        "assistant_response": {},
        "decisions": {},
    }
    action = api._cron_inherited_approval_action(target, job)
    assert action is not None
    assert action["decisions"] == {"call-auto": "allow"}
    assert api._assistant_cron_is_busy("cron-target", job) == ""

    # The job-scoped grant remains authoritative if origin grants are revoked.
    origin._auto_approve_tools.clear()
    origin._auto_approve_permissions.clear()
    target.replace_auto_approve_state([], [])
    api._sync_cron_session_permissions(job, "cron-target", target)
    assert "run_python_snippet" in target.auto_approve_tools()
    assert api._assistant_cron_is_busy("cron-target", job) == ""


def test_each_new_session_run_has_its_own_conversation(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    origin = api._ensure_session("origin")
    origin._auto_approve_tools = {"run_python_snippet"}
    job = {
        "id": "cron-distinct-job",
        "name": "Distinct",
        "prompt": "tick",
        "destination": "new_session",
        "session_id": "cron-old-latest",
        "origin_session_id": "origin",
        "approval": {"high_risk_ack": True},
        "notify": {"report_to_origin": False},
    }

    first = api._fire_assistant_cron_job(job)
    second = api._fire_assistant_cron_job(job)

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert first["session_id"] != second["session_id"]
    assert first["session_id"].startswith("cron-")
    assert second["session_id"].startswith("cron-")
    for sid in (first["session_id"], second["session_id"]):
        approved = api._assistant_sessions[sid].auto_approve_tools()
        assert "run_python_snippet" in approved  # inherited from origin
        assert "http_curl" in approved  # seeded for unattended cron
        assert sid != "cron-old-latest"


def test_dedicated_session_is_created_once_and_reused(tmp_path: Path) -> None:
    from argusai.core.assistant.cron import CronStore

    api = _Api(tmp_path)
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "id": "cron-dedicated",
        "name": "Dedicated",
        "prompt": "continue",
        "destination": "dedicated_session",
        "schedule": {"kind": "interval", "every_seconds": 60},
        "notify": {"report_to_origin": False},
    })

    first = api._fire_assistant_cron_job(job)
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert refreshed["session_id"] == first["session_id"]
    assert first["session_id"].endswith("-thread")

    second = api._fire_assistant_cron_job(refreshed)
    assert second["session_id"] == first["session_id"]
    assert list(api._assistant_sessions) == [first["session_id"]]
    wakes = [
        event for event in api._assistant_sessions[first["session_id"]].events
        if event["kind"] == "cron_wakeup"
    ]
    assert len(wakes) == 2
    assert wakes[0]["id"] != wakes[1]["id"]


@pytest.mark.parametrize("trigger_kind", ["interval", "cron", "webhook", "file_change"])
@pytest.mark.parametrize("destination", ["session", "new_session", "dedicated_session"])
def test_accepted_cron_wakeup_persists_typed_timeline_metadata(
    tmp_path: Path,
    trigger_kind: str,
    destination: str,
) -> None:
    api = _Api(tmp_path)
    job = {
        "id": "cron-wake",
        "name": "Scheduled review",
        "prompt": "Continue the authorized review",
        "destination": destination,
        "session_id": "fixed-chat",
        "schedule": {"kind": trigger_kind},
        "notify": {"report_to_origin": False},
    }
    if trigger_kind in {"webhook", "file_change"}:
        job["_trigger_event"] = {
            "event_id": "trigger-123",
            "source": trigger_kind,
            "payload": {"private_input": "not presentation metadata"},
        }

    result = api._fire_assistant_cron_job(job)

    assert result["status"] == "completed"
    session = api._assistant_sessions[result["session_id"]]
    events = [event for event in session.events if event["kind"] == "cron_wakeup"]
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "cron"
    assert event["render_policy"] == "timeline"
    assert event["context_policy"] == "exclude"
    assert event["timestamp"] > 0
    assert event["payload"] == {
        "job_id": "cron-wake",
        "job_name": "Scheduled review",
        "trigger_kind": trigger_kind,
        "destination": destination,
        "history_offset": 0,
        "run_id": "stream-cron",
        "assistant_run_job_id": "durable-cron",
        **({"trigger_event_id": "trigger-123"} if "_trigger_event" in job else {}),
    }
    # Presentation metadata adds neither a second scheduled input nor a fake
    # human turn. The real runtime still owns writing the hidden cron prompt.
    assert session.history == []
    assert api.last_stream_params["message"] == job["prompt"]
    assert api.last_stream_params["message_source"] == "cron"
    assert api.last_stream_params["message_source_ref"] == "cron-wake"
    assert api.last_stream_params["hide_user_message"] is True
    stored = json.loads(api._session_path(result["session_id"]).read_text(encoding="utf-8"))
    assert event in stored["events"]
    assert "private_input" not in json.dumps(event)


def test_cron_wakeup_survives_reload_and_projects_before_fast_reply(tmp_path: Path) -> None:
    class _ReadApi(_Api, AssistantSessionsMixin):
        pass

    api = _ReadApi(tmp_path)
    session = api._ensure_session("fixed-chat")
    session.history = [
        ChatMessage(role="user", content="Review this target", timestamp=1),
        ChatMessage(role="assistant", content="Starting the review", timestamp=2),
        ChatMessage(role="tool", content="previous tool evidence", timestamp=3),
        ChatMessage(role="user", content="old scheduled input", source="cron", ui_hidden=True, timestamp=4),
    ]
    session._prompt_system_catalog = "stable cached system catalogue"
    original_start = api._start_assistant_stream

    def _fast_start(params: dict) -> dict:
        # Simulate the actual runtime committing both its one hidden input and
        # a reply before the stream-start call returns to the scheduler.
        import time

        session.history.extend([
            ChatMessage(
                role="user", content=params["message"], ui_hidden=True,
                source="cron", source_ref="cron-fast", timestamp=time.time(),
            ),
            ChatMessage(role="assistant", content="New result", timestamp=time.time()),
        ])
        return original_start(params)

    api._start_assistant_stream = _fast_start
    result = api._fire_assistant_cron_job({
        "id": "cron-fast", "name": "Fast review", "prompt": "Check now",
        "destination": "session", "session_id": "fixed-chat",
        "schedule": {"kind": "interval"},
        "notify": {"report_to_origin": False},
    })

    assert result["status"] == "completed"
    stored = json.loads(api._session_path("fixed-chat").read_text(encoding="utf-8"))
    restored = AssistantSession.from_dict(stored, registry=session.registry)
    api._assistant_sessions["fixed-chat"] = restored
    projection = api.get_assistant_session("fixed-chat")

    assert projection["ok"] is True
    wake = next(event for event in projection["events"] if event["kind"] == "cron_wakeup")
    assert wake["payload"]["history_offset"] == 2
    assert wake["timestamp"] <= projection["history"][-1]["timestamp"]
    assert [message["content"] for message in projection["history"]] == [
        "Review this target", "Starting the review", "New result",
    ]
    assert sum(message["content"] == "Check now" for message in stored["history"]) == 1
    assert restored._prompt_system_catalog == "stable cached system catalogue"
    assert not any(message.get("source") == "cron_wakeup" for message in stored["history"])


@pytest.mark.parametrize("raises", [False, True])
def test_rejected_stream_start_does_not_create_a_wakeup(tmp_path: Path, raises: bool) -> None:
    api = _Api(tmp_path)

    def _reject(_params: dict) -> dict:
        if raises:
            raise RuntimeError("start failed")
        return {"ok": False, "error": "assistant session is already running"}

    api._start_assistant_stream = _reject
    result = api._fire_assistant_cron_job({
        "id": "cron-rejected", "name": "Rejected", "prompt": "run",
        "destination": "session", "session_id": "fixed-chat",
        "notify": {"report_to_origin": False},
    })

    assert result["status"] == "failed"
    assert not any(
        event["kind"] == "cron_wakeup"
        for event in api._assistant_sessions["fixed-chat"].events
    )


def test_accepted_wakeup_is_retained_when_the_run_fails(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    api.next_result = {"ok": False, "status": "failed", "error": "endpoint unavailable"}
    result = api._fire_assistant_cron_job({
        "id": "cron-run-failed", "name": "Failed run", "prompt": "run",
        "destination": "session", "session_id": "fixed-chat",
        "notify": {"report_to_origin": False},
    })

    assert result["status"] == "failed"
    stored = json.loads(api._session_path("fixed-chat").read_text(encoding="utf-8"))
    assert sum(event["kind"] == "cron_wakeup" for event in stored["events"]) == 1


def test_live_refresh_is_sent_after_wakeup_is_persisted(tmp_path: Path) -> None:
    api = _Api(tmp_path)
    refreshes: list[str] = []

    class _Window:
        def evaluate_js(self, script: str) -> None:
            stored = json.loads(api._session_path("fixed-chat").read_text(encoding="utf-8"))
            assert any(event["kind"] == "cron_wakeup" for event in stored["events"])
            refreshes.append(script)

    api._window = _Window()
    result = api._fire_assistant_cron_job({
        "id": "cron-live-wake", "name": "Live wake", "prompt": "run",
        "destination": "session", "session_id": "fixed-chat",
        "notify": {"report_to_origin": False},
    })

    assert result["status"] == "completed"
    assert len(refreshes) == 1
    assert "argusai:assistant-session-updated" in refreshes[0]
    assert "cron_stream_started" in refreshes[0]
