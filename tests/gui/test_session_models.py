"""Desktop model picker: listing and switching for ACP and native conversations."""

from __future__ import annotations

import json
import pytest

from argusai.core.agent_runtimes import normalize_session_runtime_binding
from argusai.core.ai import AIEndpoint
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
from argusai.core.settings import SettingsManager
from argusai.gui.agent_runtimes import AgentRuntimesMixin
from argusai.gui.channels import ChannelsMixin


def _options(current: str) -> list[dict]:
    return [{
        "id": "model",
        "name": "Model",
        "type": "select",
        "category": "model",
        "current_value": current,
        "options": [
            {"value": "m1", "name": "Model One"},
            {"value": "m2", "name": "Model Two"},
        ],
    }]


def _host(tmp_path, runtime: dict, endpoints: list[AIEndpoint] | None = None):
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.agent_runtimes = [{
        "id": "test-acp",
        "name": "Test ACP",
        "enabled": True,
        "command": "test-acp.cmd",
        "args": ["acp"],
    }]
    if endpoints is not None:
        settings.ai_endpoints = endpoints
    manager.save(settings)

    class Host(AgentRuntimesMixin, ChannelsMixin):
        settings_mgr = manager
        _assistant_sessions: dict = {}
        _sessions_dir = tmp_path / "sessions"

        def _validate_session_id(self, session_id):
            return str(session_id)

        def _session_path(self, session_id):
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            return self._sessions_dir / f"{session_id}.json"

        def _ensure_session(self, session_id):
            session = self._assistant_sessions.get(session_id)
            if session is None:
                path = self._session_path(session_id)
                if path.exists():
                    session = AssistantSession.from_dict(json.loads(path.read_text(encoding="utf-8")), ToolRegistry())
                else:
                    session = AssistantSession(registry=ToolRegistry())
                    session.runtime_binding = normalize_session_runtime_binding(runtime)
                self._assistant_sessions[session_id] = session
            return session

        def get_assistant_session(self, session_id):
            session = self._ensure_session(session_id)
            return {"ok": True, "runtime": session.runtime_binding.to_dict()}

        def _save_session(self, path, session, name):
            # Mirrors the real helper: an empty name means "keep the old one",
            # so a model switch cannot blank the conversation's title.
            old_name = ""
            if path.exists():
                old_name = str(
                    json.loads(path.read_text(encoding="utf-8")).get("name") or ""
                )
            data = session.to_dict()
            data["name"] = str(name or "") or old_name
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    return Host()


def test_desktop_lists_models_from_the_stored_binding(tmp_path) -> None:
    host = _host(tmp_path, {
        "runtime_id": "test-acp",
        "workspace_mode": "sandbox",
        "acp_config_options": _options("m1"),
    })

    result = host.list_session_models("session-1")

    assert result["ok"] is True
    assert result["kind"] == "acp_model"
    assert [row["value"] for row in result["models"]] == ["m1", "m2"]
    assert result["active"]["value"] == "m1"


def test_desktop_switches_to_the_first_row_in_the_list(tmp_path) -> None:
    # Row 0 is the regression that matters: a falsy index used to fall through
    # to the "nothing selected" branch and report the model as missing.
    host = _host(tmp_path, {
        "runtime_id": "test-acp",
        "workspace_mode": "sandbox",
        "acp_config_options": _options("m2"),
    })

    result = host.switch_session_model(
        "session-1",
        index=0,
        value="m1",
        name="Model One",
    )

    assert result["ok"] is True, result
    assert result["model"]["value"] == "m1"
    stored = json.loads(host._session_path("session-1").read_text(encoding="utf-8"))
    assert stored["runtime"]["acp_config"] == {"model": "m1"}
    assert host.list_session_models("session-1")["active"]["value"] == "m1"


def test_desktop_switch_keeps_the_conversation_name(tmp_path) -> None:
    """A model switch writes runtime state only; the title is not its to set."""
    host = _host(tmp_path, {
        "runtime_id": "test-acp",
        "workspace_mode": "sandbox",
        "acp_config_options": _options("m2"),
    })
    path = host._session_path("session-1")
    host._save_session(path, host._ensure_session("session-1"), "扫 example.com")

    assert host.switch_session_model("session-1", index=0, value="m1", name="Model One")["ok"]

    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "扫 example.com"


def test_desktop_switch_rejects_a_stale_row(tmp_path) -> None:
    host = _host(tmp_path, {
        "runtime_id": "test-acp",
        "workspace_mode": "sandbox",
        "acp_config_options": _options("m1"),
    })

    result = host.switch_session_model(
        "session-1",
        index=1,
        value="gone",
        name="Model Two",
    )

    assert result == {"ok": False, "kind": "acp_model", "code": "acp_model_changed"}


def test_desktop_model_picker_accepts_a_runtime_that_is_not_bound_yet(tmp_path) -> None:
    # The conversation exists only in the UI until its first turn, so the
    # stored binding is still the native default.
    host = _host(tmp_path, {"runtime_id": "argusai-native", "workspace_mode": "sandbox"})
    probes: list[str] = []

    def fake_probe(runtime, *, cwd, preferred=None, timeout_seconds=45.0):
        probes.append(runtime.id)

        async def _probe():
            return {"ok": True, "config_options": _options("m1")}

        return _probe()

    import argusai.core.agent_runtimes as agent_runtimes

    original = agent_runtimes.fetch_acp_config_options
    agent_runtimes.fetch_acp_config_options = fake_probe
    try:
        result = host.list_session_models("session-1", runtime_id="test-acp")
    finally:
        agent_runtimes.fetch_acp_config_options = original

    assert result["ok"] is True, result
    assert probes == ["test-acp"]
    assert [row["value"] for row in result["models"]] == ["m1", "m2"]


def test_unbound_project_runtime_uses_frontend_workspace_snapshot(tmp_path) -> None:
    host = _host(
        tmp_path,
        {"runtime_id": "argusai-native", "workspace_mode": "sandbox"},
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    probes: list[tuple[str, dict]] = []

    def fake_probe(runtime, *, cwd, preferred=None, timeout_seconds=45.0):
        probes.append((cwd, dict(preferred or {})))

        async def _probe():
            return {"ok": True, "config_options": _options("m2")}

        return _probe()

    import argusai.core.agent_runtimes as agent_runtimes

    original = agent_runtimes.fetch_acp_config_options
    agent_runtimes.fetch_acp_config_options = fake_probe
    try:
        result = host.list_session_models(
            "session-project",
            runtime_id="test-acp",
            runtime={
                "runtime_id": "test-acp",
                "workspace_mode": "project",
                "project_dir": str(project_dir),
                "acp_config": {"model": "m2"},
            },
        )
    finally:
        agent_runtimes.fetch_acp_config_options = original

    assert result["ok"] is True, result
    assert probes == [(str(project_dir), {"model": "m2"})]
    assert result["runtime"]["workspace_mode"] == "project"
    assert result["runtime"]["project_dir"] == str(project_dir)


def test_new_session_falls_back_to_same_runtime_model_cache(tmp_path) -> None:
    host = _host(
        tmp_path,
        {"runtime_id": "argusai-native", "workspace_mode": "sandbox"},
    )
    cached = AssistantSession(registry=ToolRegistry())
    cached.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "test-acp",
        "workspace_mode": "sandbox",
        "acp_config": {"model": "m2"},
        "acp_config_options": _options("m2"),
        "acp_prompt_capabilities": {"image": True},
    })
    host._save_session(host._session_path("session-cached"), cached, "cached")

    def failed_probe(runtime, *, cwd, preferred=None, timeout_seconds=45.0):
        async def _probe():
            return {
                "ok": False,
                "error_code": "acp_config_probe_failed",
                "error": "RequestError: Internal error",
            }

        return _probe()

    import argusai.core.agent_runtimes as agent_runtimes

    original = agent_runtimes.fetch_acp_config_options
    agent_runtimes.fetch_acp_config_options = failed_probe
    try:
        result = host.list_session_models(
            "session-new",
            refresh=True,
            runtime_id="test-acp",
            runtime={
                "runtime_id": "test-acp",
                "workspace_mode": "sandbox",
            },
        )
    finally:
        agent_runtimes.fetch_acp_config_options = original

    assert result["ok"] is True, result
    assert result["stale"] is True
    assert result["model_source"] == "runtime_cache"
    assert result["active"]["value"] == "m2"
    # Model inventory may be reused, but multimodal capability is negotiated
    # afresh for this session and must never be inferred from an older one.
    assert "acp_prompt_capabilities" not in result["runtime"]
    assert "Internal error" in result["warning"]


def _native_endpoints() -> list[AIEndpoint]:
    return [
        AIEndpoint(
            name="Ollama",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3:8b",
            models=["qwen3:8b", "llama3.1:8b"],
            priority=10,
        ),
        AIEndpoint(
            name="OpenAI",
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            models=["gpt-4o", "gpt-4o-mini"],
            priority=5,
        ),
        AIEndpoint(name="Disabled", model="unused", enabled=False, priority=99),
    ]


def test_native_picker_lists_every_model_of_every_enabled_endpoint(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())

    result = host.list_session_models("")

    assert result["ok"] is True
    assert result["kind"] == "endpoint"
    assert [(row["name"], row["model"]) for row in result["models"]] == [
        ("Ollama", "qwen3:8b"),
        ("Ollama", "llama3.1:8b"),
        ("OpenAI", "gpt-4o"),
        ("OpenAI", "gpt-4o-mini"),
    ]
    # Only the top-priority endpoint running its selected model is live.
    assert result["active"]["model"] == "qwen3:8b"


def test_native_switch_promotes_the_endpoint_and_selects_the_model(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())

    result = host.switch_session_model("", index=1, value="gpt-4o-mini", name="OpenAI")

    assert result["ok"] is True, result
    endpoints = host.settings_mgr.get().ai_endpoints
    assert endpoints[1].model == "gpt-4o-mini"
    assert endpoints[1].priority > endpoints[0].priority
    # The catalog is untouched, so the user can switch straight back.
    assert endpoints[1].models == ["gpt-4o", "gpt-4o-mini"]
    assert host.list_session_models("")["active"]["model"] == "gpt-4o-mini"


def test_native_switch_within_one_endpoint_keeps_it_on_top(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())

    result = host.switch_session_model("", index=0, value="llama3.1:8b", name="Ollama")

    assert result["ok"] is True, result
    endpoints = host.settings_mgr.get().ai_endpoints
    assert endpoints[0].model == "llama3.1:8b"
    assert host.list_session_models("")["active"]["model"] == "llama3.1:8b"


def test_native_switch_rejects_a_model_the_endpoint_no_longer_serves(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())

    result = host.switch_session_model("", index=0, value="removed-model", name="Ollama")

    assert result == {"ok": False, "kind": "endpoint", "code": "endpoint_not_found"}


def test_legacy_endpoint_without_a_catalog_is_still_switchable(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, [
        AIEndpoint(name="Legacy", model="gpt-4o", priority=1),
        AIEndpoint(name="Other", model="claude-3-5", priority=0),
    ])

    result = host.list_session_models("")

    assert [row["model"] for row in result["models"]] == ["gpt-4o", "claude-3-5"]
    assert host.switch_session_model("", index=1, value="claude-3-5", name="Other")["ok"]
    assert host.settings_mgr.get().ai_endpoints[1].model == "claude-3-5"


def test_native_selection_is_scoped_to_one_conversation(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())
    host.list_session_models("other")
    before = [ep.to_dict() for ep in host.settings_mgr.get().ai_endpoints]
    result = host.switch_session_model("chosen", index=1, value="gpt-4o-mini", name="OpenAI")
    assert result["ok"], result
    assert host.list_session_models("chosen")["active"]["model"] == "gpt-4o-mini"
    assert host.list_session_models("other")["active"]["model"] == "qwen3:8b"
    assert host.list_session_models("brand-new")["active"]["model"] == "qwen3:8b"
    assert [ep.to_dict() for ep in host.settings_mgr.get().ai_endpoints] == before


def test_native_selection_survives_reload_and_uses_private_provider_chain(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())
    assert host.switch_session_model("chosen", index=1, value="gpt-4o-mini", name="OpenAI")["ok"]
    del host._assistant_sessions["chosen"]
    assert host.list_session_models("chosen")["active"]["model"] == "gpt-4o-mini"
    settings = host.settings_mgr.get()
    session = host._ensure_session("chosen")
    chain = session.native_model_endpoints(settings.ai_endpoints)
    assert chain[0].name == "OpenAI" and chain[0].model == "gpt-4o-mini"
    assert chain[0] is not settings.ai_endpoints[1]
    assert settings.ai_endpoints[1].model == "gpt-4o"
    serialized = session.to_dict()["native_model_selection"]
    assert set(serialized) == {"endpoint_key", "endpoint_name", "model"}
    assert "api_key" not in serialized and "base_url" not in serialized


def test_existing_conversation_keeps_its_initial_model_when_defaults_change(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())
    assert host.list_session_models("existing")["active"]["model"] == "qwen3:8b"
    settings = host.settings_mgr.get()
    settings.ai_endpoints[1].priority = 100
    settings.ai_endpoints[1].model = "gpt-4o-mini"
    host.settings_mgr.save(settings)
    assert host.list_session_models("existing")["active"]["model"] == "qwen3:8b"
    assert host.list_session_models("new")["active"]["model"] == "gpt-4o-mini"


def test_removed_selected_route_requires_explicit_reselection(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "argusai-native"}, _native_endpoints())
    assert host.switch_session_model("chosen", index=1, value="gpt-4o-mini", name="OpenAI")["ok"]
    settings = host.settings_mgr.get()
    settings.ai_endpoints[1].enabled = False
    host.settings_mgr.save(settings)
    assert host.list_session_models("chosen")["active"] == {}
    with pytest.raises(ValueError, match="unavailable"):
        host._ensure_session("chosen").native_model_endpoints(settings.ai_endpoints)
    assert host.switch_session_model("chosen", index=0, value="llama3.1:8b", name="Ollama")["ok"]


def test_acp_selection_does_not_change_another_conversation(tmp_path) -> None:
    host = _host(tmp_path, {"runtime_id": "test-acp", "acp_config_options": _options("m2")})
    host.list_session_models("other")
    assert host.switch_session_model("chosen", index=0, value="m1", name="Model One")["ok"]
    assert host.list_session_models("other")["active"]["value"] == "m2"
