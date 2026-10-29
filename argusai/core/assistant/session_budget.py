"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import re
import time
from typing import Any

from .attachments import *
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

log = logging.getLogger("argusai.assistant")


def _utf8_size(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="ignore"))
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8", errors="ignore"))
    except Exception:
        return len(str(value).encode("utf-8", errors="ignore"))


def _message_visible_bytes(message: Any) -> int:
    if isinstance(message, dict):
        content = message.get("content")
        tool_calls = message.get("tool_calls")
        reasoning = message.get("reasoning_content")
    else:
        content = getattr(message, "content", None)
        tool_calls = getattr(message, "tool_calls", None)
        reasoning = getattr(message, "reasoning_content", None)
    return _utf8_size(content) + _utf8_size(reasoning) + _utf8_size(tool_calls)


def estimate_visible_context_bytes(
    history: Any,
    *,
    memory_summary: str = "",
    system_catalog: str = "",
) -> int:
    total = _utf8_size(memory_summary) + _utf8_size(system_catalog)
    if not history:
        return total
    for message in history:
        total += _message_visible_bytes(message)
    return total


def _usage_input_tokens(usage: Any) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    try:
        last_input = max(0, int(usage.get("input_tokens") or 0))
    except (TypeError, ValueError):
        last_input = 0
    try:
        last_output = max(0, int(usage.get("output_tokens") or 0))
    except (TypeError, ValueError):
        last_output = 0
    return last_input, last_output


def combine_context_size(
    *,
    history_bytes: int,
    last_prompt_bytes: int = 0,
    last_prompt_history_bytes: int = 0,
    last_input_tokens: int = 0,
    last_output_tokens: int = 0,
) -> tuple[int, int]:
    """Prefer the provider's last ``prompt_tokens``; only estimate the delta.

    ``bytes // 4`` is a trend heuristic, not a tokenizer. Using it for the
    whole window inflates the ring (UTF-8 JSON / CJK) far above billed
    tokens. Cursor-style fill is: last billed prompt + tokens added after
    that call (the reply and later tool results).
    """
    history_bytes = max(0, int(history_bytes or 0))
    last_prompt_bytes = max(0, int(last_prompt_bytes or 0))
    last_hist = max(0, int(last_prompt_history_bytes or 0))
    last_input = max(0, int(last_input_tokens or 0))
    last_output = max(0, int(last_output_tokens or 0))
    if last_input > 0:
        if last_hist:
            grown = max(0, (history_bytes - last_hist) // 4)
        else:
            grown = last_output
        used = last_input + grown
        return used, max(history_bytes, used * 4)
    if last_prompt_bytes and last_hist:
        used_bytes = history_bytes + max(0, last_prompt_bytes - last_hist)
    else:
        used_bytes = max(history_bytes, last_prompt_bytes)
    return max(0, used_bytes // 4), max(0, used_bytes)


def estimate_context_size_from_mapping(payload: dict[str, Any] | None) -> tuple[int, int]:
    data = payload if isinstance(payload, dict) else {}
    history_bytes = estimate_visible_context_bytes(
        data.get("history"),
        memory_summary=str(data.get("memory_summary") or ""),
        system_catalog=str(data.get("prompt_system_catalog") or ""),
    )
    try:
        last_bytes = max(0, int(data.get("last_prompt_bytes") or 0))
    except (TypeError, ValueError):
        last_bytes = 0
    try:
        last_history = max(0, int(data.get("last_prompt_history_bytes") or 0))
    except (TypeError, ValueError):
        last_history = 0
    last_input, last_output = _usage_input_tokens(data.get("last_token_usage"))
    return combine_context_size(
        history_bytes=history_bytes,
        last_prompt_bytes=last_bytes,
        last_prompt_history_bytes=last_history,
        last_input_tokens=last_input,
        last_output_tokens=last_output,
    )


class SessionBudgetMixin:
    def _summarise_elided_tool_result(self, content: str) -> str:
        """Turn a (possibly already-truncated) tool JSON string into a
        one-line summary suitable for an elided slot.
        """
        original_len = len(content)
        try:
            obj = json.loads(content)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            # Pull the few keys the model usually needs to know "what
            # this call achieved" without seeing the payload.
            hint: dict[str, Any] = {"_elided": True, "_orig_bytes": original_len}
            for k in (
                "ok", "error", "status", "count", "total", "id", "name", "url",
                "target_url", "kind", "case_id", "artifact_ref", "artifact_refs",
                "artifact_title", "evidence_ref", "interaction_ref", "interaction_refs",
                "finding_uid", "job_id", "oracle_id", "task_id",
            ):
                if k in obj:
                    v = obj[k]
                    if isinstance(v, str) and len(v) > 80:
                        v = v[:80] + "..."
                    hint[k] = v
            try:
                s = json.dumps(hint, ensure_ascii=False, default=str)
            except Exception:
                s = json.dumps({"_elided": True, "_orig_bytes": original_len})
        else:
            s = json.dumps({"_elided": True, "_orig_bytes": original_len})
        summary_cap = self._tool_result_summary_cap()
        if summary_cap > 0 and len(s) > summary_cap:
            s = s[: summary_cap - 3] + '..."'
        return s


    def _compact_old_tool_results_in_place(self, messages: list[dict[str, Any]]) -> int:
        """Elide tool-result content for all but the last
        the configured keep-last window of tool messages. Returns the number of
        bytes saved (approximate, before vs after).

        Replaces dicts at the list slot rather than mutating in-place,
        so callers that did ``list(messages)`` see their copy untouched
        but the underlying tool dict is preserved (no aliasing surprises
        on the assistant.tool_calls / tool.tool_call_id pairing).

        Gated behind actual byte pressure (``trigger_bytes``, well below the
        proactive digest-drop / hard-enforce stages that run right after this
        one): eliding purely because the tool-result COUNT crossed
        ``keep_last`` — regardless of whether the prompt is anywhere near
        budget — rewrites an old message's content on almost every
        tool-heavy turn. That busts the provider's byte-exact prompt-cache
        prefix from that position onward for the *whole* request, far more
        often than the elision ever saves meaningful tokens (DeepSeek's
        cache-hit rate is ~12x cheaper than miss, so a stable larger prefix
        usually beats a smaller but constantly-changing one).
        """
        # Collect indices of all ``tool``-role messages in order.
        tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        keep_last = self._tool_result_keep_last()
        if keep_last == 0:
            return 0
        if len(tool_idx) <= keep_last:
            return 0
        trigger_bytes = int(self._context_window_tokens() * 0.45 * 4)
        total, _, _ = self._prompt_size_bytes(messages)
        if total <= trigger_bytes:
            return 0
        # Everything except the tail gets compacted.
        to_compact = tool_idx[: -keep_last]
        saved = 0
        elided = 0
        for i in to_compact:
            m = messages[i]
            content = m.get("content")
            if not isinstance(content, str):
                continue
            # Already elided? Skip — its size won't be reduced further.
            if content.startswith('{"_elided"'):
                continue
            new_content = self._summarise_elided_tool_result(content)
            if len(new_content) >= len(content):
                continue
            saved += len(content) - len(new_content)
            # Replace the dict at this slot with a fresh shallow copy that
            # has the elided content. The original dict is unchanged so
            # other lists (e.g. ``decision_messages = list(messages) + …``)
            # that hold a reference to the same dict still see the full
            # content. Only THIS path-into-the-LLM sees the elided form.
            replacement = dict(m)
            replacement["content"] = new_content
            messages[i] = replacement
            elided += 1
        if saved:
            log.info("AI call: elided %d old tool results, saved ~%d bytes", elided, saved)
        return saved


    def _visible_context_bytes(self) -> int:
        """UTF-8 size of the transcript currently sitting in this session."""
        return estimate_visible_context_bytes(
            getattr(self, "history", None),
            memory_summary=getattr(self, "memory_summary", ""),
            system_catalog=getattr(self, "_prompt_system_catalog", ""),
        )

    def estimate_current_context_size(self) -> tuple[int, int]:
        """Current context fill for the composer ring (Cursor-style)."""
        last_input, last_output = _usage_input_tokens(getattr(self, "last_token_usage", None))
        return combine_context_size(
            history_bytes=self._visible_context_bytes(),
            last_prompt_bytes=int(getattr(self, "last_prompt_bytes", 0) or 0),
            last_prompt_history_bytes=int(getattr(self, "last_prompt_history_bytes", 0) or 0),
            last_input_tokens=last_input,
            last_output_tokens=last_output,
        )

    def _prompt_size_bytes(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> tuple[int, int, int]:
        msg_bytes = 0
        tool_bytes = 0
        for m in messages:
            try:
                msg_bytes += len(json.dumps(m, ensure_ascii=False, default=str).encode("utf-8", errors="ignore"))
            except Exception:
                content = m.get("content")
                if isinstance(content, str):
                    msg_bytes += len(content.encode("utf-8", errors="ignore"))
        if tools:
            try:
                tool_bytes = len(json.dumps(tools, ensure_ascii=False, default=str).encode("utf-8", errors="ignore"))
            except Exception:
                tool_bytes = 0
        return msg_bytes + tool_bytes, msg_bytes, tool_bytes


    def _context_window_tokens(self) -> int:
        api = getattr(self.registry, "api", None)
        try:
            settings = api.settings_mgr.get() if api is not None else None
            endpoints = list(getattr(settings, "ai_endpoints", []) or [])
            endpoints = [ep for ep in endpoints if getattr(ep, "enabled", True)]
            if endpoints:
                endpoints.sort(key=lambda ep: int(getattr(ep, "priority", 0) or 0), reverse=True)
                from argusai.core.ai_model_limits import resolve_endpoint_model_limits

                value = int(resolve_endpoint_model_limits(endpoints[0])[0] or 0)
                if value > 0:
                    return value
        except Exception:
            pass
        try:
            endpoints_fn = getattr(self.ai, "endpoints", None)
            endpoints = endpoints_fn() if callable(endpoints_fn) else []
            if endpoints:
                from argusai.core.ai_model_limits import resolve_endpoint_model_limits

                value = int(resolve_endpoint_model_limits(endpoints[0])[0] or 0)
                if value > 0:
                    return value
        except Exception:
            pass
        try:
            value = int(getattr(getattr(self.ai, "config", None), "context_window", 0) or 0)
            if value > 0:
                return value
        except Exception:
            pass
        return 131072


    def _prompt_budget_bytes(self) -> int:
        # Keep the same policy as the settings recommendation UI: only use
        # ~70% of the model window for input, leaving room for output and
        # provider-specific accounting differences. Token estimate is rough,
        # but good enough as a final preflight guard.
        return max(4096, int(self._context_window_tokens() * 0.70 * 4))


    def _compact_all_tool_results_for_budget(self, messages: list[dict[str, Any]]) -> int:
        saved = 0
        elided = 0
        for i, m in enumerate(messages):
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if not isinstance(content, str) or content.startswith('{"_elided"'):
                continue
            new_content = self._summarise_elided_tool_result(content)
            if len(new_content) >= len(content):
                continue
            replacement = dict(m)
            replacement["content"] = new_content
            messages[i] = replacement
            saved += len(content) - len(new_content)
            elided += 1
        if saved:
            _assistant_debug_log(f"assistant.context_budget_compact_all elided={elided} saved_bytes={saved}")
        return saved


    def _conversation_chunk_ranges(self, messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
        """Return ranges after system, grouped by user turn.

        Dropping a whole user-turn chunk preserves assistant/tool pairing for
        the remaining chunks. The last chunk is the active turn and is never
        dropped by the budgeter.
        """
        chunks: list[tuple[int, int]] = []
        start: int | None = None
        for i in range(1, len(messages)):
            if messages[i].get("role") == "user":
                if start is not None:
                    chunks.append((start, i))
                start = i
            elif start is None:
                start = i
        if start is not None:
            chunks.append((start, len(messages)))
        return chunks


    def _drop_range_in_place(self, messages: list[dict[str, Any]], start: int, end: int) -> None:
        del messages[start:end]

    # -- Session-level auto-compact (Loom22 §12.6) ----------------------------
    #
    # The final budgeter used to *hard-drop* the oldest conversation chunks
    # when over budget — the task intent, intermediate conclusions and the
    # artifact_ref recovery paths in those turns fell out of context entirely
    # (only ``search_memory`` could claw them back, and the model rarely thinks
    # to). Loom22 §12.6 says: summarise *before* dropping, and replace the raw
    # turns with a structured digest so the task stays executable in-context.
    #
    # We do this *deterministically* (no extra LLM call): the budgeter is the
    # synchronous "about to hit the API hard limit" guard on the hot path, and
    # firing a network round-trip from inside it would add latency, a failure
    # mode and recursion risk. A deterministic extract is also idempotent — the
    # same dropped history yields byte-identical digest text — which is exactly
    # what §12.6 asks for so the digest doesn't thrash the §12.5 prompt cache.

    _DIGEST_REF_KEYS = (
        "artifact_ref", "artifact_refs", "case_id", "finding_uid", "evidence_ref",
        "interaction_ref", "interaction_refs", "job_id", "oracle_id", "task_id",
    )
    _DIGEST_BEGIN = "[早期对话已压缩为摘要（完整记录在会话历史中，可用 search_memory / artifact_read 找回）]"
    _DIGEST_END = "[摘要结束]"

    def _collect_chunk_digest(
        self,
        messages: list[dict[str, Any]],
        start: int,
        end: int,
        goals: list[str],
        conclusions: list[str],
        refs: dict[str, None],
    ) -> None:
        """Pull the task-critical gist out of one dropped user-turn chunk."""
        for m in messages[start:end]:
            role = m.get("role")
            content = m.get("content")
            text = content.strip() if isinstance(content, str) else ""
            if role == "user":
                # Skip our own injected continuation/budget nudges and an
                # already-prepended digest — they carry no original intent.
                if text and not text.startswith(self._DIGEST_BEGIN):
                    try:
                        from .engagement import is_greenlight_seed_text

                        if is_greenlight_seed_text(text):
                            continue
                    except Exception:
                        pass
                    goals.append(text[:200])
            elif role == "assistant":
                if text:
                    conclusions.append(text[:160])
            elif role == "tool" and text:
                payload = text
                try:
                    obj = json.loads(payload)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    for k in self._DIGEST_REF_KEYS:
                        if k not in obj:
                            continue
                        v = obj[k]
                        if isinstance(v, (list, tuple)):
                            for item in list(v)[:3]:
                                refs[f"{k}={str(item)[:80]}"] = None
                        elif v not in (None, "", []):
                            refs[f"{k}={str(v)[:80]}"] = None

    def _format_context_digest(
        self,
        goals: list[str],
        conclusions: list[str],
        refs: dict[str, None],
        *,
        cap: int = 3500,
    ) -> str:
        if not (goals or conclusions or refs):
            return ""
        lines = [self._DIGEST_BEGIN]
        if goals:
            lines.append("目标:")
            lines.extend(f"- {g}" for g in goals)
        if conclusions:
            lines.append("进展/结论:")
            lines.extend(f"- {c}" for c in conclusions[-6:])
        if refs:
            lines.append("关键产物:")
            lines.extend(f"- {r}" for r in list(refs)[:20])
        lines.append(self._DIGEST_END)
        block = "\n".join(lines)
        if len(block) > cap:
            block = block[: cap - len(self._DIGEST_END) - 4] + "…\n" + self._DIGEST_END
        return block + "\n\n"

    def _prepend_context_digest_in_place(self, messages: list[dict[str, Any]], digest: str) -> None:
        """Attach the digest to the first surviving non-system message.

        Modifying one message's ``content`` string keeps the surviving message
        sequence (roles, count, tool_use/tool_result pairing) intact, so no
        provider can 400 on alternation/orphaned tool blocks — unlike inserting
        a fresh message between ``system`` and the first user turn.
        """
        for i in range(1, len(messages)):
            m = messages[i]
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                replacement = dict(m)
                replacement["content"] = digest + (m.get("content") or "")
                messages[i] = replacement
                return


    def _proactive_auto_compact_in_place(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> None:
        """Loom22 §12.6 — digest-drop old chunks before the hard budget wall.

        Triggered when estimated prompt size exceeds ``auto_compact_ratio`` of
        the full context window (not the 70% hard budget). ``0`` disables.
        Never rewrites the stable system/kernel prefix.
        """
        ratio = 0.8
        try:
            if hasattr(self, "_auto_compact_ratio"):
                ratio = float(self._auto_compact_ratio())
        except Exception:
            ratio = 0.8
        if ratio <= 0:
            return
        soft_budget = max(4096, int(self._context_window_tokens() * ratio * 4))
        total, _, _ = self._prompt_size_bytes(messages, tools)
        if total <= soft_budget:
            return

        before = total
        digest_goals: list[str] = []
        digest_conclusions: list[str] = []
        digest_refs: dict[str, None] = {}
        dropped = 0
        while total > soft_budget:
            chunks = self._conversation_chunk_ranges(messages)
            if len(chunks) <= 1:
                break
            start, end = chunks[0]
            self._collect_chunk_digest(messages, start, end, digest_goals, digest_conclusions, digest_refs)
            self._drop_range_in_place(messages, start, end)
            dropped += 1
            total, _, _ = self._prompt_size_bytes(messages, tools)
        if dropped:
            digest = self._format_context_digest(digest_goals, digest_conclusions, digest_refs)
            if digest:
                self._prepend_context_digest_in_place(messages, digest)
            _assistant_debug_log(
                f"[agent-debug] event=auto_compact before_bytes={before} after_bytes={total} "
                f"soft_budget={soft_budget} dropped_chunks={dropped} ratio={ratio}"
            )


    def _enforce_prompt_budget_in_place(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> None:
        budget = self._prompt_budget_bytes()
        total, _, _ = self._prompt_size_bytes(messages, tools)
        if total <= budget:
            return

        before = total
        self._compact_all_tool_results_for_budget(messages)
        total, _, _ = self._prompt_size_bytes(messages, tools)

        dropped_chunks = 0
        digest_attached = False
        # Loom22 §12.6: summarise the turns we're about to drop instead of
        # losing them outright. Accumulate across every chunk dropped in this
        # pass, then prepend one digest to the first surviving message.
        digest_goals: list[str] = []
        digest_conclusions: list[str] = []
        digest_refs: dict[str, None] = {}
        while total > budget:
            chunks = self._conversation_chunk_ranges(messages)
            # Keep the active/latest chunk. If only one chunk remains, there is
            # no old conversation left that can be safely removed.
            if len(chunks) <= 1:
                break
            start, end = chunks[0]
            self._collect_chunk_digest(messages, start, end, digest_goals, digest_conclusions, digest_refs)
            self._drop_range_in_place(messages, start, end)
            dropped_chunks += 1
            total, _, _ = self._prompt_size_bytes(messages, tools)

        if dropped_chunks:
            digest = self._format_context_digest(digest_goals, digest_conclusions, digest_refs)
            if digest:
                self._prepend_context_digest_in_place(messages, digest)
                digest_attached = True
                total, _, _ = self._prompt_size_bytes(messages, tools)

        # Loom22 §12.5: never truncate the stable system/kernel prefix. If still
        # over budget after digest-drop of old conversation chunks, keep the
        # oversize prompt and log — rewriting messages[0] would bust provider
        # prompt caches every turn.
        if total > budget:
            _assistant_debug_log(
                "assistant.context_budget_still_over "
                f"before_bytes={before} after_bytes={total} budget_bytes={budget} "
                f"messages={len(messages)} dropped_chunks={dropped_chunks} "
                f"digest_attached={digest_attached} system_truncated=false"
            )
        else:
            _assistant_debug_log(
                "assistant.context_budget_enforced "
                f"before_bytes={before} after_bytes={total} budget_bytes={budget} "
                f"dropped_chunks={dropped_chunks} auto_compact={int(digest_attached)}"
            )


    async def _call_ai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        # Compact old tool results before measuring / sending. This
        # mutates ``messages`` in place — that's fine because each
        # ``_call_ai`` invocation gets a freshly-built list from the
        # agent loop, and history is the canonical store, not this list.
        self._patch_message_tool_adjacency_in_place(messages)
        self._compact_old_tool_results_in_place(messages)
        self._proactive_auto_compact_in_place(messages, tools)
        self._enforce_prompt_budget_in_place(messages, tools)
        self._patch_message_tool_adjacency_in_place(messages)
        # Stamp session thinking intensity onto every cached provider so
        # failover endpoints created mid-turn inherit the same effort.
        try:
            budget = int(self._thinking_budget_tokens()) if hasattr(self, "_thinking_budget_tokens") else 1024
            effort = self._thinking_effort() if hasattr(self, "_thinking_effort") else "high"
            ai = getattr(self, "ai", None)
            if ai is not None:
                setattr(ai, "thinking_budget_tokens", budget)
                setattr(ai, "thinking_effort", effort)
            cfg = getattr(ai, "config", None)
            if cfg is not None:
                cfg.thinking_budget_tokens = budget
                cfg.thinking_effort = effort
            cache = getattr(ai, "_provider_cache", None)
            if isinstance(cache, dict):
                for provider in cache.values():
                    pcfg = getattr(provider, "config", None)
                    if pcfg is None:
                        continue
                    pcfg.thinking_budget_tokens = budget
                    pcfg.thinking_effort = effort
        except Exception:
            pass
        # Track prompt size every call. The model's prefill cost is roughly
        # linear in input bytes, so a single 500 KB request will dominate
        # the user-visible latency. Logging this lets us correlate "AI feels
        # slow" with "we sent it 380 KB".
        try:
            msg_bytes = 0
            tool_bytes = 0
            for m in messages:
                c = m.get("content")
                if isinstance(c, str):
                    msg_bytes += len(c.encode("utf-8", errors="ignore"))
                # Also account for tool_calls payloads on assistant turns.
                tc_list = m.get("tool_calls")
                if isinstance(tc_list, list):
                    try:
                        msg_bytes += len(json.dumps(tc_list, ensure_ascii=False, default=str).encode("utf-8", errors="ignore"))
                    except Exception:
                        pass
            if tools:
                try:
                    tool_bytes = len(json.dumps(tools, ensure_ascii=False, default=str).encode("utf-8", errors="ignore"))
                except Exception:
                    tool_bytes = 0
            total_bytes = msg_bytes + tool_bytes
            est_tokens = total_bytes // 4  # very rough — fine for trend monitoring
            log.info(
                "AI call: msgs=%d, msg_bytes=%d, tool_schema_bytes=%d, total=%d (≈%d tokens)",
                len(messages), msg_bytes, tool_bytes, total_bytes, est_tokens,
            )
            # Stash on the session so the UI / metrics layer can read it.
            self.last_prompt_bytes = total_bytes
            self.last_prompt_tokens_est = est_tokens
            self.last_prompt_history_bytes = self._visible_context_bytes()
        except Exception:
            pass
        # Reset the partial buffer at the start of EVERY AI round. This is
        # what ``_commit_cancelled_assistant`` reads when the user hits
        # stop while text is still streaming in — without it, mid-stream
        # cancels silently drop the half-message the user already saw on
        # screen (and we'd have a "messages disappeared after stopping"
        # bug, which is exactly what we used to have).
        self._last_partial_text = ""
        if hasattr(self.ai, "chat"):
            wrapped_on_delta: Callable[[str], Awaitable[None] | None] | None = None
            stream_filter: _PseudoTagStreamFilter | None = None
            repetition_guard = _StreamRepetitionGuard()
            stream_state: dict[str, Any] = {
                "truncated": False,
                "hard_stop": False,
                "last_warn_log": 0,
                "repeats_at_stop": 0,
                "block_preview": "",
            }
            self._stream_truncated_this_round = False
            if on_delta and hasattr(self.ai, "stream_chat"):
                stream_filter = _PseudoTagStreamFilter()

                async def _filtered_delta(delta: str) -> None:
                    cleaned = stream_filter.feed(delta) if stream_filter else delta
                    if not cleaned:
                        return
                    if stream_state["hard_stop"]:
                        # We already decided this stream is a runaway
                        # loop; silently drop everything that follows
                        # so the UI / history doesn't accumulate more
                        # garbage.
                        return
                    verdict = repetition_guard.feed(cleaned)
                    state = verdict.get("state", "ok")
                    repeats = int(verdict.get("repeats") or 0)
                    block_preview = str(verdict.get("block_preview") or "")
                    if state == "warn":
                        if repeats != stream_state["last_warn_log"]:
                            stream_state["last_warn_log"] = repeats
                            log.warning(
                                "assistant.stream_repetition_warn repeats=%d block_preview=%r turn=%s round=%d",
                                repeats,
                                block_preview,
                                self._current_turn_id,
                                self._current_round_index,
                            )
                    elif state == "truncate":
                        if not stream_state["truncated"]:
                            stream_state["truncated"] = True
                            stream_state["repeats_at_stop"] = repeats
                            stream_state["block_preview"] = block_preview
                            log.warning(
                                "assistant.stream_repetition_truncate repeats=%d block_preview=%r turn=%s round=%d",
                                repeats,
                                block_preview,
                                self._current_turn_id,
                                self._current_round_index,
                            )
                            notice = str(verdict.get("notice") or "")
                            if notice:
                                self._last_partial_text += notice
                                try:
                                    res = on_delta(notice)
                                    if inspect.isawaitable(res):
                                        await res
                                except Exception:
                                    pass
                        return
                    elif state == "stop":
                        if not stream_state["hard_stop"]:
                            stream_state["hard_stop"] = True
                            stream_state["repeats_at_stop"] = repeats
                            stream_state["block_preview"] = block_preview
                            self._stream_truncated_this_round = True
                            log.warning(
                                "assistant.stream_repetition_stop repeats=%d block_preview=%r turn=%s round=%d",
                                repeats,
                                block_preview,
                                self._current_turn_id,
                                self._current_round_index,
                            )
                            stop_note = (
                                "\n\n[模型流式输出复读次数过多，已强制中止本轮响应]\n"
                            )
                            self._last_partial_text += stop_note
                            try:
                                res = on_delta(stop_note)
                                if inspect.isawaitable(res):
                                    await res
                            except Exception:
                                pass
                        return
                    forward_text = str(verdict.get("forward_text") or "")
                    if not forward_text:
                        return
                    self._last_partial_text += forward_text
                    res = on_delta(forward_text)
                    if inspect.isawaitable(res):
                        await res

                wrapped_on_delta = _filtered_delta

            # Live "正在推理…" heartbeat. Reasoning models (DeepSeek V4 /
            # Anthropic) stream their thinking on a separate channel that emits
            # NO visible text, so the bubble would otherwise sit blank until the
            # answer finally starts — exactly the "气泡回复完才出现" symptom.
            # We surface progress through the same status row the agent loop
            # already uses (no new UI), throttled so we don't spam events.
            reasoning_hb = {"chars": 0, "started": 0.0, "last_emit": 0.0}
            _reasoning_status_cb = getattr(self, "_on_status", None)

            async def _report_reasoning(chunk: str) -> None:
                if not chunk or not _reasoning_status_cb:
                    return
                import time as _t
                hb = reasoning_hb
                if hb["started"] == 0.0:
                    hb["started"] = _t.monotonic()
                hb["chars"] += len(chunk)
                now = _t.monotonic()
                if hb["last_emit"] and now - hb["last_emit"] < 0.6:
                    return
                hb["last_emit"] = now
                elapsed = int(now - hb["started"])
                await self._emit_status(
                    _reasoning_status_cb,
                    f"正在推理…（已生成 {hb['chars']} 字思考 · {elapsed}s）",
                )

            if wrapped_on_delta is not None:
                raw = await self.ai.stream_chat(
                    messages=messages,
                    tools=tools if tools else None,
                    on_delta=wrapped_on_delta,
                    on_reasoning=_report_reasoning,
                    thinking_enabled=thinking_enabled,
                )
                if stream_filter is not None:
                    tail = stream_filter.flush()
                    if tail and on_delta and not stream_state["hard_stop"]:
                        verdict = repetition_guard.feed(tail)
                        forward_text = str(verdict.get("forward_text") or "")
                        if forward_text:
                            self._last_partial_text += forward_text
                            res = on_delta(forward_text)
                            if inspect.isawaitable(res):
                                await res
                if stream_state["truncated"] or stream_state["hard_stop"]:
                    self._stream_truncated_this_round = True
            else:
                raw = await self.ai.chat(
                    messages=messages,
                    tools=tools if tools else None,
                    thinking_enabled=thinking_enabled,
                )
            self._record_token_usage(raw.get("_cache_usage") if isinstance(raw, dict) else None)
            choices = raw.get("choices", [])
            if choices:
                msg = choices[0].get("message", {})
                raw_content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls and raw_content:
                    recovered = _recover_dsml_tool_calls(raw_content)
                    if recovered:
                        log.info(
                            "recovered %d tool_calls from leaked DSML content",
                            len(recovered),
                        )
                        tool_calls = recovered
                stripped_content = _strip_pseudo_tool_calls(raw_content)
                cleaned_content, dedup_dropped, dedup_mode = _collapse_repeated_response_text_verbose(stripped_content)
                if dedup_dropped:
                    tool_names = [str(tc.get("function", {}).get("name") or tc.get("name") or "?") for tc in tool_calls]
                    log.warning(
                        "assistant.response_dedup mode=%s dropped=%d original_len=%d final_len=%d tool_call_count=%d tools=%s",
                        dedup_mode,
                        dedup_dropped,
                        len(stripped_content),
                        len(cleaned_content),
                        len(tool_calls),
                        ",".join(tool_names[:5]),
                    )
                self._record_round_signature(cleaned_content, tool_calls)
                return {
                    "role": msg.get("role", "assistant"),
                    "content": cleaned_content,
                    "tool_calls": tool_calls,
                    "reasoning_content": msg.get("reasoning_content") or "",
                    "provider_content": msg.get("provider_content") or [],
                }
            return {"role": "assistant", "content": ""}

        user_content = "\n".join(m["content"] for m in messages if m["role"] == "user")
        active_names = _select_tool_names(
            "assistant",
            user_content,
            allow_web_search=bool(getattr(self, "allow_web_search", True)),
        )
        active_names = apply_project_workspace_tools(self, active_names)
        active_catalog = self.registry.catalog(active_names)
        text = await self.ai.analyze(
            user_content,
            system=_build_system_prompt(
                active_catalog,
                "",
                allow_web_search=bool(getattr(self, "allow_web_search", True)),
                agent_self_decide=bool(
                    getattr(self, "_should_agent_self_decide", lambda: False)()
                ),
            ),
        )
        return {"role": "assistant", "content": text}


    def _commit_cancelled_assistant(
        self,
        partial_text: str,
        tool_calls_made: list[dict[str, Any]] | None,
        *,
        reason: str = "user_cancelled",
    ) -> ChatResponse:
        """Persist whatever the assistant produced before the user hit stop.

        Without this, ``cancel_assistant_run`` orphans the user turn:

            history: [user("帮我..."), <nothing>]

        Next time the session is loaded, the user sees their own question
        with no reply at all — even if the AI had already streamed several
        paragraphs to the screen. That's the "messages disappeared after
        stopping" bug. This helper closes the loop:

        1. Pull the cleaned stream buffer captured by ``_call_ai`` —
           that's exactly what the UI showed via ``on_delta``.
        2. Pull any tool traces already executed this turn so the
           transcript records them.
        3. Append ONE assistant ``ChatMessage`` carrying both, prefixed
           with a ``[已中止]`` marker so the user (and the model on next
           load) can tell this turn was interrupted.

        Returns a ready-to-return ``ChatResponse`` carrying the same
        blocks so the cancel return point can ``return _commit_...()``
        in one line.
        """
        import time as _time

        text = (partial_text or "").rstrip()
        traces = list(tool_calls_made or [])
        # Stitch a user-visible marker. ALWAYS the same marker — that's
        # the *signal* the UI uses to render a "stopped" badge on this
        # bubble, regardless of whether anything streamed or any tool
        # ran. Without a consistent marker the front-end can't tell a
        # legit empty reply apart from a torn-off stream.
        marker = "[已中止] "
        if text:
            body = marker + text
        elif traces:
            # The executed tools already render as structured tool-trace cards
            # (blocks below); don't also dump their names as a comma string.
            body = f"{marker}已中止，本轮已执行 {len(traces)} 个工具操作。"
        else:
            body = marker + "已取消当前助手运行。"

        blocks: list[dict[str, Any]] = []
        if traces:
            blocks = self._blocks_for_tool_trace(traces) or []
        if text:
            blocks = blocks + (self._blocks_for_text(body) or [])
        elif not blocks:
            blocks = self._blocks_for_text(body) or []

        if any(
            isinstance(block, dict)
            and block.get("type") in {"user_decision", "user_decision_batch"}
            for block in blocks
        ):
            self._supersede_pending_user_decisions()

        ts = _time.time()
        self.history.append(ChatMessage(
            role="assistant",
            content=body,
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=ts,
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=body,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="cancelled",
        )


    def _commit_user_decision_stop(
        self,
        partial_text: str,
        tool_calls_made: list[dict[str, Any]] | None,
        tc_list: list[dict[str, Any]] | None,
        messages: list[dict[str, Any]] | None,
    ) -> ChatResponse:
        """Hard-stop the current turn because the model called ``stop_for_user_decision``.

        Historically ``stop_for_user_decision`` only flipped an
        in-memory flag (``_user_decision_requested``) which was
        checked by ``_should_auto_continue`` — i.e. only *after* the
        model produced a tool-call-FREE textual reply. If the same
        agent turn issued ``stop_for_user_decision`` together with
        other tool calls (which the model loves to do — "I'll just
        finish this read_text_file first"), the system happily
        executed the remaining tools, pushed their results back into
        the conversation, re-prompted the model, and the "stop" had
        zero practical effect.

        Worst case: dozens of follow-up rounds piled on top of an
        unanswered user decision, and when the model finally emitted
        a text-only reply the auto-continue was suppressed *too
        late* — by then the model had moved on to a completely
        different topic, the original decision question was lost in
        the transcript, and the user reported "对话丢失".

        This helper makes it a HARD stop:

        1. Mark every tool_call in ``tc_list`` that doesn't already
           have a recorded result as cancelled tombstones — this
           keeps the on-wire ``tool_use`` / ``tool_result`` pairing
           well-formed for Anthropic-style providers on the next
           turn.
        2. Append a single assistant ``ChatMessage`` summarising
           what ran before the stop, prefixed with ``[等待用户决定]``
           so both the UI and the model (on next load) can see
           exactly where the turn paused.
        3. Return a ``ChatResponse`` with ``status="awaiting_user_decision"``
           which the frontend already knows how to render as a
           prominent decision card (it was the original intent of
           the tool — only the enforcement was missing).
        """
        import time as _time

        # Unattended cron / child Agents must never hard-stop on a decision card.
        if self._is_unattended_runtime():
            self._user_decision_requested = False
            self._todos_plan_approved = True
            if tc_list and messages is not None:
                try:
                    self._fill_missing_tool_results(
                        tc_list, messages, reason="unattended_auto_continued"
                    )
                except Exception as e:
                    log.warning("fill_missing_tool_results in unattended auto-continue failed: %s", e)
            label = getattr(self, "_unattended_source_label", lambda: "Auto-approve")()
            text = (partial_text or "").rstrip() or f"{label} auto-continued past user decision."
            traces = list(tool_calls_made or [])
            return ChatResponse(
                text=text,
                tool_calls=traces,
                blocks=self._blocks_for_text(text) or [],
                tool_trace=traces,
                status="ok",
            )

        if tc_list and messages is not None:
            try:
                self._fill_missing_tool_results(
                    tc_list, messages, reason="awaiting_user_decision"
                )
            except Exception as e:
                log.warning("fill_missing_tool_results in user_decision_stop failed: %s", e)

        text = (partial_text or "").rstrip()
        traces = list(tool_calls_made or [])
        marker = "[等待用户决定] "
        if text:
            body = marker + text
        elif traces:
            # Tools render as structured cards below; keep the text minimal.
            body = f"{marker}已执行 {len(traces)} 个工具操作，等待你的选择。"
        else:
            body = marker + "已暂停，等待你的选择。"

        blocks: list[dict[str, Any]] = []
        if traces:
            blocks = self._blocks_for_tool_trace(traces) or []
        # Loom22 §9.6: card carries a short question/options; do not paste the
        # essay into the card. The essay stays in ``ChatMessage.content``
        # (after the ``[等待用户决定]`` marker). The UI must strip the marker
        # and still render that prose — hiding the whole body is how users
        # saw a decision card that referred to "these 10 titles" with no list.
        sanitized: list[dict[str, Any]] = []
        has_decision = False
        for block in blocks:
            if (block or {}).get("type") in {"user_decision", "user_decision_batch"}:
                has_decision = True
                if (block or {}).get("type") == "user_decision":
                    sanitized.append(self._sanitize_user_decision_block(block))
                else:
                    batch = dict(block)
                    batch["context"] = str(batch.get("context") or "")[:480]
                    qs = []
                    for q in list(batch.get("questions") or [])[:8]:
                        if isinstance(q, dict):
                            qq = dict(q)
                            qtext = str(qq.get("question") or "")
                            if len(qtext) > 240:
                                qq["question"] = self._extract_decision_card_question(qtext, max_chars=240)
                            qq["context"] = str(qq.get("context") or "")[:300]
                            qs.append(qq)
                    batch["questions"] = qs
                    sanitized.append(batch)
            else:
                sanitized.append(block)
        blocks = sanitized
        if not has_decision:
            # No structured card from the tool — synthesize a short one.
            card_q = self._extract_decision_card_question(text or body, max_chars=320)
            blocks.append(self._sanitize_user_decision_block({
                "type": "user_decision",
                "question": card_q,
                "options": ["继续", "取消"],
                "context": "等待你的选择后再继续。",
            }))
            has_decision = True
        if not has_decision and not blocks:
            blocks = self._blocks_for_text(body) or []

        ts = _time.time()
        self.history.append(ChatMessage(
            role="assistant",
            content=body,
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=ts,
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=body,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="awaiting_user_decision",
        )


    def _fill_missing_tool_results(
        self,
        tc_list: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        *,
        reason: str = "interrupted",
    ) -> None:
        """Make sure every tool_call in ``tc_list`` has a matching tool result.

        Called from cancellation / exception paths so the conversation history
        never ends up with an orphan ``tool_use`` block, which would later
        cause Anthropic-style providers to return HTTP 400 on the next turn.
        Idempotent — only inserts placeholders for IDs that aren't already
        accounted for in ``messages`` (and equivalently in ``self.history``).
        """
        if not tc_list:
            return
        import time as _time

        # Find which tool_call_ids already have a matching role=tool entry.
        seen_ids: set[str] = set()
        for m in messages:
            if m.get("role") == "tool":
                tid = m.get("tool_call_id") or ""
                if tid:
                    seen_ids.add(tid)

        placeholder_payload = json.dumps(
            {"error": "interrupted", "reason": reason},
            ensure_ascii=False,
        )
        now = _time.time()
        for tc in tc_list:
            tid = tc.get("id") or ""
            if not tid or tid in seen_ids:
                continue
            messages.append({"role": "tool", "tool_call_id": tid, "content": placeholder_payload})
            self.history.append(ChatMessage(
                role="tool",
                content=placeholder_payload,
                tool_call_id=tid,
                timestamp=now,
            ))
            seen_ids.add(tid)


    def _rewind_for_regenerate(
        self,
        message: str,
        before_ts: float | None,
        user_before_ts: float | None = None,
    ) -> None:
        """Replace a precise user turn, or use the legacy assistant cutoff."""
        if user_before_ts:
            target_idx = -1
            target = float(user_before_ts)
            for idx, item in enumerate(self.history):
                if (
                    item.role == "user"
                    and not item.hidden
                    and item.timestamp
                    and abs(float(item.timestamp) - target) <= 0.001
                ):
                    target_idx = idx
                    break
            if target_idx < 0:
                raise ValueError("regenerate target user message was not found")

            original = self.history[target_idx]
            self.history = [
                *self.history[:target_idx],
                ChatMessage(
                    role="user",
                    content=message,
                    attachments=[dict(item) for item in original.attachments],
                    voice=dict(original.voice) if isinstance(original.voice, dict) else None,
                    selection_reference=(
                        dict(original.selection_reference)
                        if isinstance(original.selection_reference, dict)
                        else None
                    ),
                    ui_hidden=original.ui_hidden,
                    source=str(getattr(original, "source", "") or ""),
                    source_ref=str(getattr(original, "source_ref", "") or ""),
                    timestamp=original.timestamp,
                ),
            ]
            self._pending = None
            return

        if before_ts:
            for idx, item in enumerate(self.history):
                if item.timestamp and item.timestamp >= before_ts and item.role == "assistant":
                    self.history = self.history[:idx]
                    self._pending = None
                    return

        for idx in range(len(self.history) - 1, -1, -1):
            item = self.history[idx]
            if item.role == "user" and not item.hidden and item.content.strip() == message.strip():
                self.history = self.history[:idx + 1]
                self._pending = None
                return

        import time as _time
        self.history.append(ChatMessage(role="user", content=message, timestamp=_time.time()))


    def _append_pending_assistant_once(self, assistant_response: dict[str, Any], tool_call_id: str) -> None:
        for item in reversed(self.history[-8:]):
            if item.role != "assistant" or not item.tool_calls:
                continue
            if any(tc.get("id") == tool_call_id for tc in item.tool_calls):
                return
        import time as _time
        self.history.append(ChatMessage(
            role="assistant",
            content=assistant_response.get("content") or "",
            tool_calls=assistant_response.get("tool_calls") or [],
            reasoning_content=assistant_response.get("reasoning_content") or "",
            provider_content=list(assistant_response.get("provider_content") or []),
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))


