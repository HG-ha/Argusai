"""Module-level helpers shared across GUI mixins."""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def configure_windows_gui_runtime() -> None:
    """Use the native Windows Desktop CLR for ARM64 Python.

    pythonnet's default .NET Framework loader ships x86/x64 native helpers.
    Selecting CoreCLR before pywebview imports WinForms avoids loading an
    x64 helper into an ARM64 process. Explicit user runtime settings win.
    """
    if sys.platform != "win32" or sysconfig.get_platform().lower() != "win-arm64":
        return
    runtime = os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")
    if runtime.lower() == "coreclr":
        local_config = Path(__file__).resolve().with_name("windows-arm64.runtimeconfig.json")
        frozen_config = (
            Path(sys.executable).resolve().parent
            / "argusai"
            / "gui"
            / "windows-arm64.runtimeconfig.json"
        )
        runtime_config = frozen_config if getattr(sys, "frozen", False) and frozen_config.is_file() else local_config
        os.environ.setdefault(
            "PYTHONNET_CORECLR_RUNTIME_CONFIG",
            str(runtime_config),
        )


def load_windows_arm64_framework_assemblies() -> None:
    """Preload framework assemblies that pywebview imports by namespace.

    Python.NET's CoreCLR loader does not implicitly load an assembly merely
    because a matching CLR namespace is imported. pywebview's WinForms backend
    imports ``Microsoft.Win32.SystemEvents`` without its own ``AddReference``;
    load the Windows Desktop framework assembly before importing pywebview.
    """
    if (
        sys.platform != "win32"
        or sysconfig.get_platform().lower() != "win-arm64"
        or os.environ.get("PYTHONNET_RUNTIME", "").lower() != "coreclr"
    ):
        return
    import clr

    clr.AddReference("Microsoft.Win32.SystemEvents")


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
