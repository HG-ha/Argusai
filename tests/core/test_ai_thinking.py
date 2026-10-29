"""Unified thinking intensity mapping for current and future models."""

from __future__ import annotations

from argusai.core.ai_thinking import (
    WIRE_ANTHROPIC_ADAPTIVE,
    WIRE_ANTHROPIC_BUDGET,
    WIRE_ANTHROPIC_BUDGET_EFFORT,
    apply_anthropic_thinking,
    apply_openai_thinking,
    detect_anthropic_thinking_wire,
    map_openai_reasoning_effort,
    normalize_thinking_effort,
    resolve_thinking_budget,
    thinking_scale_line,
)


def test_normalize_thinking_effort_aliases() -> None:
    assert normalize_thinking_effort("更高") == "xhigh"
    assert normalize_thinking_effort("MAX") == "max"
    assert normalize_thinking_effort("") == "high"
    assert normalize_thinking_effort("nope") == "high"
    assert normalize_thinking_effort("nope", default="auto") == "auto"


def test_openai_default_matches_previous_high() -> None:
    body: dict = {}
    apply_openai_thinking(body, True, model="gpt-5.4", effort="high")
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


def test_openai_auto_omits_effort() -> None:
    body: dict = {}
    apply_openai_thinking(body, True, model="gpt-5.4", effort="auto")
    assert body["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in body


def test_openai_extra_body_wins() -> None:
    body = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    apply_openai_thinking(body, True, model="gpt-5.4", effort="max")
    assert body["reasoning_effort"] == "low"


def test_openai_off_clears_effort() -> None:
    body = {"reasoning_effort": "high"}
    apply_openai_thinking(body, False, model="gpt-5.4", effort="max")
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_deepseek_and_o_series_remap() -> None:
    assert map_openai_reasoning_effort("deepseek-v4-pro", "medium") == "high"
    assert map_openai_reasoning_effort("deepseek-flash", "xhigh") == "high"
    assert map_openai_reasoning_effort("deepseek-v4-flash", "max") == "max"
    assert map_openai_reasoning_effort("o3-mini", "xhigh") == "high"
    assert map_openai_reasoning_effort("grok-4", "none") == "low"
    assert map_openai_reasoning_effort("gpt-4o", "xhigh") == "xhigh"


def test_anthropic_wire_from_model_name() -> None:
    assert detect_anthropic_thinking_wire("claude-sonnet-4-5-20250929") == WIRE_ANTHROPIC_BUDGET
    assert detect_anthropic_thinking_wire("claude-opus-4-5") == WIRE_ANTHROPIC_BUDGET_EFFORT
    assert detect_anthropic_thinking_wire("claude-sonnet-4-6") == WIRE_ANTHROPIC_ADAPTIVE
    assert detect_anthropic_thinking_wire("claude-opus-5") == WIRE_ANTHROPIC_ADAPTIVE
    assert detect_anthropic_thinking_wire("claude-mythos-preview") == WIRE_ANTHROPIC_ADAPTIVE
    assert detect_anthropic_thinking_wire("claude-3-7-sonnet") == WIRE_ANTHROPIC_BUDGET


def test_anthropic_adaptive_uses_output_config() -> None:
    body: dict = {}
    apply_anthropic_thinking(body, True, model="claude-sonnet-4-6", effort="xhigh")
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "xhigh"}


def test_anthropic_future_opus_five_uses_adaptive() -> None:
    body: dict = {}
    apply_anthropic_thinking(body, True, model="anthropic/claude-opus-5.2", effort="max")
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"]["effort"] == "max"


def test_anthropic_old_budget_scales_from_effort() -> None:
    body: dict = {}
    apply_anthropic_thinking(
        body,
        True,
        model="claude-sonnet-4-5",
        effort="max",
        budget_tokens=1024,
    )
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] == 32000
    assert "output_config" not in body


def test_anthropic_high_keeps_settings_budget() -> None:
    assert resolve_thinking_budget("high", 1024) == 1024
    assert resolve_thinking_budget("auto", 2048) == 2048
    body: dict = {}
    apply_anthropic_thinking(
        body,
        True,
        model="claude-3-7-sonnet",
        effort="high",
        budget_tokens=1024,
    )
    assert body["thinking"]["budget_tokens"] == 1024


def test_thinking_scale_line_marks_current() -> None:
    assert thinking_scale_line(False, "high") == (
        "[off] auto none minimal low medium high xhigh max"
    )
    assert thinking_scale_line(True, "max") == (
        "off auto none minimal low medium high xhigh [max]"
    )
