"""Desktop window placement helpers for pywebview startup / restore."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argusai.core.settings import AppSettings

MIN_WINDOW_WIDTH = 1000
MIN_WINDOW_HEIGHT = 680
DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 940
# Keep at least this many pixels of the title bar inside the work area when restoring.
MIN_VISIBLE_PX = 80

_FALLBACK_WORK_AREA = (0, 0, 1920, 1080)


@dataclass(frozen=True)
class StartupGeometry:
    x: int
    y: int
    width: int
    height: int
    maximized: bool


def _macos_work_area() -> tuple[int, int, int, int] | None:
    """Primary display work area in top-left coordinates (matches Windows SPI).

    AppKit uses a bottom-left origin; we convert ``visibleFrame`` so callers can
    treat ``y=0`` as the top of the usable desktop (below the menu bar).
    """
    script = """
    ObjC.import('AppKit');
    var screen = $.NSScreen.mainScreen;
    if (!screen && $.NSScreen.screens.length > 0) {
      screen = $.NSScreen.screens.objectAtIndex(0);
    }
    if (!screen) {
      JSON.stringify(null);
    } else {
      var frame = screen.frame;
      var visible = screen.visibleFrame;
      var topLeftY = frame.size.height - (visible.origin.y + visible.size.height);
      JSON.stringify({
        x: Math.round(visible.origin.x),
        y: Math.round(topLeftY),
        width: Math.round(visible.size.width),
        height: Math.round(visible.size.height)
      });
    }
    """
    try:
        raw = subprocess.check_output(
            ["osascript", "-l", "JavaScript", "-e", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        ).strip()
        if not raw or raw == "null":
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        x = int(data["x"])
        y = int(data["y"])
        w = int(data["width"])
        h = int(data["height"])
        if w < MIN_WINDOW_WIDTH or h < MIN_WINDOW_HEIGHT:
            return None
        return (x, y, w, h)
    except Exception:
        return None


def _linux_work_area() -> tuple[int, int, int, int] | None:
    """Best-effort primary geometry via ``xdpyinfo`` (no hard X11 dependency)."""
    try:
        raw = subprocess.check_output(
            ["xdpyinfo"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("dimensions:"):
            continue
        # dimensions: 1920x1080 pixels (1920x1080 millimeters)
        try:
            token = line.split()[1]
            w_s, h_s = token.lower().split("x", 1)
            w, h = int(w_s), int(h_s)
            if w >= MIN_WINDOW_WIDTH and h >= MIN_WINDOW_HEIGHT:
                return (0, 0, w, h)
        except (IndexError, ValueError):
            return None
    return None


def get_work_area() -> tuple[int, int, int, int]:
    """Return ``(x, y, width, height)`` of the primary monitor work area."""
    if sys.platform == "win32":
        try:
            from ctypes import Structure, byref, windll
            from ctypes.wintypes import LONG

            class _RECT(Structure):
                _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]

            rect = _RECT()
            # SPI_GETWORKAREA = 0x0030
            if windll.user32.SystemParametersInfoW(0x0030, 0, byref(rect), 0):
                return (
                    int(rect.left),
                    int(rect.top),
                    int(rect.right - rect.left),
                    int(rect.bottom - rect.top),
                )
        except Exception:
            pass
    elif sys.platform == "darwin":
        area = _macos_work_area()
        if area is not None:
            return area
    else:
        area = _linux_work_area()
        if area is not None:
            return area
    return _FALLBACK_WORK_AREA


def center_position(width: int, height: int) -> tuple[int, int]:
    ax, ay, aw, ah = get_work_area()
    x = ax + max(0, (aw - width) // 2)
    y = ay + max(0, (ah - height) // 2)
    return x, y


def clamp_size(width: int, height: int) -> tuple[int, int]:
    w = max(MIN_WINDOW_WIDTH, int(width or DEFAULT_WINDOW_WIDTH))
    h = max(MIN_WINDOW_HEIGHT, int(height or DEFAULT_WINDOW_HEIGHT))
    return w, h


def clamp_to_work_area(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Nudge ``(x, y)`` so a reasonable portion of the window stays visible."""
    ax, ay, aw, ah = get_work_area()
    w, h = clamp_size(width, height)
    max_x = ax + aw - MIN_VISIBLE_PX
    max_y = ay + ah - MIN_VISIBLE_PX
    min_x = ax - w + MIN_VISIBLE_PX
    min_y = ay
    cx = min(max_x, max(min_x, int(x)))
    cy = min(max_y, max(min_y, int(y)))
    return cx, cy


def resolve_startup_geometry(
    settings: AppSettings,
    *,
    default_width: int = DEFAULT_WINDOW_WIDTH,
    default_height: int = DEFAULT_WINDOW_HEIGHT,
) -> StartupGeometry:
    """Pick initial window rect: restore from settings or center on screen."""
    raw_w = int(getattr(settings, "gui_window_width", 0) or 0)
    raw_h = int(getattr(settings, "gui_window_height", 0) or 0)
    width, height = clamp_size(
        raw_w or default_width,
        raw_h or default_height,
    )
    saved_x = getattr(settings, "gui_window_x", None)
    saved_y = getattr(settings, "gui_window_y", None)
    maximized = bool(getattr(settings, "gui_window_maximized", False))
    if saved_x is not None and saved_y is not None:
        x, y = clamp_to_work_area(int(saved_x), int(saved_y), width, height)
    else:
        x, y = center_position(width, height)
    return StartupGeometry(x=x, y=y, width=width, height=height, maximized=maximized)
