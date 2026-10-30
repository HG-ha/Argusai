"""Update traffic is explicit and never contaminates ordinary CLI output."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from argusai.cli.commands import update as command
from argusai.cli.main import app, _register_commands

_register_commands()
runner = CliRunner()


class Manager:
    cache_dir = Path("verified-update-cache")

    def __init__(self, *, mode="automatic", status="available"):
        self.state = {
            "status": status, "current_version": "0.1.3", "version": "0.1.4",
            "platform": "win-x64", "install_mode": mode, "notes": "[red]literal notes[/red]",
            "release_url": "https://github.com/HG-ha/Argusai/releases/tag/v0.1.4",
            "total_bytes": 100, "downloaded_bytes": 0, "error": "",
        }
        self.check = Mock(side_effect=lambda force: dict(self.state))
        self.start_download = Mock(side_effect=self.download)
        self.cancel_download = Mock()

    def download(self):
        self.state.update(status="downloading")
        return dict(self.state)

    def status(self):
        self.state.update(status="downloaded", downloaded_bytes=100)
        return dict(self.state)


@pytest.fixture(autouse=True)
def english(monkeypatch):
    from argusai.cli.locale import set_locale
    monkeypatch.setenv("ARGUSAI_LANG", "en")
    set_locale("en")


@pytest.mark.parametrize("args", [[], ["--check"], ["--json"], ["--check", "--json"]])
def test_explicit_update_only_checks(monkeypatch, args):
    manager = Manager()
    monkeypatch.setattr(command, "_manager", lambda: manager)
    result = runner.invoke(app, ["update", *args])
    assert result.exit_code == 0, result.output
    manager.check.assert_called_once_with(force=True)
    manager.start_download.assert_not_called()
    if "--json" in args:
        payload = json.loads(result.stdout)
        assert payload["version"] == "0.1.4" and payload["ok"]
    else:
        assert "[red]literal notes[/red]" in result.stdout


def test_download_verifies_then_prints_path_without_installing(monkeypatch):
    manager = Manager()
    monkeypatch.setattr(command, "_manager", lambda: manager)
    result = runner.invoke(app, ["update", "--download", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "downloaded"
    assert payload["package_path"].endswith("ArgusAI-0.1.4-win-x64-setup.exe")
    assert "does not start an installer" in payload["guidance"]
    manager.start_download.assert_called_once()


def test_source_download_never_modifies_environment(monkeypatch):
    manager = Manager(mode="source")
    monkeypatch.setattr(command, "_manager", lambda: manager)
    result = runner.invoke(app, ["update", "--download", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error_code"] == "source_installation"
    assert "Git, uv or pip" in payload["guidance"]
    manager.start_download.assert_not_called()


def test_download_interrupt_cancels_and_returns_130(monkeypatch):
    manager = Manager()
    manager.start_download.side_effect = KeyboardInterrupt
    monkeypatch.setattr(command, "_manager", lambda: manager)
    result = runner.invoke(app, ["update", "--download", "--json"])
    assert result.exit_code == 130
    assert json.loads(result.stdout)["status"] == "cancelled"
    manager.cancel_download.assert_called_once()


def test_check_failure_is_machine_readable(monkeypatch):
    manager = Manager(status="error")
    manager.state["error"] = "offline"
    monkeypatch.setattr(command, "_manager", lambda: manager)
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "offline"


def test_initialization_failure_is_machine_readable(monkeypatch):
    monkeypatch.setattr(command, "_manager", Mock(side_effect=OSError("cache unavailable")))
    result = runner.invoke(app, ["update", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "cache unavailable"


def test_mutually_exclusive_options_do_not_check(monkeypatch):
    factory = Mock(side_effect=AssertionError("Must not create updater"))
    monkeypatch.setattr(command, "_manager", factory)
    result = runner.invoke(app, ["update", "--check", "--download"])
    assert result.exit_code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("args", [["--help"], ["version", "--json"], ["scan", "--help"],
                                  ["chat", "--help"], ["update", "--help"]])
def test_normal_commands_never_construct_updater_or_access_http(monkeypatch, args):
    factory = Mock(side_effect=AssertionError("Unexpected update check"))
    monkeypatch.setattr(command, "_manager", factory)
    monkeypatch.setattr("httpx.Client.send", Mock(side_effect=AssertionError("Unexpected network request")))
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    factory.assert_not_called()
    if args == ["version", "--json"]:
        assert set(json.loads(result.stdout)) == {"ok", "version"}


def test_update_help_localizes(monkeypatch):
    from argusai.cli.locale import set_locale
    from argusai.cli.typer_i18n import build_localized_click_app
    set_locale("zh")
    root = build_localized_click_app(app)
    update_command = root.commands["update"]
    assert "主动检查更新" in update_command.help
    download = next(p for p in update_command.params if "--download" in p.opts)
    assert "下载并校验" in download.help
    set_locale("en")
