"""Construction + minimal introspection for the GUI ``Api`` object.

This module owns:

* The ``_ApiBase.__init__`` that wires every core service the mixins later
  reach for (``settings_mgr``, ``tasks``, ``kb``, ``reverse_jobs``,
  ``proxy``, plus the assistant bookkeeping dicts).
* A few methods that don't fit any other domain (``list_checks``,
  ``app_info``, ``set_locale``) and are too small to deserve their own
  file.

The construction logic is intentionally tolerant: if a user-configured
``data_root`` subdirectory is unwritable (drive missing, perms), we fall
back to the OS default so the app still boots.

External contract: ``api.settings_mgr`` is touched by ``_tools.py``; do
not rename it. Other public method names are enumerated in
``webui/src/api.js``.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from threading import Lock
from typing import Any


log = logging.getLogger("argusai.gui")


class _ApiBase:
    """Construct + own every core service the mixins reach for.

    Subclasses must not re-implement ``__init__``; instead they layer
    pure-method mixins on top via multiple inheritance and trust the
    attributes installed here.
    """

    def __init__(self, *, host_kind: str = "gui") -> None:
        import argusai.checks  # noqa: F401 — register built-in checks

        from argusai.core.host import normalize_host_kind
        from argusai.core.settings import (
            SettingsManager,
            default_knowledge_base_dir,
            default_reverse_jobs_dir,
            default_tasks_dir,
        )
        from argusai.core.tasks import TaskManager

        self.host_kind = normalize_host_kind(host_kind, default="gui")
        self.settings_mgr = SettingsManager()
        snapshot = self.settings_mgr.get()

        tasks_root = self._init_writable_dir(
            raw=snapshot.tasks_dir,
            default=default_tasks_dir,
            label="tasks_dir",
        )
        self.tasks = TaskManager(root=tasks_root)

        from argusai.core.workspace.knowledge import KnowledgeBase
        kb_root = self._init_writable_dir(
            raw=snapshot.knowledge_base_dir,
            default=default_knowledge_base_dir,
            label="knowledge_base_dir",
        )
        try:
            self.kb = KnowledgeBase(root=kb_root)
        except Exception as e:
            log.warning(
                "knowledge_base_dir %r unusable (%s); falling back to %r",
                str(kb_root), e, default_knowledge_base_dir(),
            )
            self.kb = KnowledgeBase(root=Path(default_knowledge_base_dir()))
        self._purge_kb_trash_on_boot(snapshot)

        from argusai.core.reverse.reverse_jobs import ReverseJobStore
        rev_root = self._init_writable_dir(
            raw=snapshot.reverse_jobs_dir,
            default=default_reverse_jobs_dir,
            label="reverse_jobs_dir",
        )
        try:
            self.reverse_jobs = ReverseJobStore(root=rev_root)
        except Exception as e:
            log.warning(
                "reverse_jobs_dir %r unusable (%s); falling back to %r",
                str(rev_root), e, default_reverse_jobs_dir(),
            )
            self.reverse_jobs = ReverseJobStore(root=Path(default_reverse_jobs_dir()))

        from argusai.core.security.proxy import ProxyManager
        from argusai.core.paths import PathManager

        # Honor the configured data_root: a bare PathManager() defaults to
        # app_root/data, so the proxy CA + exports used to ignore a changed
        # data directory entirely (the migration already moves the proxy
        # subdir, so only this live binding was wrong).
        paths = PathManager(data_root=snapshot.data_root or None)
        paths.ensure_base_dirs()
        self.proxy = ProxyManager(ca_dir=paths.proxy_dir)
        self._init_proxy_decryption()

        from argusai.core.assistant import AssistantSession  # noqa: F401 — type hint only

        from argusai.core.channels.i18n import detect_system_locale

        self._locale: str = detect_system_locale()
        self._window: Any = None
        self._window_maximized: bool = False
        self._assistant_sessions: dict[str, AssistantSession] = {}
        self._assistant_streams: dict[str, dict[str, Any]] = {}
        from argusai.core.assistant.confirmation_broker import ConfirmationBroker
        from argusai.core.assistant.stream_hub import StreamHub
        from argusai.core.assistant.turn_router import AssistantTurnRouter
        from argusai.core.agent_runtimes import ArgusAIToolBroker

        self._assistant_stream_hub = StreamHub(self._assistant_streams)
        self._assistant_confirmation_broker = ConfirmationBroker()
        self._assistant_turn_router = AssistantTurnRouter()
        self._assistant_tool_broker = ArgusAIToolBroker()
        self._active_acp_turns: dict[str, Any] = {}
        self._assistant_run_lock = Lock()
        self._assistant_running_sessions: set[str] = set()
        self._attention_lock = Lock()
        self._app_attention_state: dict[str, Any] = {
            "focused": True,
            "visible": True,
            "open_session_ids": [],
            "reported": False,
            "updated_at": time.time(),
        }
        self._attention_notification_times: dict[str, float] = {}
        self._attention_notification_counts: dict[str, int] = {}
        self._data_root_migration_lock = Lock()
        self._data_root_migration: dict[str, Any] = {
            "status": "idle",
            "phase": "idle",
            "percent": 0,
            "updated_at": time.time(),
        }
        try:
            self._channels_runtime().refresh()
        except Exception:
            log.warning("failed to start configured communication channels", exc_info=True)

    def set_app_attention_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Receive typed focus/visibility state from the desktop WebView."""
        if not isinstance(state, dict):
            return {"ok": False, "error": "attention state must be an object"}
        raw_open_ids = state.get("open_session_ids")
        open_session_ids = (
            list(dict.fromkeys(
                str(item or "").strip()[:128]
                for item in raw_open_ids
                if str(item or "").strip()
            ))[:8]
            if isinstance(raw_open_ids, list)
            else None
        )
        with self._attention_lock:
            previous_needs_attention = self._app_needs_attention_locked()
            if open_session_ids is None:
                open_session_ids = list(
                    self._app_attention_state.get("open_session_ids") or []
                )
            self._app_attention_state = {
                "focused": bool(state.get("focused", True)),
                "visible": bool(state.get("visible", True)),
                "assistant_visible": bool(state.get("assistant_visible", False)),
                "active_session_id": str(state.get("active_session_id") or "")[:128],
                "open_session_ids": open_session_ids,
                "reported": True,
                "updated_at": time.time(),
            }
            needs_attention = self._app_needs_attention_locked()
        if needs_attention and not previous_needs_attention:
            self._notify_existing_attention_items()
        return {"ok": True, "needs_attention": needs_attention}

    def _app_needs_attention_locked(self) -> bool:
        state = self._app_attention_state
        return bool(
            state.get("reported")
            and (not state.get("focused") or not state.get("visible"))
        )

    def _app_needs_attention(self) -> bool:
        with self._attention_lock:
            return self._app_needs_attention_locked()

    def notify_attention(
        self,
        event_key: str,
        *,
        title: str,
        message: str,
        level: str = "info",
        cooldown_seconds: float = 30.0,
        require_background: bool = True,
        max_notifications: int = 0,
    ) -> bool:
        """HostCallbacks alias for :meth:`_notify_attention`."""
        return self._notify_attention(
            event_key,
            title=title,
            message=message,
            level=level,
            cooldown_seconds=cooldown_seconds,
            require_background=require_background,
            max_notifications=max_notifications,
        )

    def _notify_attention(
        self,
        event_key: str,
        *,
        title: str,
        message: str,
        level: str = "info",
        cooldown_seconds: float = 30.0,
        require_background: bool = True,
        max_notifications: int = 0,
    ) -> bool:
        """Schedule a deduplicated OS notification for a runtime event.

        ``max_notifications`` caps how often one ``event_key`` may ever be
        announced (0 means no cap). Callers that re-announce a state which
        persists until the user acts need it: the cooldown alone only spaces the
        repeats out, so an ignored question would keep arriving forever.
        """
        from argusai.core.host import host_supports_webview

        if not host_supports_webview(getattr(self, "host_kind", "gui")):
            log.debug(
                "attention skipped on %s host: %s — %s",
                getattr(self, "host_kind", "?"),
                title,
                message,
            )
            return False
        if require_background and not self._app_needs_attention():
            return False
        key = str(event_key or "").strip()
        if not key:
            return False
        now = time.time()
        cap = max(0, int(max_notifications or 0))
        with self._attention_lock:
            previous = float(self._attention_notification_times.get(key) or 0)
            if now - previous < max(1.0, float(cooldown_seconds or 0)):
                return False
            sent = int(self._attention_notification_counts.get(key) or 0)
            if cap and sent >= cap:
                return False
            self._attention_notification_times[key] = now
            self._attention_notification_counts[key] = sent + 1
            if len(self._attention_notification_times) > 500:
                oldest = sorted(
                    self._attention_notification_times,
                    key=self._attention_notification_times.get,
                )[:100]
                for stale_key in oldest:
                    self._attention_notification_times.pop(stale_key, None)
                    self._attention_notification_counts.pop(stale_key, None)

        def _forget() -> None:
            with self._attention_lock:
                self._attention_notification_times.pop(key, None)
                remaining = int(self._attention_notification_counts.get(key) or 0) - 1
                if remaining > 0:
                    self._attention_notification_counts[key] = remaining
                else:
                    self._attention_notification_counts.pop(key, None)

        def _send() -> None:
            try:
                from argusai.core.tools.desktop_automation import desktop_notify

                result = desktop_notify(self, title=title, message=message, level=level)
                if not result.get("sent"):
                    # An undelivered notification must not consume the budget.
                    _forget()
                    log.warning(
                        "attention notification was not delivered: %s",
                        result.get("error") or result.get("reason") or "unknown error",
                    )
            except Exception:
                _forget()
                log.debug("attention notification failed", exc_info=True)

        threading.Thread(target=_send, daemon=True, name="argus-attention-notify").start()
        return True

    def _notify_existing_attention_items(self) -> None:
        """Notify when the app loses focus while a decision is already pending.

        Every item here is a question that stays pending until the user answers
        it, and this runs on each transition into the background. Announcing one
        more than once would turn a conversation the user has set aside into a
        permanent reminder, so each is capped at a single notification.
        """
        from argusai.core.assistant.streaming import EXTERNAL_AGENT_TOOL

        with self._attention_lock:
            open_session_ids = {
                str(item)
                for item in self._app_attention_state.get("open_session_ids") or []
                if str(item)
            }
        for session_id, session in list(self._assistant_sessions.items()):
            # Closing a conversation tab means the user has deliberately set
            # that conversation aside. Its worker may continue, but it no
            # longer gets to summon the user through a desktop notification.
            if session_id not in open_session_ids:
                continue
            pending = getattr(session, "_pending", None)
            if isinstance(pending, dict):
                items = pending.get("confirmations")
                names = [
                    "本地 Agent 的工具"
                    if str(item.get("name") or "") == EXTERNAL_AGENT_TOOL
                    else str(item.get("name") or "")
                    for item in (items if isinstance(items, list) else [])
                    if isinstance(item, dict)
                ]
                self._notify_attention(
                    f"assistant-pending:{session_id}:{pending.get('pending_id') or 'active'}",
                    title="ArgusAI 等待工具确认",
                    message=f"{'、'.join(name for name in names if name) or '工具操作'}等待你的授权。",
                    level="warning",
                    max_notifications=1,
                )
            elif bool(getattr(session, "_user_decision_requested", False)):
                self._notify_attention(
                    f"assistant-decision:{session_id}",
                    title="ArgusAI 等待你的决定",
                    message="主 Agent 已暂停，返回 ArgusAI 继续处理。",
                    level="warning",
                    max_notifications=1,
                )
            manager = getattr(session, "_agent_thread_manager", None)
            if manager is None:
                continue
            try:
                agents = manager.list(include_closed=False).get("agents") or []
            except Exception:
                continue
            for agent in agents:
                pending_projection = agent.get("pending_confirmation")
                tools = (
                    pending_projection.get("pending_tools")
                    if isinstance(pending_projection, dict)
                    else []
                )
                if not tools:
                    continue
                self._notify_attention(
                    f"child-pending:{agent.get('agent_id')}:{tools[0].get('pending_id')}",
                    title="子 Agent 等待工具确认",
                    message=f"{agent.get('role') or '子 Agent'} 请求执行 {tools[0].get('name') or '工具操作'}。",
                    level="warning",
                    max_notifications=1,
                )

    def _init_proxy_decryption(self) -> None:
        """Hand saved decryption rules and the managed Node path to the proxy."""
        decryptor = self.proxy.decryptor

        def _node() -> str:
            from argusai.core.reverse.runtime_manager import node_bin

            return node_bin(self)

        decryptor.script_runner.node_resolver = _node
        try:
            self.proxy.set_decrypt_rules(list(self.settings_mgr.get().proxy_decrypt_rules or []))
        except Exception:
            log.debug("loading proxy decrypt rules failed", exc_info=True)
        try:
            self.proxy.set_rewrite_rules(list(self.settings_mgr.get().proxy_rewrite_rules or []))
        except Exception:
            log.debug("loading proxy rewrite rules failed", exc_info=True)

    def shutdown_background_services(self) -> None:
        """Best-effort fast cleanup when the GUI window closes."""
        if getattr(self, "_background_shutdown_started", False):
            return
        self._background_shutdown_started = True
        try:
            for turn in list(getattr(self, "_active_acp_turns", {}).values()):
                try:
                    turn.request_cancel()
                except Exception:
                    pass
            getattr(self, "_active_acp_turns", {}).clear()
            broker = getattr(self, "_assistant_tool_broker", None)
            if broker is not None:
                broker.shutdown()
            cancel_qq_logins = getattr(self, "_cancel_all_qq_logins", None)
            if callable(cancel_qq_logins):
                cancel_qq_logins()
            channel_runtime = getattr(self, "_channel_runtime_instance", None)
            if channel_runtime is not None:
                channel_runtime.shutdown()
            decryptor = getattr(getattr(self, "proxy", None), "decryptor", None)
            if decryptor is not None:
                decryptor.close()
            # One shared grace period for every session's agent threads, so a
            # window with many open conversations closes as fast as one.
            agent_deadline = time.monotonic() + 1.0
            for session in list(self._assistant_sessions.values()):
                try:
                    manager = getattr(session, "_agent_thread_manager", None)
                    if manager is not None:
                        manager.shutdown(
                            wait_timeout=max(0.0, agent_deadline - time.monotonic()),
                        )
                    background_jobs = getattr(session, "_background_jobs", None)
                    if background_jobs is not None:
                        background_jobs.shutdown()
                    session.cancel()
                except Exception:
                    pass
            with self._assistant_run_lock:
                self._assistant_running_sessions.clear()
            self._assistant_streams.clear()
        except Exception:
            pass
        try:
            proxy = getattr(self, "proxy", None)
            if proxy is not None and proxy.needs_shutdown():
                proxy.stop_proxy(force=True)
        except Exception:
            pass
        try:
            from argusai.core.browser.browser_pool import shutdown_all_browser_hubs

            shutdown_all_browser_hubs(timeout=0.8)
        except Exception:
            pass
        try:
            # Headless browsers we launched for attach have no window, so
            # leaving them after exit would be invisible orphans. Kill those
            # we own; never touch the user's own / headful windows.
            from argusai.core.browser.browser_attach import terminate_owned_launched_browsers

            terminate_owned_launched_browsers(headless_only=True)
        except Exception:
            pass
        try:
            from argusai.core.reverse.js_runtime import runtime_stop_all

            runtime_stop_all()
        except Exception:
            pass
        try:
            from argusai.core.assistant.agent_threads import shutdown_agent_executor
            from argusai.core.assistant.background_jobs import shutdown_background_executor

            shutdown_agent_executor()
            shutdown_background_executor()
        except Exception:
            pass
        try:
            from argusai.core.assistant.cron import get_cron_scheduler

            scheduler = get_cron_scheduler()
            if scheduler is not None:
                scheduler.stop()
        except Exception:
            pass

    @staticmethod
    def _init_writable_dir(raw: str | None, default: Any, label: str) -> Path:
        """Resolve ``raw`` into a writable directory, falling back to ``default()``."""
        raw_str = (raw or default()).strip() if isinstance(raw, str) else default()
        root = Path(raw_str or default())
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(
                "Configured %s %r is not usable (%s); falling back to %r",
                label, str(root), e, default(),
            )
            root = Path(default())
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _purge_kb_trash_on_boot(self, snapshot: Any) -> None:
        """Best-effort startup purge of trashed KB entries beyond retention."""
        try:
            retention = int(getattr(snapshot, "kb_trash_retention_days", 30) or 0)
            if retention <= 0:
                return
            res = self.kb.purge_trash(older_than_days=retention)
            if res.get("removed"):
                log.info(
                    "KB startup purge: %d entry/entries (%.1f KB) older than %d days removed; %d kept.",
                    len(res["removed"]), res.get("freed_bytes", 0) / 1024.0, retention, res.get("kept", 0),
                )
        except Exception as e:
            log.warning("KB startup purge failed: %s", e)

    def list_checks(self) -> list[dict[str, Any]]:
        import argusai.checks  # noqa: F401 - populate Registry before listing
        from argusai.core.check import Registry

        return [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "needs_browser": c.needs_browser,
            }
            for c in Registry.all()
        ]

    def app_info(self) -> dict[str, Any]:
        from argusai import __version__

        # Windows draws the whole titlebar (frameless). macOS keeps the native
        # frame but, when the transparent-titlebar styling applied, draws a
        # custom header inset past the traffic lights. Both show the custom
        # header; only Windows owns the min/max/close + resize handles.
        macos_titlebar = getattr(self, "_macos_titlebar", None)
        frameless = sys.platform.startswith("win")
        custom_titlebar = frameless or bool(macos_titlebar)
        return {
            "version": __version__,
            "locale": self._locale,
            "cwd": str(Path.cwd()),
            "platform": sys.platform,
            "frameless_window": frameless,
            "custom_titlebar": custom_titlebar,
            "native_window_controls": custom_titlebar and not frameless,
            "titlebar_inset_left": int((macos_titlebar or {}).get("inset_left", 0)),
        }

    def get_app_snapshot(self) -> dict[str, Any]:
        """Return live UI context + global app state for the web shell / assistant."""
        from argusai.core.workspace.app_snapshot import build_app_snapshot

        return build_app_snapshot(self)

    def set_locale(self, locale: str) -> dict[str, Any]:
        self._locale = locale
        return {"ok": True, "locale": self._locale}
