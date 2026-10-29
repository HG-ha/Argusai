"""Resolve argv for re-entering the current ArgusAI process.

Frozen Nuitka onefile builds are the tricky case: on Windows DLL-mode
unpacks, ``sys.executable`` can point at a non-existent ``python.exe`` under
the cache directory while ``sys.argv[0]`` remains the user-facing
``ArgusAI.exe``. Channel stream sidecars and the MCP tool proxy both need a
path that ``CreateProcess`` can actually open.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_ONEFILE_ENV_KEYS = (
    "NUITKA_ONEFILE_PARENT",
    "NUITKA_ONEFILE_BINARY",
    "NUITKA_RESUME_FILENAME",
)


def is_frozen_runtime() -> bool:
    """True when running inside a Nuitka/pyinstaller-style packaged binary."""
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        return True
    # Nuitka injects ``__compiled__`` into compiled modules. Prefer the package
    # attribute so detection still works when this helper is imported as source
    # next to a compiled ``argusai`` tree (tests / mixed layouts).
    if "__compiled__" in globals():
        return True
    try:
        import argusai as _pkg

        if getattr(_pkg, "__compiled__", None) is not None:
            return True
    except Exception:
        pass
    return False


def frozen_self_executable() -> str:
    """Return an on-disk executable path suitable for ``subprocess.Popen``.

    Preference order:
    1. ``sys.argv[0]`` when it exists (onefile bootstrap / launched .exe)
    2. ``sys.executable`` when it exists (standalone dist binary)
    3. Best-effort absolute form of whichever string we have
    """
    candidates: list[str] = []
    argv0 = str(sys.argv[0] or "").strip() if sys.argv else ""
    executable = str(sys.executable or "").strip()
    if argv0:
        candidates.append(argv0)
    if executable and executable not in candidates:
        candidates.append(executable)
    try:
        compiled = globals().get("__compiled__")
        containing = getattr(compiled, "containing_dir", None) if compiled else None
        if containing and argv0:
            joined = str(Path(containing) / Path(argv0).name)
            if joined not in candidates:
                candidates.append(joined)
    except Exception:
        pass

    for raw in candidates:
        try:
            path = Path(raw).expanduser()
            if path.is_file():
                return str(path.resolve())
        except OSError:
            continue

    fallback = argv0 or executable or sys.executable or ""
    try:
        return str(Path(fallback).expanduser().resolve()) if fallback else fallback
    except OSError:
        return fallback


def self_relaunch_command(*extra_args: str) -> list[str]:
    """Build argv that re-enters this app with optional CLI flags."""
    if is_frozen_runtime():
        return [frozen_self_executable(), *[str(arg) for arg in extra_args]]
    return [str(sys.executable), "-m", "argusai", *[str(arg) for arg in extra_args]]


def self_relaunch_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child relaunch that must not inherit onefile parent locks."""
    env = dict(os.environ if base is None else base)
    for key in _ONEFILE_ENV_KEYS:
        env.pop(key, None)
    return env
