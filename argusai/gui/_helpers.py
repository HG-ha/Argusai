"""Module-level helpers shared across GUI mixins."""

from __future__ import annotations

import importlib
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


def self_check_gui() -> dict[str, object]:
    """Load the platform's real GUI backend without constructing a window.

    On Windows, ``launch`` runs the same CLR bootstrap as an ordinary launch,
    but ``Api`` and its user settings are only instantiated by ``launch.main``.
    Inspect loaded CLR assemblies rather than trusting file-existence checks:
    the two WebView2 SDK variants must really be loadable by the selected CLR.
    """
    platform = sys.platform
    backend = {"win32": "edgechromium", "darwin": "wkwebview"}.get(platform, "")
    if platform.startswith("linux"):
        backend = "gtkwebkit2"
    report: dict[str, object] = {
        "ok": False,
        "supported": bool(backend),
        "platform": platform,
        "backend": backend,
        "runtime": "",
        "dotnet_version": "",
        "dlls": {},
    }
    if not backend:
        report["error"] = f"GUI dependency self-check is not supported on {platform}"
        return report
    if backend == "gtkwebkit2":
        return _self_check_gtk_gui(report)
    if backend == "wkwebview":
        return _self_check_cocoa_gui(report)

    stage = "bootstrap"
    try:
        launch = importlib.import_module("argusai.gui.launch")
        stage = "win32_helper"
        launch._install_pywebview_win32_helper()
        stage = "winforms"
        importlib.import_module("webview.platforms.winforms")
        # WinForms can select its legacy browser when WebView2 is unavailable.
        # Explicitly import Edge as well so that cannot hide a missing SDK DLL.
        stage = "edgechromium"
        importlib.import_module("webview.platforms.edgechromium")
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stage"] = stage

    try:
        pythonnet = sys.modules.get("pythonnet")
        info = pythonnet.get_runtime_info() if pythonnet is not None else None
        if info is not None:
            kind = str(info.kind).lower()
            report["runtime"] = "netfx" if kind == ".net framework" else kind
            system = importlib.import_module("System")
            report["dotnet_version"] = str(system.Environment.Version)
            names = {"Microsoft.Web.WebView2.Core", "Microsoft.Web.WebView2.WinForms"}
            dlls: dict[str, str] = {}
            for assembly in system.AppDomain.CurrentDomain.GetAssemblies():
                name = str(assembly.GetName().Name)
                if name in names:
                    dlls[f"{name}.dll"] = str(Path(str(assembly.Location)).resolve())
            report["dlls"] = dlls
        if "error" not in report:
            if report["runtime"] not in {"netfx", "coreclr"}:
                raise RuntimeError("No supported Windows CLR was initialized")
            if len(report["dlls"]) != 2:
                raise RuntimeError("The WebView2 Core and WinForms assemblies were not both loaded")
            report["ok"] = True
    except Exception as exc:
        if "error" not in report:
            report["error"] = f"{type(exc).__name__}: {exc}"
            report["stage"] = "assemblies"
    return report


def _self_check_gtk_gui(report: dict[str, object]) -> dict[str, object]:
    requirements = {"Gtk": "3.0", "Gdk": "3.0", "WebKit2": "4.1", "Soup": "3.0"}
    report["requirements"] = requirements
    stage = "display"
    try:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise RuntimeError("Linux GUI self-check requires DISPLAY (use xvfb-run) or WAYLAND_DISPLAY")
        stage = "gi"
        gi = importlib.import_module("gi")
        stage = "gi_requirements"
        for namespace, version in requirements.items():
            gi.require_version(namespace, version)
        stage = "gi_repository"
        namespaces = {name: importlib.import_module(f"gi.repository.{name}") for name in requirements}
        # Resolve real typelib-backed classes without allocating a widget.
        for name, symbol in (("Gtk", "Application"), ("Gdk", "Display"),
                             ("WebKit2", "WebView"), ("Soup", "Session")):
            getattr(namespaces[name], symbol)
        stage = "gtk_display"
        initialized = namespaces["Gtk"].init_check()
        if not (initialized[0] if isinstance(initialized, tuple) else initialized):
            raise RuntimeError("GTK could not connect to the display; use xvfb-run for headless checks")
        stage = "gtk"
        backend = importlib.import_module("webview.platforms.gtk")
        if getattr(backend, "renderer", "") != "gtkwebkit2":
            raise RuntimeError("pywebview did not load the GTK WebKit2 backend")
        report["versions"] = {"PyGObject": str(getattr(gi, "__version__", ""))}
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stage"] = stage
    return report


def _self_check_cocoa_gui(report: dict[str, object]) -> dict[str, object]:
    stage = "objc"
    try:
        modules = {}
        for name in ("objc", "AppKit", "Foundation", "WebKit"):
            stage = name
            modules[name] = importlib.import_module(name)
        stage = "cocoa_bridges"
        for name, symbol in (("AppKit", "NSApplication"), ("Foundation", "NSObject"), ("WebKit", "WKWebView")):
            getattr(modules[name], symbol)
        if modules["objc"].lookUpClass("WKWebView") is None:
            raise RuntimeError("PyObjC could not resolve the native WKWebView class")
        stage = "cocoa"
        backend = importlib.import_module("webview.platforms.cocoa")
        if getattr(backend, "renderer", "") != "wkwebview":
            raise RuntimeError("pywebview did not load the Cocoa WKWebView backend")
        report["versions"] = {"PyObjC": str(getattr(modules["objc"], "__version__", ""))}
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["stage"] = stage
    return report


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
