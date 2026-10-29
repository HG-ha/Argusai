"""Read/write the persisted application settings."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def _deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge JSON objects without mutating either input."""
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_ai_settings(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge AI settings while treating auxiliary API keys as write-only patches."""
    merged = dict(current)
    for key, value in patch.items():
        if key != "auxiliary":
            merged[key] = value

    auxiliary = dict(current.get("auxiliary") or {})
    auxiliary_patch = patch.get("auxiliary")
    if not isinstance(auxiliary_patch, dict):
        merged["auxiliary"] = auxiliary
        return merged

    for kind in ("vision", "asr"):
        endpoint_patch_raw = auxiliary_patch.get(kind)
        if not isinstance(endpoint_patch_raw, dict):
            continue
        endpoint_patch = dict(endpoint_patch_raw)
        clear_api_key = bool(endpoint_patch.pop("clear_api_key", False))
        if clear_api_key:
            endpoint_patch["api_key"] = ""
        elif "api_key" in endpoint_patch and not str(endpoint_patch.get("api_key") or "").strip():
            # Blank values commonly come from initial form hydration/autosave.
            # Only the explicit one-shot flag above is allowed to clear a key.
            endpoint_patch.pop("api_key", None)
        auxiliary[kind] = _deep_merge_dict(
            dict(auxiliary.get(kind) or {}),
            endpoint_patch,
        )
    merged["auxiliary"] = auxiliary
    return merged


def _merge_document_parse_settings(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge document_parse settings; blank MinerU api_key means keep existing."""
    merged = dict(current or {})
    for key, value in (patch or {}).items():
        if key != "mineru":
            merged[key] = value

    mineru = dict(merged.get("mineru") or {}) if isinstance(merged.get("mineru"), dict) else {}
    mineru_patch_raw = patch.get("mineru") if isinstance(patch, dict) else None
    if not isinstance(mineru_patch_raw, dict):
        merged["mineru"] = mineru
        return merged

    mineru_patch = dict(mineru_patch_raw)
    clear_api_key = bool(mineru_patch.pop("clear_api_key", False))
    if clear_api_key:
        mineru_patch["api_key"] = ""
    elif "api_key" in mineru_patch and not str(mineru_patch.get("api_key") or "").strip():
        mineru_patch.pop("api_key", None)
    merged["mineru"] = _deep_merge_dict(mineru, mineru_patch)
    return merged


class SettingsMixin:
    """Expose :class:`SettingsManager` to the frontend.

    The shape returned by :py:meth:`get_settings` mirrors
    :py:meth:`AppSettings.to_dict`; the frontend merges partial sections
    in via :py:meth:`save_settings` (only the keys actually present are
    overwritten, the rest keep their previous values).
    """

    def get_settings(self) -> dict[str, Any]:
        payload = self.settings_mgr.get().to_dict()
        metadata = getattr(self.settings_mgr, "metadata", None)
        if callable(metadata):
            payload["_settings_meta"] = metadata(consume_recovery=True)
        return payload

    def _set_data_root_migration_progress(self, **updates: Any) -> None:
        lock = getattr(self, "_data_root_migration_lock", None)
        if lock is None:
            return
        with lock:
            current = dict(getattr(self, "_data_root_migration", {}) or {})
            current.update(updates)
            current["percent"] = max(0, min(100, int(current.get("percent") or 0)))
            current["updated_at"] = time.time()
            self._data_root_migration = current

    def get_data_root_change_progress(self) -> dict[str, Any]:
        """Return the latest in-process data-root migration progress."""
        lock = getattr(self, "_data_root_migration_lock", None)
        if lock is None:
            return {"ok": True, "status": "idle", "phase": "idle", "percent": 0}
        with lock:
            return {"ok": True, **dict(self._data_root_migration)}

    def default_dirs(self) -> dict[str, Any]:
        """Return the OS-default ``data_root`` plus its derived subpaths.

        Pure metadata — does NOT mutate active settings. The frontend
        uses this so the storage panel can render a "Restore default"
        affordance on the single root field, and surface the four
        derived subdirectories as a read-only preview.
        """
        from argusai.core.paths import (
            PERSISTENT_DATA_SUBDIRS,
            SUBDIR_ASSISTANT_CRON,
            SUBDIR_CASES,
            SUBDIR_CHANNELS,
            SUBDIR_KNOWLEDGE,
            SUBDIR_LOGS,
            SUBDIR_POC_PAGES,
            SUBDIR_REVERSE_JOBS,
            SUBDIR_SESSIONS,
            SUBDIR_SESSION_VAULT,
            SUBDIR_TASKS,
            SUBDIR_USER_TODOS,
        )
        from argusai.core.settings import default_data_root

        root = default_data_root()
        return {
            "ok": True,
            "data_root": root,
            "subdir_names": {
                "tasks": SUBDIR_TASKS,
                "sessions": SUBDIR_SESSIONS,
                "poc_pages": SUBDIR_POC_PAGES,
                "knowledge_base": SUBDIR_KNOWLEDGE,
                "reverse_jobs": SUBDIR_REVERSE_JOBS,
                "cases": SUBDIR_CASES,
                "session_vault": SUBDIR_SESSION_VAULT,
                "user_todos": SUBDIR_USER_TODOS,
                "assistant_cron": SUBDIR_ASSISTANT_CRON,
                "channels": SUBDIR_CHANNELS,
                "logs": SUBDIR_LOGS,
            },
            "persistent_subdirs": list(PERSISTENT_DATA_SUBDIRS),
            "tasks_dir": str(Path(root) / SUBDIR_TASKS),
            "sessions_dir": str(Path(root) / SUBDIR_SESSIONS),
            "poc_pages_dir": str(Path(root) / SUBDIR_POC_PAGES),
            "knowledge_base_dir": str(Path(root) / SUBDIR_KNOWLEDGE),
            "reverse_jobs_dir": str(Path(root) / SUBDIR_REVERSE_JOBS),
            "cases_dir": str(Path(root) / SUBDIR_CASES),
            "session_vault_dir": str(Path(root) / SUBDIR_SESSION_VAULT),
            "user_todos_dir": str(Path(root) / SUBDIR_USER_TODOS),
            "assistant_cron_dir": str(Path(root) / SUBDIR_ASSISTANT_CRON),
            "channels_dir": str(Path(root) / SUBDIR_CHANNELS),
            "logs_dir": str(Path(root) / SUBDIR_LOGS),
        }

    def choose_data_root_directory(self, initial_dir: str = "") -> dict[str, Any]:
        """Open the native folder picker without changing active settings."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for folder dialog"}

            candidate = Path(str(initial_dir or self.settings_mgr.get().data_root)).expanduser()
            while not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            if not candidate.is_dir():
                candidate = Path.home()

            picked = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(candidate.resolve()),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            return {"ok": True, "path": str(Path(str(selected)).expanduser().resolve())}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def scan_toolchain_paths(self) -> dict[str, Any]:
        """Probe PATH (plus any already-saved overrides) and suggest paths for empty settings fields."""
        from argusai.core.reverse.toolchain import (
            clear_toolchain_cache,
            detect_tool,
            sync_path_overrides_from_settings,
        )
        from argusai.core.reverse.toolchain_settings import (
            SETTINGS_PROBE_ALIASES,
            SETTINGS_TOOLCHAIN_KEYS,
        )

        current = self.settings_mgr.get()
        existing = {
            str(k).strip().lower(): str(v).strip()
            for k, v in (current.toolchain_paths or {}).items()
            if str(k).strip() and str(v).strip()
        }
        sync_path_overrides_from_settings(current)
        clear_toolchain_cache()

        suggestions: dict[str, str] = {}
        tools: dict[str, dict[str, Any]] = {}
        for key in SETTINGS_TOOLCHAIN_KEYS:
            info: dict[str, Any] = {"name": key, "available": False, "path": ""}
            for probe_name in SETTINGS_PROBE_ALIASES.get(key, (key,)):
                info = detect_tool(probe_name)
                if info.get("available"):
                    break
            tools[key] = info
            if existing.get(key):
                continue
            path = str(info.get("path") or "").strip()
            if info.get("available") and path:
                suggestions[key] = path

        available = [k for k, info in tools.items() if info.get("available")]
        missing = [k for k, info in tools.items() if not info.get("available")]
        from argusai.core.paths import PathManager
        from argusai.core.reverse.toolchain_install import enrich_toolchain_scan_result

        payload = {
            "ok": True,
            "suggestions": suggestions,
            "filled_count": len(suggestions),
            "available": available,
            "missing": missing,
            "tools": tools,
        }
        tools_dir = PathManager(data_root=current.data_root).tools_dir
        return enrich_toolchain_scan_result(payload, data_root=tools_dir)

    def install_reverse_toolchain(self, tool: str, force: bool = False) -> dict[str, Any]:
        """Download/install one reverse CLI under data_root/tools/reverse and persist settings path."""
        from argusai.core.paths import PathManager
        from argusai.core.reverse.toolchain import (
            clear_toolchain_cache,
            sync_path_overrides_from_settings,
        )
        from argusai.core.reverse.toolchain_install import (
            install_reverse_tool,
            installable_tools,
        )

        key = str(tool or "").strip().lower()
        if not key:
            return {"ok": False, "error": "tool name required"}
        if key not in installable_tools():
            return {
                "ok": False,
                "error": f"{key} is not a managed-install target",
                "supported": list(installable_tools()),
            }
        current = self.settings_mgr.get()
        sync_path_overrides_from_settings(current)
        tools_dir = PathManager(data_root=current.data_root).tools_dir
        result = install_reverse_tool(
            key,
            settings_mgr=self.settings_mgr,
            data_root=tools_dir,
            force=bool(force),
        )
        if result.get("ok"):
            sync_path_overrides_from_settings(self.settings_mgr.get())
            clear_toolchain_cache()
        return result

    def sandbox_runtime_status(self) -> dict[str, Any]:
        """Return health and diagnostics for managed Python, Node, and WABT."""
        from argusai.core.managed_runtimes import (
            all_runtime_status,
            archive_sources,
            archive_spec,
            npm_registries,
        )
        from argusai.core.paths import PathManager

        settings = self.settings_mgr.get()
        tools_dir = PathManager(data_root=settings.data_root).tools_dir
        result = all_runtime_status(tools_dir)
        result["mirrors"] = dict(settings.runtime_mirrors or {})
        for name in ("python", "node", "wabt"):
            try:
                sources = archive_sources(archive_spec(name), settings.runtime_mirrors)
            except Exception:
                sources = []
            result[name]["download_sources"] = sources
        result["node"]["npm_registries"] = npm_registries(settings.runtime_mirrors)
        return result

    def install_sandbox_runtime(self, runtime: str, force: bool = False) -> dict[str, Any]:
        """Install or atomically repair one managed toolchain."""
        from argusai.core.managed_runtimes import install_runtime
        from argusai.core.paths import PathManager

        name = str(runtime or "").strip().lower()
        if name not in {"python", "node", "wabt"}:
            return {
                "ok": False,
                "error_code": "invalid_runtime",
                "error": "runtime must be 'python', 'node', or 'wabt'",
            }
        settings = self.settings_mgr.get()
        tools_dir = PathManager(data_root=settings.data_root).tools_dir
        return install_runtime(
            name,
            tools_dir=tools_dir,
            force=bool(force),
            mirrors=settings.runtime_mirrors,
        )

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        from argusai.core.infra.logging_setup import apply_logging_settings
        from argusai.core.settings import (
            AppSettings,
            SettingsVersionConflictError,
            _parse,
        )

        expected_version = str(data.get("_settings_version") or "")
        previous = self.settings_mgr.get()
        previous_cron_listener = (
            str(getattr(previous, "assistant_cron_webhook_host", "127.0.0.1")),
            int(getattr(previous, "assistant_cron_webhook_port", 8765) or 8765),
        )

        def _merge_latest(latest: AppSettings) -> AppSettings:
            raw = latest.to_dict()
            for section in (
                "proxy",
                "security",
                "browser",
                "toolchain",
                "runtime",
                "oob",
                "assistant",
                "logging",
                "mcp",
            ):
                patch = data.get(section)
                if section in data and isinstance(patch, dict):
                    raw[section] = {
                        **dict(raw.get(section) or {}),
                        **patch,
                    }

            gui_patch = data.get("gui")
            if "gui" in data and isinstance(gui_patch, dict):
                current_gui = dict(raw.get("gui") or {})
                merged_gui = {
                    **current_gui,
                    **{key: value for key, value in gui_patch.items() if key != "window"},
                }
                window_patch = gui_patch.get("window")
                if isinstance(window_patch, dict):
                    merged_gui["window"] = {
                        **dict(current_gui.get("window") or {}),
                        **window_patch,
                    }
                raw["gui"] = merged_gui

            ai_patch = data.get("ai")
            if "ai" in data and isinstance(ai_patch, dict):
                raw["ai"] = _merge_ai_settings(dict(raw.get("ai") or {}), ai_patch)

            document_parse_patch = data.get("document_parse")
            if "document_parse" in data and isinstance(document_parse_patch, dict):
                raw["document_parse"] = _merge_document_parse_settings(
                    dict(raw.get("document_parse") or {}),
                    document_parse_patch,
                )

            for key in ("kb_trash_retention_days", "user_habits", "agent_runtimes"):
                if key in data:
                    raw[key] = data[key]

            # data_root changes require the explicit migration API. The latest
            # settings object also retains gui.window and every omitted field.
            raw["data_root"] = latest.data_root
            return _parse(raw)

        update = getattr(self.settings_mgr, "update", None)
        if callable(update):
            try:
                new_settings = update(
                    _merge_latest,
                    expected_revision=expected_version,
                )
            except SettingsVersionConflictError as exc:
                return {
                    "ok": False,
                    "code": "settings_version_conflict",
                    "error": str(exc),
                    "settings_version": exc.actual,
                }
        else:
            # Preserve compatibility with lightweight manager implementations.
            # The production SettingsManager always uses the transaction above.
            new_settings = _merge_latest(self.settings_mgr.get())
            self.settings_mgr.save(new_settings)
        try:
            from argusai.core.workspace.session_sandbox import clear_shell_capability_note_cache

            clear_shell_capability_note_cache()
        except Exception:
            pass
        try:
            apply_logging_settings(new_settings.logging, data_root=new_settings.data_root)
        except Exception:
            pass
        try:
            self._channels_runtime().refresh()
        except Exception:
            pass
        current_cron_listener = (
            str(getattr(new_settings, "assistant_cron_webhook_host", "127.0.0.1")),
            int(getattr(new_settings, "assistant_cron_webhook_port", 8765) or 8765),
        )
        if current_cron_listener != previous_cron_listener:
            try:
                from argusai.core.assistant.cron import get_cron_scheduler

                scheduler = get_cron_scheduler()
                if scheduler is not None and scheduler.is_active:
                    self.start_assistant_cron_scheduler()
            except Exception:
                pass
        revision = getattr(self.settings_mgr, "revision", None)
        return {
            "ok": True,
            "settings_version": revision() if callable(revision) else "",
        }

    def preview_data_root_change(self, new_root: str = "") -> dict[str, Any]:
        """Validate a proposed data_root change without transferring files."""
        from argusai.core.settings import normalize_data_root
        from argusai.core.workspace.data_root_migrate import preview_data_root_change

        current, current_revision = self.settings_mgr.snapshot()
        dest = normalize_data_root(new_root)
        preview = preview_data_root_change(
            current.data_root,
            dest,
            toolchain_paths=current.toolchain_paths,
            mcp_servers=current.mcp_servers,
            agent_runtimes=current.agent_runtimes,
            log_dir=str(getattr(current.logging, "log_dir", "") or ""),
        )
        return {"ok": bool(preview.get("ok")), **preview}

    def apply_data_root_change(self, new_root: str = "", mode: str = "copy") -> dict[str, Any]:
        """Copy/move/switch data_root, rewrite absolute settings paths, then persist."""
        self._set_data_root_migration_progress(
            status="running",
            phase="preparing",
            percent=0,
            error="",
            current_subdir="",
            completed_bytes=0,
            total_work_bytes=0,
        )

        def _progress(payload: dict[str, Any]) -> None:
            self._set_data_root_migration_progress(status="running", **dict(payload or {}))

        def _fail(message: str) -> None:
            self._set_data_root_migration_progress(
                status="failed",
                phase="failed",
                error=str(message or "migration failed"),
            )

        active_streams = [
            stream
            for stream in getattr(self, "_assistant_streams", {}).values()
            if not stream.get("done")
        ]
        if active_streams or bool(getattr(self, "_assistant_running_sessions", set())):
            _fail("data_root_busy: wait for active assistant runs to finish")
            return {
                "ok": False,
                "error": "data_root_busy: wait for active assistant runs to finish",
            }
        try:
            from argusai.core.workspace.shell_sessions import get_shell_session_manager

            active_shells = [
                item
                for item in get_shell_session_manager().list_sessions()
                if item.get("state") in {"starting", "running"}
            ]
        except Exception:
            active_shells = []
        if active_shells:
            _fail("data_root_busy: stop active shell sessions before migration")
            return {
                "ok": False,
                "error": "data_root_busy: stop active shell sessions before migration",
            }
        from argusai.core.infra.logging_setup import apply_logging_settings
        from argusai.core.reverse.toolchain import sync_path_overrides_from_settings
        from argusai.core.settings import SettingsVersionConflictError, normalize_data_root
        from argusai.core.workspace.data_root_migrate import (
            apply_data_root_change,
            finalize_data_root_move,
            rollback_data_root_change,
        )

        scheduler_was_running = False
        try:
            from argusai.core.assistant.cron import get_cron_scheduler

            scheduler = get_cron_scheduler()
            scheduler_was_running = bool(
                scheduler
                and getattr(scheduler, "_thread", None)
                and scheduler._thread.is_alive()
            )
            if scheduler_was_running:
                scheduler.stop()
        except Exception:
            scheduler_was_running = False

        def _resume_scheduler() -> None:
            if scheduler_was_running:
                try:
                    self.start_assistant_cron_scheduler()
                except Exception:
                    pass

        snapshot = getattr(self.settings_mgr, "snapshot", None)
        if callable(snapshot):
            current, current_revision = snapshot()
        else:
            current = self.settings_mgr.get()
            current_revision = ""
        try:
            result = apply_data_root_change(
                current,
                normalize_data_root(new_root),
                mode=str(mode or "copy"),
                progress=_progress,
            )
        except Exception as exc:
            _resume_scheduler()
            _fail(str(exc))
            return {"ok": False, "error": f"migration failed: {exc}"}
        if not result.get("ok"):
            _resume_scheduler()
            _fail(str(result.get("error") or "migration failed"))
            return {
                "ok": False,
                "error": result.get("error") or "migration failed",
                "preview": result.get("preview"),
            }
        updated = result["settings"]
        try:
            self.settings_mgr.update(
                lambda _latest: updated,
                expected_revision=current_revision,
            )
        except SettingsVersionConflictError as exc:
            rollback = rollback_data_root_change(result)
            _resume_scheduler()
            _fail(str(exc))
            return {
                "ok": False,
                "error": str(exc),
                "code": "settings_version_conflict",
                "rollback": rollback,
                "preview": result.get("preview"),
            }
        except Exception as exc:
            # A save implementation may fail after touching its file. Restore
            # the prior pointer before removing verified destination copies.
            restored = False
            restore_error = ""
            try:
                self.settings_mgr.update(lambda _latest: current)
                restored = True
            except Exception as restore_exc:
                restore_error = str(restore_exc)
            rollback = (
                rollback_data_root_change(result)
                if restored
                else {
                    "ok": False,
                    "removed": [],
                    "errors": ["destination retained because prior settings could not be restored"],
                }
            )
            _resume_scheduler()
            _fail(f"failed to save migrated settings: {exc}")
            return {
                "ok": False,
                "error": f"failed to save migrated settings: {exc}",
                "restore_error": restore_error,
                "rollback": rollback,
                "preview": result.get("preview"),
            }
        try:
            source_cleanup = finalize_data_root_move(result, progress=_progress)
        except Exception as exc:
            source_cleanup = {
                "ok": False,
                "cleaned": [],
                "errors": [f"source cleanup failed: {exc}"],
            }
        try:
            sync_path_overrides_from_settings(updated)
        except Exception:
            pass
        try:
            # Rebinding the handlers here closes the old files and points
            # subsequent records at <new data_root>/logs. Records already on
            # disk stay under the old root: on Windows the running process
            # cannot relocate a log file it holds open.
            apply_logging_settings(updated.logging, data_root=updated.data_root)
        except Exception:
            pass
        _resume_scheduler()
        self._set_data_root_migration_progress(
            status="completed",
            phase="completed",
            percent=100,
            current_subdir="",
            completed_at=time.time(),
        )
        return {
            "ok": True,
            "mode": result.get("mode"),
            "source": result.get("source"),
            "destination": result.get("destination"),
            "transferred": result.get("transferred") or [],
            "path_rewrites": result.get("path_rewrites") or [],
            "path_rewrite_skipped": result.get("path_rewrite_skipped") or [],
            "mcp_launcher_repairs": result.get("mcp_launcher_repairs") or [],
            "source_cleanup": source_cleanup,
            "restart_required": True,
            "settings": updated.to_dict(),
        }

    def list_mcp_servers(self) -> dict[str, Any]:
        from argusai.core.settings import normalize_mcp_servers
        from argusai.core.tools.case_mcp import mcp_discovery_cache

        servers = normalize_mcp_servers(self.settings_mgr.get().mcp_servers)
        cache = mcp_discovery_cache(self)
        out: list[dict[str, Any]] = []
        for s in servers:
            sid = str(s.get("id") or "").strip()
            entry = cache.get(sid) if isinstance(cache, dict) else None
            if isinstance(entry, dict):
                last_probe = entry.get("last_probe")
                if isinstance(last_probe, dict):
                    s = {**s, "last_probe": last_probe}
                info = entry.get("server_info")
                if isinstance(info, dict) and info.get("name"):
                    s.setdefault("server_info", info)
            out.append(s)
        probe_status = getattr(self, "_mcp_startup_probe_status", None)
        return {
            "ok": True,
            "servers": out,
            "startup_probe": dict(probe_status) if isinstance(probe_status, dict) else None,
        }

    def search_mcp_registry(
        self,
        query: str = "",
        cursor: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the official, read-only MCP Registry.

        Results are normalized as untrusted catalogue metadata. Any generated
        server config is disabled with high-risk, confirm-every-tool defaults;
        this method never downloads, saves, enables, or executes a server.
        """
        from argusai.core.infra.mcp_registry import search_mcp_registry

        try:
            return search_mcp_registry(query, cursor, limit)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"MCP Registry request failed: {exc}",
            }

    def search_mcp_marketplace(
        self,
        source: str = "modelscope",
        query: str = "",
        cursor: str = "",
        limit: int = 20,
        category: str = "",
        hosted: bool | str | None = None,
    ) -> dict[str, Any]:
        """Search one public MCP catalogue without installing or executing it."""
        from argusai.core.infra.mcp_registry import search_mcp_marketplace

        hosted_filter: bool | None
        if isinstance(hosted, bool):
            hosted_filter = hosted
        else:
            normalized = str(hosted or "").strip().lower()
            hosted_filter = (
                True
                if normalized in {"1", "true", "hosted"}
                else False
                if normalized in {"0", "false", "local"}
                else None
            )
        try:
            return search_mcp_marketplace(
                source,
                query,
                cursor,
                limit,
                category,
                hosted_filter,
            )
        except Exception as exc:
            return {
                "ok": False,
                "source": str(source or ""),
                "error": f"MCP marketplace request failed: {exc}",
            }

    def get_mcp_marketplace_entry(
        self,
        source: str,
        server_id: str,
    ) -> dict[str, Any]:
        """Fetch one catalogue detail on demand for a user-requested import."""
        from argusai.core.infra.mcp_registry import get_mcp_marketplace_entry

        try:
            return get_mcp_marketplace_entry(source, server_id)
        except Exception as exc:
            return {
                "ok": False,
                "source": str(source or ""),
                "error": f"MCP marketplace detail request failed: {exc}",
            }

    def ensure_mcp_startup_probe(self) -> dict[str, Any]:
        """Ensure the once-per-process startup health probe has been started."""
        self.start_mcp_startup_probe()
        status = getattr(self, "_mcp_startup_probe_status", None)
        return {
            "ok": True,
            "startup_probe": dict(status) if isinstance(status, dict) else {"status": "starting"},
        }

    def probe_mcp_servers(self) -> dict[str, Any]:
        """Manual JS-callable: re-probe every enabled MCP server in background.

        Returns immediately after kicking the daemon thread. The frontend
        polls ``list_mcp_servers`` to observe ``last_probe`` updates.
        """
        self.start_mcp_startup_probe(force=True)
        return {"ok": True}

    def start_mcp_startup_probe(self, *, force: bool = False) -> None:
        """Kick a daemon thread that probes every enabled MCP server.

        Called once by :py:func:`argusai.gui.launch.main` right after the
        ``Api`` is wired to the window, and re-callable from JS via
        :py:meth:`probe_mcp_servers`. Idempotent within ~1 second using a
        flag on ``self`` so two probes never run concurrently.
        """
        import threading
        import time as _time

        existing_started = getattr(self, "_mcp_probe_started_at", 0.0)
        if not force and existing_started > 0:
            return
        thread = getattr(self, "_mcp_probe_thread", None)
        if thread is not None and thread.is_alive():
            return

        def _runner() -> None:
            import asyncio
            import logging
            from argusai.core.tools.case_mcp import probe_all_mcp_servers
            from argusai.core.workspace.data_root_migrate import repair_mcp_venv_launchers

            try:
                repair_result: dict[str, Any] = {}

                def _repair(settings):
                    repairs, updated = repair_mcp_venv_launchers(settings)
                    repair_result["repairs"] = repairs
                    return updated

                update = getattr(self.settings_mgr, "update", None)
                if callable(update):
                    update(_repair)
                else:
                    updated = _repair(self.settings_mgr.get())
                    if repair_result["repairs"]:
                        self.settings_mgr.save(updated)
                repairs = repair_result["repairs"]
                if repairs:
                    logging.getLogger("argusai.gui").info(
                        "mcp.startup_probe repaired %d moved virtualenv launcher(s)",
                        len(repairs),
                    )
                async def _probe() -> dict[str, Any]:
                    result = await probe_all_mcp_servers(self)
                    # Give Windows subprocess pipe callbacks one final event-loop
                    # turn before ``asyncio.run`` closes the Proactor loop.
                    await asyncio.sleep(0.05)
                    return result

                summary = asyncio.run(_probe())
                setattr(self, "_mcp_startup_probe_status", {
                    "status": "done",
                    "started_at": getattr(self, "_mcp_probe_started_at", _time.time()),
                    "finished_at": _time.time(),
                    "probed": int(summary.get("probed") or 0),
                    "alive": int(summary.get("alive") or 0),
                    "dead": int(summary.get("dead") or 0),
                })
                logging.getLogger("argusai.gui").info(
                    "mcp.startup_probe completed probed=%d alive=%d dead=%d",
                    int(summary.get("probed") or 0),
                    int(summary.get("alive") or 0),
                    int(summary.get("dead") or 0),
                )
            except Exception as exc:
                setattr(self, "_mcp_startup_probe_status", {
                    "status": "failed",
                    "started_at": getattr(self, "_mcp_probe_started_at", _time.time()),
                    "finished_at": _time.time(),
                    "error": str(exc)[:400],
                })
                logging.getLogger("argusai.gui").warning("mcp.startup_probe failed: %s", exc)

        t = threading.Thread(target=_runner, name="mcp-startup-probe", daemon=True)
        started_at = _time.time()
        try:
            setattr(self, "_mcp_probe_thread", t)
            setattr(self, "_mcp_probe_started_at", started_at)
            setattr(self, "_mcp_startup_probe_status", {
                "status": "running",
                "started_at": started_at,
            })
        except Exception:
            pass
        t.start()

    def save_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        from argusai.core.settings import normalize_mcp_servers

        original_args = list(payload.get("args") or []) if isinstance(payload.get("args"), list) else []
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        marketplace_prepared = False
        if str(provenance.get("source") or "").strip().lower() == "modelscope":
            from argusai.core.infra.mcp_registry import prepare_marketplace_config

            try:
                payload = prepare_marketplace_config(payload)
            except ValueError as exc:
                return {
                    "ok": False,
                    "code": "marketplace_config_unsafe",
                    "error": str(exc),
                }
            marketplace_prepared = list(payload.get("args") or []) != original_args

        candidate = normalize_mcp_servers([payload])
        if not candidate:
            return {"ok": False, "error": "command is required"}
        server = candidate[0]
        result: dict[str, Any] = {}

        def _update(settings: AppSettings) -> AppSettings:
            servers = normalize_mcp_servers(settings.mcp_servers)
            for idx, item in enumerate(servers):
                if item["id"] == server["id"]:
                    servers[idx] = server
                    break
            else:
                servers.append(server)
            settings.mcp_servers = servers
            result["servers"] = servers
            return settings

        from argusai.core.settings import AppSettings

        self.settings_mgr.update(_update)
        servers = result["servers"]
        return {
            "ok": True,
            "server": server,
            "servers": servers,
            "marketplace_prepared": marketplace_prepared,
        }

    def delete_mcp_server(self, server_id: str) -> dict[str, Any]:
        from argusai.core.settings import normalize_mcp_servers

        sid = str(server_id or "").strip()
        if not sid:
            return {"ok": False, "error": "server_id is required"}
        result: dict[str, Any] = {}

        def _update(settings):
            servers = normalize_mcp_servers(settings.mcp_servers)
            next_servers = [item for item in servers if item.get("id") != sid]
            if len(next_servers) == len(servers):
                raise ValueError(f"MCP server not found: {sid}")
            settings.mcp_servers = next_servers
            result["servers"] = next_servers
            return settings

        try:
            self.settings_mgr.update(_update)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        next_servers = result["servers"]
        return {"ok": True, "servers": next_servers}

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        import asyncio
        import time
        from argusai.core.infra.mcp_client import (
            classify_exception,
            make_mcp_client,
            mcp_error_payload,
            mcp_runtime_for_stdio,
            spec_from_server_dict,
        )
        from argusai.core.settings import normalize_mcp_servers

        sid = str(server_id or "").strip()
        server = next((item for item in normalize_mcp_servers(self.settings_mgr.get().mcp_servers) if item.get("id") == sid), None)
        if not server:
            return {"ok": False, "status": "missing", "error": f"MCP server not found: {sid}"}

        from argusai.core.paths import PathManager

        runtime_tools_dir = PathManager(data_root=self.settings_mgr.get().data_root).tools_dir
        spec = spec_from_server_dict(server, runtime_tools_dir=str(runtime_tools_dir))

        from argusai.core.tools.case_mcp import (
            _mark_mcp_last_probe,
            _update_mcp_cache,
        )

        async def _run() -> dict[str, Any]:
            started = time.perf_counter()
            runtime_info = mcp_runtime_for_stdio(spec) if spec.transport == "stdio" else {"transport": spec.transport}
            _mark_mcp_last_probe(self, sid, status="pending")
            try:
                async with make_mcp_client(spec) as client:
                    health = client.health()
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                info = health.get("server_info") if isinstance(health.get("server_info"), dict) else {}
                _update_mcp_cache(self, sid, {
                    "server_info": info,
                    "protocol_version": health.get("protocol_version") or "",
                    "capabilities": health.get("capabilities") if isinstance(health.get("capabilities"), dict) else {},
                    "transport": spec.transport,
                })
                _mark_mcp_last_probe(self, sid, status="alive", elapsed_ms=elapsed_ms)
                return {
                    "ok": True,
                    "status": "alive",
                    "server_id": sid,
                    "server": spec.safe_name,
                    "transport": spec.transport,
                    "elapsed_ms": elapsed_ms,
                    "runtime": health.get("runtime") or runtime_info,
                    **{k: v for k, v in health.items() if k != "runtime"},
                    "source": spec.source_block(),
                }
            except Exception as exc:
                code, retryable, fix = classify_exception(exc)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                _mark_mcp_last_probe(self, sid, status="dead", elapsed_ms=elapsed_ms, error_code=code, error=str(exc))
                return {
                    **mcp_error_payload(spec, error_code=code, error=str(exc), retryable=retryable, suggested_fix=fix),
                    "status": "dead",
                    "server_id": sid,
                    "server": spec.safe_name,
                    "transport": spec.transport,
                    "runtime": runtime_info,
                    "elapsed_ms": elapsed_ms,
                }

        try:
            return asyncio.run(_run())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_run())
            finally:
                loop.close()

    def list_mcp_server_tools(self, server_id: str) -> dict[str, Any]:
        """List one configured MCP server's tools for the settings UI."""
        import asyncio
        import concurrent.futures
        from argusai.core.tools.case_mcp import mcp_server_list_tools

        sid = str(server_id or "").strip()
        if not sid:
            return {"ok": False, "error": "server_id is required"}

        async def _run() -> dict[str, Any]:
            return await mcp_server_list_tools(
                self,
                server_id=sid,
                limit=100,
                include_schema=True,
            )

        try:
            asyncio.get_running_loop()
            nested = True
        except RuntimeError:
            nested = False
        try:
            if nested:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, _run()).result()
            return asyncio.run(_run())
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "tool_catalog_failed",
                "error": str(exc),
            }

    def logging_status(self) -> dict[str, Any]:
        """Return diagnostics about the live logging configuration.

        Surfaced in the settings panel so users can confirm where logs
        are being written and how full the rotating files are without
        having to navigate the filesystem manually.
        """

        from argusai.core.infra.logging_setup import (
            current_logging_settings,
            pending_legacy_log_files,
        )

        log_dir = self._active_log_dir()
        files = []
        try:
            for entry in sorted(log_dir.glob("*.log*")):
                try:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size": int(stat.st_size),
                        "modified": float(stat.st_mtime),
                    })
                except OSError:
                    continue
        except OSError:
            files = []
        return {
            "ok": True,
            "log_dir": str(log_dir),
            "files": files,
            "settings": current_logging_settings().to_dict(),
            "legacy_files": pending_legacy_log_files(log_dir),
        }

    def _active_log_dir(self) -> Path:
        """The directory log records are actually written to right now.

        Normally that is whatever :func:`init_logging` bound the handlers to.
        When logging never initialised (a bare ``Api`` in tests, or a failed
        bootstrap) fall back to deriving it from the persisted settings so the
        panel and the "open log directory" action cannot drift apart.
        """
        from argusai.core.infra.logging_setup import current_log_dir, is_initialised, resolve_log_dir

        if is_initialised():
            return current_log_dir()
        try:
            settings = self.settings_mgr.get()
        except Exception:
            return current_log_dir()
        return resolve_log_dir(settings.logging, data_root=settings.data_root)

    def open_log_dir(self) -> dict[str, Any]:
        """Reveal the active log directory in the user's file manager."""

        log_dir = self._active_log_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"cannot create log directory: {exc}", "path": str(log_dir)}
        return {**self.open_folder(str(log_dir)), "path": str(log_dir)}
