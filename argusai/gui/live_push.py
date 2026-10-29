"""Backend -> frontend live push over the pywebview bridge.

The Vue UI reaches Python only through request/response RPC
(``window.pywebview.api``); pywebview has no built-in server->client push, so
panels historically had to poll or expose a manual Refresh button. This module
adds a one-way push: backend code calls :func:`push_ui(channel, payload)` and
the webview receives ``window.__argusPush({channel, payload})``, which the
frontend turns into a reactive event. Panels subscribe to their channel and
refresh on demand instead of polling.

Push is best-effort and fire-and-forget:

* No window bound (CLI / tests / headless) or ``evaluate_js`` raises → silent
  no-op. The frontend keeps a slow fallback poll, so a missed push never leaves
  a panel permanently stale.
* Emissions run on a single daemon worker (not the caller's thread and not the
  GUI thread), so a high-frequency source (proxy capture) never blocks callers.
  The queue is bounded and coalesces by dropping the oldest when full.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any

log = logging.getLogger("argusai.live_push")

_MAX_QUEUE = 2000


class LivePush:
    def __init__(self) -> None:
        self._window: Any = None
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=_MAX_QUEUE)
        self._worker: threading.Thread | None = None

    def bind(self, window: Any) -> None:
        """Point the bus at the live pywebview window and start the worker."""
        with self._lock:
            self._window = window
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="argus-livepush", daemon=True)
                self._worker.start()

    def unbind(self) -> None:
        with self._lock:
            self._window = None

    def push(self, channel: str, payload: dict[str, Any] | None = None) -> None:
        ch = str(channel or "").strip()
        if not ch:
            return
        with self._lock:
            if self._window is None:
                return
        try:
            data = json.dumps({"channel": ch, "payload": payload or {}}, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            data = json.dumps({"channel": ch, "payload": {}})
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            # Drop the oldest so the newest state still gets through.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(data)
            except queue.Empty:
                pass

    def _run(self) -> None:
        while True:
            data = self._queue.get()
            with self._lock:
                window = self._window
            if window is None:
                continue
            try:
                window.evaluate_js(f"window.__argusPush && window.__argusPush({data})")
            except Exception:
                log.debug("live push failed", exc_info=True)


LIVE_PUSH = LivePush()


def push_ui(channel: str, payload: dict[str, Any] | None = None) -> None:
    """Best-effort notify the UI that ``channel`` state changed."""
    LIVE_PUSH.push(channel, payload)
