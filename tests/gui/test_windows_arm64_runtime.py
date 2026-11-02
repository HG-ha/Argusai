"""Native CLR selection and platform GUI dependency probes, without windows."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.gui import _helpers as helpers


@pytest.fixture(autouse=True)
def clear_runtime(monkeypatch):
    monkeypatch.delenv("PYTHONNET_RUNTIME", raising=False)
    monkeypatch.delenv("PYTHONNET_CORECLR_RUNTIME_CONFIG", raising=False)


def test_arm64_selects_desktop_runtime_before_backend_import(monkeypatch):
    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setattr(helpers.sysconfig, "get_platform", lambda: "win-arm64")
    helpers.configure_windows_gui_runtime()
    assert os.environ["PYTHONNET_RUNTIME"] == "coreclr"
    config = Path(os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"])
    options = json.loads(config.read_text(encoding="utf-8"))["runtimeOptions"]
    assert options["framework"]["name"] == "Microsoft.WindowsDesktop.App"
    assert options["rollForward"] == "LatestMajor"


def test_arm64_preloads_system_events_for_coreclr(monkeypatch):
    references = []

    class FakeClr:
        @staticmethod
        def AddReference(name):
            references.append(name)

    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setattr(helpers.sysconfig, "get_platform", lambda: "win-arm64")
    monkeypatch.setitem(sys.modules, "clr", FakeClr())
    helpers.configure_windows_gui_runtime()
    helpers.load_windows_arm64_framework_assemblies()
    assert references == ["Microsoft.Win32.SystemEvents"]


@pytest.mark.parametrize("host,platform", [("win32", "win-amd64"), ("linux", "linux-aarch64")])
def test_other_process_architectures_keep_existing_default(monkeypatch, host, platform):
    monkeypatch.setattr(helpers.sys, "platform", host)
    monkeypatch.setattr(helpers.sysconfig, "get_platform", lambda: platform)
    helpers.configure_windows_gui_runtime()
    assert "PYTHONNET_RUNTIME" not in os.environ
    assert "PYTHONNET_CORECLR_RUNTIME_CONFIG" not in os.environ


def test_explicit_runtime_and_config_are_preserved(monkeypatch):
    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setattr(helpers.sysconfig, "get_platform", lambda: "win-arm64")
    monkeypatch.setenv("PYTHONNET_RUNTIME", "netfx")
    helpers.configure_windows_gui_runtime()
    assert os.environ["PYTHONNET_RUNTIME"] == "netfx"
    assert "PYTHONNET_CORECLR_RUNTIME_CONFIG" not in os.environ
    monkeypatch.setenv("PYTHONNET_RUNTIME", "coreclr")
    monkeypatch.setenv("PYTHONNET_CORECLR_RUNTIME_CONFIG", "custom.runtimeconfig.json")
    helpers.configure_windows_gui_runtime()
    assert os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"] == "custom.runtimeconfig.json"


def test_frozen_arm64_uses_packaged_runtime_config(monkeypatch, tmp_path):
    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setattr(helpers.sysconfig, "get_platform", lambda: "win-arm64")
    monkeypatch.setattr(helpers.sys, "frozen", True, raising=False)
    monkeypatch.setattr(helpers.sys, "executable", str(tmp_path / "ArgusAI.exe"))
    packaged = tmp_path / "argusai/gui/windows-arm64.runtimeconfig.json"
    packaged.parent.mkdir(parents=True)
    packaged.write_text('{"runtimeOptions": {}}', encoding="utf-8")
    helpers.configure_windows_gui_runtime()
    assert Path(os.environ["PYTHONNET_CORECLR_RUNTIME_CONFIG"]) == packaged


@pytest.mark.parametrize("runtime,kind", [("netfx", ".NET Framework"), ("coreclr", "CoreCLR")])
def test_gui_self_check_loads_real_backend_path_and_reports_loaded_assemblies(monkeypatch, tmp_path, runtime, kind):
    calls = []
    names = ("Microsoft.Web.WebView2.Core", "Microsoft.Web.WebView2.WinForms")
    dll_root = tmp_path / ("net462" if runtime == "netfx" else "lib")
    dll_root.mkdir()
    dlls = {f"{name}.dll": str((dll_root / f"{name}.dll").resolve()) for name in names}
    assemblies = [SimpleNamespace(GetName=lambda n=name: SimpleNamespace(Name=n), Location=dlls[f"{name}.dll"])
                  for name in names]
    system = SimpleNamespace(Environment=SimpleNamespace(Version="4.0.30319" if runtime == "netfx" else "8.0.8"),
                             AppDomain=SimpleNamespace(CurrentDomain=SimpleNamespace(GetAssemblies=lambda: assemblies)))
    launch = SimpleNamespace(_install_pywebview_win32_helper=lambda: calls.append("win32_helper"))
    modules = {"argusai.gui.launch": launch, "webview.platforms.winforms": object(),
               "webview.platforms.edgechromium": object(), "System": system}

    def load(name):
        calls.append(name)
        return modules[name]

    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setenv("PYTHONNET_RUNTIME", runtime)
    monkeypatch.setitem(sys.modules, "pythonnet", SimpleNamespace(get_runtime_info=lambda: SimpleNamespace(kind=kind)))
    monkeypatch.setattr(helpers.importlib, "import_module", load)
    result = helpers.self_check_gui()

    assert result["ok"] is True
    assert result["runtime"] == runtime
    assert result["dotnet_version"] == str(system.Environment.Version)
    assert result["dlls"] == dlls
    assert calls == ["argusai.gui.launch", "win32_helper", "webview.platforms.winforms",
                     "webview.platforms.edgechromium", "System"]
    assert os.environ["PYTHONNET_RUNTIME"] == runtime


def test_gui_self_check_reports_sdk_load_error_without_creating_api(monkeypatch):
    from argusai.gui import Api
    from argusai.core.settings import SettingsManager

    def forbidden(*args, **kwargs):
        pytest.fail("dependency self-check must not initialize user settings or Api")

    def load(name):
        if name == "argusai.gui.launch":
            return SimpleNamespace(_install_pywebview_win32_helper=lambda: None)
        raise FileNotFoundError("Cannot find Microsoft.Web.WebView2.Core.dll")

    monkeypatch.setattr(helpers.sys, "platform", "win32")
    monkeypatch.setattr(Api, "__init__", forbidden)
    monkeypatch.setattr(SettingsManager, "__init__", forbidden)
    monkeypatch.setitem(sys.modules, "pythonnet", SimpleNamespace(get_runtime_info=lambda: None))
    monkeypatch.setattr(helpers.importlib, "import_module", load)

    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["supported"] is True
    assert result["stage"] == "winforms"
    assert "Microsoft.Web.WebView2.Core.dll" in result["error"]


def test_gui_self_check_is_explicitly_unsupported_on_unknown_platform(monkeypatch):
    monkeypatch.setattr(helpers.sys, "platform", "freebsd13")
    monkeypatch.setattr(helpers.importlib, "import_module", lambda name: pytest.fail("must not load an unsupported GUI"))
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["supported"] is False
    assert "not supported on freebsd13" in result["error"]


@pytest.fixture
def unix_gui_modules(monkeypatch):
    calls = []
    requirements = []
    modules = {
        "gi": SimpleNamespace(__version__="3.48.2", require_version=lambda *args: requirements.append(args)),
        "gi.repository.Gtk": SimpleNamespace(Application=object(), init_check=lambda: (True, [])),
        "gi.repository.Gdk": SimpleNamespace(Display=object()),
        "gi.repository.WebKit2": SimpleNamespace(WebView=object()),
        "gi.repository.Soup": SimpleNamespace(Session=object()),
        "webview.platforms.gtk": SimpleNamespace(renderer="gtkwebkit2"),
        "objc": SimpleNamespace(__version__="11.0", lookUpClass=lambda name: object()),
        "AppKit": SimpleNamespace(NSApplication=object()),
        "Foundation": SimpleNamespace(NSObject=object()),
        "WebKit": SimpleNamespace(WKWebView=object()),
        "webview.platforms.cocoa": SimpleNamespace(renderer="wkwebview"),
    }

    def load(name):
        calls.append(name)
        if name not in modules:
            raise ModuleNotFoundError(f"No module named '{name}'")
        return modules[name]

    from argusai.gui import Api
    from argusai.core.settings import SettingsManager

    def forbidden(*args, **kwargs):
        pytest.fail("platform dependency probes must not construct Api or load user settings")

    monkeypatch.setattr(Api, "__init__", forbidden)
    monkeypatch.setattr(SettingsManager, "__init__", forbidden)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(helpers.importlib, "import_module", load)
    return modules, calls, requirements


def test_linux_gui_probe_loads_modern_typelibs_and_real_gtk_backend(monkeypatch, unix_gui_modules):
    _, calls, requirements = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "linux")
    result = helpers.self_check_gui()
    assert result["ok"] is True
    assert result["supported"] is True
    assert result["platform"] == "linux"
    assert result["backend"] == "gtkwebkit2"
    assert requirements == [("Gtk", "3.0"), ("Gdk", "3.0"), ("WebKit2", "4.1"), ("Soup", "3.0")]
    assert calls == ["gi", "gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.WebKit2",
                     "gi.repository.Soup", "webview.platforms.gtk"]
    assert result["versions"]["PyGObject"] == "3.48.2"


@pytest.mark.parametrize("missing", ["gi", "gi.repository.WebKit2", "gi.repository.Soup", "webview.platforms.gtk"])
def test_linux_gui_probe_rejects_missing_python_bridge_or_backend(monkeypatch, unix_gui_modules, missing):
    modules, _, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "linux")
    modules.pop(missing)
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["supported"] is True
    assert missing in result["error"]


@pytest.mark.parametrize("namespace", ["Gtk", "Gdk", "WebKit2", "Soup"])
def test_linux_gui_probe_rejects_missing_required_typelib_without_legacy_fallback(monkeypatch, unix_gui_modules, namespace):
    modules, calls, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "linux")

    def require(name, version):
        if name == namespace:
            raise ValueError(f"Namespace {name} {version} is not available")

    modules["gi"].require_version = require
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["stage"] == "gi_requirements"
    assert namespace in result["error"]
    assert "webview.platforms.gtk" not in calls


@pytest.mark.parametrize("missing_environment", [True, False])
def test_linux_gui_probe_reports_unavailable_display_without_opening_windows(monkeypatch, unix_gui_modules, missing_environment):
    modules, calls, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if missing_environment:
        monkeypatch.delenv("DISPLAY", raising=False)
    else:
        modules["gi.repository.Gtk"].init_check = lambda: (False, [])
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["stage"] == ("display" if missing_environment else "gtk_display")
    assert "xvfb-run" in result["error"]
    assert "webview.platforms.gtk" not in calls


def test_macos_gui_probe_loads_objc_bridges_and_real_cocoa_backend(monkeypatch, unix_gui_modules):
    _, calls, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "darwin")
    result = helpers.self_check_gui()
    assert result["ok"] is True
    assert result["platform"] == "darwin"
    assert result["backend"] == "wkwebview"
    assert result["versions"]["PyObjC"] == "11.0"
    assert calls == ["objc", "AppKit", "Foundation", "WebKit", "webview.platforms.cocoa"]


@pytest.mark.parametrize("missing", ["objc", "AppKit", "Foundation", "WebKit", "webview.platforms.cocoa"])
def test_macos_gui_probe_rejects_missing_bridge_or_backend(monkeypatch, unix_gui_modules, missing):
    modules, _, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", "darwin")
    modules.pop(missing)
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["supported"] is True
    assert missing in result["error"]


@pytest.mark.parametrize("platform,backend", [("linux", "gtk"), ("darwin", "cocoa")])
def test_platform_gui_probe_rejects_wrong_renderer(monkeypatch, unix_gui_modules, platform, backend):
    modules, _, _ = unix_gui_modules
    monkeypatch.setattr(helpers.sys, "platform", platform)
    modules[f"webview.platforms.{backend}"].renderer = "unsupported"
    result = helpers.self_check_gui()
    assert result["ok"] is False
    assert result["stage"] == backend
