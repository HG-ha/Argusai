"""Frida CLI bridge for Reverse Agent Runtime.

Uses the host ``frida`` executable discovered by ``toolchain.py`` — Frida is
**not** bundled in vendor/. Dynamic attach/spawn/script execution is treated as
high-risk (Loom22 §21.7); this module only implements the mechanics.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.reverse.toolchain import detect_tool

log = logging.getLogger("argusai.frida_runtime")

_MAX_LOG_BYTES = 2 * 1024 * 1024
_PREVIEW_CHARS = 8000


@dataclass
class FridaSession:
    session_id: str
    mode: str
    target: str
    device: str
    job_id: str
    proc: subprocess.Popen[str] | None = None
    script_path: str = ""
    started_at: str = ""
    log_path: str = ""
    state: str = "running"
    returncode: int | None = None
    error: str = ""
    _log_handle: Any = field(default=None, repr=False)


class FridaSessionManager:
    """Track foreground/background Frida CLI sessions in-process."""

    def __init__(self) -> None:
        self._sessions: dict[str, FridaSession] = {}
        self._lock = threading.Lock()

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "mode": s.mode,
                    "target": s.target,
                    "device": s.device,
                    "job_id": s.job_id,
                    "state": s.state,
                    "started_at": s.started_at,
                    "returncode": s.returncode,
                }
                for s in self._sessions.values()
            ]

    def get(self, session_id: str) -> FridaSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def stop(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": f"session not found: {session_id}"}
        return _stop_session(session)

    def register(self, session: FridaSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def clear_all(self) -> int:
        with self._lock:
            ids = list(self._sessions.keys())
        stopped = 0
        for sid in ids:
            if self.stop(sid).get("ok"):
                stopped += 1
        return stopped


_MANAGER = FridaSessionManager()


def get_frida_manager() -> FridaSessionManager:
    return _MANAGER


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _frida_path() -> tuple[str | None, dict[str, Any]]:
    info = detect_tool("frida")
    path = str(info.get("path") or "")
    if info.get("available") and path:
        return path, info
    return None, info


def _device_args(device: str) -> list[str]:
    d = str(device or "").strip()
    if not d or d.lower() in {"local", "default"}:
        return []
    if d.lower() in {"usb", "u"}:
        return ["-U"]
    if d.lower() in {"remote", "r"}:
        return ["-R"]
    return ["-D", d]


def _build_command(
    *,
    mode: str,
    target: str,
    device: str,
    script_file: Path | None,
    eval_script: str = "",
    extra_args: list[str] | None = None,
) -> list[str]:
    frida, _info = _frida_path()
    if not frida:
        raise RuntimeError("frida not found on PATH; install frida-tools")

    cmd: list[str] = [frida, *_device_args(device)]
    mode_norm = str(mode or "").strip().lower()
    target_text = str(target or "").strip()

    if mode_norm == "spawn":
        if not target_text:
            raise ValueError("package is required for spawn mode")
        cmd.extend(["-f", target_text, "--no-pause"])
    elif mode_norm == "attach_name":
        if not target_text:
            raise ValueError("process_name is required for attach_name mode")
        cmd.extend(["-n", target_text])
    elif mode_norm == "attach_pid":
        pid = int(target_text)
        if pid <= 0:
            raise ValueError("pid must be positive")
        cmd.extend(["-p", str(pid)])
    else:
        raise ValueError(f"unsupported frida mode: {mode}")

    if script_file is not None:
        cmd.extend(["-l", str(script_file)])
    elif eval_script:
        cmd.extend(["-e", eval_script])
    else:
        cmd.extend(["-e", "console.log('[argusai] attached');"])

    if extra_args:
        cmd.extend(list(extra_args))
    return cmd


def _stop_session(session: FridaSession) -> dict[str, Any]:
    proc = session.proc
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError as exc:
            session.error = str(exc)
    if session._log_handle:
        try:
            session._log_handle.close()
        except OSError:
            pass
    session.state = "stopped"
    session.returncode = proc.returncode if proc is not None else session.returncode
    return {"ok": True, "session_id": session.session_id, "state": session.state, "returncode": session.returncode}


def _read_log_file(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    data = path.read_bytes()
    truncated = len(data) > _MAX_LOG_BYTES
    if truncated:
        data = data[:_MAX_LOG_BYTES]
    text = data.decode("utf-8", errors="replace")
    preview = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "\n…"
    return preview, truncated


def run_frida(
    *,
    mode: str,
    target: str,
    device: str = "",
    script: str = "",
    script_path: Path | None = None,
    job_id: str = "",
    timeout_seconds: int = 30,
    background: bool = False,
    extra_args: list[str] | None = None,
    on_session_start: Callable[[FridaSession], None] | None = None,
) -> dict[str, Any]:
    """Run Frida CLI once (foreground) or leave it running (background)."""
    frida, toolchain = _frida_path()
    if not frida:
        return {
            "ok": False,
            "error": "frida not found on PATH",
            "suggested_fix": "pip install frida-tools",
            "toolchain": toolchain,
        }

    temp_script: Path | None = None
    script_source = script_path
    try:
        if script_source is None and script.strip():
            fd, name = tempfile.mkstemp(suffix=".js", prefix="argusai-frida-")
            os.close(fd)
            temp_script = Path(name)
            temp_script.write_text(script, encoding="utf-8")
            script_source = temp_script
        elif script_source is not None and not script_source.is_file():
            return {"ok": False, "error": f"script not found: {script_source}"}

        cmd = _build_command(
            mode=mode,
            target=target,
            device=device,
            script_file=script_source,
            extra_args=extra_args,
        )
    except (ValueError, RuntimeError) as exc:
        if temp_script:
            temp_script.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc), "toolchain": toolchain}

    session_id = f"frida-{uuid.uuid4().hex[:10]}"
    fd, log_name = tempfile.mkstemp(suffix=".log", prefix=f"{session_id}-")
    os.close(fd)
    log_file = Path(log_name)
    session = FridaSession(
        session_id=session_id,
        mode=mode,
        target=target,
        device=device or "local",
        job_id=job_id or "",
        script_path=str(script_source or ""),
        started_at=_now_iso(),
        log_path=str(log_file),
    )

    timeout = max(1, min(int(timeout_seconds or 30), 600))

    try:
        log_handle = log_file.open("w", encoding="utf-8")
        session._log_handle = log_handle
        if background:
            proc = subprocess.Popen(
                cmd,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                **quiet_subprocess_kwargs(),
            )
            session.proc = proc
            _MANAGER.register(session)
            if on_session_start:
                on_session_start(session)
            return {
                "ok": True,
                "background": True,
                "session_id": session_id,
                "command": cmd,
                "log_path": str(log_file),
                "toolchain": toolchain,
            }

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **quiet_subprocess_kwargs(),
        )
        log_handle.write(proc.stdout or "")
        if proc.stderr:
            log_handle.write("\n--- stderr ---\n")
            log_handle.write(proc.stderr)
        log_handle.close()
        session._log_handle = None
        session.state = "stopped" if proc.returncode == 0 else "failed"
        session.returncode = proc.returncode
        preview, truncated = _read_log_file(log_file)
        out: dict[str, Any] = {
            "ok": proc.returncode == 0,
            "background": False,
            "session_id": session_id,
            "command": cmd,
            "returncode": proc.returncode,
            "log_preview": preview,
            "log_truncated": truncated,
            "log_path": str(log_file),
            "toolchain": toolchain,
        }
        if proc.returncode != 0:
            out["error"] = (proc.stderr or proc.stdout or "frida exited with error")[:2000]
        _MANAGER.register(session)
        if on_session_start:
            on_session_start(session)
        return out
    except subprocess.TimeoutExpired:
        if session.proc and session.proc.poll() is None:
            _stop_session(session)
        preview, truncated = _read_log_file(log_file)
        return {
            "ok": False,
            "error": f"frida timed out after {timeout}s",
            "session_id": session_id,
            "command": cmd,
            "log_preview": preview,
            "log_truncated": truncated,
            "log_path": str(log_file),
            "toolchain": toolchain,
        }
    finally:
        if temp_script:
            temp_script.unlink(missing_ok=True)


def save_log_artifact(
    store: Any,
    job_id: str,
    log_path: str | Path,
    *,
    tool: str,
    session_id: str,
    note: str = "",
) -> dict[str, Any] | None:
    if not store or not job_id:
        return None
    path = Path(log_path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) > _MAX_LOG_BYTES:
        data = data[:_MAX_LOG_BYTES]
    try:
        rec = store.add_artifact(
            job_id,
            content=data,
            kind="frida_log",
            filename=f"{session_id}.log",
            tool=tool,
            note=note or f"frida session {session_id}",
        )
        from argusai.core.reverse.reverse_artifacts import artifact_ref

        rec["artifact_ref"] = artifact_ref(job_id, rec["id"])
        return rec
    except Exception as exc:
        log.warning("failed to save frida log artifact: %s", exc)
        return None
