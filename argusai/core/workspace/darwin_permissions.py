"""macOS Privacy (TCC) checks and user guidance for desktop automation."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Any

# Ventura+ deep links first, then legacy System Preferences URLs.
_SETTINGS_URLS: dict[str, tuple[str, ...]] = {
    "accessibility": (
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ),
    "screen_capture": (
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ),
    "listen_event": (
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    ),
}

# Prompting from osascript grants TCC to osascript, not ArgusAI, and the
# system dialog can look like a password prompt every time a tool runs.
_AX_PROMPT_ATTEMPTED = False

# IOHIDCheckAccess / IOHIDRequestTypeListenEvent (10.15+)
_kIOHIDRequestTypeListenEvent = 1
_kIOHIDAccessTypeGranted = 0
_kIOHIDAccessTypeDenied = 1


def accessibility_trusted() -> bool:
    """Whether this process is trusted for Accessibility (mouse/keyboard)."""
    if sys.platform != "darwin":
        return True
    try:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return False


def _ax_prompt_in_process() -> bool:
    """Ask TCC to prompt for *this* process (not osascript)."""
    try:
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        app = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        k_prompt = ctypes.c_void_p.in_dll(app, "kAXTrustedCheckOptionPrompt")
        k_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        cf.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        cf.CFDictionaryCreateMutable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        cf.CFDictionarySetValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        app.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        app.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        options = cf.CFDictionaryCreateMutable(None, 1, None, None)
        if not options:
            return accessibility_trusted()
        try:
            cf.CFDictionarySetValue(options, k_prompt, k_true)
            return bool(app.AXIsProcessTrustedWithOptions(options))
        finally:
            cf.CFRelease(options)
    except Exception:
        return accessibility_trusted()


def request_accessibility_prompt() -> bool:
    """Show the system dialog to add this app under Accessibility (once per process)."""
    global _AX_PROMPT_ATTEMPTED
    if sys.platform != "darwin":
        return True
    if accessibility_trusted():
        return True
    if _AX_PROMPT_ATTEMPTED:
        return False
    _AX_PROMPT_ATTEMPTED = True
    return _ax_prompt_in_process()


def screen_capture_granted() -> bool:
    """Whether Screen Recording permission is granted (macOS 10.15+)."""
    if sys.platform != "darwin":
        return True
    try:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        if not hasattr(lib, "CGPreflightScreenCaptureAccess"):
            return True
        lib.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(lib.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def request_screen_capture_prompt() -> bool:
    """Trigger the system Screen Recording permission prompt (once per install)."""
    if sys.platform != "darwin":
        return True
    try:
        lib = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        if not hasattr(lib, "CGRequestScreenCaptureAccess"):
            return screen_capture_granted()
        lib.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        return bool(lib.CGRequestScreenCaptureAccess())
    except Exception:
        return screen_capture_granted()


def listen_event_status() -> str:
    """``granted`` / ``denied`` / ``unknown`` for Input Monitoring (no prompt)."""
    if sys.platform != "darwin":
        return "granted"
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        iokit.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        iokit.IOHIDCheckAccess.restype = ctypes.c_uint32
        status = int(iokit.IOHIDCheckAccess(_kIOHIDRequestTypeListenEvent))
        if status == _kIOHIDAccessTypeGranted:
            return "granted"
        if status == _kIOHIDAccessTypeDenied:
            return "denied"
        return "unknown"
    except Exception:
        return "unknown"


def open_privacy_settings(pane: str = "accessibility") -> dict[str, Any]:
    """Open System Settings to a Privacy pane. Returns ``{ok, pane, url}``."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "not macOS"}
    key = str(pane or "accessibility").strip().lower()
    urls = _SETTINGS_URLS.get(key) or _SETTINGS_URLS["accessibility"]
    last_err = ""
    for url in urls:
        try:
            subprocess.run(["open", url], check=True, timeout=10)
            return {"ok": True, "pane": key, "settings_url": url}
        except Exception as e:
            last_err = str(e)
    return {"ok": False, "error": last_err or "failed to open System Settings", "pane": key}


def _permission_row(
    perm_id: str,
    *,
    label: str,
    granted: bool,
    hint: str,
    required_for: list[str],
) -> dict[str, Any]:
    urls = _SETTINGS_URLS.get(perm_id, ())
    return {
        "id": perm_id,
        "label": label,
        "granted": granted,
        "hint": hint,
        "required_for": required_for,
        "settings_url": urls[0] if urls else "",
        "settings_urls": list(urls),
    }


def check_all() -> dict[str, Any]:
    """Snapshot of desktop-related macOS permissions for UI and tool results."""
    if sys.platform != "darwin":
        return {"ok": True, "platform": sys.platform, "permissions": [], "missing": []}

    ax = accessibility_trusted()
    scr = screen_capture_granted()
    listen = listen_event_status()
    perms = [
        _permission_row(
            "accessibility",
            label="辅助功能",
            granted=ax,
            hint="控制鼠标与键盘（desktop_click / desktop_type）需要开启；勾选 ArgusAI 或启动它的 Terminal/Python。授权框只应弹出一次，请不要输入管理员密码。",
            required_for=["desktop_click", "desktop_type"],
        ),
        _permission_row(
            "screen_capture",
            label="屏幕录制",
            granted=scr,
            hint="桌面截图（desktop_observe 带 screenshot）需要开启；勾选 ArgusAI 或启动它的 Terminal/Python。",
            required_for=["desktop_observe"],
        ),
        _permission_row(
            "listen_event",
            label="输入监控",
            # Unknown is treated as granted so the banner does not nag on
            # versions that do not expose IOHIDCheckAccess.
            granted=listen != "denied",
            hint="若系统额外提示「输入监控」，请在同一隐私页面一并开启（部分 macOS 版本需要）。不会主动弹出授权框。",
            required_for=["desktop_type"],
        ),
    ]
    missing = [p["id"] for p in perms if not p.get("granted")]
    lines = []
    if "accessibility" in missing:
        lines.append("未授予「辅助功能」：无法点击或输入。")
    if "screen_capture" in missing:
        lines.append("未授予「屏幕录制」：无法截取桌面。")
    if "listen_event" in missing:
        lines.append("未授予「输入监控」：键盘输入可能被系统拦截。")
    return {
        "ok": True,
        "platform": "darwin",
        "all_granted": not missing,
        "permissions": perms,
        "missing": missing,
        "user_message": " ".join(lines) if lines else "",
    }


def permission_denied_payload(perm_id: str, *, prompt_attempted: bool = False) -> dict[str, Any]:
    """Structured error attachment for tool / GUI consumers."""
    snap = check_all()
    row = next((p for p in snap.get("permissions", []) if p.get("id") == perm_id), None)
    label = (row or {}).get("label") or perm_id
    return {
        "id": perm_id,
        "label": label,
        "granted": False,
        "prompt_attempted": prompt_attempted,
        "settings_url": (row or {}).get("settings_url") or "",
        "hint": (row or {}).get("hint") or "",
        "user_message": (
            f"需要 macOS「{label}」权限。"
            + (" 已尝试弹出系统授权框；" if prompt_attempted else "")
            + " 请在 系统设置 → 隐私与安全性 中开启本应用（不是 osascript），无需输入密码，然后重试。"
        ),
    }


def ensure_accessibility(*, prompt: bool = True) -> tuple[bool, dict[str, Any] | None]:
    if accessibility_trusted():
        return True, None
    if prompt:
        request_accessibility_prompt()
    if accessibility_trusted():
        return True, None
    return False, permission_denied_payload("accessibility", prompt_attempted=prompt)


def ensure_screen_capture(*, prompt: bool = True) -> tuple[bool, dict[str, Any] | None]:
    if screen_capture_granted():
        return True, None
    if prompt:
        request_screen_capture_prompt()
    if screen_capture_granted():
        return True, None
    return False, permission_denied_payload("screen_capture", prompt_attempted=prompt)


def confirm_hint_for_tool(tool_name: str) -> str:
    """Extra confirm-card body text on macOS."""
    if sys.platform != "darwin":
        return ""
    snap = check_all()
    missing = list(snap.get("missing") or [])
    if tool_name in ("desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey"):
        if "accessibility" in missing:
            return (
                "【macOS】尚未授予「辅助功能」。确认后若仍失败，请到 "
                "系统设置 → 隐私与安全性 → 辅助功能，勾选本应用或 Terminal/Python"
                "（不要勾选 osascript）。这不是管理员密码。"
            )
    if tool_name == "desktop_observe":
        if "screen_capture" in missing:
            return (
                "【macOS】若需截图，请先在 系统设置 → 隐私与安全性 → 屏幕录制 中授权；"
                "仅查看窗口列表可不截图。"
            )
    return ""
