"""Tests for frozen/dev self-relaunch argv resolution."""

from __future__ import annotations

from pathlib import Path

from argusai.core.infra import runtime_exec


def test_dev_self_relaunch_uses_python_module(monkeypatch) -> None:
    monkeypatch.setattr(runtime_exec, "is_frozen_runtime", lambda: False)
    monkeypatch.setattr(runtime_exec.sys, "executable", r"C:\Python\python.exe")
    assert runtime_exec.self_relaunch_command("--channel-stream-sidecar") == [
        r"C:\Python\python.exe",
        "-m",
        "argusai",
        "--channel-stream-sidecar",
    ]


def test_frozen_self_relaunch_prefers_existing_argv0(monkeypatch, tmp_path: Path) -> None:
    fake_exe = tmp_path / "ArgusAI.exe"
    fake_exe.write_bytes(b"mz")
    missing_python = tmp_path / "missing" / "python.exe"
    monkeypatch.setattr(runtime_exec, "is_frozen_runtime", lambda: True)
    monkeypatch.setattr(runtime_exec.sys, "argv", [str(fake_exe), "--gui"])
    monkeypatch.setattr(runtime_exec.sys, "executable", str(missing_python))
    assert runtime_exec.self_relaunch_command("--channel-stream-sidecar") == [
        str(fake_exe.resolve()),
        "--channel-stream-sidecar",
    ]


def test_self_relaunch_env_strips_onefile_parent(monkeypatch) -> None:
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "1234")
    monkeypatch.setenv("KEEP_ME", "1")
    env = runtime_exec.self_relaunch_env()
    assert "NUITKA_ONEFILE_PARENT" not in env
    assert env.get("KEEP_ME") == "1"
