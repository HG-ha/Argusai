"""Tests for assistant session title / note metadata."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from argusai.core.assistant import AssistantSession, ChatMessage
from argusai.gui.assistant_sessions import AssistantSessionsMixin


class _SettingsMgr:
    def __init__(self, sessions_dir) -> None:
        self._settings = SimpleNamespace(
            sessions_dir=str(sessions_dir),
            agent_runtimes=[],
        )

    def get(self):
        return self._settings


class _Api(AssistantSessionsMixin):
    def __init__(self, sessions_dir) -> None:
        self.settings_mgr = _SettingsMgr(sessions_dir)
        self._assistant_sessions = {}
        self.kb = None


def test_token_usage_exposes_prompt_context(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.last_prompt_tokens_est = 123_400
    session.last_prompt_bytes = 493_600
    session._context_window_tokens = lambda: 256_000  # type: ignore[method-assign]
    api._assistant_sessions["sess-ctx"] = session

    usage = api.get_assistant_token_usage("sess-ctx")
    assert usage["ok"] is True
    assert usage["prompt_tokens"] == 123_400
    assert usage["prompt_bytes"] == 493_600
    assert usage["context_window"] == 256_000

    dumped = session.to_dict()
    assert dumped["last_prompt_tokens_est"] == 123_400
    restored = AssistantSession.from_dict(dumped, session.registry)
    assert restored.last_prompt_tokens_est == 123_400
    assert restored.last_prompt_bytes == 493_600


def test_token_usage_ring_tracks_current_history_not_last_prompt(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.last_prompt_bytes = 400
    session.last_prompt_tokens_est = 100
    session.last_prompt_history_bytes = 200
    session.history.append(ChatMessage(role="user", content="u" * 200))
    session.history.append(ChatMessage(role="assistant", content="a" * 400))
    session._context_window_tokens = lambda: 128_000  # type: ignore[method-assign]
    api._assistant_sessions["sess-ctx"] = session

    usage = api.get_assistant_token_usage("sess-ctx")
    assert usage["ok"] is True
    # No provider usage yet: current transcript (600) + last-call overhead (200).
    assert usage["prompt_tokens"] == 200
    assert usage["prompt_bytes"] == 800
    assert usage["context_window"] == 128_000


def test_token_usage_ring_prefers_provider_prompt_tokens(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.last_prompt_bytes = 1_620_000
    session.last_prompt_tokens_est = 405_000
    session.last_prompt_history_bytes = 200
    session.last_token_usage = {"input_tokens": 18_400, "output_tokens": 800}
    session.history.append(ChatMessage(role="user", content="u" * 200))
    session.history.append(ChatMessage(role="assistant", content="a" * 400))
    session._context_window_tokens = lambda: 1_000_000  # type: ignore[method-assign]
    api._assistant_sessions["sess-ctx"] = session

    usage = api.get_assistant_token_usage("sess-ctx")
    assert usage["ok"] is True
    # 18400 billed prompt + (600-200)//4 growth, not the 405k byte/4 estimate.
    assert usage["prompt_tokens"] == 18_500
    assert usage["context_window"] == 1_000_000


def test_thinking_defaults_off_and_honors_saved_on() -> None:
    fresh = AssistantSession()
    assert fresh.thinking_enabled is False
    assert fresh.to_dict()["thinking_enabled"] is False
    assert fresh.thinking_effort == "high"
    assert fresh.to_dict()["thinking_effort"] == "high"

    restored = AssistantSession.from_dict({"history": []}, fresh.registry)
    assert restored.thinking_enabled is False
    assert restored.thinking_effort == "high"

    on = AssistantSession.from_dict(
        {"history": [], "thinking_enabled": True, "thinking_effort": "max"},
        fresh.registry,
    )
    assert on.thinking_enabled is True
    assert on.thinking_effort == "max"


def test_get_assistant_session_returns_saved_thinking(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.thinking_enabled = True
    session.thinking_effort = "max"
    api._save_session(api._session_path("think-sess"), session, "think")

    detail = api.get_assistant_session("think-sess")
    assert detail["ok"] is True
    assert detail["thinking_enabled"] is True
    assert detail["thinking_effort"] == "max"


def test_set_assistant_thinking_persists_and_reloads(tmp_path) -> None:
    from argusai.gui.assistant_tools import AssistantToolsMixin

    class _ThinkingApi(AssistantToolsMixin, _Api):
        pass

    api = _ThinkingApi(tmp_path)
    session = AssistantSession()
    api._save_session(api._session_path("think-sess"), session, "think")

    result = api.set_assistant_thinking("think-sess", True, "xhigh")
    assert result["ok"] is True
    assert result["thinking_enabled"] is True
    assert result["thinking_effort"] == "xhigh"

    api._assistant_sessions.clear()
    detail = api.get_assistant_session("think-sess")
    assert detail["thinking_enabled"] is True
    assert detail["thinking_effort"] == "xhigh"


def test_channel_live_defaults_on_and_honors_saved_off() -> None:
    fresh = AssistantSession()
    assert fresh.channel_live is True
    assert fresh.to_dict()["channel_live"] is True

    restored = AssistantSession.from_dict({"history": []}, fresh.registry)
    assert restored.channel_live is True

    off = AssistantSession.from_dict({"history": [], "channel_live": False}, fresh.registry)
    assert off.channel_live is False


def test_ensure_session_loads_persisted_history_before_session_mutation(tmp_path) -> None:
    api = _Api(tmp_path)
    saved = AssistantSession()
    saved.history.append(ChatMessage(role="user", content="keep this turn", timestamp=123.0))
    api._save_session(api._session_path("sess-a"), saved, "saved")

    loaded = api._ensure_session("sess-a")

    assert [(item.role, item.content, item.timestamp) for item in loaded.history] == [
        ("user", "keep this turn", 123.0),
    ]


def test_update_session_metadata_roundtrip(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    path = api._session_path("sess-a")
    api._save_session(path, session, "old name")

    updated = api.update_assistant_session("sess-a", {"name": "new name", "note": "important note"})
    assert updated["ok"]
    assert updated["session"]["name"] == "new name"
    assert updated["session"]["note"] == "important note"

    listed = api.list_assistant_sessions()
    assert listed[0]["name"] == "new name"
    assert listed[0]["note"] == "important note"

    detail = api.get_assistant_session("sess-a")
    assert detail["name"] == "new name"
    assert detail["note"] == "important note"


def test_update_session_context_notes_roundtrip_and_live_state(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    path = api._session_path("sess-context")
    api._save_session(path, session, "context")
    api._assistant_sessions["sess-context"] = session

    updated = api.update_assistant_session(
        "sess-context",
        {"context_notes": "Keep the target limited to staging.example."},
    )

    assert updated["ok"] is True
    assert updated["session"]["context_notes"] == "Keep the target limited to staging.example."
    assert session.context_notes == "Keep the target limited to staging.example."
    api._assistant_sessions.clear()
    detail = api.get_assistant_session("sess-context")
    assert detail["context_notes"] == "Keep the target limited to staging.example."


def test_save_session_preserves_existing_note(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    path = api._session_path("sess-a")
    api._save_session(path, session, "old name")
    api.update_assistant_session("sess-a", {"note": "keep me"})

    session.history.append(ChatMessage(role="assistant", content="ok"))
    api._save_session(path, session, "old name")

    detail = api.get_assistant_session("sess-a")
    assert detail["note"] == "keep me"


def test_save_session_keeps_the_name_when_none_is_supplied(tmp_path) -> None:
    """Runtime-only saves (model switch, /live toggle) carry no name to write."""
    api = _Api(tmp_path)
    session = AssistantSession()
    path = api._session_path("sess-a")
    api._save_session(path, session, "扫 example.com")

    session.history.append(ChatMessage(role="assistant", content="ok"))
    api._save_session(path, session, "")

    assert api.get_assistant_session("sess-a")["name"] == "扫 example.com"


def test_session_detail_exposes_active_external_stream(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    api._save_session(api._session_path("cron-session"), session, "Cron")
    api._assistant_streams = {
        "run-live": {
            "session_id": "cron-session",
            "done": False,
            "lock": threading.Lock(),
            "events": [{"type": "status"}, {"type": "delta"}],
            "cron_job_id": "cron-job",
            "job": None,
        },
    }

    detail = api.get_assistant_session("cron-session")

    assert detail["active_stream"] == {
        "run_id": "run-live",
        "event_count": 2,
        "cron_job_id": "cron-job",
        "job": None,
    }


def test_session_list_exposes_runtime_and_sandbox_state(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    api._save_session(api._session_path("sess-live"), session, "Live")
    (tmp_path / "sess-live.sandbox").mkdir()
    api._assistant_run_lock = threading.Lock()
    api._assistant_running_sessions = {"sess-live"}
    api._assistant_streams = {}

    listed = api.list_assistant_sessions()

    assert listed[0]["is_running"] is True
    assert listed[0]["pending_count"] == 0
    assert listed[0]["has_sandbox"] is True


def test_session_list_hides_cron_owned_chats_and_classifies_types(tmp_path) -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding

    api = _Api(tmp_path)

    native = AssistantSession()
    native.history.append(ChatMessage(role="user", content="hello"))
    api._save_session(api._session_path("native-chat"), native, "Native")

    channel = AssistantSession()
    channel.history.append(
        ChatMessage(role="user", content="remote", source="channel")
    )
    api._save_session(api._session_path("channel-chat"), channel, "Channel")

    local = AssistantSession()
    local.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "cursor-acp",
        "workspace_mode": "sandbox",
    })
    local.history.append(ChatMessage(role="user", content="code"))
    api._save_session(api._session_path("local-agent-chat"), local, "Local")

    scheduled = AssistantSession()
    scheduled.history.append(ChatMessage(role="assistant", content="cron result"))
    api._save_session(
        api._session_path("cron-owned-thread"),
        scheduled,
        "Scheduled",
    )

    listed = {item["id"]: item for item in api.list_assistant_sessions()}
    assert "cron-owned-thread" not in listed
    assert listed["native-chat"]["session_type"] == "native"
    assert listed["channel-chat"]["session_type"] == "channel"
    assert listed["local-agent-chat"]["session_type"] == "local_agent"
    # Hidden from normal history does not make the run transcript inaccessible.
    assert api.get_assistant_session("cron-owned-thread")["ok"] is True


def test_session_list_exposes_project_dir(tmp_path) -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding

    project = tmp_path / "repo"
    project.mkdir()
    api = _Api(tmp_path)
    session = AssistantSession()
    session.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "argusai-native",
        "workspace_mode": "project",
        "project_dir": str(project),
    })
    session.history.append(ChatMessage(role="user", content="hello"))
    api._save_session(api._session_path("sess-project"), session, "Project")

    listed = api.list_assistant_sessions()
    assert listed[0]["workspace_mode"] == "project"
    assert listed[0]["project_dir"] == str(project)


def test_runtime_binding_keeps_project_when_frontend_drops_path(tmp_path) -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding

    project = tmp_path / "repo"
    project.mkdir()
    api = _Api(tmp_path)
    session = AssistantSession()
    session.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "argusai-native",
        "workspace_mode": "project",
        "project_dir": str(project),
    })
    api._assistant_sessions["sess-a"] = session

    binding = api._assistant_runtime_binding("sess-a", {
        "runtime_id": "argusai-native",
        "workspace_mode": "project",
        "project_dir": "",
    })
    assert binding.workspace_mode == "project"
    assert Path(binding.project_dir).resolve() == project.resolve()


def test_session_list_reuses_unchanged_file_metadata(tmp_path, monkeypatch) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    path = api._session_path("sess-cached")
    api._save_session(path, session, "Cached")
    real_read_text = Path.read_text
    reads = 0

    def counted_read_text(self, *args, **kwargs):
        nonlocal reads
        if self == path:
            reads += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted_read_text)

    assert api.list_assistant_sessions()[0]["name"] == "Cached"
    assert api.list_assistant_sessions()[0]["name"] == "Cached"
    assert reads == 1


def test_delete_assistant_session_removes_json_and_sandbox(tmp_path) -> None:
    api = _Api(tmp_path)
    session = AssistantSession()
    session.history.append(ChatMessage(role="user", content="hello"))
    path = api._session_path("sess-a")
    api._save_session(path, session, "name")
    sandbox = tmp_path / "sess-a.sandbox"
    sandbox.mkdir()
    (sandbox / "notes.txt").write_text("x", encoding="utf-8")
    (sandbox / ".venv").mkdir()
    runs = tmp_path / "sess-a.runs"
    runs.mkdir()
    (runs / "run-1.json").write_text("{}", encoding="utf-8")

    result = api.delete_assistant_session("sess-a")
    assert result["ok"] is True
    assert result["removed"]["json"] is True
    assert result["removed"]["sandbox"] is True
    assert result["removed"]["runs"] is True
    assert not path.exists()
    assert not sandbox.exists()
    assert not runs.exists()


def test_clear_all_assistant_sessions_removes_everything(tmp_path) -> None:
    api = _Api(tmp_path)
    for sid in ("sess-a", "sess-b"):
        session = AssistantSession()
        session.history.append(ChatMessage(role="user", content=f"hello {sid}"))
        api._save_session(api._session_path(sid), session, sid)
        root = tmp_path / f"{sid}.sandbox"
        root.mkdir()
        (root / "script.py").write_text("print(1)", encoding="utf-8")

    orphan = tmp_path / "orphan.sandbox"
    orphan.mkdir()
    (orphan / "leftover.txt").write_text("x", encoding="utf-8")

    result = api.clear_all_assistant_sessions()
    assert result["ok"] is True
    assert result["deleted_count"] == 3
    assert not list(tmp_path.glob("*.json"))
    assert not list(tmp_path.glob("*.sandbox"))
