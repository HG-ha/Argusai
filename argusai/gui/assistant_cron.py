"""GUI API + scheduler wiring for assistant cron jobs."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any


class AssistantCronMixin:
    """Expose cron CRUD to the frontend and fire due jobs into chat streams."""

    def list_assistant_cron_jobs(self) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore
        from argusai.core.assistant.cron_triggers import (
            webhook_secret_configured,
            webhook_url,
        )

        store = CronStore(self.settings_mgr.get().data_root)
        settings = self.settings_mgr.get()
        host = str(getattr(settings, "assistant_cron_webhook_host", "127.0.0.1"))
        port = int(getattr(settings, "assistant_cron_webhook_port", 8765) or 8765)
        jobs: list[dict[str, Any]] = []
        for raw in store.list_jobs():
            job = dict(raw)
            jid = str(job.get("id") or "")
            job["pending_event_count"] = store.pending_event_count(jid)
            if str((job.get("schedule") or {}).get("kind") or "") == "webhook":
                job["webhook"] = {
                    "url": webhook_url(host, port, jid),
                    "secret_configured": webhook_secret_configured(store.root, jid),
                }
            jobs.append(job)
        return {"ok": True, "jobs": jobs}

    def list_assistant_cron_runs(self, job_id: str, limit: int = 20) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore

        jid = str(job_id or "").strip()
        try:
            runs = CronStore(self.settings_mgr.get().data_root).list_runs(jid, limit=int(limit or 20))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "job_id": jid, "runs": []}
        return {"ok": True, "job_id": jid, "runs": runs}

    def save_assistant_cron_job(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore
        from argusai.core.assistant.cron_triggers import (
            ensure_webhook_secret,
            webhook_url,
        )

        raw = dict(payload or {})
        approval = (
            dict(raw.get("approval") or {})
            if isinstance(raw.get("approval"), dict)
            else {}
        )
        if approval.get("high_risk_ack"):
            approval.update({
                "granted_at": time.time(),
                "grant_source": str(approval.get("grant_source") or "gui")[:32],
                "grant_session_id": str(raw.get("origin_session_id") or ""),
            })
            raw["approval"] = approval
        store = CronStore(self.settings_mgr.get().data_root)
        try:
            job = store.upsert_job(raw)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        out: dict[str, Any] = {"ok": True, "job": job}
        if str((job.get("schedule") or {}).get("kind") or "") == "webhook":
            secret, created = ensure_webhook_secret(store.root, str(job.get("id") or ""))
            settings = self.settings_mgr.get()
            host = str(
                getattr(settings, "assistant_cron_webhook_host", "127.0.0.1")
            )
            port = int(
                getattr(settings, "assistant_cron_webhook_port", 8765) or 8765
            )
            out["webhook"] = {
                "url": webhook_url(host, port, str(job.get("id") or "")),
                "signing_secret": secret if created else "",
                "secret_created": created,
            }
        elif str((job.get("schedule") or {}).get("kind") or "") == "file_change":
            from argusai.core.assistant.cron_triggers import FileChangePoller

            baseline = FileChangePoller(store).initialize_job(job)
            out["file_watch"] = baseline
            if not baseline.get("ok"):
                out["warning"] = baseline.get("error") or "file watch baseline failed"
        return out

    def set_assistant_cron_enabled(self, job_id: str, enabled: bool = True) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore

        try:
            job = CronStore(self.settings_mgr.get().data_root).set_enabled(str(job_id or ""), bool(enabled))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not job:
            return {"ok": False, "error": "job not found"}
        return {"ok": True, "job": job}

    def delete_assistant_cron_job(self, job_id: str) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore
        from argusai.core.assistant.cron_triggers import delete_webhook_secret

        store = CronStore(self.settings_mgr.get().data_root)
        try:
            ok = store.delete_job(str(job_id or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not ok:
            return {"ok": False, "error": "job not found"}
        try:
            delete_webhook_secret(store.root, str(job_id or ""))
        except Exception:
            pass
        return {"ok": True, "deleted": True}

    def rotate_assistant_cron_webhook_secret(self, job_id: str) -> dict[str, Any]:
        from argusai.core.assistant.cron import CronStore
        from argusai.core.assistant.cron_triggers import (
            rotate_webhook_secret,
            webhook_url,
        )

        store = CronStore(self.settings_mgr.get().data_root)
        try:
            job = store.get_job(str(job_id or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not job or str((job.get("schedule") or {}).get("kind") or "") != "webhook":
            return {"ok": False, "error": "webhook job not found"}
        secret = rotate_webhook_secret(store.root, str(job.get("id") or ""))
        settings = self.settings_mgr.get()
        host = str(getattr(settings, "assistant_cron_webhook_host", "127.0.0.1"))
        port = int(getattr(settings, "assistant_cron_webhook_port", 8765) or 8765)
        return {
            "ok": True,
            "job_id": job.get("id"),
            "url": webhook_url(host, port, str(job.get("id") or "")),
            "signing_secret": secret,
        }

    def grant_assistant_cron_high_risk(
        self,
        job_id: str,
        granted: bool = True,
        source: str = "gui",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Persist the explicit second acknowledgement for unattended tools."""
        from argusai.core.assistant.cron import CronStore

        store = CronStore(self.settings_mgr.get().data_root)
        try:
            job = store.get_job(str(job_id or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not job:
            return {"ok": False, "error": "job not found"}
        approval = dict(job.get("approval") or {})
        approval.update({
            "mode": "unattended_full",
            "high_risk_ack": bool(granted),
            "granted_at": time.time() if granted else None,
            "grant_source": str(source or "gui")[:32],
            "grant_session_id": str(session_id or "")[:128],
        })
        updated = store.update_job_fields(str(job.get("id") or ""), approval=approval)
        return {"ok": True, "job": updated, "approval": approval}

    def choose_assistant_cron_watch_path(
        self,
        initial_path: str = "",
        directory: bool = True,
    ) -> dict[str, Any]:
        try:
            import webview
            from pathlib import Path

            window = getattr(self, "_window", None) or (
                webview.windows[0] if webview.windows else None
            )
            if window is None:
                return {"ok": False, "error": "no active window"}
            initial = Path(str(initial_path or "")).expanduser()
            if initial.is_file():
                initial = initial.parent
            if not initial.is_dir():
                initial = Path.home()
            picked = window.create_file_dialog(
                webview.FOLDER_DIALOG if directory else webview.OPEN_DIALOG,
                directory=str(initial.resolve()),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            value = picked[0] if isinstance(picked, (list, tuple)) else picked
            return {"ok": True, "path": str(value)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_assistant_cron_scheduler(self) -> None:
        from argusai.core.assistant.cron import start_cron_scheduler

        settings = self.settings_mgr.get()
        start_cron_scheduler(
            data_root=self.settings_mgr.get().data_root,
            fire=self._fire_assistant_cron_job,
            is_busy=self._assistant_cron_is_busy,
            webhook_host=str(
                getattr(settings, "assistant_cron_webhook_host", "127.0.0.1")
            ),
            webhook_port=int(
                getattr(settings, "assistant_cron_webhook_port", 8765) or 8765
            ),
        )

    def _sync_cron_session_permissions(
        self,
        job: dict[str, Any],
        target_session_id: str,
        target_session: Any,
    ) -> dict[str, Any]:
        """Unblock an unattended tick within its persisted authorization.

        Ordinary confirmable tools are covered by the schedule. High-risk tools
        require the job-scoped second acknowledgement stored in ``approval``.
        """
        changed = False
        try:
            from argusai.core.assistant.tool_policy import apply_settings_tool_always_allow

            before = set(target_session.auto_approve_tools())
            apply_settings_tool_always_allow(target_session, self.settings_mgr.get())
            if set(target_session.auto_approve_tools()) != before:
                changed = True
        except Exception:
            pass

        before_tools = set(target_session.auto_approve_tools())
        before_perms = set(target_session.auto_approve_permissions())
        approval = (
            dict(job.get("approval") or {})
            if isinstance(job.get("approval"), dict)
            else {}
        )
        high_risk_ack = bool(approval.get("high_risk_ack", False))
        seeded = set(
            target_session.set_auto_approve_all(
                True,
                include_high_risk=high_risk_ack,
                include_reverse_dynamic=high_risk_ack,
            )
        )
        # Authorization-management tools can never authorize themselves from
        # inside an unattended tick, even after the job's broader grant.
        if "cron_grant_high_risk" in seeded:
            target_session.set_auto_approve_tool(
                "cron_grant_high_risk",
                False,
                force=True,
            )
            seeded.discard("cron_grant_high_risk")
        merged_tools = sorted(seeded | before_tools)
        merged_perms = sorted(before_perms)

        origin_id = str(job.get("origin_session_id") or "").strip()
        if origin_id and origin_id != target_session_id:
            origin = self._assistant_sessions.get(origin_id)
            if origin is None:
                origin = self._ensure_session(origin_id)
            merged_tools = sorted(set(merged_tools) | set(origin.auto_approve_tools()))
            merged_perms = sorted(
                set(merged_perms) | set(origin.auto_approve_permissions())
            )

        receipt = target_session.replace_auto_approve_state(merged_tools, merged_perms)
        changed = changed or bool(receipt.get("changed")) or set(merged_tools) != before_tools

        # Todo plans must never block a scheduled tick.
        try:
            target_session._current_turn_source = "cron"
            target_session._current_turn_source_ref = str(job.get("id") or "")
            if target_session._auto_approve_todo_plan_for_cron():
                changed = True
            target_session._user_decision_requested = False
            target_session._todos_plan_approved = True
        except Exception:
            pass

        if changed:
            self._persist_cron_permission_event(
                job,
                target_session_id,
                target_session,
                origin_session_id=origin_id if origin_id != target_session_id else "",
                kind=(
                    "permission_state_inherited"
                    if origin_id and origin_id != target_session_id
                    else "permission_state_seeded"
                ),
            )
        return {
            "changed": changed,
            "tool_count": len(merged_tools),
            "permission_count": len(merged_perms),
            "high_risk_ack": high_risk_ack,
        }

    def _persist_cron_permission_event(
        self,
        job: dict[str, Any],
        target_session_id: str,
        target_session: Any,
        *,
        origin_session_id: str,
        kind: str,
    ) -> None:
        now = time.time()
        target_session.events.append({
            "id": f"evt_{int(now * 1000)}_{len(target_session.events)}",
            "kind": kind,
            "source": "runtime",
            "render_policy": "audit",
            "context_policy": "exclude",
            "turn_id": "",
            "timestamp": now,
            "payload": {
                "origin_session_id": origin_session_id,
                "cron_job_id": str(job.get("id") or ""),
                "tool_count": len(target_session.auto_approve_tools()),
                "permission_count": len(target_session.auto_approve_permissions()),
            },
        })
        if len(target_session.events) > 2000:
            del target_session.events[:-2000]
        path = self._session_path(target_session_id)
        name = f"Cron · {str(job.get('name') or job.get('id') or 'job')[:40]}"
        self._save_session(path, target_session, name)

    @staticmethod
    def _cron_pending_high_risk(
        session: Any,
        job: dict[str, Any] | None = None,
    ) -> list[str]:
        if not isinstance(getattr(session, "_pending", None), dict):
            return []
        approval = (
            dict((job or {}).get("approval") or {})
            if isinstance((job or {}).get("approval"), dict)
            else {}
        )
        from argusai.core.assistant.streaming import _NO_AUTO_APPROVE_TOOLS

        projection = session.pending_confirmation_projection()
        names = {
            str(item.get("name") or "")
            for item in list(projection.get("pending_tools") or [])
        }
        if "cron_grant_high_risk" in names:
            return ["cron_grant_high_risk"]
        if bool(approval.get("high_risk_ack", False)):
            return []
        return sorted({
            name for name in names if name in _NO_AUTO_APPROVE_TOOLS
        })

    @staticmethod
    def _cron_inherited_approval_action(
        session: Any,
        job: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Allow a pending batch only when the job authorization covers it."""
        if not isinstance(getattr(session, "_pending", None), dict):
            return None
        if AssistantCronMixin._cron_pending_high_risk(session, job):
            return None
        projection = session.pending_confirmation_projection()
        pending_tools = list(projection.get("pending_tools") or [])
        if not pending_tools:
            return None
        decisions: dict[str, str] = {}
        for item in pending_tools:
            call_id = str(item.get("id") or "")
            if not call_id:
                return None
            decisions[call_id] = "allow"
        first = pending_tools[0]
        return {
            "type": "batch_confirmation_decision",
            "pending_id": str(first.get("pending_id") or ""),
            "version": int(first.get("version") or 0),
            "decisions": decisions,
            # Also persist session-level auto-allow so the next call in the
            # same tick does not re-prompt.
            "auto_allow_names": sorted({
                str(item.get("name") or "")
                for item in pending_tools
                if str(item.get("name") or "").strip()
            }),
        }

    def _assistant_cron_is_busy(
        self,
        session_id: str | None,
        job: dict[str, Any] | None = None,
    ) -> str:
        """Return a typed reason when the target session blocks a cron tick."""
        if not session_id:
            return ""
        sid = str(session_id)
        lock = getattr(self, "_assistant_run_lock", None)
        running = getattr(self, "_assistant_running_sessions", set())
        if lock is None:
            busy_running = sid in running
        else:
            with lock:
                busy_running = sid in running
        if busy_running:
            return "running_target"
        for stream in list(getattr(self, "_assistant_streams", {}).values()):
            if stream.get("done"):
                continue
            if stream.get("session_id") == sid:
                return "running_target"
        try:
            session = self._assistant_sessions.get(sid)
            if session is None:
                session = self._ensure_session(sid)
            if isinstance(job, dict):
                self._sync_cron_session_permissions(job, sid, session)
            if getattr(session, "_pending", None):
                if isinstance(job, dict) and self._cron_inherited_approval_action(session, job):
                    return ""
                # Still pending but no resume action could be built — do not
                # block the scheduler; the fire path will clear or fail fast.
                return ""
            if (
                getattr(session, "todos", None)
                and not bool(getattr(session, "_todos_plan_approved", True))
            ):
                session._current_turn_source = "cron"
                session._current_turn_source_ref = str(
                    (job or {}).get("id") if isinstance(job, dict) else ""
                )
                session._auto_approve_todo_plan_for_cron()
                session._todos_plan_approved = True
                session._user_decision_requested = False
                return ""
            if getattr(session, "_user_decision_requested", False):
                session._user_decision_requested = False
                return ""
        except Exception:
            pass
        return ""

    def _fire_assistant_cron_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Start a chat turn for the job and wait until the stream finishes.

        The scheduler records the run from this return value — do not append
        a second history entry here.
        """
        from argusai.core.assistant.cron import CronStore

        job_id = str(job.get("id") or "")
        destination = str(job.get("destination") or "new_session")
        session_id = str(job.get("session_id") or "").strip()
        if destination == "new_session":
            # A standalone scheduled run owns a distinct conversation. Run
            # history links can therefore reopen the exact transcript instead
            # of every row resolving to the job's latest cumulative chat.
            session_id = f"cron-{job_id[-8:] if job_id else 'job'}-{uuid.uuid4().hex[:6]}"
        elif destination == "dedicated_session" and not session_id:
            # Job-owned thread: created lazily on first fire, then persisted
            # back into the job and reused by every subsequent trigger.
            stable = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"argusai-cron:{job_id or 'job'}",
            ).hex[:12]
            session_id = f"cron-{stable}-thread"
        elif not session_id:
            session_id = "default"
        try:
            session = self._ensure_session(session_id)
            self._sync_cron_session_permissions(job, session_id, session)
            path = self._session_path(session_id)
            name = f"Cron · {str(job.get('name') or job_id)[:40]}"
            if session is not None:
                self._save_session(path, session, name)
        except Exception as exc:
            return {"ok": False, "status": "failed", "error": str(exc), "session_id": session_id}
        blocked_reason = self._assistant_cron_is_busy(session_id, job)
        if blocked_reason in {"pending_confirmation", "pending_user_decision"}:
            return {
                "ok": False,
                "status": (
                    "waiting_confirmation"
                    if blocked_reason == "pending_confirmation"
                    else "waiting_user"
                ),
                "error": (
                    "target session is waiting for tool confirmation"
                    if blocked_reason == "pending_confirmation"
                    else "target session is waiting for a user decision"
                ),
                "session_id": session_id,
            }

        prompt = str(job.get("prompt") or "").strip()
        pending_high_risk = self._cron_pending_high_risk(session, job)
        if pending_high_risk:
            session._pending = None
            error = (
                "cron high-risk authorization required: "
                + ", ".join(pending_high_risk)
            )
            self._save_session(self._session_path(session_id), session, "")
            return {
                "ok": False,
                "status": "blocked",
                "error": error,
                "summary": error,
                "session_id": session_id,
            }
        inherited_action = self._cron_inherited_approval_action(session, job)
        trigger_event = (
            dict(job.get("_trigger_event") or {})
            if isinstance(job.get("_trigger_event"), dict)
            else {}
        )
        try:
            if inherited_action:
                started = self.start_assistant_action_stream({
                    "session_id": session_id,
                    "action": inherited_action,
                    "cron_job_id": job_id,
                    "message_source": "cron",
                    "message_source_ref": job_id,
                })
            else:
                event_context = (
                    dict(job.get("context") or {})
                    if isinstance(job.get("context"), dict)
                    else {}
                )
                if trigger_event:
                    event_context["trigger_event"] = {
                        "event_id": str(trigger_event.get("event_id") or ""),
                        "source": str(trigger_event.get("source") or ""),
                        "created_at": trigger_event.get("created_at"),
                        "payload": (
                            dict(trigger_event.get("payload") or {})
                            if isinstance(trigger_event.get("payload"), dict)
                            else {}
                        ),
                    }
                started = self._start_assistant_stream({
                    "session_id": session_id,
                    "message": prompt,
                    "context": event_context,
                    "cron_job_id": job_id,
                    # Typed provenance — UI hides via source/ui_hidden, not content markers.
                    "hide_user_message": True,
                    "message_source": "cron",
                    "message_source_ref": job_id,
                })
        except Exception as exc:
            return {"ok": False, "status": "failed", "error": str(exc), "session_id": session_id}
        if not started.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "error": started.get("error") or "failed to start stream",
                "session_id": session_id,
            }

        run_id = str(started.get("run_id") or "")
        assistant_job_id = str(started.get("job_id") or run_id)
        self._notify_assistant_session_updated(
            session_id,
            reason="cron_stream_started",
            source_ref=job_id,
        )
        try:
            CronStore(self.settings_mgr.get().data_root).update_job_fields(
                job_id,
                session_id=session_id,
            )
        except Exception:
            pass

        deadline = time.time() + 3600
        final_status = "completed"
        summary = f"Finished cron job in session {session_id}"
        error = ""
        resume_budget = 20
        while time.time() < deadline:
            stream = getattr(self, "_assistant_streams", {}).get(run_id)
            if stream is None:
                break
            if stream.get("done"):
                events = list(stream.get("events") or [])
                turn_done = False
                for ev in reversed(events[-20:]):
                    if ev.get("type") == "error":
                        error = str(ev.get("error") or "")
                        low = error.lower()
                        if "cancel" in low:
                            final_status = "cancelled"
                            summary = "Cancelled (preempted by interactive chat or user stop)"
                        else:
                            final_status = "failed"
                            summary = error or summary
                        turn_done = True
                        break
                    if ev.get("type") == "done":
                        result = ev.get("result") or {}
                        if not result.get("ok", True):
                            final_status = "failed"
                            error = str(result.get("error") or "")
                            summary = error or summary
                            turn_done = True
                            break
                        result_status = str(result.get("status") or "")
                        reply = str(
                            result.get("text")
                            or result.get("reply")
                            or result.get("content")
                            or result.get("message")
                            or ""
                        ).strip()
                        if reply:
                            summary = reply[:2000]
                        # Mid-turn confirmation that is already covered by the
                        # seeded / inherited allow-list → resume without a UI.
                        if (
                            result_status == "awaiting_confirmation"
                            and resume_budget > 0
                        ):
                            try:
                                live = self._ensure_session(session_id)
                                self._sync_cron_session_permissions(job, session_id, live)
                                blocked_tools = self._cron_pending_high_risk(live, job)
                                inherited_action = self._cron_inherited_approval_action(
                                    live,
                                    job,
                                )
                            except Exception:
                                blocked_tools = []
                                inherited_action = None
                            if blocked_tools:
                                live._pending = None
                                self._save_session(
                                    self._session_path(session_id),
                                    live,
                                    "",
                                )
                                final_status = "blocked"
                                error = (
                                    "cron high-risk authorization required: "
                                    + ", ".join(blocked_tools)
                                )
                                summary = error
                                turn_done = True
                                break
                            if inherited_action:
                                resume_budget -= 1
                                resumed = self.start_assistant_action_stream({
                                    "session_id": session_id,
                                    "action": inherited_action,
                                    "cron_job_id": job_id,
                                    "message_source": "cron",
                                    "message_source_ref": job_id,
                                })
                                if resumed.get("ok"):
                                    run_id = str(resumed.get("run_id") or run_id)
                                    assistant_job_id = str(
                                        resumed.get("job_id") or assistant_job_id
                                    )
                                    break
                        if (
                            result_status == "awaiting_user_decision"
                            and resume_budget > 0
                        ):
                            try:
                                live = self._ensure_session(session_id)
                                live._user_decision_requested = False
                                live._todos_plan_approved = True
                                live._supersede_pending_user_decisions(keep_latest=False)
                            except Exception:
                                pass
                            resume_budget -= 1
                            resumed = self._start_assistant_stream({
                                "session_id": session_id,
                                "message": (
                                    "定时任务无人值守：请按最佳判断继续执行，"
                                    "不要再调用 stop_for_user_decision。"
                                ),
                                "cron_job_id": job_id,
                                "hide_user_message": True,
                                "message_source": "cron",
                                "message_source_ref": job_id,
                            })
                            if resumed.get("ok"):
                                run_id = str(resumed.get("run_id") or run_id)
                                assistant_job_id = str(
                                    resumed.get("job_id") or assistant_job_id
                                )
                                break
                        if result_status == "awaiting_confirmation":
                            # Should be unreachable after force-allow; treat as failure.
                            final_status = "failed"
                            error = (
                                "cron turn still blocked on tool confirmation "
                                "after auto-allow — please report this bug"
                            )
                            summary = error
                        elif result_status == "awaiting_user_decision":
                            final_status = "failed"
                            error = (
                                "cron turn still blocked on user decision "
                                "after auto-continue — please report this bug"
                            )
                            summary = error
                        elif result_status in {"cancelled", "cancelled_by_user"}:
                            final_status = "cancelled"
                        elif result_status and result_status not in {"ok", "completed"}:
                            final_status = result_status
                        turn_done = True
                        break
                if turn_done:
                    break
                continue
            # Stay busy for global cron mutex while the turn runs.
            time.sleep(1.0)
        else:
            final_status = "failed"
            error = "cron turn timed out after 1h"
            summary = error
            try:
                self.cancel_assistant_run(session_id)
            except Exception:
                pass

        # Refresh job fields (origin_session_id etc.) before report-back.
        try:
            fresh = CronStore(self.settings_mgr.get().data_root).get_job(job_id)
            if fresh:
                job = fresh
        except Exception:
            pass

        reported = False
        try:
            reported = self._report_cron_result_to_origin(
                job,
                status=final_status,
                summary=summary,
                fire_session_id=session_id,
            )
        except Exception:
            pass

        notify_sent = False
        try:
            notify_sent = self._maybe_notify_cron_result(job, final_status, summary)
        except Exception:
            pass

        return {
            "ok": final_status == "completed",
            "status": final_status,
            "session_id": session_id,
            "assistant_run_job_id": assistant_job_id,
            "summary": summary[:2000],
            "error": error[:1000],
            "notify_sent": notify_sent,
            "reported_to_origin": reported,
        }

    def _report_cron_result_to_origin(
        self,
        job: dict[str, Any],
        *,
        status: str,
        summary: str,
        fire_session_id: str,
    ) -> bool:
        """Deliver a typed cron result to the origin Agent, then refresh its UI."""
        notify = job.get("notify") if isinstance(job.get("notify"), dict) else {}
        if not bool(notify.get("report_to_origin", True)):
            return False
        origin = str(job.get("origin_session_id") or "").strip()
        if not origin or origin == str(fire_session_id or "").strip():
            return False
        if status == "completed" and bool(notify.get("on_change_only", False)):
            low = summary.lower()
            if "no change" in low or "无变化" in summary or "安静" in summary:
                return False
        if status == "failed" and not bool(notify.get("on_failure", True)):
            return False
        if status == "completed" and not bool(notify.get("on_success", True)):
            return False

        import json

        from argusai.core.assistant.messages import ChatMessage

        session = self._ensure_session(origin)
        name = f"Cron · {str(job.get('name') or job.get('id') or 'job')[:40]}"
        path = self._session_path(origin)
        if path.exists():
            try:
                import json

                data = json.loads(path.read_text(encoding="utf-8"))
                name = str(data.get("name") or name)
            except Exception:
                pass
        job_id = str(job.get("id") or "")
        job_name = str(job.get("name") or job_id or "job")[:60]
        timestamp = time.time()
        summary_truncated = len(str(summary or "")) > 1800
        event = {
            "id": f"evt_{int(timestamp * 1000)}_{len(session.events)}",
            "kind": "cron_result",
            "source": "cron",
            "render_policy": "timeline",
            "context_policy": "exclude",
            "turn_id": "",
            "timestamp": timestamp,
            "payload": {
                "job_id": job_id,
                "job_name": job_name,
                "status": status,
                "run_session_id": fire_session_id,
                "summary": summary[:1800],
                "summary_truncated": summary_truncated,
                "detail_ref": {
                    "kind": "assistant_session",
                    "session_id": fire_session_id,
                },
                "delivery": "pending",
            },
        }
        session.events.append(event)
        if len(session.events) > 2000:
            del session.events[:-2000]
        self._save_session(path, session, name)

        runtime_payload = json.dumps({
            "event_type": "cron_result",
            "job_id": job_id,
            "job_name": job_name,
            "status": status,
            "run_session_id": fire_session_id,
            "summary": summary[:1800],
            "summary_truncated": summary_truncated,
            "detail_ref": {
                "kind": "assistant_session",
                "session_id": fire_session_id,
            },
        }, ensure_ascii=False)
        processed = False
        busy_reason = self._assistant_cron_is_busy(origin)
        runner = getattr(self, "_run_assistant_chat", None)
        reserved_origin = False
        if not busy_reason and callable(runner):
            with self._assistant_run_lock:
                if origin in self._assistant_running_sessions:
                    busy_reason = "running_target"
                else:
                    self._assistant_running_sessions.add(origin)
                    reserved_origin = True
        if not busy_reason and callable(runner):
            try:
                result = runner({
                    "session_id": origin,
                    "message": runtime_payload,
                    "page": "assistant",
                    "hide_user_message": True,
                    "message_source": "cron_result",
                    "message_source_ref": job_id,
                    # Runtime callbacks only need a concise user-facing
                    # summary. Disabling extended thinking also avoids
                    # provider replay requirements from unrelated old turns.
                    "thinking_enabled": False,
                })
                processed = bool(
                    result.get("ok")
                    and str(result.get("status") or "") in {
                        "done",
                        "completed",
                        "ok",
                    }
                )
            except Exception:
                processed = False
            finally:
                if reserved_origin:
                    with self._assistant_run_lock:
                        self._assistant_running_sessions.discard(origin)

        if not processed:
            body = (
                f"Cron · {job_name}\n"
                f"Status: {status}\n"
                f"Run session: {fire_session_id}\n\n"
                f"{summary[:1800]}"
            )
            session.history.append(ChatMessage(
                role="assistant",
                content=body,
                source="cron_result",
                source_ref=job_id,
                timestamp=time.time(),
            ))
        event["payload"]["delivery"] = (
            "agent_processed"
            if processed
            else ("deferred_busy" if busy_reason else "projected_fallback")
        )
        self._save_session(path, session, name)
        self._notify_assistant_session_updated(
            origin,
            reason="cron_result",
            source_ref=job_id,
        )
        return True

    def notify_session_updated(
        self,
        session_id: str,
        *,
        reason: str,
        source_ref: str = "",
    ) -> None:
        """HostCallbacks alias for :meth:`_notify_assistant_session_updated`."""
        self._notify_assistant_session_updated(
            session_id,
            reason=reason,
            source_ref=source_ref,
        )

    def _notify_assistant_session_updated(
        self,
        session_id: str,
        *,
        reason: str,
        source_ref: str = "",
    ) -> None:
        """Push an in-process session refresh signal into the mounted web UI."""
        from argusai.core.host import host_supports_webview, log_session_updated

        kind = getattr(self, "host_kind", "gui")
        if not host_supports_webview(kind):
            log_session_updated(
                kind,
                session_id,
                reason=reason,
                source_ref=source_ref,
            )
            return
        window = getattr(self, "_window", None)
        if window is None:
            log_session_updated(
                kind,
                session_id,
                reason=reason,
                source_ref=source_ref,
            )
            return
        try:
            import json

            detail = json.dumps({
                "sessionId": str(session_id or ""),
                "reason": str(reason or ""),
                "sourceRef": str(source_ref or ""),
            }, ensure_ascii=False)
            window.evaluate_js(
                "window.dispatchEvent(new CustomEvent("
                "'argusai:assistant-session-updated',"
                f"{{detail:{detail}}}"
                "));"
            )
        except Exception:
            pass

    def _maybe_notify_cron_result(
        self,
        job: dict[str, Any],
        status: str,
        summary: str,
    ) -> bool:
        notify = job.get("notify") if isinstance(job.get("notify"), dict) else {}
        on_success = bool(notify.get("on_success", True))
        on_failure = bool(notify.get("on_failure", True))
        on_change_only = bool(notify.get("on_change_only", False))
        if status == "failed" and not on_failure:
            return False
        if status == "completed" and not on_success:
            return False
        if status == "completed" and on_change_only:
            # Defense/patrol convention: skip notify when summary says quiet.
            low = summary.lower()
            if "no change" in low or "无变化" in summary or "安静" in summary:
                return False
        title = f"Cron · {str(job.get('name') or job.get('id') or 'job')[:48]}"
        body = f"{status}: {summary}"[:280]
        try:
            return self._notify_attention(
                f"cron:{job.get('id')}:{status}:{int(time.time())}",
                title=title,
                message=body,
                level="error" if status == "failed" else "info",
                cooldown_seconds=15,
            )
        except Exception:
            return False
