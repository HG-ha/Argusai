"""CLI dispatch smoke tests (Typer CliRunner)."""

from __future__ import annotations

import os
import json
import sys

import pytest

from typer.testing import CliRunner

# Stable English help for assertions (override OS UI language).
os.environ["ARGUSAI_LANG"] = "en"

from argusai.cli.locale import bootstrap_locale
from argusai.cli.main import app, _register_commands

bootstrap_locale(force=True)
_register_commands()
runner = CliRunner()


def test_version_command() -> None:
    from argusai import __version__

    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_scan_help() -> None:
    result = runner.invoke(app, ["scan", "--help"])
    assert result.exit_code == 0
    assert "Target URL" in result.stdout or "url" in result.stdout.lower()


def test_doctor_help() -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0


def test_channels_serve_help() -> None:
    result = runner.invoke(app, ["channels", "serve", "--help"])
    assert result.exit_code == 0


def test_report_requires_path() -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code != 0


def test_proxy_help() -> None:
    result = runner.invoke(app, ["proxy", "--help"])
    assert result.exit_code == 0


def test_chat_help() -> None:
    result = runner.invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "REPL" in out or "session" in out.lower() or "对话" in out


def test_serve_install_option_help_zh(monkeypatch) -> None:
    from argusai.cli.locale import bootstrap_locale, label, set_locale
    from argusai.cli.typer_i18n import build_localized_click_app

    monkeypatch.setenv("ARGUSAI_LANG", "zh")
    bootstrap_locale(force=True)
    click_root = build_localized_click_app(app)
    install = click_root.commands["serve"].commands["install"]
    cron_opt = next(p for p in install.params if "--cron-only" in (p.opts or []))
    assert cron_opt.help == label("help_opt_cron_only")
    boot_opt = next(p for p in install.params if "--boot" in (p.opts or []))
    assert boot_opt.help == label("help_opt_boot")
    monkeypatch.setenv("ARGUSAI_LANG", "en")
    bootstrap_locale(force=True)
    set_locale("en")


def test_channels_help_follows_lang(monkeypatch) -> None:
    from click import Context

    from argusai.cli.locale import bootstrap_locale, label, set_locale
    from argusai.cli.typer_i18n import build_localized_click_app

    monkeypatch.setenv("ARGUSAI_LANG", "zh")
    bootstrap_locale(force=True)
    click_root = build_localized_click_app(app)
    ch = click_root.commands["channels"]
    add = ch.commands["add"]
    assert ch.help == label("help_channels")
    assert add.help == label("help_channels_add")
    name_opt = next(p for p in add.params if "--name" in (p.opts or []))
    assert name_opt.help == label("help_opt_name")
    help_opt = add.get_help_option(Context(add, info_name="add"))
    assert help_opt is not None
    assert help_opt.help == label("help_opt_help")
    monkeypatch.setenv("ARGUSAI_LANG", "en")
    bootstrap_locale(force=True)
    set_locale("en")


def test_root_help_mentions_auto_detect() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--gui" in result.stdout
    assert "--chat" in result.stdout
    assert "--self-check-gui" not in result.stdout


@pytest.mark.parametrize("report,exit_code", [
    ({"ok": True, "supported": True, "runtime": "netfx", "dotnet_version": "4.0.30319", "dlls": {}}, 0),
    ({"ok": False, "supported": True, "error": "Cannot find Microsoft.Web.WebView2.Core.dll"}, 1),
    ({"ok": False, "supported": False, "error": "only supported on Windows"}, 2),
])
def test_gui_self_check_dispatches_before_console_and_user_config(monkeypatch, capsys, report, exit_code) -> None:
    from argusai import __main__ as entry
    from argusai.gui import _helpers as helpers
    from argusai.core.settings import SettingsManager

    def forbidden(*args, **kwargs):
        pytest.fail("self-check must not hide/relaunch the console or read user settings")

    monkeypatch.setattr(sys, "argv", ["ArgusAI.exe", "--self-check-gui"])
    monkeypatch.setattr(entry, "_hide_console_for_desktop_launch", forbidden)
    monkeypatch.setattr(entry, "_bootstrap_logging", forbidden)
    monkeypatch.setattr(SettingsManager, "__init__", forbidden)
    monkeypatch.setattr(helpers, "self_check_gui", lambda: report)
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == exit_code
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("platform,exit_code", [("win32", 1), ("linux", 1), ("darwin", 1), ("freebsd13", 2)])
def test_gui_self_check_import_failure_has_platform_aware_json(monkeypatch, capsys, platform, exit_code) -> None:
    from argusai import __main__ as entry
    from argusai.gui import _helpers as helpers

    def fail():
        raise ImportError("GUI bridge unavailable")

    monkeypatch.setattr(sys, "argv", ["ArgusAI", "--self-check-gui"])
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(helpers, "self_check_gui", fail)
    monkeypatch.setattr(entry, "_hide_console_for_desktop_launch", lambda: pytest.fail("must keep console"))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    report = json.loads(capsys.readouterr().out)
    assert exc.value.code == exit_code
    assert report["ok"] is False
    assert report["platform"] == platform
    assert report["supported"] is (exit_code == 1)
    assert report["error"] == "ImportError: GUI bridge unavailable"


def test_root_chat_passes_concrete_attachment_default(monkeypatch) -> None:
    from argusai.cli.commands import chat as chat_mod

    called: dict = {}
    monkeypatch.setattr(chat_mod, "chat", lambda **kwargs: called.update(kwargs))

    result = runner.invoke(app, ["--chat"])

    assert result.exit_code == 0
    assert called["files"] is None


def test_chat_once_requires_message() -> None:
    result = runner.invoke(app, ["chat", "--once"])
    assert result.exit_code != 0


def test_chat_team_use_is_one_opening_line(monkeypatch) -> None:
    import typer

    from argusai.cli.commands.chat import _opening_line

    assert _opening_line(["/team", "use", "6f774dd62f7d42c8"]) == "/team use 6f774dd62f7d42c8"
    assert _opening_line(["team", "use", "6f774dd62f7d42c8"]) == "/team use 6f774dd62f7d42c8"
    assert _opening_line(["look", "at", "the", "login"]) == "look at the login"

    def stop(cls):  # noqa: ARG001
        raise typer.Exit(0)

    monkeypatch.setattr("argusai.gui.Api.for_cli", classmethod(stop))
    entered = runner.invoke(app, ["chat", "/team", "use", "6f774dd62f7d42c8"])
    plain = runner.invoke(app, ["chat", "team", "use", "6f774dd62f7d42c8"])
    assert "unexpected extra argument" not in entered.output.lower()
    assert "unexpected extra argument" not in plain.output.lower()
    assert entered.exit_code == 0
    assert plain.exit_code == 0
