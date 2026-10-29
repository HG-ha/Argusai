"""Per-user Windows logon via HKCU Run (Settings → Apps → Startup).

This is the consumer-app convention (Chrome, Discord, Slack). Task Scheduler
stays in ``service_install`` for pre-logon ``startup`` and cron-only units.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "ArgusAI"

# 12-byte StartupApproved blob. First byte 0x02 = enabled, 0x03 = disabled.
_APPROVED_ENABLED = b"\x02\x00\x00\x00" + b"\x00" * 8


def run_key_command_line(cmd: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in cmd])


def run_key_status() -> dict[str, Any]:
    if sys.platform != "win32":
        return {
            "ok": True,
            "registered": False,
            "enabled": False,
            "command": "",
            "approved": "",
        }
    command = _read_sz(RUN_KEY, VALUE_NAME)
    approved = _read_binary(APPROVED_KEY, VALUE_NAME)
    registered = bool(command)
    enabled = registered and _approved_is_enabled(approved)
    return {
        "ok": True,
        "registered": registered,
        "enabled": enabled,
        "command": command,
        "approved": "disabled" if registered and not enabled else ("enabled" if registered else ""),
    }


def run_key_is_registered() -> bool:
    return bool(run_key_status().get("registered"))


def write_run_key(command: str) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": False, "error": "Windows only"}
    text = str(command or "").strip()
    if not text:
        return {"ok": False, "error": "empty command"}
    try:
        _write_sz(RUN_KEY, VALUE_NAME, text)
        _write_binary(APPROVED_KEY, VALUE_NAME, _APPROVED_ENABLED)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **run_key_status()}


def delete_run_key() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"ok": True, "removed": False}
    removed = _delete_value(RUN_KEY, VALUE_NAME)
    _delete_value(APPROVED_KEY, VALUE_NAME)
    return {"ok": True, "removed": removed}


def start_run_key_command(cmd: list[str]) -> dict[str, Any]:
    if not cmd:
        return {"ok": False, "error": "empty command"}
    cwd = str(Path(cmd[0]).expanduser().resolve().parent) if cmd[0] else None
    kwargs: dict[str, Any] = {
        "args": list(cmd),
        "cwd": cwd,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    try:
        subprocess.Popen(**kwargs)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "command": list(cmd)}


def _approved_is_enabled(blob: bytes) -> bool:
    if not blob:
        return True
    return blob[0] == 0x02


def _read_sz(subkey: str, name: str) -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(value or "")


def _read_binary(subkey: str, name: str) -> bytes:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return b""
    return bytes(value or b"")


def _write_sz(subkey: str, name: str, value: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _write_binary(subkey: str, name: str, value: bytes) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, subkey) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, value)


def _delete_value(subkey: str, name: str) -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False
