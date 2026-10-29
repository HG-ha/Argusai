"""Module-level helpers shared across GUI mixins."""

from __future__ import annotations

import os
import sys
from pathlib import Path


WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def debug_log(message: str) -> None:
    """Print a debug line when ``ARGUSAI_DEBUG_LOG`` is enabled.

    Default is off so CLI Agent chat stays readable; set
    ``ARGUSAI_DEBUG_LOG=1`` to restore the old verbose stream.
    """
    if os.environ.get("ARGUSAI_DEBUG_LOG", "0").lower() not in {"1", "true", "yes"}:
        return
    print(f"[argusai-debug] {message}", flush=True)


def resource_root() -> Path:
    """Return the directory that ships webui assets.

    Resolution order:
    1. Nuitka-frozen build with ``argusai/webui/`` next to the exe → that dir.
    2. Nuitka-frozen build with ``argusai/webui/dist/`` next to the exe → dist/.
    3. Dev mode: prefer ``argusai/webui/dist/`` (built bundle) when present.
    4. Dev mode fallback: raw ``argusai/webui/`` source dir.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        for candidate in (
            exe_dir / "argusai" / "webui",
            exe_dir / "argusai" / "webui" / "dist",
        ):
            if candidate.exists():
                return candidate
    dist = WEBUI_DIR / "dist"
    if dist.exists():
        return dist
    return WEBUI_DIR
