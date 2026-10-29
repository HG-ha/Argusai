"""Sandbox vs host Python execution tier policy."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from argusai.core.workspace import session_sandbox
from argusai.core.workspace.session_sandbox import (
    _build_shell_capability_note,
    _dangerous_shell_reason,
    _host_interpreter_escape_reason,
    _prepend_bundled_runtime_path,
    _probe_sandbox_python_env,
    _python_subprocess_env,
    _reject_sandbox_python_env,
    _resolve_snippet_shell,
    _script_uses_andor_chaining,
    _validate_host_escalation_disallowed_use,
    _vendored_python,
    ensure_python_venv,
    run_host_python_snippet,
    run_python_snippet,
    run_shell_snippet,
    sandbox_pip_install,
    sandbox_status,
    write_file,
)


def test_script_uses_andor_chaining_detects_operators() -> None:
    assert _script_uses_andor_chaining("node -v && npm -v")
    assert _script_uses_andor_chaining("node -v || echo missing")
    # Operators inside quotes are not command chaining.
    assert not _script_uses_andor_chaining('Write-Output "a && b"')
    assert not _script_uses_andor_chaining("echo hello")


def _force_windows(monkeypatch, *, has_pwsh: bool) -> None:
    monkeypatch.setattr(session_sandbox.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        session_sandbox.shutil,
        "which",
        lambda name: (f"C:/{name}.exe" if (name != "pwsh" or has_pwsh) else None),
    )


def test_resolve_snippet_shell_routes_andor_to_cmd_without_pwsh(monkeypatch) -> None:
    _force_windows(monkeypatch, has_pwsh=False)
    # Even when the caller explicitly asked for powershell, a && chain is broken
    # on Windows PowerShell 5.1, so we route to cmd.
    shell, note, error = _resolve_snippet_shell("powershell", "node -v && npm -v")
    assert shell == "cmd"
    assert note and "cmd" in note
    assert error is None


def test_resolve_snippet_shell_prefers_pwsh_when_available(monkeypatch) -> None:
    _force_windows(monkeypatch, has_pwsh=True)
    shell, note, error = _resolve_snippet_shell("powershell", "node -v && npm -v")
    assert shell == "pwsh"
    assert note and "pwsh" in note
    assert error is None


def test_resolve_snippet_shell_leaves_plain_powershell_alone(monkeypatch) -> None:
    _force_windows(monkeypatch, has_pwsh=False)
    shell, note, error = _resolve_snippet_shell("powershell", "Get-ChildItem")
    assert shell == "powershell"
    assert note is None
    assert error is None


def test_resolve_snippet_shell_respects_explicit_cmd(monkeypatch) -> None:
    _force_windows(monkeypatch, has_pwsh=False)
    shell, note, error = _resolve_snippet_shell("cmd", "node -v && npm -v")
    assert shell == "cmd"
    assert note is None
    assert error is None


def test_shell_capability_note_warns_about_ps51(monkeypatch) -> None:
    monkeypatch.setattr(session_sandbox, "discover_shell_environments", lambda: {
        "ok": True,
        "platform": "Windows",
        "shells": [
            {"name": "powershell", "available": True, "version": "5.1.22621.5697"},
            {"name": "pwsh", "available": False, "version": ""},
            {"name": "cmd", "available": True, "version": "Microsoft Windows [..]"},
        ],
    })
    note = _build_shell_capability_note()
    assert "Windows PowerShell 5.1" in note
    assert "does NOT support" in note
    assert "cmd" in note
    # Garbled localized cmd version must not leak into the note.
    assert "Microsoft Windows" not in note


def test_shell_capability_note_prefers_pwsh(monkeypatch) -> None:
    monkeypatch.setattr(session_sandbox, "discover_shell_environments", lambda: {
        "ok": True,
        "platform": "Windows",
        "shells": [
            {"name": "powershell", "available": True, "version": "5.1.22621"},
            {"name": "pwsh", "available": True, "version": "7.4.1"},
        ],
    })
    note = _build_shell_capability_note()
    assert "pwsh" in note
    assert "PowerShell 7" in note


def test_shell_capability_note_posix_defaults_bash(monkeypatch) -> None:
    monkeypatch.setattr(session_sandbox, "discover_shell_environments", lambda: {
        "ok": True,
        "platform": "Linux",
        "shells": [{"name": "bash", "available": True, "version": "5.2.21"}],
    })
    note = _build_shell_capability_note()
    assert "`bash`" in note
    assert "PowerShell" not in note


def test_reject_sandbox_python_env_non_empty() -> None:
    blocked = _reject_sandbox_python_env("C:\\Python311\\python.exe")
    assert blocked is not None
    assert blocked["error_code"] == "sandbox_host_escape_blocked"


def test_host_interpreter_escape_detects_windows_absolute_python() -> None:
    script = '& "C:\\Users\\Example\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" app.py'
    reason = _host_interpreter_escape_reason(script)
    assert reason
    assert "run_host_python" in reason


def test_host_interpreter_escape_detects_py_launcher() -> None:
    assert _host_interpreter_escape_reason("py -3.11 -c \"import tkinter\"")


def test_dangerous_shell_reason_blocks_host_python_in_shell() -> None:
    reason = _dangerous_shell_reason("py -3 demo.py")
    assert reason
    assert "host interpreter escape" in reason.lower() or "blocked" in reason.lower()


def test_run_python_snippet_rejects_python_env(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    result = asyncio.run(
        run_python_snippet(root, "print(1)", python_env="python3")
    )
    assert result["ok"] is False
    assert result["error_code"] == "sandbox_host_escape_blocked"


def test_run_python_snippet_rejects_embedded_host_path(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    code = 'import subprocess\nsubprocess.run([r"C:\\Python311\\python.exe", "-c", "pass"])'
    result = asyncio.run(run_python_snippet(root, code))
    assert result["ok"] is False
    assert result["error_code"] == "sandbox_host_escape_blocked"


def test_run_shell_snippet_blocks_py_launcher(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    result = asyncio.run(run_shell_snippet(root, "py -3 -c \"print(1)\""))
    assert result["ok"] is False
    assert "host interpreter escape" in result["error"].lower() or "blocked" in result["error"].lower()


def test_run_host_python_snippet_requires_reason_and_summary(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    result = asyncio.run(
        run_host_python_snippet(
            root,
            "print('ok')",
            reason="short",
            sandbox_attempt_summary="also short",
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "host_escalation_reason_required"


def test_run_host_python_snippet_runs_with_host_interpreter(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    result = asyncio.run(
        run_host_python_snippet(
            root,
            "print('host-tier-ok')",
            reason="Sandbox bundled runtime lacks tkinter for GUI demo.",
            sandbox_attempt_summary="run_python_snippet failed with ModuleNotFoundError: tkinter.",
        )
    )
    assert result.get("execution_tier") == "host"
    assert result.get("host_python") is True
    if result.get("ok"):
        assert "host-tier-ok" in (result.get("stdout_excerpt") or "")
    else:
        # CI may lack a usable host interpreter; still validate envelope shape.
        assert result.get("error_code") in {None, "host_interpreter_unavailable"}


def test_run_host_python_script_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    write_file(root, "probe.py", "print('from-file')", "py")
    result = asyncio.run(
        run_host_python_snippet(
            root,
            "import runpy; runpy.run_path('probe.py')",
            reason="Need host stdlib module unavailable in sandbox venv.",
            sandbox_attempt_summary="ImportError in run_python_script on probe.py.",
        )
    )
    assert result.get("execution_tier") == "host"


def test_host_escalation_blocks_pyinstaller_snippet(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    blocked = _validate_host_escalation_disallowed_use(
        "import PyInstaller.__main__\nPyInstaller.__main__.run(['--onefile', 'game.py'])"
    )
    assert blocked is not None
    assert blocked["error_code"] == "host_escalation_disallowed_use"


def test_host_escalation_blocks_pip_install_snippet(tmp_path: Path) -> None:
    blocked = _validate_host_escalation_disallowed_use("import subprocess\nsubprocess.run(['pip', 'install', 'pygame'])")
    assert blocked is not None
    assert "sandbox_pip_install" in blocked["suggested_fix"]


def test_sandbox_status_includes_python_env_probe(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    status = sandbox_status(root)
    assert status["ok"] is True
    env = status["python_venv"]
    assert "pip_available" in env
    assert "bundled_runtime_ok" in env
    assert "hint" in env


def test_probe_sandbox_python_env_without_venv(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    probe = _probe_sandbox_python_env(root)
    assert probe["pip_available"] is False
    assert "hint" in probe


def test_sandbox_pip_install_requires_packages(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    result = asyncio.run(sandbox_pip_install(root, ""))
    assert result["ok"] is False
    assert result["error_code"] == "pip_packages_empty"


def test_parse_pip_packages_handles_json_array_string() -> None:
    from argusai.core.workspace.session_sandbox import _parse_pip_packages

    assert _parse_pip_packages('["pyinstaller"]') == ["pyinstaller"]
    assert _parse_pip_packages('["requests", "httpx"]') == ["requests", "httpx"]
    assert _parse_pip_packages(["pyinstaller"]) == ["pyinstaller"]
    assert _parse_pip_packages("pygame pyinstaller") == ["pygame", "pyinstaller"]


def test_python_subprocess_env_prepends_bundled_runtime_on_windows() -> None:
    if not sys.platform.startswith("win"):
        return
    bundled = _vendored_python()
    if not bundled:
        return
    runtime_dir = Path(bundled).resolve().parent
    env: dict[str, str] = {"PATH": "C:\\Windows\\System32"}
    _prepend_bundled_runtime_path(env)
    assert str(runtime_dir / "DLLs") in env["PATH"]
    assert str(runtime_dir) in env["PATH"]


def test_ensure_python_venv_works_in_empty_sandbox_dir(tmp_path: Path) -> None:
    if not sys.platform.startswith("win") or not _vendored_python():
        return
    root = tmp_path / "sandbox"
    root.mkdir()
    result = ensure_python_venv(root)
    assert result["ok"] is True
    assert (root / ".venv" / "Scripts" / "python.exe").exists()


def _kill_pid(pid: int) -> None:
    import subprocess

    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            import os
            import signal

            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def test_run_shell_snippet_background_detaches_without_timeout(tmp_path: Path) -> None:
    """GUI/long-lived processes must return ok+detached, not timed_out+killed."""
    root = tmp_path / "sandbox"
    root.mkdir()
    if sys.platform.startswith("win"):
        script = "Start-Sleep -Seconds 120"
        shell = "powershell"
    else:
        script = "sleep 120"
        shell = "bash"
    result = asyncio.run(
        run_shell_snippet(root, script, shell=shell, timeout_seconds=30, background=True)
    )
    try:
        assert result.get("ok") is True
        assert result.get("detached") is True
        assert result.get("timed_out") is False
        assert result.get("background") is True
        assert int(result.get("pid") or 0) > 0
        assert "SUCCESS" in str(result.get("hint") or "")
    finally:
        if result.get("pid"):
            _kill_pid(int(result["pid"]))


def test_foreground_timeout_hint_mentions_background(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    if sys.platform.startswith("win"):
        script = "Start-Sleep -Seconds 30"
        shell = "powershell"
    else:
        script = "sleep 30"
        shell = "bash"
    result = asyncio.run(run_shell_snippet(root, script, shell=shell, timeout_seconds=1))
    assert result.get("timed_out") is True
    assert result.get("ok") is False
    assert "background=true" in str(result.get("hint") or "")
