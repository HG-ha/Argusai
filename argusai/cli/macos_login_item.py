"""macOS Login Items via SMAppService (stdlib ctypes, no PyObjC).

Packaged ``ArgusAI.app`` on macOS 13+ registers the main app with
``SMAppService.mainAppService`` so it appears under System Settings →
General → Login Items. Unpackaged / older macOS / cron-only units keep
using a user LaunchAgent in ``service_install``.
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
from ctypes import POINTER, byref, c_byte, c_char_p, c_long, c_uint32, c_void_p
from pathlib import Path
from typing import Any

APP_BUNDLE_ID = "com.argusai.app"
LOGIN_ITEMS_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
)

STATUS_NOT_REGISTERED = 0
STATUS_ENABLED = 1
STATUS_REQUIRES_APPROVAL = 2
STATUS_NOT_FOUND = 3

_STATUS_NAMES = {
    STATUS_NOT_REGISTERED: "not_registered",
    STATUS_ENABLED: "enabled",
    STATUS_REQUIRES_APPROVAL: "requires_approval",
    STATUS_NOT_FOUND: "not_found",
}

_kCurrentProcess = 2
_kProcessDictionaryIncludeAllInformationMask = 0xFFFFFFFF
_kCFStringEncodingUTF8 = 0x08000100


def status_name(code: int) -> str:
    return _STATUS_NAMES.get(int(code), "unknown")


def macos_major() -> int:
    ver = platform.mac_ver()[0] if sys.platform == "darwin" else ""
    try:
        return int((ver or "0").split(".")[0])
    except ValueError:
        return 0


def app_bundle_path(start: str | Path | None = None) -> Path | None:
    """Return the enclosing ``*.app`` bundle, or None."""
    candidates: list[Path] = []
    if start:
        candidates.append(Path(start))
    try:
        from argusai.core.infra.runtime_exec import frozen_self_executable

        candidates.append(Path(frozen_self_executable()))
    except Exception:
        pass
    if sys.argv:
        candidates.append(Path(str(sys.argv[0] or "")))
    candidates.append(Path(sys.executable or ""))
    seen: set[str] = set()
    for raw in candidates:
        try:
            path = raw.expanduser()
            if not path.as_posix() or path.as_posix() in seen:
                continue
            seen.add(path.as_posix())
            for node in (path, *path.parents):
                if node.suffix == ".app" and (node / "Contents" / "MacOS").is_dir():
                    return node.resolve()
        except OSError:
            continue
    return None


def available_for_host(*, cron_only: bool = False) -> bool:
    """True when the full host should register a Login Item instead of a LaunchAgent."""
    if cron_only or sys.platform != "darwin" or macos_major() < 13:
        return False
    if app_bundle_path() is None:
        return False
    return _main_app_service() is not None


def login_item_status() -> dict[str, Any]:
    """Read ``SMAppService.mainAppService.status`` (safe off-macOS)."""
    service = _main_app_service()
    if service is None:
        return {
            "ok": True,
            "available": False,
            "status": "unavailable",
            "status_code": -1,
            "registered": False,
            "enabled": False,
            "requires_approval": False,
            "bundle": str(app_bundle_path() or ""),
        }
    code = _service_status(service)
    name = status_name(code)
    return {
        "ok": True,
        "available": True,
        "status": name,
        "status_code": code,
        "registered": name in {"enabled", "requires_approval"},
        "enabled": name == "enabled",
        "requires_approval": name == "requires_approval",
        "bundle": str(app_bundle_path() or ""),
    }


def register_main_app() -> dict[str, Any]:
    """Register the current ``.app`` as a Login Item."""
    service = _main_app_service()
    if service is None:
        return {"ok": False, "error": "SMAppService is not available in this runtime"}
    err = c_void_p()
    ok = bool(
        _msg(
            service,
            "registerAndReturnError:",
            byref(err),
            restype=c_byte,
            extra_argtypes=(POINTER(c_void_p),),
        )
    )
    status = login_item_status()
    if status.get("registered"):
        return {"ok": True, **status, "registered_now": ok}
    detail = _nserror_text(err) or "SMAppService.register failed"
    return {"ok": False, "error": detail, **status}


def unregister_main_app() -> dict[str, Any]:
    """Remove the Login Item. Missing / already-off is success."""
    service = _main_app_service()
    if service is None:
        return {"ok": True, "removed": False, "status": "unavailable"}
    before = login_item_status()
    if not before.get("registered"):
        return {"ok": True, "removed": False, **before}
    err = c_void_p()
    _msg(
        service,
        "unregisterAndReturnError:",
        byref(err),
        restype=c_byte,
        extra_argtypes=(POINTER(c_void_p),),
    )
    after = login_item_status()
    removed = not after.get("registered")
    if not removed:
        detail = _nserror_text(err)
        return {
            "ok": False,
            "removed": False,
            "error": detail or "SMAppService.unregister failed",
            **after,
        }
    return {"ok": True, "removed": True, **after}


def open_login_items_settings() -> dict[str, Any]:
    """Open System Settings → General → Login Items."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "macOS only"}
    service_cls = _smappservice_class()
    if service_cls is not None:
        try:
            _msg(service_cls, "openSystemSettingsLoginItems", restype=None)
            return {"ok": True, "method": "SMAppService"}
        except Exception:
            pass
    try:
        subprocess.run(["open", LOGIN_ITEMS_SETTINGS_URL], check=False)
        return {"ok": True, "method": "url"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def launched_hidden_at_login() -> bool:
    """True when Launch Services opened this process hidden as a login item."""
    if sys.platform != "darwin":
        return False
    return _process_is_hidden() or _apple_event_is_login_item()


def open_app_hidden(bundle: Path | None = None) -> dict[str, Any]:
    """``open -a`` the .app with ``--gui --tray`` (activates an existing copy)."""
    path = bundle or app_bundle_path()
    if path is None:
        return {"ok": False, "error": "no .app bundle"}
    proc = subprocess.run(
        ["open", "-a", str(path), "--args", "--gui", "--tray"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": err, "bundle": str(path)}
    return {"ok": True, "bundle": str(path)}


# --- Objective-C / CoreFoundation -------------------------------------------------


def _objc() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    try:
        lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        lib.objc_getClass.restype = c_void_p
        lib.objc_getClass.argtypes = [c_char_p]
        lib.sel_registerName.restype = c_void_p
        lib.sel_registerName.argtypes = [c_char_p]
        return lib
    except OSError:
        return None


def _load_framework(name: str) -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    path = f"/System/Library/Frameworks/{name}.framework/{name}"
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None


def _smappservice_class() -> int | None:
    if _load_framework("ServiceManagement") is None:
        return None
    objc = _objc()
    if objc is None:
        return None
    cls = objc.objc_getClass(b"SMAppService")
    return int(cls) if cls else None


def _main_app_service() -> int | None:
    cls = _smappservice_class()
    if not cls:
        return None
    svc = _msg(cls, "mainAppService")
    return int(svc) if svc else None


def _service_status(service: int) -> int:
    raw = _msg(service, "status", restype=c_long)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _msg(
    obj: int,
    selector: str,
    *args: Any,
    restype: Any = c_void_p,
    extra_argtypes: tuple[Any, ...] = (),
) -> Any:
    objc = _objc()
    if objc is None or not obj:
        return 0 if restype is not None else None
    sel = objc.sel_registerName(selector.encode("utf-8"))
    func = objc.objc_msgSend
    func.restype = restype
    func.argtypes = [c_void_p, c_void_p, *extra_argtypes]
    return func(obj, sel, *args)


def _nserror_text(err: c_void_p) -> str:
    ptr = int(err.value or 0) if err else 0
    if not ptr:
        return ""
    desc = _msg(ptr, "localizedDescription")
    if not desc:
        return ""
    raw = _msg(desc, "UTF8String", restype=c_char_p)
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return str(raw)


def _process_is_hidden() -> bool:
    """``ProcessInformationCopyDictionary`` → ``IsHidden`` (login-item hide flag)."""
    try:
        appsvc = _load_framework("ApplicationServices")
        cf = _load_framework("CoreFoundation")
        if appsvc is None or cf is None:
            return False

        class _PSN(ctypes.Structure):
            _fields_ = [("highLongOfPSN", c_uint32), ("lowLongOfPSN", c_uint32)]

        appsvc.ProcessInformationCopyDictionary.restype = c_void_p
        appsvc.ProcessInformationCopyDictionary.argtypes = [POINTER(_PSN), c_uint32]
        info = appsvc.ProcessInformationCopyDictionary(
            byref(_PSN(0, _kCurrentProcess)),
            _kProcessDictionaryIncludeAllInformationMask,
        )
        if not info:
            return False
        try:
            cf.CFStringCreateWithCString.restype = c_void_p
            cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
            key = cf.CFStringCreateWithCString(None, b"IsHidden", _kCFStringEncodingUTF8)
            if not key:
                return False
            cf.CFDictionaryGetValue.restype = c_void_p
            cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
            val = cf.CFDictionaryGetValue(info, key)
            cf.CFRelease.argtypes = [c_void_p]
            cf.CFRelease(key)
            if not val:
                return False
            cf.CFBooleanGetValue.restype = ctypes.c_bool
            cf.CFBooleanGetValue.argtypes = [c_void_p]
            return bool(cf.CFBooleanGetValue(val))
        finally:
            cf.CFRelease.argtypes = [c_void_p]
            cf.CFRelease(info)
    except Exception:
        return False


def _apple_event_is_login_item() -> bool:
    """``kAELaunchedAsLogInItem`` on the current Apple Event, if NSApp is up."""
    try:
        if _load_framework("Foundation") is None or _load_framework("AppKit") is None:
            return False
        objc = _objc()
        if objc is None:
            return False
        mgr_cls = objc.objc_getClass(b"NSAppleEventManager")
        if not mgr_cls:
            return False
        mgr = _msg(int(mgr_cls), "sharedAppleEventManager")
        event = _msg(int(mgr), "currentAppleEvent") if mgr else 0
        if not event:
            return False
        # keyAELaunchedAsLogInItem = 'lgit'
        desc = _msg(
            int(event),
            "paramDescriptorForKeyword:",
            0x6C676974,
            extra_argtypes=(c_uint32,),
        )
        return bool(desc)
    except Exception:
        return False
