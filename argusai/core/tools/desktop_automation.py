"""Desktop observation and input tools (OS-level, permission-gated)."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import sys

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

from .common import *


def _session_id(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("_session_id") or "default")


def du_uia_ready() -> bool:
    """True when Windows UI Automation is importable/usable on this host."""
    try:
        from argusai.core.workspace import desktop_uia as du

        return bool(du.uia_available().get("ok"))
    except Exception:
        return False


def _image_dims(pixels: Any) -> tuple[int, int]:
    if isinstance(pixels, dict):
        return int(pixels.get("width") or 0), int(pixels.get("height") or 0)
    if isinstance(pixels, (list, tuple)) and len(pixels) >= 2:
        try:
            return int(pixels[0]), int(pixels[1])
        except (TypeError, ValueError):
            return 0, 0
    return 0, 0


def _screenshot_grounding(
    cap: dict[str, Any],
    monitors: list[dict[str, Any]],
    vdesk: dict[str, Any],
    hwnd_i: int,
    monitor: int,
) -> dict[str, Any] | None:
    """Map captured-image pixels to desktop_click coordinates.

    ``desktop_x = origin_x + image_x * scale_x`` (same for y). ``scale`` is the
    captured surface's desktop span divided by the saved image width/height, so
    it is correct regardless of the ``screenshot_max_width`` downscale.
    """
    out_w, out_h = _image_dims(cap.get("output_pixels"))
    if out_w <= 0 or out_h <= 0:
        return None
    if hwnd_i > 0:
        bbox = cap.get("capture_bbox") or {}
        ox, oy = int(bbox.get("left", 0)), int(bbox.get("top", 0))
        sw = int(bbox.get("right", 0)) - ox
        sh = int(bbox.get("bottom", 0)) - oy
    elif 0 <= monitor < len(monitors):
        m = monitors[monitor]
        ox, oy = int(m.get("left", 0)), int(m.get("top", 0))
        sw, sh = int(m.get("width", 0)), int(m.get("height", 0))
    else:
        ox, oy = int(vdesk.get("left", 0)), int(vdesk.get("top", 0))
        sw, sh = int(vdesk.get("width", 0)), int(vdesk.get("height", 0))
    if sw <= 0 or sh <= 0:
        return None
    return {
        "origin_x": ox,
        "origin_y": oy,
        "image_width": out_w,
        "image_height": out_h,
        "scale_x": round(sw / out_w, 6),
        "scale_y": round(sh / out_h, 6),
        "formula": "desktop_x = origin_x + image_x * scale_x; desktop_y = origin_y + image_y * scale_y",
    }


def _ground_image_point(image_x: Any, image_y: Any, grounding: dict[str, Any]) -> tuple[int, int]:
    """Convert an image-pixel point to desktop coordinates via a grounding map."""
    gx = float(grounding.get("origin_x", 0)) + float(image_x) * float(grounding.get("scale_x", 1.0))
    gy = float(grounding.get("origin_y", 0)) + float(image_y) * float(grounding.get("scale_y", 1.0))
    return int(round(gx)), int(round(gy))


def _register_windows_notification_identity(app_id: str) -> bool:
    """Register an unpackaged desktop identity so Windows does not drop toasts."""
    try:
        import winreg

        key_path = rf"Software\Classes\AppUserModelId\{app_id}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "ArgusAI")
            winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
            executable = str(Path(sys.executable).resolve())
            if executable:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, executable)
        return True
    except (OSError, ValueError):
        return False


def desktop_notify(
    api: Any,
    title: str = "ArgusAI",
    message: str = "",
    level: str = "info",
    **kwargs: Any,
) -> dict[str, Any]:
    """Notify a GUI user only while ArgusAI is hidden or unfocused.

    Calls made outside an assistant turn (for example the runtime attention
    notifier) omit ``_session_id`` and manage their own background checks.
    """
    session_id = str(kwargs.get("_session_id") or "").strip()
    if session_id:
        sessions = getattr(api, "_assistant_sessions", None) or {}
        session = sessions.get(session_id) if isinstance(sessions, dict) else None
        source = str(
            getattr(session, "_current_turn_source", "") or ""
        ).strip().lower()
        if source == "channel":
            return {"ok": True, "sent": False, "reason": "channel_turn"}
        needs_attention = getattr(api, "_app_needs_attention", None)
        if not callable(needs_attention):
            return {
                "ok": True,
                "sent": False,
                "reason": "attention_state_unavailable",
            }
        if not bool(needs_attention()):
            return {"ok": True, "sent": False, "reason": "app_focused"}

    settings = getattr(getattr(api, "settings_mgr", None), "get", lambda: None)()
    if settings is not None and not bool(getattr(settings, "assistant_desktop_notifications", True)):
        return {"ok": True, "sent": False, "reason": "notifications_disabled"}

    safe_title = str(title or "ArgusAI").strip()[:120] or "ArgusAI"
    safe_message = str(message or "").strip()[:1000]
    if not safe_message:
        return {"ok": False, "error": "message is required"}
    severity = str(level or "info").strip().lower()
    if severity not in {"info", "success", "warning", "error"}:
        severity = "info"

    try:
        if sys.platform.startswith("win"):
            # Windows Runtime toast APIs are built in to supported Windows
            # versions. Pass text through environment variables so the user
            # message is never interpreted as PowerShell source.
            app_id = "ArgusAI.Desktop"
            registered = _register_windows_notification_identity(app_id)
            if registered:
                script = (
                    "Add-Type -AssemblyName System.Runtime.WindowsRuntime; "
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; "
                    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null; "
                    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                    "$t = [System.Security.SecurityElement]::Escape($env:ARGUSAI_NOTIFY_TITLE); "
                    "$m = [System.Security.SecurityElement]::Escape($env:ARGUSAI_NOTIFY_MESSAGE); "
                    "$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual></toast>\"); "
                    "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:ARGUSAI_NOTIFY_APP_ID).Show($toast)"
                )
            else:
                # The legacy tray balloon remains available on systems where
                # the per-user desktop identity cannot be registered.
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "Add-Type -AssemblyName System.Drawing; "
                    "$n = New-Object System.Windows.Forms.NotifyIcon; "
                    "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                    "$n.BalloonTipTitle = $env:ARGUSAI_NOTIFY_TITLE; "
                    "$n.BalloonTipText = $env:ARGUSAI_NOTIFY_MESSAGE; "
                    "$n.Visible = $true; "
                    "$n.ShowBalloonTip(5000); "
                    "Start-Sleep -Milliseconds 5200; "
                    "$n.Dispose()"
                )
            env = dict(
                os.environ,
                ARGUSAI_NOTIFY_TITLE=safe_title,
                ARGUSAI_NOTIFY_MESSAGE=safe_message,
                ARGUSAI_NOTIFY_APP_ID=app_id,
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                **quiet_subprocess_kwargs(),
            )
        elif sys.platform == "darwin":
            script = 'on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run'
            proc = subprocess.run(
                ["osascript", "-e", script, safe_title, safe_message],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            notifier = shutil.which("notify-send")
            if not notifier:
                return {"ok": False, "error": "desktop notifications require notify-send on this platform"}
            proc = subprocess.run(
                [notifier, "--app-name=ArgusAI", safe_title, safe_message],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"desktop notification failed: {exc}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "desktop notification command failed").strip()[:500],
        }
    return {"ok": True, "sent": True, "title": safe_title, "level": severity}


def _windows_start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    for raw in (
        os.environ.get("APPDATA", ""),
        os.environ.get("PROGRAMDATA", ""),
    ):
        if not raw:
            continue
        base = Path(raw)
        if base.name.lower() == "roaming":
            dirs.append(base / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        else:
            dirs.append(base / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [p for p in dirs if p.exists()]


def _windows_app_path(exe_name: str) -> Path | None:
    try:
        import winreg
    except Exception:
        return None
    subkey = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    roots = (
        (winreg.HKEY_CURRENT_USER, 0),
        (winreg.HKEY_LOCAL_MACHINE, 0),
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_32KEY", 0)),
        (winreg.HKEY_LOCAL_MACHINE, getattr(winreg, "KEY_WOW64_64KEY", 0)),
    )
    for root, flag in roots:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | flag) as key:
                value, _ = winreg.QueryValueEx(key, "")
                path = Path(str(value)).expanduser()
                if path.exists():
                    return path
        except OSError:
            continue
    return None


def _windows_find_launch_target(name: str, executable_path: str = "") -> tuple[Path | str | None, str]:
    text = str(executable_path or "").strip() or str(name or "").strip()
    if not text:
        return None, "name is required"
    expanded = os.path.expandvars(os.path.expanduser(text)).strip('"')
    path = Path(expanded)
    if path.exists():
        return path, "explicit_path"

    exe_names = [Path(text).name]
    if not exe_names[0].lower().endswith((".exe", ".bat", ".cmd", ".com", ".lnk")):
        exe_names.insert(0, exe_names[0] + ".exe")

    for exe in exe_names:
        found = shutil.which(exe)
        if found:
            return Path(found), "path"
        reg = _windows_app_path(exe)
        if reg:
            return reg, "app_paths_registry"

    needle = Path(text).stem.lower()
    for root in _windows_start_menu_dirs():
        try:
            for item in root.rglob("*.lnk"):
                stem = item.stem.lower()
                if needle in stem or stem in needle:
                    return item, "start_menu_shortcut"
        except OSError:
            continue
    return None, "not_found"


def desktop_launch_app(
    api: Any,
    name: str = "",
    executable_path: str = "",
    args: list | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Launch a local desktop app by name/path without running arbitrary shell commands."""
    app_name = str(name or executable_path or "").strip()
    argv = [str(x) for x in (args or []) if str(x).strip()]
    try:
        if sys.platform.startswith("win"):
            target, source = _windows_find_launch_target(app_name, executable_path)
            if target is None:
                return {
                    "ok": False,
                    "error": f"app not found: {app_name}",
                    "lookup": source,
                    "fallback": "Use desktop_hotkey(['win']) + desktop_type(app name) + desktop_hotkey(['enter']).",
                }
            if isinstance(target, Path) and target.suffix.lower() == ".lnk":
                os.startfile(str(target))  # type: ignore[attr-defined]
                return {"ok": True, "name": app_name, "path": str(target), "source": source, "method": "os.startfile"}
            proc = subprocess.Popen([str(target), *argv], close_fds=True)
            return {
                "ok": True,
                "name": app_name,
                "path": str(target),
                "source": source,
                "pid": proc.pid,
                "method": "subprocess.Popen",
            }
        if sys.platform == "darwin":
            app = str(executable_path or name or "").strip()
            if not app:
                return {"ok": False, "error": "name is required"}
            cmd = ["open"]
            path = Path(os.path.expandvars(os.path.expanduser(app))).expanduser()
            if path.exists():
                cmd.append(str(path))
            else:
                cmd.extend(["-a", app])
            if argv:
                cmd.append("--args")
                cmd.extend(argv)
            proc = subprocess.Popen(cmd, close_fds=True)
            return {"ok": True, "name": app, "method": "open", "pid": proc.pid}
        return {"ok": False, "error": "desktop app launching is not implemented on this OS"}
    except Exception as e:
        return {"ok": False, "error": str(e), "name": app_name}


def desktop_observe(
    api: Any,
    mode: str = "foreground",
    screenshot: bool = False,
    screenshot_path: str = "",
    monitor: int = -1,
    hwnd: int = 0,
    screenshot_max_width: int = -1,
    screenshot_quality: int = 90,
    **kwargs: Any,
) -> dict[str, Any]:
    """Observe the OS desktop (windows, monitors, optional screenshot). Use before desktop_click/type.

    Windows: virtual-desktop coordinates; ``monitor=-1`` captures all displays.
    Pass ``hwnd`` with ``screenshot=true`` to capture one window instead of a display.
    macOS: ``screencapture`` per display; observe/click coords are Cocoa bottom-left global;
    click/type need Accessibility. Linux: metadata placeholders only.
    Screenshots default to JPEG. ``screenshot_max_width=-1`` auto-fits the
    captured surface (one monitor, or the stitched virtual desktop) capped at
    3840. Use ``0`` for native pixels, or raise ``screenshot_quality`` (1-100)
    / save as ``.png`` when text looks soft.
    """
    from argusai.core.workspace import desktop_control as dc

    mode = str(mode or "foreground").strip().lower()
    monitors = dc.list_monitors()
    vdesk = dc.virtual_desktop_bounds(monitors)
    macos_perms: dict[str, Any] | None = None
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_permissions as dp

        macos_perms = dp.check_all()
    out: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "monitors": monitors,
        "virtual_desktop": vdesk,
        "coordinate_hint": (
            "macOS：坐标为 Cocoa 全局坐标（左下角为原点，y 向上）；desktop_click 与 monitors[] 一致。"
            if monitors and monitors[0].get("coordinate_system") == "macos_cocoa_bottom_left"
            else (
                "鼠标坐标使用虚拟桌面坐标系：原点不一定在左上角主屏，副屏可能是负坐标。"
                f" 当前共 {vdesk.get('monitor_count', len(monitors))} 块屏，"
                f"范围 left={vdesk.get('left')} top={vdesk.get('top')} "
                f"width={vdesk.get('width')} height={vdesk.get('height')}。"
                " 优先用 windows[].hwnd + desktop_uia_find/desktop_uia_action 定位控件；"
                "像素点击仅作兜底（desktop_click hwnd 相对坐标）。"
            )
        ),

        "sensitive_warning": "桌面截图/窗口标题可能包含敏感信息，仅供当前审计上下文使用。",
    }
    if macos_perms is not None:
        out["macos_permissions"] = macos_perms
        if macos_perms.get("user_message"):
            out["permission_notice"] = macos_perms["user_message"]
    if mode in ("monitors", "all"):
        pass  # already included
    if mode in ("foreground", "all"):
        out["foreground"] = dc.foreground_window()
    if mode in ("windows", "all"):
        out["windows"] = dc.list_windows()
    if mode in ("programs", "all"):
        out["programs"] = dc.running_programs_summary()
    if screenshot:
        root = Path(_session_sandbox_root(api, kwargs))
        rel = str(screenshot_path or "").strip() or f"_media/desktop-{int(time.time())}.jpg"
        target = root / rel
        hwnd_i = int(hwnd or 0)
        # -1 = auto from the surface being captured (one monitor, one window,
        # or the stitched virtual desktop). Cap at 3840 so 8K dumps stay small.
        # Coerce once: a model may pass "", null or a word here, and a bare int()
        # would raise straight out of the tool instead of taking the default.
        requested_width = _safe_int(screenshot_max_width, -1, -1, 7680)
        if requested_width < 0:
            widths = [int(item.get("width") or 0) for item in monitors]
            widest = max(widths) if widths else 1920
            surface_w = widest
            if hwnd_i <= 0 and int(monitor) < 0:
                surface_w = int(vdesk.get("width") or 0) or sum(widths) or widest
            elif hwnd_i <= 0 and 0 <= int(monitor) < len(monitors):
                surface_w = int(monitors[int(monitor)].get("width") or 0) or widest
            max_w = min(max(surface_w, 1920), 3840)
        else:
            max_w = requested_width
        quality = _safe_int(screenshot_quality, 90, 1, 100)
        if hwnd_i > 0 and sys.platform.startswith("win"):
            try:
                from argusai.core.workspace import desktop_uia as du

                assess = du.assess_window_uia(hwnd_i)
                if assess.get("ok"):
                    out["window_action_hints"] = assess
                    if assess.get("warning"):
                        out["uia_warning"] = assess["warning"]
            except Exception as exc:
                out["window_action_hints"] = {"ok": False, "error": str(exc)}
        if hwnd_i > 0:
            cap = dc.capture_window_screenshot(
                target,
                hwnd=hwnd_i,
                max_width=max_w,
                jpeg_quality=quality,
            )
        else:
            mon_arg: int | None = None
            if int(monitor) >= 0:
                mon_arg = int(monitor)
            cap = dc.capture_screenshot(
                target,
                monitor=mon_arg,
                max_width=max_w,
                jpeg_quality=quality,
            )
        out["screenshot"] = cap
        out["screenshot_max_width"] = max_w
        out["screenshot_quality"] = quality
        # Vision fallback grounding: when UIA can't locate a control, the model
        # reads this screenshot, outputs the target's pixel position, and
        # converts it to a desktop_click coordinate with this mapping. Windows
        # only (macOS click coords are Cocoa bottom-left, handled elsewhere).
        if cap.get("ok") and sys.platform.startswith("win"):
            grounding = _screenshot_grounding(cap, monitors, vdesk, hwnd_i, int(monitor))
            if grounding:
                out["screenshot_grounding"] = grounding
            if out.get("uia_warning") or not du_uia_ready():
                out["vision_fallback"] = (
                    "该窗口的 UIA 不可用或不完整（部分 Electron/游戏/自绘程序如此）。"
                    "请改用视觉兜底：观察随附截图，识别目标控件，输出其在图像中的像素坐标 (image_x, image_y)，"
                    "再用 screenshot_grounding 换算为桌面坐标后调用 desktop_click(x, y)；"
                    "或直接调用 desktop_click(image_x=…, image_y=…, grounding=screenshot_grounding)。"
                )
        if cap.get("ok"):
            if cap.get("screenshot_looks_black") or cap.get("warning"):
                out["screenshot_looks_black"] = bool(cap.get("screenshot_looks_black"))
                out["screenshot_warning"] = cap.get("warning") or (
                    "Window screenshot looks black/empty; use monitor= capture for GPU/Chromium apps."
                )
            shot_path = Path(cap.get("path") or target)
            out["screenshot_path"] = str(shot_path.name)
            if "/" in rel or "\\" in rel:
                out["screenshot_path"] = rel.replace("\\", "/")
            try:
                data = shot_path.read_bytes()
                if len(data) <= 4_000_000:
                    mime = str(cap.get("mime") or "image/jpeg")
                    out["_assistant_attachments"] = [{
                        "type": "image",
                        "mime": mime,
                        "name": shot_path.name,
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }]
                    out["attached_to_model"] = True
                else:
                    out["attached_to_model"] = False
                    out["attachment_skipped_reason"] = (
                        f"desktop screenshot too large for model attachment ({len(data)} bytes > 4000000)"
                    )
            except Exception as e:
                out["attached_to_model"] = False
                out["attachment_skipped_reason"] = f"failed to attach desktop screenshot: {e}"
    return out


def desktop_focus_window(api: Any, hwnd: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Focus/raise a window by HWND from desktop_observe windows[]."""
    del api, kwargs
    from argusai.core.workspace import desktop_control as dc

    return dc.focus_window(int(hwnd or 0))


def desktop_action_session_start(
    api: Any,
    minutes: int = 5,
    scope: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Deprecated no-op. Do not call; use desktop_click / desktop_mouse_move directly."""
    from argusai.core.workspace import desktop_control as dc

    info = dc.start_desktop_action_session(_session_id(kwargs), minutes=_safe_int(minutes, 5, 1, 30), scope=scope)
    msg = str(info.get("message") or "桌面鼠标/键盘已不再需要限时授权，可直接点击或移动。")
    if sys.platform == "darwin":
        from argusai.core.workspace import darwin_permissions as dp

        perms = dp.check_all()
        info["macos_permissions"] = perms
        if perms.get("missing"):
            hint = dp.confirm_hint_for_tool("desktop_click")
            if hint:
                msg += f" {hint}"
    info["message"] = msg
    return info


def desktop_action_session_stop(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility no-op. There is no timed desktop authorization session to stop."""
    from argusai.core.workspace import desktop_control as dc

    return dc.stop_desktop_action_session(_session_id(kwargs))


def desktop_action_session_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report that timed desktop authorization is retired and not required."""
    from argusai.core.workspace import desktop_control as dc

    return dc.desktop_session_status(_session_id(kwargs))


def desktop_mouse_move(
    api: Any,
    x: int = 0,
    y: int = 0,
    hwnd: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Move the OS mouse cursor without clicking.

    When ``hwnd`` is set, ``x``/``y`` are treated as coordinates relative to
    that window's top-left (after focusing it).
    """
    from argusai.core.workspace import desktop_control as dc

    hwnd_i = int(hwnd or 0)
    abs_x, abs_y = int(x), int(y)
    focus_info: dict[str, Any] | None = None
    if hwnd_i > 0:
        focus_info = dc.focus_window(hwnd_i)
        if not focus_info.get("ok"):
            return {**focus_info, "moved": False}
        abs_x = int(focus_info.get("left", 0)) + int(x)
        abs_y = int(focus_info.get("top", 0)) + int(y)
    result = dc.mouse_move(_session_id(kwargs), x=abs_x, y=abs_y)
    if focus_info is not None and isinstance(result, dict):
        result["hwnd"] = hwnd_i
        result["window_relative"] = {"x": int(x), "y": int(y)}
        result["screen"] = {"x": abs_x, "y": abs_y}
        result["focus"] = focus_info
    return result


def desktop_click(
    api: Any,
    x: int = 0,
    y: int = 0,
    button: str = "left",
    hwnd: int = 0,
    image_x: Any = None,
    image_y: Any = None,
    grounding: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Click at OS screen coordinates.

    When ``hwnd`` is set, ``x``/``y`` are treated as coordinates relative to
    that window's top-left (after focusing it).

    Vision fallback (UIA unavailable): pass ``image_x``/``image_y`` (the target's
    pixel position in a desktop_observe screenshot) together with that call's
    ``screenshot_grounding`` dict, and the tool converts them to absolute
    desktop coordinates for you.
    """
    from argusai.core.workspace import desktop_control as dc

    hwnd_i = int(hwnd or 0)
    if image_x is not None and image_y is not None and isinstance(grounding, dict) and grounding:
        try:
            x, y = _ground_image_point(image_x, image_y, grounding)
            hwnd_i = 0  # grounded coords are already absolute desktop coords
        except (TypeError, ValueError):
            return {"ok": False, "clicked": False, "error": "invalid image_x/image_y/grounding"}
    abs_x, abs_y = int(x), int(y)
    focus_info: dict[str, Any] | None = None
    if hwnd_i > 0:
        focus_info = dc.focus_window(hwnd_i)
        if not focus_info.get("ok"):
            return {**focus_info, "clicked": False}
        abs_x = int(focus_info.get("left", 0)) + int(x)
        abs_y = int(focus_info.get("top", 0)) + int(y)
    result = dc.mouse_click(_session_id(kwargs), x=abs_x, y=abs_y, button=button)
    if focus_info is not None and isinstance(result, dict):
        result["hwnd"] = hwnd_i
        result["window_relative"] = {"x": int(x), "y": int(y)}
        result["screen"] = {"x": abs_x, "y": abs_y}
        result["focus"] = focus_info
    return result


def desktop_type(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Type text into the focused desktop app via clipboard paste."""
    from argusai.core.workspace import desktop_control as dc

    return dc.type_text(_session_id(kwargs), text=str(text or ""))


def desktop_hotkey(api: Any, keys: list | str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Send a hotkey chord.

    Prefer ``keys=["ctrl","a"]`` / ``keys=["enter"]``. A string like ``\"enter\"``
    or ``\"ctrl+a\"`` is also accepted (do not character-split named keys).
    """
    from argusai.core.workspace import desktop_control as dc

    # Models sometimes send `key` (singular) instead of `keys`.
    if keys is None and kwargs.get("key") is not None:
        keys = kwargs.get("key")
    # Pass through raw value — normalize_hotkey_keys handles str vs list.
    # Never list("enter") here (that becomes e,n,t,e,r).
    return dc.hotkey(_session_id(kwargs), keys=keys)


def desktop_uia_find(
    api: Any,
    hwnd: int = 0,
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
    limit: int = 30,
    include_offscreen: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Find Windows UI Automation controls under a window (semantic tree, not pixels).

    Prefer this over guessing click coordinates when a control has a name /
    automation_id / type (edit, button, listitem, …). Use hwnd from
    desktop_observe windows[].
    """
    del api, kwargs
    from argusai.core.workspace import desktop_uia as du

    return du.find_elements(
        hwnd=int(hwnd or 0),
        name=str(name or ""),
        name_contains=str(name_contains or ""),
        automation_id=str(automation_id or ""),
        control_type=str(control_type or ""),
        limit=_safe_int(limit, 30, 1, 80),
        include_offscreen=bool(include_offscreen),
    )


def desktop_uia_action(
    api: Any,
    action: str = "focus",
    hwnd: int = 0,
    runtime_id: str = "",
    name: str = "",
    name_contains: str = "",
    automation_id: str = "",
    control_type: str = "",
    value: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Act on a UIA control: focus | invoke | set_value | select | toggle | click.

    ``click`` presses the element's bounding-rect center (needed when Chromium/QQ
    ``invoke`` returns ok but does not open the hit).
    """
    from argusai.core.workspace import desktop_uia as du

    sid = _session_id(kwargs)
    return du.element_action(
        action=str(action or "focus"),
        hwnd=int(hwnd or 0),
        runtime_id=str(runtime_id or ""),
        name=str(name or ""),
        name_contains=str(name_contains or ""),
        automation_id=str(automation_id or ""),
        control_type=str(control_type or ""),
        value=str(value or ""),
        session_id=sid,
    )
