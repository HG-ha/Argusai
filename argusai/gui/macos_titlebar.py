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
from typing import Any

log = logging.getLogger("argusai.gui.titlebar")

# AppKit constants (stable ABI values; avoids importing symbols that may be
# absent in older pyobjc).
_NS_FULL_SIZE_CONTENT_VIEW = 1 << 15  # NSWindowStyleMaskFullSizeContentView
_NS_TITLE_HIDDEN = 1                   # NSWindowTitleHidden
# Width reserved on the left of our header for the three traffic lights.
TRAFFIC_LIGHT_INSET = 78


def _resolve_nswindow(window: Any) -> Any:
    """Best-effort: find the Cocoa ``NSWindow`` behind a pywebview window."""
    candidate = getattr(window, "native", None)
    if candidate is not None and hasattr(candidate, "setStyleMask_"):
        return candidate
    try:
        from webview.platforms import cocoa  # type: ignore

        instances = getattr(cocoa.BrowserView, "instances", {}) or {}
        inst = instances.get(getattr(window, "uid", None)) or next(iter(instances.values()), None)
        for attr in ("window", "_window"):
            nswin = getattr(inst, attr, None)
            if nswin is not None and hasattr(nswin, "setStyleMask_"):
                return nswin
    except Exception:
        log.debug("cocoa NSWindow lookup failed", exc_info=True)
    return None


def apply_unified_titlebar(window: Any) -> dict[str, Any] | None:
    """Make the macOS titlebar transparent + full-size-content.

    Returns ``{"inset_left": ...}`` when applied so the caller can tell the
    frontend to draw its custom header with that left inset; ``None`` otherwise.
    """
    if sys.platform != "darwin":
        return None
    nswin = _resolve_nswindow(window)
    if nswin is None:
        log.info("macos titlebar: native NSWindow not reachable; keeping native frame")
        return None
    try:
        nswin.setTitlebarAppearsTransparent_(True)
        nswin.setTitleVisibility_(_NS_TITLE_HIDDEN)
        nswin.setStyleMask_(nswin.styleMask() | _NS_FULL_SIZE_CONTENT_VIEW)
        # Dragging is handled by the web ``.titlebar-drag`` region, not the
        # whole background, so selection in the content still works.
        nswin.setMovableByWindowBackground_(False)
    except Exception:
        log.debug("macos titlebar styling failed", exc_info=True)
        return None
    log.info("macos titlebar: unified transparent titlebar applied")
    return {"inset_left": TRAFFIC_LIGHT_INSET}
