"""User profile tool confirmation governance."""

from __future__ import annotations

from argusai.core.assistant.streaming import (
    _CONFIRM_LABEL_AFTER_APPROVAL,
    _CONFIRM_TOOLS,
    _NO_AUTO_APPROVE_TOOLS,
    prepare_tool_arguments_for_dispatch,
)
from argusai.core.assistant.skills import _build_system_prompt, _select_tool_names


def test_profile_apply_in_confirm_sets() -> None:
    assert "user_profile_apply_update" in _CONFIRM_TOOLS
    assert "user_profile_suggest_update" in _CONFIRM_TOOLS
    assert "user_profile_apply_update" in _NO_AUTO_APPROVE_TOOLS


def test_confirm_label_injected_after_approval() -> None:
    args = prepare_tool_arguments_for_dispatch(
        "user_profile_apply_update",
        {"patch": {"speaking_style": "简洁"}},
        user_approved=True,
    )
    assert args["confirm_label"] == _CONFIRM_LABEL_AFTER_APPROVAL["user_profile_apply_update"]


def test_confirm_label_not_injected_without_approval() -> None:
    args = prepare_tool_arguments_for_dispatch(
        "user_profile_apply_update",
        {"patch": {"speaking_style": "简洁"}},
    )
    assert "confirm_label" not in args


def test_profile_tools_are_always_available_for_auto_summary() -> None:
    names = _select_tool_names("tasks", "随便看一下这个任务")
    assert "user_profile_read" in names
    assert "user_profile_suggest_update" in names
    assert "user_profile_apply_update" in names


def test_system_prompt_requires_proactive_profile_suggestions() -> None:
    prompt = _build_system_prompt("", "", "")
    assert "proactively summarize durable user preferences" in prompt
    assert "user_profile_suggest_update" in prompt
