"""Assistant GUI mixin slice split from assistant.py."""

from __future__ import annotations

import re

from argusai.core.assistant.run_jobs import AssistantRunJobStore, AssistantRunState
from argusai.core.assistant.ui_protocol import normalize_block

from .assistant_common import *


# Pre-compiled patterns for the legacy Chinese status strings. They keep the
# v1 ``type=status`` event flowing while letting us emit a typed v2
# ``kind=tool_activity``/``stage`` envelope on the same event.
_TOOL_START_RE = re.compile(r"^正在执行工具[:：]\s*(.+?)(?:（上限\s*(\d+)s）)?$")
_TOOL_END_TOKENS = ("工具执行完成",)


def _classify_status_to_v2(text: str) -> tuple[str, dict[str, Any]]:
    """Return ``(kind, data)`` for a legacy status text.

    The chat loop pushes localized status strings; we keep that source of
    truth but layer a typed envelope on top so the frontend can stop parsing
    Chinese.
    """
    s = str(text or "")
    m = _TOOL_START_RE.match(s)
    if m:
        tool = m.group(1).strip()
        data: dict[str, Any] = {"tool": tool, "status": "start"}
        if m.group(2):
            data["timeout_seconds"] = int(m.group(2))
        return "tool_activity", data
    if any(token in s for token in _TOOL_END_TOKENS):
        return "tool_activity", {"status": "end", "summary": s}
    return "stage", {"label": s}


class AssistantStreamMixin:
    _ASSISTANT_STREAM_GC_DELAY = 60.0

    def _stream_hub(self):
        from argusai.core.assistant.stream_hub import StreamHub

        hub = getattr(self, "_assistant_stream_hub", None)
        if hub is None:
            hub = StreamHub(self._assistant_streams)
            self._assistant_stream_hub = hub
        return hub

    def _regenerate_user_target_exists(self, session_id: str, target_ts: float) -> bool:
        """Validate a branch point before the frontend drops its visible tail."""
        session = self._assistant_sessions.get(session_id)
        if session is not None:
            return any(
                item.role == "user"
                and not item.hidden
                and item.timestamp
                and abs(float(item.timestamp) - target_ts) <= 0.001
                for item in session.history
            )
        path = self._session_path(session_id)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return any(
            isinstance(item, dict)
            and item.get("role") == "user"
            and not item.get("hidden")
            and item.get("timestamp")
            and abs(float(item["timestamp"]) - target_ts) <= 0.001
            for item in (data.get("history") or [])
        )

    def start_assistant_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start a new durable assistant job."""
        if params.get("confirmation_action") or params.get("confirmation"):
            return {
                "ok": False,
                "error": "confirmation actions must use start_assistant_action_stream",
            }
        return self._start_assistant_stream(params)

    def start_assistant_action_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resume a pending turn from a typed approval action."""

        from argusai.core.assistant.streaming import normalize_confirmation_decision

        action = normalize_confirmation_decision(params.get("action"))
        if action is None:
            return {"ok": False, "error": "invalid confirmation action"}
        stream_params = dict(params)
        stream_params.pop("action", None)
        try:
            external_session_id = self._validate_session_id(
                stream_params.get("session_id") or "default"
            )
            external_session = self._assistant_sessions.get(external_session_id)
            broker = getattr(self, "_assistant_confirmation_broker", None)
            if external_session is not None and broker is not None:
                external = broker.resolve_external(
                    external_session_id,
                    external_session,
                    action,
                    source="gui",
                )
                if external is not None:
                    return external
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        resume_job_id = str(stream_params.pop("job_id", "") or "").strip() or None
        if resume_job_id is None:
            try:
                session_id = self._validate_session_id(
                    stream_params.get("session_id") or "default"
                )
                store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
                waiting = [
                    job for job in store.list(
                        session_id,
                        protect_job_ids=self._live_assistant_job_ids(session_id),
                    )
                    if job.state is AssistantRunState.WAITING_USER
                ]
                if len(waiting) == 1:
                    resume_job_id = waiting[0].job_id
            except (OSError, ValueError):
                pass
        stream_params["message"] = ""
        stream_params["confirmation"] = action
        stream_params["confirmation_action"] = True
        stream_params["hide_user_message"] = True
        stream_params["_typed_action_rpc"] = True
        return self._start_assistant_stream(
            stream_params,
            resume_job_id=resume_job_id,
        )

    def _preempt_cron_stream_for_user(self, session_id: str) -> bool:
        """Cancel a cron-owned turn so interactive chat can take the session.

        Returns True when the session was held by cron and has been released.
        """
        has_cron = False
        for stream in list(getattr(self, "_assistant_streams", {}).values()):
            if stream.get("session_id") != session_id or stream.get("done"):
                continue
            if stream.get("cron_job_id"):
                has_cron = True
                break
        if not has_cron:
            return False
        try:
            self.cancel_assistant_run(session_id)
        except Exception as exc:
            debug_log(f"preempt_cron_cancel_failed session_id={session_id} error={exc}")
            return False
        import time

        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._assistant_run_lock:
                if session_id not in self._assistant_running_sessions:
                    return True
            time.sleep(0.05)
        with self._assistant_run_lock:
            self._assistant_running_sessions.discard(session_id)
        return True

    def _start_assistant_stream(
        self,
        params: dict[str, Any],
        *,
        resume_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Start assistant chat in a background thread and poll deltas from JS."""
        import threading
        import uuid

        try:
            session_id = self._validate_session_id(params.get("session_id") or "default")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        debug_log(f"start_assistant_stream session_id={session_id}")

        try:
            regenerate_user_before = float(params.get("regenerate_user_before") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid regenerate user timestamp"}
        if regenerate_user_before and not self._regenerate_user_target_exists(
            session_id,
            regenerate_user_before,
        ):
            return {"ok": False, "error": "regenerate target user message was not found"}

        cron_job_id = str(params.get("cron_job_id") or "").strip()
        with self._assistant_run_lock:
            already_running = session_id in self._assistant_running_sessions
        if already_running:
            # User messages preempt cron-owned turns on the same session so a
            # destination=session loop cannot permanently block interactive chat.
            if cron_job_id or not self._preempt_cron_stream_for_user(session_id):
                return {"ok": False, "error": "assistant session is already running"}
        with self._assistant_run_lock:
            if session_id in self._assistant_running_sessions:
                return {"ok": False, "error": "assistant session is already running"}
            self._assistant_running_sessions.add(session_id)

        # A stream id identifies one in-memory event buffer. A resumed durable
        # job deliberately gets a *new* stream id while retaining its job id.
        # This prevents stale pollers from reading a new epoch's events.
        run_id = uuid.uuid4().hex
        # Publish the stream shell before durable transitions so a concurrent
        # list/get cannot crash-recover the job we are about to mark running.
        protect_job_id = resume_job_id or run_id
        self._stream_hub().create(
            run_id,
            session_id,
            job=None,
            job_store=None,
            protect_job_id=protect_job_id,
            cron_job_id=cron_job_id or None,
        )
        job_store = None
        job = None
        try:
            job_store = AssistantRunJobStore(self.settings_mgr.get().sessions_dir)
            live_ids = self._live_assistant_job_ids(session_id)
            if resume_job_id:
                job = job_store.load(
                    session_id,
                    resume_job_id,
                    protect_job_ids=live_ids,
                )
                if job.state not in {AssistantRunState.PAUSED, AssistantRunState.WAITING_USER}:
                    raise ValueError(f"assistant job is not resumable ({job.state.value})")
                job_store.close_waiting_jobs(
                    session_id,
                    except_job_id=job.job_id,
                    protect_job_ids=live_ids,
                )
                job_store.transition(job, AssistantRunState.RUNNING, status="Assistant job resumed")
            else:
                job_store.close_waiting_jobs(session_id, protect_job_ids=live_ids)
                job = job_store.create(
                    session_id,
                    job_id=run_id,
                    status="Assistant stream queued",
                    budget={"max_wall_seconds": int(getattr(
                        self.settings_mgr.get(), "assistant_turn_deadline_seconds", 604800,
                    ) or 0)},
                )
                job_store.transition(job, AssistantRunState.RUNNING, status="Assistant stream running")
            stream = self._assistant_streams.get(run_id)
            if stream is not None:
                stream["job"] = job
                stream["job_store"] = job_store
                stream["protect_job_id"] = job.job_id if job is not None else protect_job_id
        except Exception as exc:
            if resume_job_id:
                self._assistant_streams.pop(run_id, None)
                with self._assistant_run_lock:
                    self._assistant_running_sessions.discard(session_id)
                return {"ok": False, "error": f"could not resume assistant job: {exc}"}
            # Durable job tracking must never make an otherwise usable chat
            # unavailable (for example when a user-selected data drive is
            # temporarily read-only). The session persistence path still owns
            # the conversation's source of truth.
            debug_log(f"assistant_job_tracking_unavailable run_id={run_id} error={exc}")
            stream = self._assistant_streams.get(run_id)
            if stream is not None:
                stream["job"] = job
                stream["job_store"] = job_store

        def checkpoint_job(*, throttled: bool = False, **changes: Any) -> None:
            if job_store is None or job is None or job.is_terminal:
                return
            try:
                if throttled:
                    job_store.checkpoint_throttled(job, **changes)
                else:
                    job_store.checkpoint(job, **changes)
            except Exception as exc:
                debug_log(f"assistant_job_checkpoint_failed run_id={run_id} error={exc}")

        def push(event: dict[str, Any]) -> None:
            # AI UI Protocol v2: each pushed event carries BOTH legacy fields
            # (type/text/block) and the typed envelope (kind/data/ts). New
            # consumers prefer kind; old ones keep working unchanged.
            self._stream_hub().publish(run_id, event)

        def on_delta(text: str) -> None:
            if text:
                push({
                    "type": "delta", "text": text,
                    "kind": "text_chunk", "data": {"text": text},
                })

        def on_status(text: str) -> None:
            if not text:
                return
            kind, data = _classify_status_to_v2(text)
            push({
                "type": "status", "text": text,
                "kind": kind, "data": data,
            })
            # Status callbacks can arrive many times per second. Keep the UI
            # live, but coalesce identical/rapid durable status mutations.
            checkpoint_job(throttled=True, status=text)

        def on_block(block: dict[str, Any]) -> None:
            if not block:
                return
            normalized = normalize_block(block)
            push({
                "type": "block", "block": normalized,
                "kind": "block_upsert", "data": {"block": normalized},
            })

        def on_segment(round_index: int) -> None:
            ri = int(round_index)
            if ri <= 0:
                return
            push({
                "type": "segment",
                "round": ri,
                "kind": "agent_round_start",
                "data": {"round": ri},
            })
            # Round starts are meaningful recovery boundaries and stay
            # synchronous. The store merges any pending status into this write.
            checkpoint_job(progress_current=ri)

        push({
            "type": "status", "text": "正在启动助手...",
            "kind": "stage", "data": {"label": "正在启动助手..."},
        })

        def worker() -> None:
            try:
                debug_log(f"assistant_worker_start run_id={run_id} session_id={session_id}")
                result = self._run_assistant_chat(
                    params,
                    on_delta=on_delta,
                    on_status=on_status,
                    on_block=on_block,
                    on_segment=on_segment,
                )
                debug_log(f"assistant_worker_done run_id={run_id} ok={result.get('ok')}")
                # Desktop turns never hit the channel reply path; push wait cards
                # so Feishu/Telegram users get the same clickable options.
                if isinstance(result, dict) and result.get("ok"):
                    push_wait = getattr(self, "_push_channel_wait_cards", None)
                    if callable(push_wait):
                        try:
                            push_wait(session_id, result)
                        except Exception as push_exc:
                            debug_log(
                                f"channel_wait_card_push_failed run_id={run_id} error={push_exc}"
                            )
                if job_store is not None and job is not None and not job.is_terminal:
                    try:
                        if result.get("ok"):
                            result_status = str(result.get("status") or "")
                            if result_status in {"awaiting_user_decision", "awaiting_confirmation"}:
                                job_store.transition(
                                    job,
                                    AssistantRunState.WAITING_USER,
                                    status=(
                                        "Assistant is waiting for tool confirmation"
                                        if result_status == "awaiting_confirmation"
                                        else "Assistant is waiting for user input"
                                    ),
                                )
                            else:
                                job_store.transition(job, AssistantRunState.COMPLETED, status="Assistant stream completed")
                        else:
                            job_store.transition(
                                job, AssistantRunState.FAILED,
                                status=str(result.get("error") or "Assistant stream failed"),
                            )
                    except Exception as job_exc:
                        debug_log(f"assistant_job_completion_checkpoint_failed run_id={run_id} error={job_exc}")
                try:
                    result_status = str(result.get("status") or "")
                    if result_status in {
                        "awaiting_confirmation",
                        "pending_confirmation",
                    }:
                        self._notify_attention(
                            f"assistant-confirm:{session_id}:{run_id}",
                            title="ArgusAI 等待工具确认",
                            message="主 Agent 请求执行一项需要你授权的操作。",
                            level="warning",
                        )
                    elif result_status in {
                        "awaiting_user_decision",
                        "pending_user_decision",
                    }:
                        self._notify_attention(
                            f"assistant-decision:{session_id}:{run_id}",
                            title="ArgusAI 等待你的决定",
                            message="主 Agent 已暂停，返回 ArgusAI 选择下一步。",
                            level="warning",
                        )
                    elif not result.get("ok") or result_status == "ai_error":
                        self._notify_attention(
                            f"assistant-error:{session_id}:{run_id}",
                            title="ArgusAI 任务执行失败",
                            message=str(result.get("error") or "主 Agent 运行失败，请返回查看详情。")[:220],
                            level="error",
                        )
                    elif not cron_job_id:
                        started_at = float(getattr(job, "created_at", 0) or 0)
                        if started_at and time.time() - started_at >= 30:
                            self._notify_attention(
                                f"assistant-complete:{session_id}:{run_id}",
                                title="ArgusAI 任务已完成",
                                message="长时间运行的主 Agent 任务已经完成。",
                                level="success",
                            )
                except Exception:
                    debug_log(f"assistant_attention_notify_failed run_id={run_id}")
                push({
                    "type": "done", "result": result,
                    "kind": "run_done",
                    "data": {
                        "status": "ok" if result.get("ok") else "error",
                        "job": job.to_dict() if job is not None else None,
                    },
                })
            except Exception as e:
                debug_log(f"assistant_worker_error run_id={run_id} error={e}")
                if job_store is not None and job is not None and not job.is_terminal:
                    try:
                        job_store.transition(job, AssistantRunState.FAILED, status=str(e))
                    except Exception as job_exc:
                        debug_log(f"assistant_job_failure_checkpoint_failed run_id={run_id} error={job_exc}")
                push({
                    "type": "error", "error": str(e),
                    "kind": "run_error", "data": {"message": str(e)},
                })
                try:
                    self._notify_attention(
                        f"assistant-error:{session_id}:{run_id}",
                        title="ArgusAI 任务执行失败",
                        message=str(e)[:220],
                        level="error",
                    )
                except Exception:
                    pass
            finally:
                self._stream_hub().finish(run_id)
                with self._assistant_run_lock:
                    self._assistant_running_sessions.discard(session_id)
                self._schedule_assistant_stream_gc(run_id)
                # A channel message may have interjected into this run and is
                # waiting for the session to come free.
                try:
                    self._drain_channel_interjections_async(session_id)
                except Exception as exc:
                    debug_log(f"channel_interjection_drain_failed session_id={session_id} error={exc}")

        threading.Thread(target=worker, daemon=True).start()
        return {
            "ok": True,
            "run_id": run_id,
            "job_id": job.job_id if job is not None else run_id,
            "job": job.to_dict() if job is not None else None,
        }


    def _schedule_assistant_stream_gc(self, run_id: str) -> None:
        """Retain a completed stream briefly, even when its tab detached."""
        import threading

        stream = self._assistant_streams.get(run_id)
        if not stream:
            return
        with stream["lock"]:
            if stream.get("_gc_scheduled"):
                return
            stream["_gc_scheduled"] = True

        def _gc() -> None:
            debug_log(f"assistant_stream_gc run_id={run_id}")
            self._stream_hub().remove(run_id)

        timer = threading.Timer(self._ASSISTANT_STREAM_GC_DELAY, _gc)
        timer.daemon = True
        timer.start()

    def get_assistant_stream_events(
        self, run_id: str, offset: int = 0
    ) -> dict[str, Any]:
        """Long-poll the in-memory event queue for a running assistant turn.

        Historically this used to ``pop()`` the stream from
        ``_assistant_streams`` the first time ``done`` was true. That
        was a serious bug: if the user refreshed the panel a few
        hundred milliseconds after the turn completed (or the frontend
        debounced its poll), the second poll would see
        ``stream not found`` and the entire tool-trace history that
        the UI hadn't yet persisted to the message bubble would vanish
        from screen, while the on-disk session was still being
        flushed by the worker's finally block.

        The new contract: ``done=True`` is reported once events are
        fully drained, but the event buffer is kept around for
        ``_ASSISTANT_STREAM_GC_DELAY`` seconds afterwards so a
        re-mounting UI can re-poll from offset 0 and rebuild the
        trace cards. A background ``threading.Timer`` does the actual
        cleanup; if the user re-runs the same session before the
        timer fires the new ``start_assistant_stream`` call mints a
        fresh ``run_id`` anyway, so the lingering old buffer is
        harmless.
        """
        read = self._stream_hub().read(run_id, offset)
        if not read.get("ok"):
            return {
                "ok": False, "error": "stream not found", "events": [],
                "next": offset, "done": True, "job": None,
            }
        stream = read["record"]
        events = read["events"]
        next_offset = read["next"]
        done = read["done"]
        already_gc_scheduled = read["gc_scheduled"]
        if events or done:
            debug_log(f"get_assistant_stream_events run_id={run_id} events={len(events)} done={done}")
        if done and not already_gc_scheduled:
            self._schedule_assistant_stream_gc(run_id)
        job = stream.get("job")
        return {
            "ok": True,
            "events": events,
            "next": next_offset,
            "done": done,
            "job": job.to_dict() if job is not None else None,
        }


