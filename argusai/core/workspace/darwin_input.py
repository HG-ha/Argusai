"""macOS desktop mouse/keyboard via Quartz CoreGraphics (stdlib ctypes only)."""

from __future__ import annotations

import ctypes
import ctypes.util
import subprocess
import sys
from typing import Any

_CG_LIB: ctypes.CDLL | bool | None = None

# CGEvent types / taps
kCGHIDEventTap = 0
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGMouseButtonLeft = 0
kCGMouseButtonRight = 1
kCGEventFlagMaskCommand = 1 << 20

# Virtual key codes (ANSI)
kVK_Command = 0x37
kVK_ANSI_V = 0x09
kVK_Return = 0x24
kVK_Space = 0x31
kVK_Escape = 0x35
kVK_Tab = 0x30

def _accessibility_denied(*, prompt: bool) -> tuple[bool, str, dict[str, Any] | None]:
    from argusai.core.workspace import darwin_permissions as dp

    ok, payload = dp.ensure_accessibility(prompt=prompt)
    if ok:
        return True, "", None
    msg = (payload or {}).get("user_message") or "需要 macOS 辅助功能权限"
    return False, msg, payload


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _cg_lib() -> ctypes.CDLL | None:
    global _CG_LIB
    if not sys.platform == "darwin":
        return None
    if _CG_LIB is False:
        return None
    if _CG_LIB is not None:
        return _CG_LIB  # type: ignore[return-value]
    try:
        path = ctypes.util.find_library("CoreGraphics") or (
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        lib = ctypes.CDLL(path)
        lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        lib.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            _CGPoint,
            ctypes.c_uint32,
        ]
        lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        lib.CGWarpMouseCursorPosition.argtypes = [_CGPoint]
        lib.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        lib.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_bool,
        ]
        lib.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.CFRelease.argtypes = [ctypes.c_void_p]
        _CG_LIB = lib
    except Exception:
        _CG_LIB = False
        return None
    return lib  # type: ignore[return-value]


def max_cocoa_y(monitors: list[dict[str, Any]] | None) -> float:
    mons = monitors or []
    if not mons:
        return 1080.0
    return float(max(int(m.get("bottom", 0)) for m in mons))


def cocoa_point_to_cg(x: int, y: int, monitors: list[dict[str, Any]] | None = None) -> tuple[float, float]:
    """Convert AppKit global coords (origin bottom-left) to CGEvent coords (origin top-left)."""
    max_y = max_cocoa_y(monitors)
    return float(x), max_y - float(y)


def _post_event(lib: ctypes.CDLL, event: int) -> None:
    lib.CGEventPost(kCGHIDEventTap, event)
    lib.CFRelease(event)


def _post_mouse(lib: ctypes.CDLL, event_type: int, pt: _CGPoint, button: int) -> bool:
    ev = lib.CGEventCreateMouseEvent(None, event_type, pt, button)
    if not ev:
        return False
    _post_event(lib, ev)
    return True


def mouse_move(
    x: int,
    y: int,
    *,
    monitors: list[dict[str, Any]] | None = None,
    prompt_accessibility: bool = True,
) -> tuple[bool, str, dict[str, Any] | None]:
    ok_ax, err_ax, perm = _accessibility_denied(prompt=prompt_accessibility)
    if not ok_ax:
        return False, err_ax, perm
    lib = _cg_lib()
    if lib is None:
        return False, "CoreGraphics unavailable on this Mac", None
    cg_x, cg_y = cocoa_point_to_cg(x, y, monitors)
    pt = _CGPoint(cg_x, cg_y)
    lib.CGWarpMouseCursorPosition(pt)
    return True, "", None


def mouse_click(
    x: int,
    y: int,
    button: str = "left",
    *,
    monitors: list[dict[str, Any]] | None = None,
    prompt_accessibility: bool = True,
) -> tuple[bool, str, dict[str, Any] | None]:
    ok_ax, err_ax, perm = _accessibility_denied(prompt=prompt_accessibility)
    if not ok_ax:
        return False, err_ax, perm
    lib = _cg_lib()
    if lib is None:
        return False, "CoreGraphics unavailable on this Mac", None
    cg_x, cg_y = cocoa_point_to_cg(x, y, monitors)
    pt = _CGPoint(cg_x, cg_y)
    lib.CGWarpMouseCursorPosition(pt)
    btn = str(button or "left").lower()
    if btn == "right":
        down_t, up_t, mouse_btn = kCGEventRightMouseDown, kCGEventRightMouseUp, kCGMouseButtonRight
    else:
        down_t, up_t, mouse_btn = kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft
    if not _post_mouse(lib, down_t, pt, mouse_btn) or not _post_mouse(lib, up_t, pt, mouse_btn):
        _, err2, perm2 = _accessibility_denied(prompt=False)
        return False, err2 or "CGEvent mouse failed", perm2
    return True, "", None


def _clipboard_copy(text: str) -> tuple[bool, str]:
    try:
        proc = subprocess.Popen(
            ["pbcopy"],
            stdin=subprocess.PIPE,
            close_fds=True,
        )
        proc.communicate(text.encode("utf-8"), timeout=15)
        if proc.returncode == 0:
            return True, ""
    except Exception:
        pass
    try:
        import pyperclip

        pyperclip.copy(text)
        return True, ""
    except Exception as e:
        return False, f"clipboard failed: {e}"


def _post_key(lib: ctypes.CDLL, keycode: int, keydown: bool, flags: int = 0) -> bool:
    ev = lib.CGEventCreateKeyboardEvent(None, keycode, keydown)
    if not ev:
        return False
    if flags:
        lib.CGEventSetFlags(ev, flags)
    _post_event(lib, ev)
    return True


def _paste_cmd_v(lib: ctypes.CDLL) -> bool:
    """Command+V via CGEvent (matches Windows Ctrl+V semantics)."""
    if not _post_key(lib, kVK_Command, True):
        return False
    if not _post_key(lib, kVK_ANSI_V, True, kCGEventFlagMaskCommand):
        return False
    if not _post_key(lib, kVK_ANSI_V, False, kCGEventFlagMaskCommand):
        return False
    return _post_key(lib, kVK_Command, False)


_DARWIN_KEY_CODES: dict[str, int] = {
    "a": 0x00,
    "s": 0x01,
    "d": 0x02,
    "f": 0x03,
    "h": 0x04,
    "g": 0x05,
    "z": 0x06,
    "x": 0x07,
    "c": 0x08,
    "v": kVK_ANSI_V,
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "9": 0x19,
    "7": 0x1A,
    "8": 0x1C,
    "0": 0x1D,
    "o": 0x1F,
    "u": 0x20,
    "i": 0x22,
    "p": 0x23,
    "enter": kVK_Return,
    "return": kVK_Return,
    "space": kVK_Space,
    "esc": kVK_Escape,
    "escape": kVK_Escape,
    "tab": kVK_Tab,
}
_DARWIN_MODIFIER_CODES: dict[str, tuple[int, int]] = {
    "cmd": (kVK_Command, kCGEventFlagMaskCommand),
    "command": (kVK_Command, kCGEventFlagMaskCommand),
    "meta": (kVK_Command, kCGEventFlagMaskCommand),
}


def hotkey(keys: list[str], *, prompt_accessibility: bool = True) -> tuple[bool, str, dict[str, Any] | None]:
    ok_ax, err_ax, perm = _accessibility_denied(prompt=prompt_accessibility)
    if not ok_ax:
        return False, err_ax, perm
    lib = _cg_lib()
    if lib is None:
        return False, "CoreGraphics unavailable on this Mac", None
    clean = [str(k or "").strip().lower() for k in (keys or []) if str(k or "").strip()]
    if not clean:
        return False, "keys is required", None

    modifiers: list[int] = []
    flags = 0
    normal: list[int] = []
    unknown: list[str] = []
    for key in clean:
        if key in _DARWIN_MODIFIER_CODES:
            code, flag = _DARWIN_MODIFIER_CODES[key]
            modifiers.append(code)
            flags |= flag
        elif key in _DARWIN_KEY_CODES:
            normal.append(_DARWIN_KEY_CODES[key])
        else:
            unknown.append(key)
    if unknown:
        return False, f"unsupported key(s): {', '.join(unknown)}", None
    if not normal and not modifiers:
        return False, "keys is required", None

    for code in modifiers:
        if not _post_key(lib, code, True):
            return False, "CGEvent modifier keydown failed", None
    targets = normal or modifiers[-1:]
    for code in targets:
        if not _post_key(lib, code, True, flags):
            return False, "CGEvent keydown failed", None
        if not _post_key(lib, code, False, flags):
            return False, "CGEvent keyup failed", None
    for code in reversed(modifiers):
        if not _post_key(lib, code, False):
            return False, "CGEvent modifier keyup failed", None
    return True, "", None


def type_text(text: str, *, prompt_accessibility: bool = True) -> tuple[bool, str, dict[str, Any] | None]:
    ok_ax, err_ax, perm = _accessibility_denied(prompt=prompt_accessibility)
    if not ok_ax:
        return False, err_ax, perm
    payload = str(text or "")
    ok, err = _clipboard_copy(payload)
    if not ok:
        return False, err, None
    lib = _cg_lib()
    if lib is None:
        return False, "CoreGraphics unavailable on this Mac", None
    if not _paste_cmd_v(lib):
        _, err2, perm2 = _accessibility_denied(prompt=False)
        return False, err2 or "CGEvent keyboard failed", perm2
    return True, "", None
