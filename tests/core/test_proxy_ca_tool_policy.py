"""Proxy CA tools must stay confirmed and never auto-approved (Loom22 §21.7)."""

from __future__ import annotations

from argusai.core.assistant.streaming import (
    _CONFIRM_TOOLS,
    _NO_AUTO_APPROVE_TOOLS,
    _WRITE_TOOLS,
)
from argusai.core.assistant.tools import _tool_risk


def test_ca_tools_require_confirmation() -> None:
    for name in ("install_proxy_ca", "uninstall_proxy_ca"):
        assert name in _WRITE_TOOLS
        assert name in _CONFIRM_TOOLS
        assert name in _NO_AUTO_APPROVE_TOOLS
        assert _tool_risk(name) == "high"
