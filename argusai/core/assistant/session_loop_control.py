"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .attachments import *
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

log = logging.getLogger("argusai.assistant")

# Agent-loop stall detection (Loom22 §10 + §11).
#
# Loom22 §10 does not use user-configured round/tool-call counts as task
# completion signals. Durable terminal/wait states, the wall-clock safety
# boundary and the progress guards below decide when a turn stops. §11's
# duplicate-skip is purely a "防抖 + 让模型自救" mechanism: skip the call,
# return ``ok=True, skipped=True``, inject a hint so the model re-reads
# earlier results.
#
# Beyond those two, we keep a single safety net for genuine deadlock:
# when the assistant emits the *exact same round* (same cleaned text
# AND same set of (tool_name, args_hash)) this many rounds in a row,
# we display a user_decision card. This is strictly stricter than the
# old "content fingerprint repeated 3 times" rule — it never fires on
# "model is silently making progress with new tool args".
#
# The threshold is an internal framework invariant. Legacy settings still
# deserialize so old settings.json files remain valid, but no longer tune this
# safety boundary.
_DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT = 4
# Ring buffer cap leaves room for the hard-stop and the result-aware guards.
_ROUND_SIGNATURE_BUFFER_LIMIT = 16

# Result-aware loop detector. The round-signature breaker
# above catches ``generic_repeat`` (byte-identical rounds). These two cover
# the patterns it structurally cannot see:
#
#   poll_no_progress — same tool returning byte-identical RESULTS N times in
#       a row. Survives the round-signature breaker because the model often
#       interleaves fresh reasoning text or because the tool is a polling
#       tool exempt from §11 dedup. Hashing the *result* (not the call) is
#       what makes "no progress" observable.
#   ping_pong — the model alternates between exactly two (tool,args) states
#       A-B-A-B…; each round differs so the round-signature set never
#       collapses to size 1, yet no progress is being made.
#
# Both are soft-first: at the warn threshold we inject ONE hidden guidance
# hint (like the §11 dedup hint); only at 2x do we escalate to a
# user_decision card, so a legitimate long poll isn't killed outright.
_DEFAULT_POLL_NO_PROGRESS_COUNT = 4
_DEFAULT_PING_PONG_COUNT = 6
_TOOL_OUTCOME_BUFFER_LIMIT = 24

# Taxonomy-driven guards (Loom22 §11.2). The result-aware detectors above all
# key off "no progress / byte-identical", which structurally miss two loops:
#
#   same_tool_failure — the same tool FAILS N times in a row while varying its
#       args or error each time. Result differs and the round signature differs,
#       so generic_repeat / poll_no_progress / ping_pong are all blind, yet it
#       is plainly "hitting the same wall". Needs only the ``ok`` flag + name.
#   mutating_repetition — a ``mutating`` tool SUCCEEDS with the identical call
#       signature ≥ N times (re-applying the same patch, re-creating the same
#       job). Its success payload usually carries a fresh id/timestamp, so the
#       result hash exempts it; only counting by signature + kind catches it.
#
# Same soft-first contract as §11.1: a one-shot hidden hint at the warn
# threshold, escalate to a user_decision card only at 2x, ``0`` = disabled.
_DEFAULT_SAME_TOOL_FAILURE_COUNT = 3
_DEFAULT_MUTATING_REPETITION_COUNT = 3
_SAME_TOOL_FAILURE_HINT_MESSAGE = (
    "[系统] 检测到你连续 {count} 次调用 `{tool}` 都失败了（即使参数/报错略有不同），"
    "一直在撞同一堵墙。请先读懂失败原因、换一种方法或补齐前置条件，"
    "不要用几乎相同的方式反复重试；若无法继续，请向用户说明卡点。"
)
_MUTATING_REPETITION_HINT_MESSAGE = (
    "[系统] 检测到你用完全相同的参数成功调用了有副作用的 `{tool}` {count} 次，"
    "这很可能是在重复做同一件事（如反复打同一个补丁/建同一个任务），并未推进。"
    "请确认上一次是否已生效并基于其结果继续，不要再重复同一次变更操作。"
)
_POLL_NO_PROGRESS_HINT_MESSAGE = (
    "[系统] 检测到你连续 {count} 次调用 `{tool}` 得到完全相同的结果，没有任何进展。"
    "如果在等待某个长任务完成，请改用更长的等待间隔或先推进其他步骤，不要忙等轮询；"
    "若状态已不会再变，请基于现有结果继续推进或向用户说明，不要再重复同一调用。"
)
_PING_PONG_HINT_MESSAGE = (
    "[系统] 检测到你在 `{a}` 与 `{b}` 两个调用之间反复横跳（A-B-A-B），没有推进。"
    "请停下来重读这两个结果，换一种策略或工具，而不是继续交替调用。"
)
_DUPLICATE_SKIP_HINT_MESSAGE = (
    "[系统] 运行时检测到与本回合早些时候完全相同的工具调用，已跳过执行（这不是工具失败）。"
    "先前结果仍保留在工具轨迹中——请回看 earlier trace，换用不同参数"
    "（如 fresh_context、force、不同 url/expression）或其他工具；若确实卡住，"
    "请向用户说明阻塞原因，不要继续重复同一调用。"
)


def _tool_outcome_ok(result: Any) -> bool:
    """Whether a tool result counts as a success for failure-run detection.

    A dict carrying ``error`` or an explicit ``ok is False`` is a failure;
    everything else (including non-dict results) is treated as success.
    """
    if isinstance(result, dict):
        if result.get("error") or result.get("ok") is False:
            return False
    return True


class SessionLoopControlMixin:
    def _unfinished_todos(self) -> list[dict[str, Any]]:
        """Return todos whose status implies the assistant still has work to do."""
        unfinished_states = {"pending", "in_progress"}
        return [t for t in self._flatten_todos(self.todos) if (t.get("status") or "pending") in unfinished_states]


    def _has_unfinished_todos(self) -> bool:
        return bool(self._unfinished_todos())

    # -- Child-Agent completion delivery -----------------------------------

    def _child_agent_manager(self) -> Any:
        """Return the child-Agent manager only when this session already owns one.

        Deliberately never constructs one: building a manager touches the
        sessions directory, and a session that never delegated must stay free
        of that cost (and of stray ``_agents/`` folders).
        """
        return getattr(self, "_agent_thread_manager", None)

    def _pending_child_agent_notice(self) -> tuple[str, list[str]]:
        """Build this turn's child-completion note plus the ids it covers."""
        reader = getattr(self._child_agent_manager(), "pending_parent_notices", None)
        if not callable(reader):
            return "", []
        try:
            notices = list(reader() or [])
        except Exception:
            log.debug("reading child-Agent completion notices failed", exc_info=True)
            return "", []
        if not notices:
            return "", []
        from .agent_threads import format_parent_agent_notice

        agent_ids = [str(item.get("agent_id") or "") for item in notices if item.get("agent_id")]
        return format_parent_agent_notice(notices), agent_ids

    def _flush_child_agent_notice_delivery(self) -> None:
        """Retire notices once the provider has actually seen them.

        ``_pending_child_agent_notice_ids`` is filled when the synthetic
        message is appended to this turn's ``messages`` and drained here, right
        after the first successful provider round. That ordering is what makes
        delivery exactly-once without dropping the news when a round errors.
        """
        agent_ids = list(getattr(self, "_pending_child_agent_notice_ids", None) or [])
        if not agent_ids:
            return
        self._pending_child_agent_notice_ids = []
        marker = getattr(self._child_agent_manager(), "mark_parent_notices_reported", None)
        if not callable(marker):
            return
        try:
            marker(agent_ids)
        except Exception:
            log.debug("marking child-Agent notices reported failed", exc_info=True)

    def _active_child_agent_count(self) -> int:
        counter = getattr(self._child_agent_manager(), "active_agent_count", None)
        if not callable(counter):
            return 0
        try:
            return max(0, int(counter()))
        except Exception:
            log.debug("counting active child Agents failed", exc_info=True)
            return 0

    def _append_active_child_agent_note(self, text: str) -> str:
        """Keep a finishing turn honest while children are still working.

        Deliberately non-blocking: the turn still ends now, the note only stops
        the summary from reading as if everything were done. The real results
        arrive through the child-completion notice on a later turn.
        """
        count = self._active_child_agent_count()
        if count <= 0:
            return text
        note = (
            f"\n\n（注：本会话仍有 {count} 个子 Agent 在运行，"
            "上面的结论还不包含它们的结果；它们结束后我会再向你汇报。）"
        )
        body = str(text or "").rstrip()
        return body + note if body else note.strip()

    def _max_acceptance_repair_attempts(self) -> int:
        return self._assistant_setting_int(
            "assistant_max_acceptance_repair_attempts",
            2,
            0,
            10,
        )

    def _acceptance_needed(self) -> bool:
        """True when this turn should run §8.5 verification before done."""
        from .acceptance import unfinished_validation_todos, verify_validation_plan

        flat = self._flatten_todos(self.todos)
        if unfinished_validation_todos(flat):
            return True
        report = verify_validation_plan(flat)
        return bool(report.get("checked") and not report.get("passed"))

    async def _run_acceptance_closed_loop(
        self,
        *,
        result: str,
        response: dict[str, Any],
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
        ts: float,
        on_status: Callable[[str], Awaitable[None] | None] | None = None,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any] | None:
        """Gate a toolless finish through deterministic validation (Loom22 §8.5).

        Returns:
          * ``None`` — caller may commit ``done``
          * ``{"continue": True}`` — repair prompt injected; caller should ``continue``
          * ``{"response": ChatResponse}`` — stop with decision / failed
        """
        from .acceptance import (
            VALIDATOR_VERSION,
            build_repair_prompt,
            unfinished_validation_todos,
            verify_validation_plan,
        )

        if not self._acceptance_needed():
            return None

        flat = self._flatten_todos(self.todos)
        report = verify_validation_plan(flat)
        _assistant_debug_log(
            f"[agent-debug] event=acceptance_verify passed={report.get('passed')} "
            f"checked={report.get('checked')} failures={len(report.get('failures') or [])} "
            f"repair_count={int(getattr(self, '_acceptance_repair_count', 0) or 0)}"
        )
        if report.get("passed"):
            return None

        max_repair = self._max_acceptance_repair_attempts()
        repair_count = int(getattr(self, "_acceptance_repair_count", 0) or 0)
        failures = list(report.get("failures") or [])

        if repair_count < max_repair:
            self._acceptance_repair_count = repair_count + 1
            prompt = build_repair_prompt(
                failures,
                attempt=self._acceptance_repair_count,
                max_attempts=max_repair,
            )
            self.history.append(ChatMessage(
                role="assistant",
                content=result,
                reasoning_content=response.get("reasoning_content") or "",
                provider_content=list(response.get("provider_content") or []),
                blocks=blocks,
                hidden=True,
                timestamp=ts,
            ))
            messages.append({
                "role": "assistant",
                "content": result,
                "reasoning_content": response.get("reasoning_content") or "",
                "provider_content": response.get("provider_content") or [],
            })
            messages.append({"role": "user", "content": prompt})
            await self._emit_status(on_status, "验收未通过，正在注入修复上下文…")
            await self._emit_single_block(on_block, {
                "type": "status",
                "text": f"acceptance repair {self._acceptance_repair_count}/{max_repair}",
                "validator_version": VALIDATOR_VERSION,
                "failures": failures[:8],
            })
            return {"continue": True}

        reason = (
            f"确定性验收未通过（validator={VALIDATOR_VERSION}），"
            f"修复次数已达上限 {max_repair}。"
            f"失败项：{', '.join(str(f.get('todo_id') or '?') for f in failures[:6]) or 'validation'}"
        )
        return {
            "response": self._commit_auto_limit_decision_stop(
                reason=reason,
                current_text=result,
                tool_calls_made=tool_calls_made,
            ),
        }

    def _should_auto_continue(self, reply_text: str = "") -> bool:
        """Decide whether a tool-call-free reply should auto-continue.

        Order of checks (most decisive first):

        1. The model explicitly called ``stop_for_user_decision`` this turn.
           NEVER auto-continue — the user must answer.
        2. An assessment todo plan exists but the user has not confirmed it yet.
           NEVER auto-continue — execution must wait for explicit approval.
        3. The last reply text reads like a direct question to the user
           (heuristic on ?-marks / "继续吗" / "do you want me to"…).
           Stop, even if todos look unfinished — the model is asking.
           User auto-approve skips this: the agent must decide itself.
        4. The model must have touched the todo list during THIS user turn.
           Stale ``in_progress`` todos from an older run must never hijack a
           brand-new user question.
        5. There must be at least one unfinished todo.
        """
        if self._user_decision_requested:
            return False
        if self.todos and not self._todos_plan_approved:
            return False
        if (
            reply_text
            and self._looks_like_user_question(reply_text)
            and not getattr(self, "_should_agent_self_decide", lambda: False)()
        ):
            return False
        if not self._todos_touched_this_turn:
            return False
        unfinished = self._unfinished_todos()
        if not unfinished:
            return False
        return True


    def _build_todo_plan_confirmation_payload(self) -> dict[str, Any]:
        unfinished = self._unfinished_todos()
        count = len(unfinished)
        return {
            "question": (
                f"以上是多步任务计划（{count} 项待处理）。"
                "请确认是否按此计划开始执行？"
            ),
            "options": ["开始执行", "修改计划", "取消任务"],
            "context": (
                "确认前不会自动执行探测、扫描或沙盒脚本等工具。"
                "如需调整，可直接说明要增删的步骤或子任务；"
                "AI 会更新 todo 并再次等待确认。"
            ),
            "decision_kind": "todo_plan",
        }


    def _coerce_todos_pending_for_plan_review(self) -> None:
        for item in self._flatten_todos(self.todos):
            if item.get("status") == "in_progress":
                item["status"] = "pending"
        self.todos = self._enforce_single_in_progress(self.todos)


    def _arm_todo_plan_confirmation(self, *, stop_turn: bool) -> dict[str, Any] | None:
        """Mark the current todo list as awaiting user approval."""
        if not self.todos:
            self._todos_plan_approved = True
            return None
        source = str(getattr(self, "_current_turn_source", "") or "")
        # Child Agents inherit the parent's authorization to run unattended.
        # Confirmation resumes use message_source="confirmation", so depth is
        # the reliable signal — without it a mid-resume set_todos on an
        # unapproved child would try to show a card the child UI cannot answer.
        if source in {"cron", "subagent"} or int(getattr(self, "_subagent_depth", 0) or 0) > 0:
            self._todos_plan_approved = False
            self._auto_approve_todo_plan_for_runtime(
                source if source in {"cron", "subagent"} else "subagent"
            )
            return None
        if getattr(self, "_user_auto_approve_mode", False):
            self._todos_plan_approved = False
            self._auto_approve_todo_plan_for_runtime("auto_approve")
            return None
        self._todos_plan_approved = False
        self._coerce_todos_pending_for_plan_review()
        payload = self._build_todo_plan_confirmation_payload()
        if stop_turn:
            self._user_decision_requested = True
        return payload


    def _auto_approve_todo_plan_for_cron(self) -> bool:
        """Approve a cron-authored plan under the already confirmed schedule."""
        return self._auto_approve_todo_plan_for_runtime("cron")

    def _auto_approve_todo_plan_for_runtime(self, source: str) -> bool:
        """Approve plans authored by an already-authorized unattended runtime."""
        if not self.todos:
            self._todos_plan_approved = True
            return False
        was_approved = bool(self._todos_plan_approved)
        self._approve_todo_plan()
        self._user_decision_requested = False
        if not was_approved:
            timestamp = time.time()
            source_ref = str(getattr(self, "_current_turn_source_ref", "") or "")
            payload = {
                "source_ref": source_ref,
                "todo_count": len(self._flatten_todos(self.todos)),
            }
            if source == "cron":
                payload["cron_job_id"] = source_ref
            self.events.append({
                "id": f"evt_{int(timestamp * 1000)}_{len(self.events)}",
                "kind": "todo_plan_auto_approved",
                "source": source,
                "render_policy": "audit_only",
                "context_policy": "exclude",
                "turn_id": str(getattr(self, "_current_turn_id", "") or ""),
                "timestamp": timestamp,
                "payload": payload,
            })
            if len(self.events) > 2000:
                del self.events[:-2000]
        return not was_approved


    def _approve_todo_plan(self) -> None:
        self._todos_plan_approved = True
        current = self._current_action_todo(self.todos)
        if current is None:
            return
        if (current.get("status") or "pending") != "in_progress":
            current["status"] = "in_progress"
            active_id = str(current.get("id") or "")
            self.todos = self._enforce_single_in_progress(self.todos, active_id=active_id)


    def _classify_todo_plan_choice(self, message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return ""
        low = text.lower()
        approve_keys = {
            "开始执行", "确认计划", "按此执行", "开始", "确认", "执行",
            "approve", "go", "yes", "start",
        }
        cancel_keys = {
            "取消任务", "取消计划", "取消", "cancel", "stop", "abort",
        }
        modify_keys = {
            "修改计划", "修改", "调整计划", "调整", "modify", "edit",
        }
        if text in approve_keys or low in approve_keys:
            return "approve"
        if text in cancel_keys or low in cancel_keys:
            return "cancel"
        if text in modify_keys or low in modify_keys:
            return "modify"
        return ""


    def _apply_todo_plan_choice(self, choice: str, message: str) -> str:
        """Apply a plan-card click and return the prompt the model should see.

        A bare「开始执行」used to be appended as the latest user turn. Models
        then called ``set_todos`` again (the skill says to plan before the
        first execution tool), which re-armed the card and stopped the turn.
        """
        text = str(message or "").strip()
        if choice == "approve":
            self._approve_todo_plan()
            return (
                f"{text}\n\n"
                "用户已确认当前 todo 计划，请立即按已批准的步骤执行，"
                "用 update_todo / complete_todo_with_refs 推进状态。"
                "不要再次调用 set_todos 来展示确认卡，也不要重新提问是否开始。"
            )
        if choice == "modify":
            self._todos_plan_approved = False
            return (
                f"{text}\n\n"
                "请根据上述要求调整 todo 计划（可增删步骤或子任务），"
                "更新后再次展示计划并等待我确认，不要开始执行探测/扫描等工具。"
            )
        return text


    def _should_probe_toolless_continuation(
        self,
        reply_text: str = "",
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Ask the model once more when it stops after tools without todos.

        This avoids brittle natural-language cue lists. The runtime only
        identifies the structural gray zone; the model then chooses by either
        emitting more tool calls or writing a final answer.
        """
        if self._user_decision_requested:
            return False
        if not tool_calls_made:
            return False
        if (
            reply_text
            and self._looks_like_user_question(reply_text)
            and not getattr(self, "_should_agent_self_decide", lambda: False)()
        ):
            return False
        # If the model already rendered a presentation block this turn (a card /
        # table / file view), it has deliberately presented its conclusion.
        # Presentation tools are terminal by nature — nudging the model to
        # "continue" here is what previously burned an extra round that just
        # re-emitted the same answer. Treat any presentation tool as closure.
        if any(
            (t.get("name") if isinstance(t, dict) else None) in _PRESENTATION_TOOLS
            for t in (tool_calls_made or [])
        ):
            return False
        if self._todos_touched_this_turn:
            # An unfinished plan is handled by `_should_auto_continue` before
            # this gate. Once every item is terminal, force one finalization
            # pass: models otherwise often stop at “进展汇报/已完成检查” without
            # turning the evidence into the answer the user requested.
            return not self._unfinished_todos()
        return True


    def _looks_like_user_question(self, text: str) -> bool:
        """Detect 'AI is asking the user' patterns in the final reply text.

        Only the LAST 600 chars are inspected — mid-reply rhetorical questions
        ('should we try X next?' followed by more reasoning) shouldn't trip
        this. The decisive cue is usually at the very end of the message.
        """
        if not text:
            return False
        tail = text.strip()[-600:]
        if not tail:
            return False
        low = tail.lower()
        if any(cue in tail for cue in self._USER_QUESTION_CUES if any(ord(c) > 127 for c in cue)):
            # Chinese cues — match against original case-preserving tail.
            return True
        if any(cue in low for cue in self._USER_QUESTION_CUES if all(ord(c) < 128 for c in cue)):
            return True
        # As a final fallback, a tail that ends with a question mark AND
        # contains a 2nd-person pronoun is almost always a real question.
        last_punct = tail.rstrip()[-1:]
        if last_punct in ("?", "？"):
            if any(p in low for p in (" you ", " your ")) or any(p in tail for p in ("你", "您")):
                return True
        return False


    def _text_has_proceed_cue(self, text: str) -> bool:
        """True when ``text`` is a go / no-go, not an open question."""
        raw = (text or "").strip()
        if not raw:
            return False
        low = raw.lower()
        cues = self._PROCEED_CONFIRM_CUES
        if any(cue in raw for cue in cues if any(ord(c) > 127 for c in cue)):
            return True
        return any(cue in low for cue in cues if all(ord(c) < 128 for c in cue))


    def _warrants_proceed_decision_card(self, text: str) -> bool:
        """Start/Cancel is only valid when the ask itself is proceed-or-stop.

        The card question is what sits next to the buttons. Matching a cue
        buried in the essay (or a bare "你" + "?") used to mint 「开始 / 取消」
        under "需要接着做什么？", which does not start or cancel anything.
        """
        raw = (text or "").strip()
        if not raw:
            return False
        question = self._extract_decision_card_question(raw, max_chars=320)
        return self._text_has_proceed_cue(question)


    def _finish_if_implicit_user_question(
        self,
        response: dict[str, Any],
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse | None:
        """Drop tool calls that arrived with a user-facing question.

        A real proceed/cancel ask becomes a decision card. An open question
        is kept as a normal reply: the tools stay unexecuted, and the user
        answers in the composer instead of a Start/Cancel card.
        """
        if not self._should_stop_before_tool_calls(response):
            return None
        text = response.get("content") or ""
        if self._warrants_proceed_decision_card(text):
            return self._commit_implicit_user_question_stop(text, tool_calls_made)
        return self._commit_open_question_without_decision_card(text, tool_calls_made)


    def _commit_open_question_without_decision_card(
        self,
        question_text: str,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Persist the reply and leave the composer free.

        The same-message tool calls are intentionally not executed. They are
        also not shown: there is no pending action for the user to approve.
        """
        import time as _time

        text = (question_text or "").strip()
        traces = list(tool_calls_made or [])
        blocks = self._blocks_for_tool_trace(traces) if traces else []
        self.history.append(ChatMessage(
            role="assistant",
            content=text,
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=text,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="done",
        )


    def _should_stop_before_tool_calls(self, response: dict[str, Any]) -> bool:
        """Hard-stop if the model asks for confirmation while also emitting tools.

        Some providers/models produce natural-language content such as "要现在
        开始吗？" and tool_calls in the same assistant message. The visible text
        has already asked the user to decide, so executing the attached tools
        would violate the product contract. Treat that as an implicit
        stop_for_user_decision and drop the tool calls from persisted history.
        """
        if not (response.get("tool_calls") or []):
            return False
        if self._is_unattended_runtime():
            return False
        return self._looks_like_user_question(response.get("content") or "")


    def _extract_decision_card_question(self, text: str, *, max_chars: int = 320) -> str:
        """Pick a short card question from a long assistant reply (Loom22 §9.6).

        The decision card is a product-state control, not a second copy of the
        essay. Prefer trailing question lines; never dump the full explanation
        into ``question``.
        """
        import re

        raw = (text or "").strip()
        if not raw:
            return "请确认是否继续。"
        max_n = max(80, min(int(max_chars or 320), 600))

        def _looks_like_q(p: str) -> bool:
            if "?" in p or "？" in p:
                return True
            # Avoid bare tokens like "选择" that appear in prose ("类型选择").
            return any(
                tok in p
                for tok in (
                    "吗？", "吗?", "吗\n", "呢？", "呢?",
                    "哪些", "是否", "还是", "请选", "请确认",
                    "你想", "您想", "要不要", "可否",
                )
            ) or p.rstrip().endswith(("吗", "呢"))

        # Prefer blank-line paragraphs; fall back to single lines.
        paras = [p.strip() for p in re.split(r"\n\s*\n+", raw) if p.strip()]
        if len(paras) <= 1:
            paras = [ln.strip() for ln in raw.splitlines() if ln.strip()] or [raw]

        # Walk upward from the end while lines still look like the ask.
        cluster: list[str] = []
        for para in reversed(paras):
            if _looks_like_q(para):
                cluster.append(para)
                if sum(len(x) for x in cluster) >= max_n or len(cluster) >= 3:
                    break
                continue
            if cluster:
                break
        out = "\n".join(reversed(cluster)).strip() if cluster else paras[-1]
        if len(out) > max_n:
            clip = out[-max_n:]
            cut = clip.find("\n")
            out = (clip[cut + 1 :] if 0 <= cut < 80 else clip).strip()
        return out or "请确认是否继续。"

    def _sanitize_user_decision_block(self, block: dict[str, Any]) -> dict[str, Any]:
        """Clamp card fields so the model cannot paste the essay into the card."""
        out = dict(block or {})
        q = str(out.get("question") or "").strip()
        if len(q) > 320:
            out["question"] = self._extract_decision_card_question(q, max_chars=320)
        elif q:
            out["question"] = q
        ctx = str(out.get("context") or "").strip()
        if len(ctx) > 480:
            out["context"] = ctx[:480].rstrip() + "…"
        else:
            out["context"] = ctx
        return out

    def _commit_implicit_user_question_stop(
        self,
        question_text: str,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Persist a visible confirmation question without executing tools."""
        import time as _time

        text = (question_text or "").strip() or "请确认是否继续。"
        card_question = self._extract_decision_card_question(text, max_chars=320)
        traces = list(tool_calls_made or [])
        blocks = self._blocks_for_tool_trace(traces) if traces else []
        blocks.append(self._sanitize_user_decision_block({
            "type": "user_decision",
            "question": card_question,
            "options": ["开始", "取消"],
            "context": "AI 已请求用户确认，因此本轮不会自动执行后续工具。",
        }))
        self._user_decision_requested = True
        self.history.append(ChatMessage(
            role="assistant",
            content=text,
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=text,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="awaiting_user_decision",
        )


    def _commit_auto_limit_decision_stop(
        self,
        *,
        reason: str,
        current_text: str = "",
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Pause automatic continuation after a configured safety limit.

        This is distinct from model-initiated ``stop_for_user_decision``: the
        runtime hit a user-configured auto-advance cap and must ask before
        spending more work.
        """
        import time as _time

        traces = list(tool_calls_made or [])
        _assistant_debug_log(
            "assistant.auto_limit_stop "
            f"reason={reason!r} tool_count={len(traces)} unfinished_todos={len(self._unfinished_todos())} "
            f"current_text_len={len(current_text or '')}"
        )
        log.warning(
            "assistant.auto_limit_stop reason=%r tool_count=%d unfinished_todos=%d current_text_len=%d",
            reason,
            len(traces),
            len(self._unfinished_todos()),
            len(current_text or ""),
        )
        try:
            self._log_budget_exit(
                reason,
                tool_calls_made_count=len(traces),
                current_text_len=len(current_text or ""),
            )
        except Exception as exc:
            log.debug("budget_exit logging failed: %s", exc)
        prefix = "[等待用户决定] "
        question = f"{reason}。是否继续让 AI 从当前进度接着推进？"
        body_parts = [prefix + question]
        if current_text.strip():
            body_parts.append("\n\nAI 当前回复：\n" + current_text.strip())
        body = "\n".join(body_parts)

        blocks: list[dict[str, Any]] = []
        if traces:
            blocks.extend(self._blocks_for_tool_trace(traces) or [])
        if current_text.strip():
            blocks.extend(self._blocks_for_text(current_text.strip()) or [])
        blocks.append({
            "type": "user_decision",
            "question": question,
            "options": ["继续", "停止"],
            "context": reason,
        })
        self._user_decision_requested = True
        self.history.append(ChatMessage(
            role="assistant",
            content=body,
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=body,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="awaiting_user_decision",
        )


    def _commit_team_room_silence(
        self,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """End a standing-team turn without posting anything to the room."""
        import time as _time

        traces = list(tool_calls_made or [])
        self.history.append(ChatMessage(
            role="assistant",
            content="",
            ui_hidden=True,
            tool_trace=traces,
            blocks=[],
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text="",
            tool_calls=traces,
            blocks=[],
            tool_trace=traces,
            status="done",
        )


    @staticmethod
    def _is_terminal_channel_reply_trace(trace: dict[str, Any] | None) -> bool:
        """Return whether ``trace`` accepted the turn's final channel reply.

        ``channel_reply`` is a delivery primitive, not an intermediate lookup:
        once its exact bubbles are accepted by the outbox, asking the model for
        another assistant message only invites a redundant delivery receipt.
        Failed/rejected calls deliberately remain non-terminal so the model can
        retry or explain the failure.
        """
        if not isinstance(trace, dict) or trace.get("name") != "channel_reply":
            return False
        result = trace.get("result")
        return bool(
            isinstance(result, dict)
            and result.get("ok") is True
            and result.get("queued") is True
            and result.get("terminal_delivery") is True
        )


    def _commit_terminal_channel_reply(
        self,
        tool_calls_made: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Finish silently after ``channel_reply`` accepted its message bubbles."""
        traces = list(tool_calls_made or [])
        self.history.append(ChatMessage(
            role="assistant",
            content="",
            ui_hidden=True,
            tool_trace=traces,
            blocks=[],
            timestamp=time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text="",
            tool_calls=traces,
            blocks=[],
            tool_trace=traces,
            status="done",
        )


    def _commit_stream_repetition_stop(
        self,
        *,
        response: dict[str, Any] | None,
        tool_calls_made: list[dict[str, Any]] | None,
    ) -> ChatResponse:
        """Finalise the turn when the in-stream repetition watchdog tripped.

        We persist whatever bytes the user actually saw on screen, append
        a clear marker, surface a ``stream_repetition`` status to the UI,
        and offer **three** in-session recovery options instead of asking
        the user to abandon the conversation:

        1. ``self_correct`` — re-enter the agent loop with a system note
           that explains the model was repeating itself; usually nudges
           the sampler out of the low-entropy loop.
        2. ``skip`` — accept the truncated answer and ask the model to
           move on to the next step, keeping all prior tool results.
        3. ``pause`` — do nothing automatic; wait for the user to type a
           follow-up message.
        """

        import time as _time

        traces = list(tool_calls_made or [])
        response = response or {}
        current_text = (self._last_partial_text or response.get("content") or "").rstrip()
        marker = "[模型复读已自动中止] "
        if current_text:
            body = marker + current_text
        else:
            body = marker + "本轮无可用文本输出。"

        log.warning(
            "assistant.stream_repetition_commit text_len=%d tool_count=%d turn=%s round=%d",
            len(current_text or ""),
            len(traces),
            self._current_turn_id,
            self._current_round_index,
        )

        blocks: list[dict[str, Any]] = []
        if traces:
            blocks.extend(self._blocks_for_tool_trace(traces) or [])
        if current_text:
            blocks.extend(self._blocks_for_text(current_text) or [])
        blocks.append({
            "type": "user_decision",
            "question": (
                "模型本轮出现复读，已自动中止。请选择如何继续（会话不会丢，"
                "工具结果和工作区文件都还在）："
            ),
            "options": [
                "自我纠偏后继续",
                "跳过本轮、继续推进",
                "暂停，等我输入",
            ],
            "context": "stream_repetition",
        })
        self._user_decision_requested = True
        self._stream_repetition_marker = True
        self.history.append(ChatMessage(
            role="assistant",
            content=body,
            reasoning_content=str(response.get("reasoning_content") or ""),
            provider_content=list(response.get("provider_content") or []),
            blocks=list(blocks),
            tool_trace=traces,
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(
            text=body,
            tool_calls=traces,
            blocks=blocks,
            tool_trace=traces,
            status="stream_repetition",
        )


    def _classify_stream_repetition_choice(self, message: str) -> str:
        """Map a user reply to one of the three recovery options.

        Matches both the canonical Chinese labels the user_decision card
        shows and a few short English aliases / numeric shortcuts, so a
        terminal-style "1/2/3" reply also works.
        """

        if not message:
            return ""
        text = message.strip().lower()
        if not text:
            return ""
        self_correct_keys = {
            "自我纠偏后继续", "自我纠偏", "纠偏后继续", "纠偏继续",
            "纠偏", "self_correct", "self-correct", "correct", "1",
        }
        skip_keys = {
            "跳过本轮、继续推进", "跳过本轮", "跳过", "继续推进",
            "continue", "skip", "2",
        }
        pause_keys = {
            "暂停，等我输入", "暂停等我输入", "暂停", "pause", "wait",
            "3",
        }
        if text in self_correct_keys:
            return "self_correct"
        if text in skip_keys:
            return "skip"
        if text in pause_keys:
            return "pause"
        return ""


    def _build_repetition_recovery_prompt(self, choice: str) -> str:
        """Translate a recovery choice into a clear user-side prompt.

        Using a transparent user-visible prompt (rather than a magic
        hidden system note) means the next exported trace / report still
        shows exactly what nudged the model — useful for audit and for
        diagnosing whether the recovery itself worked.
        """

        if choice == "self_correct":
            return (
                "[复读恢复 · 自我纠偏后继续]\n"
                "刚才你的上一条回复出现了重复输出，已被自动折叠。"
                "请用一个完全不同的角度重新表达当前结论，"
                "并明确给出下一步要做什么；不要再复述刚才那段话。"
            )
        if choice == "skip":
            return (
                "[复读恢复 · 跳过本轮、继续推进]\n"
                "上一轮的文字回复因复读已被中止，但已收集到的工具结果都还在。"
                "请直接基于这些工具结果继续推进下一步动作，不要重复说之前已经说过的话。"
            )
        return (
            "[复读恢复 · 继续]\n"
            "请基于已有上下文，简洁地给出下一步。"
        )


    def _log_auto_continue_trigger(self, count: int, reply_text: str = "") -> None:
        _assistant_debug_log(
            "assistant.auto_continue "
            f"count={count} limit={self._max_auto_continue()} unfinished_todos={len(self._unfinished_todos())} "
            f"reply_len={len(reply_text or '')}"
        )
        log.warning(
            "assistant.auto_continue count=%d limit=%d unfinished_todos=%d reply_len=%d",
            count,
            self._max_auto_continue(),
            len(self._unfinished_todos()),
            len(reply_text or ""),
        )


    def _log_toolless_probe_trigger(self, count: int, tool_calls_made: list[dict[str, Any]] | None, reply_text: str = "") -> None:
        recent = ",".join(str(t.get("name") or "?") for t in (tool_calls_made or [])[-5:])
        _assistant_debug_log(
            "assistant.toolless_probe "
            f"count={count} limit={self._max_toolless_continue_decisions()} tool_count={len(tool_calls_made or [])} "
            f"recent_tools={recent} reply_len={len(reply_text or '')}"
        )
        log.warning(
            "assistant.toolless_probe count=%d limit=%d tool_count=%d recent_tools=%s reply_len=%d",
            count,
            self._max_toolless_continue_decisions(),
            len(tool_calls_made or []),
            recent,
            len(reply_text or ""),
        )


    def _log_budget_restart_trigger(self, count: int, tool_calls_made: list[dict[str, Any]] | None) -> None:
        recent = ",".join(str(t.get("name") or "?") for t in (tool_calls_made or [])[-5:])
        _assistant_debug_log(
            "assistant.budget_restart "
            f"count={count} limit={self._max_budget_restarts()} max_agent_rounds={self._max_agent_rounds()} "
            f"tool_count={len(tool_calls_made or [])} recent_tools={recent}"
        )
        log.warning(
            "assistant.budget_restart count=%d limit=%d max_agent_rounds=%d tool_count=%d recent_tools=%s",
            count,
            self._max_budget_restarts(),
            self._max_agent_rounds(),
            len(tool_calls_made or []),
            recent,
        )


    def _tool_debug_hint(self, args: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("method", "target", "url", "scope", "wait_seconds", "since", "force", "fresh_context"):
            value = args.get(key)
            if value is None or value == "":
                continue
            text = str(value).replace("\r", "\\r").replace("\n", "\\n")
            if len(text) > 160:
                text = text[:157] + "..."
            parts.append(f"{key}={text!r}")
        if "data" in args:
            parts.append(f"data_len={len(str(args.get('data') or ''))}")
        if "expression" in args:
            parts.append(f"expression_len={len(str(args.get('expression') or ''))}")
        if not parts:
            parts.append("arg_keys=" + ",".join(sorted(str(k) for k in args.keys())[:12]))
        return " ".join(parts)


    def _log_tool_dispatch_start(self, fn_name: str, args: dict[str, Any], tool_calls_made: list[dict[str, Any]]) -> None:
        _assistant_debug_log(
            "assistant.tool_dispatch "
            f"index={len(tool_calls_made) + 1} max_agent_rounds={self._max_agent_rounds()} "
            f"name={fn_name} {self._tool_debug_hint(args)}"
        )
        try:
            self._log_tool_signature(fn_name, args, len(tool_calls_made) + 1)
        except Exception as exc:
            log.debug("tool_signature logging failed: %s", exc)


    def _log_tool_duplicate_skip(self, fn_name: str, args: dict[str, Any], tool_calls_made: list[dict[str, Any]]) -> None:
        _assistant_debug_log(
            "assistant.tool_duplicate_skip "
            f"index={len(tool_calls_made) + 1} name={fn_name} {self._tool_debug_hint(args)}"
        )


    def _reset_agent_loop_stall_tracking(self) -> None:
        """Clear per-turn deadlock detector state.

        Called at the start of every user turn (see ``session_chat_loop``).
        The duplicate-skip hint guard and the round-signature ring buffer
        must both reset so a fresh turn never inherits stale state from a
        previous one.
        """
        self._duplicate_skip_hint_injected = False
        self._recent_round_signatures = []
        self._recent_tool_outcomes = []
        self._poll_no_progress_hint_injected = False
        self._ping_pong_hint_injected = False
        self._same_tool_failure_hint_injected = False
        self._mutating_repetition_hint_injected = False
        self._auto_continue_progress_sig = None
        self._auto_continue_stalled = False


    def _inject_duplicate_skip_hint(self, messages: list[dict[str, Any]]) -> None:
        """Inject one hidden system hint per turn when §11 dedup kicks in.

        This is the *only* feedback channel for duplicate skips now —
        Loom22 §11 explicitly forbids treating dedup as a "stop signal".
        The hint nudges the model to re-read prior tool results instead
        of firing the same call again. Once injected per turn, we don't
        spam it again — the model already knows.
        """
        if getattr(self, "_duplicate_skip_hint_injected", False):
            return
        import time as _time

        self._duplicate_skip_hint_injected = True
        warn = _DUPLICATE_SKIP_HINT_MESSAGE
        messages.append({"role": "user", "content": warn})
        self.history.append(ChatMessage(
            role="user",
            content=warn,
            hidden=True,
            timestamp=_time.time(),
        ))
        _assistant_debug_log(
            f"assistant.duplicate_skip_hint turn={self._current_turn_id}"
        )


    def _round_signature_hard_stop_count(self) -> int:
        """Return the framework-owned exact-round deadlock threshold."""
        return _DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT


    def _agent_loop_circuit_breaker_exceeded(self) -> bool:
        """True only when the assistant emitted the same round N times running.

        "Same round" = identical cleaned content fingerprint *and*
        identical sorted set of ``(tool_name, args_hash)`` calls. The
        check is intentionally strict: a turn where the model is
        silently progressing (empty content, but each tool call has a
        new ``args_hash``) produces a *different* signature each round
        and will never trip this guard.

        Returns ``False`` immediately when the threshold is set to ``0``
        (user has opted out of the safety net).
        """
        threshold = self._round_signature_hard_stop_count()
        if threshold <= 0:
            return False
        buf = getattr(self, "_recent_round_signatures", None) or []
        if len(buf) < threshold:
            return False
        tail = buf[-threshold:]
        return len({entry["sig"] for entry in tail}) == 1


    def _commit_agent_loop_circuit_breaker(
        self,
        *,
        tool_calls_made: list[dict[str, Any]],
        tc_list: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> ChatResponse:
        buf = getattr(self, "_recent_round_signatures", None) or []
        repeated = self._round_signature_hard_stop_count() or _DEFAULT_ROUND_SIGNATURE_HARD_STOP_COUNT
        sample = buf[-1] if buf else {}
        preview = sample.get("preview") or "(空回复)"
        tool_names = sample.get("tool_names") or []
        reason = (
            f"检测到 Agent 卡死（连续 {repeated} 轮回复与工具调用完全相同）。"
            f"最近一次工具：{','.join(tool_names[:3]) or '无'}；"
            f"回复预览：{preview[:60]!r}。"
            "请阅读已有结果并换一种策略，或选择「停止」结束本轮。"
        )
        self._fill_missing_tool_results(tc_list, messages, reason="agent_loop_circuit_breaker")
        _assistant_debug_log(
            f"assistant.loop_circuit_breaker repeated={repeated} tool_count={len(tool_calls_made)} "
            f"turn={self._current_turn_id}"
        )
        log.warning(
            "assistant.loop_circuit_breaker repeated=%d tool_count=%d turn=%s tools=%s",
            repeated,
            len(tool_calls_made),
            self._current_turn_id,
            ",".join(tool_names[:5]),
        )
        return self._commit_auto_limit_decision_stop(
            reason=reason,
            current_text="",
            tool_calls_made=tool_calls_made,
        )


    # -- Result-aware loop detector (poll_no_progress / ping_pong) ----------

    def _result_fingerprint(self, result: Any, *, length: int = 16) -> str:
        """Stable digest of a tool *result* for no-progress comparison.

        We hash the whole normalized result. Volatile envelope keys that
        the runtime adds *after* execution (``case_id`` / ``artifact_ref``)
        are not present yet because recording happens before archiving, so
        two genuinely identical poll results hash the same.
        """
        try:
            payload = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = str(result)
        return self._short_fingerprint(payload, length=length)


    def _record_tool_outcome(self, fn_name: str, args: dict[str, Any], result: Any) -> None:
        """Append one ``(name, arg_fp, result_fp)`` record per real execution.

        Hooked from :meth:`_auto_archive_tool_result`, the single chokepoint
        every actually-executed tool passes through. §11 dedup skips never
        reach here (the archive helper returns early on them), so a skipped
        repeat does not pollute the no-progress window.
        """
        try:
            if isinstance(result, dict) and (
                result.get("skipped") or result.get("reason") == "duplicate_tool_call_skipped"
            ):
                return
            buf = getattr(self, "_recent_tool_outcomes", None)
            if buf is None:
                buf = self._recent_tool_outcomes = []
            buf.append({
                "name": str(fn_name),
                "arg_fp": self._arg_fingerprint(args if isinstance(args, dict) else {"_raw": args}),
                "result_fp": self._result_fingerprint(result),
                "ok": _tool_outcome_ok(result),
                "round": int(getattr(self, "_current_round_index", 0)),
            })
            if len(buf) > _TOOL_OUTCOME_BUFFER_LIMIT:
                del buf[: len(buf) - _TOOL_OUTCOME_BUFFER_LIMIT]
        except Exception as exc:
            log.debug("record_tool_outcome failed: %s", exc)


    def _poll_no_progress_count(self) -> int:
        return self._assistant_setting_int(
            "assistant_poll_no_progress_stop", _DEFAULT_POLL_NO_PROGRESS_COUNT, 0, 50,
        )


    def _ping_pong_count(self) -> int:
        return self._assistant_setting_int(
            "assistant_ping_pong_stop", _DEFAULT_PING_PONG_COUNT, 0, 50,
        )


    def _poll_no_progress_run(self) -> int:
        """Length of the trailing run of identical ``(name, result_fp)``.

        ``read_status → running`` five times in a row returns 5. A different
        tool or a changed result anywhere in the tail resets the count.
        """
        buf = getattr(self, "_recent_tool_outcomes", None) or []
        if not buf:
            return 0
        last = (buf[-1]["name"], buf[-1]["result_fp"])
        run = 0
        for entry in reversed(buf):
            if (entry["name"], entry["result_fp"]) == last:
                run += 1
            else:
                break
        return run


    def _ping_pong_run(self) -> int:
        """Length of the trailing strict A-B-A-B alternation over (name,args).

        Returns 0 unless the tail strictly alternates between exactly two
        distinct ``(name, arg_fp)`` states. Any third state, or a repeat of
        the same state twice in a row, ends the run.
        """
        buf = getattr(self, "_recent_tool_outcomes", None) or []
        sigs = [(e["name"], e["arg_fp"]) for e in buf]
        if len(sigs) < 2:
            return 0
        a = sigs[-1]
        b = sigs[-2]
        if a == b:
            return 0
        run = 0
        expected = a
        for sig in reversed(sigs):
            if sig == expected:
                run += 1
                expected = b if expected == a else a
            else:
                break
        return run


    def _inject_poll_no_progress_hint(self, messages: list[dict[str, Any]], tool_name: str, count: int) -> None:
        if getattr(self, "_poll_no_progress_hint_injected", False):
            return
        import time as _time

        self._poll_no_progress_hint_injected = True
        warn = _POLL_NO_PROGRESS_HINT_MESSAGE.format(count=count, tool=tool_name)
        messages.append({"role": "user", "content": warn})
        self.history.append(ChatMessage(role="user", content=warn, hidden=True, timestamp=_time.time()))
        _assistant_debug_log(
            f"assistant.poll_no_progress_hint tool={tool_name} count={count} turn={self._current_turn_id}"
        )


    def _inject_ping_pong_hint(self, messages: list[dict[str, Any]], a: str, b: str, count: int) -> None:
        if getattr(self, "_ping_pong_hint_injected", False):
            return
        import time as _time

        self._ping_pong_hint_injected = True
        warn = _PING_PONG_HINT_MESSAGE.format(a=a, b=b)
        messages.append({"role": "user", "content": warn})
        self.history.append(ChatMessage(role="user", content=warn, hidden=True, timestamp=_time.time()))
        _assistant_debug_log(
            f"assistant.ping_pong_hint a={a} b={b} count={count} turn={self._current_turn_id}"
        )


    # -- Taxonomy-driven guards (same_tool_failure / mutating_repetition) ----

    def _same_tool_failure_count(self) -> int:
        return self._assistant_setting_int(
            "assistant_same_tool_failure_stop", _DEFAULT_SAME_TOOL_FAILURE_COUNT, 0, 50,
        )


    def _mutating_repetition_count(self) -> int:
        return self._assistant_setting_int(
            "assistant_mutating_repetition_stop", _DEFAULT_MUTATING_REPETITION_COUNT, 0, 50,
        )


    def _same_tool_failure_run(self) -> int:
        """Length of the trailing run of consecutive FAILURES of one tool.

        Args and error text may vary each time (that is exactly the blind spot
        the result/round detectors miss); only the ``ok`` flag and tool name
        matter. Any success, or a switch to a different tool, ends the run.
        """
        buf = getattr(self, "_recent_tool_outcomes", None) or []
        run = 0
        last: str | None = None
        for entry in reversed(buf):
            if entry.get("ok", True):
                break
            name = entry["name"]
            if last is None or name == last:
                run += 1
                last = name
            else:
                break
        return run


    def _mutating_repetition_run(self) -> int:
        """Trailing run of identical successful calls to the SAME mutating tool.

        Keyed on ``(name, arg_fp)`` — the call signature — not the result,
        because a mutating tool's success payload usually carries a fresh
        id/timestamp that defeats result hashing. Non-mutating tools, failures,
        or a changed signature end the run.
        """
        buf = getattr(self, "_recent_tool_outcomes", None) or []
        run = 0
        sig: tuple[str, str] | None = None
        for entry in reversed(buf):
            if not entry.get("ok", True) or tool_kind(entry["name"]) != "mutating":
                break
            cur = (entry["name"], entry["arg_fp"])
            if sig is None or cur == sig:
                run += 1
                sig = cur
            else:
                break
        return run


    def _inject_same_tool_failure_hint(self, messages: list[dict[str, Any]], tool_name: str, count: int) -> None:
        if getattr(self, "_same_tool_failure_hint_injected", False):
            return
        import time as _time

        self._same_tool_failure_hint_injected = True
        warn = _SAME_TOOL_FAILURE_HINT_MESSAGE.format(count=count, tool=tool_name)
        messages.append({"role": "user", "content": warn})
        self.history.append(ChatMessage(role="user", content=warn, hidden=True, timestamp=_time.time()))
        _assistant_debug_log(
            f"assistant.same_tool_failure_hint tool={tool_name} count={count} turn={self._current_turn_id}"
        )


    def _inject_mutating_repetition_hint(self, messages: list[dict[str, Any]], tool_name: str, count: int) -> None:
        if getattr(self, "_mutating_repetition_hint_injected", False):
            return
        import time as _time

        self._mutating_repetition_hint_injected = True
        warn = _MUTATING_REPETITION_HINT_MESSAGE.format(count=count, tool=tool_name)
        messages.append({"role": "user", "content": warn})
        self.history.append(ChatMessage(role="user", content=warn, hidden=True, timestamp=_time.time()))
        _assistant_debug_log(
            f"assistant.mutating_repetition_hint tool={tool_name} count={count} turn={self._current_turn_id}"
        )


    def _commit_tool_loop_stop(
        self,
        *,
        pattern: str,
        detail: str,
        count: int,
        tool_calls_made: list[dict[str, Any]],
        tc_list: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> ChatResponse:
        if pattern == "poll_no_progress":
            reason = (
                f"检测到无进展轮询：连续 {count} 次调用 `{detail}` 得到完全相同的结果。"
                "请基于现有结果继续推进、改变策略，或选择「停止」结束本轮。"
            )
        elif pattern == "same_tool_failure":
            reason = (
                f"检测到反复失败：连续 {count} 次调用 `{detail}` 均失败。"
                "请换一种方法或补齐前置条件，或选择「停止」结束本轮。"
            )
        elif pattern == "mutating_repetition":
            reason = (
                f"检测到重复变更：用相同参数成功调用有副作用的 `{detail}` 共 {count} 次，疑似原地打转。"
                "请确认上次是否已生效并基于其结果继续，或选择「停止」结束本轮。"
            )
        else:
            reason = (
                f"检测到 Agent 在 {detail} 之间反复横跳，共 {count} 次无进展交替。"
                "请换一种策略或工具，或选择「停止」结束本轮。"
            )
        self._fill_missing_tool_results(tc_list, messages, reason=f"tool_loop_{pattern}")
        log.warning(
            "assistant.tool_loop_stop pattern=%s detail=%s count=%d turn=%s",
            pattern, detail, count, self._current_turn_id,
        )
        _assistant_debug_log(
            f"assistant.tool_loop_stop pattern={pattern} detail={detail} count={count} turn={self._current_turn_id}"
        )
        return self._commit_auto_limit_decision_stop(
            reason=reason, current_text="", tool_calls_made=tool_calls_made,
        )


    def _tool_loop_guard(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        tc_list: list[dict[str, Any]],
    ) -> ChatResponse | None:
        """Round-start loop guard. Returns a stop ChatResponse or ``None``.

        Tiering: ``generic_repeat`` (byte-identical rounds) hard-stops via the
        existing circuit breaker. ``poll_no_progress`` and ``ping_pong`` are
        soft-first — a one-shot hidden hint at the warn threshold, escalating
        to a user_decision card only at 2x — so a legitimate long poll gets a
        nudge to back off instead of being killed.
        """
        if self._turn_deadline_exceeded():
            return self._commit_turn_deadline_stop(
                tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
            )

        if self._agent_loop_circuit_breaker_exceeded():
            return self._commit_agent_loop_circuit_breaker(
                tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
            )

        buf = getattr(self, "_recent_tool_outcomes", None) or []

        warn = self._poll_no_progress_count()
        if warn > 0 and buf:
            run = self._poll_no_progress_run()
            tool_name = buf[-1]["name"]
            if run >= warn * 2:
                return self._commit_tool_loop_stop(
                    pattern="poll_no_progress", detail=tool_name, count=run,
                    tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
                )
            if run >= warn:
                self._inject_poll_no_progress_hint(messages, tool_name, run)

        pp = self._ping_pong_count()
        if pp > 0 and len(buf) >= 2:
            run = self._ping_pong_run()
            if run >= pp:
                a = buf[-1]["name"]
                b = buf[-2]["name"]
                pair = f"`{a}` ⇄ `{b}`"
                if run >= pp * 2:
                    return self._commit_tool_loop_stop(
                        pattern="ping_pong", detail=pair, count=run,
                        tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
                    )
                self._inject_ping_pong_hint(messages, a, b, run)

        stf = self._same_tool_failure_count()
        if stf > 0 and buf:
            run = self._same_tool_failure_run()
            if run >= stf:
                tool_name = buf[-1]["name"]
                if run >= stf * 2:
                    return self._commit_tool_loop_stop(
                        pattern="same_tool_failure", detail=tool_name, count=run,
                        tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
                    )
                self._inject_same_tool_failure_hint(messages, tool_name, run)

        mut = self._mutating_repetition_count()
        if mut > 0 and buf:
            run = self._mutating_repetition_run()
            if run >= mut:
                tool_name = buf[-1]["name"]
                if run >= mut * 2:
                    return self._commit_tool_loop_stop(
                        pattern="mutating_repetition", detail=tool_name, count=run,
                        tool_calls_made=tool_calls_made, tc_list=tc_list, messages=messages,
                    )
                self._inject_mutating_repetition_hint(messages, tool_name, run)

        return None


    def _tool_allows_parallel(self, fn_name: str, args: dict[str, Any]) -> bool:
        """Loom22 §22.6 — only read_parallel / idempotent+polling with unique keys."""
        tool = self.registry.get(fn_name) if getattr(self, "registry", None) else None
        if tool is None:
            kind = tool_kind(fn_name)
            concurrency = "read_parallel" if kind in {"idempotent", "polling"} else "serial"
            if concurrency != "read_parallel":
                return False
            return True
        if tool.completion == "background":
            return False
        if str(tool.concurrency or "serial") != "read_parallel":
            return False
        if tool.kind in {"mutating", "presentation", "control"}:
            return False
        if fn_name in _CONFIRM_TOOLS and not self._tool_auto_approved(fn_name, args):
            return False
        return True


    def _tool_resource_keys(self, fn_name: str, args: dict[str, Any]) -> list[str]:
        tool = self.registry.get(fn_name) if getattr(self, "registry", None) else None
        if tool is not None:
            return tool.resource_keys(args)
        return [fn_name]


    async def _dispatch_one_tool_call(
        self,
        *,
        fn_name: str,
        args: dict[str, Any],
        tc: dict[str, Any],
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        executed_keys: set[str],
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
        on_status: Callable[[str], Awaitable[None] | None] | None,
        visual_tool_attachments: list[dict[str, Any]] | None,
    ) -> None:
        """Dispatch one tool, archive, emit blocks, append messages/history."""
        await self._emit_tool_start(on_status, fn_name, args)
        self._log_tool_dispatch_start(fn_name, args, tool_calls_made)
        tool_result = await self._dispatch_tool(fn_name, args)
        tool_result = self._auto_archive_tool_result(fn_name, args, tool_result)
        if visual_tool_attachments is not None:
            visual_tool_attachments.extend(self._pop_tool_attachments(tool_result))
        self._remember_executed_tool_key(executed_keys, fn_name, args)
        result_str = self._tool_result_to_prompt(fn_name, tool_result)
        trace = self._trace(fn_name, args, tool_result)
        tool_calls_made.append(trace)
        await self._emit_trace_blocks(on_block, trace)
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
        self.history.append(ChatMessage(
            role="tool", content=result_str, tool_call_id=tc["id"], timestamp=time.time(),
        ))
        await self._emit_checkpoint()


    async def _execute_tool_calls_batch(
        self,
        tc_list: list[dict[str, Any]],
        *,
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        executed_keys: set[str],
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], Awaitable[None] | None] | None = None,
        visual_tool_attachments: list[dict[str, Any]] | None = None,
    ) -> ChatResponse | None:
        """Execute a tool-call list with optional readonly parallelism (Loom22 §22.6).

        Returns a ChatResponse when cancel/dedup/terminal-delivery stops the turn;
        else None.
        Results are always committed in the original ``tc_list`` order.
        """
        # Parse once.
        parsed: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        for tc in tc_list:
            fn_name, args = resolve_session_tool_call(tc)
            parsed.append((tc, fn_name, args))

        i = 0
        while i < len(parsed):
            if self._cancel_requested:
                self._fill_missing_tool_results(tc_list, messages, reason="user_cancelled_before_dispatch")
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)

            tc, fn_name, args = parsed[i]
            key = self._tool_call_key(fn_name, args)
            if self._should_dedupe_tool_call(fn_name, args) and key in executed_keys:
                breaker = await self._handle_duplicate_tool_call(
                    fn_name=fn_name,
                    args=args,
                    tc=tc,
                    tc_list=tc_list,
                    messages=messages,
                    tool_calls_made=tool_calls_made,
                    on_block=on_block,
                )
                if breaker is not None:
                    return breaker
                i += 1
                continue

            # Grow a parallel wave of consecutive readonly tools with unique keys.
            wave: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
            used_keys: set[str] = set()
            j = i
            while j < len(parsed):
                tc_j, name_j, args_j = parsed[j]
                key_j = self._tool_call_key(name_j, args_j)
                # Stop the wave before a would-be exact-dedupe skip so the
                # serial path can emit the structured skipped result.
                if self._should_dedupe_tool_call(name_j, args_j) and key_j in executed_keys:
                    break
                if not self._tool_allows_parallel(name_j, args_j):
                    break
                rkeys = set(self._tool_resource_keys(name_j, args_j))
                if used_keys & rkeys:
                    break
                used_keys |= rkeys
                wave.append((tc_j, name_j, args_j))
                j += 1

            if len(wave) >= 2:
                remaining = None
                deadline = getattr(self, "_turn_deadline_at", None)
                if deadline:
                    remaining = max(0, int(float(deadline) - time.time()))
                _assistant_debug_log(
                    f"[agent-debug] event=tool_batch mode=parallel count={len(wave)} "
                    f"deadline_remaining={remaining} tools="
                    + ",".join(n for _, n, _ in wave)
                )
                async def _run(item: tuple[dict[str, Any], str, dict[str, Any]]) -> tuple[str, dict[str, Any], Any]:
                    tc_i, name_i, args_i = item
                    await self._emit_tool_start(on_status, name_i, args_i)
                    self._log_tool_dispatch_start(name_i, args_i, tool_calls_made)
                    result = await self._dispatch_tool(name_i, args_i)
                    return name_i, args_i, result

                gathered = await asyncio.gather(*[_run(item) for item in wave], return_exceptions=True)
                for idx, item in enumerate(wave):
                    tc_i, name_i, args_i = item
                    raw = gathered[idx]
                    if isinstance(raw, BaseException):
                        tool_result: Any = {"ok": False, "error": str(raw)}
                    else:
                        _n, _a, tool_result = raw
                    tool_result = self._auto_archive_tool_result(name_i, args_i, tool_result)
                    if visual_tool_attachments is not None:
                        visual_tool_attachments.extend(self._pop_tool_attachments(tool_result))
                    self._remember_executed_tool_key(executed_keys, name_i, args_i)
                    result_str = self._tool_result_to_prompt(name_i, tool_result)
                    trace = self._trace(name_i, args_i, tool_result)
                    tool_calls_made.append(trace)
                    await self._emit_trace_blocks(on_block, trace)
                    messages.append({"role": "tool", "tool_call_id": tc_i["id"], "content": result_str})
                    self.history.append(ChatMessage(
                        role="tool", content=result_str, tool_call_id=tc_i["id"], timestamp=time.time(),
                    ))
                    await self._emit_checkpoint()
                    if self._cancel_requested:
                        self._fill_missing_tool_results(tc_list, messages, reason="user_cancelled_after_dispatch")
                        await self._emit_status(on_status, "工具执行完成，正在整理结果...")
                        return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                if self._user_decision_requested:
                    await self._emit_status(on_status, "已暂停，等待用户决定。")
                    return self._commit_user_decision_stop(
                        self._last_partial_text,
                        tool_calls_made,
                        tc_list,
                        messages,
                    )
                if self._team_room_passed:
                    return self._commit_team_room_silence(tool_calls_made)
                i = j
                continue

            # Serial single tool (or wave of size 1).
            await self._dispatch_one_tool_call(
                fn_name=fn_name,
                args=args,
                tc=tc,
                messages=messages,
                tool_calls_made=tool_calls_made,
                executed_keys=executed_keys,
                on_block=on_block,
                on_status=on_status,
                visual_tool_attachments=visual_tool_attachments,
            )
            if self._is_terminal_channel_reply_trace(
                tool_calls_made[-1] if tool_calls_made else None
            ):
                # Preserve provider tool-call invariants if a model batched
                # work *after* the terminal reply. Those calls are intentionally
                # not executed: channel_reply must be the final action.
                self._fill_missing_tool_results(
                    tc_list,
                    messages,
                    reason="terminal_channel_reply",
                )
                return self._commit_terminal_channel_reply(tool_calls_made)
            if self._cancel_requested:
                self._fill_missing_tool_results(tc_list, messages, reason="user_cancelled_after_dispatch")
                await self._emit_status(on_status, "工具执行完成，正在整理结果...")
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            if self._user_decision_requested:
                await self._emit_status(on_status, "已暂停，等待用户决定。")
                return self._commit_user_decision_stop(
                    self._last_partial_text,
                    tool_calls_made,
                    tc_list,
                    messages,
                )
            if self._team_room_passed:
                return self._commit_team_room_silence(tool_calls_made)
            i += 1

        return None


    async def _handle_duplicate_tool_call(
        self,
        *,
        fn_name: str,
        args: dict[str, Any],
        tc: dict[str, Any],
        tc_list: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    ) -> ChatResponse | None:
        """Record a Loom22 §11 dedup skip and inject a one-shot self-rescue hint.

        Per Loom22 §11 this is *防抖*, not an error path: we return
        ``ok=True, skipped=True`` so the model knows the previous result
        is still in the trace and should be re-read. We never escalate a
        skip into a stop on its own — that's handled exclusively by the
        round-signature deadlock detector at the start of the next round.
        """
        import time as _time

        self._log_tool_duplicate_skip(fn_name, args, tool_calls_made)
        tool_result = self._duplicate_tool_result(fn_name, args)
        result_str = self._tool_result_to_prompt(fn_name, tool_result)
        trace = self._trace(fn_name, args, tool_result)
        tool_calls_made.append(trace)
        await self._emit_trace_blocks(on_block, trace)
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
        self.history.append(ChatMessage(
            role="tool",
            content=result_str,
            tool_call_id=tc["id"],
            timestamp=_time.time(),
        ))
        await self._emit_checkpoint()
        self._inject_duplicate_skip_hint(messages)
        return None


    def _short_fingerprint(self, text: str, *, length: int = 16) -> str:
        """Stable hex digest of ``text`` for cross-round comparison.

        Two responses with the same fingerprint are byte-identical after
        text-dedup. We deliberately fingerprint the cleaned, dedup-applied
        content so the warning only fires on repetition the dedup pass
        could not collapse (e.g. tool-call sequences with no body text).
        """

        import hashlib

        digest = hashlib.sha1((text or "").encode("utf-8", errors="ignore")).hexdigest()
        return digest[:length]


    def _arg_fingerprint(self, args: dict[str, Any], *, length: int = 12) -> str:
        try:
            payload = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            payload = str(args)
        import hashlib

        return hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:length]


    def _round_signature(
        self,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
    ) -> str:
        """Compute a stable, *progress-sensitive* signature for one round.

        Signature = ``sha1(content_fp + "|" + sorted([tool_name:args_fp]))``.

        Two rounds with the same signature mean **the model produced
        byte-identical text AND issued an identical set of tool calls
        with identical arguments**. That is the only honest definition
        of "the round made no progress". A turn where the model is
        silently iterating (empty content, new ``args_hash`` each step)
        gets a fresh signature every round and is *not* a stall.
        """
        content_fp = self._short_fingerprint(content or "")
        tool_parts: list[str] = []
        for tc in tool_calls or []:
            try:
                name = tc.get("function", {}).get("name") or tc.get("name") or "?"
            except AttributeError:
                name = "?"
            raw_args = None
            try:
                raw_args = tc.get("function", {}).get("arguments")
            except AttributeError:
                raw_args = None
            if isinstance(raw_args, str):
                try:
                    args_obj: Any = json.loads(raw_args)
                except Exception:
                    args_obj = raw_args
            else:
                args_obj = raw_args or {}
            tool_parts.append(f"{name}:{self._arg_fingerprint(args_obj if isinstance(args_obj, dict) else {'_raw': args_obj})}")
        tool_parts.sort()
        return self._short_fingerprint(content_fp + "|" + ",".join(tool_parts))


    def _record_round_signature(
        self,
        content: str,
        tool_calls: list[dict[str, Any]] | None,
    ) -> None:
        """Push one entry into the round-signature ring buffer.

        The deadlock check itself happens in
        :py:meth:`_agent_loop_circuit_breaker_exceeded`, which is
        consulted at the *start of the next round* (and after every
        duplicate skip). This split keeps the recording side pure: it
        only ever appends + logs, never mutates a stall score and never
        returns control flow signals.
        """
        preview_lines = (content or "").strip().splitlines()[:3]
        preview_text = " ⏎ ".join(p[:80] for p in preview_lines if p)
        tool_names: list[str] = []
        for tc in tool_calls or []:
            try:
                name = tc.get("function", {}).get("name") or tc.get("name") or "?"
            except AttributeError:
                name = "?"
            tool_names.append(str(name))
        sig = self._round_signature(content, tool_calls)
        entry = {
            "round": int(self._current_round_index),
            "turn": str(self._current_turn_id),
            "len": len(content or ""),
            "tool_count": len(tool_calls or []),
            "tool_names": tool_names,
            "sig": sig,
            "preview": preview_text,
            "ts": time.time(),
        }
        buf = self._recent_round_signatures
        buf.append(entry)
        if len(buf) > _ROUND_SIGNATURE_BUFFER_LIMIT:
            del buf[: len(buf) - _ROUND_SIGNATURE_BUFFER_LIMIT]

        log.info(
            "assistant.round_signature round=%d turn=%s len=%d tool_count=%d tools=%s sig=%s preview=%r",
            entry["round"],
            entry["turn"],
            entry["len"],
            entry["tool_count"],
            ",".join(tool_names[:5]),
            sig,
            preview_text,
        )

        threshold = self._round_signature_hard_stop_count()
        if threshold > 0:
            tail = buf[-threshold:]
            if (
                len(tail) == threshold
                and len({e["sig"] for e in tail}) == 1
            ):
                log.warning(
                    "assistant.round_signature_repeat rounds=%s sig=%s preview=%r tool_names=%s",
                    ",".join(str(e["round"]) for e in tail),
                    sig,
                    preview_text,
                    ",".join(tool_names[:5]),
                )


    def _log_round_start(self, round_index: int, tool_calls_made_count: int) -> None:
        self._current_round_index = int(round_index)
        log.info(
            "assistant.round_start round=%d turn=%s tool_count_so_far=%d max_agent_rounds=%d",
            self._current_round_index,
            self._current_turn_id,
            tool_calls_made_count,
            self._max_agent_rounds(),
        )


    def _log_round_end(
        self,
        round_index: int,
        *,
        tool_calls_made_count: int,
        elapsed_ms: int,
        had_tool_calls: bool,
        text_len: int,
        reason: str = "",
    ) -> None:
        log.info(
            "assistant.round_end round=%d turn=%s tool_count_so_far=%d had_tool_calls=%s text_len=%d elapsed_ms=%d reason=%s",
            int(round_index),
            self._current_turn_id,
            int(tool_calls_made_count),
            "yes" if had_tool_calls else "no",
            int(text_len),
            int(elapsed_ms),
            reason or "-",
        )


    def _log_tool_signature(
        self,
        fn_name: str,
        args: dict[str, Any],
        tool_calls_made_count: int,
    ) -> None:
        """Detect "same tool + same args fired 3+ times in 5 rounds".

        This catches the loop pattern that survives the duplicate-skip
        gate, e.g. when each call mutates one byte of an argument so the
        registry-level cache cannot dedupe but the model is plainly
        spinning. We use the recent_tool_signatures ring buffer here so
        the lookback window is bounded.
        """

        signature = (fn_name, self._arg_fingerprint(args))
        buf = self._recent_tool_signatures
        buf.append(signature)
        if len(buf) > 24:
            del buf[: len(buf) - 24]
        recent = buf[-6:]
        same_calls = sum(1 for sig in recent if sig == signature)
        if same_calls >= 3:
            log.warning(
                "assistant.tool_loop_pattern name=%s arg_fp=%s repeats_in_last_6=%d total_calls=%d",
                fn_name,
                signature[1],
                same_calls,
                tool_calls_made_count,
            )


    def _log_budget_exit(
        self,
        reason: str,
        *,
        tool_calls_made_count: int,
        current_text_len: int,
    ) -> None:
        log.warning(
            "assistant.budget_exit reason=%s tool_count=%d unfinished_todos=%d current_text_len=%d "
            "max_agent_rounds=%d max_auto_continue=%d max_toolless=%d max_budget_restarts=%d",
            reason,
            int(tool_calls_made_count),
            len(self._unfinished_todos()),
            int(current_text_len),
            self._max_agent_rounds(),
            self._max_auto_continue(),
            self._max_toolless_continue_decisions(),
            self._max_budget_restarts(),
        )


    def _assistant_setting_int(self, attr: str, default: int, lo: int, hi: int) -> int:
        api = getattr(self.registry, "api", None)
        try:
            settings = api.settings_mgr.get() if api is not None else None
            value = getattr(settings, attr, default)
            if value is None or value == "":
                return default
            return max(lo, min(hi, int(value)))
        except Exception:
            return default


    def _turn_deadline_seconds(self) -> int:
        # This is an emergency boundary, not a task-completion budget. Long
        # investigations can legitimately run for hours; progress and loop
        # guards still apply independently. ``0`` explicitly disables this
        # wall-clock boundary for a user-managed session.
        override = getattr(self, "_turn_deadline_override_seconds", None)
        if override is not None:
            try:
                return max(1, min(int(override), 604800))
            except (TypeError, ValueError):
                pass
        return self._assistant_setting_int(
            "assistant_turn_deadline_seconds", 604800, 0, 604800,
        )


    def _auto_compact_ratio(self) -> float:
        try:
            api = getattr(self.registry, "api", None)
            settings = api.settings_mgr.get() if api is not None else None
            value = getattr(settings, "assistant_auto_compact_ratio", 0.8)
            return max(0.0, min(1.0, float(value or 0.0)))
        except Exception:
            return 0.8


    def _thinking_budget_tokens(self) -> int:
        return self._assistant_setting_int(
            "assistant_thinking_budget_tokens", 1024, 256, 64000,
        )

    def _thinking_effort(self) -> str:
        from argusai.core.ai_thinking import DEFAULT_EFFORT, normalize_thinking_effort

        raw = getattr(self, "thinking_effort", None)
        if raw:
            return normalize_thinking_effort(raw)
        try:
            api = getattr(self.registry, "api", None)
            settings = api.settings_mgr.get() if api is not None else None
            return normalize_thinking_effort(
                getattr(settings, "assistant_thinking_effort", DEFAULT_EFFORT)
            )
        except Exception:
            return DEFAULT_EFFORT


    def _arm_turn_deadline(self) -> None:
        secs = self._turn_deadline_seconds()
        self._turn_deadline_at = (time.time() + secs) if secs > 0 else None


    def _turn_deadline_exceeded(self) -> bool:
        deadline = getattr(self, "_turn_deadline_at", None)
        return bool(deadline and time.time() >= float(deadline))


    def _max_agent_rounds(self) -> int:
        # Kept for legacy diagnostics and dead code paths. Zero now means that
        # framework state/progress guards own termination.
        return 0


    def _max_auto_continue(self) -> int:
        return 0


    def _max_toolless_continue_decisions(self) -> int:
        # A no-tool model response is authoritative in the standard
        # function-calling contract. Progress-based Todo continuation is
        # handled separately.
        return 0


    def _max_budget_restarts(self) -> int:
        return 0


    def _agent_round_iter(self):
        # No fixed model/tool round count (Loom22 §10). Each iteration still
        # passes through _tool_loop_guard, which enforces cancellation,
        # deadline, exact-round deadlock and result-aware progress checks.
        return itertools.count()


    def _enter_round(self, round_index: int, tool_calls_made: list[dict[str, Any]] | None) -> int:
        """Bookkeeping wrapper called at the top of each agent-loop iteration.

        Returns ``time.monotonic_ns()`` so the caller can compute the
        round duration and pass it back to :meth:`_log_round_end`. We log
        the start at INFO level so the user-facing rotating log captures
        every iteration; the assistant_loop file keeps the full ladder
        for forensics.
        """

        try:
            self._log_round_start(int(round_index) + 1, len(tool_calls_made or []))
            remaining = None
            deadline = getattr(self, "_turn_deadline_at", None)
            if deadline:
                remaining = max(0, int(float(deadline) - time.time()))
            _assistant_debug_log(
                f"[agent-debug] event=round_start round={int(round_index) + 1} "
                f"tools_so_far={len(tool_calls_made or [])} "
                f"deadline_remaining={remaining} "
                f"subagent_depth={int(getattr(self, '_subagent_depth', 0) or 0)}"
            )
        except Exception as exc:
            log.debug("round_start logging failed: %s", exc)
        return time.monotonic_ns()


    def _commit_turn_deadline_stop(
        self,
        *,
        tool_calls_made: list[dict[str, Any]],
        tc_list: list[dict[str, Any]] | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        secs = self._turn_deadline_seconds()
        reason = (
            f"本回合已达到墙钟截止时间（{secs}s）。"
            "请选择继续、调整计划，或停止本轮。"
        )
        if tc_list is not None and messages is not None:
            self._fill_missing_tool_results(tc_list, messages, reason="turn_deadline")
        log.warning(
            "assistant.turn_deadline_stop turn=%s seconds=%s tools=%d",
            self._current_turn_id, secs, len(tool_calls_made or []),
        )
        _assistant_debug_log(
            f"assistant.turn_deadline_stop turn={self._current_turn_id} seconds={secs}"
        )
        return self._commit_auto_limit_decision_stop(
            reason=reason, current_text="", tool_calls_made=tool_calls_made,
        )


    def _leave_round(
        self,
        *,
        round_index: int,
        start_ns: int,
        tool_calls_made: list[dict[str, Any]] | None,
        had_tool_calls: bool,
        text: str,
        reason: str,
    ) -> None:
        try:
            elapsed_ms = max(0, (time.monotonic_ns() - int(start_ns)) // 1_000_000)
            self._log_round_end(
                int(round_index) + 1,
                tool_calls_made_count=len(tool_calls_made or []),
                elapsed_ms=int(elapsed_ms),
                had_tool_calls=bool(had_tool_calls),
                text_len=len(text or ""),
                reason=reason or "-",
            )
        except Exception as exc:
            log.debug("round_end logging failed: %s", exc)


    def _auto_continue_allowed(self, count: int) -> bool:
        del count
        if self._turn_deadline_exceeded():
            self._auto_continue_stalled = True
            return False
        signature = self._auto_continue_progress_signature()
        previous = getattr(self, "_auto_continue_progress_sig", None)
        if previous is not None and signature == previous:
            self._auto_continue_stalled = True
            return False
        self._auto_continue_progress_sig = signature
        self._auto_continue_stalled = False
        return True


    def _auto_continue_exhausted(self, count: int) -> bool:
        del count
        return bool(getattr(self, "_auto_continue_stalled", False))


    def _auto_continue_progress_signature(self) -> str:
        """Summarize durable Todo/tool progress for continuation gating."""
        todos = []
        try:
            for item in self._flatten_todos(getattr(self, "todos", [])):
                todos.append({
                    "id": item.get("id"),
                    "status": item.get("status") or "pending",
                    "validation_status": item.get("validation_status"),
                    "evidence_ref": item.get("evidence_ref"),
                })
        except Exception:
            todos = []
        outcomes = list(getattr(self, "_recent_tool_outcomes", None) or [])
        tail = outcomes[-1] if outcomes else {}
        return json.dumps({
            "todos": todos,
            "tool_count": len(outcomes),
            "last_tool": tail.get("name"),
            "last_result": tail.get("result_fp"),
            "last_ok": tail.get("ok"),
        }, ensure_ascii=False, sort_keys=True, default=str)


    def _toolless_continue_allowed(self, count: int) -> bool:
        del count
        return False


    def _toolless_continue_exhausted(self, count: int) -> bool:
        del count
        return False


    def _budget_restart_allowed(self, count: int) -> bool:
        del count
        return False


    def _budget_restart_exhausted(self, count: int) -> bool:
        del count
        return False


    def _tool_result_keep_last(self) -> int:
        budget_tokens = self._automatic_input_budget_tokens()
        if budget_tokens <= 32 * 1024:
            return 6
        if budget_tokens <= 64 * 1024:
            return 10
        if budget_tokens <= 128 * 1024:
            return 15
        return 20


    def _tool_result_summary_cap(self) -> int:
        budget_tokens = self._automatic_input_budget_tokens()
        if budget_tokens <= 32 * 1024:
            return 300
        if budget_tokens <= 64 * 1024:
            return 400
        return 500


    def _automatic_input_budget_tokens(self) -> int:
        """Derive the prompt budget from the active endpoint context window."""
        context_tokens = 131072
        try:
            resolver = getattr(self, "_context_window_tokens", None)
            if callable(resolver):
                context_tokens = max(4096, int(resolver()))
        except Exception:
            context_tokens = 131072
        return max(4096, int(context_tokens * 0.70))


    def _auto_archive_tool_names(self) -> set[str]:
        if self.auto_archive_tools_override is not None:
            return set(self._normalize_tool_names(self.auto_archive_tools_override))
        return self._settings_auto_archive_tool_names()


    def _settings_auto_archive_tool_names(self) -> set[str]:
        api = getattr(self.registry, "api", None)
        try:
            settings = api.settings_mgr.get() if api is not None else None
            names = getattr(settings, "assistant_auto_archive_tools", None)
            if isinstance(names, list):
                return set(self._normalize_tool_names(names))
        except Exception:
            pass
        return set(self._normalize_tool_names(list(_DEFAULT_AUTO_ARCHIVE_TOOL_RESULTS)))


    def _build_budget_decision_prompt(self, rounds_used: int) -> str:
        """Hidden user-role prompt asked when the per-turn round cap is hit.

        We do NOT decide for the model. Instead we phrase the situation
        explicitly and let the model itself choose between two paths:

        * 继续: emit fresh tool calls; the runtime will restart the loop.
        * 收尾: reply with plain text only; that text becomes the answer.
        """
        unfinished = self._unfinished_todos()
        todo_block = ""
        if unfinished:
            lines = []
            for t in unfinished[:20]:
                status = t.get("status") or "pending"
                todo_id = (t.get("id") or "").strip()
                content = (t.get("content") or t.get("id") or "").strip()
                done_when = (t.get("done_when") or "").strip()
                parent_id = (t.get("parent_id") or "").strip()
                trigger = (t.get("trigger") or "").strip()
                suffix = f" | done_when: {done_when}" if done_when else ""
                if parent_id:
                    suffix += f" | parent: {parent_id}"
                if trigger:
                    suffix += f" | trigger: {trigger}"
                lines.append(f"- [{status}] {todo_id}: {content}{suffix}")
            more = f"\n... 另有 {len(unfinished) - 20} 项未展示" if len(unfinished) > 20 else ""
            todo_block = f"\n当前未完成的 todo（共 {len(unfinished)} 项，展示前 20 项）：\n" + "\n".join(lines) + more + "\n"
        return (
            f"系统提示（不是用户发的）：本回合已经连续执行 {rounds_used} 次工具调用，"
            f"触达单回合软上限 {rounds_used}。"
            f"{todo_block}\n"
            "请你自己判断接下来怎么办，二选一：\n\n"
            "选项 A — 继续推进：如果你认为剩下的事确实必须继续做（例如 todo 还没完、"
            "正在中间状态、再做几步就能拿到结论），请直接发出下一步要执行的工具调用，"
            "不要写解释、不要写「我将继续」。一旦你输出 tool_calls，系统会自动重置"
            "本轮计数器并让你继续干，最多再重启几次。\n\n"
            "选项 B — 收尾结束：如果你认为继续投入的边际收益不大，或者当前结果已经足够"
            "回答用户，请用一段简洁的中文给出最终结论（看到了什么、得出什么结论、"
            "建议的下一步），不要再调用任何工具、也不要重复原始数据。\n\n"
            "判断标准提示：盲注/枚举类按字节循环很烧轮次，优先考虑改用 UNION / "
            "GROUP_CONCAT / CAST 报错回显 / 一次性导出全部行；类似的也别再用 N 轮 "
            "agent loop 跑可以在单个 run_python_snippet 里完成的内层循环。"
        )


    def _build_auto_continue_prompt(self) -> str:
        """Build the hidden user-role prompt used when auto-continuing the turn.

        We do NOT infer model intent — the model itself manages the todo list
        via ``set_todos`` / ``update_todo``. As long as it leaves todos in
        ``pending`` or ``in_progress``, we ask it to keep going. To stop the
        auto-continuation it just has to mark the remaining items
        ``completed`` / ``blocked`` / ``cancelled``.
        """
        unfinished = self._unfinished_todos()
        # Same "do not acknowledge" contract as the toolless gate above:
        # this is an internal nudge, not a user message. The model must
        # advance the task or close the todos — not narrate it.
        if not unfinished:
            return (
                "[INTERNAL CONTINUATION NUDGE — do not acknowledge or quote.]\n"
                "你已停止输出工具调用，但本回合还未完成。请直接发出下一步 "
                "tool_calls 推进任务，不要写「让我…/明白/继续」之类的过场白；"
                "如果确实已经完全完成，请用 set_todos / update_todo 把所有项 "
                "标为 completed / blocked / cancelled，然后用一句话给出新的 "
                "实质结论（不要复述刚才已经写过的内容）。"
            )
        lines = []
        for t in unfinished[:20]:
            status = t.get("status") or "pending"
            todo_id = (t.get("id") or "").strip()
            content = (t.get("content") or t.get("id") or "").strip()
            done_when = (t.get("done_when") or "").strip()
            parent_id = (t.get("parent_id") or "").strip()
            trigger = (t.get("trigger") or "").strip()
            suffix = f" | done_when: {done_when}" if done_when else ""
            if parent_id:
                suffix += f" | parent: {parent_id}"
            if trigger:
                suffix += f" | trigger: {trigger}"
            lines.append(f"- [{status}] {todo_id}: {content}{suffix}")
        if len(unfinished) > 20:
            lines.append(f"... 另有 {len(unfinished) - 20} 项未展示；不要把未展示项视为已完成。")
        remaining_block = "\n".join(lines)
        return (
            f"[INTERNAL CONTINUATION NUDGE — do not acknowledge or quote.]\n"
            f"Todo 还有未完成的项目（共 {len(unfinished)} 项），系统自动让你继续推进：\n"
            f"{remaining_block}\n\n"
            "请直接 update_todo 把下一项切到 in_progress，然后立刻发出对应的 "
            "tool_calls — 不要写「明白/继续/让我…」之类的过场白，也不要复述刚才"
            "做了什么。只有当结果满足该项 done_when / 可验证完成条件时，才 "
            "update_todo 标为 completed 再开始下一项。如果某项确实无法完成，"
            "把它标为 blocked / cancelled 并简述原因——整张清单清零之前不要"
            "停下输出工具调用。"
        )


    def _build_toolless_continuation_prompt(self) -> str:
        # This message is INTERNAL — it is injected into ``messages`` with a
        # ``user`` role so the provider treats it as guidance, but it is never
        # shown in the chat UI. The model MUST NOT acknowledge it. Acknowledge-
        # ments produce visible meta-talk such as "明白。上一轮我已经回答了…
        # 任务已完成" that reads to the end user like the assistant is talking
        # to itself, since the user never asked anything in between.
        return (
            "[INTERNAL CONTINUATION GATE — do not acknowledge, do not quote, "
            "do not reference this message in your reply.]\n\n"
            "You stopped after tool results, so the runtime needs one final "
            "closure check. Pick exactly ONE of:\n\n"
            "A. There is a concrete next verification / probe / read / "
            "execute step. → Emit the next tool_calls. No preamble, no "
            "narration, no 'let me…'.\n"
            "B. The task is finished, OR the next step needs the user to "
            "choose / authorise / supply info. → Emit ONLY the final answer "
            "(or the explicit question for the user). Do NOT write 明白 / "
            "好的 / 我已完成 / 任务已完成 / 上一轮我已… / I already answered / "
            "no further action needed, and do NOT restate what you did last "
            "round. If your previous visible message was already the final "
            "answer, this turn must either call a tool or stay empty — pick "
            "B only when you have a NEW concrete answer or a NEW question for "
            "the user. A progress report is NOT a final answer: if the todo "
            "plan just completed, synthesize its evidence into the concrete "
            "result or instructions the user requested.\n\n"
            "Never repeat a tool you just executed. Never produce a status "
            "line whose only content is 'I'm done'."
        )


    def _normalize_todos(self, items: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        if not isinstance(items, list):
            return normalized
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            normalized.append(self._normalize_todo(item, fallback_index=index))
        return normalized


    def _normalize_todo(self, item: dict[str, Any], depth: int = 0, fallback_index: int | None = None) -> dict[str, Any]:
        valid = self._TODO_STATUS_VALUES
        fallback = f"todo-{fallback_index or len(self._flatten_todos(self.todos)) + 1}"
        raw_title = item.get("title") or item.get("content") or item.get("id") or fallback
        title = str(raw_title).strip()[:180] or fallback
        todo_id = str(item.get("id") or title or fallback).strip()[:120]
        content = str(item.get("content") or title or todo_id).strip()[:1000]
        status = str(item.get("status") or "pending").strip()
        if status not in valid:
            status = "pending"
        normalized: dict[str, Any] = {"id": todo_id, "title": title, "content": content, "status": status}

        for key, limit in (
            ("kind", 80),
            ("phase", 80),
            ("risk", 40),
            ("target_ref", 240),
            ("tool_hint", 120),
            ("done_when", 1000),
            ("evidence", 1000),
            ("parent_id", 120),
            ("trigger", 1000),
            ("blocked_reason", 1000),
        ):
            value = str(item.get(key) or "").strip()
            if value:
                normalized[key] = value[:limit]

        for key in ("finding_refs", "evidence_refs", "artifact_refs"):
            refs = item.get(key)
            if isinstance(refs, list):
                clean = []
                seen: set[str] = set()
                for ref in refs:
                    value = str(ref or "").strip()
                    if value and value not in seen and len(clean) < 50:
                        clean.append(value[:240])
                        seen.add(value)
                if clean:
                    normalized[key] = clean

        if depth == 0:
            children = item.get("children")
            if isinstance(children, list):
                normalized_children = [
                    self._normalize_todo(child, depth=1, fallback_index=idx)
                    for idx, child in enumerate(children, start=1)
                    if isinstance(child, dict)
                ]
                if normalized_children:
                    normalized["children"] = normalized_children
        return normalized


    def _flatten_todos(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flat: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            parent_id = str(item.get("id") or "")
            flat.append(item)
            children = item.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        if parent_id and not child.get("parent_id"):
                            child["parent_id"] = parent_id
                        flat.append(child)
        return flat


    def _find_todo_by_id(self, todo_id: str) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        needle = str(todo_id or "")
        if not needle:
            return None
        for item in self.todos:
            if item.get("id") == needle:
                return item, None
            children = item.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict) and child.get("id") == needle:
                        return child, item
        return None


    def _current_action_todo(self, items: list[dict[str, Any]]) -> dict[str, Any] | None:
        flat = self._flatten_todos(items)
        for item in flat:
            if item.get("status") == "in_progress":
                return item
        for item in flat:
            if (item.get("status") or "pending") == "pending":
                return item
        return None


    def _enforce_single_in_progress(self, items: list[dict[str, Any]], active_id: str = "") -> list[dict[str, Any]]:
        seen_active = False
        for item in self._flatten_todos(items):
            if item.get("status") != "in_progress":
                continue
            if not seen_active and (not active_id or item.get("id") == active_id):
                seen_active = True
                continue
            item["status"] = "pending"
        return items


