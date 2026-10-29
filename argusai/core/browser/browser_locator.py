"""System browser discovery for the managed Playwright hub.

ArgusAI prefers to drive the user's *real* installed Chrome / Edge / Brave
/ Vivaldi (Playwright supports them all because they speak CDP) instead of
downloading the bundled Playwright Chromium (~120 MB). This module owns the
"where is a usable browser on this box?" question.

Public surface:

* :data:`KNOWN_CHANNELS` — Playwright-recognised ``channel=`` values.
* :func:`scan_system_browsers` — return a list of detected Chromium-family
  browsers with their executable path, version (best effort) and a
  Playwright-compatible ``channel`` if any.
* :func:`resolve_browser_binary` — given an optional explicit path /
  channel / preference list, return the (channel, executable_path) pair
  the hub should hand to Playwright; ``("", "")`` means "let Playwright
  pick its bundled Chromium".
* :func:`playwright_bundle_status` — does ``ms-playwright`` already hold
  a downloaded Chromium / Firefox / WebKit?
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs


# Playwright-recognised channel ids. Keep in sync with
# https://playwright.dev/python/docs/browsers#google-chrome--microsoft-edge
KNOWN_CHANNELS: tuple[str, ...] = (
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
    "msedge-canary",
)


# Brand metadata for the inventory UI. ``channel`` is the Playwright channel
# string to use; empty means we'll fall back to ``executable_path``.
_BRANDS: dict[str, dict[str, Any]] = {
    "chrome": {"label": "Google Chrome", "channel": "chrome"},
    "chrome-beta": {"label": "Chrome Beta", "channel": "chrome-beta"},
    "chrome-canary": {"label": "Chrome Canary", "channel": "chrome-canary"},
    "msedge": {"label": "Microsoft Edge", "channel": "msedge"},
    "msedge-beta": {"label": "Edge Beta", "channel": "msedge-beta"},
    "msedge-canary": {"label": "Edge Canary", "channel": "msedge-canary"},
    "brave": {"label": "Brave", "channel": ""},
    "vivaldi": {"label": "Vivaldi", "channel": ""},
    "chromium-system": {"label": "Chromium", "channel": ""},
}


# ---------------------------------------------------------------------------
# Platform-specific candidate paths
# ---------------------------------------------------------------------------


def _windows_candidates() -> list[tuple[str, Path]]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates: list[tuple[str, Path]] = []
    chrome_paths = [
        (Path(pf) / r"Google\Chrome\Application\chrome.exe", "chrome"),
        (Path(pf86) / r"Google\Chrome\Application\chrome.exe", "chrome"),
        (Path(pf) / r"Google\Chrome Beta\Application\chrome.exe", "chrome-beta"),
        (Path(pf86) / r"Google\Chrome Beta\Application\chrome.exe", "chrome-beta"),
        (Path(pf) / r"Google\Chrome SxS\Application\chrome.exe", "chrome-canary"),
        (Path(pf86) / r"Google\Chrome SxS\Application\chrome.exe", "chrome-canary"),
    ]
    if local:
        chrome_paths.extend([
            (Path(local) / r"Google\Chrome\Application\chrome.exe", "chrome"),
            (Path(local) / r"Google\Chrome Beta\Application\chrome.exe", "chrome-beta"),
            (Path(local) / r"Google\Chrome SxS\Application\chrome.exe", "chrome-canary"),
        ])
    edge_paths = [
        (Path(pf) / r"Microsoft\Edge\Application\msedge.exe", "msedge"),
        (Path(pf86) / r"Microsoft\Edge\Application\msedge.exe", "msedge"),
        (Path(pf) / r"Microsoft\Edge Beta\Application\msedge.exe", "msedge-beta"),
        (Path(pf86) / r"Microsoft\Edge Beta\Application\msedge.exe", "msedge-beta"),
        (Path(pf) / r"Microsoft\Edge SxS\Application\msedge.exe", "msedge-canary"),
        (Path(pf86) / r"Microsoft\Edge SxS\Application\msedge.exe", "msedge-canary"),
    ]
    brave_paths = [
        (Path(pf) / r"BraveSoftware\Brave-Browser\Application\brave.exe", "brave"),
        (Path(pf86) / r"BraveSoftware\Brave-Browser\Application\brave.exe", "brave"),
    ]
    if local:
        brave_paths.append(
            (Path(local) / r"BraveSoftware\Brave-Browser\Application\brave.exe", "brave")
        )
    vivaldi_paths = [
        (Path(pf) / r"Vivaldi\Application\vivaldi.exe", "vivaldi"),
        (Path(pf86) / r"Vivaldi\Application\vivaldi.exe", "vivaldi"),
    ]
    if local:
        vivaldi_paths.append((Path(local) / r"Vivaldi\Application\vivaldi.exe", "vivaldi"))
    for path, brand in chrome_paths + edge_paths + brave_paths + vivaldi_paths:
        candidates.append((brand, path))
    return candidates


def _macos_candidates() -> list[tuple[str, Path]]:
    app_roots = [Path("/Applications"), Path.home() / "Applications"]
    apps = [
        ("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("chrome-beta", "Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta"),
        ("chrome-canary", "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        ("msedge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("msedge-beta", "Microsoft Edge Beta.app/Contents/MacOS/Microsoft Edge Beta"),
        ("msedge-canary", "Microsoft Edge Canary.app/Contents/MacOS/Microsoft Edge Canary"),
        ("brave", "Brave Browser.app/Contents/MacOS/Brave Browser"),
        ("vivaldi", "Vivaldi.app/Contents/MacOS/Vivaldi"),
        ("chromium-system", "Chromium.app/Contents/MacOS/Chromium"),
    ]
    return [(brand, root / rel) for root in app_roots for brand, rel in apps]


def _linux_candidates() -> list[tuple[str, Path]]:
    pairs: list[tuple[str, str]] = [
        ("chrome", "google-chrome"),
        ("chrome", "google-chrome-stable"),
        ("chrome-beta", "google-chrome-beta"),
        ("chrome-canary", "google-chrome-unstable"),
        ("msedge", "microsoft-edge"),
        ("msedge", "microsoft-edge-stable"),
        ("msedge-beta", "microsoft-edge-beta"),
        ("msedge-canary", "microsoft-edge-dev"),
        ("brave", "brave-browser"),
        ("brave", "brave"),
        ("vivaldi", "vivaldi"),
        ("vivaldi", "vivaldi-stable"),
        ("chromium-system", "chromium"),
        ("chromium-system", "chromium-browser"),
    ]
    candidates: list[tuple[str, Path]] = []
    for brand, name in pairs:
        which = shutil.which(name)
        if which:
            candidates.append((brand, Path(which)))
        candidates.append((brand, Path(f"/usr/bin/{name}")))
        candidates.append((brand, Path(f"/usr/local/bin/{name}")))
        candidates.append((brand, Path(f"/snap/bin/{name}")))
    return candidates


def _candidates_for_platform() -> list[tuple[str, Path]]:
    plat = sys.platform
    if plat.startswith("win"):
        return _windows_candidates()
    if plat == "darwin":
        return _macos_candidates()
    return _linux_candidates()


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def _probe_version(exe: Path) -> str:
    """Best-effort version string for a browser binary."""
    if sys.platform.startswith("win"):
        try:
            # Use PowerShell to query the file version. Cheap, no subprocess
            # exec of the browser itself which would actually launch it.
            r = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Item '{exe}').VersionInfo.ProductVersion",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=False,
                text=False,
                timeout=4,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **quiet_subprocess_kwargs(),
            )
            raw = (r.stdout or b"") + (r.stderr or b"")
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                text = raw.decode("utf-16", errors="replace")
            else:
                text = raw.decode("utf-8", errors="replace")
            ver = text.strip().splitlines()
            return ver[0].strip() if ver else ""
        except Exception:
            return ""
    try:
        r = subprocess.run(
            [str(exe), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=4,
            **quiet_subprocess_kwargs(),
        )
        line = (r.stdout or r.stderr or "").strip().splitlines()
        if not line:
            return ""
        m = re.search(r"\d+\.\d+\.\d+\.\d+", line[0])
        return m.group(0) if m else line[0][:80]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_system_browsers() -> list[dict[str, Any]]:
    """Return all Chromium-family browsers we can find on this machine.

    The list is deduplicated by executable path and is ordered with most
    preferred entries first (stable channels before beta/canary, Chrome
    before Edge before Brave/Vivaldi). Each entry has ``brand``,
    ``label``, ``channel``, ``executable_path`` and ``version`` keys.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for brand, path in _candidates_for_platform():
        if not path:
            continue
        key = str(path)
        if key in seen:
            continue
        try:
            if not path.exists():
                continue
        except OSError:
            continue
        seen.add(key)
        meta = _BRANDS.get(brand) or {"label": brand, "channel": ""}
        out.append({
            "brand": brand,
            "label": meta["label"],
            "channel": meta.get("channel", ""),
            "executable_path": str(path),
            "version": _probe_version(path),
        })
    # Prefer stable channels and Chrome over Edge over Brave/Vivaldi.
    priority = {
        "chrome": 0, "msedge": 1, "brave": 2, "vivaldi": 3, "chromium-system": 4,
        "chrome-beta": 5, "msedge-beta": 6,
        "chrome-canary": 7, "msedge-canary": 8, "chrome-dev": 9,
    }
    out.sort(key=lambda r: priority.get(r["brand"], 99))
    return out


def _settings_file() -> Path:
    from argusai.core.paths import PathManager

    return PathManager().settings_path


def _managed_tools_dir() -> Path:
    """``data_root/tools`` from settings, not the default app-data folder.

    A bare ``PathManager()`` ignores a custom data directory. The Node
    sandbox the user installed lives under that directory.
    """
    import json

    from argusai.core.paths import PathManager

    data_root = ""
    try:
        raw = json.loads(_settings_file().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data_root = str(raw.get("data_root") or "").strip()
    except (OSError, UnicodeError, ValueError):
        data_root = ""
    return PathManager(data_root=data_root or None).tools_dir


def _usable_node(path: Path) -> bool:
    """True when ``path`` is a Node binary new enough for Playwright's cli.js."""
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            text=True,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    match = re.search(r"v?(\d+)", proc.stdout or "")
    return bool(match) and int(match.group(1)) >= 18


def _bundled_driver_node() -> Path | None:
    try:
        import playwright
    except Exception:
        return None
    name = "node.exe" if sys.platform == "win32" else "node"
    bundled = Path(playwright.__file__).parent / "driver" / name
    return bundled if bundled.is_file() else None


def playwright_driver_node() -> dict[str, Any]:
    """Resolve the Node binary that runs Playwright's ``cli.js``.

    Order:

    1. ``PLAYWRIGHT_NODEJS_PATH`` when the process already set it.
    2. The Node sandbox under the configured data directory. This is
       ArgusAI's runtime. Packaged builds drop Playwright's own
       ``driver/node`` because this sandbox is the interpreter, and a
       source checkout must make the same choice once the sandbox is
       installed. A runnable ``node.exe`` is enough — a stale webcrack
       lockfile does not block a browser download.
    3. Playwright's bundled driver Node, only when that sandbox is absent.
       ``cli.js`` declares ``node >= 18``.

    A Node binary on ``PATH`` is not a candidate. The sandbox is the runtime.
    """
    info: dict[str, Any] = {"ok": False, "source": "", "path": "", "error": ""}

    explicit = os.environ.get("PLAYWRIGHT_NODEJS_PATH", "").strip()
    if explicit:
        info.update(ok=True, source="env", path=explicit)
        return info

    from argusai.core.managed_runtimes import node_executable, runtime_status

    tools_dir = _managed_tools_dir()
    status = runtime_status("node", tools_dir)
    managed = node_executable(tools_dir)
    if status.get("healthy") or _usable_node(managed):
        info.update(ok=True, source="managed", path=str(managed))
        return info

    bundled = _bundled_driver_node()
    if bundled is not None:
        info.update(ok=True, source="bundled", path=str(bundled))
        return info

    info["error"] = (
        "Playwright needs a Node runtime to start its driver. Install the "
        "Node sandbox runtime from Settings, or run the runtime_install tool."
    )
    info["runtime_required"] = "node"
    return info


def ensure_playwright_driver_node() -> dict[str, Any]:
    """Export ``PLAYWRIGHT_NODEJS_PATH`` before Playwright's driver starts."""
    info = playwright_driver_node()
    if info.get("ok") and info.get("source") != "bundled":
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(info["path"])
    return info


def playwright_install_argv(engine: str) -> list[str]:
    """Argv for ``playwright install <engine>`` that does not re-exec this program.

    A packaged Nuitka build's ``sys.executable`` is ArgusAI itself. Calling
    ``sys.executable -m playwright`` hits Nuitka's self-execution guard and
    never downloads a browser. The driver ``cli.js`` plus the managed (or
    bundled) Node binary is the supported install entry point.
    """
    eng = (engine or "chromium").strip().lower()
    if eng not in ("chromium", "firefox", "webkit"):
        raise ValueError(f"unknown engine: {engine}")
    driver = ensure_playwright_driver_node()
    if not driver.get("ok"):
        raise RuntimeError(driver.get("error") or "Playwright driver runtime is unavailable")
    import playwright

    cli = Path(playwright.__file__).resolve().parent / "driver" / "package" / "cli.js"
    if not cli.is_file():
        raise RuntimeError("Playwright driver cli.js is missing from this build")
    return [str(driver["path"]), str(cli), "install", eng]


def playwright_bundle_status() -> dict[str, Any]:
    """Inspect the ``ms-playwright`` cache for downloaded engines."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom and custom != "0":
        root = Path(custom)
    elif sys.platform.startswith("win"):
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        root = Path.home() / ".cache" / "ms-playwright"
    info: dict[str, Any] = {"root": str(root), "exists": False, "engines": {}}
    try:
        if not root.exists():
            return info
    except OSError:
        return info
    info["exists"] = True
    for engine, prefix in (
        ("chromium", "chromium-"),
        ("firefox", "firefox-"),
        ("webkit", "webkit-"),
    ):
        try:
            matches = sorted(
                (p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)),
                reverse=True,
            )
        except OSError:
            matches = []
        info["engines"][engine] = {
            "installed": bool(matches),
            "latest": str(matches[0]) if matches else "",
            "count": len(matches),
        }
    return info


def find_channel_executable(channel: str) -> str:
    """Return the on-disk executable for a Playwright channel, or ``""``.

    ``resolve_browser_binary`` deliberately leaves ``executable_path``
    empty when it picks a channel — Playwright's ``.launch(channel=...)``
    handles binary discovery itself. But CDP attach has to spawn the
    browser via :mod:`subprocess`, which needs the real path, so this
    helper bridges the gap.
    """
    if not channel:
        return ""
    needle = channel.strip().lower()
    for entry in scan_system_browsers():
        if (entry.get("channel") or "").lower() == needle:
            return entry.get("executable_path", "") or ""
    return ""


def resolve_browser_binary(
    *,
    explicit_executable: str = "",
    explicit_channel: str = "",
    prefer_channels: tuple[str, ...] = ("chrome", "msedge"),
) -> dict[str, Any]:
    """Return the launch hints for Playwright based on user config + scan.

    Priority:
    1. ``explicit_executable`` if it exists on disk.
    2. ``explicit_channel`` if it is a known Playwright channel.
    3. First detected system browser matching ``prefer_channels``.
    4. Any detected system browser.
    5. ``ms-playwright`` bundle (Playwright will resolve it itself).
    6. ``not_found`` — caller should ask the user to install one or
       trigger ``browser_install_engine``.
    """
    if explicit_executable:
        p = Path(explicit_executable)
        try:
            if p.exists():
                return {
                    "channel": "",
                    "executable_path": str(p),
                    "source": "explicit_executable",
                    "label": p.name,
                }
        except OSError:
            pass
    if explicit_channel and explicit_channel in KNOWN_CHANNELS:
        return {
            "channel": explicit_channel,
            "executable_path": "",
            "source": "explicit_channel",
            "label": explicit_channel,
        }
    system = scan_system_browsers()
    if system:
        # Match user's preferred order first.
        prefer_set = {c for c in prefer_channels if c}
        for entry in system:
            if entry["channel"] in prefer_set:
                return {
                    "channel": entry["channel"],
                    "executable_path": "" if entry["channel"] else entry["executable_path"],
                    "source": "system_preferred",
                    "label": entry["label"],
                    "version": entry.get("version", ""),
                }
        # Otherwise any detected browser, channel first then explicit path.
        first = system[0]
        return {
            "channel": first["channel"],
            "executable_path": "" if first["channel"] else first["executable_path"],
            "source": "system_any",
            "label": first["label"],
            "version": first.get("version", ""),
        }
    bundle = playwright_bundle_status()
    if bundle["exists"] and bundle["engines"].get("chromium", {}).get("installed"):
        return {
            "channel": "",
            "executable_path": "",
            "source": "playwright_bundle",
            "label": "Playwright Chromium",
        }
    return {
        "channel": "",
        "executable_path": "",
        "source": "not_found",
        "label": "",
    }
