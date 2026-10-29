"""Regression tests for malformed model-authored tool arguments."""

from __future__ import annotations

import asyncio

from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.streaming import (
    TOOL_ARGUMENTS_ERROR_KEY,
    parse_tool_call_arguments,
)
from argusai.core.assistant.tools import ToolRegistry


def test_parser_repairs_windows_paths_and_raw_newlines() -> None:
    raw = (
        '{"script":"Get-ChildItem C:\\Users -Force\n'
        'Write-Output done","shell":"powershell"}'
    )

    args = parse_tool_call_arguments(raw)

    assert TOOL_ARGUMENTS_ERROR_KEY not in args
    assert args["script"] == "Get-ChildItem C:\\Users -Force\nWrite-Output done"
    assert args["shell"] == "powershell"


def test_parser_marks_truncated_json_instead_of_returning_empty_arguments() -> None:
    args = parse_tool_call_arguments('{"script":"Get-ChildItem C:')

    assert args[TOOL_ARGUMENTS_ERROR_KEY] == "arguments were not valid JSON"


def test_dispatch_rejects_unparseable_arguments_without_calling_tool() -> None:
    calls = 0

    def probe(**kwargs):
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry = ToolRegistry()
    registry.register("probe", "Probe.", {}, probe)
    session = AssistantSession(registry=registry)
    malformed = parse_tool_call_arguments('{"value":"unterminated')

    result = asyncio.run(session._dispatch_tool("probe", malformed))

    assert result["ok"] is False
    assert result["error_code"] == "invalid_arguments"
    assert result["retryable"] is True
    assert calls == 0
