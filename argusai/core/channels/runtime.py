"""Lifecycle manager for enabled communication adapters."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any

from .adapters import ChannelAdapter, create_channel_adapter
from .gateway import ChannelGateway
from .i18n import DEFAULT_LOCALE, channel_text, normalize_locale
from .models import (
    ChannelConfig,
    ChannelDeliveryError,
    ChannelEvent,
    ChannelReply,
    infer_chat_kind,
)
from .store import ChannelStore

log = logging.getLogger(__name__)


class ChannelRuntimeManager:
    def __init__(
        self,
        store: ChannelStore,
        gateway: ChannelGateway,
        *,
        outbound_delivered_handler: Any | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.outbound_delivered_handler = outbound_delivered_handler
        self._adapters: dict[str, ChannelAdapter] = {}
        self._fingerprints: dict[str, str] = {}
        self._startup_errors: dict[str, dict[str, Any]] = {}
        self._event_locks: dict[tuple[str, str, str, str], Lock] = {}
        self._rate_windows: dict[tuple[str, str, str, str], deque[float]] = {}
        self._host_lock_file: Any = None
        self._lock = RLock()
        self._outbox_stop = Event()
        self._outbox_wake = Event()
        self._outbox_thread: Thread | None = None
        self._watchdog_stop = Event()
        self._watchdog_thread: Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="argusai-channel",
        )

    def refresh(self) -> dict[str, Any]:
        configs = {
            row["id"]: (
                self.store.get_channel(row["id"]),
                self.store.credentials(row["id"]),
            )
            for row in self.store.list_channels(public=False)
        }
        enabled_configs = [
            config
            for config, _credentials in configs.values()
            if config.enabled
        ]
        host_error = self._ensure_host_lock() if enabled_configs else ""
        with self._lock:
            for channel_id in list(self._adapters):
                current = configs.get(channel_id)
                config, credentials = current if current is not None else (None, {})
                fingerprint = (
                    self._fingerprint(config, credentials)
                    if config is not None
                    else None
                )
                if (
                    config is None
                    or not config.enabled
                    or fingerprint != self._fingerprints.get(channel_id)
                ):
                    adapter = self._adapters.pop(channel_id)
                    try:
                        adapter.stop()
                    except Exception:
                        log.exception("channel adapter stop failed channel_id=%s", channel_id)
                    self._fingerprints.pop(channel_id, None)
                    self._startup_errors.pop(channel_id, None)
            known_ids = set(configs)
            for channel_id in set(self._startup_errors) - known_ids:
                self._startup_errors.pop(channel_id, None)
            for channel_id, (config, credentials) in configs.items():
                if not config.enabled:
                    self._startup_errors.pop(channel_id, None)
                    continue
                if channel_id in self._adapters:
                    continue
                if host_error:
                    # Another ArgusAI process already hosts this data root.
                    # That is not a channel failure — CLI/GUI observers must
                    # not paint Status=err while ``serve`` is healthy.
                    self._startup_errors[channel_id] = {
                        "channel_id": channel_id,
                        "provider": config.provider,
                        "connected": False,
                        "hosted_elsewhere": True,
                        "host_pid": self._host_lock_pid(),
                        "last_error": host_error,
                    }
                    continue
                try:
                    adapter = create_channel_adapter(config, credentials)
                    adapter.start(self._on_event)
                    self._adapters[channel_id] = adapter
                    self._fingerprints[channel_id] = self._fingerprint(config, credentials)
                    self._startup_errors.pop(channel_id, None)
                except Exception as exc:
                    self._startup_errors[channel_id] = {
                        "channel_id": channel_id,
                        "provider": config.provider,
                        "connected": False,
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                    log.exception("channel adapter start failed channel_id=%s", channel_id)
        if not enabled_configs or not self._adapters:
            # Nothing is actually hosted (disabled, or every adapter failed to
            # start). Keeping the lock would block the OS service from taking
            # over for a process that hosts no channel at all.
            self._stop_outbox_worker()
            self._release_host_lock()
        elif not host_error:
            self._ensure_outbox_worker()
        if enabled_configs:
            # Boot / logon often races DNS and the NIC. Retry failed starts and
            # revive dead poll threads so WeChat iLink does not sit on
            # "OpenClaw cannot connect" until someone opens the GUI.
            self._ensure_watchdog()
        else:
            self._stop_watchdog()
        return {
            "ok": True,
            "active": len(self._adapters),
            "channels": self.statuses(),
        }

    def _host_lock_path(self) -> Path:
        root_key = hashlib.sha256(
            str(self.store.root.expanduser().resolve()).encode("utf-8")
        ).hexdigest()[:20]
        return Path(tempfile.gettempdir()) / f"argusai-channels-{root_key}.lock"

    def _host_lock_pid(self) -> int | None:
        """Best-effort PID written by the process that holds the host lock."""
        path = self._host_lock_path()
        try:
            raw = path.read_text(encoding="ascii", errors="ignore").strip()
            pid = int(raw)
            return pid if pid > 0 else None
        except (OSError, ValueError):
            return None

    def _ensure_host_lock(self) -> str:
        if self._host_lock_file is not None:
            return ""
        path = self._host_lock_path()
        handle = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
            self._host_lock_file = handle
            return ""
        except OSError:
            handle.close()
            holder = self._host_lock_pid()
            if holder is not None:
                return (
                    "Channels are already hosted by another ArgusAI process "
                    f"(pid {holder}) for this data root."
                )
            return (
                "Channels are already hosted by another ArgusAI process "
                "for this data root."
            )

    def _release_host_lock(self) -> None:
        handle = self._host_lock_file
        self._host_lock_file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    @staticmethod
    def _fingerprint(config: ChannelConfig, credentials: dict[str, str]) -> str:
        raw = json.dumps(
            {
                "config": config.to_dict(),
                "credentials": sorted(credentials.items()),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _locale(self) -> str:
        resolver = getattr(self.gateway, "locale", None)
        if not callable(resolver):
            return DEFAULT_LOCALE
        try:
            return normalize_locale(resolver())
        except Exception:
            return DEFAULT_LOCALE

    def _on_event(self, event: ChannelEvent) -> None:
        self._executor.submit(self._handle_event, event)

    def _handle_event(self, event: ChannelEvent) -> None:
        try:
            if infer_chat_kind(
                event.provider,
                chat_id=event.external_chat_id,
                raw=event.raw,
                chat_kind=event.chat_kind,
            ) == "group":
                return
            key = (
                event.channel_id,
                event.external_user_id,
                event.external_chat_id,
                event.external_thread_id,
            )
            if not self.store.claim_event(event):
                return
            if not self._within_rate_limit(event, key):
                locale = self._locale()
                reply = ChannelReply(
                    text=channel_text("rate_limited", locale),
                    external_chat_id=event.external_chat_id,
                    external_thread_id=event.external_thread_id,
                    reply_to_message_id=event.message_id,
                    locale=locale,
                )
            else:
                with self._lock:
                    event_lock = self._event_locks.setdefault(key, Lock())
                # Only a chat message can occupy this thread for minutes. Commands
                # and card callbacks must never queue behind one, or /cancel and
                # /btw would be unreachable exactly when they are needed most.
                if self.gateway.starts_turn(event):
                    with event_lock:
                        reply = self.gateway.handle(event)
                else:
                    reply = self.gateway.handle(event)
            with self._lock:
                adapter = self._adapters.get(event.channel_id)
            if adapter is not None and (
                getattr(reply, "text", "")
                or getattr(reply, "blocks", ())
                or getattr(reply, "attachments", ())
            ):
                adapter.send(reply)
        except Exception:
            log.exception("channel event handling failed channel_id=%s", event.channel_id)

    def _within_rate_limit(
        self,
        event: ChannelEvent,
        key: tuple[str, str, str, str],
    ) -> bool:
        config = self.store.get_channel(event.channel_id)
        try:
            limit = int(config.settings.get("rate_limit_per_minute") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 120))
        now = time.monotonic()
        cutoff = now - 60
        with self._lock:
            window = self._rate_windows.setdefault(key, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            statuses = [adapter.status() for adapter in self._adapters.values()]
            statuses.extend(self._startup_errors.values())
        statuses.sort(key=lambda item: str(item.get("channel_id") or ""))
        return statuses

    def enqueue_outbound(
        self,
        *,
        identity_id: str,
        session_id: str,
        text: str = "",
        attachments: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        blocks: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        allow_cross_session: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue a proactive delivery for the active GUI or headless host."""
        job = self.store.enqueue_outbound(
            identity_id=identity_id,
            session_id=session_id,
            text=text,
            attachments=attachments,
            blocks=blocks,
            allow_cross_session=allow_cross_session,
            meta=meta,
        )
        self._outbox_wake.set()
        return job

    def _ensure_watchdog(self) -> None:
        thread = self._watchdog_thread
        if thread is not None and thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="argusai-channel-watchdog",
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self, *, fast: bool = False) -> None:
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not current_thread()
        ):
            thread.join(timeout=0.3 if fast else 2)
        self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        delay = 5.0
        while not self._watchdog_stop.wait(delay):
            try:
                self._watchdog_tick()
            except Exception:
                log.exception("channel watchdog tick failed")
            delay = 30.0

    def _watchdog_tick(self) -> None:
        """Retry failed starts and revive adapters whose worker thread died."""
        self._restart_dead_workers()
        self.refresh()

    def _restart_dead_workers(self) -> None:
        with self._lock:
            adapters = list(self._adapters.items())
        for channel_id, adapter in adapters:
            alive_fn = getattr(adapter, "worker_alive", None)
            if callable(alive_fn):
                alive = bool(alive_fn())
            else:
                thread = getattr(adapter, "_thread", None)
                if thread is None:
                    continue
                alive = bool(getattr(thread, "is_alive", lambda: True)())
            if alive:
                continue
            try:
                adapter.start(self._on_event)
                log.warning("restarted dead channel adapter channel_id=%s", channel_id)
            except Exception:
                log.exception("channel adapter restart failed channel_id=%s", channel_id)

    def _ensure_outbox_worker(self) -> None:
        thread = self._outbox_thread
        if thread is not None and thread.is_alive():
            return
        self._outbox_stop.clear()
        self._outbox_wake.clear()
        self._outbox_thread = Thread(
            target=self._outbox_loop,
            daemon=True,
            name="argusai-channel-outbox",
        )
        self._outbox_thread.start()

    def _stop_outbox_worker(self, *, fast: bool = False) -> None:
        self._outbox_stop.set()
        self._outbox_wake.set()
        thread = self._outbox_thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not current_thread()
        ):
            thread.join(timeout=0.3 if fast else 2)
        self._outbox_thread = None

    def _outbox_loop(self) -> None:
        while not self._outbox_stop.is_set():
            delivered = self.drain_outbox_once()
            if delivered:
                continue
            self._outbox_wake.wait(1.0)
            self._outbox_wake.clear()

    @staticmethod
    def _outbound_parts(job: dict[str, Any]) -> list[dict[str, Any]]:
        """Split one delivery into independently retryable provider calls."""
        parts: list[dict[str, Any]] = []
        text = str(job.get("text") or "")
        blocks = [
            dict(item) for item in list(job.get("blocks") or [])
            if isinstance(item, dict)
        ]
        if text.strip() or blocks:
            parts.append({"text": text, "blocks": blocks, "attachments": []})
        for attachment in list(job.get("attachments") or []):
            if isinstance(attachment, dict):
                parts.append({"text": "", "blocks": [], "attachments": [dict(attachment)]})
        return parts

    def drain_outbox_once(self) -> int:
        """Deliver one leased batch; exposed for deterministic tests."""
        claimed = self.store.claim_outbound(limit=8)
        for job in claimed:
            error = ""
            permanent = False
            job_id = str(job.get("id") or "")
            delivered = int(job.get("delivered_parts") or 0)
            try:
                identity = self.store.get_identity(str(job.get("identity_id") or ""))
                # A confirmed GUI turn may enqueue delivery to an identity whose
                # incoming chat remains bound to another conversation. The store
                # records that binding at enqueue time; require it to remain
                # unchanged instead of incorrectly comparing it with the sending
                # GUI session.
                expected_recipient_session = str(
                    job.get("recipient_session_id")
                    or job.get("session_id")
                    or ""
                )
                if identity.bound_session_id != expected_recipient_session:
                    raise ValueError("channel identity is bound to a different conversation")
                if "send" not in identity.permissions:
                    raise ValueError("channel identity does not allow outbound delivery")
                with self._lock:
                    adapter = self._adapters.get(identity.channel_id)
                if adapter is None:
                    raise RuntimeError("channel is not connected in the active host")
                for index, part in enumerate(self._outbound_parts(job)):
                    if index < delivered:
                        continue
                    adapter.send(ChannelReply(
                        text=str(part.get("text") or ""),
                        external_chat_id=identity.external_chat_id,
                        external_thread_id=identity.external_thread_id,
                        blocks=tuple(part.get("blocks") or ()),
                        locale=self._locale(),
                        attachments=tuple(part.get("attachments") or ()),
                    ))
                    delivered = index + 1
                    self.store.record_outbound_progress(job_id, delivered)
                handler = self.outbound_delivered_handler
                if callable(handler):
                    try:
                        handler(dict(job))
                    except Exception:
                        # Provider delivery already succeeded. A local history
                        # sync failure must not retry and duplicate the message.
                        log.exception(
                            "channel outbound history sync failed job_id=%s",
                            job_id,
                        )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                # Missing scopes and rebound identities fail the same way forever.
                permanent = bool(getattr(exc, "permanent", False)) or isinstance(exc, ValueError)
                log.warning(
                    "channel outbound delivery failed job_id=%s channel_id=%s error=%s",
                    job.get("id"),
                    job.get("channel_id"),
                    error,
                )
            finished = self.store.finish_outbound(
                job_id,
                error=error,
                permanent=permanent,
            )
            if str(finished.get("state") or "") == "failed":
                self._notify_delivery_failure(job, error)
        return len(claimed)

    def _notify_delivery_failure(self, job: dict[str, Any], error: str) -> None:
        """Tell the requester in-channel, so a dropped file is never silent."""
        try:
            identity = self.store.get_identity(str(job.get("identity_id") or ""))
            with self._lock:
                adapter = self._adapters.get(identity.channel_id)
            if adapter is None:
                return
            adapter.send(ChannelReply(
                text=channel_text(
                    "delivery_failed",
                    self._locale(),
                    reason=str(error or "")[:300],
                ),
                external_chat_id=identity.external_chat_id,
                external_thread_id=identity.external_thread_id,
                locale=self._locale(),
            ))
        except Exception:
            log.debug("channel delivery failure notice could not be sent", exc_info=True)

    def test_channel(self, channel_id: str) -> dict[str, Any]:
        config = self.store.get_channel(channel_id)
        self.refresh()
        adapter = create_channel_adapter(
            config,
            self.store.credentials(channel_id),
        )
        return adapter.test()

    def shutdown(self) -> None:
        self._stop_watchdog(fast=True)
        self._stop_outbox_worker(fast=True)
        with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
            self._fingerprints.clear()
            self._startup_errors.clear()
            self._event_locks.clear()
            self._rate_windows.clear()
        # Adapters are stopped in parallel on daemon threads: one unreachable
        # channel must not add its own timeout to the window close, and must
        # not keep the interpreter alive once the deadline passes.
        stoppers = [
            Thread(
                target=self._stop_adapter,
                args=(adapter,),
                daemon=True,
                name="argusai-channel-stop",
            )
            for adapter in adapters
        ]
        for stopper in stoppers:
            stopper.start()
        deadline = time.monotonic() + 1.5
        for stopper in stoppers:
            stopper.join(timeout=max(0.0, deadline - time.monotonic()))
        self._release_host_lock()
        self._executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _stop_adapter(adapter: ChannelAdapter) -> None:
        try:
            adapter.stop()
        except Exception:
            log.debug("channel adapter stop failed", exc_info=True)
