"""Cache-usage normalization + tool-result compaction budget gate.

Covers the changes made to improve prompt-cache hit rate:
- ``_normalize_openai_cache_usage`` / ``_normalize_anthropic_cache_usage``
  (argusai/core/ai.py) turn provider-specific ``usage`` payloads into one
  canonical shape.
- ``_compact_old_tool_results_in_place`` (argusai/core/assistant/
  session_budget.py) now only elides once the request is actually big,
  instead of unconditionally rewriting old tool results whenever their
  count crosses ``keep_last`` — that used to bust the provider's
  byte-exact prompt-cache prefix on almost every tool-heavy turn.
"""

from __future__ import annotations

import json

from argusai.core.ai import (
    _normalize_anthropic_cache_usage,
    _normalize_openai_cache_usage,
)
from argusai.core.assistant.session_budget import SessionBudgetMixin


def test_openai_cache_usage_normalizes_hit_miss_and_reasoning():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 120,
        "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 40},
    }
    out = _normalize_openai_cache_usage(usage)
    assert out == {
        "input_tokens": 1000,
        "cache_hit_tokens": 900,
        "cache_miss_tokens": 100,
        "cache_write_tokens": 0,
        "output_tokens": 120,
        "reasoning_tokens": 40,
        "reply_tokens": 80,
    }


def test_openai_cache_usage_derives_miss_when_absent():
    out = _normalize_openai_cache_usage({"prompt_tokens": 500, "completion_tokens": 10, "prompt_cache_hit_tokens": 300})
    assert out["cache_miss_tokens"] == 200


def test_openai_cache_usage_reads_dashscope_prompt_tokens_details():
    out = _normalize_openai_cache_usage({
        "prompt_tokens": 2000,
        "completion_tokens": 12,
        "prompt_tokens_details": {
            "cached_tokens": 1600,
            "cache_creation_input_tokens": 100,
        },
    })
    assert out == {
        "input_tokens": 2000,
        "cache_hit_tokens": 1600,
        "cache_miss_tokens": 400,
        "cache_write_tokens": 100,
        "output_tokens": 12,
        "reasoning_tokens": 0,
        "reply_tokens": 12,
    }


def test_openai_cache_usage_reads_openai_cache_write_tokens():
    out = _normalize_openai_cache_usage({
        "prompt_tokens": 3000,
        "completion_tokens": 8,
        "prompt_tokens_details": {
            "cached_tokens": 2500,
            "cache_write_tokens": 200,
        },
    })
    assert out["cache_hit_tokens"] == 2500
    assert out["cache_write_tokens"] == 200
    assert out["cache_miss_tokens"] == 500


def test_openai_cache_usage_none_for_missing_or_empty():
    assert _normalize_openai_cache_usage(None) is None
    assert _normalize_openai_cache_usage({}) is None
    assert _normalize_openai_cache_usage({"prompt_tokens": 0, "completion_tokens": 0}) is None


def test_anthropic_cache_usage_splits_hit_miss_write():
    usage = {
        "input_tokens": 50,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 200,
        "output_tokens": 75,
    }
    out = _normalize_anthropic_cache_usage(usage)
    assert out == {
        "input_tokens": 1150,
        "cache_hit_tokens": 900,
        "cache_miss_tokens": 50,
        "cache_write_tokens": 200,
        "output_tokens": 75,
        "reasoning_tokens": 0,
        "reply_tokens": 75,
    }


def test_anthropic_cache_usage_none_for_empty():
    assert _normalize_anthropic_cache_usage(None) is None
    assert _normalize_anthropic_cache_usage({}) is None


class _CompactHarness(SessionBudgetMixin):
    """Minimal ``SessionBudgetMixin`` host for a fixed context window / keep_last."""

    def __init__(self, *, context_window: int = 131072, keep_last: int = 6) -> None:
        self._context_window = context_window
        self._keep_last = keep_last

    def _context_window_tokens(self) -> int:
        return self._context_window

    def _tool_result_keep_last(self) -> int:
        return self._keep_last

    def _tool_result_summary_cap(self) -> int:
        return 300


def _tool_messages(count: int, *, payload_chars: int = 40) -> list[dict]:
    body = json.dumps({"ok": True, "data": "x" * payload_chars})
    return [{"role": "tool", "tool_call_id": f"t{i}", "content": body} for i in range(count)]


def test_compact_skips_small_prompt_even_past_keep_last():
    """Regression: eliding purely on tool-result COUNT busts the cache prefix
    on almost every tool-heavy turn. A small prompt (far under budget) must
    keep every tool result verbatim, count notwithstanding."""
    h = _CompactHarness(keep_last=6)
    messages = [{"role": "system", "content": "sys"}, *_tool_messages(20)]
    original = [dict(m) for m in messages]
    saved = h._compact_old_tool_results_in_place(messages)
    assert saved == 0
    assert messages == original


def test_compact_fires_once_prompt_is_actually_big():
    h = _CompactHarness(context_window=32 * 1024, keep_last=6)
    # Push total size comfortably past the 45%-of-context trigger.
    messages = [{"role": "system", "content": "sys"}, *_tool_messages(40, payload_chars=2000)]
    saved = h._compact_old_tool_results_in_place(messages)
    assert saved > 0
    elided = [
        m for m in messages
        if m.get("role") == "tool" and str(m.get("content", "")).startswith('{"_elided"')
    ]
    # Everything except the tail keep_last window was compacted.
    assert len(elided) == 40 - 6


def test_compact_noop_when_keep_last_disabled():
    h = _CompactHarness(keep_last=0)
    messages = [{"role": "system", "content": "sys"}, *_tool_messages(40, payload_chars=2000)]
    assert h._compact_old_tool_results_in_place(messages) == 0


def test_prompt_tool_names_stay_frozen() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL, build_tool_registry

    session = AssistantSession(registry=build_tool_registry(None))
    first = session._merge_prompt_tool_names(["tool_search", "get_app_snapshot"])
    second = session._merge_prompt_tool_names(["nmap_scan"])
    assert second == first
    assert "tool_search" in first
    assert INVOKE_CATALOGUE_TOOL in first
    assert "cron_upsert" in first
    assert "spawn_agent" in first
    assert "nmap_scan" not in first


def test_legacy_session_tool_list_reseeds_core() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL, build_tool_registry

    registry = build_tool_registry(None)
    bloated = ["tool_search", *list(registry._tools)[:120]]
    loaded = AssistantSession.from_dict(
        {"history": [], "prompt_tool_names": bloated},
        registry=registry,
    )
    assert loaded._prompt_tool_names == loaded._core_prompt_tool_names()
    assert "tool_search" in loaded._prompt_tool_names
    assert INVOKE_CATALOGUE_TOOL in loaded._prompt_tool_names
    assert "cron_upsert" in loaded._prompt_tool_names
    assert "spawn_agent" in loaded._prompt_tool_names
    assert "nmap_scan" not in loaded._prompt_tool_names


def test_first_turn_keywords_do_not_dump_domain_groups() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.skills import _select_tool_names
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL, build_tool_registry

    session = AssistantSession(registry=build_tool_registry(None))
    selected = _select_tool_names("assistant", "用 nmap 扫一下端口", allow_web_search=True)
    assert "nmap_scan" in selected
    seeded = session._merge_prompt_tool_names(selected)
    assert "nmap_scan" not in seeded
    assert "tool_search" in seeded
    assert INVOKE_CATALOGUE_TOOL in seeded
    assert session._merge_prompt_tool_names(["nmap_scan"]) == seeded


def test_to_openai_tools_keeps_caller_order() -> None:
    from argusai.core.assistant.tools import build_tool_registry

    registry = build_tool_registry(None)
    names = ["cron_upsert", "tool_search", "get_app_snapshot"]
    serialized = [
        item["function"]["name"]
        for item in registry.to_openai_tools(names)
    ]
    assert serialized == names


def test_frozen_system_catalog_does_not_grow() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import build_tool_registry

    session = AssistantSession(registry=build_tool_registry(None))
    first = session._frozen_system_catalog(["tool_search"])
    later = session._frozen_system_catalog(["tool_search", "cron_upsert"])
    assert later == first
    assert "cron_upsert" not in first


def test_later_user_keywords_do_not_grow_frozen_tool_list() -> None:
    """After the first seed, nmap-style keywords must not stack a new group."""
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.skills import _select_tool_names
    from argusai.core.assistant.tools import build_tool_registry

    session = AssistantSession(registry=build_tool_registry(None))
    seed = _select_tool_names("assistant", "hello", allow_web_search=True)
    session._merge_prompt_tool_names(seed)
    frozen = list(session._prompt_tool_names)
    later = _select_tool_names("assistant", "用 nmap 扫一下端口", allow_web_search=True)
    assert "nmap_scan" in later
    assert "nmap_scan" not in frozen

    names, _tools = session._continue_tool_state(
        [{"role": "user", "content": "用 nmap 扫一下端口"}],
        [],
    )
    assert names == frozen
    assert "nmap_scan" not in names


def test_second_chat_turn_does_not_absorb_keyword_tools() -> None:
    import asyncio

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import build_tool_registry

    class RecorderAI:
        def __init__(self) -> None:
            self.tool_names: list[list[str]] = []

        async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
            names = [
                item["function"]["name"]
                for item in tools or []
                if isinstance(item, dict) and isinstance(item.get("function"), dict)
            ]
            self.tool_names.append(names)
            return {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}

    ai = RecorderAI()
    session = AssistantSession(registry=build_tool_registry(None), ai_provider=ai)
    asyncio.run(session.chat("hello", thinking_enabled=False))
    first = list(session._prompt_tool_names)
    asyncio.run(session.chat("用 nmap 扫一下端口", thinking_enabled=False))
    assert session._prompt_tool_names == first
    assert "nmap_scan" not in first
    assert "web_search" in first
    assert "cron_upsert" in first
    assert "spawn_agent" in first
    assert len(first) < 36
    assert ai.tool_names[0] == first
    assert ai.tool_names[1] == first
