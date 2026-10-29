"""Shell default setting + bash/heredoc routing guards."""

from __future__ import annotations

from unittest import mock

import pytest

from argusai.core.settings import normalize_default_shell
from argusai.core.workspace.session_sandbox import (
    _is_wsl_bash_stub,
    _normalize_bash_newlines,
    _resolve_snippet_shell,
    _script_looks_like_bash,
    _ssh_stdin_risk_hint,
    clear_shell_capability_note_cache,
    shell_capability_note,
)


def test_normalize_default_shell() -> None:
    assert normalize_default_shell("AUTO") == "auto"
    assert normalize_default_shell("PowerShell") == "powershell"
    assert normalize_default_shell("weird") == "auto"


@pytest.mark.parametrize(
    "script",
    [
        "#!/bin/bash\necho hi",
        "cat <<'EOF'\nhello\nEOF",
        "set -euo pipefail\nss -tuln",
        "ufw status",
    ],
)
def test_script_looks_like_bash(script: str) -> None:
    assert _script_looks_like_bash(script)


def test_bash_heredoc_routes_to_bash_when_available() -> None:
    script = "cat <<'EOF'\nx\nEOF"
    with mock.patch(
        "argusai.core.workspace.session_sandbox.find_bash_executable",
        return_value=r"C:\Program Files\Git\bin\bash.exe",
    ):
        shell, note, err = _resolve_snippet_shell("", script, default_shell="powershell")
    assert err is None
    assert shell == "bash"
    assert note and "bash" in note


def test_bash_heredoc_errors_when_bash_missing() -> None:
    script = "#!/usr/bin/env bash\necho hi"
    with mock.patch(
        "argusai.core.workspace.session_sandbox.find_bash_executable",
        return_value=None,
    ):
        shell, note, err = _resolve_snippet_shell("", script, default_shell="cmd")
    assert shell == "cmd"
    assert note is None
    assert err and "Refusing to auto-route" in err
    assert "shell_session_start" in err


def test_default_shell_honored_when_empty_request() -> None:
    with mock.patch("argusai.core.workspace.session_sandbox.platform.system", return_value="Windows"):
        with mock.patch(
            "argusai.core.workspace.session_sandbox.find_bash_executable",
            return_value=None,
        ):
            shell, note, err = _resolve_snippet_shell("", "echo hi", default_shell="cmd")
    assert err is None
    assert shell == "cmd"
    assert note is None


def test_shell_capability_note_mentions_default() -> None:
    clear_shell_capability_note_cache()
    fake = {
        "platform": "Windows",
        "shells": [
            {"name": "powershell", "available": True, "version": "5.1"},
            {"name": "bash", "available": True, "version": "5.0"},
        ],
    }
    with mock.patch(
        "argusai.core.workspace.session_sandbox.discover_shell_environments",
        return_value=fake,
    ):
        clear_shell_capability_note_cache()
        text = shell_capability_note(default_shell="bash")
    assert "Settings default shell" in text
    assert "`bash`" in text


def test_shell_capability_note_treats_git_bash_and_mentions_ssh() -> None:
    clear_shell_capability_note_cache()
    fake = {
        "platform": "Windows",
        "shells": [
            {"name": "powershell", "available": True, "version": "5.1"},
            {"name": "git-bash", "available": True, "version": "5.2"},
        ],
    }
    with mock.patch(
        "argusai.core.workspace.session_sandbox.discover_shell_environments",
        return_value=fake,
    ):
        clear_shell_capability_note_cache()
        text = shell_capability_note(default_shell="auto")
    assert "Git Bash" in text or "available" in text
    assert "ssh -n" in text or "shell_session_start" in text


def test_wsl_bash_stub_detection() -> None:
    assert _is_wsl_bash_stub(r"C:\Windows\System32\bash.exe")
    assert _is_wsl_bash_stub(r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe")
    assert not _is_wsl_bash_stub(r"C:\Program Files\Git\bin\bash.exe")


def test_normalize_bash_newlines_strips_crlf() -> None:
    assert _normalize_bash_newlines("cat <<'EOF'\r\nhello\r\nEOF\r\n") == "cat <<'EOF'\nhello\nEOF\n"


def test_ssh_stdin_risk_hint() -> None:
    assert _ssh_stdin_risk_hint("echo hi") is None
    assert _ssh_stdin_risk_hint("ssh -n host 'uptime'") is None
    assert _ssh_stdin_risk_hint("ssh host 'uptime'") is not None
    assert "BatchMode" in (_ssh_stdin_risk_hint("ssh user@host bash -s") or "")
