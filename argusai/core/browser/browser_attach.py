"""CDP attach helpers — launch / kill / discover the user's real browser.

This module powers the "reuse my real browser session" feature. The
workflow is:

1. Find the user's existing Chrome / Edge user-data-dir so we can
   relaunch the same browser with its cookies, history, extensions and
   logged-in state intact.
2. Kill any running browser instance for that channel (Chromium dedupes
   by ``--user-data-dir``; we can't sneak ``--remote-debugging-port``
   into a process that's already running).
3. Spawn the browser ourselves with ``--remote-debugging-port=N`` and
   the discovered user-data-dir.
4. Poll ``http://127.0.0.1:N/json/version`` until the DevTools endpoint
   is alive, then return the endpoint URL so callers can hand it to
   ``BrowserHub.attach_cdp(...)``.

Everything here is best-effort; on misconfigured systems any step can
fail and we surface a structured error instead of crashing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import ensure_valid_stdio, quiet_subprocess_kwargs

import threading as _threading

# Browsers WE launched via ``launch_with_debug_port`` (pid + headless). These
# are started detached so a normal detach / app close does not nuke a window
# the user can see. But a *headless* one we launched has no window at all, so
# leaving it running after the app exits is an invisible orphan — we terminate
# those on shutdown. Headful ones we launched (and browsers the user started
# themselves) are left alone.
_OWNED_LAUNCHED: list[dict[str, Any]] = []
_OWNED_LAUNCHED_LOCK = _threading.Lock()


def _register_launched_browser(pid: int, *, headless: bool, port: int) -> None:
    if not pid:
        return
    with _OWNED_LAUNCHED_LOCK:
        _OWNED_LAUNCHED.append({"pid": int(pid), "headless": bool(headless), "port": int(port or 0)})


def terminate_owned_launched_browsers(*, headless_only: bool = True) -> dict[str, Any]:
    """Kill browsers this process launched (headless ones by default).

    Best-effort and PID-scoped: never touches a browser the user started or
    one we launched with a visible window.
    """
    with _OWNED_LAUNCHED_LOCK:
        pending = list(_OWNED_LAUNCHED)
        _OWNED_LAUNCHED.clear()
    killed: list[int] = []
    kept: list[int] = []
    for item in pending:
        pid = int(item.get("pid") or 0)
        if not pid:
            continue
        if headless_only and not item.get("headless"):
            kept.append(pid)
            continue
        try:
            if sys.platform.startswith("win"):
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                    **quiet_subprocess_kwargs(),
                )
            else:
                os.kill(pid, 15)
            killed.append(pid)
        except Exception:
            pass
    # Keep the ones we deliberately spared so a later call can still see them.
    if kept:
        with _OWNED_LAUNCHED_LOCK:
            for pid in kept:
                _OWNED_LAUNCHED.append({"pid": pid, "headless": False, "port": 0})
    return {"ok": True, "killed": killed, "kept": kept}


# Map our internal channel labels to the user-data-dir locations and the
# OS-level process names that Chromium uses on each platform.
_PROFILE_DIRS = {
    "chrome": {
        "win": r"Google\Chrome\User Data",
        "darwin": "Library/Application Support/Google/Chrome",
        "linux": ".config/google-chrome",
    },
    "chrome-beta": {
        "win": r"Google\Chrome Beta\User Data",
        "darwin": "Library/Application Support/Google/Chrome Beta",
        "linux": ".config/google-chrome-beta",
    },
    "chrome-canary": {
        "win": r"Google\Chrome SxS\User Data",
        "darwin": "Library/Application Support/Google/Chrome Canary",
        "linux": ".config/google-chrome-unstable",
    },
    "msedge": {
        "win": r"Microsoft\Edge\User Data",
        "darwin": "Library/Application Support/Microsoft Edge",
        "linux": ".config/microsoft-edge",
    },
    "msedge-beta": {
        "win": r"Microsoft\Edge Beta\User Data",
        "darwin": "Library/Application Support/Microsoft Edge Beta",
        "linux": ".config/microsoft-edge-beta",
    },
    "msedge-canary": {
        "win": r"Microsoft\Edge SxS\User Data",
        "darwin": "Library/Application Support/Microsoft Edge Canary",
        "linux": ".config/microsoft-edge-dev",
    },
    "brave": {
        "win": r"BraveSoftware\Brave-Browser\User Data",
        "darwin": "Library/Application Support/BraveSoftware/Brave-Browser",
        "linux": ".config/BraveSoftware/Brave-Browser",
    },
    "vivaldi": {
        "win": r"Vivaldi\User Data",
        "darwin": "Library/Application Support/Vivaldi",
        "linux": ".config/vivaldi",
    },
}


_PROCESS_NAMES = {
    "chrome": {"win": ["chrome.exe"], "darwin": ["Google Chrome"], "linux": ["chrome", "google-chrome"]},
    "chrome-beta": {"win": ["chrome.exe"], "darwin": ["Google Chrome Beta"], "linux": ["google-chrome-beta"]},
    "chrome-canary": {"win": ["chrome.exe"], "darwin": ["Google Chrome Canary"], "linux": ["google-chrome-unstable"]},
    "msedge": {"win": ["msedge.exe"], "darwin": ["Microsoft Edge"], "linux": ["microsoft-edge", "microsoft-edge-stable"]},
    "msedge-beta": {"win": ["msedge.exe"], "darwin": ["Microsoft Edge Beta"], "linux": ["microsoft-edge-beta"]},
    "msedge-canary": {"win": ["msedge.exe"], "darwin": ["Microsoft Edge Canary"], "linux": ["microsoft-edge-dev"]},
    "brave": {"win": ["brave.exe"], "darwin": ["Brave Browser"], "linux": ["brave"]},
    "vivaldi": {"win": ["vivaldi.exe"], "darwin": ["Vivaldi"], "linux": ["vivaldi"]},
}


def _os_key() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def find_default_profile_path(channel: str) -> str:
    """Return the user's default ``--user-data-dir`` for the given channel.

    Returns an empty string when the channel is unknown or the directory
    does not exist on this machine.
    """
    info = _PROFILE_DIRS.get(channel.lower())
    if not info:
        return ""
    osk = _os_key()
    rel = info.get(osk, "")
    if not rel:
        return ""
    if osk == "win":
        base = os.environ.get("LOCALAPPDATA", "")
        if not base:
            return ""
        path = Path(base) / rel
    else:
        path = Path.home() / rel
    try:
        return str(path) if path.exists() else ""
    except OSError:
        return ""


def argusai_cdp_profile_path(channel: str) -> str:
    """Return a dedicated profile path that ArgusAI can launch with CDP.

    This avoids Chromium single-instance dedupe when the user's real Edge /
    Chrome profile is already running and cannot be killed from a packaged app.
    """
    safe_channel = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (channel or "browser").lower())
    if _os_key() == "win":
        base = Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
        root = base / "ArgusAI" / "cdp-profiles"
    elif _os_key() == "darwin":
        root = Path.home() / "Library" / "Application Support" / "ArgusAI" / "cdp-profiles"
    else:
        root = Path.home() / ".local" / "share" / "argusai" / "cdp-profiles"
    path = root / safe_channel
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(path)


def kill_browser_processes(channel: str, timeout: float = 5.0) -> dict[str, Any]:
    """Best-effort kill of every running process for ``channel``.

    Chromium uses ``--user-data-dir`` as its single-instance discriminator,
    so if a browser is still alive on the user's real profile our follow-up
    ``--remote-debugging-port`` launch will silently merge into it without
    enabling the debug port. Killing first sidesteps that.
    """
    names = (_PROCESS_NAMES.get(channel.lower()) or {}).get(_os_key()) or []
    if not names:
        return {"ok": False, "killed": [], "reason": f"unknown channel {channel!r}"}
    killed: list[str] = []
    errors: list[str] = []
    for name in names:
        try:
            if _os_key() == "win":
                r = subprocess.run(
                    ["taskkill", "/F", "/IM", name, "/T"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    **quiet_subprocess_kwargs(),
                )
                if r.returncode == 0:
                    killed.append(name)
                else:
                    msg = (r.stdout + r.stderr).strip()
                    # taskkill returns 128 when no process matched; that
                    # is fine — nothing to kill.
                    if "not found" not in msg.lower() and "找不到" not in msg:
                        errors.append(f"{name}: {msg[:120]}")
            else:
                r = subprocess.run(["pkill", "-x", name], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
                if r.returncode in (0, 1):  # 1 = no processes matched
                    killed.append(name)
                else:
                    errors.append(f"{name}: rc={r.returncode}")
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: kill timed out")
        except FileNotFoundError as e:
            errors.append(f"{name}: {e}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    # Give the OS a moment to actually tear down the process tree before
    # we try to launch a new instance using the same user-data-dir.
    time.sleep(0.4)
    return {"ok": not errors, "killed": killed, "errors": errors}


def _cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def cdp_endpoint_alive(port: int, timeout: float = 1.0) -> dict[str, Any]:
    """Probe ``/json/version`` to see whether a CDP server is live on port."""
    url = f"{_cdp_endpoint(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            import json as _json

            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
            return {
                "ok": True,
                "endpoint": _cdp_endpoint(port),
                "browser": data.get("Browser") or "",
                "user_agent": data.get("User-Agent") or "",
                "ws": data.get("webSocketDebuggerUrl") or "",
            }
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return {"ok": False, "endpoint": _cdp_endpoint(port)}


def wait_for_cdp_endpoint(port: int, timeout: float = 15.0, poll_interval: float = 0.25) -> dict[str, Any]:
    """Block until ``/json/version`` answers or ``timeout`` elapses."""
    deadline = time.time() + max(1.0, timeout)
    last: dict[str, Any] = {"ok": False, "endpoint": _cdp_endpoint(port)}
    while time.time() < deadline:
        last = cdp_endpoint_alive(port, timeout=1.0)
        if last["ok"]:
            return last
        time.sleep(poll_interval)
    last["error"] = "CDP endpoint did not come up before timeout"
    return last


def discover_running_cdp_endpoints(start: int = 9222, end: int = 9230) -> list[dict[str, Any]]:
    """Scan a small range of common CDP ports for live endpoints."""
    out: list[dict[str, Any]] = []
    for p in range(start, end + 1):
        info = cdp_endpoint_alive(p, timeout=0.4)
        if info["ok"]:
            out.append(info)
    return out


def launch_with_debug_port(
    *,
    executable_path: str,
    user_data_dir: str = "",
    port: int = 9222,
    extra_args: list[str] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    """Spawn the browser with ``--remote-debugging-port`` and the user's profile.

    Returns the subprocess pid, the CDP endpoint URL, and whether the
    endpoint is responsive. The browser is started detached so it
    survives the Python parent — we do NOT want closing argusai to
    nuke the user's Edge window.
    """
    exe = Path(executable_path)
    if not exe.exists():
        return {"ok": False, "error": f"executable not found: {executable_path}"}
    argv: list[str] = [str(exe), f"--remote-debugging-port={int(port)}"]
    if user_data_dir:
        argv.append(f"--user-data-dir={user_data_dir}")
    if headless:
        argv.append("--headless=new")
    # A few flags that make the attached browser cooperate with us:
    #   --no-first-run    : skip the welcome dance on a fresh data dir
    #   --no-default-browser-check : avoid the modal popup
    #   --restore-last-session     : ask Edge/Chrome to reopen prior tabs
    argv += [
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--remote-allow-origins=*",
        # The agent drives this off-screen and the user only sees the muted
        # mirror, so never let a page autoplay audio with no visible source.
        "--mute-audio",
    ]
    try:
        from argusai.core.browser.browser_profiles import automation_window_size

        _w, _h = automation_window_size()
        argv.append(f"--window-size={_w},{_h}")
    except Exception:
        pass
    argv += list(extra_args or [])

    creationflags = 0
    start_new_session = False
    if sys.platform.startswith("win"):
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    else:
        start_new_session = True

    try:
        ensure_valid_stdio()
        proc = subprocess.Popen(
            argv,
            creationflags=creationflags,
            start_new_session=start_new_session,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": f"could not start browser: {e}", "argv": argv}
    except Exception as e:
        return {"ok": False, "error": str(e), "argv": argv}

    ready = wait_for_cdp_endpoint(int(port), timeout=20.0)
    _register_launched_browser(proc.pid, headless=headless, port=int(port))
    return {
        "ok": ready["ok"],
        "pid": proc.pid,
        "argv": argv,
        "endpoint": _cdp_endpoint(int(port)),
        "browser": ready.get("browser") or "",
        "error": ready.get("error") or "",
    }


def channel_friendly_label(channel: str) -> str:
    return {
        "chrome": "Google Chrome",
        "chrome-beta": "Chrome Beta",
        "chrome-canary": "Chrome Canary",
        "msedge": "Microsoft Edge",
        "msedge-beta": "Edge Beta",
        "msedge-canary": "Edge Canary",
        "brave": "Brave",
        "vivaldi": "Vivaldi",
    }.get(channel.lower(), channel)


def attach_summary(channel: str, port: int = 9222) -> dict[str, Any]:
    """One-shot diagnostic for the settings panel."""
    info = cdp_endpoint_alive(port)
    return {
        "channel": channel,
        "channel_label": channel_friendly_label(channel),
        "default_user_data_dir": find_default_profile_path(channel),
        "alive": info["ok"],
        "endpoint": info.get("endpoint", _cdp_endpoint(port)),
        "browser": info.get("browser", ""),
    }


# Keep ``shutil`` import so type-checkers don't complain — used elsewhere when
# implementations evolve (e.g. resolving extra args via ``shutil.which``).
_ = shutil
