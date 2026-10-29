"""Sub-agent depth/readonly guards (Loom22 §22)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from argusai.core.ai import AIEndpoint
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.subagent import (
    _create_isolated_ai_provider,
    _readonly_tool_names,
    run_subagent,
)
from argusai.core.assistant.tools import ToolRegistry


def test_readonly_tool_names_exclude_mutating() -> None:
    reg = ToolRegistry()
    reg.register("skill_read", "read", {}, lambda: None, kind="idempotent")
    reg.register("http_probe", "confirmed read", {}, lambda: None, kind="idempotent")
    reg.register("http_request", "write", {}, lambda: None, kind="mutating")
    reg.register("run_subagent", "delegate", {}, lambda: None, kind="control")
    names = _readonly_tool_names(reg)
    assert "skill_read" in names
    assert "http_probe" not in names
    assert "http_request" not in names
    assert "run_subagent" not in names


def test_subagent_nesting_denied() -> None:
    session = AssistantSession(registry=ToolRegistry(), ai_provider=None)
    session._subagent_depth = 1

    async def _run():
        return await run_subagent(session, role="analyst", objective="x")

    out = asyncio.run(_run())
    assert out["ok"] is False
    assert out["stopped_reason"] == "denied"
    assert "nesting" in out["error"]


def test_subagent_requires_objective() -> None:
    session = AssistantSession(registry=ToolRegistry(), ai_provider=None)

    async def _run():
        return await run_subagent(session, role="analyst", objective="")

    out = asyncio.run(_run())
    assert out["ok"] is False


def test_async_subagent_enforces_per_session_concurrency_limit() -> None:
    class _FullStore:
        @staticmethod
        def running_count(*, name: str = "") -> int:
            assert name == "run_subagent"
            return 12

    session = AssistantSession(registry=ToolRegistry(), ai_provider=None)
    session._background_jobs = _FullStore()

    out = asyncio.run(run_subagent(
        session,
        role="reviewer",
        objective="inspect one component",
        mode="async",
    ))

    assert out["ok"] is False
    assert out["error_code"] == "subagent_concurrency_limit"
    assert out["max_concurrent"] == 12


def test_async_subagent_provider_is_created_from_current_settings(monkeypatch) -> None:
    isolated = object()
    settings = SimpleNamespace(ai_enabled=True, ai_endpoints=[{"model": "test"}])
    api = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))
    registry = ToolRegistry(api=api)
    session = AssistantSession(registry=registry, ai_provider=object())

    monkeypatch.setattr(
        "argusai.core.ai.create_provider",
        lambda *, endpoints: isolated if endpoints == settings.ai_endpoints else None,
    )

    assert _create_isolated_ai_provider(session) is isolated


def test_subagent_can_select_exact_model_within_endpoint(monkeypatch) -> None:
    isolated = object()
    endpoint = AIEndpoint(
        enabled=True,
        name="DeepSeek",
        model="deepseek-flash",
        models=["deepseek-flash", "deepseek-v4-pro"],
    )
    settings = SimpleNamespace(ai_enabled=True, ai_endpoints=[endpoint])
    api = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))
    session = AssistantSession(registry=ToolRegistry(api=api), ai_provider=object())
    captured = {}

    def _create(*, endpoints):
        captured["endpoints"] = endpoints
        return isolated

    monkeypatch.setattr("argusai.core.ai.create_provider", _create)
    assert _create_isolated_ai_provider(session, "DeepSeek::deepseek-v4-pro") is isolated
    assert captured["endpoints"][0].name == "DeepSeek"
    assert captured["endpoints"][0].model == "deepseek-v4-pro"
    # The configured global endpoint remains untouched.
    assert endpoint.model == "deepseek-flash"


def test_subagent_can_use_explicit_isolated_provider(monkeypatch) -> None:
    parent = AssistantSession(registry=ToolRegistry(), ai_provider=object())
    parent.engagement_declaration = {"active": True, "scope": ["example.test"]}
    parent.current_case_id = "case-123"
    isolated = object()
    captured: dict[str, object] = {}

    async def _chat(self, *_args, **_kwargs):  # noqa: ANN001
        captured["provider"] = self.ai
        captured["round_limit"] = self._max_agent_rounds()
        captured["deadline_seconds"] = self._turn_deadline_seconds()
        captured["engagement"] = self.engagement_declaration
        captured["case_id"] = self.current_case_id
        return SimpleNamespace(
            text="independent result",
            tool_calls=[],
            tool_trace=[],
            status="done",
        )

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    out = asyncio.run(run_subagent(
        parent,
        role="reviewer",
        objective="inspect one component",
        _ai_provider=isolated,
    ))

    assert out["ok"] is True
    assert out["summary"] == "independent result"
    assert captured["provider"] is isolated
    assert captured["round_limit"] == 0
    assert captured["deadline_seconds"] == 1800
    assert captured["engagement"] == parent.engagement_declaration
    assert captured["engagement"] is not parent.engagement_declaration
    assert captured["case_id"] == "case-123"


def test_sync_subagent_strips_all_delegation_controls(monkeypatch) -> None:
    parent = AssistantSession(registry=ToolRegistry(), ai_provider=object())
    captured: dict[str, object] = {}

    async def _chat(self, *_args, **_kwargs):  # noqa: ANN001
        captured["tools"] = set(self.registry._tools)
        captured["depth"] = self._subagent_depth
        return SimpleNamespace(text="ok", tool_calls=[], tool_trace=[], status="done")

    monkeypatch.setattr(AssistantSession, "chat", _chat)
    out = asyncio.run(run_subagent(
        parent,
        role="reviewer",
        objective="inspect one component",
        _ai_provider=object(),
    ))

    assert out["ok"] is True
    assert captured["depth"] == 1
    assert "run_subagent" not in captured["tools"]
    assert "spawn_agent" not in captured["tools"]
    assert "spawn_external_agent" not in captured["tools"]
