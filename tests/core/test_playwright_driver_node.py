"""Playwright's driver Node is resolved from the managed runtime when unbundled."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from argusai.core.browser import browser_locator


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYWRIGHT_NODEJS_PATH", raising=False)


def _fake_playwright(tmp_path: Path, *, with_node: bool) -> object:
    package = tmp_path / "playwright"
    driver = package / "driver"
    driver.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    if with_node:
        name = "node.exe" if sys.platform == "win32" else "node"
        (driver / name).write_text("", encoding="utf-8")

    class _Module:
        __file__ = str(package / "__init__.py")

    return _Module()


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", r"C:\custom\node.exe")

    info = browser_locator.playwright_driver_node()

    assert info["ok"] is True
    assert info["source"] == "env"
    assert info["path"] == r"C:\custom\node.exe"


def _hide_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import argusai.core.managed_runtimes as managed_runtimes

    monkeypatch.setattr(managed_runtimes, "runtime_status", lambda *a, **k: {"healthy": False})
    monkeypatch.setattr(managed_runtimes, "node_executable", lambda *a, **k: tmp_path / "missing-node.exe")


def test_sandbox_beats_playwright_bundled_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=True)
    monkeypatch.setitem(sys.modules, "playwright", module)
    managed = tmp_path / "sandbox" / "node.exe"
    managed.parent.mkdir()
    managed.write_text("", encoding="utf-8")

    import argusai.core.managed_runtimes as managed_runtimes

    monkeypatch.setattr(managed_runtimes, "runtime_status", lambda *a, **k: {"healthy": True})
    monkeypatch.setattr(managed_runtimes, "node_executable", lambda *a, **k: managed)

    info = browser_locator.playwright_driver_node()

    assert info["ok"] is True
    assert info["source"] == "managed"
    assert info["path"] == str(managed)


def test_bundled_node_is_only_the_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=True)
    monkeypatch.setitem(sys.modules, "playwright", module)
    _hide_sandbox(monkeypatch, tmp_path)

    info = browser_locator.playwright_driver_node()

    assert info["ok"] is True
    assert info["source"] == "bundled"


def test_packaged_build_falls_back_to_managed_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=False)
    monkeypatch.setitem(sys.modules, "playwright", module)
    managed = tmp_path / "managed" / "node.exe"
    managed.parent.mkdir(parents=True)
    managed.write_text("", encoding="utf-8")

    import argusai.core.managed_runtimes as managed_runtimes

    monkeypatch.setattr(managed_runtimes, "runtime_status", lambda *a, **k: {"healthy": True})
    monkeypatch.setattr(managed_runtimes, "node_executable", lambda *a, **k: managed)

    info = browser_locator.ensure_playwright_driver_node()

    assert info["ok"] is True
    assert info["source"] == "managed"
    import os

    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == str(managed)


def test_install_argv_uses_driver_cli_not_this_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=True)
    cli = tmp_path / "playwright" / "driver" / "package" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "playwright", module)
    _hide_sandbox(monkeypatch, tmp_path)

    argv = browser_locator.playwright_install_argv("chromium")

    assert argv[0] != sys.executable
    assert argv[1].endswith("cli.js")
    assert argv[2:] == ["install", "chromium"]
    assert "-m" not in argv


def test_missing_managed_node_reports_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=False)
    monkeypatch.setitem(sys.modules, "playwright", module)

    system = tmp_path / "system-node.exe"
    system.write_text("", encoding="utf-8")
    _hide_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(browser_locator.shutil, "which", lambda *a, **k: str(system))
    monkeypatch.setattr(browser_locator, "_usable_node", lambda path: Path(path) == system)

    info = browser_locator.playwright_driver_node()

    assert info["ok"] is False
    assert info["runtime_required"] == "node"
    assert "runtime_install" in info["error"]


def test_tools_dir_follows_persisted_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "store"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"data_root": str(data)}), encoding="utf-8")
    monkeypatch.setattr(browser_locator, "_settings_file", lambda: settings)

    assert browser_locator._managed_tools_dir() == data / "tools"


def test_stale_lockfile_still_uses_runnable_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _fake_playwright(tmp_path, with_node=False)
    monkeypatch.setitem(sys.modules, "playwright", module)
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")

    import argusai.core.managed_runtimes as managed_runtimes

    monkeypatch.setattr(browser_locator, "_managed_tools_dir", lambda: tmp_path)
    monkeypatch.setattr(
        managed_runtimes,
        "runtime_status",
        lambda *a, **k: {"healthy": False, "error": "lockfile changed"},
    )
    monkeypatch.setattr(managed_runtimes, "node_executable", lambda *a, **k: node)
    monkeypatch.setattr(browser_locator, "_usable_node", lambda path: Path(path) == node)

    info = browser_locator.ensure_playwright_driver_node()

    assert info["ok"] is True
    assert info["source"] == "managed"
    import os

    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == str(node)
