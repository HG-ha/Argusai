"""Assistant GUI mixin slice split from assistant.py."""

from __future__ import annotations

from .assistant_common import *


class AssistantToolsMixin:
    def get_desktop_action_status(self, session_id: str = "") -> dict[str, Any]:
        """Return desktop continuous-action authorization status for the assistant session."""
        from argusai.core.workspace import desktop_control as dc

        sid = str(session_id or "default").strip() or "default"
        try:
            sid = self._validate_session_id(sid)
        except Exception:
            pass
        return dc.desktop_session_status(sid)

    def stop_desktop_action(self, session_id: str = "") -> dict[str, Any]:
        """Stop desktop continuous-action authorization immediately."""
        from argusai.core.workspace import desktop_control as dc

        sid = str(session_id or "default").strip() or "default"
        try:
            sid = self._validate_session_id(sid)
        except Exception:
            pass
        return dc.stop_desktop_action_session(sid)

    def open_macos_privacy_settings(self, pane: str = "accessibility") -> dict[str, Any]:
        """Open System Settings → Privacy for desktop automation (macOS only)."""
        from argusai.core.workspace import darwin_permissions as dp

        return dp.open_privacy_settings(str(pane or "accessibility"))

    def list_assistant_tools(self, session_id: str = "") -> dict[str, Any]:
        try:
            session = None
            if session_id:
                try:
                    session = self._ensure_session(self._validate_session_id(session_id))
                except Exception:
                    session = None
            if session is None:
                registry = build_tool_registry(self, "default", allow_web_search=self._default_assistant_web_search())
                session = AssistantSession(registry=registry, ai_provider=None, kb=self.kb)
                session.allow_web_search = self._default_assistant_web_search()
            return {"ok": True, "tools": session.registry.list_tools()}
        except Exception as e:
            return {"ok": False, "error": str(e), "tools": []}


    def set_assistant_web_search(self, session_id: str, enabled: bool) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            session.allow_web_search = bool(enabled)
            self._refresh_session_registry(session_id, session)
            p = self._session_path(session_id)
            name = session.history[0].content[:30] + ("..." if session.history and len(session.history[0].content) > 30 else "") if session.history else "AI 助手"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    name = str(data.get("name") or name)
                except Exception:
                    pass
            self._save_session(p, session, name)
            return {
                "ok": True,
                "session_id": session_id,
                "allow_web_search": bool(session.allow_web_search),
                "tools": session.registry.list_tools(),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_assistant_thinking(
        self,
        session_id: str,
        enabled: bool,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Toggle per-session model thinking / reasoning (CLI and channels)."""
        try:
            from argusai.core.ai_thinking import normalize_thinking_effort

            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            session.thinking_enabled = bool(enabled)
            if effort is not None:
                normalized = normalize_thinking_effort(effort, default="")
                if normalized:
                    session.thinking_effort = normalized
            p = self._session_path(session_id)
            name = "AI 助手"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    name = str(data.get("name") or name)
                except Exception:
                    pass
            self._save_session(p, session, name)
            return {
                "ok": True,
                "session_id": session_id,
                "thinking_enabled": bool(session.thinking_enabled),
                "thinking_effort": str(getattr(session, "thinking_effort", "high") or "high"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_assistant_channel_live(self, session_id: str, enabled: bool) -> dict[str, Any]:
        """Toggle mid-turn channel live updates (segments + cards only)."""
        try:
            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            session.channel_live = bool(enabled)
            p = self._session_path(session_id)
            name = "AI 助手"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    name = str(data.get("name") or name)
                except Exception:
                    pass
            self._save_session(p, session, name)
            return {
                "ok": True,
                "session_id": session_id,
                "channel_live": bool(session.channel_live),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def set_assistant_engagement(
        self,
        session_id: str,
        declaration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set or clear the per-session Greenlight Mode declaration."""
        try:
            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            payload = dict(declaration) if isinstance(declaration, dict) else None
            if payload is not None:
                payload.pop("goal_text", None)
                payload.pop("context_seed", None)
            session.set_engagement_declaration(payload)
            p = self._session_path(session_id)
            name = "AI 助手"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    name = str(data.get("name") or name)
                except Exception:
                    pass
            self._save_session(p, session, name)
            return {
                "ok": True,
                "session_id": session_id,
                "engagement_declaration": session.engagement_declaration,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_assistant_auto_approve_mode(
        self,
        session_id: str,
        enabled: bool,
        persist: bool = False,
    ) -> dict[str, Any]:
        """Enable auto-approve for this conversation and persist its snapshot.

        When on, the runtime auto-allows confirmable tools (including high-risk)
        and auto-picks user decision / todo-plan cards until the turn finishes.
        Legacy callers may explicitly set ``persist=True`` to also change the
        default for new conversations. Existing conversations are never changed.
        """
        try:
            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            enabled = bool(enabled)
            from argusai.core.assistant.tool_policy import apply_settings_tool_always_allow

            settings = self.settings_mgr.get()
            if persist:
                try:
                    if bool(getattr(settings, "assistant_auto_approve", False)) != enabled:
                        def _update(current):
                            current.assistant_auto_approve = enabled
                            return current

                        settings = self.settings_mgr.update(_update)
                except Exception:
                    pass
            session.apply_user_auto_approve_mode(enabled)
            if not enabled:
                apply_settings_tool_always_allow(session, settings)
            child_sync = self._sync_child_auto_allow(session)
            p = self._session_path(session_id)
            name = "AI 助手"
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    name = str(data.get("name") or name)
                except Exception:
                    pass
            self._save_session(p, session, name)
            return {
                "ok": True,
                "session_id": session_id,
                "auto_approve_mode": bool(session.user_auto_approve_mode),
                "auto_allow_tools": session.auto_approve_tools(),
                "child_sync": child_sync,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_assistant_auto_allow_all(
        self,
        session_id: str,
        enabled: bool,
        include_high_risk: bool = False,
        include_reverse_dynamic: bool = False,
    ) -> dict[str, Any]:
        """Bulk toggle auto-allow for every confirmable tool in the session.

        ``enabled=True`` adds every CONFIRM_TOOLS to auto-allow. By
        default this excludes NO_AUTO_APPROVE_TOOLS. Pass
        ``include_reverse_dynamic=True`` after the user acknowledges Frida
        attach/spawn in the session allow-all flow. Pass
        ``include_high_risk=True`` only after the user acknowledges sandbox
        scripts / SQLi / raw HTTP risk.

        ``enabled=False`` clears the whole auto-allow set.
        """
        try:
            session_id = self._validate_session_id(session_id)
            session = self._ensure_session(session_id)
            auto_allow_tools = session.set_auto_approve_all(
                bool(enabled),
                include_high_risk=bool(include_high_risk),
                include_reverse_dynamic=bool(include_reverse_dynamic),
            )
            child_sync = self._sync_child_auto_allow(session)
            return {
                "ok": True,
                "session_id": session_id,
                "enabled": bool(enabled),
                "include_high_risk": bool(include_high_risk),
                "include_reverse_dynamic": bool(include_reverse_dynamic),
                "auto_allow_tools": auto_allow_tools,
                "child_sync": child_sync,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def set_assistant_auto_allow(
        self,
        session_id: str,
        tool_name: str,
        enabled: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
            tool_name = (tool_name or "").strip()
            if not tool_name:
                return {"ok": False, "error": "tool_name is required"}
            session = self._ensure_session(session_id)
            session.set_auto_approve_tool(tool_name, bool(enabled), force=bool(force))
            child_sync = self._sync_child_auto_allow(session)
            return {
                "ok": True,
                "session_id": session_id,
                "tool_name": tool_name,
                "enabled": bool(enabled),
                "force": bool(force),
                "auto_allow_tools": session.auto_approve_tools(),
                "child_sync": child_sync,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _sync_child_auto_allow(self, session: Any) -> dict[str, Any]:
        """Push the parent's auto-allow policy into live child Agents."""
        try:
            from argusai.core.assistant.agent_threads import get_agent_thread_manager

            return get_agent_thread_manager(session).sync_auto_approve_from_parent()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


    def open_assistant_sandbox(
        self,
        session_id: str,
        path: str = "",
    ) -> dict[str, Any]:
        """Open the session sandbox (or a path inside it) in the OS file manager.

        This is the same folder ``sandbox_reveal_in_folder`` (the AI-facing
        tool) would open, but exposed as a regular UI button so the user
        doesn't need to ask the assistant to do it. Creates the sandbox
        directory on first use — same lazy-init behaviour as
        ``sandbox_dir_for_session`` everywhere else in the code.

        ``path`` is an optional sandbox-relative path. Empty opens the root;
        a directory opens that folder; a file selects it in the file manager.
        """
        from argusai.core.workspace.session_sandbox import (
            reveal_in_folder,
            sandbox_dir_for_session,
        )

        debug_log(f"open_assistant_sandbox session_id={session_id!r} path={path!r}")
        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            debug_log(f"open_assistant_sandbox resolved root={root}")
            result = reveal_in_folder(root, str(path or "").strip())
            result.setdefault("session_id", session_id)
            return result
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def get_assistant_sandbox_summary(self, session_id: str) -> dict[str, Any]:
        """Return sandbox statistics without creating a missing sandbox."""
        from argusai.core.workspace.session_sandbox import get_session_sandbox_summary

        try:
            session_id = self._validate_session_id(session_id)
            return get_session_sandbox_summary(self._sessions_dir, session_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def read_assistant_sandbox_file(
        self,
        session_id: str,
        path: str,
        max_chars: int = 60000,
        as_base64: bool = False,
        offset_chars: int = 0,
    ) -> dict[str, Any]:
        """Read a file from the current assistant session's sandbox."""
        from argusai.core.workspace.session_sandbox import read_file, sandbox_dir_for_session

        debug_log(f"read_assistant_sandbox_file session_id={session_id!r} path={path!r} as_base64={as_base64}")
        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            limit = max(1, min(int(max_chars or 60000), 200000))
            result = read_file(
                root,
                path,
                limit,
                as_base64=as_base64,
                offset_chars=max(0, int(offset_chars or 0)),
            )
            result.setdefault("session_id", session_id)
            return result
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def list_assistant_sandbox_directory(self, session_id: str, path: str = "") -> dict[str, Any]:
        """List immediate children in a sandbox directory."""
        from argusai.core.workspace.session_sandbox import list_directory, sandbox_dir_for_session

        debug_log(f"list_assistant_sandbox_directory session_id={session_id!r} path={path!r}")
        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            result = list_directory(root, path)
            result.setdefault("session_id", session_id)
            return result
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def export_assistant_sandbox_items(
        self,
        session_id: str,
        paths: list[str] | None = None,
        destination_dir: str = "",
    ) -> dict[str, Any]:
        """Zip selected sandbox files/directories into a user-chosen folder."""
        import os
        import zipfile

        from argusai.core.workspace.session_sandbox import (
            PY_VENV_DIR,
            audit_event,
            resolve_in_sandbox,
            sandbox_dir_for_session,
        )

        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            selected = [str(p or "").strip().replace("\\", "/").strip("/") for p in (paths or []) if str(p or "").strip()]
            if not selected:
                return {"ok": False, "error": "no paths selected"}
            dest_text = str(destination_dir or "").strip()
            if dest_text:
                export_dir = Path(os.path.expandvars(dest_text)).expanduser().resolve()
                export_dir.mkdir(parents=True, exist_ok=True)
            else:
                picked = self._pick_export_folder()
                if picked.get("cancelled"):
                    return {"ok": False, "cancelled": True}
                if not picked.get("ok"):
                    return picked
                export_dir = Path(str(picked["path"])).resolve()
            zip_path = export_dir / f"sandbox-export-{int(time.time())}.zip"
            root_resolved = root.resolve()
            added: list[str] = []
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for rel in selected[:200]:
                    src = resolve_in_sandbox(root, rel)
                    if not src.exists():
                        continue
                    candidates = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
                    for file_path in candidates:
                        try:
                            arc = file_path.resolve().relative_to(root_resolved).as_posix()
                        except ValueError:
                            continue
                        if arc == PY_VENV_DIR or arc.startswith(f"{PY_VENV_DIR}/"):
                            continue
                        if any(part.startswith(".") and part != ".tmp" for part in arc.split("/")):
                            continue
                        zf.write(file_path, arc)
                        added.append(arc)
            rel_zip = ""
            try:
                rel_zip = zip_path.relative_to(root_resolved).as_posix()
            except ValueError:
                rel_zip = zip_path.name
            audit_event(root, "export_items", paths=selected, output=str(zip_path), file_count=len(added))
            return {
                "ok": True,
                "session_id": session_id,
                "relative_path": rel_zip,
                "path": str(zip_path),
                "folder": str(export_dir),
                "file_count": len(added),
                "size": zip_path.stat().st_size,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def export_assistant_sandbox_file(
        self,
        session_id: str,
        path: str,
        destination: str = "",
    ) -> dict[str, Any]:
        """Copy one sandbox file to a user-chosen path via the save dialog."""
        import os
        import shutil

        from argusai.core.workspace.session_sandbox import audit_event, resolve_in_sandbox, sandbox_dir_for_session

        try:
            session_id = self._validate_session_id(session_id)
            rel = str(path or "").strip().replace("\\", "/").strip("/")
            if not rel:
                return {"ok": False, "error": "path is required"}
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            src = resolve_in_sandbox(root, rel)
            if not src.exists():
                return {"ok": False, "error": f"not found: {rel}"}
            if not src.is_file():
                return {"ok": False, "error": "only files can be exported individually"}
            dest_text = str(destination or "").strip()
            if dest_text:
                target = Path(os.path.expandvars(dest_text)).expanduser()
            else:
                picked = self._pick_save_file(save_filename=src.name)
                if picked.get("cancelled"):
                    return {"ok": False, "cancelled": True}
                if not picked.get("ok"):
                    return picked
                target = Path(str(picked["path"])).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            audit_event(root, "export_file", path=rel, output=str(target.resolve()))
            return {
                "ok": True,
                "session_id": session_id,
                "source": rel,
                "path": str(target.resolve()),
                "folder": str(target.parent.resolve()),
                "size": target.stat().st_size,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def delete_assistant_sandbox_items(
        self,
        session_id: str,
        paths: list[str] | None = None,
        confirm_label: str = "",
    ) -> dict[str, Any]:
        """Delete selected sandbox files/directories after UI confirmation."""
        import shutil

        from argusai.core.workspace.session_sandbox import AUDIT_FILE, audit_event, resolve_in_sandbox, sandbox_dir_for_session

        if str(confirm_label or "").strip() != "DELETE_SANDBOX_ITEMS":
            return {"ok": False, "error": "confirm_label must be DELETE_SANDBOX_ITEMS"}
        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            selected = [str(p or "").strip().replace("\\", "/").strip("/") for p in (paths or []) if str(p or "").strip()]
            if not selected:
                return {"ok": False, "error": "no paths selected"}
            removed: list[str] = []
            for rel in sorted(set(selected), key=lambda x: x.count("/"), reverse=True)[:200]:
                target = resolve_in_sandbox(root, rel)
                if target.name == AUDIT_FILE:
                    continue
                if not target.exists():
                    continue
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()
                else:
                    continue
                removed.append(rel)
            audit_event(root, "delete_items", paths=removed)
            return {"ok": True, "session_id": session_id, "removed": removed, "count": len(removed)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def move_assistant_sandbox_items(
        self,
        session_id: str,
        paths: list[str] | None = None,
        destination: str = "",
        confirm_label: str = "",
    ) -> dict[str, Any]:
        """Move selected sandbox files/directories into a sandbox destination directory."""
        import shutil

        from argusai.core.workspace.session_sandbox import AUDIT_FILE, audit_event, resolve_in_sandbox, sandbox_dir_for_session

        if str(confirm_label or "").strip() != "MOVE_SANDBOX_ITEMS":
            return {"ok": False, "error": "confirm_label must be MOVE_SANDBOX_ITEMS"}
        try:
            session_id = self._validate_session_id(session_id)
            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            selected = [str(p or "").strip().replace("\\", "/").strip("/") for p in (paths or []) if str(p or "").strip()]
            dest_rel = str(destination or "").strip().replace("\\", "/").strip("/")
            if not selected:
                return {"ok": False, "error": "no paths selected"}
            dest = root if not dest_rel else resolve_in_sandbox(root, dest_rel)
            dest.mkdir(parents=True, exist_ok=True)
            moved: list[dict[str, str]] = []
            for rel in sorted(set(selected), key=lambda x: x.count("/"), reverse=True)[:200]:
                src = resolve_in_sandbox(root, rel)
                if not src.exists() or src.name == AUDIT_FILE:
                    continue
                src_resolved = src.resolve()
                dest_resolved = dest.resolve()
                if src_resolved == dest_resolved or src_resolved in dest_resolved.parents:
                    continue
                target = dest / src.name
                if target.exists():
                    return {"ok": False, "error": f"destination exists: {target.relative_to(root).as_posix()}"}
                shutil.move(str(src), str(target))
                moved.append({"from": rel, "to": target.relative_to(root).as_posix()})
            audit_event(root, "move_items", destination=dest_rel, items=moved)
            return {"ok": True, "session_id": session_id, "moved": moved, "count": len(moved)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


    def search_assistant_memory(
        self, session_id: str, query: str, limit: int = 8
    ) -> dict[str, Any]:
        try:
            session_id = self._validate_session_id(session_id)
            query = (query or "").strip()
            if not query:
                return {"ok": False, "error": "query is required", "results": []}
            session = self._assistant_sessions.get(session_id)
            if not session:
                p = self._session_path(session_id)
                if not p.exists():
                    return {"ok": True, "session_id": session_id, "results": []}
                registry = build_tool_registry(self, session_id)
                data = json.loads(p.read_text(encoding="utf-8"))
                session = AssistantSession.from_dict(data, registry, None, kb=self.kb)
            return {
                "ok": True,
                "session_id": session_id,
                "query": query,
                "results": session.search_memory(query, limit=int(limit or 8)),
                "memory_summary": session.memory_summary,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e), "results": []}
        except Exception as e:
            return {"ok": False, "error": str(e), "results": []}


    def search_all_assistant_memory(
        self,
        query: str,
        limit: int = 10,
        session_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "query is required", "results": []}
        allowed = None
        if session_ids is not None:
            allowed = {str(item).strip() for item in session_ids if str(item).strip()}
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        registry = build_tool_registry(self, "default")

        def add_session_hits(session_id: str, session: AssistantSession, name: str = "") -> None:
            if allowed is not None and session_id not in allowed:
                return
            seen.add(session_id)
            for hit in session.search_memory(query, limit=max(1, int(limit or 10))):
                item = dict(hit)
                item["session_id"] = session_id
                item["session_name"] = name or session_id
                results.append(item)

        for sid, session in list(self._assistant_sessions.items()):
            add_session_hits(sid, session)

        for f in sorted(self._sessions_dir.glob("*.json"), key=lambda x: -x.stat().st_mtime):
            sid = f.stem
            if sid in seen or (allowed is not None and sid not in allowed):
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                session = AssistantSession.from_dict(data, registry, None, kb=self.kb)
                add_session_hits(sid, session, data.get("name", sid))
            except Exception:
                continue

        results.sort(key=lambda x: float(x.get("timestamp") or 0), reverse=True)
        return {
            "ok": True,
            "query": query,
            "results": results[:max(1, int(limit or 10))],
            "session_count": len(seen),
        }


