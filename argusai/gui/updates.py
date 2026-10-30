"""Desktop update bridge; downloading never interrupts active work."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("argusai.gui")


class UpdatesMixin:
    def _update_protected_paths(self) -> tuple[Path, ...]:
        from argusai.core.paths import PathManager

        settings = self.settings_mgr.get()
        paths = PathManager(data_root=settings.data_root or None)
        protected = [paths.app_root, paths.data_root]
        for name in ("tasks_dir", "sessions_dir", "knowledge_base_dir", "reverse_jobs_dir"):
            value = getattr(settings, name, None)
            if value:
                protected.append(Path(value))
        for name in ("tasks", "kb", "reverse_jobs"):
            root = getattr(getattr(self, name, None), "root", None)
            if root:
                protected.append(Path(root))
        return tuple(Path(path).resolve() for path in protected)

    def _updates(self):
        from argusai import __version__
        from argusai.core.paths import PathManager
        from argusai.core.updater import UpdateManager

        with self._update_manager_lock:
            manager = getattr(self, "_update_manager_instance", None)
            if manager is None:
                settings = self.settings_mgr.get()
                paths = PathManager(data_root=settings.data_root or None)
                manager = UpdateManager(
                    cache_dir=Path(paths.app_root) / "cache" / "updates",
                    current_version=__version__,
                    protected_paths=self._update_protected_paths(),
                )
                self._update_manager_instance = manager
            return manager

    def check_for_updates(self, force: bool = False) -> dict[str, Any]:
        self._update_handoff_error = {}
        return self._updates().check(force=bool(force))

    def update_status(self) -> dict[str, Any]:
        state = self._updates().status()
        if getattr(self, "_update_handoff_pending", False):
            state["status"] = "installing"
        return {**state, **getattr(self, "_update_handoff_error", {})}

    def download_update(self) -> dict[str, Any]:
        self._update_handoff_error = {}
        return self._updates().start_download()

    def cancel_update_download(self) -> dict[str, Any]:
        return self._updates().cancel_download()

    def open_update_folder(self) -> dict[str, Any]:
        from argusai.core.infra.explorer import reveal_path

        folder = Path(self._updates().cache_dir)
        folder.mkdir(parents=True, exist_ok=True)
        return reveal_path(folder)

    def _update_busy_reasons(self) -> list[str]:
        """Use live runtime state, rather than stale persisted task statuses."""
        reasons: list[str] = []
        if getattr(self, "_assistant_running_sessions", None) or getattr(self, "_active_acp_turns", None):
            reasons.append("agent")
        for session in list(getattr(self, "_assistant_sessions", {}).values()):
            jobs = getattr(session, "_background_jobs", None)
            if jobs is not None and jobs.running_count():
                reasons.append("background_jobs")
            threads = getattr(session, "_agent_thread_manager", None)
            if threads is not None and threads.active_agent_count():
                reasons.append("agent_team")
        tasks = getattr(self, "tasks", None)
        if tasks is not None:
            for meta in tasks.list():
                task = tasks.get(meta["id"])
                if task is not None and task.is_running():
                    reasons.append("scan")
                    break
        proxy = getattr(self, "proxy", None)
        if proxy is not None and proxy.is_running:
            reasons.append("proxy")
        reverse = getattr(self, "reverse_jobs", None)
        if reverse is not None and any(row.get("active_runtime_sessions") for row in reverse.list()):
            reasons.append("reverse")
        if getattr(self, "_data_root_migration", {}).get("status") in {"running", "in_progress"}:
            reasons.append("data_migration")
        return list(dict.fromkeys(reasons))

    def install_update(self) -> dict[str, Any]:
        manager = self._updates()
        if not self._update_install_lock.acquire(blocking=False):
            return {**manager.status(), "ok": False, "error_code": "install_pending", "error": "An update is already being installed."}
        scheduled = False
        helper = None
        try:
            self._update_handoff_error = {}
            if not getattr(self, "_window", None):
                return {**manager.status(), "ok": False, "error_code": "no_window", "error": "Install updates from the desktop application."}
            reasons = self._update_busy_reasons()
            if reasons:
                return {**manager.status(), "ok": False, "error_code": "busy", "busy_reasons": reasons, "error": "Finish active work before installing this update."}
            # Settings/data migration may have changed since the startup check.
            manager.protected_paths = self._update_protected_paths()
            prepared = manager.prepare_install()
            if not prepared.get("ok"):
                return {**manager.status(), **prepared}
            # Staging an archive can take seconds; recheck before handing off.
            reasons = self._update_busy_reasons()
            if reasons:
                manager.installation_aborted()
                return {**manager.status(), "ok": False, "error_code": "busy", "busy_reasons": reasons, "error": "New work started. Finish it before installing this update."}
            from argusai.core.infra.runtime_exec import self_relaunch_env

            options: dict[str, Any] = {"cwd": prepared.get("cwd"), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": self_relaunch_env()}
            if sys.platform == "win32":
                options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            helper = subprocess.Popen(prepared["command"], **options)
            self._update_handoff_pending = True

            def finish() -> None:
                try:
                    reasons = self._update_busy_reasons()
                    if helper.poll() is not None:
                        raise RuntimeError("Update helper exited before the application closed.")
                    result = self.quit_application() if not reasons else {"ok": False}
                    if not result.get("ok"):
                        helper.terminate()
                        manager.installation_aborted()
                        self._update_handoff_error = {"ok": False, "error_code": "busy" if reasons else "install_failed", "error": "Application was kept open; retry installation after finishing active work."}
                except Exception as exc:
                    helper.terminate()
                    manager.installation_aborted(str(exc))
                    self._update_handoff_error = {"ok": False, "error_code": "install_failed", "error": str(exc)}
                    log.exception("Update handoff failed; application left running")
                finally:
                    self._update_handoff_pending = False
                    self._update_install_lock.release()

            timer = threading.Timer(0.35, finish)
            timer.daemon = True
            timer.start()
            scheduled = True
            return {**manager.status(), "ok": True, "status": "installing"}
        except Exception as exc:
            if helper is not None:
                helper.terminate()
            self._update_handoff_pending = False
            manager.installation_aborted(str(exc))
            return {**manager.status(), "ok": False, "error_code": "install_failed", "error": str(exc)}
        finally:
            if not scheduled:
                self._update_install_lock.release()
