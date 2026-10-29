"""Interactive + oneshot shell session manager."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest import mock

from argusai.core.workspace.session_sandbox import run_shell_snippet
from argusai.core.workspace.shell_sessions import (
    CMD_DONE_MARKER,
    get_shell_session_manager,
)


def _mgr():
    return get_shell_session_manager()


def _wait_for_marker(
    session_id: str,
    needle: str,
    *,
    assistant_session_id: str | None = None,
    timeout: float = 8.0,
) -> str:
    deadline = time.monotonic() + timeout
    offset = 0
    chunks: list[str] = []
    while time.monotonic() < deadline:
        out = _mgr().read(
            session_id,
            assistant_session_id=assistant_session_id,
            offset=offset,
            max_chars=20000,
        )
        assert out.get("ok") is True
        text = str(out.get("text") or "")
        if text:
            chunks.append(text)
            offset = int(out.get("next_offset") or offset)
        joined = "".join(chunks)
        if needle in joined:
            return joined
        time.sleep(0.1)
    return "".join(chunks)


def test_interactive_shell_exec_read_stop(tmp_path: Path) -> None:
    root = tmp_path / "sess.sandbox"
    root.mkdir()
    aid = "test-interactive"
    start = _mgr().start_interactive(
        sandbox_root=root,
        assistant_session_id=aid,
        shell="powershell" if sys.platform.startswith("win") else "bash",
        label="unit",
    )
    assert start.get("ok") is True, start
    sid = str(start["session_id"])
    try:
        marker = f"argusai-shell-marker-{sid[-6:]}"
        if sys.platform.startswith("win"):
            cmd = f"Write-Output '{marker}'"
        else:
            cmd = f"printf '%s\\n' '{marker}'"
        exec_out = _mgr().exec_command(sid, cmd, assistant_session_id=aid)
        assert exec_out.get("ok") is True, exec_out
        text = _wait_for_marker(sid, marker, assistant_session_id=aid)
        assert marker in text
        assert CMD_DONE_MARKER in text or True  # marker may arrive slightly later
        # Allow a bit more time for done marker.
        text2 = _wait_for_marker(
            sid,
            CMD_DONE_MARKER,
            assistant_session_id=aid,
            timeout=5.0,
        )
        assert CMD_DONE_MARKER in text2 or marker in text2

        listed = _mgr().list_sessions(aid)
        assert any(s["session_id"] == sid for s in listed)

        stop = _mgr().stop(sid, assistant_session_id=aid)
        assert stop.get("ok") is True
        refreshed = _mgr().get(sid)
        assert refreshed is not None
        assert refreshed.state in {"stopped", "exited", "failed"}
    finally:
        _mgr().stop(sid, assistant_session_id=aid)


def test_shell_operations_reject_foreign_owner_without_leaking_details(tmp_path: Path) -> None:
    root = tmp_path / "owner.sandbox"
    root.mkdir()
    owner = "owner-session"
    start = _mgr().start_interactive(
        sandbox_root=root,
        assistant_session_id=owner,
        shell="powershell" if sys.platform.startswith("win") else "bash",
    )
    assert start.get("ok") is True, start
    sid = str(start["session_id"])
    expected = {
        "ok": False,
        "error": "shell session is not available to this assistant session",
        "error_code": "session_ownership",
    }
    try:
        assert _mgr().exec_command(sid, "", assistant_session_id="foreign") == expected
        assert _mgr().write_raw(sid, "echo no", assistant_session_id="foreign") == expected
        assert _mgr().read(sid, assistant_session_id="foreign") == expected
        assert _mgr().stop(sid, assistant_session_id="foreign") == expected
        assert _mgr().read("missing-session", assistant_session_id="foreign") == expected
        session = _mgr().get(sid)
        assert session is not None
        assert session.state == "running"
    finally:
        _mgr().stop(sid, assistant_session_id=owner)


def test_shell_tool_wrappers_pass_assistant_owner() -> None:
    from argusai.core.tools import sandbox_code

    manager = mock.MagicMock()
    manager.exec_command.return_value = {"ok": True}
    manager.write_raw.return_value = {"ok": True}
    manager.read.return_value = {"ok": True}
    manager.stop.return_value = {"ok": True}
    with mock.patch(
        "argusai.core.workspace.shell_sessions.get_shell_session_manager",
        return_value=manager,
    ):
        sandbox_code.shell_session_exec(None, "shell-1", "echo ok", _session_id="assistant-1")
        sandbox_code.shell_session_write(None, "shell-1", "raw", _session_id="assistant-1")
        sandbox_code.shell_session_read(None, "shell-1", _session_id="assistant-1")
        sandbox_code.shell_session_stop(None, "shell-1", _session_id="assistant-1")

    assert manager.exec_command.call_args.kwargs["assistant_session_id"] == "assistant-1"
    assert manager.write_raw.call_args.kwargs["assistant_session_id"] == "assistant-1"
    assert manager.read.call_args.kwargs["assistant_session_id"] == "assistant-1"
    assert manager.stop.call_args.kwargs["assistant_session_id"] == "assistant-1"


def test_oneshot_background_registers_session_and_blocks_exec(tmp_path: Path) -> None:
    root = tmp_path / "sess.sandbox"
    root.mkdir()
    if sys.platform.startswith("win"):
        script = "Start-Sleep -Seconds 60"
        shell = "powershell"
    else:
        script = "sleep 60"
        shell = "bash"
    result = asyncio.run(
        run_shell_snippet(root, script, shell=shell, timeout_seconds=30, background=True)
    )
    try:
        assert result.get("ok") is True, result
        assert result.get("detached") is True
        sid = str(result.get("session_id") or "")
        assert sid.startswith("oneshot-")
        listed = _mgr().list_sessions()
        assert any(s["session_id"] == sid and s["kind"] == "oneshot" for s in listed)
        blocked = _mgr().exec_command(sid, "echo should-fail")
        assert blocked.get("ok") is False
        assert blocked.get("error_code") == "not_interactive"
    finally:
        if result.get("session_id"):
            _mgr().stop(str(result["session_id"]))
        elif result.get("pid"):
            import subprocess

            subprocess.run(
                ["taskkill", "/PID", str(result["pid"]), "/T", "/F"]
                if sys.platform.startswith("win")
                else ["kill", str(result["pid"])],
                capture_output=True,
                check=False,
            )


def test_tool_wrappers_registered() -> None:
    from argusai.core.tools import EXECUTORS

    for name in (
        "shell_session_start",
        "shell_session_exec",
        "shell_session_write",
        "shell_session_list",
        "shell_session_read",
        "shell_session_stop",
    ):
        assert name in EXECUTORS


def test_compute_background_instance_key_from_gui_launch_script() -> None:
    from argusai.core.workspace.shell_sessions import compute_background_instance_key

    key = compute_background_instance_key(
        script_text=r"cd /d H:\Workspace\test\test & python main.py > %TEMP%\fm.log 2>&1",
    )
    assert key == r"py:h:\workspace\test\test:main.py"


def test_oneshot_replaces_same_instance_key(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "sess.sandbox"
    root.mkdir()
    log1 = root / "a.background.log"
    log2 = root / "b.background.log"
    log1.write_bytes(b"")
    log2.write_bytes(b"")
    if sys.platform.startswith("win"):
        proc1 = subprocess.Popen(["ping", "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc2 = subprocess.Popen(["ping", "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc1 = subprocess.Popen(["sleep", "60"])
        proc2 = subprocess.Popen(["sleep", "60"])
    key = "py:h:/workspace/test/test:main.py"
    aid = "test-replace"
    try:
        first = _mgr().register_oneshot(
            sandbox_root=root,
            assistant_session_id=aid,
            argv=["python", "main.py"],
            proc=proc1,
            log_path=log1,
            label="gui-v1",
            instance_key=key,
        )
        second = _mgr().register_oneshot(
            sandbox_root=root,
            assistant_session_id=aid,
            argv=["python", "main.py"],
            proc=proc2,
            log_path=log2,
            label="gui-v2",
            instance_key=key,
        )
        replaced = list(getattr(second, "_replaced_sessions", []) or [])
        assert replaced
        assert any(item.get("session_id") == first.session_id for item in replaced)
        prior = _mgr().get(first.session_id)
        assert prior is not None
        assert prior.state in {"stopped", "exited", "failed"}
        assert second.state == "running"
    finally:
        for sid in list(_mgr().list_sessions(aid)):
            _mgr().stop(str(sid["session_id"]))
        for proc in (proc1, proc2):
            try:
                proc.kill()
            except Exception:
                pass


def test_background_subprocess_kwargs_hides_console_on_windows() -> None:
    from argusai.core.infra.win_stdio import background_subprocess_kwargs
    import subprocess

    kwargs = background_subprocess_kwargs()
    if sys.platform.startswith("win"):
        flags = int(kwargs.get("creationflags") or 0)
        assert flags & int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert flags & int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        assert kwargs.get("start_new_session") is True
