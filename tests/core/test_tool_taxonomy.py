"""Tool taxonomy (Loom22 §11.2) — kinds must stay consistent with fragment sets.

An incomplete classification table silently disables loop guards. These tests
pin that every tool in each fragment set maps to the expected kind via
``tool_kind``, and that ``ToolRegistry.register`` seeds matching metadata.
"""

from __future__ import annotations

from types import SimpleNamespace

from argusai.core.assistant.streaming import (
    _CONTROL_TOOLS,
    _POLLING_TOOLS,
    _PRESENTATION_TOOLS,
    _WRITE_TOOLS,
    should_dedupe_tool_call,
    tool_kind,
)
from argusai.core.assistant.tools import ToolRegistry, _default_tool_metadata


def test_presentation_tools_are_presentation() -> None:
    for name in _PRESENTATION_TOOLS:
        assert tool_kind(name) == "presentation", name


def test_control_tools_are_control() -> None:
    for name in _CONTROL_TOOLS:
        assert tool_kind(name) == "control", name


def test_polling_tools_are_polling() -> None:
    for name in _POLLING_TOOLS:
        assert tool_kind(name) == "polling", name


def test_write_tools_without_higher_precedence_are_mutating() -> None:
    # Presentation / control / polling win when a name appears in multiple
    # fragments; everything else in _WRITE_TOOLS is mutating.
    higher = _PRESENTATION_TOOLS | _CONTROL_TOOLS | _POLLING_TOOLS
    for name in _WRITE_TOOLS:
        if name in higher:
            continue
        assert tool_kind(name) == "mutating", name


def test_unknown_tool_defaults_to_idempotent() -> None:
    assert tool_kind("totally_unknown_tool_xyz") == "idempotent"
    assert tool_kind("") == "idempotent"


def test_kind_precedence_presentation_over_write() -> None:
    # If a presentation tool were ever also listed as write, presentation wins.
    assert tool_kind("show_structured_card") == "presentation"


def test_kind_precedence_control_over_idempotent() -> None:
    assert tool_kind("set_todos") == "control"
    assert tool_kind("stop_for_user_decision") == "control"


def test_default_metadata_matches_kind_fragments() -> None:
    for name in _PRESENTATION_TOOLS:
        meta = _default_tool_metadata(name)
        assert meta["kind"] == "presentation"
        assert meta["dedupe"] is False
        assert meta["concurrency"] == "serial"
    for name in _POLLING_TOOLS:
        meta = _default_tool_metadata(name)
        assert meta["kind"] == "polling"
        assert meta["dedupe"] is False
        assert meta["concurrency"] == "read_parallel"
    for name in ("skill_read", "tool_search"):
        meta = _default_tool_metadata(name)
        assert meta["kind"] == "idempotent"
        assert meta["dedupe"] is True
        assert meta["concurrency"] == "read_parallel"
    # Loom22 §11: mutating tools must not exact-dedupe.
    for name in ("sandbox_write_file", "run_shell_snippet", "shell_session_exec"):
        meta = _default_tool_metadata(name)
        assert meta["kind"] == "mutating", name
        assert meta["dedupe"] is False, name


def test_should_dedupe_tool_call_matches_loom22() -> None:
    idem = SimpleNamespace(name="skill_read", kind="idempotent", dedupe=True)
    assert should_dedupe_tool_call(idem, {"q": "x"}) is True
    assert should_dedupe_tool_call(idem, {"q": "x", "force": True}) is False
    assert should_dedupe_tool_call(idem, {"q": "x", "fresh_context": 1}) is False

    mut = SimpleNamespace(name="sandbox_write_file", kind="mutating", dedupe=True)
    assert should_dedupe_tool_call(mut, {"path": "a"}) is False

    poll = SimpleNamespace(name="shell_session_read", kind="polling", dedupe=False)
    assert should_dedupe_tool_call(poll, {"session_id": "s"}) is False

    assert should_dedupe_tool_call(None, {"q": "x"}, name="skill_read") is False


def test_registry_register_seeds_metadata() -> None:
    reg = ToolRegistry()
    reg.register("skill_read", "Read a skill.", {}, lambda: None)
    reg.register("oob_check_events", "Poll OOB.", {}, lambda: None)
    reg.register("sandbox_write_file", "Write file.", {}, lambda: None, risk="medium")
    reg.register("show_data_table", "Show table.", {}, lambda: None)

    assert reg.get("skill_read").kind == "idempotent"
    assert reg.get("skill_read").concurrency == "read_parallel"
    assert reg.get("skill_read").dedupe is True

    assert reg.get("oob_check_events").kind == "polling"
    assert reg.get("oob_check_events").dedupe is False

    assert reg.get("sandbox_write_file").kind == "mutating"
    assert reg.get("sandbox_write_file").dedupe is False
    assert reg.get("sandbox_write_file").concurrency == "serial"

    assert reg.get("show_data_table").kind == "presentation"
    assert reg.get("show_data_table").dedupe is False

    listed = {t["name"]: t for t in reg.list_tools()}
    assert listed["skill_read"]["kind"] == "idempotent"
    assert listed["oob_check_events"]["completion"] == "inline"


def test_scanner_tools_register_as_background() -> None:
    reg = ToolRegistry()
    reg.register("sqlmap_run_scan", "scan", {}, lambda: None, completion="background", concurrency="serial")
    reg.register("nuclei_run_scan", "scan", {}, lambda: None, completion="background", concurrency="serial")
    reg.register("ffuf_run_scan", "scan", {}, lambda: None, completion="background", concurrency="serial")
    for name in ("sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan"):
        tool = reg.get(name)
        assert tool.completion == "background"
        assert tool.concurrency == "serial"
