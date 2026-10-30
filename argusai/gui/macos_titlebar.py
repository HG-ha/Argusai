"""macOS unified (transparent) titlebar styling for the pywebview window.

On Windows the window is frameless and the Vue side draws the whole titlebar.
macOS users expect the native traffic lights, so instead of going frameless we
keep the native window but make its titlebar transparent and let the web
content run full height underneath it (``fullSizeContentView``). The traffic
lights then float over the top-left of our custom header, which reserves an
inset for them.

Everything here is best-effort and fail-safe: if the native ``NSWindow`` can't
be reached (pywebview internals change, non-Cocoa backend, etc.) it returns
``None`` and the caller leaves the window as a plain native frame — exactly the
behaviour before this module existed, so there is never a double titlebar.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Any

log = logging.getLogger("argusai.gui.titlebar")

# AppKit constants (stable ABI values; avoids importing symbols that may be
# absent in older pyobjc).
_NS_FULL_SIZE_CONTENT_VIEW = 1 << 15  # NSWindowStyleMaskFullSizeContentView
_NS_TITLE_HIDDEN = 1                   # NSWindowTitleHidden
# Width reserved on the left of our header for the three traffic lights.
TRAFFIC_LIGHT_INSET = 78


def _native_window_from(value: Any) -> Any:
    """Return an ``NSWindow`` from either the window or its BrowserView host."""
    if value is not None and hasattr(value, "setStyleMask_"):
        return value
    for attr in ("window", "_window"):
        candidate = getattr(value, attr, None)
        if candidate is not None and hasattr(candidate, "setStyleMask_"):
            return candidate
    return None


def _resolve_nswindow(window: Any, *, wait_seconds: float = 0.0) -> Any:
    """Best-effort: find the Cocoa ``NSWindow`` behind a pywebview window.

    Packaged pywebview starts the user callback on a helper thread while the
    Cocoa ``BrowserView`` may still be registering itself.  Source/dev runs
    usually hit the callback on the main thread after registration, which hid
    this race.  A helper thread may therefore wait briefly; the AppKit main
    thread must never be slept while it is constructing the window.
    """
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        candidate = _native_window_from(getattr(window, "native", None))
        if candidate is not None:
            return candidate
        try:
            from webview.platforms import cocoa  # type: ignore

            uid = getattr(window, "uid", None)
            inst = None
            getter = getattr(cocoa.BrowserView, "get_instance", None)
            if callable(getter) and uid is not None:
                inst = getter("uid", uid)
            if inst is None:
                instances = getattr(cocoa.BrowserView, "instances", {}) or {}
                inst = instances.get(uid) or next(iter(instances.values()), None)
            candidate = _native_window_from(inst)
            if candidate is not None:
                return candidate
        except Exception:
            log.debug("cocoa NSWindow lookup failed", exc_info=True)
            return None
        if threading.current_thread() is threading.main_thread() or time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _apply_style_on_main_thread(nswin: Any, *, timeout_seconds: float = 3.0) -> bool:
    """Apply AppKit mutations on the Cocoa main thread and report completion."""
    def apply() -> None:
        nswin.setTitlebarAppearsTransparent_(True)
        nswin.setTitleVisibility_(_NS_TITLE_HIDDEN)
        nswin.setStyleMask_(nswin.styleMask() | _NS_FULL_SIZE_CONTENT_VIEW)
        # Dragging is handled by the web ``.titlebar-drag`` region, not the
        # whole background, so selection in the content still works.
        nswin.setMovableByWindowBackground_(False)

    if threading.current_thread() is threading.main_thread():
        apply()
        return True

    done = threading.Event()
    outcome = {"ok": False, "cancelled": False}

    def invoke() -> None:
        if outcome["cancelled"]:
            done.set()
            return
        try:
            apply()
            outcome["ok"] = True
        except Exception:
            log.debug("macos titlebar styling failed on main thread", exc_info=True)
        finally:
            done.set()

    try:
        from PyObjCTools import AppHelper  # type: ignore

        AppHelper.callAfter(invoke)
    except Exception:
        log.debug("macos titlebar main-thread dispatch failed", exc_info=True)
        return False
    if not done.wait(max(0.1, float(timeout_seconds or 0.0))):
        outcome["cancelled"] = True
        log.warning("macos titlebar: main-thread styling timed out")
        return False
    return bool(outcome["ok"])


def apply_unified_titlebar(window: Any) -> dict[str, Any] | None:
    """Make the macOS titlebar transparent + full-size-content.

    Returns ``{"inset_left": ...}`` when applied so the caller can tell the
    frontend to draw its custom header with that left inset; ``None`` otherwise.
    """
    if sys.platform != "darwin":
        return None
    wait_seconds = 4.0 if threading.current_thread() is not threading.main_thread() else 0.0
    nswin = _resolve_nswindow(window, wait_seconds=wait_seconds)
    if nswin is None:
        log.info("macos titlebar: native NSWindow not reachable; keeping native frame")
        return None
    try:
        if not _apply_style_on_main_thread(nswin):
            return None
    except Exception:
        log.debug("macos titlebar styling failed", exc_info=True)
        return None
    log.info("macos titlebar: unified transparent titlebar applied")
    return {"inset_left": TRAFFIC_LIGHT_INSET}
