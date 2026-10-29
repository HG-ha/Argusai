"""OS desktop observation and input (Windows + macOS; safe fallbacks on Linux)."""

from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import Any


_DESKTOP_SESSIONS: dict[str, dict[str, Any]] = {}
_WIN_DPI_AWARE: bool = False


def _session_key(session_id: str) -> str:
    return str(session_id or "default").strip() or "default"


def _ensure_win_dpi_awareness() -> None:
    """Make Win32 monitor/capture coords match physical pixels (Per-Monitor v2)."""
    global _WIN_DPI_AWARE
    if _WIN_DPI_AWARE or not sys.platform.startswith("win"):
        return
    ctypes, _wintypes, ok = _win_imports()
    if not ok:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        _WIN_DPI_AWARE = True
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        _WIN_DPI_AWARE = True
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            _WIN_DPI_AWARE = True
        except Exception:
            pass


def start_desktop_action_session(session_id: str, *, minutes: int = 5, scope: str = "") -> dict[str, Any]:
    """Compatibility shim. Timed desktop authorization is retired."""
    del minutes
    key = _session_key(session_id)
    _DESKTOP_SESSIONS.pop(key, None)
    return {
        "ok": True,
        "session_id": key,
        "required": False,
        "deprecated": True,
        "active": False,
        "scope": str(scope or "")[:500],
        "message": (
            "Desktop mouse/keyboard no longer needs a timed authorization session. "
            "Call desktop_click / desktop_mouse_move / desktop_type / desktop_hotkey directly."
        ),
    }


def stop_desktop_action_session(session_id: str) -> dict[str, Any]:
    key = _session_key(session_id)
    existed = key in _DESKTOP_SESSIONS
    _DESKTOP_SESSIONS.pop(key, None)
    return {"ok": True, "stopped": existed, "required": False, "deprecated": True, "active": False}


def desktop_action_session_active(session_id: str) -> bool:
    del session_id
    return True


def desktop_session_status(session_id: str) -> dict[str, Any]:
    key = _session_key(session_id)
    _DESKTOP_SESSIONS.pop(key, None)
    out = {
        "ok": True,
        "active": False,
        "required": False,
        "deprecated": True,
        "session_id": key,
        "state": "idle",
    }
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_permissions as dp

        out["macos_permissions"] = dp.check_all()
    return out


def _require_action_session(session_id: str) -> None:
    del session_id


def _win_imports():
    if not sys.platform.startswith("win"):
        return None, None, None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None, None, None
    return ctypes, wintypes, True


def _darwin_osascript(script: str, *, timeout: int = 15) -> str | None:
    import subprocess

    try:
        out = subprocess.check_output(
            ["osascript", "-l", "JavaScript", "-e", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return out.strip() or None
    except Exception:
        return None


def _darwin_list_monitors() -> list[dict[str, Any]]:
    raw = _darwin_osascript(
        """
        ObjC.import('AppKit');
        var screens = $.NSScreen.screens;
        var main = $.NSScreen.mainScreen;
        var out = [];
        for (var i = 0; i < screens.length; i++) {
          var s = screens.objectAtIndex(i);
          var f = s.frame;
          out.push({
            index: i,
            left: Math.round(f.origin.x),
            top: Math.round(f.origin.y),
            width: Math.round(f.size.width),
            height: Math.round(f.size.height),
            right: Math.round(f.origin.x + f.size.width),
            bottom: Math.round(f.origin.y + f.size.height),
            primary: s.isEqual(main),
            name: 'display-' + (i + 1)
          });
        }
        JSON.stringify(out);
        """
    )
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                monitors: list[dict[str, Any]] = []
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    monitors.append({
                        "index": int(item.get("index", len(monitors))),
                        "primary": bool(item.get("primary")),
                        "left": int(item.get("left", 0)),
                        "top": int(item.get("top", 0)),
                        "width": int(item.get("width", 0)),
                        "height": int(item.get("height", 0)),
                        "right": int(item.get("right", 0)),
                        "bottom": int(item.get("bottom", 0)),
                        "name": str(item.get("name") or "")[:64],
                        "coordinate_system": "macos_cocoa_bottom_left",
                    })
                if monitors:
                    monitors.sort(key=lambda m: (not m.get("primary"), m.get("left", 0), m.get("top", 0)))
                    for i, mon in enumerate(monitors):
                        mon["index"] = i
                    return monitors
        except Exception:
            pass
    return [{
        "index": 0,
        "primary": True,
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
        "right": 1920,
        "bottom": 1080,
        "name": platform.platform(),
        "note": "monitor enumeration fell back to defaults on macOS",
    }]


def _darwin_screencapture(path: Path, *, monitor: int | None) -> tuple[bool, str]:
    import subprocess

    png_path = path if path.suffix.lower() == ".png" else path.with_suffix(".png")
    cmd = ["screencapture", "-x"]
    if monitor is not None and monitor >= 0:
        cmd.append(f"-D{int(monitor) + 1}")
    cmd.append(str(png_path))
    try:
        subprocess.run(cmd, check=True, timeout=90)
        if png_path.exists():
            return True, ""
        return False, "screencapture produced no file"
    except Exception as e:
        return False, str(e)


def list_monitors() -> list[dict[str, Any]]:
    """Enumerate displays with virtual-desktop bounds (multi-monitor aware)."""
    if sys.platform == "darwin":
        return _darwin_list_monitors()
    _ensure_win_dpi_awareness()
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return [{
            "index": 0,
            "primary": True,
            "left": 0,
            "top": 0,
            "width": 1920,
            "height": 1080,
            "right": 1920,
            "bottom": 1080,
            "name": platform.platform(),
            "note": "monitor enumeration unavailable on this OS",
        }]

    user32 = ctypes.windll.user32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    MONITORINFOF_PRIMARY = 1
    monitors: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def _callback(hmon, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        r = info.rcMonitor
        monitors.append({
            "handle": int(hmon),
            "left": int(r.left),
            "top": int(r.top),
            "width": int(r.right - r.left),
            "height": int(r.bottom - r.top),
            "right": int(r.right),
            "bottom": int(r.bottom),
            "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
            "name": str(info.szDevice or "")[:64],
        })
        return True

    user32.EnumDisplayMonitors(None, None, _callback, 0)
    monitors.sort(key=lambda m: (not m.get("primary"), m.get("left", 0), m.get("top", 0)))
    for i, mon in enumerate(monitors):
        mon["index"] = i
    return monitors


def virtual_desktop_bounds(monitors: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Union bounding box of all monitors in virtual screen coordinates."""
    mons = monitors if monitors is not None else list_monitors()
    if not mons:
        return {"left": 0, "top": 0, "right": 1920, "bottom": 1080, "width": 1920, "height": 1080}
    left = min(int(m["left"]) for m in mons)
    top = min(int(m["top"]) for m in mons)
    right = max(int(m.get("right", m["left"] + m["width"])) for m in mons)
    bottom = max(int(m.get("bottom", m["top"] + m["height"])) for m in mons)
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": right - left,
        "height": bottom - top,
        "monitor_count": len(mons),
    }


def monitor_at_point(x: int, y: int, monitors: list[dict[str, Any]] | None = None) -> int | None:
    """Return monitor index containing point (x,y), or None."""
    px, py = int(x), int(y)
    for mon in monitors or list_monitors():
        if mon["left"] <= px < mon["left"] + mon["width"] and mon["top"] <= py < mon["top"] + mon["height"]:
            return int(mon["index"])
    return None


def _window_monitor_index(win: dict[str, Any], monitors: list[dict[str, Any]]) -> int | None:
    cx = int(win.get("left", 0)) + int(win.get("width", 0)) // 2
    cy = int(win.get("top", 0)) + int(win.get("height", 0)) // 2
    return monitor_at_point(cx, cy, monitors)


def _win_pid_alive(pid: int) -> bool:
    """True when the process id still refers to a live process."""
    if pid <= 0:
        return False
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return False
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        # STILL_ACTIVE = 259
        return int(exit_code.value) == 259
    finally:
        kernel32.CloseHandle(handle)


def _hwnd_process_id(hwnd: int) -> int:
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return 0
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
    return int(pid.value or 0)


def list_windows() -> list[dict[str, Any]]:
    ctypes, wintypes, ok = _win_imports()
    monitors = list_monitors() if ok else []
    if not ok:
        return [{"title": platform.platform(), "note": "window enumeration unavailable on this OS"}]
    user32 = ctypes.windll.user32
    results: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value.strip()
        if not title:
            return True
        pid = _hwnd_process_id(int(hwnd))
        # Drop zombie HWNDs whose owning process has already exited.
        if pid and not _win_pid_alive(pid):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return True
        item = {
            "hwnd": int(hwnd),
            "pid": pid,
            "title": title[:300],
            "left": int(rect.left),
            "top": int(rect.top),
            "width": width,
            "height": height,
        }
        midx = _window_monitor_index(item, monitors)
        if midx is not None:
            item["monitor_index"] = midx
        results.append(item)
        return True

    user32.EnumWindows(_enum, 0)
    results.sort(key=lambda x: (-int(x.get("width", 0) or 0) * int(x.get("height", 0) or 0), x.get("title", "")))
    return results[:80]


def focus_window(hwnd: int) -> dict[str, Any]:
    """Bring a window to the foreground by HWND (Windows)."""
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return {"ok": False, "error": "focus_window is only implemented on Windows"}
    user32 = ctypes.windll.user32
    target = int(hwnd or 0)
    if target <= 0 or not user32.IsWindow(target):
        return {"ok": False, "error": f"invalid hwnd: {hwnd}", "hwnd": target}
    pid = _hwnd_process_id(target)
    if pid and not _win_pid_alive(pid):
        return {"ok": False, "error": "window process is not alive (zombie hwnd)", "hwnd": target, "pid": pid}

    # Restore if minimized, then try to foreground.
    SW_RESTORE = 9
    user32.ShowWindow(target, SW_RESTORE)
    # Attach thread input so SetForegroundWindow is more reliable.
    foreground = user32.GetForegroundWindow()
    target_tid = user32.GetWindowThreadProcessId(target, None)
    fg_tid = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached_fg = False
    attached_target = False
    try:
        if fg_tid and fg_tid != current_tid:
            attached_fg = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
        if target_tid and target_tid != current_tid:
            attached_target = bool(user32.AttachThreadInput(current_tid, target_tid, True))
        user32.BringWindowToTop(target)
        ok_fg = bool(user32.SetForegroundWindow(target))
    finally:
        if attached_target:
            user32.AttachThreadInput(current_tid, target_tid, False)
        if attached_fg:
            user32.AttachThreadInput(current_tid, fg_tid, False)

    rect = wintypes.RECT()
    user32.GetWindowRect(target, ctypes.byref(rect))
    return {
        "ok": True,
        "hwnd": target,
        "pid": pid,
        "set_foreground": ok_fg,
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
        "foreground_now": int(user32.GetForegroundWindow() or 0) == target,
    }


def _image_mostly_black(img: Any, *, sample: int = 48, dark_threshold: int = 12) -> bool:
    """True when a downscaled sample is nearly all near-black (GPU/PrintWindow miss)."""
    try:
        from PIL import Image

        if img is None:
            return True
        im = img.convert("RGB") if getattr(img, "mode", "") != "RGB" else img
        thumb = im.resize((sample, sample), getattr(Image, "BILINEAR", 2))
        pixels = list(thumb.getdata())
        if not pixels:
            return True
        dark = sum(1 for r, g, b in pixels if r <= dark_threshold and g <= dark_threshold and b <= dark_threshold)
        return (dark / len(pixels)) >= 0.97
    except Exception:
        return False


def capture_window_screenshot(
    path: Path,
    *,
    hwnd: int,
    max_width: int = 1920,
    jpeg_quality: int = 90,
) -> dict[str, Any]:
    """Capture one window by HWND (PrintWindow / Pillow window grab)."""
    path = Path(path)
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        path = path.with_suffix(".jpg")
    path.parent.mkdir(parents=True, exist_ok=True)

    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return {"ok": False, "error": "window screenshot is only implemented on Windows"}
    user32 = ctypes.windll.user32
    target = int(hwnd or 0)
    if target <= 0 or not user32.IsWindow(target):
        return {"ok": False, "error": f"invalid hwnd: {hwnd}", "hwnd": target}
    pid = _hwnd_process_id(target)
    if pid and not _win_pid_alive(pid):
        return {"ok": False, "error": "window process is not alive (zombie hwnd)", "hwnd": target, "pid": pid}

    rect = wintypes.RECT()
    user32.GetWindowRect(target, ctypes.byref(rect))
    left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    width, height = max(1, right - left), max(1, bottom - top)
    meta = {
        "hwnd": target,
        "pid": pid,
        "capture_bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
    }

    img = None
    backend = ""
    # Prefer Pillow's window-aware grab when available.
    try:
        from PIL import ImageGrab

        try:
            img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True, window=target)
            backend = "pil_window"
        except TypeError:
            img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            backend = "pil_bbox"
    except Exception:
        img = None

    # Chromium/GPU apps (QQ/WeChat/Electron) often yield a black PrintWindow/
    # pil_window buffer. Fall back to screen BitBlt of the window rect.
    if img is not None and _image_mostly_black(img):
        try:
            gdi_img = _capture_bbox_win_gdi((left, top, right, bottom))
            if gdi_img is not None and not _image_mostly_black(gdi_img):
                img = gdi_img
                backend = "win_gdi_after_black"
            else:
                backend = f"{backend}_black"
        except Exception:
            backend = f"{backend}_black"

    if img is None:
        try:
            img = _capture_bbox_win_gdi((left, top, right, bottom))
            backend = "win_gdi"
        except Exception:
            img = None

    if img is None:
        return {**meta, "ok": False, "error": "window screenshot capture failed"}

    try:
        comp = _save_compressed_image(img, path, max_width=max_width, jpeg_quality=jpeg_quality)
        out_path = Path(comp.get("path") or path)
        out = {
            **meta,
            "ok": True,
            "path": str(out_path),
            "size": out_path.stat().st_size if out_path.exists() else 0,
            "mime": comp.get("mime", "image/jpeg"),
            "original_pixels": comp.get("original_pixels"),
            "output_pixels": comp.get("output_pixels"),
            "jpeg_quality": comp.get("jpeg_quality"),
            "compressed": True,
            "capture_backend": backend,
        }
        if backend.endswith("_black") or _image_mostly_black(img):
            out["warning"] = (
                "Window screenshot looks black/empty (common for GPU/Chromium apps). "
                "Retry with desktop_observe(screenshot=true, monitor=<window.monitor_index>) "
                "instead of hwnd-only capture."
            )
            out["screenshot_looks_black"] = True
        return out
    except Exception as e:
        return {**meta, "ok": False, "error": str(e)}


def foreground_window() -> dict[str, Any]:
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return {"ok": False, "error": "foreground window unavailable on this OS"}
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"ok": False, "error": "no foreground window"}
    length = user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    out: dict[str, Any] = {
        "ok": True,
        "hwnd": int(hwnd),
        "title": buff.value.strip()[:300],
        "left": int(rect.left),
        "top": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }
    monitors = list_monitors()
    midx = _window_monitor_index(out, monitors)
    if midx is not None:
        out["monitor_index"] = midx
    return out


def _win_powershell(script: str, *, timeout: int = 60) -> tuple[bool, str]:
    import subprocess

    from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **quiet_subprocess_kwargs(),
        )
        return True, out.strip()
    except Exception as e:
        return False, str(e)


def _monitor_bbox(monitor: int | None, monitors: list[dict[str, Any]] | None = None) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) for one monitor or full virtual desktop."""
    mons = monitors if monitors is not None else list_monitors()
    if not mons:
        return None
    if monitor is None:
        v = virtual_desktop_bounds(mons)
        return v["left"], v["top"], v["right"], v["bottom"]
    idx = int(monitor)
    for m in mons:
        if int(m.get("index", -1)) == idx:
            return int(m["left"]), int(m["top"]), int(m["right"]), int(m["bottom"])
    return None


def _save_compressed_image(img: Any, path: Path, *, max_width: int = 1920, jpeg_quality: int = 90) -> dict[str, Any]:
    from PIL import Image

    original_size = (int(img.width), int(img.height))
    # max_width <= 0 keeps the native capture resolution (no downscale).
    if max_width > 0 and img.width > max_width:
        ratio = max_width / float(img.width)
        new_h = max(1, int(img.height * ratio))
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        out_path = path
        mime = "image/jpeg"
    elif suffix == ".png":
        out_path = path
        mime = "image/png"
        img.save(out_path, format="PNG", optimize=True)
        return {
            "original_pixels": original_size,
            "output_pixels": (img.width, img.height),
            "mime": mime,
        }
    else:
        out_path = path.with_suffix(".jpg")
        mime = "image/jpeg"

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    q = max(1, min(100, int(jpeg_quality or 90)))
    img.save(out_path, format="JPEG", quality=q, optimize=True)
    return {
        "original_pixels": original_size,
        "output_pixels": (img.width, img.height),
        "mime": mime,
        "path": str(out_path),
        "jpeg_quality": q,
    }


def _capture_bbox_pil(bbox: tuple[int, int, int, int]) -> Any:
    """Pillow grab; on Windows always use all_screens so secondary monitors are not black."""
    from PIL import ImageGrab

    left, top, right, bottom = (int(v) for v in bbox)
    kwargs: dict[str, Any] = {"bbox": (left, top, right, bottom)}
    if sys.platform.startswith("win"):
        # Without all_screens=True, Pillow often returns a primary-only buffer
        # and paints other monitors black when the bbox uses virtual-desktop
        # coordinates (esp. negative left/top on secondary displays).
        kwargs["all_screens"] = True
    return ImageGrab.grab(**kwargs)


def _capture_bbox_win_gdi(bbox: tuple[int, int, int, int]) -> Any:
    """Capture a Windows screen bbox through Win32 GDI, returning a Pillow image."""
    _ensure_win_dpi_awareness()
    ctypes, wintypes, ok = _win_imports()
    if not ok:
        return None

    from PIL import Image

    left, top, right, bottom = (int(v) for v in bbox)
    width = max(1, right - left)
    height = max(1, bottom - top)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise OSError("GetDC failed")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    if not hdc_mem:
        user32.ReleaseDC(None, hdc_screen)
        raise OSError("CreateCompatibleDC failed")
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    if not hbmp:
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        raise OSError("CreateCompatibleBitmap failed")

    old_obj = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        srccopy = 0x00CC0020
        captureblt = 0x40000000
        if not gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, srccopy | captureblt):
            raise OSError("BitBlt failed")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        # Negative height requests a top-down DIB, avoiding manual row flipping.
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        stride = width * 4
        buf = ctypes.create_string_buffer(stride * height)
        rows = gdi32.GetDIBits(
            hdc_mem,
            hbmp,
            0,
            height,
            buf,
            ctypes.byref(bmi),
            0,  # DIB_RGB_COLORS
        )
        if rows != height:
            raise OSError(f"GetDIBits returned {rows}/{height} rows")
        return Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", stride, 1).copy()
    finally:
        if old_obj:
            gdi32.SelectObject(hdc_mem, old_obj)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def _capture_all_screens_powershell(path: Path) -> tuple[bool, str]:
    ps_path = str(path).replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$screens = [Windows.Forms.Screen]::AllScreens
$bounds = [Drawing.Rectangle]::Empty
foreach ($s in $screens) {{ $bounds = [Drawing.Rectangle]::Union($bounds, $s.Bounds) }}
$bmp = New-Object Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [Drawing.Graphics]::FromImage($bmp)
$g.Clear([Drawing.Color]::Black)
foreach ($s in $screens) {{
  $destX = [int]($s.Bounds.X - $bounds.X)
  $destY = [int]($s.Bounds.Y - $bounds.Y)
  $g.CopyFromScreen(
    [int]$s.Bounds.X,
    [int]$s.Bounds.Y,
    $destX,
    $destY,
    $s.Bounds.Size
  )
}}
$bmp.Save('{ps_path}', [Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""
    return _win_powershell(script, timeout=90)


def _capture_one_bbox(bbox: tuple[int, int, int, int]) -> Any:
    """Capture one screen rectangle; prefer GDI on Windows, then Pillow."""
    if sys.platform.startswith("win"):
        try:
            img = _capture_bbox_win_gdi(bbox)
            if img is not None:
                return img
        except Exception:
            pass
    try:
        return _capture_bbox_pil(bbox)
    except Exception:
        return None


def _capture_monitors_stitched(monitors: list[dict[str, Any]]) -> Any:
    """Stitch per-monitor captures onto the virtual-desktop canvas.

    A single virtual-desktop BitBlt/ImageGrab often leaves secondary monitors
    black; capturing each monitor and pasting into the union box avoids that.
    """
    from PIL import Image

    mons = list(monitors or [])
    if not mons:
        return None
    if len(mons) == 1:
        m = mons[0]
        return _capture_one_bbox((
            int(m["left"]), int(m["top"]), int(m["right"]), int(m["bottom"]),
        ))

    v = virtual_desktop_bounds(mons)
    canvas = Image.new("RGB", (int(v["width"]), int(v["height"])), (0, 0, 0))
    pasted = 0
    for m in mons:
        tile = _capture_one_bbox((
            int(m["left"]), int(m["top"]), int(m["right"]), int(m["bottom"]),
        ))
        if tile is None:
            continue
        dest = (int(m["left"]) - int(v["left"]), int(m["top"]) - int(v["top"]))
        canvas.paste(tile.convert("RGB"), dest)
        pasted += 1
    return canvas if pasted else None


def capture_screenshot(
    path: Path,
    *,
    monitor: int | None = None,
    max_width: int = 1920,
    jpeg_quality: int = 90,
) -> dict[str, Any]:
    """Capture one monitor or full virtual desktop; save compressed JPEG by default."""
    path = Path(path)
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        path = path.with_suffix(".jpg")
    path.parent.mkdir(parents=True, exist_ok=True)

    monitors = list_monitors()
    bbox = _monitor_bbox(monitor, monitors)
    meta: dict[str, Any] = {
        "monitor": monitor,
        "monitors": monitors,
        "virtual_desktop": virtual_desktop_bounds(monitors),
        "capture_bbox": {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]} if bbox else None,
    }

    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_permissions as dp

        ok_scr, perm_scr = dp.ensure_screen_capture(prompt=True)
        if not ok_scr:
            return {
                **meta,
                "ok": False,
                "error": (perm_scr or {}).get("user_message") or "需要 macOS 屏幕录制权限",
                "macos_permission": perm_scr,
                "macos_permissions": dp.check_all(),
            }
        mon_idx = None if monitor is None else int(monitor)
        if mon_idx is not None and mon_idx < 0:
            mon_idx = None
        ok_cap, err_cap = _darwin_screencapture(path.with_suffix(".png"), monitor=mon_idx)
        if not ok_cap:
            return {**meta, "ok": False, "error": err_cap or "macOS screencapture failed"}
        try:
            from PIL import Image

            img = Image.open(path.with_suffix(".png"))
            comp = _save_compressed_image(img, path, max_width=max_width, jpeg_quality=jpeg_quality)
            out_path = Path(comp.get("path") or path)
            size = out_path.stat().st_size if out_path.exists() else 0
            return {
                **meta,
                "ok": True,
                "path": str(out_path),
                "size": size,
                "mime": comp.get("mime", "image/jpeg"),
                "original_pixels": comp.get("original_pixels"),
                "output_pixels": comp.get("output_pixels"),
                "jpeg_quality": comp.get("jpeg_quality"),
                "compressed": True,
                "capture_backend": "screencapture",
            }
        except Exception as e:
            return {**meta, "ok": False, "error": f"macOS screenshot post-process failed: {e}"}

    if not sys.platform.startswith("win"):
        return {**meta, "ok": False, "error": "screenshot capture not implemented on this OS yet"}

    _ensure_win_dpi_awareness()

    if bbox is None:
        return {**meta, "ok": False, "error": f"invalid monitor index: {monitor}"}

    img = None
    backend = ""

    # Multi-monitor full-desktop: stitch each display. A single virtual-desktop
    # grab commonly paints secondary monitors black.
    if monitor is None and len(monitors) > 1:
        try:
            img = _capture_monitors_stitched(monitors)
            if img is not None:
                backend = "win_stitch"
        except Exception:
            img = None

    if img is None:
        try:
            img = _capture_one_bbox(bbox)
            if img is not None:
                backend = "win_gdi_or_pil"
        except Exception:
            img = None

    if img is None and monitor is None:
        ok, err = _capture_all_screens_powershell(path.with_suffix(".png"))
        if ok and path.with_suffix(".png").exists():
            try:
                from PIL import Image
                img = Image.open(path.with_suffix(".png"))
                path = path.with_suffix(".jpg")
                backend = "powershell_stitch"
            except Exception:
                return {**meta, "ok": False, "error": err or "screenshot failed"}

    if img is None and monitor is not None:
        return {**meta, "ok": False, "error": "per-monitor capture requires Pillow (pip install Pillow)"}

    if img is None:
        return {**meta, "ok": False, "error": "screenshot capture failed (install Pillow for multi-monitor)"}

    try:
        comp = _save_compressed_image(img, path, max_width=max_width, jpeg_quality=jpeg_quality)
        out_path = Path(comp.get("path") or path)
        size = out_path.stat().st_size if out_path.exists() else 0
        return {
            **meta,
            "ok": True,
            "path": str(out_path),
            "size": size,
            "mime": comp.get("mime", "image/jpeg"),
            "original_pixels": comp.get("original_pixels"),
            "output_pixels": comp.get("output_pixels"),
            "jpeg_quality": comp.get("jpeg_quality"),
            "compressed": True,
            "capture_backend": backend or "unknown",
        }
    except Exception as e:
        return {**meta, "ok": False, "error": str(e)}


def mouse_move(session_id: str, *, x: int, y: int) -> dict[str, Any]:
    del session_id
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_input as di

        monitors = list_monitors()
        ok, err, perm = di.mouse_move(int(x), int(y), monitors=monitors)
        if not ok:
            from argusai.core.workspace import darwin_permissions as dp

            out = {"ok": False, "error": err}
            if perm:
                out["macos_permission"] = perm
                out["macos_permissions"] = dp.check_all()
            return out
        return {
            "ok": True,
            "x": int(x),
            "y": int(y),
            "monitor_index": monitor_at_point(x, y, monitors),
            "coordinate_system": "macos_cocoa_bottom_left",
        }
    ctypes, _, ok = _win_imports()
    if not ok:
        return {"ok": False, "error": "mouse control unavailable on this OS"}
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    return {"ok": True, "x": int(x), "y": int(y), "monitor_index": monitor_at_point(x, y)}


def mouse_click(session_id: str, *, x: int, y: int, button: str = "left") -> dict[str, Any]:
    _require_action_session(session_id)
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_input as di

        monitors = list_monitors()
        ok, err, perm = di.mouse_click(int(x), int(y), button, monitors=monitors)
        if not ok:
            from argusai.core.workspace import darwin_permissions as dp

            out = {"ok": False, "error": err}
            if perm:
                out["macos_permission"] = perm
                out["macos_permissions"] = dp.check_all()
            return out
        midx = monitor_at_point(x, y, monitors)
        btn = str(button or "left").lower()
        return {
            "ok": True,
            "x": int(x),
            "y": int(y),
            "button": btn,
            "monitor_index": midx,
            "coordinate_system": "macos_cocoa_bottom_left",
        }
    ctypes, _, ok = _win_imports()
    if not ok:
        return {"ok": False, "error": "mouse control unavailable on this OS"}
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    btn = str(button or "left").lower()
    if btn == "right":
        user32.mouse_event(0x0008, 0, 0, 0, 0)
        user32.mouse_event(0x0010, 0, 0, 0, 0)
    else:
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    midx = monitor_at_point(x, y)
    return {"ok": True, "x": int(x), "y": int(y), "button": btn, "monitor_index": midx}


def _send_ctrl_v() -> bool:
    ctypes, _, ok = _win_imports()
    if not ok:
        return False
    user32 = ctypes.windll.user32
    VK_CONTROL, VK_V = 0x11, 0x56
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    return True


_WIN_KEY_CODES: dict[str, int] = {
    "alt": 0x12,
    "backspace": 0x08,
    "ctrl": 0x11,
    "control": 0x11,
    "delete": 0x2E,
    "down": 0x28,
    "end": 0x23,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "home": 0x24,
    "left": 0x25,
    "pagedown": 0x22,
    "pageup": 0x21,
    "right": 0x27,
    "shift": 0x10,
    "space": 0x20,
    "tab": 0x09,
    "up": 0x26,
    "win": 0x5B,
    "windows": 0x5B,
}
for _i in range(26):
    _WIN_KEY_CODES[chr(ord("a") + _i)] = ord("A") + _i
for _i in range(10):
    _WIN_KEY_CODES[str(_i)] = ord("0") + _i
for _i in range(1, 13):
    _WIN_KEY_CODES[f"f{_i}"] = 0x70 + _i - 1


_WIN_KEY_ALIASES: dict[str, str] = {
    "return": "enter",
    "ret": "enter",
    "arrowdown": "down",
    "arrowup": "up",
    "arrowleft": "left",
    "arrowright": "right",
    "del": "delete",
    "cmd": "win",
    "meta": "win",
    "super": "win",
}


def _win_key_code(name: str) -> int | None:
    token = str(name or "").strip().lower().replace(" ", "")
    token = _WIN_KEY_ALIASES.get(token, token)
    return _WIN_KEY_CODES.get(token)


def normalize_hotkey_keys(keys: Any) -> list[str]:
    """Normalize model-provided hotkey args into a chord list.

    LLMs often pass ``keys=\"enter\"`` or ``keys=\"ctrl+a\"``. ``list(\"enter\")``
    would become ``[\"e\",\"n\",\"t\",\"e\",\"r\"]`` and type garbage into the
    focused field — never split multi-character named keys into characters.
    """
    if keys is None:
        return []

    def _canon(token: str) -> str:
        t = str(token or "").strip().lower().replace(" ", "")
        return _WIN_KEY_ALIASES.get(t, t)

    def _parse_spec(spec: str) -> list[str]:
        s = str(spec or "").strip()
        if not s:
            return []
        if "+" in s:
            return [_canon(p) for p in s.split("+") if str(p).strip()]
        if "," in s:
            return [_canon(p) for p in s.split(",") if str(p).strip()]
        # Space-separated chord: "ctrl a"
        if " " in s:
            parts = [_canon(p) for p in s.split() if str(p).strip()]
            if parts and all(_win_key_code(p) is not None for p in parts):
                return parts
        token = _canon(s)
        # Named multi-char key (enter/down/escape/…) or single character.
        if _win_key_code(token) is not None:
            return [token]
        # Unknown token — keep as one key so caller gets "unsupported", not char spam.
        return [token]

    if isinstance(keys, str):
        return _parse_spec(keys)

    if isinstance(keys, (list, tuple)):
        # Single-element list that is itself a chord/spec string.
        if len(keys) == 1 and isinstance(keys[0], str):
            one = str(keys[0]).strip()
            if "+" in one or "," in one or " " in one or len(one) > 1:
                return _parse_spec(one)
        out: list[str] = []
        for item in keys:
            if isinstance(item, str) and ("+" in item or "," in item):
                out.extend(_parse_spec(item))
            else:
                tok = _canon(str(item))
                if tok:
                    out.append(tok)
        return out

    return _parse_spec(str(keys))


def _send_win_hotkey(keys: list[str]) -> tuple[bool, str]:
    ctypes, _, ok = _win_imports()
    if not ok:
        return False, "keyboard input unavailable on this OS"
    clean = normalize_hotkey_keys(keys)
    codes: list[int] = []
    unknown: list[str] = []
    for key in clean:
        code = _win_key_code(str(key))
        if code is None:
            unknown.append(str(key))
        else:
            codes.append(code)
    if unknown:
        return False, f"unsupported key(s): {', '.join(unknown)}"
    if not codes:
        return False, "keys is required"
    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
    return True, ""


def type_text(session_id: str, *, text: str) -> dict[str, Any]:
    _require_action_session(session_id)
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_input as di

        payload = str(text or "")
        ok, err, perm = di.type_text(payload)
        if not ok:
            out = {"ok": False, "error": err}
            if perm:
                out["macos_permission"] = perm
            return out
        return {"ok": True, "chars": len(payload), "paste": "cmd+v"}
    if not sys.platform.startswith("win"):
        return {"ok": False, "error": "keyboard input unavailable on this OS"}
    payload = str(text or "")
    try:
        import pyperclip
        pyperclip.copy(payload)
    except Exception:
        escaped = payload.replace("'", "''")
        ok, err = _win_powershell(f"Set-Clipboard -Value '{escaped}'")
        if not ok:
            return {"ok": False, "error": f"clipboard failed: {err}"}
    if not _send_ctrl_v():
        return {"ok": False, "error": "keyboard unavailable"}
    return {"ok": True, "chars": len(payload)}


def hotkey(session_id: str, *, keys: Any) -> dict[str, Any]:
    _require_action_session(session_id)
    clean = normalize_hotkey_keys(keys)
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_input as di

        ok, err, perm = di.hotkey(clean)
        if not ok:
            out = {"ok": False, "error": err}
            if perm:
                out["macos_permission"] = perm
            return out
        return {"ok": True, "keys": clean}
    ok, err = _send_win_hotkey(clean)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "keys": clean}


def running_programs_summary() -> list[dict[str, Any]]:
    if not sys.platform.startswith("win"):
        return []
    try:
        import subprocess

        from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object -First 40 ProcessName,MainWindowTitle | ConvertTo-Json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **quiet_subprocess_kwargs(),
        )
        data = json.loads(out or "[]")
        if isinstance(data, dict):
            data = [data]
        rows = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            rows.append({
                "process": str(item.get("ProcessName") or ""),
                "title": str(item.get("MainWindowTitle") or "")[:200],
            })
        return rows
    except Exception:
        return []
