"""Window-control bridge methods for the custom title bar.

All of these route to the underlying pywebview window installed on
``self._window`` by ``launch.main`` immediately after the window is
created. Each method gracefully degrades to ``{"ok": False, ...}`` if
the window is missing or pywebview raised an error.
"""

from __future__ import annotations

import logging
from typing import Any

from argusai.gui.window_geometry import clamp_size, clamp_to_work_area

log = logging.getLogger("argusai.gui")


class WindowMixin:
    """Title-bar reachable window controls."""

    def minimize_window(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            # WebView2 does not reliably emit DOM blur/visibility events before
            # a native minimize. Publish the state here so already-rendered
            # approval cards immediately produce an OS notification.
            publish_attention = getattr(self, "set_app_attention_state", None)
            if callable(publish_attention):
                previous = dict(getattr(self, "_app_attention_state", {}) or {})
                previous.update({"focused": False, "visible": False})
                publish_attention(previous)
            self._window.minimize()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def maximize_window(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            if self._window_maximized:
                self._window.restore()
                self._window_maximized = False
            else:
                self._window.maximize()
                self._window_maximized = True
            return {"ok": True, "maximized": self._window_maximized}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def resize_window(self, width: int, height: int) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            self._window_maximized = False
            self._window.resize(max(1000, int(width)), max(680, int(height)))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def move_window(self, x: int, y: int) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            self._window.move(int(x), int(y))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_window_geometry(self, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        """Atomically apply position + size in a single ``SetWindowPos``.

        The frontend's custom edge-drag resize previously called
        ``move_window`` followed by ``resize_window`` on every animation
        frame. On Windows that compiles down to two separate
        ``SetWindowPos`` calls — for one frame the window sits at
        (new position, OLD size), and the next frame snaps it to the
        new size. Eyes see this as the window edge jittering during
        drag-resize.

        Doing one combined ``SetWindowPos`` removes that "two-step"
        per-frame artifact, which is the single biggest source of
        flicker on a frameless WebView2 window. Width/height clamping
        mirrors ``resize_window`` so the JS side does not have to
        duplicate min-size guards.
        """
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            self._window_maximized = False
            w = max(1000, int(width))
            h = max(680, int(height))

            import sys

            if sys.platform == "win32":
                ok = self._win32_set_window_pos(int(x), int(y), w, h)
                if ok:
                    return {"ok": True, "atomic": True}
                # Fall through to the two-step path on any failure.

            self._window.move(int(x), int(y))
            self._window.resize(w, h)
            return {"ok": True, "atomic": False}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _win32_set_window_pos(self, x: int, y: int, width: int, height: int) -> bool:
        """One-shot ``SetWindowPos`` using pywebview's WinForms BrowserForm.

        Returns ``True`` if the call succeeded, ``False`` if any of the
        pywebview internals we depend on aren't available (so the
        caller can fall back to the two-step path).
        """
        try:
            from ctypes import windll  # type: ignore[import-untyped]
            from webview.platforms.winforms import BrowserView  # type: ignore[import-untyped]

            uid = getattr(self._window, "uid", None)
            instance = BrowserView.instances.get(uid) if uid else None
            if instance is None:
                return False
            handle = getattr(instance, "Handle", None)
            if handle is None:
                return False
            hwnd = int(handle.ToInt32())
            scale = float(getattr(instance, "_scale", 1.0) or 1.0)
            x_phys = int(x * scale)
            y_phys = int(y * scale)
            w_phys = int(width * scale)
            h_phys = int(height * scale)
            # SWP_NOZORDER (0x0004) — keep current Z order so we don't
            # disturb the focus stack mid-drag.
            # SWP_NOACTIVATE (0x0010) — don't re-activate the window on
            # every frame, otherwise child WebView2 hosts steal focus.
            # SWP_NOSENDCHANGING (0x0400) — skip WM_WINDOWPOSCHANGING
            # round-trip; the BrowserForm's own resize handlers are not
            # needed for our manual drag-resize.
            flags = 0x0004 | 0x0010 | 0x0400
            windll.user32.SetWindowPos(hwnd, 0, x_phys, y_phys, w_phys, h_phys, flags)
            return True
        except Exception:
            return False

    def close_to_tray_enabled(self) -> bool:
        if getattr(self, "_quit_requested", False):
            return False
        try:
            from argusai.gui.tray import close_to_tray_supported

            if not close_to_tray_supported():
                return False
            tray = getattr(self, "_tray", None)
            if tray is not None and not tray.icon_visible():
                return False
            return bool(getattr(self.settings_mgr.get(), "gui_close_to_tray", True))
        except Exception:
            return True

    def hide_window_to_tray(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            self.persist_window_geometry()
            publish_attention = getattr(self, "set_app_attention_state", None)
            if callable(publish_attention):
                previous = dict(getattr(self, "_app_attention_state", {}) or {})
                previous.update({"focused": False, "visible": False})
                publish_attention(previous)
            hide = getattr(self._window, "hide", None)
            if callable(hide):
                hide()
            else:
                self._window.minimize()
            return {"ok": True, "hidden": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def show_window_from_tray(self) -> dict[str, Any]:
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            show = getattr(self._window, "show", None)
            if callable(show):
                show()
            if getattr(self, "_window_maximized", False):
                maximize = getattr(self._window, "maximize", None)
                if callable(maximize):
                    maximize()
            else:
                restore = getattr(self._window, "restore", None)
                if callable(restore):
                    restore()
            self._win32_reveal_form()
            publish_attention = getattr(self, "set_app_attention_state", None)
            if callable(publish_attention):
                previous = dict(getattr(self, "_app_attention_state", {}) or {})
                previous.update({"focused": True, "visible": True})
                publish_attention(previous)
            return {"ok": True, "hidden": False}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _win32_reveal_form(self) -> None:
        """Bring the WinForms host back after Hide() — pywebview show() is not enough."""
        try:
            import sys
            from ctypes import windll

            if sys.platform != "win32":
                return
            from webview.platforms.winforms import BrowserView  # type: ignore[import-untyped]

            uid = getattr(self._window, "uid", None)
            instance = BrowserView.instances.get(uid) if uid else None
            if instance is None:
                return
            show = getattr(instance, "Show", None)
            if callable(show):
                show()
            activate = getattr(instance, "Activate", None)
            if callable(activate):
                activate()
            handle = getattr(instance, "Handle", None)
            if handle is None:
                return
            to64 = getattr(handle, "ToInt64", None)
            hwnd = int(to64()) if callable(to64) else int(handle.ToInt32())
            if hwnd:
                windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            log.debug("gui.tray reveal form failed", exc_info=True)

    def close_window(self) -> dict[str, Any]:
        if self.close_to_tray_enabled():
            return self.hide_window_to_tray()
        return self.quit_application()

    def quit_application(self) -> dict[str, Any]:
        self._quit_requested = True
        stopper = getattr(self, "_stop_tray", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            self.persist_window_geometry()
            self._window.destroy()
            return {"ok": True, "quit": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_gui_close_to_tray(self, enabled: bool = True) -> dict[str, Any]:
        try:
            def _update(settings) -> None:
                settings.gui_close_to_tray = bool(enabled)

            self.settings_mgr.update(_update)
            return {"ok": True, "close_to_tray": bool(enabled)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_window_geometry(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        maximized: bool = False,
    ) -> dict[str, Any]:
        """Persist main-window placement to ``settings.json``."""
        try:
            w, h = clamp_size(width, height)
            cx, cy = clamp_to_work_area(int(x), int(y), w, h)

            def _update(settings) -> None:
                settings.gui_window_x = cx
                settings.gui_window_y = cy
                settings.gui_window_width = w
                settings.gui_window_height = h
                settings.gui_window_maximized = bool(maximized)

            self.settings_mgr.update(_update)
            return {"ok": True}
        except Exception as e:
            log.warning("save_window_geometry failed: %s", e)
            return {"ok": False, "error": str(e)}

    def persist_window_geometry(self) -> dict[str, Any]:
        """Read the live pywebview window and write geometry to settings."""
        if not self._window:
            return {"ok": False, "error": "no window"}
        try:
            x = int(getattr(self._window, "x", 0) or 0)
            y = int(getattr(self._window, "y", 0) or 0)
            width = int(getattr(self._window, "width", 0) or 0)
            height = int(getattr(self._window, "height", 0) or 0)
            return self.save_window_geometry(
                x,
                y,
                width,
                height,
                maximized=bool(getattr(self, "_window_maximized", False)),
            )
        except Exception as e:
            log.warning("persist_window_geometry failed: %s", e)
            return {"ok": False, "error": str(e)}
