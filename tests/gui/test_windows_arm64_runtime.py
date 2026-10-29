"""Native Windows ARM64 desktop CLR selection, without loading a GUI."""

import json
import os
from pathlib import Path

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
