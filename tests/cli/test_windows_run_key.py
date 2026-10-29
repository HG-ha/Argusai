"""HKCU Run key helpers for Windows logon autostart."""

from __future__ import annotations

from argusai.cli.windows_run_key import (
    VALUE_NAME,
    _approved_is_enabled,
    run_key_command_line,
)


def test_run_value_name_matches_nsis_and_app() -> None:
    assert VALUE_NAME == "ArgusAI"


def test_command_line_quotes_spaces() -> None:
    line = run_key_command_line([r"C:\Program Files\ArgusAI\ArgusAI.exe", "--gui", "--tray"])
    assert "ArgusAI.exe" in line
    assert "--gui" in line
    assert "--tray" in line


def test_startup_approved_blob() -> None:
    assert _approved_is_enabled(b"") is True
    assert _approved_is_enabled(b"\x02\x00\x00\x00") is True
    assert _approved_is_enabled(b"\x03\x00\x00\x00") is False
