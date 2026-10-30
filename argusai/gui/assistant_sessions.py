"""Assistant GUI mixin slice split from assistant.py."""

from __future__ import annotations

from .assistant_common import *

_AGENT_COMPLETION_WAKE_PROMPT = (
    "[系统事件 · 子 Agent 已结束（这不是用户发来的消息）]\n"
    "本会话委派的子 Agent 已全部结束。请阅读随后的结束通知，用一段简洁的中文向用户汇报："
    "每个子 Agent 得出了什么、目标是否达成、以及你建议的下一步。"
    "需要核实时可调用 `get_agent` / `list_agents`；"
    "不要声称这是用户发来的消息，也不要在未获用户确认的情况下执行有副作用的操作。"
)


class AssistantSessionsMixin:
    @property
    def _sessions_dir(self) -> Path:
        p = Path(self.settings_mgr.get().sessions_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


    def _validate_session_id(self, session_id: str) -> str:
        sid = (session_id or "default").strip()
        if (
            not sid
            or len(sid) > 128
            or sid in {".", ".."}
            or any(ch not in _SESSION_ID_ALLOWED for ch in sid)
        ):
            raise ValueError("invalid session_id")
        return sid


    def _session_path(self, session_id: str) -> Path:
        safe_id = self._validate_session_id(session_id)
        base = self._sessions_dir.resolve()
        path = (base / f"{safe_id}.json").resolve()
        # Belt-and-braces containment check; ``_validate_session_id`` already
        # rejects path traversal but resolve() is the final word.
        if path.parent != base:
            raise ValueError("invalid session_id")
        return path


    def _assistant_runtime_binding(
        self,
        session_id: str,
        requested: dict[str, Any] | None = None,
    ):
        """Resolve stable runtime metadata for a new or existing session."""
        from argusai.core.agent_runtimes import (
            NATIVE_RUNTIME_ID,
            normalize_session_runtime_binding,
            preserve_session_project_dir,
            runtime_configs,
        )

        safe_id = self._validate_session_id(session_id)
        live = self._assistant_sessions.get(safe_id)
        if live is not None:
            current_binding = normalize_session_runtime_binding(
                getattr(live, "runtime_binding", None).to_dict()
                if getattr(live, "runtime_binding", None) is not None
                else None
            )
            requested_binding = normalize_session_runtime_binding(requested)
            binding = (
                current_binding
                if current_binding.locked or getattr(live, "history", [])
                else preserve_session_project_dir(current_binding, requested_binding)
            )
        else:
            data: dict[str, Any] = {}
            path = self._session_path(safe_id)
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            binding = normalize_session_runtime_binding(data.get("runtime") or requested)
        known = {
            item.id: item
            for item in runtime_configs(self.settings_mgr.get().agent_runtimes)
        }
        runtime = known.get(binding.runtime_id)
        if runtime is None or not runtime.enabled:
            if binding.locked:
                raise ValueError(f"bound assistant runtime is unavailable: {binding.runtime_id}")
            binding = normalize_session_runtime_binding({"runtime_id": NATIVE_RUNTIME_ID})
        if binding.workspace_mode == "project":
            project = Path(binding.project_dir).expanduser()
            if not project.is_absolute() or not project.is_dir():
                raise ValueError("agent project directory must be an existing absolute directory")
        return binding


    def _save_session(self, path: Path, session: AssistantSession, name: str) -> None:
        """Persist one conversation. An empty ``name`` means "keep the old one".

        Callers that only touch runtime state (a model switch, a live-mode
        toggle) have no name to supply, and writing "" would erase the title the
        user gave the conversation.
        """
        old_note = ""
        old_name = ""
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                old_note = str(old.get("note") or "")[:4000]
                old_name = str(old.get("name") or "")
            except Exception:
                old_note = ""
                old_name = ""
        data = session.to_dict()
        data["name"] = str(name or "") or old_name
        if old_note:
            data["note"] = old_note
        data["updated_at"] = time.time()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


    def _looks_like_hidden_confirmation(self, message: dict[str, Any]) -> bool:
        """Hide tool-confirm replies via typed attributes only."""
        if message.get("role") != "user":
            return False
        if message.get("ui_hidden") or message.get("hidden"):
            return True
        return str(message.get("source") or "").strip().lower() == "confirmation"

    def _looks_like_cron_trigger(self, message: dict[str, Any]) -> bool:
        """Scheduler-injected user prompts — typed via ``source`` / ``ui_hidden``."""
        if message.get("role") != "user":
            return False
        if message.get("ui_hidden") or message.get("hidden"):
            return True
        if str(message.get("source") or "").strip().lower() == "cron":
            return True
        # Legacy rows before source=cron existed.
        content = str(message.get("content") or "").strip()
        return content.lower().startswith("[cron:")

    def _apply_session_auto_approve_policy(self, session: Any, settings: Any = None) -> None:
        """Seed a new/legacy conversation once; keep its persisted preference."""
        try:
            from argusai.core.assistant.tool_policy import apply_settings_tool_always_allow

            settings = settings if settings is not None else self.settings_mgr.get()
            apply_settings_tool_always_allow(session, settings)
            if not getattr(session, "_auto_approve_policy_initialized", False):
                if bool(getattr(settings, "assistant_auto_approve", False)):
                    session.apply_user_auto_approve_mode(True)
                else:
                    # Legacy snapshots may have per-tool grants but no mode
                    # field. Seeding a false default must not revoke them.
                    session._auto_approve_policy_initialized = True
        except Exception:
            pass

    def _ensure_session(self, session_id: str) -> AssistantSession:
        """Return the in-memory session for ``session_id``, creating it if missing.

        Used by the auto-allow toggles: they must not depend on whether a
        chat has been started yet, so we lazily build a session (with a
        provider if AI is configured) and stash it.
        """
        session = self._assistant_sessions.get(session_id)
        if session:
            return session
        allow_web_search = self._default_assistant_web_search()
        registry = build_tool_registry(self, session_id, allow_web_search=allow_web_search)
        provider = None
        try:
            app_settings = self.settings_mgr.get()
            if app_settings.ai_enabled and app_settings.ai_endpoints:
                from argusai.core.ai import create_provider
                provider = create_provider(endpoints=app_settings.ai_endpoints)
        except Exception:
            pass
        session_path = self._session_path(session_id)
        if session_path.exists():
            try:
                data = json.loads(session_path.read_text(encoding="utf-8"))
                session = AssistantSession.from_dict(data, registry, provider, kb=self.kb)
            except Exception:
                # A malformed legacy snapshot must not prevent the engagement
                # / auto-allow controls from being used, but never overwrite a
                # readable session with an empty one just because it exists.
                session = AssistantSession(registry=registry, ai_provider=provider, kb=self.kb)
                session.allow_web_search = allow_web_search
        else:
            session = AssistantSession(registry=registry, ai_provider=provider, kb=self.kb)
            session.allow_web_search = allow_web_search
        self._apply_session_auto_approve_policy(session)
        self._assistant_sessions[session_id] = session
        try:
            from argusai.core.tools.sandbox_code import _ensure_workspace_codebase_scope

            _ensure_workspace_codebase_scope(
                self,
                {"_session_id": session_id, "_session": session},
            )
        except Exception:
            pass
        return session

    def list_assistant_agents(self, session_id: str, include_closed: bool = False) -> dict[str, Any]:
        """List durable child-Agent threads owned by one assistant session."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).list(
                include_closed=bool(include_closed),
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "agents": []}

    def get_assistant_agent(self, session_id: str, agent_id: str) -> dict[str, Any]:
        """Read one child-Agent lifecycle and visible transcript."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).get(
                str(agent_id or ""),
                include_detail=True,
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def confirm_assistant_agent_action(
        self,
        session_id: str,
        agent_id: str,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a child-Agent tool request through the parent session UI."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).confirm(
                str(agent_id or ""),
                confirmation if isinstance(confirmation, dict) else {},
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def close_assistant_agent(self, session_id: str, agent_id: str) -> dict[str, Any]:
        """Cancel and permanently close one child-Agent thread."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).close(str(agent_id or ""))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def delete_assistant_agent(self, session_id: str, agent_id: str) -> dict[str, Any]:
        """Permanently delete one non-running child-Agent thread."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).remove(str(agent_id or ""))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def pause_assistant_agent(self, session_id: str, agent_id: str) -> dict[str, Any]:
        """Pause one running child Agent at its next safe boundary."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).pause(str(agent_id or ""))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def resume_assistant_agent(self, session_id: str, agent_id: str, message: str = "") -> dict[str, Any]:
        """Resume one paused child Agent, optionally with a steering message."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).send_input(
                str(agent_id or ""),
                str(message or "继续"),
                resume=True,
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def send_assistant_agent_input(self, session_id: str, agent_id: str, message: str = "") -> dict[str, Any]:
        """Interject: queue a steering message for a running child Agent."""
        try:
            safe_id = self._validate_session_id(session_id)
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(self._ensure_session(safe_id)).send_input(
                str(agent_id or ""),
                str(message or ""),
            )
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def _agent_wake_block_reason(self, session_id: str) -> str:
        """Typed reason a finished-child wake must not start a parent turn.

        Every branch here is a hard guard, not a heuristic: an unattended turn
        may only start on a genuinely idle session that is not waiting on the
        user for anything. Deliberately reads the live session instead of
        ``_ensure_session`` — a closed session must stay closed.
        """
        session = self._assistant_sessions.get(session_id)
        if session is None:
            return "session_closed"
        try:
            settings = self.settings_mgr.get()
            from argusai.core.agent_runtimes import NATIVE_RUNTIME_ID, runtime_configs

            runtime_id = str(
                getattr(getattr(session, "runtime_binding", None), "runtime_id", "")
                or NATIVE_RUNTIME_ID
            )
            if runtime_id == NATIVE_RUNTIME_ID:
                if not (
                    getattr(settings, "ai_enabled", False)
                    and getattr(settings, "ai_endpoints", None)
                ):
                    return "ai_disabled"
            elif not any(
                item.id == runtime_id and item.enabled and item.backend == "acp"
                for item in runtime_configs(settings.agent_runtimes)
            ):
                return "runtime_unavailable"
        except Exception:
            return "runtime_unavailable"
        lock = getattr(self, "_assistant_run_lock", None)
        running = getattr(self, "_assistant_running_sessions", set())
        if lock is None:
            busy = session_id in running
        else:
            with lock:
                busy = session_id in running
        if busy:
            return "running_target"
        for stream in list(getattr(self, "_assistant_streams", {}).values()):
            if not stream.get("done") and stream.get("session_id") == session_id:
                return "running_target"
        if getattr(session, "_pending", None):
            return "pending_confirmation"
        if bool(getattr(session, "_user_decision_requested", False)):
            return "pending_user_decision"
        if getattr(session, "todos", None) and not bool(getattr(session, "_todos_plan_approved", True)):
            return "pending_user_decision"
        if bool(getattr(session, "_cancel_requested", False)):
            return "cancelled_by_user"
        if bool(getattr(session, "_pause_after_current_response", False)):
            return "paused_by_user"
        return ""

    def _wake_assistant_after_agent_completion(
        self,
        session_id: str,
        *,
        agent_ids: Any = (),
    ) -> dict[str, Any]:
        """Run one parent turn so the Agent can report finished child Agents.

        Called from ``AgentThreadManager``'s debounce timer thread, which is the
        hop off the child executor thread. ``_start_assistant_stream`` owns its
        own worker thread plus the per-session run lock, so this returns
        immediately and a second wake for the same batch is rejected as
        ``running_target`` rather than starting a duplicate turn.

        The woken turn is an ordinary turn: same tool-confirmation gating, no
        inherited child approvals, no automatic todo-plan approval.
        """
        ids = [str(item or "") for item in (agent_ids or ()) if str(item or "")]
        try:
            sid = self._validate_session_id(session_id)
        except ValueError:
            return {"ok": False, "error": "invalid session_id"}
        # A standing-team lead session is a container, not a chat anyone reads —
        # its members surface in the Team panel. Never spend an AI turn making
        # the lead "report" finished members.
        if sid.startswith("team-"):
            return {"ok": False, "skipped": True, "reason": "team_lead_no_operator"}
        reason = self._agent_wake_block_reason(sid)
        if reason:
            debug_log(f"agent_completion_wake_skipped session_id={sid} reason={reason}")
            return {"ok": False, "skipped": True, "reason": reason}
        started = self._start_assistant_stream({
            "session_id": sid,
            "message": _AGENT_COMPLETION_WAKE_PROMPT,
            "page": "assistant",
            "hide_user_message": True,
            "message_source": "agent_completion",
            "message_source_ref": ids[0] if ids else "",
            "thinking_enabled": False,
        })
        if not started.get("ok"):
            debug_log(
                f"agent_completion_wake_failed session_id={sid} error={started.get('error')}"
            )
            return started
        debug_log(f"agent_completion_wake_started session_id={sid} agents={len(ids)}")
        try:
            self._notify_assistant_session_updated(
                sid,
                reason="agent_completion",
                source_ref=ids[0] if ids else "",
            )
        except Exception:
            pass
        return started


    def _default_assistant_web_search(self) -> bool:
        try:
            return bool(getattr(self.settings_mgr.get(), "assistant_enable_web_search", True))
        except Exception:
            return True


    def _refresh_session_registry(self, session_id: str, session: AssistantSession) -> None:
        """Rebuild registry when per-session tool availability changes."""
        approved = set(session.auto_approve_tools())
        session.registry = build_tool_registry(self, session_id, allow_web_search=session.allow_web_search)
        session._register_session_tools()
        session._catalog = session.registry.catalog()
        session._auto_approve_tools = {name for name in approved if name in session.registry._tools}


    def list_assistant_sessions(self) -> list[dict[str, Any]]:
        debug_log("list_assistant_sessions")
        sessions: list[dict[str, Any]] = []
        file_entries: list[tuple[Path, Any]] = []
        for path in self._sessions_dir.glob("*.json"):
            try:
                file_entries.append((path, path.stat()))
            except OSError:
                continue
        file_entries.sort(key=lambda item: -item[1].st_mtime)

        row_cache = getattr(self, "_assistant_session_list_row_cache", None)
        if not isinstance(row_cache, dict):
            row_cache = {}
            self._assistant_session_list_row_cache = row_cache
        seen_ids: set[str] = set()

        for f, stat in file_entries:
            session_id = f.stem
            seen_ids.add(session_id)
            # Cron-owned per-run and dedicated conversations are navigated from
            # Scheduled tasks -> Run history. Keep them out of every ordinary
            # assistant/session picker while preserving direct get/open by id.
            # A job bound to an existing user conversation keeps that original
            # non-cron id and therefore remains visible.
            if session_id.startswith("cron-") or session_id.startswith("team-"):
                row_cache.pop(session_id, None)
                continue
            file_key = (int(stat.st_mtime_ns), int(stat.st_size))
            cached = row_cache.get(session_id)
            base_row: dict[str, Any] | None
            if isinstance(cached, dict) and cached.get("file_key") == file_key:
                cached_row = cached.get("row")
                base_row = dict(cached_row) if isinstance(cached_row, dict) else None
            else:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    from argusai.core.agent_runtimes import normalize_session_runtime_binding

                    binding = normalize_session_runtime_binding(data.get("runtime"))
                    visible_history = [
                        m for m in data.get("history", [])
                        if m.get("role") in ("user", "assistant")
                        and not m.get("hidden")
                        and not m.get("ui_hidden")
                        and not self._looks_like_hidden_confirmation(m)
                        and not self._looks_like_cron_trigger(m)
                    ]
                    latest_origin = next(
                        (
                            str(m.get("source") or "")
                            for m in reversed(data.get("history", []))
                            if m.get("role") == "user" and str(m.get("source") or "")
                        ),
                        "",
                    )
                    origin = "channel" if latest_origin == "channel" else "gui"
                    session_type = (
                        "channel"
                        if origin == "channel"
                        else (
                            "local_agent"
                            if binding.runtime_id not in {
                                "",
                                "native",
                                "argusai-native",
                            }
                            else "native"
                        )
                    )
                    base_row = None
                    if visible_history:
                        count = len(visible_history)
                        base_row = {
                            "id": session_id,
                            "name": self._assistant_session_display_name(data, session_id),
                            "note": str(data.get("note") or ""),
                            "msg_count": count,
                            # Channel gateway /sessions reads message_count.
                            "message_count": count,
                            "updated_at": data.get("updated_at", 0),
                            "runtime_id": binding.runtime_id,
                            "workspace_mode": binding.workspace_mode,
                            "project_dir": binding.project_dir,
                            "origin": origin,
                            "session_type": session_type,
                        }
                    row_cache[session_id] = {"file_key": file_key, "row": base_row}
                except Exception:
                    row_cache.pop(session_id, None)
                    continue
            if base_row is None:
                continue
            if base_row.get("origin") not in {"gui", "channel"}:
                base_row["origin"] = "gui"
            if base_row.get("session_type") not in {
                "native",
                "local_agent",
                "channel",
            }:
                base_row["session_type"] = (
                    "channel"
                    if base_row.get("origin") == "channel"
                    else (
                        "local_agent"
                        if str(base_row.get("runtime_id") or "") not in {
                            "",
                            "native",
                            "argusai-native",
                        }
                        else "native"
                    )
                )

            runtime = self._assistant_sessions.get(session_id)
            pending_count = 0
            if runtime is not None:
                try:
                    projection = runtime.pending_confirmation_projection()
                    pending_count = len(projection.get("pending_tools") or [])
                    if not pending_count and projection.get("pending_tool"):
                        pending_count = 1
                except Exception:
                    pending_count = 0
            is_running = False
            try:
                with self._assistant_run_lock:
                    is_running = session_id in self._assistant_running_sessions
            except Exception:
                pass
            if not is_running:
                try:
                    is_running = any(
                        stream.get("session_id") == session_id and not stream.get("done")
                        for stream in self._assistant_streams.values()
                    )
                except Exception:
                    pass
            sessions.append({
                **base_row,
                "is_running": bool(is_running),
                "pending_count": pending_count,
                "has_sandbox": (self._sessions_dir / f"{session_id}.sandbox").is_dir(),
            })

        for cached_id in set(row_cache) - seen_ids:
            try:
                row_cache.pop(cached_id, None)
            except Exception:
                pass
        return sessions


    def _drop_session_runtime(self, session_id: str) -> None:
        """Cancel and forget an in-memory assistant session."""
        acp_turn = getattr(self, "_active_acp_turns", {}).pop(session_id, None)
        if acp_turn is not None:
            try:
                acp_turn.request_cancel()
            except Exception:
                pass
        session = self._assistant_sessions.pop(session_id, None)
        if session is not None:
            try:
                manager = getattr(session, "_agent_thread_manager", None)
                if manager is not None:
                    manager.shutdown()
                session.cancel()
            except Exception:
                pass
        try:
            with self._assistant_run_lock:
                self._assistant_running_sessions.discard(session_id)
        except Exception:
            pass
        try:
            self._assistant_streams.pop(session_id, None)
        except Exception:
            pass


    def _remove_session_storage(self, session_id: str) -> dict[str, bool]:
        from argusai.core.workspace.session_sandbox import remove_session_storage

        return remove_session_storage(self._sessions_dir, session_id)


    def _collect_session_ids_on_disk(self) -> set[str]:
        ids: set[str] = set()
        base = self._sessions_dir.resolve()
        for path in base.glob("*.json"):
            try:
                ids.add(self._validate_session_id(path.stem))
            except ValueError:
                continue
        for path in base.glob("*.sandbox"):
            if not path.is_dir():
                continue
            stem = path.name.removesuffix(".sandbox") if path.name.endswith(".sandbox") else path.stem
            try:
                ids.add(self._validate_session_id(stem))
            except ValueError:
                shutil.rmtree(path, ignore_errors=True)
        return ids


    def delete_assistant_session(self, session_id: str) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        self._drop_session_runtime(session_id)
        unbound_identities = 0
        try:
            unbound_identities = self._channels_store().unbind_session(session_id)
        except Exception:
            pass
        removed = self._remove_session_storage(session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "removed": removed,
            "unbound_channel_identities": unbound_identities,
        }


    def clear_all_assistant_sessions(self) -> dict[str, Any]:
        """Delete every persisted assistant session JSON and sandbox directory."""
        ids = self._collect_session_ids_on_disk()
        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for sid in sorted(ids):
            try:
                self._drop_session_runtime(sid)
                unbound_identities = 0
                try:
                    unbound_identities = self._channels_store().unbind_session(sid)
                except Exception:
                    pass
                removed = self._remove_session_storage(sid)
                deleted.append({
                    "session_id": sid,
                    "unbound_channel_identities": unbound_identities,
                    **removed,
                })
            except Exception as e:
                errors.append({"session_id": sid, "error": str(e)})
        return {
            "ok": not errors,
            "deleted_count": len(deleted),
            "deleted": deleted,
            "errors": errors,
        }


    def rename_assistant_session(self, session_id: str, name: str) -> dict[str, Any]:
        return self.update_assistant_session(session_id, {"name": name})


    def update_assistant_session(self, session_id: str, patch: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
            p = self._session_path(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not p.exists():
            return {"ok": False, "error": "session not found"}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            patch = patch or {}
            if "name" in patch:
                name = str(patch.get("name") or "").strip()
                data["name"] = name[:120]
            if "note" in patch:
                note = str(patch.get("note") or "").strip()
                data["note"] = note[:4000]
            if "context_notes" in patch:
                from argusai.core.assistant.session import MAX_CONTEXT_NOTES_CHARS

                context_notes = str(patch.get("context_notes") or "").strip()
                data["context_notes"] = context_notes[:MAX_CONTEXT_NOTES_CHARS]
            data["updated_at"] = time.time()
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if "context_notes" in patch:
                live = self._assistant_sessions.get(session_id)
                if live is not None:
                    live.set_context_notes(data["context_notes"])
            return {
                "ok": True,
                "session": {
                    "id": session_id,
                    "name": self._assistant_session_display_name(data, session_id),
                    "note": str(data.get("note") or ""),
                    "context_notes": str(data.get("context_notes") or ""),
                    "updated_at": data.get("updated_at", 0),
                },
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def import_assistant_context(self, session_id: str) -> dict[str, Any]:
        """Import persistent context from a user-selected text, Markdown, or JSON file."""
        try:
            session_id = self._validate_session_id(session_id)
            if not self._session_path(session_id).exists():
                return {"ok": False, "error": "session not found"}
            picker = getattr(self, "_pick_open_file", None)
            if not callable(picker):
                return {"ok": False, "error": "file picker is unavailable"}
            picked = picker(
                file_types=(
                    "Context files (*.md;*.markdown;*.txt;*.json)",
                    "All files (*.*)",
                ),
            )
            if not picked.get("ok"):
                return picked
            source = Path(str(picked.get("path") or "")).resolve()
            if not source.is_file():
                return {"ok": False, "error": f"not a file: {source}"}
            # A context note is capped at 12k characters. Leave room for JSON
            # metadata while refusing unexpectedly large files selected by mistake.
            if source.stat().st_size > 256_000:
                return {"ok": False, "error": "context file is too large (maximum 256 KB)"}
            raw = source.read_text(encoding="utf-8-sig")
            notes: Any = raw
            if source.suffix.lower() == ".json":
                payload = json.loads(raw)
                if isinstance(payload, str):
                    notes = payload
                elif isinstance(payload, dict):
                    notes = next(
                        (
                            payload[key]
                            for key in ("context_notes", "context", "notes")
                            if key in payload
                        ),
                        None,
                    )
                    if notes is None:
                        return {"ok": False, "error": "JSON context has no context_notes field"}
                else:
                    return {"ok": False, "error": "JSON context must be an object or string"}
            updated = self.update_assistant_session(session_id, {"context_notes": notes})
            if not updated.get("ok"):
                return updated
            return {
                **updated,
                "context_notes": str(updated.get("session", {}).get("context_notes") or ""),
                "source": str(source),
            }
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}


    def export_assistant_context(self, session_id: str) -> dict[str, Any]:
        """Export persistent context to a user-selected Markdown file."""
        try:
            session_id = self._validate_session_id(session_id)
            path = self._session_path(session_id)
            if not path.exists():
                return {"ok": False, "error": "session not found"}
            data = json.loads(path.read_text(encoding="utf-8"))
            live = self._assistant_sessions.get(session_id)
            notes = str(
                getattr(live, "context_notes", "")
                if live is not None
                else data.get("context_notes") or ""
            )
            picker = getattr(self, "_pick_save_file", None)
            if not callable(picker):
                return {"ok": False, "error": "file picker is unavailable"}
            picked = picker(save_filename=f"ArgusAI-context-{session_id[:32]}.md")
            if not picked.get("ok"):
                return picked
            target = Path(str(picked.get("path") or "")).expanduser()
            target.write_text(notes + ("\n" if notes else ""), encoding="utf-8")
            return {
                "ok": True,
                "path": str(target),
                "context_notes": notes,
                "char_count": len(notes),
            }
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}


    def _assistant_session_display_name(self, data: dict[str, Any], fallback: str = "") -> str:
        """Return a useful display name without mutating the session file."""
        raw = str(data.get("name") or "").strip()
        if raw not in _DEFAULT_ASSISTANT_SESSION_NAMES:
            return raw
        for msg in data.get("history") or []:
            if msg.get("role") != "user" or msg.get("hidden") or msg.get("ui_hidden"):
                continue
            if self._looks_like_hidden_confirmation(msg) or self._looks_like_cron_trigger(msg):
                continue
            text = str(msg.get("content") or "").strip()
            if text:
                return text[:30] + ("..." if len(text) > 30 else "")
        return raw or fallback


    def get_assistant_history(self, session_id: str) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
            p = self._session_path(session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e), "history": []}
        debug_log(f"get_assistant_history session_id={session_id}")
        if not p.exists():
            return {"ok": True, "session_id": session_id, "history": []}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            from argusai.core.agent_runtimes import normalize_session_runtime_binding

            binding = normalize_session_runtime_binding(data.get("runtime"))
            history = [
                m for m in (data.get("history") or [])
                if m.get("role") in ("user", "assistant")
                and not m.get("hidden")
                and not m.get("ui_hidden")
                and not self._looks_like_hidden_confirmation(m)
                and not self._looks_like_cron_trigger(m)
            ]
            return {
                "ok": True,
                "session_id": session_id,
                "name": self._assistant_session_display_name(data, session_id),
                "note": str(data.get("note") or ""),
                "context_notes": str(data.get("context_notes") or ""),
                "runtime": binding.to_dict(),
                "history": history,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "history": []}


    def _session_prompt_context(
        self,
        session: Any = None,
        stored: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Current context fill + active model window for the composer ring."""
        used = 0
        nbytes = 0
        if session is not None:
            estimator = getattr(session, "estimate_current_context_size", None)
            if callable(estimator):
                used, nbytes = estimator()
            else:
                used = int(getattr(session, "last_prompt_tokens_est", 0) or 0)
                nbytes = int(getattr(session, "last_prompt_bytes", 0) or 0)
        elif isinstance(stored, dict):
            from argusai.core.assistant.session_budget import estimate_context_size_from_mapping

            used, nbytes = estimate_context_size_from_mapping(stored)
        window = 0
        try:
            resolver = getattr(session, "_context_window_tokens", None)
            if callable(resolver):
                window = int(resolver() or 0)
        except Exception:
            window = 0
        if window <= 0:
            try:
                from argusai.core.ai_model_limits import resolve_endpoint_model_limits

                settings = self.settings_mgr.get()
                endpoints = [
                    ep
                    for ep in list(getattr(settings, "ai_endpoints", []) or [])
                    if getattr(ep, "enabled", True)
                ]
                endpoints.sort(key=lambda ep: int(getattr(ep, "priority", 0) or 0), reverse=True)
                if endpoints:
                    window = int(resolve_endpoint_model_limits(endpoints[0])[0] or 0)
            except Exception:
                window = 0
        return {
            "prompt_tokens": max(0, used),
            "prompt_bytes": max(0, nbytes),
            "context_window": max(0, window),
        }

    def get_assistant_token_usage(self, session_id: str) -> dict[str, Any]:
        """Return live cache/token counters without loading history.

        Used by the Agent panel to refresh the context ring / cache popover
        every few seconds while a turn is still running — ``get_assistant_session``
        would re-serialize the full transcript each poll.
        """
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        session = self._assistant_sessions.get(session_id)
        if session is not None:
            return {
                "ok": True,
                "session_id": session_id,
                "token_usage": dict(session.token_usage_total),
                "last_token_usage": (
                    dict(session.last_token_usage) if session.last_token_usage else None
                ),
                "cache_hit_rate": session.cache_hit_rate(),
                **self._session_prompt_context(session),
            }
        path = self._session_path(session_id)
        if not path.exists():
            return {"ok": False, "error": "session not found"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        raw = data.get("token_usage_total") if isinstance(data, dict) else None
        token_usage = {
            str(k): int(v or 0)
            for k, v in (raw.items() if isinstance(raw, dict) else [])
        }
        hit = int(token_usage.get("cache_hit_tokens") or 0)
        miss = int(token_usage.get("cache_miss_tokens") or 0)
        denom = hit + miss
        return {
            "ok": True,
            "session_id": session_id,
            "token_usage": token_usage,
            "last_token_usage": None,
            "cache_hit_rate": round(100.0 * hit / denom, 1) if denom > 0 else None,
            **self._session_prompt_context(stored=data if isinstance(data, dict) else None),
        }

    def get_assistant_session(self, session_id: str) -> dict[str, Any]:
        hist = self.get_assistant_history(session_id)
        if not hist.get("ok"):
            return hist
        try:
            session_id = self._validate_session_id(session_id)
            session = self._assistant_sessions.get(session_id)
            if not session and self._session_path(session_id).exists():
                session = self._ensure_session(session_id)
            summary = session.memory_summary if session else ""
            memory_count = len(session.memory_index) if session else 0
            todos = session.todos if session else []
            events = list(session.events[-2000:]) if session else []
            pending_projection = (
                session.pending_confirmation_projection()
                if session else
                {"pending_tool": None, "pending_tools": [], "pending_blocks": []}
            )
            auto_allow_tools = session.auto_approve_tools() if session else []
            auto_approve_mode = (bool(session.user_auto_approve_mode) if session else
                                 bool(getattr(self.settings_mgr.get(), "assistant_auto_approve", False)))
            allow_web_search = bool(session.allow_web_search) if session else self._default_assistant_web_search()
            thinking_enabled = bool(getattr(session, "thinking_enabled", False)) if session else False
            thinking_effort = str(getattr(session, "thinking_effort", "high") or "high") if session else "high"
            channel_live = bool(session.channel_live) if session else True
            engagement_declaration = session.engagement_declaration if session else None
            token_usage_total = dict(session.token_usage_total) if session else {}
            last_token_usage = dict(session.last_token_usage) if session and session.last_token_usage else None
            cache_hit_rate = session.cache_hit_rate() if session else None
            runtime_binding = (
                session.runtime_binding.to_dict()
                if session is not None
                else self._assistant_runtime_binding(session_id).to_dict()
            )
            active_stream = None
            for run_id, stream in reversed(
                list(getattr(self, "_assistant_streams", {}).items())
            ):
                if (
                    stream.get("session_id") != session_id
                    or bool(stream.get("done"))
                ):
                    continue
                lock = stream.get("lock")
                if lock is not None:
                    with lock:
                        event_count = len(stream.get("events") or [])
                else:
                    event_count = len(stream.get("events") or [])
                job = stream.get("job")
                active_stream = {
                    "run_id": str(run_id),
                    "event_count": event_count,
                    "cron_job_id": str(stream.get("cron_job_id") or ""),
                    "job": job.to_dict() if job is not None else None,
                }
                break
        except Exception:
            session = None
            summary = ""
            memory_count = 0
            todos = []
            events = []
            pending_projection = {
                "pending_tool": None,
                "pending_tools": [],
                "pending_blocks": [],
            }
            auto_allow_tools = []
            auto_approve_mode = False
            allow_web_search = self._default_assistant_web_search()
            thinking_enabled = False
            thinking_effort = "high"
            channel_live = False
            engagement_declaration = None
            token_usage_total = {}
            last_token_usage = None
            cache_hit_rate = None
            from argusai.core.agent_runtimes import normalize_session_runtime_binding

            runtime_binding = normalize_session_runtime_binding(None).to_dict()
            active_stream = None
        return {
            "ok": True,
            "session_id": hist.get("session_id"),
            "name": hist.get("name"),
            "note": hist.get("note") or "",
            "context_notes": str(
                getattr(session, "context_notes", "")
                if session is not None
                else hist.get("context_notes") or ""
            ),
            "message_count": len(hist.get("history") or []),
            "memory_summary": summary,
            "memory_count": memory_count,
            "todos": todos,
            "events": events,
            "auto_allow_tools": auto_allow_tools,
            "auto_approve_mode": auto_approve_mode,
            "allow_web_search": allow_web_search,
            "thinking_enabled": thinking_enabled,
            "thinking_effort": thinking_effort,
            "channel_live": channel_live,
            "engagement_declaration": engagement_declaration,
            "runtime": runtime_binding,
            "active_stream": active_stream,
            "history": hist.get("history") or [],
            "token_usage": token_usage_total,
            "last_token_usage": last_token_usage,
            "cache_hit_rate": cache_hit_rate,
            **self._session_prompt_context(session),
            **pending_projection,
        }


