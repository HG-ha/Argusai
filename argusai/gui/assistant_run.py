"""Assistant GUI mixin slice split from assistant.py."""

from __future__ import annotations

from argusai.core.assistant.run_jobs import AssistantRunJobStore, AssistantRunState

from .assistant_common import *

# Upper bound on how long a cancelled turn may keep its session's busy marker
# before the watchdog force-frees it (normally the turn's finally clears it in
# seconds). Long enough to cover an in-flight AI stream plus a short tool.
_CANCEL_BUSY_RELEASE_SECONDS = 20.0


class AssistantRunMixin:
    def _live_assistant_job_ids(self, session_id: str) -> set[str]:
        """Job ids still owned by an unfinished in-memory stream."""
        live: set[str] = set()
        for stream in list(getattr(self, "_assistant_streams", {}).values()):
            if stream.get("session_id") != session_id or stream.get("done"):
                continue
            job = stream.get("job")
            job_id = getattr(job, "job_id", None) if job is not None else None
            if not job_id:
                job_id = stream.get("protect_job_id")
            if job_id:
                live.add(str(job_id))
        return live

    def list_assistant_run_jobs(self, session_id: str) -> dict[str, Any]:
        """List durable assistant jobs for one session.

        Crash-recovery only applies to orphaned RUNNING snapshots. Jobs still
        owned by a live stream in this process are protected so a UI refresh
        cannot demote an active run to paused.
        """
        try:
            session_id = self._validate_session_id(session_id)
            store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
            jobs = store.list(session_id, protect_job_ids=self._live_assistant_job_ids(session_id))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "jobs": []}
        return {"ok": True, "session_id": session_id, "jobs": [job.to_dict() for job in jobs]}

    def get_assistant_run_job(self, session_id: str, job_id: str) -> dict[str, Any]:
        """Load one durable job without exposing raw filesystem paths."""
        try:
            session_id = self._validate_session_id(session_id)
            store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
            job = store.load(
                session_id,
                job_id,
                protect_job_ids=self._live_assistant_job_ids(session_id),
            )
        except FileNotFoundError:
            return {"ok": False, "error": "assistant job not found"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job": job.to_dict()}

    def dismiss_assistant_run_job(self, session_id: str, job_id: str = "") -> dict[str, Any]:
        """Dismiss one paused/waiting recovery banner without resuming the job."""
        try:
            session_id = self._validate_session_id(session_id)
            job_id = str(job_id or "").strip()
            if not job_id:
                return {"ok": False, "error": "job_id is required"}
            store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
            job = store.load(
                session_id,
                job_id,
                protect_job_ids=self._live_assistant_job_ids(session_id),
            )
        except FileNotFoundError:
            return {"ok": False, "error": "assistant job not found"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if job.state not in {AssistantRunState.PAUSED, AssistantRunState.WAITING_USER}:
            return {
                "ok": False,
                "error": f"assistant job is not dismissible ({job.state.value})",
                "job": job.to_dict(),
            }
        try:
            store.cancel(job, reason="Dismissed by user")
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "job": job.to_dict()}
        return {"ok": True, "job": job.to_dict()}

    def resume_assistant_run_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resume a recovered paused job from an explicit user-authored turn."""
        if not isinstance(params, dict):
            return {"ok": False, "error": "resume params must be an object"}
        message = str(params.get("message") or "").strip()
        if not message:
            return {"ok": False, "error": "a continuation message is required to resume an assistant job"}
        try:
            session_id = self._validate_session_id(params.get("session_id") or "")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            return {"ok": False, "error": "job_id is required"}

        session = self._assistant_sessions.get(session_id)
        if session is not None and getattr(session, "_pending", None):
            return {
                "ok": False,
                "error": "resolve the pending tool confirmation before resuming this job",
            }

        # Use the ordinary chat stream and ordinary user message. In
        # particular, do not set confirmation_action/hide_user_message:
        # confirmations and safety policy must be evaluated exactly as on a
        # newly authored turn.
        stream_params = dict(params)
        stream_params["session_id"] = session_id
        stream_params["message"] = message
        stream_params.pop("confirmation_action", None)
        stream_params.pop("confirmation", None)
        stream_params.pop("hide_user_message", None)
        return self._start_assistant_stream(stream_params, resume_job_id=job_id)

    def cancel_assistant_run(self, session_id: str) -> dict[str, Any]:
        """Stop the session's running turn and every child Agent it spawned.

        The busy marker is deliberately *not* dropped here. A running turn only
        observes ``_cancel_requested`` at the next round/tool boundary; if we
        freed the slot immediately, a new channel message could start a second
        turn on the same session whose ``chat()`` resets that flag, so the first
        turn would never stop. Instead each run clears its own marker in its
        ``finally``; a watchdog force-clears a genuinely stuck one.
        """
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        acp_turn = getattr(self, "_active_acp_turns", {}).get(session_id)
        if acp_turn is not None:
            try:
                acp_turn.request_cancel()
            except Exception:
                pass
        session = self._assistant_sessions.get(session_id)
        if session:
            session.cancel()
            manager = getattr(session, "_agent_thread_manager", None)
            if manager is not None:
                try:
                    manager.cancel_active()
                except Exception as exc:
                    debug_log(f"assistant_cancel_children_failed session_id={session_id} error={exc}")
        cancelled_job_ids: set[str] = set()
        for stream in list(self._assistant_streams.values()):
            if stream.get("session_id") != session_id:
                continue
            job_store = stream.get("job_store")
            job = stream.get("job")
            if job_store is not None and job is not None and not job.is_terminal:
                try:
                    job_store.cancel(job, reason="Cancelled by user")
                    cancelled_job_ids.add(job.job_id)
                except Exception as exc:
                    debug_log(f"assistant_job_cancel_checkpoint_failed session_id={session_id} error={exc}")
            lock = stream.get("lock")
            if lock:
                with lock:
                    stream["done"] = True
                    stream["events"].append({"type": "error", "error": "assistant run cancelled"})
        # Streams are GC'd about a minute after completion. A waiting_user job
        # can therefore outlive its stream; still honour an explicit cancel.
        try:
            store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
            for job in store.list(session_id, recover_running=True):
                if job.job_id in cancelled_job_ids or job.is_terminal:
                    continue
                if job.state not in {AssistantRunState.WAITING_USER, AssistantRunState.PAUSED}:
                    continue
                try:
                    store.cancel(job, reason="Cancelled by user")
                except Exception as exc:
                    debug_log(
                        f"assistant_job_waiting_cancel_failed session_id={session_id} "
                        f"job_id={job.job_id} error={exc}"
                    )
        except Exception as exc:
            debug_log(f"assistant_job_store_cancel_scan_failed session_id={session_id} error={exc}")
        self._schedule_cancel_busy_release(session_id)
        return {"ok": True}

    def _schedule_cancel_busy_release(self, session_id: str) -> None:
        """Force-free a session whose cancelled turn never reached its finally.

        The turn normally clears its own busy marker when it unwinds, which is
        what lets a queued channel message run next. This watchdog is only the
        backstop for a turn wedged in a long tool or stream.
        """
        import threading
        import time

        with self._assistant_run_lock:
            if session_id not in self._assistant_running_sessions:
                return

        def _watchdog() -> None:
            deadline = time.monotonic() + _CANCEL_BUSY_RELEASE_SECONDS
            while time.monotonic() < deadline:
                with self._assistant_run_lock:
                    if session_id not in self._assistant_running_sessions:
                        return
                time.sleep(0.1)
            with self._assistant_run_lock:
                self._assistant_running_sessions.discard(session_id)
            debug_log(f"assistant_cancel_force_released session_id={session_id}")

        threading.Thread(target=_watchdog, name="assistant-cancel-release", daemon=True).start()


    def pause_assistant_after_current_response(self, session_id: str) -> dict[str, Any]:
        """Ask a running assistant turn to stop before hidden auto-continue.

        Used by the UI's queued interjection feature: the current tool/result
        cycle is allowed to finish, then the queued user message gets the next
        turn instead of waiting for the whole multi-step task to complete.
        """
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        session = self._assistant_sessions.get(session_id)
        if not session:
            return {"ok": False, "error": "assistant session is not running"}
        session.pause_after_current_response()
        return {"ok": True, "session_id": session_id}


    def assistant_side_question(self, session_id: str, question: str = "") -> dict[str, Any]:
        """Answer one question about a conversation without joining it.

        Backs ``/btw``. The exchange is never written to history, so it costs no
        context on later turns, and the running turn is left completely alone —
        which is the point: you ask while the agent is still working. The side
        agent gets no tools; it answers from the transcript or says it can't.
        """
        import asyncio
        import concurrent.futures

        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        text = str(question or "").strip()[:4000]
        if not text:
            return {"ok": False, "error": "side question is empty"}
        app_settings = self.settings_mgr.get()
        if not (app_settings.ai_enabled and app_settings.ai_endpoints):
            return {"ok": False, "error": "no AI endpoint is configured"}
        try:
            session = self._ensure_session(session_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        from argusai.core.ai import create_provider
        from argusai.core.assistant.side_question import answer_side_question

        try:
            provider = create_provider(endpoints=session.native_model_endpoints(app_settings.ai_endpoints))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if provider is None:
            return {"ok": False, "error": "no AI endpoint is configured"}
        # Snapshots, because the running turn keeps appending to the real list
        # and its provider is closed the moment that turn ends.
        history = list(session.history)
        memory_summary = str(getattr(session, "memory_summary", "") or "")

        async def _ask() -> str:
            try:
                return await answer_side_question(
                    provider,
                    history,
                    text,
                    memory_summary=memory_summary,
                )
            finally:
                try:
                    await provider.close()
                except Exception:
                    pass

        try:
            asyncio.get_running_loop()
            nested = True
        except RuntimeError:
            nested = False
        try:
            if nested:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    answer = pool.submit(asyncio.run, _ask()).result()
            else:
                answer = asyncio.run(_ask())
        except Exception as exc:
            debug_log(f"assistant_side_question_failed session_id={session_id} error={exc}")
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "session_id": session_id,
            "question": text,
            "text": answer,
            "persisted": False,
        }


    def confirm_assistant_action(
        self,
        session_id: str,
        action: str | bool | dict[str, Any],
    ) -> dict[str, Any]:
        # Loom22 §9: typed confirmation RPC. Prefer a structured ``confirmation``
        # object; legacy bool/string tokens are migrated into that shape.
        from argusai.core.assistant.streaming import (
            confirmation_decision_from_legacy_message,
            normalize_confirmation_decision,
        )

        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        confirmation: dict[str, Any] | None = None
        if isinstance(action, dict):
            confirmation = normalize_confirmation_decision(action)
        elif isinstance(action, bool):
            confirmation = normalize_confirmation_decision({
                "type": "confirmation_decision",
                "verdict": "allow" if action else "deny",
            })
        else:
            confirmation = confirmation_decision_from_legacy_message(str(action or ""))
        if confirmation is None:
            return {"ok": False, "error": "confirmation action is required"}
        session = self._ensure_session(session_id)
        from argusai.core.assistant.confirmation_broker import ConfirmationBroker

        broker = getattr(self, "_assistant_confirmation_broker", None) or ConfirmationBroker()
        external = broker.resolve_external(
            session_id,
            session,
            confirmation,
            source="gui",
        )
        if external is not None:
            return external
        prepared = broker.prepare(session, confirmation, source="gui")
        if not prepared.get("ok"):
            return prepared
        confirmation = prepared["decision"]
        return self._run_assistant_chat({
            "session_id": session_id,
            "message": "",
            "confirmation": confirmation,
            "confirmation_action": True,
            "hide_user_message": True,
            "_typed_action_rpc": True,
        })


    def _run_acp_assistant_turn(
        self,
        params: dict[str, Any],
        on_delta: Any = None,
        on_status: Any = None,
        on_block: Any = None,
        on_segment: Any = None,
    ) -> dict[str, Any]:
        """Run one turn through the session's explicitly bound ACP runtime."""
        import asyncio
        import concurrent.futures
        from dataclasses import replace

        from argusai.core.agent_runtimes import (
            ArgusAIToolBroker,
            acp_workspace_instruction,
            run_acp_turn,
            runtime_configs,
        )
        from argusai.core.assistant.attachments import (
            _materialize_acp_media_attachments,
            _materialize_binary_attachments,
            _materialize_text_attachments,
            _materialize_video,
            _materialize_voice,
            _sanitize_attachments,
            _sanitize_voice,
        )
        from argusai.core.assistant.messages import ChatMessage
        from argusai.core.assistant.ui_protocol import selection_reference_to_prompt_text
        from argusai.core.paths import PathManager

        message = str(params.get("message") or "").strip()
        try:
            session_id = self._validate_session_id(params.get("session_id") or "default")
            session_path = self._session_path(session_id)
            session = self._ensure_session(session_id)
            binding = self._assistant_runtime_binding(session_id, params.get("runtime"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        runtime = next(
            (
                item
                for item in runtime_configs(self.settings_mgr.get().agent_runtimes)
                if item.id == binding.runtime_id and item.enabled
            ),
            None,
        )
        if runtime is None or runtime.backend != "acp":
            return {
                "ok": False,
                "error_code": "acp_runtime_unavailable",
                "error": f"ACP runtime is unavailable: {binding.runtime_id}",
            }
        paths = PathManager(data_root=self.settings_mgr.get().data_root)
        sandbox_root = paths.assistant_sandbox(session_id)
        cwd = (
            str(Path(binding.project_dir).expanduser().resolve())
            if binding.workspace_mode == "project"
            else str(sandbox_root)
        )
        attachments = _sanitize_attachments(list(params.get("attachments") or []))
        voice = _sanitize_voice(
            params.get("voice") if isinstance(params.get("voice"), dict) else None
        )
        selection_reference = (
            params.get("selection_reference")
            if isinstance(params.get("selection_reference"), dict)
            else None
        )

        if selection_reference and selection_reference.get("kind") == "image_attachment":
            referenced_id = str(selection_reference.get("attachment_id") or "")
            referenced_name = str(selection_reference.get("attachment_name") or "")
            referenced: dict[str, Any] | None = None
            named_fallback: dict[str, Any] | None = None
            for historical in reversed(session.history):
                for item in historical.attachments or []:
                    if not isinstance(item, dict) or item.get("type") != "image":
                        continue
                    if referenced_id and str(item.get("id") or "") == referenced_id:
                        referenced = item
                        break
                    if (
                        named_fallback is None
                        and referenced_name
                        and str(item.get("name") or "") == referenced_name
                    ):
                        named_fallback = item
                if referenced is not None:
                    break
            if referenced is None:
                referenced = named_fallback
            if referenced is not None:
                recovered = _sanitize_attachments([referenced])
                if recovered:
                    attachments.extend(recovered)

        if not message and not attachments and voice is None:
            return {
                "ok": False,
                "error_code": "acp_prompt_required",
                "error": "ACP turns require text or an attachment",
            }

        history_attachments, text_addendum = _materialize_text_attachments(
            attachments,
            sandbox_root,
        )
        history_attachments, binary_addendum = _materialize_binary_attachments(
            history_attachments,
            sandbox_root,
        )
        history_attachments = _materialize_video(history_attachments, sandbox_root)
        history_attachments = _materialize_acp_media_attachments(
            history_attachments,
            sandbox_root,
        )
        stored_voice = _materialize_voice(voice, sandbox_root)
        prompt_attachments = [dict(item) for item in attachments]
        attachment_resources = [dict(item) for item in history_attachments]
        if voice is not None:
            prompt_voice = {
                **voice,
                "type": "audio",
                "name": str(voice.get("name") or "voice-message"),
            }
            prompt_attachments.append(prompt_voice)
            attachment_resources.append({
                **(stored_voice or {}),
                "type": "audio",
                "name": str((stored_voice or {}).get("name") or "voice-message"),
            })
        prompt_parts = [message]
        reference_text = selection_reference_to_prompt_text(selection_reference)
        if reference_text:
            prompt_parts.append(reference_text)
        if text_addendum:
            prompt_parts.append(text_addendum.strip())
        if binary_addendum:
            prompt_parts.append(binary_addendum.strip())
        prompt_message = "\n\n".join(part for part in prompt_parts if part)
        workspace_note = acp_workspace_instruction(
            cwd,
            project=binding.workspace_mode == "project",
        )
        # Repeat the workspace line only when this process has no ACP session
        # yet. Later turns keep cwd via session/load; rewriting the user prefix
        # every time spends uncached tail tokens for no extra capability.
        if workspace_note and not binding.acp_session_id:
            prompt_message = f"{workspace_note}{prompt_message}".strip()

        binding = replace(binding, locked=True)
        session.runtime_binding = binding
        try:
            from argusai.core.tools.sandbox_code import _ensure_workspace_codebase_scope

            _ensure_workspace_codebase_scope(
                self,
                {"_session_id": session_id, "_session": session},
            )
        except Exception:
            pass
        now = time.time()
        user_message = ChatMessage(
            role="user",
            content=message,
            attachments=history_attachments,
            voice=stored_voice,
            selection_reference=selection_reference,
            hidden=bool(params.get("hide_user_message")),
            ui_hidden=bool(params.get("hide_user_message")),
            source=str(params.get("message_source") or ""),
            source_ref=str(params.get("message_source_ref") or ""),
            timestamp=now,
        )
        session.history.append(user_message)
        transcript_rows: list[str] = []
        transcript_size = 0
        for item in reversed(session.history[:-1]):
            if item.hidden or item.ui_hidden or item.role not in {"user", "assistant"}:
                continue
            content = str(item.content or "").strip()
            attachment_names = [
                str(attachment.get("name") or attachment.get("sandbox_rel") or "")
                for attachment in item.attachments or []
                if isinstance(attachment, dict)
            ]
            if attachment_names:
                marker = "[attachments: " + ", ".join(
                    name for name in attachment_names if name
                )[:500] + "]"
                content = f"{content}\n{marker}".strip()
            if not content:
                continue
            row = f"{item.role.upper()}: {content}"
            if transcript_size + len(row) > 24_000:
                break
            transcript_rows.append(row)
            transcript_size += len(row)
            if len(transcript_rows) >= 24:
                break
        transcript_rows.reverse()
        stateless_message = "\n\n".join([
            "The ACP runtime cannot reload its previous process session. "
            "Continue the same conversation from this ArgusAI transcript.",
            *transcript_rows,
            f"USER: {prompt_message}",
        ])
        session_name = message[:30] + ("..." if len(message) > 30 else "")
        if not session_name and history_attachments:
            session_name = str(
                history_attachments[0].get("name")
                or history_attachments[0].get("type")
                or "附件会话"
            )[:30]
        if not session_name and stored_voice:
            session_name = "语音消息"
        if session_path.exists():
            try:
                saved_name = str(json.loads(session_path.read_text(encoding="utf-8")).get("name") or "").strip()
                if saved_name and saved_name not in _DEFAULT_ASSISTANT_SESSION_NAMES:
                    session_name = saved_name
            except Exception:
                pass
        if on_status:
            on_status(f"正在连接本地 Agent：{runtime.name}")
        if on_segment:
            on_segment(1)

        tool_updates: list[dict[str, Any]] = []
        argusai_tool_traces: list[dict[str, Any]] = []

        from argusai.core.agent_runtimes.tool_broker import MCP_SERVER_NAME

        mcp_title_fingerprint = "".join(
            char for char in MCP_SERVER_NAME.casefold() if char.isalnum()
        )

        def _on_update(update: dict[str, Any]) -> None:
            kind = str(update.get("sessionUpdate") or "")
            content = update.get("content")
            if kind == "agent_message_chunk" and isinstance(content, dict):
                text = str(content.get("text") or "") if content.get("type") == "text" else ""
                if text and on_delta:
                    on_delta(text)
            elif kind == "agent_thought_chunk":
                if on_status:
                    on_status(f"{runtime.name} 正在思考...")
            elif kind in {"tool_call", "tool_call_update"}:
                tool_updates.append(dict(update))
                title = str(update.get("title") or "")
                folded_title = "".join(
                    char for char in title.casefold() if char.isalnum()
                )
                if mcp_title_fingerprint in folded_title:
                    # This is the outer ACP view of an ArgusAI MCP call. The
                    # broker emits the native tool start/result blocks below,
                    # which carry real arguments, risk and typed result data.
                    return
                block = {
                    "type": "tool_activity",
                    "runtime": "acp",
                    "runtime_id": runtime.id,
                    "tool_call_id": str(update.get("toolCallId") or ""),
                    "title": title,
                    "status": str(update.get("status") or "in_progress"),
                    "kind": str(update.get("kind") or "other"),
                }
                if on_block:
                    on_block(block)
                if on_status and block["title"]:
                    on_status(block["title"])
            elif kind == "plan" and on_block:
                on_block({
                    "type": "todo_list",
                    "runtime": "acp",
                    "runtime_id": runtime.id,
                    "items": list(update.get("entries") or []),
                })

        def _on_tool_start(name: str, _arguments: dict[str, Any]) -> None:
            if on_status:
                on_status(f"正在执行工具：{name}")

        def _on_tool_trace(trace: dict[str, Any]) -> None:
            item = dict(trace)
            argusai_tool_traces.append(item)
            if on_block:
                try:
                    rendered = session._blocks_for_tool_trace([item])
                except Exception:
                    rendered = [{"type": "tool_trace", **item}]
                for block in rendered:
                    on_block({
                        **block,
                        "runtime": "acp",
                        "runtime_id": runtime.id,
                    })
            if on_status:
                on_status("工具执行完成，正在整理结果...")

        def _on_active(handle: Any) -> None:
            self._active_acp_turns[session_id] = handle

        def _checkpoint() -> None:
            try:
                self._save_session(session_path, session, session_name)
            except Exception as exc:
                debug_log(f"acp_session_checkpoint_failed session_id={session_id} error={exc}")

        def _on_pending(pending: dict[str, Any]) -> None:
            from argusai.core.assistant.streaming import EXTERNAL_AGENT_TOOL

            own_tool = str(pending.get("name") or "") == EXTERNAL_AGENT_TOOL
            what = "它自己的工具" if own_tool else "一项需要授权的 ArgusAI 工具"
            projection = session.pending_confirmation_projection()
            pending_blocks = [
                {
                    **block,
                    "external_confirmation": True,
                    "runtime": "acp",
                    "runtime_id": runtime.id,
                }
                for block in list(projection.get("pending_blocks") or [])
                if isinstance(block, dict)
            ]
            if on_block:
                for block in pending_blocks:
                    on_block(block)
            # ACP confirmation blocks the turn until the user answers, so the
            # normal end-of-turn channel fan-out never runs. Push wait cards now.
            push_wait = getattr(self, "_push_channel_wait_cards", None)
            if callable(push_wait) and pending_blocks:
                try:
                    push_wait(
                        session_id,
                        {
                            "ok": True,
                            "status": "awaiting_confirmation",
                            "blocks": pending_blocks,
                            "pending_tools": list(projection.get("pending_tools") or []),
                        },
                    )
                except Exception as push_exc:
                    debug_log(
                        f"acp_channel_wait_card_push_failed "
                        f"session_id={session_id} error={push_exc}"
                    )
            if on_status:
                on_status(f"本地 Agent 请求执行{what}，等待授权...")
            try:
                self._notify_attention(
                    f"acp-confirm:{session_id}",
                    title="本地 Agent 等待工具确认",
                    message=f"{runtime.name} 请求执行{what}。",
                    level="warning",
                )
            except Exception:
                pass

        confirmation_broker = getattr(
            self,
            "_assistant_confirmation_broker",
            None,
        )
        if confirmation_broker is None:
            from argusai.core.assistant.confirmation_broker import ConfirmationBroker

            confirmation_broker = ConfirmationBroker()
            self._assistant_confirmation_broker = confirmation_broker
        tool_broker = getattr(self, "_assistant_tool_broker", None)
        if tool_broker is None:
            tool_broker = ArgusAIToolBroker()
            self._assistant_tool_broker = tool_broker
        turn_timeout_seconds = float(
            getattr(self.settings_mgr.get(), "assistant_turn_deadline_seconds", 604800)
            or 604800
        )
        broker_token = tool_broker.register_session(
            session_id,
            session,
            confirmation_broker,
            on_pending=_on_pending,
            on_tool_start=_on_tool_start,
            on_tool_trace=_on_tool_trace,
            on_checkpoint=_checkpoint,
            confirmation_timeout_seconds=turn_timeout_seconds,
        )
        mcp_server = tool_broker.mcp_server(broker_token)

        async def _run() -> dict[str, Any]:
            result = await run_acp_turn(
                runtime,
                binding,
                message=prompt_message,
                cwd=cwd,
                attachments=prompt_attachments,
                attachment_resources=attachment_resources,
                on_update=_on_update,
                on_active=_on_active,
                on_permission=tool_broker.permission_handler(broker_token),
                mcp_servers=[mcp_server],
                stateless_message=stateless_message,
                timeout_seconds=turn_timeout_seconds,
            )
            return result.to_dict()

        try:
            try:
                asyncio.get_running_loop()
                nested = True
            except RuntimeError:
                # Resolve outside the except body: running asyncio.run while
                # sys.exc_info() still holds this RuntimeError makes ACP
                # shutdown errors chain as "During handling of the above…".
                nested = False
            if nested:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(asyncio.run, _run()).result()
            else:
                result = asyncio.run(_run())
        finally:
            self._active_acp_turns.pop(session_id, None)
            tool_broker.unregister(broker_token)

        text = str(result.get("text") or "")
        reasoning = str(result.get("reasoning") or "")
        if result.get("acp_session_id") or result.get("prompt_capabilities"):
            options = [
                dict(item)
                for item in list(result.get("config_options") or [])
                if isinstance(item, dict)
            ]
            session.runtime_binding = replace(
                binding,
                acp_session_id=str(
                    result.get("acp_session_id") or binding.acp_session_id
                ),
                locked=True,
                acp_config=dict(binding.acp_config or {}),
                acp_config_options=tuple(options) if options else binding.acp_config_options,
                acp_prompt_capabilities={
                    str(key): bool(value)
                    for key, value in dict(result.get("prompt_capabilities") or {}).items()
                    if key in {"image", "audio", "embedded_context"}
                },
            )
        try:
            blocks = session._blocks_for_tool_trace(argusai_tool_traces)
        except Exception:
            blocks = [{"type": "tool_trace", **item} for item in argusai_tool_traces]
        blocks.append({
            "type": "runtime_trace",
            "runtime": "acp",
            "runtime_id": runtime.id,
            "runtime_name": runtime.name,
            "stop_reason": str(result.get("stop_reason") or ""),
            "continuity": (
                "transcript_replay"
                if result.get("session_restarted")
                else "native_session"
            ),
            "status": "completed" if result.get("ok") else "failed",
            "update_count": len(result.get("updates") or []),
        })
        if text or reasoning or argusai_tool_traces:
            session.history.append(ChatMessage(
                role="assistant",
                content=text,
                reasoning_content=reasoning,
                blocks=blocks,
                tool_trace=argusai_tool_traces,
                timestamp=time.time(),
            ))
        _checkpoint()
        if not result.get("ok"):
            return {
                **result,
                "blocks": blocks,
                "tool_trace": argusai_tool_traces,
                "status": "cancelled" if result.get("error_code") == "acp_turn_cancelled" else "ai_error",
                "session_name": session_name,
                "user_timestamp": now,
                "runtime": session.runtime_binding.to_dict(),
            }
        return {
            "ok": True,
            "text": text,
            "tool_calls": [],
            "options": [],
            "pending_tool": None,
            "pending_tools": [],
            "blocks": blocks,
            "tool_trace": argusai_tool_traces,
            "plan": None,
            "status": "done",
            "session_name": session_name,
            "user_timestamp": now,
            "acp_session_id": result.get("acp_session_id"),
            "runtime": session.runtime_binding.to_dict(),
        }


    def _run_assistant_chat(
        self,
        params: dict[str, Any],
        on_delta: Any = None,
        on_status: Any = None,
        on_block: Any = None,
        on_segment: Any = None,
        _routed: bool = False,
    ) -> dict[str, Any]:
        """Chat with the AI assistant.

        ``params`` expects::

            {
                "message": "user message",
                "session_id": "optional-session-id",
                "page": "current app page (tasks/task/proxy/settings/new)",
                "context": {"task_id": "...", ...},
                "allow_web_search": True,  # optional per-session toolbar override
            }

        ``allow_web_search`` (optional) is the per-session toolbar toggle and takes
        precedence over the global ``assistant_enable_web_search`` setting; omit it
        (or pass ``None``) to inherit the global default.
        """
        if not _routed:
            from argusai.core.assistant.turn_router import (
                AssistantTurnRequest,
                AssistantTurnRouter,
            )

            router = getattr(self, "_assistant_turn_router", None) or AssistantTurnRouter()
            routed_params = dict(params)
            requested_runtime = routed_params.get("runtime")
            if not isinstance(requested_runtime, dict):
                requested_runtime = {
                    "runtime_id": routed_params.get("runtime_id"),
                    "workspace_mode": routed_params.get("workspace_mode"),
                    "project_dir": routed_params.get("project_dir"),
                }
            try:
                binding = self._assistant_runtime_binding(
                    routed_params.get("session_id") or "default",
                    requested_runtime,
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            routed_params["runtime_id"] = binding.runtime_id
            routed_params["runtime"] = binding.to_dict()
            request = AssistantTurnRequest.from_params(routed_params)
            return router.route_sync(
                request,
                native_handler=lambda routed: self._run_assistant_chat(
                    routed.params,
                    on_delta=on_delta,
                    on_status=on_status,
                    on_block=on_block,
                    on_segment=on_segment,
                    _routed=True,
                ),
                runtime_handler=lambda routed: self._run_acp_assistant_turn(
                    routed.params,
                    on_delta=on_delta,
                    on_status=on_status,
                    on_block=on_block,
                    on_segment=on_segment,
                ),
            )
        message = (params.get("message") or "").strip()
        if (
            (params.get("confirmation_action") or params.get("confirmation"))
            and not params.get("_typed_action_rpc")
        ):
            return {
                "ok": False,
                "error": "confirmation actions must use the typed action RPC",
            }
        if not assistant_chat_has_input(params):
            return {"ok": False, "error": "message or attachment is required"}

        if on_status:
            on_status("正在准备会话...")

        page = params.get("page") or ""
        session_id = params.get("session_id") or "default"
        try:
            session_id = self._validate_session_id(session_id)
            session_path = self._session_path(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        # The per-session toolbar toggle (联网 开/关) is the authoritative, highest
        # priority source of truth for web search: the frontend sends it on every
        # turn so "what the user sees in the toolbar" always wins over the global
        # ``assistant_enable_web_search`` default (which only seeds brand-new
        # sessions). ``None`` means the caller didn't express an opinion (e.g. a
        # programmatic / headless invocation) — fall back to the global default.
        raw_allow_web = params.get("allow_web_search")
        allow_web_search_override = bool(raw_allow_web) if raw_allow_web is not None else None

        if session_id not in self._assistant_sessions:
            if on_status:
                on_status("正在初始化助手工具...")
            saved_data: dict[str, Any] | None = None
            if session_path.exists():
                try:
                    saved_data = json.loads(session_path.read_text(encoding="utf-8"))
                except Exception:
                    saved_data = None
            allow_web_search = bool(
                (saved_data or {}).get("allow_web_search", self._default_assistant_web_search())
            )
            registry = build_tool_registry(self, session_id, allow_web_search=allow_web_search)
            provider = None
            try:
                if on_status:
                    on_status("正在读取 AI 配置...")
                app_settings = self.settings_mgr.get()
                if app_settings.ai_enabled and app_settings.ai_endpoints:
                    from argusai.core.ai import create_provider
                    if on_status:
                        on_status("正在初始化 AI 端点...")
                    provider = create_provider(endpoints=app_settings.ai_endpoints)
            except Exception:
                pass

            if session_path.exists():
                try:
                    if on_status:
                        on_status("正在加载助手会话...")
                    data = saved_data if saved_data is not None else json.loads(session_path.read_text(encoding="utf-8"))
                    session = AssistantSession.from_dict(data, registry, provider, kb=self.kb)
                except Exception:
                    session = AssistantSession(registry=registry, ai_provider=provider, kb=self.kb)
                    session.allow_web_search = allow_web_search
            else:
                session = AssistantSession(registry=registry, ai_provider=provider, kb=self.kb)
                session.allow_web_search = allow_web_search

            self._apply_session_auto_approve_policy(session)
            self._assistant_sessions[session_id] = session

        session = self._assistant_sessions[session_id]
        self._apply_session_auto_approve_policy(session)
        from dataclasses import replace
        from argusai.core.agent_runtimes import normalize_session_runtime_binding

        binding = normalize_session_runtime_binding(params.get("runtime"))
        if assistant_chat_has_input(params) and not binding.locked:
            binding = replace(binding, locked=True)
        session.runtime_binding = binding
        try:
            from argusai.core.tools.sandbox_code import _ensure_workspace_codebase_scope

            _ensure_workspace_codebase_scope(
                self,
                {"_session_id": session_id, "_session": session},
            )
        except Exception:
            pass
        # Per-turn WYSIWYG reconcile: honor the per-session toolbar toggle as the
        # highest priority. Without this, a brand-new session created while the
        # global default is off would stay off even though the user flipped the
        # toolbar on (the registry would be missing ``web_search``), and a cached
        # session could drift from what the toolbar shows.
        if (
            allow_web_search_override is not None
            and bool(getattr(session, "allow_web_search", True)) != allow_web_search_override
        ):
            session.allow_web_search = allow_web_search_override
            try:
                self._refresh_session_registry(session_id, session)
            except Exception:
                pass
        raw_thinking = params.get("thinking_enabled")
        if isinstance(raw_thinking, bool):
            thinking_enabled = raw_thinking
        else:
            thinking_enabled = bool(getattr(session, "thinking_enabled", False))
        raw_effort = params.get("thinking_effort")
        thinking_effort = raw_effort if isinstance(raw_effort, str) and raw_effort.strip() else None
        if thinking_effort is not None:
            from argusai.core.ai_thinking import normalize_thinking_effort

            session.thinking_effort = normalize_thinking_effort(thinking_effort)
        # Cached sessions keep their original provider, so edits made in
        # Settings → AI 端点 (model, key, max_tokens, ...) would otherwise only
        # take effect after a restart. Rebuild the provider from the current
        # settings on every turn so "改配置" actually applies in place.
        try:
            app_settings = self.settings_mgr.get()
            if app_settings.ai_enabled and app_settings.ai_endpoints:
                from argusai.core.ai import create_provider
                session.ai = create_provider(endpoints=session.native_model_endpoints(app_settings.ai_endpoints))
            else:
                session.ai = None
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            pass

        if on_status:
            on_status("正在进入助手循环...")

        import asyncio
        import concurrent.futures

        # Resolve the per-session sandbox so any text/code files the user
        # dragged into the assistant get spilled there. We don't fail the
        # chat if sandbox resolution blows up — the assistant has a
        # documented degraded mode that inlines the preview only.
        sandbox_root_for_chat: Path | None = None
        try:
            from argusai.core.paths import PathManager
            sandbox_root_for_chat = PathManager(data_root=self.settings_mgr.get().data_root).assistant_sandbox(session_id)
        except Exception:
            sandbox_root_for_chat = None

        def _resolve_session_name() -> str:
            """Pick the session name we'll record on disk.

            Reuses the existing on-disk name if the session file already
            exists (so checkpoints don't keep renaming the session to the
            *latest* user message). Falls back to a snippet of the
            current user message for brand-new sessions.
            """
            sp = session_path
            if sp.exists():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    nm = str(data.get("name") or "").strip()
                    if nm and nm not in _DEFAULT_ASSISTANT_SESSION_NAMES:
                        return nm
                except Exception:
                    pass
            if message:
                return message[:30] + ("..." if len(message) > 30 else "")
            voice = params.get("voice")
            if isinstance(voice, dict):
                return "语音消息"
            for att in params.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                label = str(att.get("name") or "").strip()
                if att.get("type") == "audio":
                    return label or "语音"
                if label:
                    return label[:30] + ("..." if len(label) > 30 else "")
            return "新会话"

        def _persist_session_now() -> None:
            """Flush the in-memory session to disk RIGHT NOW.

            Wired up as the session's ``on_checkpoint`` callback (Fix A)
            and also called from the ``finally`` block (Fix B) so any
            exception path still leaves an up-to-date snapshot. Failures
            here are intentionally swallowed — losing a single
            checkpoint must not kill the whole assistant turn.
            """
            try:
                name = _resolve_session_name()
                self._save_session(session_path, session, name)
            except Exception as save_err:
                debug_log(f"assistant_checkpoint_failed session_id={session_id} error={save_err}")

        async def _chat() -> dict[str, Any]:
            try:
                with session_log_context(session_id):
                    resp = await session.chat(
                        message,
                        page=page,
                        context=params.get("context") or {},
                        on_delta=on_delta,
                        on_status=on_status,
                        on_block=on_block,
                        on_segment=on_segment,
                        on_checkpoint=_persist_session_now,
                        regenerate=bool(params.get("regenerate")),
                        regenerate_before=float(params.get("regenerate_before") or 0) or None,
                        regenerate_user_before=(
                            float(params.get("regenerate_user_before") or 0) or None
                        ),
                        thinking_enabled=thinking_enabled,
                        thinking_effort=thinking_effort,
                        attachments=params.get("attachments") or [],
                        voice=params.get("voice"),
                        selection_reference=params.get("selection_reference"),
                        sandbox_root=sandbox_root_for_chat,
                        hide_user_message=bool(params.get("hide_user_message")),
                        confirmation_action=bool(params.get("confirmation_action")),
                        confirmation=(
                            params.get("confirmation")
                            if isinstance(params.get("confirmation"), dict)
                            else None
                        ),
                        message_source=str(params.get("message_source") or ""),
                        message_source_ref=str(params.get("message_source_ref") or ""),
                    )
                latest_user_timestamp = 0.0
                for history_item in reversed(session.history):
                    if history_item.role == "user" and not history_item.hidden:
                        latest_user_timestamp = float(history_item.timestamp or 0)
                        break
                return {
                    "ok": True,
                    "text": resp.text,
                    "tool_calls": resp.tool_calls,
                    "options": [o.to_dict() for o in resp.options],
                    "pending_tool": resp.pending_tool,
                    "pending_tools": resp.pending_tools,
                    "blocks": resp.blocks,
                    "tool_trace": resp.tool_trace,
                    "plan": resp.plan,
                    "status": resp.status,
                    "session_id": session_id,
                    "session_name": _resolve_session_name(),
                    "user_timestamp": latest_user_timestamp,
                    "token_usage": dict(session.token_usage_total),
                    "last_token_usage": session.last_token_usage,
                    "cache_hit_rate": session.cache_hit_rate(),
                    **self._session_prompt_context(session),
                }
            finally:
                # Final flush guarantees the on-disk snapshot reflects
                # whatever the in-memory session has accumulated, even
                # if ``session.chat`` re-raised. Without this a network
                # blowup in round 47 of an 80-round agent loop would
                # silently revert the user's history to round 0.
                _persist_session_now()
                # Release the turn's pooled HTTP client. The provider is
                # rebuilt from current settings on every turn (above), so the
                # one we used here is discarded — close it on this turn's loop
                # before it ends, otherwise the pooled httpx.AsyncClient leaks.
                try:
                    if session.ai is not None:
                        await session.ai.close()
                except Exception:
                    pass

        try:
            asyncio.get_running_loop()
            nested = True
        except RuntimeError:
            nested = False
        if nested:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, _chat()).result()
        return asyncio.run(_chat())


    def chat_assistant(self, params: dict[str, Any]) -> dict[str, Any]:
        public_params = dict(params or {})
        public_params.pop("_typed_action_rpc", None)
        return self._run_assistant_chat(public_params)


