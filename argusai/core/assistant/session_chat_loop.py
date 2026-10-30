"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import hashlib
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
from .ui_protocol import (
    normalize_selection_reference,
    selection_reference_to_prompt_text,
)

log = logging.getLogger("argusai.assistant")


def _pre_call_status_label(thinking_enabled: bool | None) -> str:
    """Status shown right before an AI call, before any stream chunk arrives.

    Only say "思考" when reasoning is actually enabled. Otherwise the label
    lies — it shows "正在思考" even though the user turned thinking off, and the
    later frontend "正在等待模型响应" stall hint makes it look like the model
    *finished* thinking when in reality it just hadn't started streaming yet.
    """
    return "正在思考…" if thinking_enabled else "正在请求模型…"

class SessionChatLoopMixin:
    def _activate_discovered_tool_schemas(
        self,
        active_names: list[str],
        current_tools: list[dict[str, Any]],
        tool_traces: list[dict[str, Any]],
        cursor: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Do not splice discovered schemas into tools[] — that busts prefix cache.

        Anthropic loads deferred tools via tool_reference in the conversation;
        Codex/OpenAI keep the tools array byte-stable. Discovered catalogue
        tools are invoked with ``invoke_catalogue_tool`` using the schema
        already in this tool_search result.
        """
        _ = active_names
        _ = cursor
        return current_tools, len(tool_traces)

    def _core_prompt_tool_names(self, selected: list[str] | None = None) -> list[str]:
        """Stable schemas that stay attached for the whole session.

        Keyword-matched domain groups are NOT part of the core — send those
        schemas in ``tool_search`` results and run them with
        ``invoke_catalogue_tool``. ``selected`` is accepted for call-site
        compatibility.
        """
        _ = selected
        names: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            text = str(name or "").strip()
            if not text or text in seen or self.registry.get(text) is None:
                return
            names.append(text)
            seen.add(text)

        for name in ALWAYS_LOADED_TOOLS:
            add(name)
        for name in ALWAYS_LOADED_AGENT_TOOLS:
            add(name)
        for name in ALWAYS_LOADED_CRON_TOOLS:
            add(name)
        if bool(getattr(self, "allow_web_search", True)):
            for name in ALWAYS_LOADED_WEB_TOOLS:
                add(name)
        binding = getattr(self, "runtime_binding", None)
        if (
            str(getattr(binding, "workspace_mode", "") or "") == "project"
            and str(getattr(binding, "project_dir", "") or "").strip()
        ):
            for name in ALWAYS_LOADED_PROJECT_TOOLS:
                add(name)
        return names

    def _restore_prompt_tool_partition(self, payload: dict[str, Any] | None = None) -> None:
        """Keep a frozen core tools[] after load; drop legacy extra schemas."""
        _ = payload
        core = self._core_prompt_tool_names()
        self._prompt_tool_names = list(core)
        self._prompt_tool_core_len = len(core)
        self._prompt_tool_flex_lru = []
        self._prompt_tool_flex_used = set()

    def _merge_prompt_tool_names(self, selected: list[str], *, used: bool = False) -> list[str]:
        """Seed the session tools[] once; never grow or shrink it.

        OpenAI/Codex prefix cache requires a byte-identical tools array.
        Extra catalogue tools are reached through ``invoke_catalogue_tool``.
        """
        _ = used
        frozen = list(getattr(self, "_prompt_tool_names", None) or [])
        if frozen:
            return list(frozen)
        core = self._core_prompt_tool_names(selected)
        self._prompt_tool_names = list(core)
        self._prompt_tool_core_len = len(core)
        self._prompt_tool_flex_lru = []
        self._prompt_tool_flex_used = set()
        return list(core)

    def _frozen_system_catalog(self, tool_names: list[str]) -> str:
        """Snapshot the system ## Tools block once so later additions stay in the API list only."""
        catalog = str(getattr(self, "_prompt_system_catalog", "") or "")
        if catalog:
            return catalog
        catalog = self.registry.catalog(tool_names)
        self._prompt_system_catalog = catalog
        return catalog

    def _note_prompt_prefix(self, prefix: str) -> None:
        digest = hashlib.sha256(prefix.encode("utf-8", errors="ignore")).hexdigest()[:16]
        previous = str(getattr(self, "_prompt_prefix_sha", "") or "")
        if previous and previous != digest:
            log.info("assistant.prompt_prefix_changed prev=%s now=%s", previous, digest)
        self._prompt_prefix_sha = digest

    def _user_text_for_tool_selection(self, messages: list[dict[str, Any]]) -> str:
        from .engagement import is_greenlight_seed_text

        parts: list[str] = []
        for msg in messages:
            if (msg or {}).get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                if is_greenlight_seed_text(content):
                    continue
                parts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = str(item.get("text") or "").strip()
                        if text:
                            parts.append(text)
                    elif isinstance(item, str) and item.strip():
                        parts.append(item)
        return "\n".join(parts)

    def _continue_tool_state(
        self,
        messages: list[dict[str, Any]],
        tool_calls_made: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Rebuild the progressive tool list after a confirmation pause.

        Do not fall back to the full catalogue: local models (and several
        hosted ones) reject or degrade when handed every schema at once.
        tools[] is seeded once and kept byte-identical for prefix cache
        (OpenAI/Codex). Discovered tools stay in the tool_search result and
        are run with ``invoke_catalogue_tool``.
        """
        if not getattr(self, "_prompt_tool_names", None):
            selected = _select_tool_names(
                "assistant",
                self._user_text_for_tool_selection(messages),
                allow_web_search=bool(getattr(self, "allow_web_search", True)),
            )
            selected = apply_project_workspace_tools(self, selected)
            self._merge_prompt_tool_names(selected)
        active_names = list(self._prompt_tool_names)
        tools = self.registry.to_openai_tools(active_names)
        tools, _ = self._activate_discovered_tool_schemas(
            active_names,
            tools,
            tool_calls_made,
            0,
        )
        return list(self._prompt_tool_names), tools

    def _workspace_capability_note(self) -> str:
        """Tell the model which directory this conversation actually works in."""
        binding = getattr(self, "runtime_binding", None)
        api = getattr(getattr(self, "registry", None), "api", None)
        session_id = str(getattr(getattr(self, "registry", None), "session_id", "") or "default")
        sandbox = ""
        if api is not None:
            try:
                from argusai.core.tools.common import _session_sandbox_root

                sandbox = str(_session_sandbox_root(api, {"_session_id": session_id}))
            except Exception:
                sandbox = ""
        mode = str(getattr(binding, "workspace_mode", "sandbox") or "sandbox")
        project = str(getattr(binding, "project_dir", "") or "").strip() if binding is not None else ""
        if mode == "project" and project:
            return (
                "\n## Workspace\n"
                f"- Mode: **local project**. The working directory and codebase scope is `{project}`.\n"
                "- Read, search, patch, and run in that directory with `codebase_*` tools "
                "(`codebase_tree`, `codebase_search`, `codebase_read`, `codebase_run_shell_snippet`). "
                "Do not ask the user to set scope; it is already bound to this conversation.\n"
                "- Host files can be read anywhere via `stat_text_file` / `grep_text` / "
                "`read_text_file` / `vision_inspect_image` / `codebase_read`. Do not copy "
                "them into the scratch sandbox just to look.\n"
                + (
                    f"- Session scratch sandbox (generated artifacts, snippets, venv) is `{sandbox}`. "
                    "Use `sandbox_*` for intermediate and generated work, not as a read jail.\n"
                    if sandbox
                    else ""
                )
            )
        if sandbox:
            return (
                "\n## Workspace\n"
                f"- Mode: **isolated sandbox**. Generated artifacts and scratch files go in `{sandbox}`.\n"
                "- Use `sandbox_*` / `run_shell_snippet` for intermediate and generated work. "
                "Call `codebase_scope_set` only if the user asks to work on a host project.\n"
                "- Host files are not sandbox-only: read any path with "
                "`stat_text_file` / `grep_text` / `read_text_file` / `vision_inspect_image`. "
                "Risky writes still go through the existing confirmation card.\n"
            )
        return ""

    def _rehydrate_video_attachments(
        self,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Read spilled video bytes back for the turn that is about to run.

        History deliberately stores videos as a sandbox path (see
        ``_materialize_video``). Only the newest user turn gets its bytes
        inlined again, so a video-capable model receives the clip exactly once
        instead of on every later request. Cached per turn because the tool
        loop rebuilds the message list on each round.
        """
        if not any(
            (att.get("type") or "").lower() == "video"
            and not att.get("data_base64")
            and (att.get("sandbox_path") or att.get("host_path") or att.get("path"))
            for att in attachments
        ):
            return attachments

        import base64
        from pathlib import Path

        cache = getattr(self, "_video_rehydration_cache", None)
        if cache is None:
            cache = {}
            self._video_rehydration_cache = cache

        out: list[dict[str, Any]] = []
        for att in attachments:
            path = str(att.get("host_path") or att.get("path") or att.get("sandbox_path") or "")
            if (att.get("type") or "").lower() != "video" or att.get("data_base64") or not path:
                out.append(att)
                continue
            data = cache.get(path)
            if data is None:
                try:
                    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
                except Exception:
                    log.info("assistant.video_rehydrate_failed path=%s", path, exc_info=True)
                    data = ""
                cache[path] = data
            out.append({**att, "data_base64": data} if data else att)
        return out

    async def chat(
        self,
        message: str,
        page: str = "",
        context: dict[str, Any] | None = None,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], Awaitable[None] | None] | None = None,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_segment: Callable[[int], Awaitable[None] | None] | None = None,
        on_checkpoint: Callable[[], Awaitable[None] | None] | None = None,
        regenerate: bool = False,
        regenerate_before: float | None = None,
        regenerate_user_before: float | None = None,
        thinking_enabled: bool | None = None,
        attachments: list[dict[str, Any]] | None = None,
        voice: dict[str, Any] | None = None,
        selection_reference: dict[str, Any] | None = None,
        sandbox_root: Path | str | None = None,
        hide_user_message: bool = False,
        confirmation_action: bool = False,
        confirmation: dict[str, Any] | None = None,
        message_source: str = "",
        message_source_ref: str = "",
        thinking_effort: str | None = None,
    ) -> ChatResponse:
        import time as _time
        if thinking_effort is not None:
            from argusai.core.ai_thinking import normalize_thinking_effort

            normalized = normalize_thinking_effort(thinking_effort, default="")
            if normalized:
                self.thinking_effort = normalized
        self._cancel_requested = False
        self._pause_after_current_response = False
        self._todos_touched_this_turn = False
        self._user_decision_requested = False
        self._team_room_passed = False
        # Model-authored presentation cards are deduped per turn at the source:
        # if the model re-calls show_structured_card with identical content in
        # the same turn (a common agent-loop repeat), the second call is a
        # no-op and the duplicate block never reaches history. Reset each turn
        # so re-showing a card in a *later* turn stays legitimate.
        self._emitted_card_signatures: set[str] = set()
        # Make the checkpoint callback accessible to the rest of the
        # method without threading it through every helper. Stashed on
        # the instance so :py:meth:`_emit_checkpoint` (called from many
        # tool-loop exit points) can find it. Reset every turn so a
        # previous turn's callback can't leak into a new chat.
        self._on_checkpoint = on_checkpoint
        # Stashed so ``_call_ai`` can surface a live "正在推理…" heartbeat while
        # a reasoning model (DeepSeek V4 / Anthropic) streams its thinking
        # channel — that phase emits no visible text, so without this the
        # bubble looks frozen until the answer finally starts. Reset each turn.
        self._on_status = on_status
        # Diagnostic counters / IDs used by the self-loop logging helpers.
        # Reset every user turn so each conversation starts with a clean
        # ring buffer and a fresh, sortable turn identifier.
        self._current_turn_id = f"t{int(_time.time() * 1000)}"
        self._current_round_index = 0
        self._recent_tool_signatures = []
        self._stream_truncated_this_round = False
        self._reset_agent_loop_stall_tracking()
        self._arm_turn_deadline()
        self._acceptance_repair_count = 0
        self._pending_child_agent_notice_ids = []
        # Per-turn cache/token usage, reset here and accumulated by
        # ``_record_token_usage``. Attached to the turn's final visible
        # assistant ChatMessage so the "Token" button under a single reply
        # can show that reply's own cost instead of the whole session's.
        self._turn_token_usage: dict[str, int] = {}
        log.info(
            "assistant.turn_start turn=%s message_chars=%d page=%s deadline=%s",
            self._current_turn_id,
            len(message or ""),
            page or "-",
            getattr(self, "_turn_deadline_at", None),
        )

        attachments = _sanitize_attachments(attachments)
        self._video_rehydration_cache = {}
        voice_payload = _sanitize_voice(voice)
        enrichment_attachments = list(attachments)
        if voice_payload:
            enrichment_attachments.insert(0, {**voice_payload, "origin": "voice"})
        # Spill text and inert generic binary attachments to the session
        # sandbox. Binary bytes never enter the model request; the prompt gets
        # only a relative path, size, and digest.
        attachments, attachment_addendum = _materialize_text_attachments(enrichment_attachments, sandbox_root)
        attachments, binary_addendum = _materialize_binary_attachments(attachments, sandbox_root)
        if attachment_addendum:
            message = (message or "").rstrip() + attachment_addendum
        if binary_addendum:
            message = (message or "").rstrip() + binary_addendum

        # Persisted history must keep the ORIGINAL user-supplied attachments
        # (post-text-materialize, pre-vision/ASR-enrichment). Two reasons:
        #
        #   1. Reloading a session in the UI should still show the image
        #      thumbnail / audio bubble the user actually sent.
        #   2. If vision/ASR aux was off when the message was sent, the user
        #      can later re-reference the same image from the sidebar and the
        #      raw data is still there to re-enrich.
        #
        # Voice rides on its own ChatMessage.voice field, not in attachments.
        history_attachments = [
            {k: v for k, v in att.items() if k != "origin"}
            for att in attachments
            if not (
                (att.get("type") or "").lower() == "audio"
                and att.get("origin") == "voice"
            )
        ]

        try:
            api = getattr(self.registry, "api", None)
            if api is not None:
                from .media_enrichment import enrich_user_attachments

                app_settings = api.settings_mgr.get()
                message, attachments = await enrich_user_attachments(
                    app_settings,
                    message,
                    attachments,
                )
        except Exception:
            log.exception("assistant.media_enrichment_failed turn=%s", self._current_turn_id)

        # Keyframes sampled out of a video the model can't watch natively are
        # the only thing enrichment adds to history: they ARE what the model
        # sees, so the transcript must show them too.
        history_attachments.extend(
            {k: v for k, v in att.items() if k != "origin"}
            for att in attachments
            if att.get("derived") == "video_frame"
        )
        # Videos are one-shot: the bytes go to the sandbox and history keeps
        # only the path, otherwise every later turn would re-upload tens of MB
        # of base64. _build_llm_messages rehydrates the newest user turn.
        history_attachments = _materialize_video(history_attachments, sandbox_root)

        if voice_payload:
            attachments = [att for att in attachments if (att.get("type") or "").lower() != "audio"]
            if not str(message or "").strip():
                sec = max(1, int(int(voice_payload.get("duration_ms") or 1000) / 1000))
                message = f"[语音消息 · {sec}秒]"
            voice_payload = _materialize_voice(voice_payload, sandbox_root)

        # Selection reference is structured metadata (AI UI Protocol v2 §5.1).
        # It rides alongside the user message — never mixed into ``content`` —
        # and is rendered into a prompt block only at LLM-call time below.
        selection_reference = normalize_selection_reference(selection_reference) if selection_reference else None
        msg_source = str(message_source or "").strip().lower()
        msg_source_ref = str(message_source_ref or "").strip()
        self._current_turn_source = msg_source
        self._current_turn_source_ref = msg_source_ref
        is_runtime_result = msg_source == "cron_result"
        is_scheduled_input = msg_source == "cron"
        # A turn woken by finished child Agents is UI-hidden like cron's, but it
        # is an ordinary turn otherwise: full history, full confirmation gating,
        # and no automatic todo-plan approval.
        is_agent_wake = msg_source == "agent_completion"

        todo_plan_choice = ""
        if (
            not is_scheduled_input
            and not is_runtime_result
            and self.todos
            and not self._todos_plan_approved
            and str(message or "").strip()
        ):
            todo_plan_choice = self._classify_todo_plan_choice(message)

        if todo_plan_choice == "cancel":
            import time as _time

            self.clear_todos()
            if not regenerate:
                if hide_user_message and not confirmation_action and msg_source != "cron":
                    self._mark_latest_user_decision_submitted(message)
                self.history.append(ChatMessage(
                    role="user",
                    content=message,
                    attachments=history_attachments,
                    voice=voice_payload,
                    selection_reference=selection_reference,
                    ui_hidden=bool(hide_user_message) or msg_source == "cron",
                    source=msg_source,
                    source_ref=msg_source_ref,
                    timestamp=_time.time(),
                ))
            ack = "已取消当前任务计划。你可以随时输入新的指令。"
            self.history.append(ChatMessage(
                role="assistant",
                content=ack,
                blocks=self._blocks_for_text(ack),
                timestamp=_time.time(),
                token_usage=self._turn_token_usage_snapshot(),
            ))
            return ChatResponse(
                text=ack,
                blocks=self._blocks_for_text(ack),
                status="ok",
            )

        if todo_plan_choice in {"approve", "modify"}:
            message = self._apply_todo_plan_choice(todo_plan_choice, message)

        # Handle pending confirmation. Loom22 §9: only a typed confirmation
        # RPC (``confirmation_action`` + structured ``confirmation``) may
        # consume ``_pending``. Legacy ``confirm_batch:`` / ``yes`` chat
        # strings are migrated into the same shape and never trusted alone
        # without the UI flag.
        if self._pending:
            if (is_scheduled_input or is_runtime_result) and not confirmation_action:
                return ChatResponse(
                    text="Runtime event deferred: this session is waiting for tool confirmation.",
                    status="awaiting_confirmation",
                )
            typed_decision = None
            legacy_confirmation = False
            if confirmation_action:
                typed_decision = normalize_confirmation_decision(confirmation)
                if typed_decision is None:
                    typed_decision = confirmation_decision_from_legacy_message(message)
                    legacy_confirmation = typed_decision is not None
            if confirmation_action and typed_decision is not None:
                binding_error = self._validate_confirmation_binding(
                    typed_decision,
                    self._pending,
                    allow_legacy_unbound=legacy_confirmation,
                )
                if binding_error:
                    return ChatResponse(
                        text=binding_error,
                        status="awaiting_confirmation",
                    )
                if "confirmations" in self._pending:
                    return await self._handle_pending_batch(
                        message,
                        decision=typed_decision,
                        on_delta=on_delta,
                        on_status=on_status,
                        on_block=on_block,
                        on_segment=on_segment,
                        thinking_enabled=thinking_enabled,
                    )
                return await self._handle_pending(
                    message,
                    decision=typed_decision,
                    on_delta=on_delta,
                    on_status=on_status,
                    on_block=on_block,
                    on_segment=on_segment,
                    thinking_enabled=thinking_enabled,
                )
            # Typing in the input box is real user
            # input, not an implicit click on the confirmation card. Only a
            # UI/RPC-marked ``confirmation_action`` may consume ``_pending``;
            # even a literal "yes" typed by the user enters the normal agent
            # loop below.
            pending_kind = "batch" if "confirmations" in self._pending else "single"
            log.info(
                "assistant.pending_abandoned_by_user_input kind=%s turn=%s chars=%d",
                pending_kind,
                self._current_turn_id,
                len(message or ""),
            )
            pending_id, version = self._ensure_pending_identity()
            self._record_confirmation_decision(
                {
                    "type": "approval_abandoned",
                    "pending_id": pending_id,
                    "version": version,
                    "verdict": "abandoned_by_new_message",
                },
                self._pending,
                timestamp=_time.time(),
            )
            self._pending = None

        # Handle the in-session recovery flow from the repetition
        # watchdog. The watchdog committed a special user_decision card
        # in the previous turn and set ``_stream_repetition_marker``;
        # whatever the user types now may be one of the three preset
        # recovery options (self-correct / skip / pause). The session is
        # *not* destroyed — recovery just rewrites the user message into
        # a clear follow-up prompt that the rest of ``chat()`` then
        # processes normally. An unrecognised reply falls through and is
        # treated as an ordinary user message.
        if self._stream_repetition_marker:
            self._stream_repetition_marker = False
            self._user_decision_requested = False
            recovery_choice = self._classify_stream_repetition_choice(message)
            if recovery_choice == "pause":
                self.history.append(ChatMessage(
                    role="user",
                    content=message,
                    timestamp=_time.time(),
                ))
                ack = (
                    "好的，已暂停。你可以随时输入下一步指令，"
                    "或者贴新的提示让 AI 接着推进。"
                )
                self.history.append(ChatMessage(
                    role="assistant",
                    content=ack,
                    blocks=self._blocks_for_text(ack),
                    timestamp=_time.time(),
                    token_usage=self._turn_token_usage_snapshot(),
                ))
                log.warning(
                    "assistant.stream_repetition_recovery choice=pause turn=%s",
                    self._current_turn_id,
                )
                return ChatResponse(
                    text=ack,
                    blocks=self._blocks_for_text(ack),
                    status="paused_by_user",
                )
            if recovery_choice in {"self_correct", "skip"}:
                log.warning(
                    "assistant.stream_repetition_recovery choice=%s turn=%s",
                    recovery_choice,
                    self._current_turn_id,
                )
                message = self._build_repetition_recovery_prompt(recovery_choice)
                hide_user_message = True
            # Fall through: unrecognised reply becomes a normal turn.

        if regenerate:
            self._rewind_for_regenerate(
                message,
                regenerate_before,
                user_before_ts=regenerate_user_before,
            )
        else:
            if (
                hide_user_message
                and not confirmation_action
                and not is_scheduled_input
                and not is_runtime_result
                and not is_agent_wake
            ):
                self._mark_latest_user_decision_submitted(message)
            if not is_runtime_result:
                self.history.append(ChatMessage(
                    role="user",
                    content=message,
                    attachments=history_attachments,
                    voice=voice_payload,
                    selection_reference=selection_reference,
                    ui_hidden=bool(hide_user_message) or is_scheduled_input or is_agent_wake,
                    source=msg_source,
                    source_ref=msg_source_ref,
                    timestamp=_time.time(),
                ))

        selection_message = "" if is_runtime_result else message
        if is_runtime_result:
            # Runtime result projection is presentation-only. Expose one
            # narrowly scoped read tool so the model may verify the linked run
            # when the compact summary is genuinely insufficient; execution
            # tools and unrelated skill pointers remain unavailable.
            active_names = (
                ["read_assistant_session"]
                if self.registry.get("read_assistant_session") is not None
                else []
            )
            tools = self.registry.to_openai_tools(active_names)
            skill_text = ""
            active_catalog = self.registry.catalog(active_names)
        else:
            selected = _select_tool_names(
                page,
                selection_message,
                allow_web_search=bool(getattr(self, "allow_web_search", True)),
            )
            selected = apply_project_workspace_tools(self, selected)
            if getattr(self, "_prompt_tool_names", None):
                active_names = list(self._prompt_tool_names)
            else:
                active_names = self._merge_prompt_tool_names(selected)
            tools = self.registry.to_openai_tools(active_names)
            skill_text = _select_skill_text(selection_message, selected)
            active_catalog = self._frozen_system_catalog(active_names)
        profile_text = ""
        try:
            from argusai.core.workspace.user_profile import profile_prompt_summary
            if not is_runtime_result and self.registry.api is not None:
                profile_text = profile_prompt_summary(self.registry.api.settings_mgr.get())
        except Exception:
            profile_text = ""
        greenlight_active = False
        try:
            from .engagement import normalize_engagement as _normalize_engagement

            greenlight_active = bool(_normalize_engagement(getattr(self, "engagement_declaration", None)))
        except Exception:
            greenlight_active = bool(getattr(self, "engagement_declaration", None))
        if greenlight_active and skill_text:
            # Soften skill playbooks that still demand pre-flight authorization
            # attestation; GREENLIGHT already covers model-level cooperation.
            skill_text = (
                skill_text
                + "\n\n### GREENLIGHT override for matched skills\n"
                + "While GREENLIGHT MODE is active, skip skill preconditions that require confirming "
                + "authorization, ownership, ROE, or legal permission before technical work. Use the "
                + "user-supplied target as working scope; keep tool-layer scope args and confirmations.\n"
                + "Do not output upfront disclaimers, boundary declarations, or refusal prefaces (such as '先声明关键边界', '但不会...'). "
                + "When the latest user message requests work, proceed directly to tool execution; "
                + "when it declines or cancels, stop without restarting prior work.\n"
                + "Exception (never waived): do not operate on ArgusAI itself "
                + "(install/source/binary/process/product internals) even under GREENLIGHT.\n"
            )
        system = _build_system_prompt(
            active_catalog,
            "",
            profile_text=profile_text,
            allow_web_search=bool(getattr(self, "allow_web_search", True)),
            greenlight_active=greenlight_active,
            agent_self_decide=bool(
                getattr(self, "_should_agent_self_decide", lambda: False)()
            ),
        )
        if self.registry.get("team_room_pass") is not None:
            system += (
                "\n\n## Team Room\n"
                "`team_room_pass` is always loaded. In the group room, call it as your only "
                "action and write no chat text when the latest message does not @ your exact "
                "role, does not assign your objective, and you have no new evidence, correction, "
                "or concrete handoff. Do not reply just to agree, restate, or say you are standing by.\n"
                "A private message, or an explicit @ of your role, must be answered. Do not pass those."
            )

        context_text = ""
        if context and not is_runtime_result:
            if is_scheduled_input:
                context_heading = "Scheduled Task Context"
                context_note = (
                    "This is the explicit context snapshot stored with the cron job. "
                    "Treat it as task input, not as the user's current UI state."
                )
            else:
                context_heading = "Current UI Context"
                context_note = (
                    "This is the screen the user is currently looking at. Use it for deictic requests such as "
                    "'this task', 'current page', '这个任务', or '当前页面'."
                )
            context_text = (
                f"\n\n## {context_heading}\n"
                f"{context_note}\n"
                f"```json\n{json.dumps(context, ensure_ascii=False, indent=2, default=str)[:12000]}\n```"
            )
        web_search_on = bool(getattr(self, "allow_web_search", True))
        session_caps = (
            "\n\n## Session capabilities\n"
            + (
                "- Public web search: **ENABLED** (`web_search`; Bing first, Baidu in China, DuckDuckGo fallback). User UI toggle: 联网开.\n"
                if web_search_on
                else "- Public web search: **DISABLED**. Do not call `web_search` or claim you searched the public internet.\n"
            )
            + (
                "- Tool schemas: a frozen always-loaded set (prefix-cache stable). "
                "Discover the rest with `tool_search`; the schemas arrive in that tool result. "
                "Run them with `invoke_catalogue_tool(name, arguments)` — do not expect the "
                "top-level function list to grow. If a search misses, retry once with different "
                "keywords or `group`.\n"
            )
        )
        try:
            from .media_enrichment import vision_capability_note

            session_caps += vision_capability_note(self._current_app_settings()) + "\n"
        except Exception:
            log.debug("vision_capability_note failed", exc_info=True)

        # Tell the agent the host shell reality (PowerShell version, available
        # shells) so it writes valid command chaining on the first try instead
        # of discovering Windows PowerShell 5.1 rejects `&&` / `||` by failing.
        try:
            from argusai.core.workspace.session_sandbox import shell_capability_note

            default_shell = "auto"
            try:
                api = getattr(self.registry, "api", None)
                if api is not None:
                    default_shell = str(
                        getattr(api.settings_mgr.get(), "assistant_default_shell", "auto") or "auto"
                    )
            except Exception:
                pass
            shell_note = shell_capability_note(default_shell)
            if shell_note:
                session_caps += shell_note
        except Exception:
            log.debug("shell_capability_note failed", exc_info=True)

        try:
            workspace_note = self._workspace_capability_note()
            if workspace_note:
                session_caps += workspace_note
        except Exception:
            log.debug("workspace_capability_note failed", exc_info=True)

        try:
            engagement_note = self.engagement_prompt_note()
            if engagement_note:
                session_caps += engagement_note
        except Exception:
            log.debug("engagement_prompt_note failed", exc_info=True)

        if is_runtime_result:
            session_caps = ""

        # Dynamic MCP server roster. MCP child tools are NOT registered as
        # top-level tools (see Loom22 §5 / MCP plan), so the model has no
        # way to know "the user wired up a `可视化图表` server" unless we
        # tell it. Inject server names + transports only — env / headers /
        # tool catalogues stay behind the policy gate and only surface
        # through explicit `mcp_server_list_tools(server_id)` calls.
        mcp_text = "" if is_runtime_result else self._build_mcp_servers_prompt()
        memory_text = "" if is_runtime_result else self.memory_prompt(message)
        skill_note = ""
        if skill_text and not is_runtime_result:
            skill_note = (
                "\n\n## Relevant playbooks for this turn\n"
                "Pointers only — `skill_read` the matching playbook before executing.\n"
                + str(skill_text).strip()
            )
        # Loom22 §12.5 / Pi-Reasonix: system prefix is frozen; per-turn skill
        # pointers join UI snapshot and memory on the live user turn.
        system_prefix = system + session_caps + mcp_text
        if not is_runtime_result:
            self._note_prompt_prefix(system_prefix)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prefix}]
        try:
            from .engagement import detect_reply_language as _detect_reply_language

            language_source = str(message or "")
            if is_runtime_result:
                for prior in reversed(self.history):
                    if (
                        prior.role == "user"
                        and not prior.hidden
                        and prior.source not in {"cron", "cron_result", "confirmation"}
                    ):
                        language_source = prior.content
                        break
            _gl_lang = _detect_reply_language(language_source)
        except Exception:
            _gl_lang = "zh"
        pending_tool_ids: set[str] = set()
        history_window = [] if is_runtime_result else self.history[-self.MAX_HISTORY :]
        latest_user = next(
            (h for h in reversed(history_window) if h.role == "user" and not h.hidden),
            None,
        )
        for h in history_window:
            if h.hidden or h.source == "cron_result":
                continue
            if h.role == "tool":
                if not h.tool_call_id or h.tool_call_id not in pending_tool_ids:
                    continue
                msg = {"role": h.role, "content": h.content, "tool_call_id": h.tool_call_id}
                messages.append(msg)
                pending_tool_ids.discard(h.tool_call_id)
                continue
            if h.role == "user":
                pending_tool_ids.clear()
            content = h.content
            if h.role == "user" and h.selection_reference:
                # Render the structured reference into the legacy v1 prompt
                # block at the moment we hand the message to the LLM. The
                # persistence layer keeps ``content`` clean so the UI can show
                # the chip without re-parsing prose.
                ref_text = selection_reference_to_prompt_text(h.selection_reference)
                if ref_text:
                    content = ((content or "").rstrip() + "\n\n" + ref_text).strip()
            msg: dict[str, Any] = {"role": h.role, "content": content}
            if h.role == "user" and h.attachments:
                msg["attachments"] = (
                    self._rehydrate_video_attachments(h.attachments)
                    if h is latest_user
                    else h.attachments
                )
            if h.tool_calls:
                msg["tool_calls"] = h.tool_calls
                pending_tool_ids.update(tc.get("id") for tc in h.tool_calls if tc.get("id"))
            if h.provider_content:
                msg["provider_content"] = h.provider_content
                pending_tool_ids.update(self._provider_tool_use_ids(h.provider_content))
            if h.reasoning_content:
                msg["reasoning_content"] = h.reasoning_content
            if h.tool_call_id:
                msg["tool_call_id"] = h.tool_call_id
            messages.append(msg)

        if is_runtime_result:
            messages.append({
                "role": "system",
                "content": (
                    "## ArgusAI runtime event\n"
                    "This is a typed scheduled-task result from the application, not a user message. "
                    f"Explain the outcome concisely in {'Simplified Chinese' if _gl_lang == 'zh' else 'English'}. "
                    "The ordinary conversation history is intentionally excluded so an unrelated recent turn cannot "
                    "override this event. Treat the summary as a compact notification, not as an authoritative replacement "
                    "for the linked run transcript. Normally present it directly. Only if it is marked truncated, materially "
                    "ambiguous, or lacks facts needed for an accurate notification, call `read_assistant_session` with the "
                    "exact `run_session_id`; do not inspect every run routinely. Do not continue old work, call any other "
                    "tool, ask whether to inspect it, or claim the user sent this event.\n\n"
                    + str(message or "")[:8000]
                ),
            })
            # Anthropic-compatible transports extract every system message
            # into the top-level ``system`` field and require at least one
            # item to remain in ``messages``. This ephemeral instruction is
            # provider framing only: it is never persisted as user history,
            # and the typed event payload remains exclusively in system.
            messages.append({
                "role": "user",
                "content": "Present the typed runtime event above as the user-facing notification now.",
            })
        self._attach_volatile_context(messages, context_text + memory_text + skill_note)

        # Layer 1 of child-completion delivery: one compact synthetic message
        # per turn, appended after the volatile tail so it is the last thing the
        # model reads. Injected once here — never re-injected per agent round —
        # and only retired after the provider actually receives it below.
        if not is_runtime_result:
            notice_text, notice_agent_ids = self._pending_child_agent_notice()
            if notice_text:
                messages.append({"role": "user", "content": notice_text})
                self.history.append(ChatMessage(
                    role="user",
                    content=notice_text,
                    hidden=True,
                    source="agent_completion_notice",
                    timestamp=_time.time(),
                ))
                self._pending_child_agent_notice_ids = notice_agent_ids
                log.info(
                    "assistant.child_agent_notice_injected turn=%s agents=%d",
                    self._current_turn_id,
                    len(notice_agent_ids),
                )

        tool_calls_made: list[dict[str, Any]] = []
        discovery_trace_cursor = 0
        executed_keys: set[str] = set()
        auto_continue_count = 0
        toolless_decision_count = 0
        toolless_decision_pending = False
        budget_restart_count = 0
        result = ""
        terminal_status = "done"

        for _round in self._agent_round_iter():
            round_start_ns = self._enter_round(_round, tool_calls_made)
            await self._emit_agent_round_start(on_segment, _round)
            loop_guard = self._tool_loop_guard(
                messages=messages,
                tool_calls_made=tool_calls_made,
                tc_list=[],
            )
            if loop_guard is not None:
                return loop_guard
            if self._cancel_requested:
                self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_before_call")
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            if not self.ai:
                result = self._no_ai_response()
                self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text=result, reason="no_ai_provider")
                break

            try:
                await self._emit_status(on_status, _pre_call_status_label(thinking_enabled))
                response = await self._call_ai(messages, tools, on_delta=on_delta, thinking_enabled=thinking_enabled)
            except Exception as e:
                from argusai.core.ai import format_ai_failure

                log.error("AI call failed: %s", e)
                result = format_ai_failure(e)
                terminal_status = "ai_error"
                self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text=result, reason="ai_call_error")
                break
            self._flush_child_agent_notice_delivery()
            if self._cancel_requested:
                self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_after_call")
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            if self._stream_truncated_this_round:
                self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text=response.get("content", ""), reason="stream_repetition_stop")
                return self._commit_stream_repetition_stop(
                    response=response,
                    tool_calls_made=tool_calls_made,
                )

            tc_list = response.get("tool_calls") or []
            if tc_list:
                toolless_decision_pending = False
            if not tc_list:
                result = response.get("content", "")
                ts = _time.time()
                blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
                if (
                    not is_runtime_result
                    and self._should_auto_continue(result)
                    and self._auto_continue_allowed(auto_continue_count)
                    and not self._cancel_requested
                    and not self._pause_after_current_response
                ):
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
                    messages.append({"role": "user", "content": self._build_auto_continue_prompt()})
                    auto_continue_count += 1
                    self._log_auto_continue_trigger(auto_continue_count, result)
                    await self._emit_status(on_status, "Todo 未完成，自动继续推进...")
                    await self._emit_single_block(on_block, {"type": "todo_list", "mode": "assessment_plan", **self.list_todos()})
                    continue
                if (
                    not is_runtime_result
                    and self._should_auto_continue(result)
                    and self._auto_continue_exhausted(auto_continue_count)
                    and not self._cancel_requested
                    and not self._pause_after_current_response
                ):
                    await self._emit_status(on_status, "Todo 自动续跑未产生新进展，等待用户决定。")
                    return self._commit_auto_limit_decision_stop(
                        reason="Todo 仍未完成，但自动续跑后没有新的 Todo 状态、工具结果或证据进展。",
                        current_text=result,
                        tool_calls_made=tool_calls_made,
                    )
                if (
                    not is_runtime_result
                    and self._should_probe_toolless_continuation(result, tool_calls_made)
                    and self._toolless_continue_allowed(toolless_decision_count)
                    and not toolless_decision_pending
                    and not self._cancel_requested
                    and not self._pause_after_current_response
                ):
                    self.history.append(ChatMessage(
                        role="assistant",
                        content=result,
                        reasoning_content=response.get("reasoning_content") or "",
                        provider_content=list(response.get("provider_content") or []),
                        blocks=blocks,
                        timestamp=ts,
                        token_usage=self._turn_token_usage_snapshot(),
                    ))
                    messages.append({
                        "role": "assistant",
                        "content": result,
                        "reasoning_content": response.get("reasoning_content") or "",
                        "provider_content": response.get("provider_content") or [],
                    })
                    messages.append({"role": "user", "content": self._build_toolless_continuation_prompt()})
                    toolless_decision_count += 1
                    toolless_decision_pending = True
                    self._log_toolless_probe_trigger(toolless_decision_count, tool_calls_made, result)
                    await self._emit_status(on_status, "工具后未见明确收尾，正在让 AI 判断是否继续...")
                    continue
                if (
                    not is_runtime_result
                    and self._should_probe_toolless_continuation(result, tool_calls_made)
                    and self._toolless_continue_exhausted(toolless_decision_count)
                    and not toolless_decision_pending
                    and not self._cancel_requested
                    and not self._pause_after_current_response
                ):
                    await self._emit_status(on_status, "无工具续跑判断次数已用完，等待用户决定。")
                    return self._commit_auto_limit_decision_stop(
                        reason=f"工具后仍未明确收尾，但已达到无工具续跑判断次数上限 {self._max_toolless_continue_decisions()}",
                        current_text=result,
                        tool_calls_made=tool_calls_made,
                    )
                acceptance = None
                if not is_runtime_result:
                    acceptance = await self._run_acceptance_closed_loop(
                        result=result,
                        response=response,
                        messages=messages,
                        tool_calls_made=tool_calls_made,
                        blocks=blocks,
                        ts=ts,
                        on_status=on_status,
                        on_block=on_block,
                    )
                if acceptance is not None:
                    if acceptance.get("continue"):
                        continue
                    if acceptance.get("response") is not None:
                        return acceptance["response"]
                if not is_runtime_result:
                    result = self._append_active_child_agent_note(result)
                self.history.append(ChatMessage(
                    role="assistant",
                    content=result,
                    reasoning_content=response.get("reasoning_content") or "",
                    provider_content=list(response.get("provider_content") or []),
                    blocks=blocks,
                    timestamp=ts,
                    token_usage=self._turn_token_usage_snapshot(),
                ))
                return ChatResponse(text=result, tool_calls=tool_calls_made, blocks=blocks, tool_trace=tool_calls_made, status="done")
                break

            stopped = self._finish_if_implicit_user_question(response, tool_calls_made)
            if stopped is not None:
                if stopped.status == "awaiting_user_decision":
                    await self._emit_status(on_status, "已暂停，等待用户决定。")
                return stopped

            if self._pause_after_current_response:
                await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                return self._commit_pause_after_current_response(
                    response.get("content") or "",
                    response,
                    tool_calls_made,
                )

            # Collect every tool that still needs explicit permission so the
            # UI can render them as a batch and let the user confirm each
            # card without round-tripping to the model in between. Tools
            # the user has already auto-approved (this session) are still
            # executed inline below so they do not create busy-work cards.
            visual_tool_attachments: list[dict[str, Any]] = []
            permission_needed = self._confirmation_requirements(tc_list)

            if permission_needed:
                self._pending = {
                    "confirmations": permission_needed,
                    "tool_calls": tc_list,
                    "messages": messages,
                    "assistant_response": response,
                    "decisions": {},
                    "auto_allow_names": [],
                    "auto_allow_permissions": [],
                }
                blocks = [
                    self._confirm_block(
                        item["name"],
                        item["arguments"],
                        confirm_id=item["tool_call"]["id"],
                        runtime=item.get("runtime", ""),
                        covered_tool_names=item.get("covered_tool_names"),
                    )
                    for item in permission_needed
                ]
                pending_tools = [
                    self._pending_tool_projection(item)
                    for item in permission_needed
                ]
                return ChatResponse(
                    text=response.get("content") or "",
                    pending_tool=pending_tools[0] if pending_tools else None,
                    pending_tools=pending_tools,
                    blocks=blocks,
                    status="awaiting_confirmation",
                )

            # Execute all tools (all read)
            messages.append({
                "role": "assistant",
                "content": response.get("content") or "",
                "tool_calls": tc_list,
                "reasoning_content": response.get("reasoning_content") or "",
                "provider_content": response.get("provider_content") or [],
            })
            self.history.append(ChatMessage(
                role="assistant", content=response.get("content") or "",
                tool_calls=tc_list,
                reasoning_content=response.get("reasoning_content") or "",
                provider_content=list(response.get("provider_content") or []),
                timestamp=_time.time(),
                token_usage=self._turn_token_usage_snapshot(),
            ))

            batch_stop = await self._execute_tool_calls_batch(
                tc_list,
                messages=messages,
                tool_calls_made=tool_calls_made,
                executed_keys=executed_keys,
                on_block=on_block,
                on_status=on_status,
                visual_tool_attachments=visual_tool_attachments,
            )
            if batch_stop is not None:
                return batch_stop
            tools, discovery_trace_cursor = self._activate_discovered_tool_schemas(
                active_names,
                tools,
                tool_calls_made,
                discovery_trace_cursor,
            )
            if self._user_decision_requested:
                # Defense in depth: _execute_tool_calls_batch normally commits
                # the decision stop itself. Keep this guard for custom batch
                # implementations and restored legacy sessions.
                await self._emit_status(on_status, "已暂停，等待用户决定。")
                return self._commit_user_decision_stop(
                    self._last_partial_text,
                    tool_calls_made,
                    tc_list,
                    messages,
                )
            await self._emit_status(on_status, "工具执行完成，正在整理结果...")
            self._append_visual_tool_attachments(messages, visual_tool_attachments, source="visual_tool")
            if self._pause_after_current_response:
                await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                return self._commit_pause_after_current_response("", response, tool_calls_made)
        else:
            result = ""

        if not result:
            # Per-turn round budget exhausted with no textual response yet.
            # Ask the MODEL itself: do you want to keep going, or wrap up?
            # We provide tools on this call — if the model picks "continue"
            # it will emit fresh tool_calls and we restart the inner loop
            # (capped by the configured max budget restarts per turn). If it picks
            # "wrap up", it will emit a plain text answer instead, which
            # becomes the final result.
            user_lang = self._detect_user_language(messages)
            while self._budget_restart_allowed(budget_restart_count) and not self._cancel_requested:
                _assistant_debug_log(
                    "assistant.budget_decision "
                    f"max_agent_rounds={self._max_agent_rounds()} restart_count={budget_restart_count} "
                    f"restart_limit={self._max_budget_restarts()} tool_count={len(tool_calls_made)}"
                )
                log.warning(
                    "assistant.budget_decision max_agent_rounds=%d restart_count=%d restart_limit=%d tool_count=%d",
                    self._max_agent_rounds(),
                    budget_restart_count,
                    self._max_budget_restarts(),
                    len(tool_calls_made),
                )
                await self._emit_status(
                    on_status,
                    "已达到本轮上限，正在让 AI 自行判断要不要继续..." if user_lang == "zh"
                    else "Round budget hit — asking model whether to continue...",
                )
                decision_messages = list(messages) + [
                    {"role": "user", "content": self._build_budget_decision_prompt(self._max_agent_rounds())}
                ]
                try:
                    decision_resp = await self._call_ai(
                        decision_messages,
                        tools,
                        on_delta=on_delta,
                        thinking_enabled=thinking_enabled,
                    )
                except Exception as e:
                    log.warning("budget decision call failed: %s", e)
                    decision_resp = {}
                if self._cancel_requested:
                    return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)

                decision_tcs = decision_resp.get("tool_calls") or []
                if decision_tcs:
                    stopped = self._finish_if_implicit_user_question(decision_resp, tool_calls_made)
                    if stopped is not None:
                        if stopped.status == "awaiting_user_decision":
                            await self._emit_status(on_status, "已暂停，等待用户决定。")
                        return stopped

                    # Model chose "continue" by emitting fresh tool_calls.
                    # Splice the decision turn into history & messages, then
                    # restart the inner agent loop with reset round counter.
                    budget_restart_count += 1
                    self._log_budget_restart_trigger(budget_restart_count, tool_calls_made)
                    decision_content = decision_resp.get("content") or ""
                    messages.append({
                        "role": "assistant",
                        "content": decision_content,
                        "tool_calls": decision_tcs,
                        "reasoning_content": decision_resp.get("reasoning_content") or "",
                        "provider_content": decision_resp.get("provider_content") or [],
                    })
                    self.history.append(ChatMessage(
                        role="assistant",
                        content=decision_content,
                        tool_calls=decision_tcs,
                        reasoning_content=decision_resp.get("reasoning_content") or "",
                        provider_content=list(decision_resp.get("provider_content") or []),
                        timestamp=_time.time(),
                        token_usage=self._turn_token_usage_snapshot(),
                    ))

                    # Execute those tool calls right here so the next inner
                    # loop starts from a clean "post-tools" state. Reuses
                    # the same auto-confirm + duplicate-result logic as the
                    # main loop. If any tool needs confirmation, hand it
                    # back to the UI just like the main loop would.
                    visual_tool_attachments_dec: list[dict[str, Any]] = []
                    permission_needed_dec = self._confirmation_requirements(decision_tcs)
                    if permission_needed_dec:
                        self._pending = {
                            "confirmations": permission_needed_dec,
                            "tool_calls": decision_tcs,
                            "messages": messages,
                            "assistant_response": decision_resp,
                            "decisions": {},
                            "auto_allow_names": [],
                            "auto_allow_permissions": [],
                        }
                        blocks = [
                            self._confirm_block(
                                item["name"],
                                item["arguments"],
                                confirm_id=item["tool_call"]["id"],
                                runtime=item.get("runtime", ""),
                                covered_tool_names=item.get("covered_tool_names"),
                            )
                            for item in permission_needed_dec
                        ]
                        pending_tools = [
                            self._pending_tool_projection(item)
                            for item in permission_needed_dec
                        ]
                        return ChatResponse(
                            text=decision_content,
                            pending_tool=pending_tools[0] if pending_tools else None,
                            pending_tools=pending_tools,
                            blocks=blocks,
                            status="awaiting_confirmation",
                        )

                    batch_stop = await self._execute_tool_calls_batch(
                        decision_tcs,
                        messages=messages,
                        tool_calls_made=tool_calls_made,
                        executed_keys=executed_keys,
                        on_block=on_block,
                        on_status=on_status,
                        visual_tool_attachments=visual_tool_attachments_dec,
                    )
                    if batch_stop is not None:
                        return batch_stop
                    tools, discovery_trace_cursor = self._activate_discovered_tool_schemas(
                        active_names,
                        tools,
                        tool_calls_made,
                        discovery_trace_cursor,
                    )
                    if self._user_decision_requested:
                        await self._emit_status(on_status, "已暂停，等待用户决定。")
                        return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, tc_list, messages)

                    self._append_visual_tool_attachments(messages, visual_tool_attachments_dec, source="visual_tool")
                    if self._pause_after_current_response:
                        await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                        return self._commit_pause_after_current_response("", decision_resp, tool_calls_made)

                    # Restart the inner agent loop with a fresh round budget.
                    for _round2 in self._agent_round_iter():
                        round_start_ns2 = self._enter_round(_round2, tool_calls_made)
                        await self._emit_agent_round_start(on_segment, _round2, force=True)
                        loop_guard = self._tool_loop_guard(
                            messages=messages,
                            tool_calls_made=tool_calls_made,
                            tc_list=[],
                        )
                        if loop_guard is not None:
                            return loop_guard
                        if self._cancel_requested:
                            self._leave_round(round_index=_round2, start_ns=round_start_ns2, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_before_call_restart")
                            return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                        await self._emit_status(on_status, _pre_call_status_label(thinking_enabled))
                        response = await self._call_ai(messages, tools, on_delta=on_delta, thinking_enabled=thinking_enabled)
                        if self._cancel_requested:
                            self._leave_round(round_index=_round2, start_ns=round_start_ns2, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_after_call_restart")
                            return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                        if self._stream_truncated_this_round:
                            self._leave_round(round_index=_round2, start_ns=round_start_ns2, tool_calls_made=tool_calls_made, had_tool_calls=False, text=response.get("content", ""), reason="stream_repetition_stop_restart")
                            return self._commit_stream_repetition_stop(response=response, tool_calls_made=tool_calls_made)
                        tc_list = response.get("tool_calls") or []
                        if tc_list:
                            toolless_decision_pending = False
                        if not tc_list:
                            result = response.get("content", "")
                            ts = _time.time()
                            blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
                            if (
                                self._should_auto_continue(result)
                                and self._auto_continue_allowed(auto_continue_count)
                                and not self._cancel_requested
                                and not self._pause_after_current_response
                            ):
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
                                messages.append({"role": "user", "content": self._build_auto_continue_prompt()})
                                auto_continue_count += 1
                                self._log_auto_continue_trigger(auto_continue_count, result)
                                await self._emit_status(on_status, "Todo 未完成，自动继续推进...")
                                await self._emit_single_block(on_block, {"type": "todo_list", "mode": "assessment_plan", **self.list_todos()})
                                continue
                            if (
                                self._should_auto_continue(result)
                                and self._auto_continue_exhausted(auto_continue_count)
                                and not self._cancel_requested
                                and not self._pause_after_current_response
                            ):
                                await self._emit_status(on_status, "Todo 自动续跑未产生新进展，等待用户决定。")
                                return self._commit_auto_limit_decision_stop(
                                    reason="Todo 仍未完成，但自动续跑后没有新的 Todo 状态、工具结果或证据进展。",
                                    current_text=result,
                                    tool_calls_made=tool_calls_made,
                                )
                            if (
                                self._should_probe_toolless_continuation(result, tool_calls_made)
                                and self._toolless_continue_allowed(toolless_decision_count)
                                and not toolless_decision_pending
                                and not self._cancel_requested
                                and not self._pause_after_current_response
                            ):
                                self.history.append(ChatMessage(
                                    role="assistant",
                                    content=result,
                                    reasoning_content=response.get("reasoning_content") or "",
                                    provider_content=list(response.get("provider_content") or []),
                                    blocks=blocks,
                                    timestamp=ts,
                                    token_usage=self._turn_token_usage_snapshot(),
                                ))
                                messages.append({
                                    "role": "assistant",
                                    "content": result,
                                    "reasoning_content": response.get("reasoning_content") or "",
                                    "provider_content": response.get("provider_content") or [],
                                })
                                messages.append({"role": "user", "content": self._build_toolless_continuation_prompt()})
                                toolless_decision_count += 1
                                toolless_decision_pending = True
                                self._log_toolless_probe_trigger(toolless_decision_count, tool_calls_made, result)
                                await self._emit_status(on_status, "工具后未见明确收尾，正在让 AI 判断是否继续...")
                                continue
                            if (
                                self._should_probe_toolless_continuation(result, tool_calls_made)
                                and self._toolless_continue_exhausted(toolless_decision_count)
                                and not toolless_decision_pending
                                and not self._cancel_requested
                                and not self._pause_after_current_response
                            ):
                                await self._emit_status(on_status, "无工具续跑判断次数已用完，等待用户决定。")
                                return self._commit_auto_limit_decision_stop(
                                    reason=f"工具后仍未明确收尾，但已达到无工具续跑判断次数上限 {self._max_toolless_continue_decisions()}",
                                    current_text=result,
                                    tool_calls_made=tool_calls_made,
                                )
                            acceptance = await self._run_acceptance_closed_loop(
                                result=result,
                                response=response,
                                messages=messages,
                                tool_calls_made=tool_calls_made,
                                blocks=blocks,
                                ts=ts,
                                on_status=on_status,
                                on_block=on_block,
                            )
                            if acceptance is not None:
                                if acceptance.get("continue"):
                                    continue
                                if acceptance.get("response") is not None:
                                    return acceptance["response"]
                            self.history.append(ChatMessage(
                                role="assistant",
                                content=result,
                                reasoning_content=response.get("reasoning_content") or "",
                                provider_content=list(response.get("provider_content") or []),
                                blocks=blocks,
                                timestamp=ts,
                                token_usage=self._turn_token_usage_snapshot(),
                            ))
                            return ChatResponse(text=result, tool_calls=tool_calls_made, blocks=blocks, tool_trace=tool_calls_made, status="done")

                        stopped = self._finish_if_implicit_user_question(response, tool_calls_made)
                        if stopped is not None:
                            if stopped.status == "awaiting_user_decision":
                                await self._emit_status(on_status, "已暂停，等待用户决定。")
                            return stopped

                        if self._pause_after_current_response:
                            await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                            return self._commit_pause_after_current_response(
                                response.get("content") or "",
                                response,
                                tool_calls_made,
                            )

                        # Same permission / execute path as the main loop.
                        visual_tool_attachments_b: list[dict[str, Any]] = []
                        permission_needed_b = self._confirmation_requirements(tc_list)
                        if permission_needed_b:
                            self._pending = {
                                "confirmations": permission_needed_b,
                                "tool_calls": tc_list,
                                "messages": messages,
                                "assistant_response": response,
                                "decisions": {},
                                "auto_allow_names": [],
                                "auto_allow_permissions": [],
                            }
                            blocks = [
                                self._confirm_block(
                                    item["name"],
                                    item["arguments"],
                                    confirm_id=item["tool_call"]["id"],
                                    runtime=item.get("runtime", ""),
                                    covered_tool_names=item.get("covered_tool_names"),
                                )
                                for item in permission_needed_b
                            ]
                            pending_tools = [
                                self._pending_tool_projection(item)
                                for item in permission_needed_b
                            ]
                            return ChatResponse(
                                text=response.get("content") or "",
                                pending_tool=pending_tools[0] if pending_tools else None,
                                pending_tools=pending_tools,
                                blocks=blocks,
                                status="awaiting_confirmation",
                            )
                        messages.append({
                            "role": "assistant",
                            "content": response.get("content") or "",
                            "tool_calls": tc_list,
                            "reasoning_content": response.get("reasoning_content") or "",
                            "provider_content": response.get("provider_content") or [],
                        })
                        self.history.append(ChatMessage(
                            role="assistant", content=response.get("content") or "",
                            tool_calls=tc_list,
                            reasoning_content=response.get("reasoning_content") or "",
                            provider_content=list(response.get("provider_content") or []),
                            timestamp=_time.time(),
                            token_usage=self._turn_token_usage_snapshot(),
                        ))
                        batch_stop = await self._execute_tool_calls_batch(
                            tc_list,
                            messages=messages,
                            tool_calls_made=tool_calls_made,
                            executed_keys=executed_keys,
                            on_block=on_block,
                            on_status=on_status,
                            visual_tool_attachments=visual_tool_attachments_b,
                        )
                        if batch_stop is not None:
                            return batch_stop
                        tools, discovery_trace_cursor = self._activate_discovered_tool_schemas(
                            active_names,
                            tools,
                            tool_calls_made,
                            discovery_trace_cursor,
                        )
                        if self._user_decision_requested:
                            await self._emit_status(on_status, "已暂停，等待用户决定。")
                            return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, tc_list, messages)
                        self._append_visual_tool_attachments(messages, visual_tool_attachments_b, source="visual_tool")
                        if self._pause_after_current_response:
                            await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                            return self._commit_pause_after_current_response("", response, tool_calls_made)
                    # If this inner restart also exhausted its rounds we
                    # loop back and ask the model again (still capped by
                    # the configured max budget restarts).
                    continue

                # Model chose "wrap up" — plain text reply, take it as result.
                forced_text = (decision_resp.get("content") or "").strip()
                if forced_text:
                    result = forced_text
                break

            if not result:
                if self._budget_restart_exhausted(budget_restart_count):
                    return self._commit_auto_limit_decision_stop(
                        reason=f"轮次预算重启次数已达到上限 {self._max_budget_restarts()}",
                        tool_calls_made=tool_calls_made,
                    )
                result = (
                    "我已经收集到上面的工具结果，但模型这一轮没有给出文字回答。你可以再补充一句"
                    "希望我重点分析哪部分，或者点 \"重新生成\"。"
                    if user_lang == "zh"
                    else (
                        "I gathered the tool results above but the model did not return a text "
                        "answer this round. Tell me which part you want analysed, or press "
                        "Regenerate."
                    )
                )

        blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
        if result and not (is_runtime_result and terminal_status == "ai_error"):
            self.history.append(ChatMessage(
                role="assistant",
                content=result,
                blocks=blocks,
                tool_trace=tool_calls_made,
                timestamp=_time.time(),
                token_usage=self._turn_token_usage_snapshot(),
            ))
        return ChatResponse(
            text=result,
            tool_calls=tool_calls_made,
            blocks=blocks,
            tool_trace=tool_calls_made,
            status=terminal_status,
        )


    def _record_confirmation_decision(
        self,
        decision: dict[str, Any],
        pending: dict[str, Any],
        *,
        timestamp: float,
    ) -> None:
        """Persist an auditable runtime event outside model-visible history."""

        events = getattr(self, "events", None)
        if not isinstance(events, list):
            self.events = []
            events = self.events
        pending_id = str(pending.get("pending_id") or "")
        try:
            version = max(1, int(pending.get("version") or 1))
        except (TypeError, ValueError):
            version = 1
        payload = {
            "type": str(decision.get("type") or "confirmation_decision"),
            "pending_id": pending_id,
            "version": version,
            "call_id": str(decision.get("call_id") or ""),
            "verdict": str(decision.get("verdict") or ""),
            "decisions": dict(decision.get("decisions") or {}),
            "auto_allow_names": list(decision.get("auto_allow_names") or []),
            "auto_allow_permissions": list(decision.get("auto_allow_permissions") or []),
        }
        event_kind = (
            "approval_abandoned"
            if payload["type"] == "approval_abandoned"
            else "approval_decision"
        )
        events.append({
            "id": f"evt_{int(timestamp * 1000)}_{len(events)}",
            "kind": event_kind,
            "source": "user",
            "render_policy": "timeline",
            "context_policy": "exclude",
            "turn_id": str(getattr(self, "_current_turn_id", "") or ""),
            "timestamp": timestamp,
            "payload": payload,
        })
        if len(events) > 2000:
            del events[:-2000]

    def _validate_confirmation_binding(
        self,
        decision: dict[str, Any],
        pending: dict[str, Any],
        *,
        allow_legacy_unbound: bool,
    ) -> str:
        """Reject stale/replayed decisions before they can consume pending state."""

        pending_id, version = self._ensure_pending_identity()
        if allow_legacy_unbound:
            decision.setdefault("pending_id", pending_id)
            decision.setdefault("version", version)
        if str(decision.get("pending_id") or "") != pending_id:
            return "确认请求已过期（pending_id 不匹配）。"
        try:
            action_version = int(decision.get("version"))
        except (TypeError, ValueError):
            return "确认请求缺少有效版本。"
        if action_version != version:
            return "确认请求已过期（version 不匹配）。"

        expected_ids: set[str] = set()
        confirmations = pending.get("confirmations")
        if isinstance(confirmations, list):
            for item in confirmations:
                if not isinstance(item, dict):
                    continue
                tool_call = item.get("tool_call")
                if isinstance(tool_call, dict) and tool_call.get("id"):
                    expected_ids.add(str(tool_call["id"]))
        item = pending.get("tool_call")
        if isinstance(item, dict):
            tool_call = item.get("tool_call")
            if isinstance(tool_call, dict) and tool_call.get("id"):
                expected_ids.add(str(tool_call["id"]))

        dtype = str(decision.get("type") or "")
        if dtype == "batch_confirmation_decision":
            submitted = {str(key) for key in (decision.get("decisions") or {})}
            if not submitted or not submitted.issubset(expected_ids):
                return "确认请求包含不属于当前 pending 的 call_id。"
        elif decision.get("call_id"):
            if str(decision["call_id"]) not in expected_ids:
                return "确认请求的 call_id 已过期。"
        elif not allow_legacy_unbound and str(decision.get("verdict") or "") != "deny":
            return "确认请求缺少 call_id。"
        return ""

    def _resolved_tool_call(
        self,
        tool_call: dict[str, Any],
        *,
        requirement: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Inner catalogue name/args for dispatch after confirm or in a leftover batch.

        Confirmation cards store the unwrapped tool on ``requirement``; the raw
        ``tool_call`` may still be ``invoke_catalogue_tool``. Resume must dispatch
        the inner tool — never the wrapper stub.
        """
        if requirement is not None:
            name = str(requirement.get("name") or "").strip()
            args = requirement.get("arguments")
            if name and isinstance(args, dict):
                return name, args
        return resolve_session_tool_call(tool_call)

    async def _handle_pending(
        self,
        message: str,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], Awaitable[None] | None] | None = None,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_segment: Callable[[int], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
        decision: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Handle user's response to a permission request."""
        import time as _time
        typed = decision or confirmation_decision_from_legacy_message(message) or {}
        intent = str(typed.get("verdict") or "").strip().lower() or _confirmation_intent(message)
        approve_session = intent == "allow_session"
        approved = intent in {"allow", "allow_session"}
        denied = intent == "deny"

        if denied:
            p = self._pending
            self._pending = None
            if isinstance(p, dict):
                self._record_confirmation_decision(typed, p, timestamp=_time.time())
            return ChatResponse(text="操作已取消。", status="cancelled")

        if approve_session:
            approved = True

        if not approved:
            pending_name = (self._pending or {}).get("tool_call", {}).get("name", "tool")
            return ChatResponse(
                text=(
                    f"正在等待你确认是否执行 **{pending_name}**。\n"
                    "回复“允许/yes”执行，或回复“拒绝/取消/不搞了/算了”取消。"
                ),
                status="awaiting_confirmation",
            )

        # Execute the pending tool
        p = self._pending
        self._pending = None
        requirement = p["tool_call"]
        tc = requirement["tool_call"]
        fn_name, args = self._resolved_tool_call(tc, requirement=requirement)
        approved_runtime = str(requirement.get("runtime") or "")
        if approve_session and not approved_runtime:
            self.set_auto_approve_tool(fn_name, True)

        if self._cancel_requested:
            return self._commit_cancelled_assistant(self._last_partial_text, [])
        if approved_runtime:
            await self._emit_status(on_status, f"正在安装并校验 {approved_runtime} 沙盒环境...")
        await self._emit_tool_start(on_status, fn_name, args)
        self._log_tool_dispatch_start(fn_name, args, [])
        tool_result = await self._dispatch_tool(
            fn_name,
            args,
            user_approved=True,
            approved_runtime=approved_runtime,
        )
        tool_result = self._auto_archive_tool_result(fn_name, args, tool_result)
        visual_tool_attachments = self._pop_tool_attachments(tool_result)
        if self._cancel_requested:
            return self._commit_cancelled_assistant(self._last_partial_text, [])
        await self._emit_status(on_status, "工具执行完成，正在整理结果...")
        result_str = self._tool_result_to_prompt(fn_name, tool_result)
        trace = self._trace(fn_name, args, tool_result)
        await self._emit_trace_blocks(on_block, trace)
        executed_keys: set[str] = set()
        self._remember_executed_tool_key(executed_keys, fn_name, args)

        assistant_response = p.get("assistant_response")
        if assistant_response:
            self._append_pending_assistant_once(assistant_response, tc["id"])
        self._record_confirmation_decision(typed, p, timestamp=_time.time())
        self.history.append(ChatMessage(role="tool", content=result_str, tool_call_id=tc["id"], timestamp=_time.time()))
        await self._emit_checkpoint()
        if self._user_decision_requested:
            await self._emit_status(on_status, "已暂停，等待用户决定。")
            return self._commit_user_decision_stop(self._last_partial_text, [self._trace(fn_name, args, tool_result)], [], [])
        if self._team_room_passed:
            return self._commit_team_room_silence([self._trace(fn_name, args, tool_result)])

        base_messages = list(p["messages"])
        if assistant_response:
            base_messages.append({
                "role": "assistant",
                "content": assistant_response.get("content") or "",
                "tool_calls": assistant_response.get("tool_calls") or [tc],
                "reasoning_content": assistant_response.get("reasoning_content") or "",
                "provider_content": assistant_response.get("provider_content") or [],
            })
        base_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})

        remaining = list(p.get("remaining") or [])
        tool_calls_made = [trace]
        while remaining:
            next_tc = remaining.pop(0)
            next_name, next_args = self._resolved_tool_call(next_tc)
            next_requirements = self._confirmation_requirements([next_tc])
            if next_requirements:
                requirement = next_requirements[0]
                self._pending = {
                    "tool_call": requirement,
                    "remaining": remaining,
                    "messages": base_messages,
                    "assistant_response": None,
                }
                blocks = self._blocks_for_tool_trace([trace])
                blocks.append(self._confirm_block(
                    next_name,
                    next_args,
                    runtime=requirement.get("runtime", ""),
                    covered_tool_names=requirement.get("covered_tool_names"),
                ))
                return ChatResponse(
                    text=f"已执行 {fn_name}。还需要确认下一步：{next_name}。",
                    tool_calls=[trace],
                    blocks=blocks,
                    tool_trace=[trace],
                    pending_tool=self._pending_tool_projection(requirement),
                    pending_tools=[self._pending_tool_projection(requirement)],
                    status="awaiting_confirmation",
                )
            if self._cancel_requested:
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            await self._emit_tool_start(on_status, next_name, next_args)
            next_result = await self._dispatch_tool(next_name, next_args)
            next_result = self._auto_archive_tool_result(next_name, next_args, next_result)
            visual_tool_attachments.extend(self._pop_tool_attachments(next_result))
            self._remember_executed_tool_key(executed_keys, next_name, next_args)
            if self._cancel_requested:
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            await self._emit_status(on_status, "工具执行完成，正在整理结果...")
            next_result_str = self._tool_result_to_prompt(next_name, next_result)
            next_trace = self._trace(next_name, next_args, next_result)
            tool_calls_made.append(next_trace)
            await self._emit_trace_blocks(on_block, next_trace)
            base_messages.append({"role": "tool", "tool_call_id": next_tc["id"], "content": next_result_str})
            self.history.append(ChatMessage(role="tool", content=next_result_str, tool_call_id=next_tc["id"], timestamp=_time.time()))
            await self._emit_checkpoint()
            if self._user_decision_requested:
                await self._emit_status(on_status, "已暂停，等待用户决定。")
                return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, [], base_messages)

        if self._pause_after_current_response:
            await self._emit_status(on_status, "已暂停，优先处理你的插话...")
            return self._commit_pause_after_current_response("", assistant_response or {}, tool_calls_made)

        # Rebuild messages and continue the conversation
        messages = base_messages
        self._append_visual_tool_attachments(messages, visual_tool_attachments, source="visual_tool")

        _active_names, tools = self._continue_tool_state(messages, tool_calls_made)
        auto_continue_count = 0
        toolless_decision_count = 0
        toolless_decision_pending = False
        result = ""
        try:
            for _round in self._agent_round_iter():
                round_start_ns = self._enter_round(_round, tool_calls_made)
                await self._emit_agent_round_start(on_segment, _round, force=True)
                loop_guard = self._tool_loop_guard(
                    messages=messages,
                    tool_calls_made=tool_calls_made,
                    tc_list=[],
                )
                if loop_guard is not None:
                    return loop_guard
                if self._cancel_requested:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_before_call")
                    return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                await self._emit_status(on_status, _pre_call_status_label(thinking_enabled))
                response = await self._call_ai(messages, tools, on_delta=on_delta, thinking_enabled=thinking_enabled)
                if self._cancel_requested:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_after_call")
                    return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                if self._stream_truncated_this_round:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text=response.get("content", ""), reason="stream_repetition_stop")
                    return self._commit_stream_repetition_stop(response=response, tool_calls_made=tool_calls_made)

                tc_list = response.get("tool_calls") or []
                if tc_list:
                    toolless_decision_pending = False
                if not tc_list:
                    result = response.get("content", "Done.")
                    blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
                    if (
                        self._should_auto_continue(result)
                        and self._auto_continue_allowed(auto_continue_count)
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        self.history.append(ChatMessage(
                            role="assistant",
                            content=result,
                            reasoning_content=response.get("reasoning_content") or "",
                            provider_content=list(response.get("provider_content") or []),
                            blocks=blocks,
                            tool_trace=tool_calls_made,
                            hidden=True,
                            timestamp=_time.time(),
                        ))
                        messages.append({
                            "role": "assistant",
                            "content": result,
                            "reasoning_content": response.get("reasoning_content") or "",
                            "provider_content": response.get("provider_content") or [],
                        })
                        messages.append({"role": "user", "content": self._build_auto_continue_prompt()})
                        auto_continue_count += 1
                        self._log_auto_continue_trigger(auto_continue_count, result)
                        await self._emit_status(on_status, "Todo 未完成，自动继续推进...")
                        await self._emit_single_block(on_block, {"type": "todo_list", "mode": "assessment_plan", **self.list_todos()})
                        continue
                    if (
                        self._should_auto_continue(result)
                        and self._auto_continue_exhausted(auto_continue_count)
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        await self._emit_status(on_status, "Todo 自动续跑未产生新进展，等待用户决定。")
                        return self._commit_auto_limit_decision_stop(
                            reason="Todo 仍未完成，但自动续跑后没有新的 Todo 状态、工具结果或证据进展。",
                            current_text=result,
                            tool_calls_made=tool_calls_made,
                        )
                    if (
                        self._should_probe_toolless_continuation(result, tool_calls_made)
                        and self._toolless_continue_allowed(toolless_decision_count)
                        and not toolless_decision_pending
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        self.history.append(ChatMessage(
                            role="assistant",
                            content=result,
                            reasoning_content=response.get("reasoning_content") or "",
                            provider_content=list(response.get("provider_content") or []),
                            blocks=blocks,
                            tool_trace=tool_calls_made,
                            timestamp=_time.time(),
                            token_usage=self._turn_token_usage_snapshot(),
                        ))
                        messages.append({
                            "role": "assistant",
                            "content": result,
                            "reasoning_content": response.get("reasoning_content") or "",
                            "provider_content": response.get("provider_content") or [],
                        })
                        messages.append({"role": "user", "content": self._build_toolless_continuation_prompt()})
                        toolless_decision_count += 1
                        toolless_decision_pending = True
                        self._log_toolless_probe_trigger(toolless_decision_count, tool_calls_made, result)
                        await self._emit_status(on_status, "工具后未见明确收尾，正在让 AI 判断是否继续...")
                        continue
                    if (
                        self._should_probe_toolless_continuation(result, tool_calls_made)
                        and self._toolless_continue_exhausted(toolless_decision_count)
                        and not toolless_decision_pending
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        await self._emit_status(on_status, "无工具续跑判断次数已用完，等待用户决定。")
                        return self._commit_auto_limit_decision_stop(
                            reason=f"工具后仍未明确收尾，但已达到无工具续跑判断次数上限 {self._max_toolless_continue_decisions()}",
                            current_text=result,
                            tool_calls_made=tool_calls_made,
                        )
                    acceptance = await self._run_acceptance_closed_loop(
                        result=result,
                        response=response,
                        messages=messages,
                        tool_calls_made=tool_calls_made,
                        blocks=blocks,
                        ts=_time.time(),
                        on_status=on_status,
                        on_block=on_block,
                    )
                    if acceptance is not None:
                        if acceptance.get("continue"):
                            continue
                        if acceptance.get("response") is not None:
                            return acceptance["response"]
                    self.history.append(ChatMessage(
                        role="assistant",
                        content=result,
                        reasoning_content=response.get("reasoning_content") or "",
                        provider_content=list(response.get("provider_content") or []),
                        blocks=blocks,
                        tool_trace=tool_calls_made,
                        timestamp=_time.time(),
                        token_usage=self._turn_token_usage_snapshot(),
                    ))
                    return ChatResponse(
                        text=result,
                        tool_calls=tool_calls_made,
                        blocks=blocks,
                        tool_trace=tool_calls_made,
                        status="done",
                    )

                stopped = self._finish_if_implicit_user_question(response, tool_calls_made)
                if stopped is not None:
                    if stopped.status == "awaiting_user_decision":
                        await self._emit_status(on_status, "已暂停，等待用户决定。")
                    return stopped

                if self._pause_after_current_response:
                    await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                    return self._commit_pause_after_current_response(
                        response.get("content") or "",
                        response,
                        tool_calls_made,
                    )

                assistant_appended = False
                fresh_tc_list = []
                for next_tc in tc_list:
                    next_name, next_args = self._resolved_tool_call(next_tc)
                    key = self._tool_call_key(next_name, next_args)
                    if self._should_dedupe_tool_call(next_name, next_args) and key in executed_keys:
                        if not assistant_appended:
                            messages.append({
                                "role": "assistant",
                                "content": response.get("content") or "",
                                "tool_calls": tc_list,
                                "reasoning_content": response.get("reasoning_content") or "",
                                "provider_content": response.get("provider_content") or [],
                            })
                            self.history.append(ChatMessage(
                                role="assistant",
                                content=response.get("content") or "",
                                tool_calls=tc_list,
                                reasoning_content=response.get("reasoning_content") or "",
                                provider_content=list(response.get("provider_content") or []),
                                timestamp=_time.time(),
                                token_usage=self._turn_token_usage_snapshot(),
                            ))
                            assistant_appended = True
                        duplicate = self._duplicate_tool_result(next_name, next_args)
                        duplicate_str = self._tool_result_to_prompt(next_name, duplicate)
                        dup_trace = self._trace(next_name, next_args, duplicate)
                        tool_calls_made.append(dup_trace)
                        await self._emit_trace_blocks(on_block, dup_trace)
                        messages.append({"role": "tool", "tool_call_id": next_tc["id"], "content": duplicate_str})
                        self.history.append(ChatMessage(role="tool", content=duplicate_str, tool_call_id=next_tc["id"], timestamp=_time.time()))
                    else:
                        fresh_tc_list.append(next_tc)
                if assistant_appended and not fresh_tc_list:
                    continue
                tc_list = fresh_tc_list

                requirements = self._confirmation_requirements(tc_list)
                needs_permission = requirements[0] if requirements else None
                if needs_permission:
                    self._pending = {
                        "tool_call": needs_permission,
                        "remaining": [t for t in tc_list if t["id"] != needs_permission["tool_call"]["id"]],
                        "messages": messages,
                        "assistant_response": response,
                    }
                    blocks = self._blocks_for_tool_trace(tool_calls_made)
                    blocks.append(self._confirm_block(
                        needs_permission["name"],
                        needs_permission["arguments"],
                        runtime=needs_permission.get("runtime", ""),
                        covered_tool_names=needs_permission.get("covered_tool_names"),
                    ))
                    return ChatResponse(
                        text=response.get("content") or "",
                        tool_calls=tool_calls_made,
                        blocks=blocks,
                        tool_trace=tool_calls_made,
                        pending_tool=self._pending_tool_projection(needs_permission),
                        pending_tools=[self._pending_tool_projection(needs_permission)],
                        status="awaiting_confirmation",
                    )

                if not assistant_appended:
                    messages.append({
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": tc_list,
                        "reasoning_content": response.get("reasoning_content") or "",
                        "provider_content": response.get("provider_content") or [],
                    })
                    self.history.append(ChatMessage(
                        role="assistant",
                        content=response.get("content") or "",
                        tool_calls=tc_list,
                        reasoning_content=response.get("reasoning_content") or "",
                        provider_content=list(response.get("provider_content") or []),
                        timestamp=_time.time(),
                        token_usage=self._turn_token_usage_snapshot(),
                    ))
                batch_stop = await self._execute_tool_calls_batch(
                    tc_list,
                    messages=messages,
                    tool_calls_made=tool_calls_made,
                    executed_keys=executed_keys,
                    on_block=on_block,
                    on_status=on_status,
                    visual_tool_attachments=visual_tool_attachments,
                )
                if batch_stop is not None:
                    return batch_stop
                _active_names, tools = self._continue_tool_state(messages, tool_calls_made)
                if self._user_decision_requested:
                    await self._emit_status(on_status, "已暂停，等待用户决定。")
                    return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, [], messages)
                self._append_visual_tool_attachments(messages, visual_tool_attachments, source="visual_tool")
                visual_tool_attachments = []
                if self._pause_after_current_response:
                    await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                    return self._commit_pause_after_current_response("", response, tool_calls_made)
            return self._commit_auto_limit_decision_stop(
                reason=(
                    f"已达到本回合 {self._max_agent_rounds()} 轮工具调用的上限。"
                    "如果是盲注/枚举场景，建议优先考虑 UNION、CAST 报错回显或一次性导出全部行"
                ),
                tool_calls_made=tool_calls_made,
            )
        except Exception as e:
            blocks = self._blocks_for_tool_trace(tool_calls_made)
            return ChatResponse(text=f"操作已执行，但后续 agent loop 失败: {e}", tool_calls=tool_calls_made, blocks=blocks, tool_trace=tool_calls_made, status="error")


    async def _handle_pending_batch(
        self,
        message: str,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_status: Callable[[str], Awaitable[None] | None] | None = None,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_segment: Callable[[int], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
        decision: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Handle a batched confirmation flow via typed decision (Loom22 §9).

        Preferred input is a normalized ``confirmation`` object::

            {"type": "batch_confirmation_decision", "decisions": {...}}
            {"type": "confirmation_decision", "verdict": "allow"|"deny"|…}
            {"type": "confirmation_decision", "call_id": "…", "verdict": "…"}

        Legacy chat-channel strings (``confirm_batch:`` / ``yes``) are accepted
        only after migration into the same shape. Decisions accumulate on
        ``self._pending["decisions"]`` until every card has a verdict; only
        then are the approved tools run. Any denial cancels the entire batch.
        """
        import time as _time

        p = self._pending
        confirmations = list(p.get("confirmations") or [])
        decisions: dict[str, str] = dict(p.get("decisions") or {})
        auto_allow_names: list[str] = list(p.get("auto_allow_names") or [])
        auto_allow_permissions: list[str] = list(p.get("auto_allow_permissions") or [])

        typed = decision or confirmation_decision_from_legacy_message(message) or {}
        dtype = str(typed.get("type") or "").strip().lower()

        for name in typed.get("auto_allow_names") or []:
            if name and str(name) not in auto_allow_names:
                auto_allow_names.append(str(name))
        for perm in typed.get("auto_allow_permissions") or []:
            if perm and str(perm) not in auto_allow_permissions:
                auto_allow_permissions.append(str(perm))

        if dtype == "batch_confirmation_decision":
            for key, verdict in (typed.get("decisions") or {}).items():
                decisions[str(key)] = str(verdict).lower()
        elif dtype == "confirmation_decision":
            call_id = str(typed.get("call_id") or "").strip()
            verdict = str(typed.get("verdict") or "").strip().lower()
            if call_id and verdict:
                decisions[call_id] = verdict
                if verdict == "allow_session":
                    for item in confirmations:
                        if item["tool_call"]["id"] != call_id:
                            continue
                        if item.get("runtime"):
                            break
                        perm_key = self._mcp_permission_key(
                            item["name"], item.get("arguments") or {}
                        )
                        if perm_key:
                            if perm_key not in auto_allow_permissions:
                                auto_allow_permissions.append(perm_key)
                        elif item["name"] not in auto_allow_names:
                            auto_allow_names.append(item["name"])
                        break
            elif verdict == "allow":
                for item in confirmations:
                    decisions.setdefault(item["tool_call"]["id"], "allow")
            elif verdict == "allow_session":
                for item in confirmations:
                    decisions.setdefault(item["tool_call"]["id"], "allow_session")
                    if item.get("runtime"):
                        continue
                    perm_key = self._mcp_permission_key(
                        item["name"], item.get("arguments") or {}
                    )
                    if perm_key:
                        if perm_key not in auto_allow_permissions:
                            auto_allow_permissions.append(perm_key)
                    elif item["name"] not in auto_allow_names:
                        auto_allow_names.append(item["name"])
            elif verdict == "deny":
                for item in confirmations:
                    decisions[item["tool_call"]["id"]] = "deny"

        # ---- any denial cancels the whole batch ----
        if any(decisions.get(item["tool_call"]["id"]) in {"deny", "no", "reject"} for item in confirmations):
            self._record_confirmation_decision(typed, p, timestamp=_time.time())
            self._pending = None
            denied_runtime = next(
                (str(item.get("runtime") or "") for item in confirmations if item.get("runtime")),
                "",
            )
            if denied_runtime:
                return ChatResponse(
                    text=f"已取消安装 {denied_runtime} 沙盒环境；原工具未执行。",
                    blocks=[{
                        "type": "tool_result",
                        "name": "runtime_install",
                        "ok": False,
                        "blocked": True,
                        "runtime": denied_runtime,
                        "error_code": "managed_runtime_install_denied",
                    }],
                    status="cancelled",
                )
            return ChatResponse(text="操作已取消。", status="cancelled")

        # ---- not all decided yet → return updated cards and keep waiting ----
        approved_set = {"allow", "yes", "approve", "ok", "allow_session"}
        undecided = [item for item in confirmations if decisions.get(item["tool_call"]["id"]) not in approved_set]
        if undecided:
            p["decisions"] = decisions
            p["auto_allow_names"] = auto_allow_names
            p["auto_allow_permissions"] = auto_allow_permissions
            p["version"] = max(1, int(p.get("version") or 1)) + 1
            blocks = [
                self._confirm_block(
                    item["name"],
                    item["arguments"],
                    confirm_id=item["tool_call"]["id"],
                    decision="allow" if decisions.get(item["tool_call"]["id"]) in approved_set else "",
                    runtime=item.get("runtime", ""),
                    covered_tool_names=item.get("covered_tool_names"),
                )
                for item in confirmations
            ]
            pending_tools = [
                self._pending_tool_projection(item)
                for item in undecided
            ]
            return ChatResponse(
                text="",
                pending_tool=pending_tools[0] if pending_tools else None,
                pending_tools=pending_tools,
                blocks=blocks,
                status="awaiting_confirmation",
            )

        # ---- everyone approved → register session auto-allows and run ----
        for name in auto_allow_names:
            if name and (name not in _NO_AUTO_APPROVE_TOOLS or name in _REVERSE_DYNAMIC_TOOLS):
                self.set_auto_approve_tool(name, True)
        for perm_key in auto_allow_permissions:
            if perm_key:
                self.remember_permission(perm_key)

        self._pending = None
        assistant_response = p.get("assistant_response") or {}
        tc_list = list(p.get("tool_calls") or [])
        base_messages = list(p.get("messages") or [])
        if assistant_response and tc_list:
            self._append_pending_assistant_once(assistant_response, tc_list[0]["id"])
            base_messages.append({
                "role": "assistant",
                "content": assistant_response.get("content") or "",
                "tool_calls": assistant_response.get("tool_calls") or tc_list,
                "reasoning_content": assistant_response.get("reasoning_content") or "",
                "provider_content": assistant_response.get("provider_content") or [],
            })
        self._record_confirmation_decision(typed, p, timestamp=_time.time())

        tool_calls_made: list[dict[str, Any]] = []
        executed_keys: set[str] = set()
        visual_tool_attachments: list[dict[str, Any]] = []
        approved_runtime_by_id: dict[str, str] = {}
        for item in confirmations:
            runtime = str(item.get("runtime") or "")
            if not runtime:
                continue
            for tool_id in item.get("covered_tool_ids") or [item["tool_call"]["id"]]:
                approved_runtime_by_id[str(tool_id)] = runtime
        runtime_failures: dict[str, dict[str, Any]] = {}
        for tc in tc_list:
            fn_name, args = self._resolved_tool_call(tc)
            if self._cancel_requested:
                return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
            approved_runtime = approved_runtime_by_id.get(str(tc["id"]), "")
            if approved_runtime and approved_runtime in runtime_failures:
                from argusai.core.managed_runtimes import runtime_kind

                failure = runtime_failures[approved_runtime]
                tool_result = {
                    **failure,
                    "original_tool": fn_name,
                    "error_code": "managed_runtime_install_failed",
                    "error": (
                        f"{approved_runtime} {runtime_kind(approved_runtime)} installation "
                        "failed earlier in this dispatch; this tool was not executed."
                    ),
                }
            else:
                if approved_runtime and not self._runtime_is_ready(approved_runtime):
                    noun = "工具链" if approved_runtime == "wabt" else "沙盒环境"
                    await self._emit_status(
                        on_status, f"正在安装并校验 {approved_runtime} {noun}..."
                    )
                await self._emit_tool_start(on_status, fn_name, args)
                self._log_tool_dispatch_start(fn_name, args, tool_calls_made)
                tool_result = await self._dispatch_tool(
                    fn_name,
                    args,
                    user_approved=True,
                    approved_runtime=approved_runtime,
                )
                if (
                    approved_runtime
                    and isinstance(tool_result, dict)
                    and not tool_result.get("ok")
                    and str(tool_result.get("error_code") or "").startswith("runtime_")
                ):
                    runtime_failures[approved_runtime] = dict(tool_result)
            tool_result = self._auto_archive_tool_result(fn_name, args, tool_result)
            visual_tool_attachments.extend(self._pop_tool_attachments(tool_result))
            self._remember_executed_tool_key(executed_keys, fn_name, args)
            await self._emit_status(on_status, "工具执行完成，正在整理结果...")
            result_str = self._tool_result_to_prompt(fn_name, tool_result)
            trace = self._trace(fn_name, args, tool_result)
            tool_calls_made.append(trace)
            await self._emit_trace_blocks(on_block, trace)
            base_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})
            self.history.append(ChatMessage(role="tool", content=result_str, tool_call_id=tc["id"], timestamp=_time.time()))
            await self._emit_checkpoint()
            if self._user_decision_requested:
                await self._emit_status(on_status, "已暂停，等待用户决定。")
                return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, tc_list, base_messages)

        if self._pause_after_current_response:
            await self._emit_status(on_status, "已暂停，优先处理你的插话...")
            return self._commit_pause_after_current_response("", assistant_response, tool_calls_made)
        self._append_visual_tool_attachments(base_messages, visual_tool_attachments, source="visual_tool")

        # After the approved batch runs, continue the agent loop normally:
        # call the model, execute any further tool calls, and pause for a
        # new batch confirmation if needed.
        result = ""
        _active_names, tools = self._continue_tool_state(base_messages, tool_calls_made)
        auto_continue_count = 0
        toolless_decision_count = 0
        toolless_decision_pending = False
        try:
            for _round in self._agent_round_iter():
                round_start_ns = self._enter_round(_round, tool_calls_made)
                await self._emit_agent_round_start(on_segment, _round, force=True)
                loop_guard = self._tool_loop_guard(
                    messages=base_messages,
                    tool_calls_made=tool_calls_made,
                    tc_list=[],
                )
                if loop_guard is not None:
                    return loop_guard
                if self._cancel_requested:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_before_call_post_tool")
                    return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                await self._emit_status(on_status, _pre_call_status_label(thinking_enabled))
                response = await self._call_ai(base_messages, tools, on_delta=on_delta, thinking_enabled=thinking_enabled)
                if self._cancel_requested:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text="", reason="cancelled_after_call_post_tool")
                    return self._commit_cancelled_assistant(self._last_partial_text, tool_calls_made)
                if self._stream_truncated_this_round:
                    self._leave_round(round_index=_round, start_ns=round_start_ns, tool_calls_made=tool_calls_made, had_tool_calls=False, text=response.get("content", ""), reason="stream_repetition_stop_post_tool")
                    return self._commit_stream_repetition_stop(response=response, tool_calls_made=tool_calls_made)
                tc_next = response.get("tool_calls") or []
                if tc_next:
                    toolless_decision_pending = False
                if not tc_next:
                    result = response.get("content") or "工具已执行完成。"
                    blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
                    if (
                        self._should_auto_continue(result)
                        and self._auto_continue_allowed(auto_continue_count)
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        self.history.append(ChatMessage(
                            role="assistant",
                            content=result,
                            reasoning_content=response.get("reasoning_content") or "",
                            provider_content=list(response.get("provider_content") or []),
                            tool_trace=tool_calls_made,
                            hidden=True,
                            timestamp=_time.time(),
                        ))
                        base_messages.append({
                            "role": "assistant",
                            "content": result,
                            "reasoning_content": response.get("reasoning_content") or "",
                            "provider_content": response.get("provider_content") or [],
                        })
                        base_messages.append({"role": "user", "content": self._build_auto_continue_prompt()})
                        auto_continue_count += 1
                        self._log_auto_continue_trigger(auto_continue_count, result)
                        await self._emit_status(on_status, "Todo 未完成，自动继续推进...")
                        await self._emit_single_block(on_block, {"type": "todo_list", "mode": "assessment_plan", **self.list_todos()})
                        continue
                    if (
                        self._should_auto_continue(result)
                        and self._auto_continue_exhausted(auto_continue_count)
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        await self._emit_status(on_status, "Todo 自动续跑未产生新进展，等待用户决定。")
                        return self._commit_auto_limit_decision_stop(
                            reason="Todo 仍未完成，但自动续跑后没有新的 Todo 状态、工具结果或证据进展。",
                            current_text=result,
                            tool_calls_made=tool_calls_made,
                        )
                    if (
                        self._should_probe_toolless_continuation(result, tool_calls_made)
                        and self._toolless_continue_allowed(toolless_decision_count)
                        and not toolless_decision_pending
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        self.history.append(ChatMessage(
                            role="assistant",
                            content=result,
                            reasoning_content=response.get("reasoning_content") or "",
                            provider_content=list(response.get("provider_content") or []),
                            tool_trace=tool_calls_made,
                            timestamp=_time.time(),
                            token_usage=self._turn_token_usage_snapshot(),
                        ))
                        base_messages.append({
                            "role": "assistant",
                            "content": result,
                            "reasoning_content": response.get("reasoning_content") or "",
                            "provider_content": response.get("provider_content") or [],
                        })
                        base_messages.append({"role": "user", "content": self._build_toolless_continuation_prompt()})
                        toolless_decision_count += 1
                        toolless_decision_pending = True
                        self._log_toolless_probe_trigger(toolless_decision_count, tool_calls_made, result)
                        await self._emit_status(on_status, "工具后未见明确收尾，正在让 AI 判断是否继续...")
                        continue
                    if (
                        self._should_probe_toolless_continuation(result, tool_calls_made)
                        and self._toolless_continue_exhausted(toolless_decision_count)
                        and not toolless_decision_pending
                        and not self._cancel_requested
                        and not self._pause_after_current_response
                    ):
                        await self._emit_status(on_status, "无工具续跑判断次数已用完，等待用户决定。")
                        return self._commit_auto_limit_decision_stop(
                            reason=f"工具后仍未明确收尾，但已达到无工具续跑判断次数上限 {self._max_toolless_continue_decisions()}",
                            current_text=result,
                            tool_calls_made=tool_calls_made,
                        )
                    acceptance = await self._run_acceptance_closed_loop(
                        result=result,
                        response=response,
                        messages=base_messages,
                        tool_calls_made=tool_calls_made,
                        blocks=blocks,
                        ts=_time.time(),
                        on_status=on_status,
                        on_block=on_block,
                    )
                    if acceptance is not None:
                        if acceptance.get("continue"):
                            continue
                        if acceptance.get("response") is not None:
                            return acceptance["response"]
                    self.history.append(ChatMessage(
                        role="assistant",
                        content=result,
                        reasoning_content=response.get("reasoning_content") or "",
                        provider_content=list(response.get("provider_content") or []),
                        tool_trace=tool_calls_made,
                        timestamp=_time.time(),
                        token_usage=self._turn_token_usage_snapshot(),
                    ))
                    return ChatResponse(text=result, tool_calls=tool_calls_made, blocks=blocks, tool_trace=tool_calls_made, status="done")

                stopped = self._finish_if_implicit_user_question(response, tool_calls_made)
                if stopped is not None:
                    if stopped.status == "awaiting_user_decision":
                        await self._emit_status(on_status, "已暂停，等待用户决定。")
                    return stopped

                if self._pause_after_current_response:
                    await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                    return self._commit_pause_after_current_response(
                        response.get("content") or "",
                        response,
                        tool_calls_made,
                    )

                permission_needed = self._confirmation_requirements(tc_next)
                if permission_needed:
                    self._pending = {
                        "confirmations": permission_needed,
                        "tool_calls": tc_next,
                        "messages": base_messages,
                        "assistant_response": response,
                        "decisions": {},
                        "auto_allow_names": [],
                        "auto_allow_permissions": [],
                    }
                    blocks = self._blocks_for_tool_trace(tool_calls_made)
                    blocks.extend(
                        self._confirm_block(
                            item["name"],
                            item["arguments"],
                            confirm_id=item["tool_call"]["id"],
                            runtime=item.get("runtime", ""),
                            covered_tool_names=item.get("covered_tool_names"),
                        )
                        for item in permission_needed
                    )
                    pending_tools = [
                        self._pending_tool_projection(item)
                        for item in permission_needed
                    ]
                    return ChatResponse(
                        text=response.get("content") or "",
                        tool_calls=tool_calls_made,
                        blocks=blocks,
                        tool_trace=tool_calls_made,
                        pending_tool=pending_tools[0] if pending_tools else None,
                        pending_tools=pending_tools,
                        status="awaiting_confirmation",
                    )

                # No confirmation needed → execute every tool inline.
                base_messages.append({
                    "role": "assistant",
                    "content": response.get("content") or "",
                    "tool_calls": tc_next,
                    "reasoning_content": response.get("reasoning_content") or "",
                    "provider_content": response.get("provider_content") or [],
                })
                self.history.append(ChatMessage(
                    role="assistant", content=response.get("content") or "",
                    tool_calls=tc_next,
                    reasoning_content=response.get("reasoning_content") or "",
                    provider_content=list(response.get("provider_content") or []),
                    timestamp=_time.time(),
                    token_usage=self._turn_token_usage_snapshot(),
                ))
                visual_tool_attachments_next: list[dict[str, Any]] = []
                batch_stop = await self._execute_tool_calls_batch(
                    tc_next,
                    messages=base_messages,
                    tool_calls_made=tool_calls_made,
                    executed_keys=executed_keys,
                    on_block=on_block,
                    on_status=on_status,
                    visual_tool_attachments=visual_tool_attachments_next,
                )
                if batch_stop is not None:
                    return batch_stop
                _active_names, tools = self._continue_tool_state(base_messages, tool_calls_made)
                if self._user_decision_requested:
                    await self._emit_status(on_status, "已暂停，等待用户决定。")
                    return self._commit_user_decision_stop(self._last_partial_text, tool_calls_made, [], base_messages)
                self._append_visual_tool_attachments(base_messages, visual_tool_attachments_next, source="visual_tool")
                if self._pause_after_current_response:
                    await self._emit_status(on_status, "已暂停，优先处理你的插话...")
                    return self._commit_pause_after_current_response("", response, tool_calls_made)
            return self._commit_auto_limit_decision_stop(
                reason=(
                    f"已达到本回合 {self._max_agent_rounds()} 轮工具调用的上限。"
                    "如果是盲注/枚举场景，建议优先考虑 UNION、CAST 报错回显或一次性导出全部行"
                ),
                tool_calls_made=tool_calls_made,
            )
        except Exception as e:
            result = f"工具已执行完成，但后续整理失败: {e}"

        blocks = self._blocks_for_tool_trace(tool_calls_made) or self._blocks_for_text(result)
        self.history.append(ChatMessage(
            role="assistant",
            content=result,
            blocks=blocks,
            tool_trace=tool_calls_made,
            timestamp=_time.time(),
            token_usage=self._turn_token_usage_snapshot(),
        ))
        return ChatResponse(text=result, tool_calls=tool_calls_made, blocks=blocks, tool_trace=tool_calls_made, status="done")


