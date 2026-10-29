"""Unified thinking / reasoning intensity for chat providers.

The composer and settings expose one scale. Each request then maps that
value onto whatever the active model actually accepts:

    OpenAI-compatible   thinking + reasoning_effort
    Anthropic 4.6+      thinking.adaptive + output_config.effort
    Anthropic 4.5 Opus  thinking.budget_tokens + output_config.effort
    older Claude        thinking.budget_tokens
    DeepSeek V4         thinking + reasoning_effort (low / high / max)

Unknown future OpenAI-compatible models get the industry default
(``thinking`` + ``reasoning_effort``). Unknown future Claude ids are
classified from the version in the name (4.6+ / 5+ → adaptive).
``extra_body`` still wins via ``setdefault``.
"""

from __future__ import annotations

import re
from typing import Any

EFFORT_LEVELS = ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"

WIRE_OPENAI = "openai"
WIRE_ANTHROPIC_ADAPTIVE = "anthropic_adaptive"
WIRE_ANTHROPIC_BUDGET = "anthropic_budget"
WIRE_ANTHROPIC_BUDGET_EFFORT = "anthropic_budget_effort"
WIRE_ANTHROPIC_DEEPSEEK = "anthropic_deepseek"

# Aliases accepted from the UI, CLI, channels, and settings.json.
EFFORT_ALIASES: dict[str, str] = {
    "auto": "auto",
    "automatic": "auto",
    "default": "auto",
    "none": "none",
    "off": "none",
    "minimal": "minimal",
    "min": "minimal",
    "lowest": "minimal",
    "low": "low",
    "medium": "medium",
    "mid": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra": "xhigh",
    "max": "max",
    "maximum": "max",
    "自动": "auto",
    "默认": "auto",
    "无": "none",
    "无推理": "none",
    "最低": "minimal",
    "低": "low",
    "中": "medium",
    "高": "high",
    "更高": "xhigh",
    "极高": "xhigh",
    "最大": "max",
}

# Anthropic extended-thinking token caps when the user picked a named level.
# ``high`` / ``auto`` keep the settings budget so today's default (1024) is unchanged.
_EFFORT_BUDGET: dict[str, int | None] = {
    "none": 256,
    "minimal": 512,
    "low": 1024,
    "medium": 4096,
    "high": None,
    "xhigh": 16384,
    "max": 32000,
}

_ADAPTIVE_NAME_HINTS = ("mythos", "fable")
_CLAUDE_FAMILY = re.compile(
    r"claude[-./](?:opus|sonnet|haiku)[-./](\d+)(?:[-./](\d+))?",
    re.IGNORECASE,
)
_CLAUDE_NUMBERED = re.compile(
    r"claude[-./](\d+)(?:[-./](\d+))?",
    re.IGNORECASE,
)
_O_SERIES = re.compile(r"(?:^|[/_.-])o[1-4](?:[-.]|mini|pro|\b)", re.IGNORECASE)


def normalize_thinking_effort(value: Any, *, default: str = DEFAULT_EFFORT) -> str:
    """Return a canonical effort level, or ``default`` when the value is empty/unknown."""
    text = str(value or "").strip()
    if not text:
        return default if default in EFFORT_LEVELS else DEFAULT_EFFORT
    mapped = EFFORT_ALIASES.get(text.casefold()) or EFFORT_ALIASES.get(text)
    if mapped in EFFORT_LEVELS:
        return mapped
    return default if default in EFFORT_LEVELS else DEFAULT_EFFORT


def resolve_thinking_budget(effort: str, settings_budget: int) -> int:
    """Token budget for Anthropic extended thinking."""
    normalized = normalize_thinking_effort(effort)
    mapped = _EFFORT_BUDGET.get(normalized)
    raw = settings_budget if mapped is None else mapped
    try:
        tokens = int(raw or 1024)
    except (TypeError, ValueError):
        tokens = 1024
    return max(256, min(tokens, 64000))


def _claude_generation(model: str) -> tuple[int, int] | None:
    text = model or ""
    match = _CLAUDE_FAMILY.search(text) or _CLAUDE_NUMBERED.search(text)
    if match is None:
        return None
    major = int(match.group(1))
    minor_raw = match.group(2)
    if minor_raw is None:
        return major, 0
    minor = int(minor_raw)
    # Dated snapshots such as ``claude-sonnet-4-5-20250929`` already captured 4.5;
    # a leftover YYYYMMDD must not become the minor version.
    if minor >= 2000:
        return major, 0
    return major, minor


def detect_anthropic_thinking_wire(model: str) -> str:
    """Choose the Anthropic thinking object for this model id."""
    name = (model or "").lower()
    if "deepseek" in name:
        return WIRE_ANTHROPIC_DEEPSEEK
    if any(hint in name for hint in _ADAPTIVE_NAME_HINTS):
        return WIRE_ANTHROPIC_ADAPTIVE
    generation = _claude_generation(name)
    if generation is not None:
        major, minor = generation
        if major >= 5 or (major == 4 and minor >= 6):
            return WIRE_ANTHROPIC_ADAPTIVE
        if major == 4 and minor == 5 and "opus" in name:
            return WIRE_ANTHROPIC_BUDGET_EFFORT
    return WIRE_ANTHROPIC_BUDGET


def detect_thinking_wire(protocol: str, model: str) -> str:
    proto = str(protocol or "openai").strip().lower()
    if proto == "anthropic":
        return detect_anthropic_thinking_wire(model)
    return WIRE_OPENAI


def _is_o_series(model: str) -> bool:
    name = (model or "").lower()
    if "gpt-4o" in name or "gpt-5" in name:
        return False
    return bool(_O_SERIES.search(name))


def map_openai_reasoning_effort(model: str, effort: str) -> str | None:
    """Value for top-level ``reasoning_effort``, or ``None`` to omit the field."""
    normalized = normalize_thinking_effort(effort)
    if normalized == "auto":
        return None
    name = (model or "").lower()
    if "deepseek" in name:
        return {
            "none": "low",
            "minimal": "low",
            "low": "low",
            "medium": "high",
            "high": "high",
            "xhigh": "high",
            "max": "max",
        }[normalized]
    if _is_o_series(name) or "grok" in name:
        return {
            "none": "low",
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
            "max": "high",
        }[normalized]
    return normalized


def map_anthropic_effort(effort: str) -> str | None:
    normalized = normalize_thinking_effort(effort)
    if normalized == "auto":
        return None
    if normalized in {"none", "minimal"}:
        return "low"
    return normalized


def _set_output_effort(body: dict[str, Any], effort: str | None) -> None:
    if not effort:
        return
    current = body.get("output_config")
    if current is None:
        body["output_config"] = {"effort": effort}
        return
    if isinstance(current, dict):
        current.setdefault("effort", effort)


def apply_openai_thinking(
    body: dict[str, Any],
    thinking_enabled: bool | None,
    *,
    model: str = "",
    effort: str = DEFAULT_EFFORT,
) -> None:
    """Mutate an OpenAI-compatible chat body. ``extra_body`` already merged."""
    if thinking_enabled is False:
        body["thinking"] = {"type": "disabled"}
        body.pop("reasoning_effort", None)
        return
    if thinking_enabled is not True:
        return
    body.setdefault("thinking", {"type": "enabled"})
    mapped = map_openai_reasoning_effort(model, effort)
    if mapped:
        body.setdefault("reasoning_effort", mapped)


def apply_anthropic_thinking(
    body: dict[str, Any],
    thinking_enabled: bool | None,
    *,
    model: str = "",
    effort: str = DEFAULT_EFFORT,
    budget_tokens: int = 1024,
) -> None:
    """Mutate an Anthropic Messages body. ``extra_body`` already merged."""
    if thinking_enabled is False:
        body["thinking"] = {"type": "disabled"}
        return
    if thinking_enabled is not True:
        return
    wire = detect_anthropic_thinking_wire(model)
    normalized = normalize_thinking_effort(effort)
    mapped = map_anthropic_effort(normalized)
    budget = resolve_thinking_budget(normalized, budget_tokens)
    if "thinking" not in body:
        if wire == WIRE_ANTHROPIC_ADAPTIVE:
            body["thinking"] = {"type": "adaptive"}
        elif wire == WIRE_ANTHROPIC_DEEPSEEK:
            body["thinking"] = {"type": "enabled"}
        else:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
    if wire in {
        WIRE_ANTHROPIC_ADAPTIVE,
        WIRE_ANTHROPIC_BUDGET_EFFORT,
        WIRE_ANTHROPIC_DEEPSEEK,
    }:
        _set_output_effort(body, mapped)


def thinking_effort_from_config(config: Any, fallback: str = DEFAULT_EFFORT) -> str:
    return normalize_thinking_effort(
        getattr(config, "thinking_effort", None),
        default=fallback,
    )


MENU_STEPS = ("off",) + EFFORT_LEVELS


def thinking_scale_current(enabled: bool, effort: Any = DEFAULT_EFFORT) -> str:
    """Session-level menu step: ``off`` or a canonical effort."""
    if not enabled:
        return "off"
    return normalize_thinking_effort(effort)


def thinking_scale_line(enabled: bool, effort: Any = DEFAULT_EFFORT) -> str:
    """One-line intensity ruler for CLI / channel status (``[high]`` = current)."""
    current = thinking_scale_current(enabled, effort)
    parts = [f"[{step}]" if step == current else step for step in MENU_STEPS]
    return " ".join(parts)
