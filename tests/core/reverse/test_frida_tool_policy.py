"""Frida dynamic tools must stay confirmed and never auto-approved."""

from __future__ import annotations

from argusai.core.assistant.streaming import (
    _CONFIRM_TOOLS,
    _NO_AUTO_APPROVE_TOOLS,
    _REVERSE_DYNAMIC_TOOLS,
    _WRITE_TOOLS,
)
from argusai.core.assistant.tools import _tool_risk


def test_frida_dynamic_tools_require_confirmation() -> None:
    for name in ("frida_attach", "frida_spawn", "frida_run_script", "frida_session_stop"):
        assert name in _WRITE_TOOLS
        assert name in _CONFIRM_TOOLS
        assert name in _NO_AUTO_APPROVE_TOOLS
        assert name in _REVERSE_DYNAMIC_TOOLS
        assert _tool_risk(name) == "high"


def test_frida_discovery_tools_are_low_risk() -> None:
    for name in ("frida_list_devices", "frida_list_processes", "frida_session_list"):
        assert name not in _NO_AUTO_APPROVE_TOOLS
        assert name not in _REVERSE_DYNAMIC_TOOLS
        assert _tool_risk(name) == "low"


def test_session_allow_all_can_include_reverse_dynamic_only() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    registry = ToolRegistry()

    async def _noop(**_kwargs):
        return {}

    for name in ("frida_attach", "run_python_snippet", "http_probe"):
        registry.register(name, "stub", {}, _noop)

    session = AssistantSession(registry=registry)
    allowed = session.set_auto_approve_all(True, include_reverse_dynamic=True)
    assert "frida_attach" in allowed
    assert "run_python_snippet" not in allowed
    assert "http_probe" in allowed
