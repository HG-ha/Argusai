"""Unit tests for Frida CLI bridge (mocked subprocess)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from argusai.core.reverse.frida_runtime import _build_command, run_frida


@patch("argusai.core.reverse.frida_runtime._frida_path", return_value=("/usr/bin/frida", {"available": True}))
def test_build_command_spawn(_mock: MagicMock) -> None:
    cmd = _build_command(mode="spawn", target="com.example.app", device="usb", script_file=None)
    assert cmd[:3] == ["/usr/bin/frida", "-U", "-f"]
    assert "com.example.app" in cmd


@patch("argusai.core.reverse.frida_runtime._frida_path", return_value=("/usr/bin/frida", {"available": True}))
@patch("argusai.core.reverse.frida_runtime.subprocess.run")
def test_run_frida_foreground(mock_run: MagicMock, _mock_path: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
    out = run_frida(mode="attach_name", target="Calculator", device="", script="console.log(1);", timeout_seconds=5)
    assert out["ok"] is True
    assert "log_preview" in out
    mock_run.assert_called_once()


@patch("argusai.core.reverse.frida_runtime._frida_path", return_value=(None, {"available": False}))
def test_run_frida_missing_toolchain(_mock: MagicMock) -> None:
    out = run_frida(mode="attach_name", target="foo", script="console.log(1);")
    assert out["ok"] is False
    assert "frida not found" in out["error"]
