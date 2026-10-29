"""Interactive request breakpoints for the passive proxy.

Unlike the rule-based rewrite engine (which mutates matching traffic without a
human in the loop), a breakpoint *parks* a matching in-flight request and waits
for a human — or the AI agent — to edit it and either forward or drop it. This
is the "hold this request, let me change it, then send" workflow of Fiddler /
Charles / Burp.

Design constraints that keep it from wedging the browser:

* Opt-in. When disabled (default) ``pause_request`` returns immediately with
  ``forward`` and never parks anything.
* Bounded wait. A parked request auto-forwards after ``PAUSE_TIMEOUT_SECONDS``
  if nobody resolves it, so a forgotten breakpoint can't hang a tab forever.
* Cross-thread safe. The request coroutine awaits an ``asyncio.Event`` on the
  proxy loop; the GUI/agent resolves from another thread, which wakes it via
  ``loop.call_soon_threadsafe``.

Only request bodies already buffered as ``bytes`` are editable; streamed
uploads are forwarded without parking. Response-stage breakpoints are a
separate, later addition (they need whole-body buffering like body-rewrite).
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from typing import Any, Callable

PAUSE_TIMEOUT_SECONDS = 120.0
_MAX_BODY_PREVIEW = 100_000


def _decode_body(body: bytes | None) -> tuple[str, bool]:
    """Return (text, is_text). Binary bodies come back base64 with is_text=False."""
    if not body:
        return "", True
    try:
        return body[:_MAX_BODY_PREVIEW].decode("utf-8"), True
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii"), False


class _Paused:
    __slots__ = ("id", "snapshot", "event", "loop", "resolution", "created_at")

    def __init__(self, pause_id: str, snapshot: dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
        self.id = pause_id
        self.snapshot = snapshot
        self.event = asyncio.Event()
        self.loop = loop
        self.resolution: dict[str, Any] | None = None
        self.created_at = time.time()


class BreakpointEngine:
    """Owns breakpoint config + the registry of parked requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._url_contains = ""
        self._methods: set[str] = set()
        self._host_contains = ""
        self._paused: dict[str, _Paused] = {}
        self._seq = 0
        self._on_change: Callable[[], None] | None = None

    # ---- config -----------------------------------------------------

    def set_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        cfg = dict(config or {})
        with self._lock:
            self._enabled = bool(cfg.get("enabled"))
            self._url_contains = str(cfg.get("url_contains") or "").strip()
            self._host_contains = str(cfg.get("host_contains") or "").strip()
            methods = cfg.get("methods") or []
            if isinstance(methods, str):
                methods = [methods]
            self._methods = {str(m).strip().upper() for m in methods if str(m).strip()}
        self._notify()
        return self.get_config()

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "url_contains": self._url_contains,
                "host_contains": self._host_contains,
                "methods": sorted(self._methods),
            }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def _notify(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    # ---- matching ---------------------------------------------------

    def matches(self, method: str, url: str, host: str) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            if self._methods and str(method or "").upper() not in self._methods:
                return False
            if self._url_contains and self._url_contains.lower() not in str(url or "").lower():
                return False
            if self._host_contains and self._host_contains.lower() not in str(host or "").lower():
                return False
            return True

    # ---- resolve (called from the GUI / agent thread) ---------------

    def list_paused(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p.snapshot) for p in self._paused.values()]

    def resolve(self, pause_id: str, action: str = "forward", edits: dict[str, Any] | None = None) -> dict[str, Any]:
        act = str(action or "forward").strip().lower()
        if act not in ("forward", "drop"):
            act = "forward"
        with self._lock:
            entry = self._paused.get(str(pause_id))
            if entry is None:
                return {"ok": False, "error": "no such paused request (it may have timed out)"}
            entry.resolution = {"action": act, "edits": dict(edits or {})}
            loop, event = entry.loop, entry.event
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # Loop already closed; nothing is waiting anymore.
            pass
        self._notify()
        return {"ok": True, "id": str(pause_id), "action": act}

    def resolve_all(self, action: str = "forward") -> dict[str, Any]:
        with self._lock:
            ids = list(self._paused)
        for pid in ids:
            self.resolve(pid, action)
        return {"ok": True, "resolved": len(ids)}

    # ---- pause (awaited on the proxy loop) --------------------------

    async def pause_request(
        self,
        *,
        method: str,
        url: str,
        host: str,
        header_items: list[tuple[str, str]],
        body: bytes | None,
        is_https: bool = False,
    ) -> dict[str, Any]:
        """Park a matching request until resolved; return {action, edits}.

        Returns ``{"action": "forward"}`` immediately when breakpoints are off,
        the request does not match, or the body is not editable (streamed).
        """
        if not self.matches(method, url, host):
            return {"action": "forward"}
        loop = asyncio.get_running_loop()
        with self._lock:
            self._seq += 1
            pause_id = f"bp-{self._seq}"
            body_text, is_text = _decode_body(body)
            snapshot = {
                "id": pause_id,
                "method": str(method or ""),
                "url": str(url or ""),
                "host": str(host or ""),
                "is_https": bool(is_https),
                "headers": [[str(k), str(v)] for k, v in header_items],
                "body_text": body_text,
                "body_is_text": is_text,
                "created_at": time.time(),
            }
            entry = _Paused(pause_id, snapshot, loop)
            self._paused[pause_id] = entry
        self._notify()
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=PAUSE_TIMEOUT_SECONDS)
            resolution = entry.resolution or {"action": "forward"}
        except asyncio.TimeoutError:
            resolution = {"action": "forward", "timed_out": True}
        finally:
            with self._lock:
                self._paused.pop(pause_id, None)
        self._notify()
        return resolution


def apply_request_edits(
    edits: dict[str, Any],
    *,
    method: str,
    url: str,
    header_items: list[tuple[str, str]],
    body: bytes | None,
) -> tuple[str, str, list[tuple[str, str]], bytes | None]:
    """Overlay a breakpoint resolution's edits onto the request parts.

    ``edits`` may carry ``method``, ``url``, ``headers`` (list of [k, v]) and
    either ``body_text`` (utf-8) or ``body_b64`` (binary). Absent keys keep the
    original value.
    """
    new_method = str(edits.get("method") or method)
    new_url = str(edits.get("url") or url)
    new_headers = header_items
    if isinstance(edits.get("headers"), list):
        new_headers = [
            (str(pair[0]), str(pair[1]))
            for pair in edits["headers"]
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and str(pair[0]).strip()
        ]
    new_body = body
    if "body_b64" in edits and edits["body_b64"]:
        try:
            new_body = base64.b64decode(str(edits["body_b64"]))
        except (ValueError, TypeError):
            new_body = body
    elif "body_text" in edits and edits.get("body_text") is not None:
        new_body = str(edits["body_text"]).encode("utf-8")
    return new_method, new_url, new_headers, new_body
