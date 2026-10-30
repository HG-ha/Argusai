"""AI Assistant.

Two-layer design:

**Tool layer** — Universal function-calling tools. Always available in every
turn (with progressive disclosure: only context-relevant schemas sent).

**Skills layer** — .md documents in ``argusai/skills/``. Pure knowledge that
the AI reads as guidance. Skills are injected into the system prompt when
their trigger keywords match the user's message.

Adding a new skill = dropping a .md file into ``argusai/skills/``.
No Python code needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import inspect
import itertools
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable, Iterable

import yaml

from argusai.core.agent_runtimes import (
    SessionRuntimeBinding,
    normalize_session_runtime_binding,
)
from argusai.core.tools import EXECUTORS

log = logging.getLogger("argusai.assistant")

# Canonical shape normalized providers attach as ``result["_cache_usage"]``
# (see argusai/core/ai.py). Kept as an ordered tuple so ``token_usage_total``
# always serializes with a stable key set even before the first AI call.
_TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "reply_tokens",
    "calls",
)

MAX_CONTEXT_NOTES_CHARS = 12_000

from .attachments import *
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

from .session_blocks import SessionBlocksMixin
from .session_budget import SessionBudgetMixin
from .session_chat_loop import SessionChatLoopMixin
from .session_loop_control import SessionLoopControlMixin
from .session_memory import SessionMemoryMixin
from .session_tool_methods import SessionToolMethodsMixin












# ---------------------------------------------------------------------------
# Assistant session
# ---------------------------------------------------------------------------


class AssistantSession(
    SessionBlocksMixin,
    SessionBudgetMixin,
    SessionChatLoopMixin,
    SessionMemoryMixin,
    SessionLoopControlMixin,
    SessionToolMethodsMixin,
):
    # ``MAX_HISTORY`` is the soft ceiling of recent messages we replay to the AI
    # every turn. Each agent round in a single turn adds 2 entries to history
    # (assistant w/ tool_calls + tool result), so an 80-round turn alone can
    # produce ~160 entries. If MAX_HISTORY is smaller than that, the EARLY part
    # of the same turn (including the original user goal, the initial planning,
    # and the first batch of findings) silently disappears from the model's
    # context, which feels like memory loss. Keep this >= ``2 * max_agent_rounds``
    # plus a comfortable cushion for the user/assistant messages from earlier
    # turns of the same session. ``rebuild_memory`` still summarises the very
    # old tail so we never grow unboundedly.
    MAX_HISTORY = 200
    MEMORY_INDEX_LIMIT = 600

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        ai_provider: Any = None,
        kb: Any = None,
    ) -> None:
        self.registry = registry or ToolRegistry()
        self.ai = ai_provider
        # Optional reference to a ``KnowledgeBase`` instance so the session
        # prompt can surface a "you've solved similar things before" hint
        # to the model on every turn. Tool dispatch reaches the same kb
        # through ``api.kb`` so this is only for the prompt-side preview.
        self.kb = kb
        self.history: list[ChatMessage] = []
        # Typed runtime/UI facts live outside model-visible chat history.
        self.events: list[dict[str, Any]] = []
        self.memory_summary: str = ""
        self.memory_index: list[dict[str, Any]] = []
        self.todos: list[dict[str, Any]] = []
        # Current analysis case used for automatic archival of large/important
        # tool results. The model can still create/read cases explicitly, but
        # the loop also lazily creates one for web evidence so compression keeps
        # stable artifact references instead of losing payload/response details.
        self.current_case_id: str = ""
        # Optional per-session override for automatic tool-result archival.
        # ``None`` means "use the global settings default"; a list means the
        # model has intentionally adjusted this session's archive policy.
        self.auto_archive_tools_override: list[str] | None = None
        self.allow_web_search: bool = True
        # Per-session thinking / reasoning toggle for CLI and channels
        # (``/think on|off``). GUI still sends an explicit per-request override.
        self.thinking_enabled: bool = False
        self.thinking_effort: str = "high"
        # When True, channel turns push intermediate assistant segments and
        # projectable UI cards to the remote chat instead of only the final reply.
        # Default on; ``/live off`` still waits for the end-of-turn reply.
        self.channel_live: bool = True
        self.runtime_binding = SessionRuntimeBinding()
        # User-maintained persistent context for this conversation. It is
        # attached to the live user turn (never promoted to system authority)
        # and therefore cannot override authorization or tool-layer controls.
        self.context_notes: str = ""
        # Frozen tools[] for OpenAI/Codex prefix cache. Extra catalogue tools
        # are discovered via tool_search results and run with invoke_catalogue_tool.
        self._prompt_tool_names: list[str] = []
        self._prompt_tool_core_len: int = 0
        self._prompt_tool_flex_lru: list[str] = []
        self._prompt_tool_flex_used: set[str] = set()
        self._prompt_system_catalog: str = ""
        self._prompt_prefix_sha: str = ""
        # Optional per-session Greenlight declaration (scope, RoE). Injected
        # into the system prompt so the model cooperates without repeated
        # intent/authorization review. Tool-layer hard controls are unchanged.
        self.engagement_declaration: dict[str, Any] | None = None
        # Set by ``_call_ai`` on every AI request so the UI / metrics layer
        # can show the user how big the last prompt was. Read this if the
        # assistant feels slow — values around or above 200 KB explain why.
        self.last_prompt_bytes: int = 0
        self.last_prompt_tokens_est: int = 0
        # History-only byte count captured with the last prompt, so the
        # composer ring can add system/tool overhead onto the *current*
        # transcript (Cursor-style context fill, not last-request size).
        self.last_prompt_history_bytes: int = 0
        # Cumulative + last-call cache/token usage, populated by
        # ``_record_token_usage`` right after each provider response. Powers
        # the "Token 消耗明细" panel and lets a low cache-hit ratio be
        # diagnosed from the UI instead of grepping logs.
        self.token_usage_total: dict[str, int] = dict.fromkeys(_TOKEN_USAGE_FIELDS, 0)
        self.last_token_usage: dict[str, int] | None = None
        # Per-turn accumulator; reset at the top of ``chat()``. Initialized
        # here too so any call path that reaches ``_record_token_usage``
        # without going through a full turn (e.g. a side question) never hits
        # an AttributeError.
        self._turn_token_usage: dict[str, int] = {}
        self._register_session_tools()
        self._pending: dict[str, Any] | None = None
        self._cancel_requested = False
        self._auto_approve_tools: set[str] = set()
        # Conversation preference: when True the runtime auto-allows
        # confirmable tools (including high-risk) and auto-picks decision /
        # todo-plan cards until the turn finishes. Persisted per session and
        # seeded once from the global default, never mirrored back to settings.
        self._user_auto_approve_mode: bool = False
        self._auto_approve_policy_initialized: bool = False
        self.native_model_selection: dict[str, str] = {}
        # Finer-grained auto-allow surface introduced for MCP. The
        # confirmation card can carry an opaque ``permission_key`` (e.g.
        # ``mcp:filesystem-abc:read_file``) which the user can elect to
        # remember for this session WITHOUT auto-allowing every other
        # use of the wrapping tool (e.g. mcp_server_call_tool).
        self._auto_approve_permissions: set[str] = set()
        # Per-turn flag — set to True whenever the model touches the todo list
        # in the current user turn. Used to gate auto-continue so stale pending
        # todos left over from a previous turn don't hijack a brand-new question.
        self._todos_touched_this_turn: bool = False
        # Typed origin of the active turn. Cron-created plans are already
        # authorized by the confirmed schedule and must not ask for a second
        # plan-level confirmation.
        self._current_turn_source: str = ""
        self._current_turn_source_ref: str = ""
        # False after ``set_todos`` / plan edits until the user confirms the
        # assessment plan. While False, auto-continue and in_progress todos
        # are blocked so execution cannot start before review.
        self._todos_plan_approved: bool = True
        # Set by ``stop_for_user_decision``. Auto-continue checks this flag
        # before reinjecting a hidden continue prompt — if the model has
        # explicitly asked the user something, we must NOT auto-continue,
        # regardless of how the todo list looks. Reset at the start of each
        # user turn.
        self._user_decision_requested: bool = False
        # Standing-team members set this by calling ``team_room_pass``.
        # The turn ends without a room message.
        self._team_room_passed: bool = False
        # Set by the UI when the user types an interjection while the
        # assistant is running. We let the current tool/result cycle finish,
        # then suppress hidden auto-continue so the queued user message can
        # be sent before the agent advances to the next step.
        self._pause_after_current_response: bool = False
        # Persistence callback installed by ``chat()`` for the duration of
        # one user turn. Invoked by ``_emit_checkpoint`` after every
        # meaningful state mutation so the host can flush history to disk
        # incrementally — a network error in round 47 must not lose the
        # work of rounds 1-46.
        self._on_checkpoint: Callable[[], Awaitable[None] | None] | None = None
        # Accumulates the most recent stream's cleaned content (post pseudo-
        # tag-stripping) as it gets emitted to the UI via ``on_delta``. Read
        # by ``_commit_cancelled_assistant`` so a mid-stream cancel can still
        # persist whatever the user already saw on screen. Reset at the
        # start of every ``_call_ai`` invocation so each AI round only
        # records *its* own partial output.
        self._last_partial_text: str = ""
        # Set to True by ``_call_ai`` whenever the in-stream repetition
        # guard triggered "truncate" or "stop" for the most recent LLM
        # response. The agent loop reads this flag after every round so
        # it can refuse to feed another tool batch into a model that is
        # plainly stuck in a low-entropy loop — without this the model
        # may immediately re-emit the same paragraph on the next round.
        self._stream_truncated_this_round: bool = False
        # Set to True by :func:`_commit_stream_repetition_stop` so the
        # next ``chat()`` call can recognise the user is replying to a
        # repetition recovery prompt and route them to the matching
        # in-session branch (self-correct / skip / pause).
        self._stream_repetition_marker: bool = False
        # Ring buffer of per-round signatures used by the deadlock
        # detector (see ``_record_round_signature`` and
        # ``_agent_loop_circuit_breaker_exceeded``). A signature combines
        # the cleaned assistant text fingerprint with the sorted set of
        # this round's ``(tool_name, args_hash)`` calls — so two rounds
        # only collide when the model produced literally the same reply
        # *and* the same tool plan. Length-bounded so an extremely long
        # conversation cannot grow this list without bound.
        self._recent_round_signatures: list[dict[str, Any]] = []
        # Diagnostic ring buffer of recent (tool_name, arg_hash) tuples so
        # we can detect "the model is firing the same tool with the same
        # arguments over and over". The actual de-duplication happens
        # elsewhere; this is purely for logging.
        self._recent_tool_signatures: list[tuple[str, str]] = []
        # Last logged agent-loop round counter. ``_log_round_start`` /
        # ``_log_round_end`` use this so the assistant_loop log gets a
        # crisp "round N started → round N ended (took … ms, … tool calls)"
        # paired entry that the developer can grep for when chasing a
        # particular runaway turn.
        self._current_round_index: int = 0
        self._current_turn_id: str = "-"
        # Per-turn flag: when Loom22 §11 dedup fires we inject a one-shot
        # hidden system hint nudging the model to re-read prior results.
        # Resets every user turn (see ``_reset_agent_loop_stall_tracking``).
        self._duplicate_skip_hint_injected: bool = False
        # Cheap fingerprint of the history the memory summary/index was last
        # built from. ``rebuild_memory`` skips its O(history) scan when this
        # still matches — long turns checkpoint ~10x and the finally-flush /
        # repeated searches would otherwise rebuild over unchanged history.
        self._memory_fingerprint: tuple[int, float, int, int, int] | None = None
        # Durable background job ledger (Loom22 §16.7). Created lazily on first
        # ``completion=background`` dispatch so sessions that never park a job
        # pay zero cost.
        self._background_jobs = None
        # Sub-agent nesting depth (hard-capped at 1 — see ``subagent.py``).
        self._subagent_depth: int = 0
        # Acceptance closed-loop repair counter for the current user turn.
        self._acceptance_repair_count: int = 0
        # Child-Agent ids whose completion notice is in this turn's prompt but
        # not yet confirmed delivered. Drained after the first successful
        # provider round so delivery is exactly-once, not at-most-once.
        self._pending_child_agent_notice_ids: list[str] = []

    def _record_token_usage(self, usage: dict[str, Any] | None) -> None:
        """Accumulate one provider call's cache/token usage onto the session.

        ``usage`` is the canonical ``_cache_usage`` dict providers attach in
        ``argusai/core/ai.py`` (``None`` for endpoints that report no usage at
        all — never call this with the raw provider-specific shape).
        """
        if not isinstance(usage, dict):
            return
        self.last_token_usage = dict(usage)
        for key in _TOKEN_USAGE_FIELDS:
            if key == "calls":
                continue
            self.token_usage_total[key] = self.token_usage_total.get(key, 0) + int(usage.get(key) or 0)
            self._turn_token_usage[key] = self._turn_token_usage.get(key, 0) + int(usage.get(key) or 0)
        self.token_usage_total["calls"] = self.token_usage_total.get("calls", 0) + 1
        self._turn_token_usage["calls"] = self._turn_token_usage.get("calls", 0) + 1

    def cache_hit_rate(self) -> float | None:
        """Session-cumulative cache hit ratio, or ``None`` before any AI call."""
        hit = self.token_usage_total.get("cache_hit_tokens", 0)
        miss = self.token_usage_total.get("cache_miss_tokens", 0)
        denom = hit + miss
        if denom <= 0:
            return None
        return round(100.0 * hit / denom, 1)

    def _turn_token_usage_snapshot(self) -> dict[str, int] | None:
        """The current turn's accumulated usage, or ``None`` if no AI call has
        completed yet this turn. Attached to the turn's final visible
        assistant ``ChatMessage`` so a per-message "Token" button can show
        that one reply's cost instead of the whole session's running total.
        """
        if not self._turn_token_usage:
            return None
        return dict(self._turn_token_usage)

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        self.rebuild_memory()
        return {
            "history": [m.to_dict() for m in self.history],
            "events": list(self.events[-2000:]),
            "memory_summary": self.memory_summary,
            "memory_index": self.memory_index,
            "todos": self.todos,
            "todos_plan_approved": bool(self._todos_plan_approved),
            "current_case_id": self.current_case_id,
            "auto_archive_tools_override": self.auto_archive_tools_override,
            "allow_web_search": bool(self.allow_web_search),
            "thinking_enabled": bool(self.thinking_enabled),
            "thinking_effort": str(self.thinking_effort or "high"),
            "channel_live": bool(self.channel_live),
            "runtime": self.runtime_binding.to_dict(),
            "context_notes": self.context_notes,
            "engagement_declaration": self.engagement_declaration,
            "auto_approve_mode": bool(self._user_auto_approve_mode),
            "native_model_selection": dict(self.native_model_selection),
            "auto_approve_tools": sorted(self._auto_approve_tools),
            "auto_approve_permissions": sorted(self._auto_approve_permissions),
            "pending": self._pending,
            "token_usage_total": dict(self.token_usage_total),
            "last_token_usage": dict(self.last_token_usage) if self.last_token_usage else None,
            "last_prompt_tokens_est": int(self.last_prompt_tokens_est or 0),
            "last_prompt_bytes": int(self.last_prompt_bytes or 0),
            "last_prompt_history_bytes": int(self.last_prompt_history_bytes or 0),
            "prompt_tool_names": list(self._prompt_tool_names),
            "prompt_tool_core_len": int(self._prompt_tool_core_len or 0),
            "prompt_tool_flex_lru": list(self._prompt_tool_flex_lru or []),
            "prompt_tool_flex_used": sorted(self._prompt_tool_flex_used or []),
            "prompt_system_catalog": self._prompt_system_catalog,
        }

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        registry: ToolRegistry,
        ai_provider: Any = None,
        kb: Any = None,
    ) -> "AssistantSession":
        s = cls(registry=registry, ai_provider=ai_provider, kb=kb)
        s.history = [ChatMessage.from_dict(m) for m in (d.get("history") or [])]
        s.events = [
            dict(item)
            for item in (d.get("events") or [])
            if isinstance(item, dict)
        ][-2000:]
        s.memory_summary = str(d.get("memory_summary") or "")
        s.memory_index = list(d.get("memory_index") or [])
        s.todos = s._normalize_todos(d.get("todos") or [])
        raw_plan_approved = d.get("todos_plan_approved")
        if raw_plan_approved is None:
            flat = s._flatten_todos(s.todos)
            s._todos_plan_approved = (
                not s.todos
                or any(item.get("status") == "in_progress" for item in flat)
            )
        else:
            s._todos_plan_approved = bool(raw_plan_approved)
        s.current_case_id = str(d.get("current_case_id") or "")
        s.allow_web_search = bool(d.get("allow_web_search", True))
        s.thinking_enabled = bool(d.get("thinking_enabled", False))
        from argusai.core.ai_thinking import normalize_thinking_effort

        s.thinking_effort = normalize_thinking_effort(d.get("thinking_effort"))
        try:
            s.last_prompt_tokens_est = max(0, int(d.get("last_prompt_tokens_est") or 0))
        except (TypeError, ValueError):
            s.last_prompt_tokens_est = 0
        try:
            s.last_prompt_bytes = max(0, int(d.get("last_prompt_bytes") or 0))
        except (TypeError, ValueError):
            s.last_prompt_bytes = 0
        try:
            s.last_prompt_history_bytes = max(0, int(d.get("last_prompt_history_bytes") or 0))
        except (TypeError, ValueError):
            s.last_prompt_history_bytes = 0
        s.channel_live = bool(d.get("channel_live", True))
        s.runtime_binding = normalize_session_runtime_binding(d.get("runtime"))
        s.context_notes = str(d.get("context_notes") or "").strip()[:MAX_CONTEXT_NOTES_CHARS]
        eng_raw = d.get("engagement_declaration")
        if isinstance(eng_raw, dict):
            from .engagement import normalize_engagement

            s.engagement_declaration = normalize_engagement(eng_raw)
        archive_override = d.get("auto_archive_tools_override")
        if isinstance(archive_override, list):
            s.auto_archive_tools_override = s._normalize_tool_names(archive_override)
        s._user_auto_approve_mode = bool(d.get("auto_approve_mode", False))
        s._auto_approve_policy_initialized = "auto_approve_mode" in d
        selection = d.get("native_model_selection")
        if isinstance(selection, dict):
            s.native_model_selection = {
                key: str(selection[key]) for key in ("endpoint_key", "endpoint_name", "model")
                if isinstance(selection.get(key), str)
            }
        auto_tools = d.get("auto_approve_tools")
        if auto_tools is True:
            s._auto_approve_tools = {name for name in s.registry._tools if name in _CONFIRM_TOOLS}
        elif isinstance(auto_tools, list):
            s._auto_approve_tools = {str(name) for name in auto_tools if str(name) in s.registry._tools}
        auto_perms = d.get("auto_approve_permissions")
        if isinstance(auto_perms, list):
            s._auto_approve_permissions = {str(p) for p in auto_perms if isinstance(p, str)}
        pending = d.get("pending")
        s._pending = dict(pending) if isinstance(pending, dict) else None
        raw_names = d.get("prompt_tool_names")
        if isinstance(raw_names, list):
            s._prompt_tool_names = [
                str(name).strip()
                for name in raw_names
                if str(name).strip() and str(name).strip() in s.registry._tools
            ]
        s._restore_prompt_tool_partition(d)
        s._prompt_system_catalog = str(d.get("prompt_system_catalog") or "")
        raw_usage = d.get("token_usage_total")
        if isinstance(raw_usage, dict):
            for key in _TOKEN_USAGE_FIELDS:
                try:
                    s.token_usage_total[key] = int(raw_usage.get(key) or 0)
                except (TypeError, ValueError):
                    s.token_usage_total[key] = 0
        raw_last = d.get("last_token_usage")
        if isinstance(raw_last, dict):
            restored_last: dict[str, int] = {}
            for key in _TOKEN_USAGE_FIELDS:
                if key == "calls":
                    continue
                try:
                    restored_last[key] = int(raw_last.get(key) or 0)
                except (TypeError, ValueError):
                    restored_last[key] = 0
            s.last_token_usage = restored_last
        s._repair_submitted_user_decisions()
        if not s.memory_index and s.history:
            s.rebuild_memory()
        return s

    def set_engagement_declaration(self, raw: dict[str, Any] | None) -> dict[str, Any] | None:
        from .engagement import normalize_engagement

        if not raw or not raw.get("active"):
            self.engagement_declaration = None
            return None
        payload = dict(raw)
        payload["active"] = True
        payload.setdefault("declared_at", time.time())
        self.engagement_declaration = normalize_engagement(payload)
        return self.engagement_declaration

    def set_context_notes(self, raw: Any) -> str:
        """Replace the persistent user context attached to future turns."""
        self.context_notes = str(raw or "").strip()[:MAX_CONTEXT_NOTES_CHARS]
        return self.context_notes

    def engagement_prompt_note(self) -> str:
        from .engagement import engagement_prompt_section

        return engagement_prompt_section(self.engagement_declaration)

    @property
    def message_count(self) -> int:
        return sum(1 for m in self.history if m.role in ("user", "assistant"))

    def cancel(self) -> None:
        self._cancel_requested = True
        self._pending = None

    def pause_after_current_response(self) -> None:
        self._pause_after_current_response = True

    def _commit_pause_after_current_response(
        self,
        text: str = "",
        response: dict[str, Any] | None = None,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Finish the current visible unit so a queued user message can run next."""
        response = response or {}
        traces = list(tool_calls_made or [])
        visible_text = text or ""
        blocks = self._blocks_for_tool_trace(traces) or self._blocks_for_text(visible_text)
        if visible_text:
            self.history.append(ChatMessage(
                role="assistant",
                content=visible_text,
                reasoning_content=response.get("reasoning_content") or "",
                provider_content=list(response.get("provider_content") or []),
                blocks=blocks,
                tool_trace=traces,
                timestamp=time.time(),
                token_usage=self._turn_token_usage_snapshot(),
            ))
        return ChatResponse(
            text=visible_text,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="done",
        )

    def set_auto_approve_tool(self, name: str, enabled: bool, force: bool = False) -> None:
        """Toggle a single tool's auto-approve state.

        ``_NO_AUTO_APPROVE_TOOLS`` are considered high-risk (sandbox
        scripts, SQLi probes, raw HTTP, sqlmap). For these we refuse the
        request unless the caller explicitly sets ``force=True`` — the
        UI uses this to enforce a "second confirmation" dialog before
        adding such tools to the auto-approve set. Once added, those
        tools execute without further prompts, just like any other
        auto-approved tool. The split is in the *enabling* path; the
        *runtime* honours the user's choice as-is.
        """
        if name not in self.registry._tools:
            raise KeyError(f"Unknown tool: {name}")
        if enabled and name in _NO_AUTO_APPROVE_TOOLS and name not in _REVERSE_DYNAMIC_TOOLS and not force:
            self._auto_approve_tools.discard(name)
            return
        if enabled:
            self._auto_approve_tools.add(name)
        else:
            self._auto_approve_tools.discard(name)

    def auto_approve_tools(self) -> list[str]:
        return sorted(self._auto_approve_tools)

    @staticmethod
    def native_endpoint_key(endpoint: Any) -> str:
        """Identify a configured route without persisting its credentials."""
        import hashlib

        identity = "\0".join(str(getattr(endpoint, key, "") or "") for key in ("name", "base_url", "protocol"))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def select_native_model(self, endpoint: Any, model: str) -> None:
        if not getattr(endpoint, "enabled", True) or model not in endpoint.model_catalog():
            raise ValueError("Selected conversation model is unavailable")
        self.native_model_selection = {
            "endpoint_key": self.native_endpoint_key(endpoint),
            "endpoint_name": str(getattr(endpoint, "name", "") or ""),
            "model": model,
        }

    def native_model_endpoints(self, endpoints: list[Any]) -> list[Any]:
        """Build a private failover chain; settings and other sessions stay untouched."""
        from dataclasses import replace

        enabled = sorted(
            (item for item in endpoints if getattr(item, "enabled", True)),
            key=lambda item: int(getattr(item, "priority", 0) or 0), reverse=True,
        )
        if not self.native_model_selection:
            if not enabled:
                return []
            self.select_native_model(enabled[0], enabled[0].model)
        selection = self.native_model_selection
        chosen = next((item for item in enabled if self.native_endpoint_key(item) == selection.get("endpoint_key")), None)
        model = selection.get("model", "")
        if chosen is None or model not in chosen.model_catalog():
            raise ValueError("Selected conversation model is unavailable; choose another model for this conversation")
        top = max(int(getattr(item, "priority", 0) or 0) for item in enabled)
        return [replace(chosen, model=model, priority=top + 10),
                *(replace(item) for item in enabled if item is not chosen)]

    @property
    def user_auto_approve_mode(self) -> bool:
        return bool(self._user_auto_approve_mode)

    def apply_user_auto_approve_mode(self, enabled: bool) -> list[str]:
        """Turn auto-approve on or off for this session.

        On: treat the session as an unattended runtime. Tools skip
        confirmation; the model must decide domain choices itself (cron and
        child Agents still host-pick). Todo plans auto-resolve. The composer
        toggle is the explicit opt-in.

        Off: clear the bulk auto-allow set. Callers should re-apply the
        settings always-allow list afterwards so extension-tier defaults
        remain.
        """
        self._user_auto_approve_mode = bool(enabled)
        self._auto_approve_policy_initialized = True
        if self._user_auto_approve_mode:
            return self.set_auto_approve_all(
                True,
                include_high_risk=True,
                include_reverse_dynamic=True,
            )
        return self.set_auto_approve_all(False)

    def replace_auto_approve_state(
        self,
        tools: Iterable[str],
        permissions: Iterable[str],
    ) -> dict[str, Any]:
        """Replace delegated approval state, filtering tools to this registry."""
        next_tools = {
            str(name)
            for name in tools
            if str(name) in self.registry._tools
        }
        next_permissions = {
            str(key).strip()
            for key in permissions
            if str(key).strip()
        }
        changed = (
            next_tools != self._auto_approve_tools
            or next_permissions != self._auto_approve_permissions
        )
        self._auto_approve_tools = next_tools
        self._auto_approve_permissions = next_permissions
        return {
            "changed": changed,
            "tool_count": len(next_tools),
            "permission_count": len(next_permissions),
        }

    def _is_unattended_child_agent(self) -> bool:
        """True for native/external child Agents that have no operator of their own."""
        return int(getattr(self, "_subagent_depth", 0) or 0) > 0

    def _is_unattended_runtime(self) -> bool:
        """Cron ticks, child Agents, and user auto-approve must not wait on a card."""
        source = str(getattr(self, "_current_turn_source", "") or "").strip().lower()
        return (
            source == "cron"
            or self._is_unattended_child_agent()
            or bool(self._user_auto_approve_mode)
        )

    def _should_agent_self_decide(self) -> bool:
        """User auto-approve: the model chooses. Cron/child still host-pick.

        Cursor-style auto-run means the agent keeps working and makes the
        domain choice itself. Host-picking option[0] is only for runtimes
        that have no operator and no extra model turn (cron ticks, children).
        """
        if not bool(self._user_auto_approve_mode):
            return False
        if self._is_unattended_child_agent():
            return False
        source = str(getattr(self, "_current_turn_source", "") or "").strip().lower()
        return source != "cron"

    def _unattended_source_label(self) -> str:
        if self._is_unattended_child_agent():
            return "child Agent"
        source = str(getattr(self, "_current_turn_source", "") or "").strip().lower()
        if source == "cron":
            return "Cron"
        return "Auto-approve"

    def _seed_unattended_child_auto_approve(self) -> None:
        """Treat the child's capability projection as already authorized.

        Parent delegation is the confirmation gate (same idea as cron ticks).
        A second card on the child deadlocks ``run_subagent`` / ``spawn_agent``
        because the child UI cannot answer it, and the parent wait never
        finishes. High-risk tools in the projection are included: the parent
        already chose the exact tool set.
        """
        if (
            not self._is_unattended_child_agent()
            or str(getattr(self, "_team_approval_mode", "leader_auto")) == "manual"
        ):
            return
        self.set_auto_approve_all(
            True,
            include_high_risk=True,
            include_reverse_dynamic=True,
        )

    def set_auto_approve_all(
        self,
        enabled: bool,
        include_high_risk: bool = False,
        include_reverse_dynamic: bool = False,
    ) -> list[str]:
        """Bulk toggle the per-session auto-approve list.

        ``enabled=True`` flips every confirmable tool to auto-allow. By
        default this EXCLUDES :data:`_NO_AUTO_APPROVE_TOOLS` so a single
        impatient click can't silently enable script execution / SQLi
        probes / raw HTTP — those still need per-tool double confirmation
        from the UI. Pass ``include_reverse_dynamic=True`` after the user
        acknowledges Frida attach/spawn in the session allow-all flow.
        Pass ``include_high_risk=True`` only when the user has explicitly
        acknowledged sandbox scripts / SQLi / raw HTTP risk.

        ``enabled=False`` clears the entire auto-approve set so every
        future tool call surfaces a confirmation card.
        """
        if enabled:
            self._auto_approve_tools = {
                name for name in self.registry._tools
                if name in _CONFIRM_TOOLS and (
                    name not in _NO_AUTO_APPROVE_TOOLS
                    or (include_reverse_dynamic and name in _REVERSE_DYNAMIC_TOOLS)
                    or (include_high_risk and name in _NO_AUTO_APPROVE_TOOLS)
                )
            }
        else:
            self._auto_approve_tools = set()
        return self.auto_approve_tools()

    def _tool_auto_approved(self, name: str, arguments: dict[str, Any] | None = None) -> bool:
        # Note: we no longer second-guess _NO_AUTO_APPROVE_TOOLS at the
        # call site. The double-confirmation happens at the *enable* path
        # (see set_auto_approve_tool). Once a tool is in the user's
        # auto-approve set we trust their choice and execute silently.
        if self._user_auto_approve_mode:
            return True
        if name in self._auto_approve_tools:
            return True
        if not arguments:
            return False
        # MCP per-tool auto-allow: user may have ticked "auto-allow this
        # specific MCP server/tool" on a previous card without enabling
        # mcp_server_call_tool wholesale.
        key = self._mcp_permission_key(name, arguments)
        if key and key in self._auto_approve_permissions:
            return True
        return False

    def _runtime_tools_dir(self) -> Path:
        from argusai.core.paths import PathManager

        api = getattr(self.registry, "api", None)
        try:
            data_root = api.settings_mgr.get().data_root
        except Exception:
            data_root = None
        return PathManager(data_root=data_root).tools_dir

    def _runtime_mirrors(self) -> dict[str, Any]:
        api = getattr(self.registry, "api", None)
        try:
            value = api.settings_mgr.get().runtime_mirrors
        except Exception:
            value = {}
        return dict(value) if isinstance(value, dict) else {}

    def _runtime_dependency(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Return the managed runtime required before dispatching one tool."""
        args = arguments or {}
        python_tools = {
            "run_python_script",
            "run_python_snippet",
            "sandbox_pip_install",
            "python_reproducer_verify",
        }
        node_tools = {
            "run_node_script",
            "run_js_function",
            "js_deobfuscate_heavy",
            "js_unpack_bundle",
            "js_runtime_create",
            "js_runtime_load",
            "js_runtime_eval",
            "js_runtime_call",
            "js_runtime_hook",
            "js_runtime_patch",
            "js_runtime_instrument",
            "js_runtime_restore",
        }
        # Only these two shell out to WABT; the other wasm_* tools parse the
        # module in pure Python and must keep working without an install.
        wabt_tools = {"wasm_disassemble", "wasm_decompile"}
        if name in python_tools:
            return "python"
        if name in node_tools:
            return "node"
        if name in wabt_tools:
            return "wabt"
        if name in {"run_shell_snippet", "run_shell_script"}:
            body = str(args.get("script") or args.get("code") or "")
            if re.search(r"\b(?:py|python3?|pip3?)\b", body, re.IGNORECASE):
                return "python"
        if name == "reverse_toolchain_install":
            if str(args.get("tool") or "").strip().lower() in {"frida", "objection", "capa"}:
                return "python"

        command = ""
        if name.startswith("mcp_stdio_"):
            command = str(args.get("command") or "")
        elif name.startswith("mcp_server_") and name not in {"mcp_server_add", "mcp_server_delete"}:
            server_id = str(args.get("server_id") or "").strip()
            api = getattr(self.registry, "api", None)
            try:
                servers = api.settings_mgr.get().mcp_servers
            except Exception:
                servers = []
            server = next(
                (item for item in servers if str(item.get("id") or "").strip() == server_id),
                {},
            )
            command = str(server.get("command") or "")
        clean_command = command.strip()
        if clean_command and not re.search(r"[/\\]", clean_command):
            base = clean_command.lower()
            if base.endswith((".exe", ".cmd")):
                base = base.rsplit(".", 1)[0]
            if base in {"node", "npm", "npx", "yarn", "pnpm", "pnpx"}:
                return "node"
        return ""

    def _runtime_is_ready(self, runtime: str) -> bool:
        if not runtime:
            return True
        try:
            from argusai.core.managed_runtimes import runtime_status

            return bool(runtime_status(runtime, self._runtime_tools_dir()).get("healthy"))
        except Exception:
            return False

    def _confirmation_requirements(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build confirmation cards, grouping each missing runtime into one card."""
        if self._is_unattended_child_agent():
            if str(getattr(self, "_team_approval_mode", "leader_auto")) != "manual":
                self._seed_unattended_child_auto_approve()
                return []
        confirmations: list[dict[str, Any]] = []
        runtime_items: dict[str, dict[str, Any]] = {}
        for tool_call in tool_calls:
            name, args = resolve_session_tool_call(tool_call)
            if tool_arguments_error(args):
                # Malformed calls are rejected at dispatch; never ask the user
                # to approve an action whose arguments cannot be displayed.
                continue
            runtime = self._runtime_dependency(name, args)
            if runtime and not self._runtime_is_ready(runtime):
                item = runtime_items.get(runtime)
                if item is None:
                    item = {
                        "name": name,
                        "arguments": args,
                        "tool_call": tool_call,
                        "runtime": runtime,
                        "covered_tool_ids": [],
                        "covered_tool_names": [],
                    }
                    runtime_items[runtime] = item
                    confirmations.append(item)
                item["covered_tool_ids"].append(tool_call["id"])
                item["covered_tool_names"].append(name)
                continue
            if name in _CONFIRM_TOOLS and not self._tool_auto_approved(name, args):
                confirmations.append({
                    "name": name,
                    "arguments": args,
                    "tool_call": tool_call,
                })
        return confirmations

    async def _ensure_runtime_for_dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approved_runtime: str = "",
    ) -> dict[str, Any] | None:
        runtime = self._runtime_dependency(name, arguments)
        if not runtime or self._runtime_is_ready(runtime):
            return None
        from argusai.core.managed_runtimes import (
            install_runtime,
            managed_runtime_required,
        )

        tools_dir = self._runtime_tools_dir()
        if approved_runtime != runtime:
            return managed_runtime_required(runtime, tools_dir)
        import asyncio

        result = await asyncio.to_thread(
            install_runtime,
            runtime,
            tools_dir=tools_dir,
            mirrors=self._runtime_mirrors(),
        )
        if not result.get("ok"):
            return {
                **result,
                "blocked": True,
                "original_tool": name,
                "suggested_fix": "Open Settings → Sandbox runtimes, review diagnostics, then repair and retry.",
            }
        return {"ok": True, "runtime_install": result}

    async def _dispatch_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        user_approved: bool = False,
        approved_runtime: str = "",
    ) -> Any:
        """Dispatch a tool, injecting confirm_label after user approval when required.

        Tools registered with ``completion=background`` return an immediate
        ``job_ref`` receipt (Loom22 §16.7) unless the caller passes
        ``wait=true`` / ``_await_inline=true`` for diagnostic sync runs.
        """
        import asyncio

        from .background_jobs import BackgroundJobStore
        from .streaming import (
            _assistant_debug_log,
            prepare_tool_arguments_for_dispatch,
            tool_arguments_error,
        )

        arguments_error = tool_arguments_error(arguments)
        if arguments_error:
            _assistant_debug_log(
                f"[agent-debug] event=invalid_tool_arguments name={name} reason={arguments_error}"
            )
            return {
                "ok": False,
                "error_code": "invalid_arguments",
                "error": f"tool call arguments could not be parsed: {arguments_error}",
                "retryable": True,
                "suggested_fix": (
                    "Re-issue this tool call with valid JSON arguments. Escape backslashes "
                    "(C:\\\\Users) and encode newlines as \\n inside JSON strings."
                ),
            }
        args = prepare_tool_arguments_for_dispatch(
            name,
            arguments,
            user_approved=user_approved,
            session_auto_approved=self._tool_auto_approved(name, arguments),
        )
        runtime_gate = await self._ensure_runtime_for_dispatch(
            name,
            args,
            approved_runtime=approved_runtime,
        )
        if runtime_gate is not None and not runtime_gate.get("ok"):
            return runtime_gate
        runtime_install = (
            runtime_gate.get("runtime_install")
            if isinstance(runtime_gate, dict)
            else None
        )
        tool = self.registry.get(name)
        wait_inline = False
        if isinstance(args, dict):
            wait_inline = bool(args.pop("wait", False) or args.pop("_await_inline", False))
        if (
            tool is not None
            and str(getattr(tool, "completion", "inline") or "inline") == "background"
            and not wait_inline
        ):
            if self._background_jobs is None:
                self._background_jobs = BackgroundJobStore()
            args_copy = dict(args or {})
            registry = self.registry
            turn_id = str(getattr(self, "_current_turn_id", "") or "")

            def _runner() -> Any:
                return asyncio.run(registry.dispatch(name, args_copy))

            receipt = self._background_jobs.start(
                name=name,
                arguments=args_copy,
                runner=_runner,
                turn_id=turn_id,
            )
            _assistant_debug_log(
                f"[agent-debug] event=background_park name={name} "
                f"job_ref={receipt.get('job_ref')} turn={turn_id}"
            )
            if runtime_install:
                receipt["runtime_install"] = runtime_install
            return receipt
        result = await self.registry.dispatch(name, args)
        if runtime_install and isinstance(result, dict):
            result.setdefault("runtime_install", runtime_install)
        return result

    def _mcp_permission_key(self, name: str, arguments: dict[str, Any]) -> str:
        """Return the per-card permission key for MCP and external-agent tools.

        Returns an empty string for ordinary tools — those use the
        coarser per-tool auto-allow path.
        """
        if not isinstance(arguments, dict):
            return ""
        if name == EXTERNAL_AGENT_TOOL:
            # An outside agent's tools cannot enter ``_auto_approve_tools``:
            # that set is filtered against our own registry. The permission set
            # takes free-form keys, so a grant is scoped to one agent and one
            # ACP tool kind rather than to everything the agent might run.
            agent = str(arguments.get("agent") or "").strip()
            kind = str(arguments.get("kind") or "").strip() or "other"
            return f"acp:{agent}:{kind}" if agent else ""
        server_id = str(arguments.get("server_id") or "").strip()
        if name == "mcp_server_call_tool":
            tool = str(arguments.get("tool_name") or "").strip()
            if server_id and tool:
                return f"mcp:{server_id}:{tool}"
        elif name == "mcp_server_read_resource":
            if server_id:
                return f"mcp:{server_id}:resource_read"
        elif name in {"mcp_server_list_tools", "mcp_server_list_resources", "mcp_server_health"}:
            if server_id:
                return f"mcp:{server_id}:{name.removeprefix('mcp_server_')}"
        return ""

    def remember_permission(self, key: str) -> None:
        """Add ``key`` to the session-level permission auto-allow set."""
        text = str(key or "").strip()
        if text:
            self._auto_approve_permissions.add(text)

    def _mcp_server_allows_auto_approve(self, arguments: dict[str, Any]) -> bool:
        """Resolve whether the configured MCP server permits per-card auto-allow.

        The user-side switch lives in ``settings.mcp_servers[*].policy.allow_auto_approve``
        (defaulting to ``False``). Even when the framework would otherwise be
        willing to surface a "本次对话自动允许此 MCP 工具" button — e.g. because
        ``mcp_server_call_tool`` is gated by a fine-grained ``permission_key`` —
        we still honour the per-server preference so the user keeps a single
        authoritative knob.

        Returns ``False`` whenever the server cannot be resolved, so the more
        conservative behaviour (no auto-allow button) wins on uncertainty.
        """
        if not isinstance(arguments, dict):
            return False
        server_id = str(arguments.get("server_id") or "").strip()
        if not server_id:
            return False
        api = getattr(getattr(self, "registry", None), "api", None)
        if api is None:
            return False
        try:
            from argusai.core.settings import normalize_mcp_servers
            servers = normalize_mcp_servers(api.settings_mgr.get().mcp_servers)
        except Exception:
            return False
        for s in servers:
            if str(s.get("id") or "").strip() == server_id:
                policy = s.get("policy") or {}
                return bool(policy.get("allow_auto_approve"))
        return False

    def auto_approve_permissions(self) -> list[str]:
        return sorted(self._auto_approve_permissions)

    def forget_permission(self, key: str) -> None:
        self._auto_approve_permissions.discard(str(key or "").strip())

    def history_payload(self) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.history if m.role in ("user", "assistant")]

    def _mark_latest_user_decision_submitted(self, response: str) -> bool:
        """Persist a user's response onto the latest pending decision block."""
        text = str(response or "").strip()
        if not text:
            return False
        for msg in reversed(self.history):
            if msg.role != "assistant" or not msg.blocks:
                continue
            mutated = False
            blocks = []
            for block in msg.blocks:
                if (
                    isinstance(block, dict)
                    and block.get("type") in {"user_decision", "user_decision_batch"}
                    and not block.get("submitted_response")
                    and not mutated
                ):
                    block = {
                        **block,
                        "submitted_response": text,
                        "submitted_at": time.time(),
                    }
                    mutated = True
                blocks.append(block)
            if mutated:
                msg.blocks = blocks
                return True
        return False

    def _supersede_pending_user_decisions(self, *, keep_latest: bool = False) -> int:
        """Seal stale decision cards so a session has at most one active card."""
        pending: list[tuple[int, int]] = []
        for mi, msg in enumerate(self.history):
            if msg.role != "assistant" or not msg.blocks:
                continue
            for bi, block in enumerate(msg.blocks):
                if (
                    isinstance(block, dict)
                    and block.get("type") in {"user_decision", "user_decision_batch"}
                    and not block.get("submitted_response")
                ):
                    pending.append((mi, bi))

        targets = pending[:-1] if keep_latest else pending
        for mi, bi in targets:
            blocks = list(self.history[mi].blocks or [])
            block = dict(blocks[bi])
            block["submitted_response"] = "已由后续决策替代"
            block["submitted_at"] = time.time()
            block["superseded"] = True
            blocks[bi] = block
            self.history[mi].blocks = blocks
        return len(targets)

    def _repair_submitted_user_decisions(self) -> None:
        """Backfill submitted_response for sessions saved before this field existed."""
        pending: tuple[int, int] | None = None
        for mi, msg in enumerate(self.history):
            if msg.role == "assistant" and msg.blocks:
                for bi, block in enumerate(msg.blocks):
                    if (
                        isinstance(block, dict)
                        and block.get("type") in {"user_decision", "user_decision_batch"}
                        and not block.get("submitted_response")
                    ):
                        pending = (mi, bi)
            elif msg.role == "user" and msg.ui_hidden and pending:
                text = str(msg.content or "").strip()
                if text:
                    pmi, pbi = pending
                    blocks = list(self.history[pmi].blocks or [])
                    block = dict(blocks[pbi])
                    block["submitted_response"] = text
                    block.setdefault("submitted_at", msg.timestamp or time.time())
                    blocks[pbi] = block
                    self.history[pmi].blocks = blocks
                pending = None
            elif msg.role == "user" and not msg.ui_hidden:
                pending = None
        # Older runtimes could append several unanswered cards in one model
        # turn. Only the newest question is actionable after restoration.
        self._supersede_pending_user_decisions(keep_latest=True)

    # -- Long-term memory -----------------------------------------------------





















    # Cues that strongly suggest the reply is a question addressed to the
    # user rather than mid-task thinking. Conservative on purpose: false
    # positives here mean the loop stops when it shouldn't, but that's a
    # cheap mistake (user just types "继续"). False negatives (i.e. the
    # model wanted to ask but we kept going) are the painful case.
    _USER_QUESTION_CUES = (
        "要继续吗", "继续吗？", "要不要继续", "需要继续吗", "是否继续",
        "要现在开始吗", "现在开始吗", "要开始吗", "开始吗？", "是否开始",
        "需要我开始", "需要我执行", "要我执行", "是否执行", "要我现在",
        "你想继续", "你希望继续", "你希望我", "你想让我", "你要我",
        "请告诉我", "请决定", "请确认", "请选择", "你怎么想", "你的意见",
        "等你回复", "等你回应", "等你决定", "等你确认", "等待确认",
        "should i continue", "shall i continue", "do you want me to",
        "would you like me to", "let me know if", "should i proceed",
        "do you want to proceed", "should i start", "shall i start",
        "do you want me to start", "do you want me to run",
        "need me to execute", "please confirm", "please decide",
        "your call", "what would you like", "let me know how",
    )

    # Narrower than ``_USER_QUESTION_CUES``. Only these justify a Start/Cancel
    # card when prose and tool calls arrive together. An open question such as
    # "需要接着做什么？" still drops the unexecuted tools, but it is not a
    # go / no-go and must not mint that card.
    _PROCEED_CONFIRM_CUES = (
        "要继续吗", "继续吗", "要不要继续", "需要继续吗", "是否继续",
        "要现在开始", "现在开始吗", "要开始吗", "开始吗", "是否开始",
        "需要我开始", "需要我执行", "要我执行", "是否执行", "要我现在",
        "你想继续", "你希望继续", "你希望我", "你想让我", "你要我",
        "请确认", "请决定",
        "should i continue", "shall i continue", "do you want me to",
        "would you like me to", "should i proceed", "do you want to proceed",
        "should i start", "shall i start", "do you want me to start",
        "do you want me to run", "need me to execute", "please confirm",
        "please decide",
    )














    # --- Self-loop diagnostics ---------------------------------------------
    #
    # The five helpers below are the ones we tail when chasing "the agent
    # is in an infinite loop" reports. They are deliberately cheap (no
    # heavy serialisation) so leaving them on at INFO level in production
    # is safe.





























    _TODO_STATUS_VALUES = {"pending", "in_progress", "completed", "blocked", "cancelled"}
    _TODO_PATCH_FIELDS = (
        "title", "content", "status", "kind", "phase", "risk", "target_ref", "tool_hint",
        "done_when", "evidence", "parent_id", "trigger", "finding_refs", "evidence_refs",
        "artifact_refs", "blocked_reason", "children",
    )



















    # ------------------------------------------------------------------
    # Tool-result → prompt content
    # ------------------------------------------------------------------
    #
    # Every tool result the model is supposed to see in its next prompt
    # turn goes through ``_tool_result_to_prompt`` first. Without this,
    # a single ``js_fetch_asset`` (200 KB content) or a chain of
    # ``run_python_snippet`` (20 KB stdout each) would bloat the prompt
    # by hundreds of KB per agent round; with N rounds in one turn the
    # prefill cost scales quadratically and the user perceives the
    # assistant "getting slower the longer it works".
    #
    # The strategy is per-field, not flat byte-cap:
    #
    # 1. Walk the top-level dict; keep ``ok / error / status / count``
    #    and other tiny metadata as-is — they're what the model uses
    #    to decide what to do next.
    # 2. For each "fat" string field (content / stdout / stderr /
    #    beautified / body / preview / source / map_text / html …)
    #    keep a head+tail excerpt and append explicit omission metadata.
    #    The tail often contains stack traces, closing JSON fields, or final
    #    response markers that a naïve first-N hard cut would lose.
    # 3. For list fields with many items (matches / strings / files /
    #    modules / hits …) keep the first ``LIST_HEAD`` and append a
    #    short ``"...N more elided"`` marker.
    # 4. Recurse into nested dicts/lists with the same rules.
    # 5. Finally bound the whole JSON dump at ``TOTAL_CAP`` characters.
    #
    # The output is still valid JSON, still tool-result-shaped, so the
    # model doesn't have to learn a new format.

    # Per-field byte budgets (UTF-8 chars). Tuned empirically:
    # - ``content`` of a fetched asset is the worst offender so it's
    #   cut hardest (4 KB still gives ~80 lines of beautified JS).
    # - ``stdout`` / ``stderr`` of a Python snippet keeps 6 KB since
    #   the model often needs to see the actual error.
    # - ``beautified`` of a JS dump is mid-tier; we'd rather the model
    #   call ``js_search`` for specifics than dump the whole file.
    _FAT_STRING_BUDGETS: dict[str, int] = {
        # File-like / source dumps
        "content": 4096,
        "beautified": 4096,
        "source": 4096,
        "code": 4096,
        "map_text": 4096,
        "body": 4096,
        "html": 2048,
        "markdown": 4096,
        "script": 4096,
        # Subprocess / shell output
        "stdout": 6144,
        "stderr": 4096,
        "stdout_excerpt": 6144,
        "stderr_excerpt": 4096,
        # Live-evaluation / probe payloads — these are the worst offenders
        # for browser_* tools, which can dump arbitrary page state.
        "evaluation": 4096,
        "value": 2048,
        "value_preview": 2048,
        "result": 4096,
        "output": 4096,
        "output_text": 4096,
        "response": 4096,
        "data": 4096,
        # Misc / preview-style fields
        "preview": 2048,
        "snippet": 2048,
        "raw": 2048,
        "text": 4096,
        "summary": 4096,
        "description": 2048,
        "details": 4096,
        # Finding / repro / KB bodies
        "evidence": 4096,
        "repro": 4096,
        "reproduce": 4096,
        "notes": 4096,
        "explanation": 4096,
        # Network-ish
        "response_excerpt": 4096,
        "request_body": 2048,
        "post_data": 2048,
    }
    _LIST_HEAD = 30          # keep first N items in long lists
    _TOTAL_CAP = 16384        # final hard ceiling on the JSON dump
    _GENERIC_STRING_CAP = 2048  # any other string we don't recognise
    _GENERIC_DICT_DEPTH = 6   # avoid runaway recursion on cyclic-ish payloads

