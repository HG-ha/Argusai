"""ACP turn routing for negotiated append-only system prompts, without an Agent.

The fake process exposes the same session wrappers as ACPAgentProcess and also
records calls made directly to its connection, so routing regressions are visible.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import argusai.core.agent_runtimes.acp_turn as turns
from argusai.core.agent_runtimes.config import normalize_session_runtime_binding
from argusai.core.agent_runtimes.models import AgentRuntimeConfig, SessionRuntimeBinding

MISSING = object()
HOST = "[ArgusAI host context]\nUse only this workbench's scoped tools."
HABITS = "Use concise Chinese and cite saved evidence."
MESSAGE = "  Inspect the authorized fixture; keep this message unchanged.\n"
CWD = str(Path(__file__).resolve().parents[2] / "build/acp-system-prompt-fixture")
MCP_SERVERS = [{"name": "fixture-tools", "command": "fixture-stdio", "args": [], "env": []}]


def test_acp_reuses_native_workbench_policy():
    from argusai.core.assistant.skills import _build_system_prompt

    native = _build_system_prompt("", "")
    external = turns.acp_host_instruction()
    for shared in (
        "proactive-defense", "assistant-cron", "multi-agent-orchestration",
        "channel_delivery_status", "search_all_memory", "read_assistant_session",
        "artifact_read", "wait_for_duration", "wait_for_agents", "background_job_status",
        "## Agent Loop", "## Context and Files", "## Knowledge and Profile",
        "Difficulty, workload, unfamiliar code", "Reply language (always)",
    ):
        assert shared in native and shared in external
    assert "invoke_catalogue_tool" in native
    assert "invoke_catalogue_tool" not in external
    assert "argusai_invoke_tool" in external
    assert "Keep your actual Agent identity" in external
    assert "You are the ArgusAI AI assistant" not in external
    assert "runtime ends the turn without another model pass" not in external
    assert "always loaded" not in external
    assert "AUTO-APPROVE is on" not in external
    assert "permission requests remain separate" in external


def test_native_prompt_default_is_unchanged_by_external_projection():
    from argusai.core.assistant.skills import _build_system_prompt

    before = _build_system_prompt("fixture schema", "", "fixture preferences")
    turns.acp_host_instruction()
    assert before == _build_system_prompt("fixture schema", "", "fixture preferences", external_agent=False)
    assert before.endswith("fixture preferences")


@pytest.mark.parametrize("supported", [True, False])
def test_greenlight_requires_fresh_negotiation_and_never_falls_back(monkeypatch, supported):
    harness = ACPHarness(monkeypatch, {} if supported else MISSING)
    host = turns.acp_host_instruction(greenlight_active=True)
    result = harness.run(host=host, greenlight_active=True)
    if supported:
        assert result.ok
        assert "GREENLIGHT MODE is active" in _system_text(harness.calls[0])
        assert "runtime's own policies" in _system_text(harness.calls[0])
        assert "GREENLIGHT" not in _prompt_text(harness.prompts[0])
        assert result.prompt_capabilities["system_prompt"] is True
    else:
        assert not result.ok
        assert result.error_code == "acp_system_prompt_required"
        assert not harness.calls and not harness.prompts
    assert harness.processes[0].closed


class ACPHarness:
    def __init__(self, monkeypatch, system_capability=MISSING):
        self.system_capability = system_capability
        self.session_capabilities = MISSING
        self.supports_load = True
        self.capabilities_by_runtime = {}
        self.fail_system_method = ""
        self.calls = []
        self.prompts = []
        self.processes = []
        owner = self

        class Connection:
            def __init__(self, runtime_id):
                self.runtime_id = runtime_id

            async def new_session(self, cwd, *, mcp_servers=None, **kwargs):
                return await self._session("session/new", cwd, "", mcp_servers, kwargs)

            async def load_session(self, cwd, session_id, *, mcp_servers=None, **kwargs):
                return await self._session("session/load", cwd, session_id, mcp_servers, kwargs)

            async def _session(self, method, cwd, session_id, mcp_servers, kwargs):
                payload = {"cwd": cwd, "mcpServers": deepcopy(mcp_servers or [])}
                if session_id:
                    payload["sessionId"] = session_id
                # The real process wrappers serialize system_prompt at the RPC
                # top level rather than hiding it inside the SDK's _meta kwargs.
                system_prompt = kwargs.pop("system_prompt", kwargs.pop("systemPrompt", MISSING))
                if system_prompt is not MISSING and system_prompt is not None:
                    payload["systemPrompt"] = deepcopy(system_prompt)
                payload.update(deepcopy(kwargs))
                owner.calls.append({"method": method, "runtime_id": self.runtime_id, "payload": payload})
                if owner.fail_system_method == method and "systemPrompt" in payload:
                    raise RuntimeError("fixture adapter rejected the advertised systemPrompt")
                return {"sessionId": session_id or f"remote-{self.runtime_id}", "configOptions": []}

            async def prompt(self, session_id, blocks):
                owner.prompts.append({
                    "runtime_id": self.runtime_id,
                    "session_id": session_id,
                    "blocks": [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in blocks],
                })
                if hasattr(owner, "on_prompt"):
                    owner.on_prompt()
                return {"stopReason": "end_turn"}

        class Process:
            def __init__(self, runtime, *, cwd="", **kwargs):
                self.runtime = runtime
                self.cwd = cwd
                self.connection = Connection(runtime.id)
                self.closed = False
                owner.processes.append(self)

            async def initialize(self, **kwargs):
                if self.runtime.id in owner.capabilities_by_runtime:
                    capabilities = deepcopy(owner.capabilities_by_runtime[self.runtime.id])
                else:
                    capabilities = {"loadSession": owner.supports_load, "promptCapabilities": {"image": True}}
                    if owner.session_capabilities is not MISSING:
                        capabilities["sessionCapabilities"] = deepcopy(owner.session_capabilities)
                    elif owner.system_capability is not MISSING:
                        capabilities["sessionCapabilities"] = {"systemPrompt": deepcopy(owner.system_capability)}
                return {"agentCapabilities": capabilities}

            async def new_session(self, cwd, *, mcp_servers=None, system_prompt=None, **kwargs):
                return await self.connection.new_session(
                    cwd, mcp_servers=mcp_servers, system_prompt=system_prompt, **kwargs,
                )

            async def load_session(self, cwd, session_id, *, mcp_servers=None, system_prompt=None, **kwargs):
                return await self.connection.load_session(
                    cwd, session_id, mcp_servers=mcp_servers, system_prompt=system_prompt, **kwargs,
                )

            async def close(self):
                self.closed = True

        monkeypatch.setattr(turns, "ACPAgentProcess", Process)

    def run(self, binding=None, *, habits=HABITS, host=HOST, message=MESSAGE, runtime_id="fixture-acp", **kwargs):
        runtime = AgentRuntimeConfig(id=runtime_id, name=runtime_id, backend="acp", command="fixture-agent")
        binding = binding or SessionRuntimeBinding(runtime_id=runtime_id)
        return asyncio.run(turns.run_acp_turn(
            runtime, binding, message=message, cwd=CWD, host_instruction=host,
            user_habits=habits, mcp_servers=deepcopy(MCP_SERVERS), **kwargs,
        ))


def _system_text(call):
    payload = call["payload"]
    assert "_meta" not in payload
    system = payload["systemPrompt"]
    assert set(system) == {"mode", "content"}
    assert system["mode"] == "append"
    assert system["content"]
    assert all(set(block) == {"type", "text"} and block["type"] == "text"
               and isinstance(block["text"], str) for block in system["content"])
    return "\n".join(block["text"] for block in system["content"])


def _prompt_text(prompt):
    return "\n".join(block.get("text", "") for block in prompt["blocks"] if block.get("type") == "text")


@pytest.mark.parametrize("capability", [{}, {"append": True}, {"mode": {"override": {}}}])
def test_native_append_on_session_new_keeps_host_out_of_user_message(monkeypatch, capability):
    harness = ACPHarness(monkeypatch, capability)
    result = harness.run()
    assert result.ok, result.to_dict()
    assert [call["method"] for call in harness.calls] == ["session/new"]
    text = _system_text(harness.calls[0])
    assert HOST in text and HABITS in text
    assert harness.calls[0]["payload"]["systemPrompt"]["content"] == [
        {"type": "text", "text": HOST},
        {"type": "text", "text": "[ArgusAI user preferences]\n" + HABITS},
    ]
    assert result.host_context["delivery"] == "system_prompt"
    assert harness.prompts[0]["blocks"] == [{"type": "text", "text": MESSAGE}]
    assert harness.processes[0].closed


def test_loaded_session_always_receives_same_native_append_for_compaction_recovery(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    first = harness.run()
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id=first.acp_session_id,
        locked=True, acp_host_context=dict(first.host_context),
    )
    second = harness.run(binding)
    third = harness.run(replace(binding, acp_host_context=dict(second.host_context)))
    assert first.ok and second.ok and third.ok
    assert [call["method"] for call in harness.calls] == ["session/new", "session/load", "session/load"]
    assert len({_system_text(call) for call in harness.calls}) == 1
    assert all(_prompt_text(prompt) == MESSAGE for prompt in harness.prompts)
    assert harness.calls[1]["payload"]["sessionId"] == first.acp_session_id
    assert all(process.closed for process in harness.processes)


def test_native_append_reflects_changed_and_cleared_preferences(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    first = harness.run()
    binding = SessionRuntimeBinding(runtime_id="fixture-acp", acp_session_id=first.acp_session_id, locked=True)
    updated = harness.run(binding, habits="Explain each result in detail.")
    cleared = harness.run(binding, habits="")
    assert first.ok and updated.ok and cleared.ok
    original_text, updated_text, cleared_text = map(_system_text, harness.calls)
    assert HABITS in original_text
    assert "Explain each result in detail." in updated_text and HABITS not in updated_text
    assert HOST in cleared_text
    assert HABITS not in cleared_text and "Explain each result in detail." not in cleared_text
    assert cleared_text != updated_text
    assert harness.calls[-1]["payload"]["systemPrompt"]["content"] == [{"type": "text", "text": HOST}]
    assert all(_prompt_text(prompt) == MESSAGE for prompt in harness.prompts)


@pytest.mark.parametrize("capability", [MISSING, None, False, True, "append", [], 0],
                         ids=["missing", "null", "false", "true", "string", "list", "number"])
def test_missing_null_or_non_object_capability_uses_deduplicated_fallback(monkeypatch, capability):
    harness = ACPHarness(monkeypatch, capability)
    first = harness.run()
    assert first.ok, first.to_dict()
    assert first.host_context["delivery"] == "prompt"
    assert "systemPrompt" not in harness.calls[0]["payload"]
    assert HOST in _prompt_text(harness.prompts[0]) and HABITS in _prompt_text(harness.prompts[0])
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id=first.acp_session_id,
        locked=True, acp_host_context=dict(first.host_context),
    )
    second = harness.run(binding)
    assert second.ok, second.to_dict()
    assert "systemPrompt" not in harness.calls[1]["payload"]
    assert _prompt_text(harness.prompts[1]) == MESSAGE


@pytest.mark.parametrize("session_capabilities", [None, [], "not-an-object"])
def test_invalid_session_capabilities_do_not_enable_native_system_prompt(monkeypatch, session_capabilities):
    harness = ACPHarness(monkeypatch)
    harness.session_capabilities = session_capabilities
    result = harness.run()
    assert result.ok, result.to_dict()
    assert "systemPrompt" not in harness.calls[0]["payload"]
    assert HOST in _prompt_text(harness.prompts[0])


@pytest.mark.parametrize("method", ["session/new", "session/load"])
def test_advertised_system_prompt_rejection_does_not_downgrade_or_recreate(monkeypatch, method):
    harness = ACPHarness(monkeypatch, {})
    harness.fail_system_method = method
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id="existing-remote" if method.endswith("load") else "",
        locked=True,
    )
    result = harness.run(binding)
    assert not result.ok
    assert "systemPrompt" in result.error
    assert [call["method"] for call in harness.calls] == [method]
    assert _system_text(harness.calls[0])
    assert harness.prompts == []
    assert harness.processes[0].closed


def test_native_append_preserves_attachment_message_mcp_and_working_directory(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    attachments = [{"type": "image", "mime": "image/png", "data_base64": "aW1hZ2U="}]
    before = deepcopy(attachments)
    result = harness.run(attachments=attachments)
    assert result.ok, result.to_dict()
    assert attachments == before
    assert harness.calls[0]["payload"]["cwd"] == CWD
    assert harness.calls[0]["payload"]["mcpServers"] == MCP_SERVERS
    assert harness.processes[0].cwd == CWD
    assert harness.prompts[0]["blocks"] == [
        {"type": "text", "text": MESSAGE},
        {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"},
    ]
    assert HOST not in _prompt_text(harness.prompts[0])


def test_each_runtime_negotiates_its_own_delivery_mode_and_context(monkeypatch):
    harness = ACPHarness(monkeypatch)
    harness.capabilities_by_runtime = {
        "parent-acp": {"loadSession": True, "sessionCapabilities": {"systemPrompt": {}}},
        "child-acp": {"loadSession": True},
    }
    parent = harness.run(runtime_id="parent-acp", host="Parent workbench context", habits="Parent preference")
    child = harness.run(runtime_id="child-acp", host="Child workbench context", habits="Child preference")
    assert parent.ok and child.ok
    assert harness.calls[0]["runtime_id"] == "parent-acp"
    assert "Parent workbench context" in _system_text(harness.calls[0])
    assert "systemPrompt" not in harness.calls[1]["payload"]
    assert "Child workbench context" in _prompt_text(harness.prompts[1])
    assert "Parent workbench context" not in _prompt_text(harness.prompts[1])
    assert "Parent preference" not in _prompt_text(harness.prompts[1])
    assert _prompt_text(harness.prompts[0]) == MESSAGE


def test_unsupported_fallback_still_updates_and_clears_preferences(monkeypatch):
    harness = ACPHarness(monkeypatch)
    first = harness.run()
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id=first.acp_session_id,
        acp_host_context=dict(first.host_context),
    )
    updated = harness.run(binding, habits="New fallback preference")
    cleared = harness.run(replace(binding, acp_host_context=dict(updated.host_context)), habits="")
    assert first.ok and updated.ok and cleared.ok
    assert "New fallback preference" in _prompt_text(harness.prompts[1])
    assert HOST not in _prompt_text(harness.prompts[1])
    assert "preferences have been cleared" in _prompt_text(harness.prompts[2])
    assert all("systemPrompt" not in call["payload"] for call in harness.calls)


@pytest.mark.parametrize("start_native", [True, False], ids=["native-to-fallback", "fallback-to-native"])
def test_delivery_change_with_same_remote_and_hashes_reapplies_correct_context(monkeypatch, start_native):
    harness = ACPHarness(monkeypatch, {} if start_native else MISSING)
    first = harness.run()
    harness.system_capability = MISSING if start_native else {}
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id=first.acp_session_id,
        locked=True, acp_host_context=dict(first.host_context),
    )
    second = harness.run(binding)
    harness.system_capability = {} if start_native else MISSING
    third = harness.run(replace(binding, acp_host_context=dict(second.host_context)))
    assert first.ok and second.ok and third.ok
    assert [call["method"] for call in harness.calls] == ["session/new", "session/load", "session/load"]
    assert len({result.acp_session_id for result in (first, second, third)}) == 1
    for field in ("session_id", "instruction_hash", "profile_hash"):
        assert len({result.host_context[field] for result in (first, second, third)}) == 1
    for index, native in enumerate((start_native, not start_native, start_native)):
        if native:
            assert HOST in _system_text(harness.calls[index])
            assert HABITS in _system_text(harness.calls[index])
            assert _prompt_text(harness.prompts[index]) == MESSAGE
        else:
            assert "systemPrompt" not in harness.calls[index]["payload"]
            assert HOST in _prompt_text(harness.prompts[index])
            assert HABITS in _prompt_text(harness.prompts[index])
    assert [result.host_context["delivery"] for result in (first, second, third)] == [
        "system_prompt" if native else "prompt" for native in (start_native, not start_native, start_native)
    ]


@pytest.mark.parametrize("native", [True, False], ids=["system-prompt", "fallback-prompt"])
def test_delivery_marker_survives_binding_serialization_and_controls_next_turn(monkeypatch, native):
    harness = ACPHarness(monkeypatch, {} if native else MISSING)
    first = harness.run()
    binding = SessionRuntimeBinding(
        runtime_id="fixture-acp", acp_session_id=first.acp_session_id,
        locked=True, acp_host_context=dict(first.host_context),
    )
    restored = normalize_session_runtime_binding(binding.to_dict())
    assert restored.acp_host_context == first.host_context
    second = harness.run(restored)
    assert first.ok and second.ok
    assert _prompt_text(harness.prompts[1]) == MESSAGE
    assert second.host_context == first.host_context
    if native:
        assert harness.calls[0]["payload"]["systemPrompt"] == harness.calls[1]["payload"]["systemPrompt"]
    else:
        assert "systemPrompt" not in harness.calls[1]["payload"]


@pytest.mark.parametrize("delivery", [MISSING, None, "", "override", "SYSTEM_PROMPT", "system_prompt ",
                                     True, [], {"mode": "append"}],
                         ids=["legacy-missing", "null", "empty", "override", "uppercase", "whitespace",
                              "bool", "list", "object"])
def test_invalid_or_legacy_delivery_normalizes_to_compatible_prompt_fallback(monkeypatch, delivery):
    harness = ACPHarness(monkeypatch)
    first = harness.run()
    marker = dict(first.host_context)
    if delivery is MISSING:
        marker.pop("delivery")
    else:
        marker["delivery"] = delivery
    raw = {"runtime_id": "fixture-acp", "acp_session_id": first.acp_session_id,
           "acp_host_context": marker}
    original = deepcopy(raw)
    restored = normalize_session_runtime_binding(raw)
    assert raw == original
    assert "delivery" not in restored.acp_host_context
    assert restored.acp_host_context == {key: value for key, value in first.host_context.items() if key != "delivery"}
    second = harness.run(restored)
    assert first.ok and second.ok
    assert "systemPrompt" not in harness.calls[1]["payload"]
    assert _prompt_text(harness.prompts[1]) == MESSAGE
    assert second.host_context["delivery"] == "prompt"


def test_empty_host_and_preferences_do_not_send_empty_system_prompt(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    first = harness.run(host="", habits="")
    binding = SessionRuntimeBinding(runtime_id="fixture-acp", acp_session_id=first.acp_session_id)
    second = harness.run(binding, host="", habits="")
    assert first.ok and second.ok
    assert [call["method"] for call in harness.calls] == ["session/new", "session/load"]
    assert all("systemPrompt" not in call["payload"] for call in harness.calls)
    assert all(_prompt_text(prompt) == MESSAGE for prompt in harness.prompts)
    assert first.host_context == second.host_context == {}


def test_native_preferences_can_be_sent_without_host_instruction(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    result = harness.run(host="")
    assert result.ok, result.to_dict()
    assert harness.calls[0]["payload"]["systemPrompt"]["content"] == [
        {"type": "text", "text": "[ArgusAI user preferences]\n" + HABITS},
    ]
    assert _prompt_text(harness.prompts[0]) == MESSAGE
    assert result.host_context["delivery"] == "system_prompt"


def test_unsupported_preferences_without_host_preserve_existing_no_injection_behavior(monkeypatch):
    harness = ACPHarness(monkeypatch)
    result = harness.run(host="")
    assert result.ok, result.to_dict()
    assert "systemPrompt" not in harness.calls[0]["payload"]
    assert _prompt_text(harness.prompts[0]) == MESSAGE
    assert result.host_context == {}


def test_native_append_does_not_rewrite_stateless_history_replay(monkeypatch):
    harness = ACPHarness(monkeypatch, {})
    harness.supports_load = False
    history = "Historical user: retain  spaces.\nHistorical agent: acknowledged.\nCurrent user: " + MESSAGE
    binding = SessionRuntimeBinding(runtime_id="fixture-acp", acp_session_id="previous-remote", locked=True)
    result = harness.run(binding, stateless_message=history)
    assert result.ok, result.to_dict()
    assert result.session_restarted
    assert [call["method"] for call in harness.calls] == ["session/new"]
    assert HOST in _system_text(harness.calls[0])
    assert harness.prompts[0]["blocks"] == [{"type": "text", "text": history}]


@pytest.mark.parametrize("stop", ["completed", "stalled", "decision", "cancelled", "terminal", "pending"])
def test_acp_native_todo_continuation_obeys_host_state(monkeypatch, stop):
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    session = AssistantSession(registry=ToolRegistry())
    session._user_auto_approve_mode = True
    harness = ACPHarness(monkeypatch, {})
    calls = []

    def respond():
        calls.append(1)
        if len(calls) == 1:
            session.set_todos([{"id": "work", "title": "fixture work"}])
            session._todos_plan_approved = True
            if stop == "decision":
                session._user_decision_requested = True
            elif stop == "cancelled":
                session._cancel_requested = True
            elif stop == "terminal":
                session._acp_terminal_reply = True
            elif stop == "pending":
                session._pending = {"name": "fixture"}
        elif stop == "completed":
            session.todos[0]["status"] = "completed"

    harness.on_prompt = respond
    result = harness.run(continuation_session=session)
    assert result.ok is (stop != "stalled")
    if stop == "stalled":
        assert result.error_code == "acp_progress_stalled"
    assert len(calls) == (2 if stop in {"completed", "stalled"} else 1)
    if len(calls) == 2:
        assert "invoke_catalogue_tool" not in _prompt_text(harness.prompts[1])
        assert len(harness.calls) == 1  # same ACP session, no history replay/restart
