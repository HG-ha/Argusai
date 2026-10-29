"""Interactive (stdin-pipe) and oneshot shell sessions for the assistant.

Mirrors Codex/Cursor session usage without a full PTY: a long-lived shell
reads commands from stdin, stdout/stderr go to a log file, and the agent
uses start/exec/read/stop instead of blocking on process exit.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from argusai.core.infra.win_stdio import background_subprocess_kwargs, ensure_valid_stdio

log = logging.getLogger("argusai.shell_sessions")

SHELL_SESSIONS_DIR = ".tmp/shell-sessions"
CMD_DONE_MARKER = "__ARGUSAI_CMD_DONE__"
EXIT_MARKER = "__ARGUSAI_EXIT__"
_MAX_READ_CHARS = 80_000
_DEFAULT_READ_CHARS = 12_000
_SETTLE_SECONDS = 1.5

Kind = Literal["interactive", "oneshot"]
State = Literal["starting", "running", "exited", "stopped", "failed"]


@dataclass
class ShellSession:
    session_id: str
    assistant_session_id: str
    kind: Kind
    shell: str
    argv: list[str]
    cwd: str
    pid: int
    log_path: str
    started_at: str
    state: State = "starting"
    returncode: int | None = None
    label: str = ""
    instance_key: str = ""
    bytes_written: int = 0
    error: str = ""
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    _stdin: Any = field(default=None, repr=False)
    _log_handle: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ShellSessionManager:
    """In-process registry for interactive shells and oneshot background jobs."""

    def __init__(self) -> None:
        self._sessions: dict[str, ShellSession] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> ShellSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def register(self, session: ShellSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def _owned_session(
        self,
        session_id: str,
        assistant_session_id: str | None,
    ) -> tuple[ShellSession | None, dict[str, Any] | None]:
        session = self.get(session_id)
        # Omitted ownership is retained only for trusted in-process callers.
        # Every assistant tool wrapper supplies an explicit owner.
        if assistant_session_id is None:
            if session is None:
                return None, {
                    "ok": False,
                    "error": f"session not found: {session_id}",
                    "error_code": "not_found",
                }
            return session, None
        owner = str(assistant_session_id or "").strip()
        if session is None or not owner or session.assistant_session_id != owner:
            return None, {
                "ok": False,
                "error": "shell session is not available to this assistant session",
                "error_code": "session_ownership",
            }
        return session, None

    def list_sessions(self, assistant_session_id: str = "") -> list[dict[str, Any]]:
        aid = str(assistant_session_id or "").strip()
        with self._lock:
            items = list(self._sessions.values())
        out: list[dict[str, Any]] = []
        for session in items:
            if aid and session.assistant_session_id != aid:
                continue
            self.refresh(session)
            out.append(_public_session(session))
        return out

    def refresh(self, session: ShellSession) -> None:
        proc = session.proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            if session.state in {"starting", "running"}:
                session.state = "running"
            return
        session.returncode = code
        if session.state not in {"stopped", "failed"}:
            session.state = "exited" if code == 0 else "failed"
        _close_session_handles(session)

    def start_interactive(
        self,
        *,
        sandbox_root: Path,
        assistant_session_id: str,
        shell: str = "",
        initial_command: str = "",
        label: str = "",
        env: dict[str, str] | None = None,
        dangerous_check: Any = None,
    ) -> dict[str, Any]:
        root = Path(sandbox_root)
        root.mkdir(parents=True, exist_ok=True)
        shell_name = _resolve_shell_name(shell)
        if initial_command and dangerous_check:
            reason = dangerous_check(initial_command)
            if reason:
                return {"ok": False, "error": reason, "error_code": "dangerous_shell"}

        session_id = f"shell-{uuid.uuid4().hex[:10]}"
        sess_dir = root / SHELL_SESSIONS_DIR
        sess_dir.mkdir(parents=True, exist_ok=True)
        log_path = sess_dir / f"{session_id}.log"
        # cmd has no clean stdin REPL — reuse the PowerShell driver under the hood.
        driver_shell = "powershell" if shell_name == "cmd" else shell_name
        driver_path = sess_dir / f"{session_id}.driver{_driver_ext(driver_shell)}"
        driver_path.write_text(_driver_script(driver_shell), encoding="utf-8")
        if driver_shell in {"powershell", "pwsh"}:
            # BOM helps Windows PowerShell 5.1 parse UTF-8 drivers.
            payload = driver_path.read_bytes()
            if not payload.startswith(b"\xef\xbb\xbf"):
                driver_path.write_bytes(b"\xef\xbb\xbf" + payload)

        argv = _interactive_argv(shell_name, driver_path)
        ensure_valid_stdio()
        log_fh = open(log_path, "wb")  # noqa: SIM115 — kept open for child lifetime
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                **background_subprocess_kwargs(),
            )
        except FileNotFoundError as exc:
            log_fh.close()
            return {
                "ok": False,
                "error": f"shell not found: {argv[0]} ({exc})",
                "error_code": "shell_unavailable",
                "shell": shell_name,
            }
        except OSError as exc:
            log_fh.close()
            return {"ok": False, "error": str(exc), "error_code": "spawn_failed", "shell": shell_name}

        session = ShellSession(
            session_id=session_id,
            assistant_session_id=str(assistant_session_id or "default"),
            kind="interactive",
            shell=shell_name,
            argv=argv,
            cwd=str(root),
            pid=int(proc.pid or 0),
            log_path=str(log_path),
            started_at=_now_iso(),
            state="running",
            label=str(label or "").strip(),
            proc=proc,
            _stdin=proc.stdin,
            _log_handle=log_fh,
        )
        self.register(session)
        _reap_later(session)

        # Brief settle to catch immediate driver/shell crashes.
        deadline = time.monotonic() + _SETTLE_SECONDS
        while time.monotonic() < deadline:
            self.refresh(session)
            if session.state != "running":
                break
            time.sleep(0.05)

        if session.state != "running":
            preview, _ = _read_log(log_path, 0, 4000)
            return {
                "ok": False,
                "error": "interactive shell exited during settle",
                "error_code": "shell_early_exit",
                "session_id": session_id,
                "state": session.state,
                "returncode": session.returncode,
                "log_excerpt": preview,
                "shell": shell_name,
            }

        result: dict[str, Any] = {
            "ok": True,
            "session_id": session_id,
            "kind": "interactive",
            "shell": shell_name,
            "pid": session.pid,
            "cwd": str(root),
            "log_path": _rel_to_sandbox(root, log_path),
            "state": session.state,
            "label": session.label,
            "hint": (
                "Interactive shell is running. Use shell_session_exec to send commands, "
                "shell_session_read to poll output, shell_session_stop to end. "
                "For GUI apps prefer Start-Process / background=true oneshot so the shell stays free."
            ),
        }
        if initial_command.strip():
            exec_result = self.exec_command(
                session_id,
                initial_command,
                assistant_session_id=session.assistant_session_id,
                dangerous_check=dangerous_check,
            )
            result["initial_command"] = initial_command
            result["initial_exec"] = {
                "ok": exec_result.get("ok"),
                "error": exec_result.get("error"),
                "bytes_written": exec_result.get("bytes_written"),
            }
            if not exec_result.get("ok"):
                result["ok"] = False
                result["error"] = exec_result.get("error") or "initial_command failed"
        return result

    def stop_running_oneshots_by_key(
        self,
        assistant_session_id: str,
        instance_key: str,
        *,
        except_session_id: str = "",
    ) -> list[dict[str, Any]]:
        """Stop prior oneshots that match ``instance_key`` (single-instance GUI)."""
        key = str(instance_key or "").strip().lower()
        if not key:
            return []
        aid = str(assistant_session_id or "").strip()
        except_id = str(except_session_id or "").strip()
        with self._lock:
            candidates = [
                s for s in self._sessions.values()
                if s.kind == "oneshot"
                and (not aid or s.assistant_session_id == aid)
                and str(s.instance_key or "").strip().lower() == key
                and s.session_id != except_id
            ]
        stopped: list[dict[str, Any]] = []
        for session in candidates:
            self.refresh(session)
            if session.state not in {"running", "starting"}:
                continue
            result = self.stop(session.session_id, assistant_session_id=session.assistant_session_id)
            stopped.append({
                "session_id": session.session_id,
                "pid": session.pid,
                "label": session.label,
                "instance_key": session.instance_key,
                "stop": result,
            })
        return stopped

    def register_oneshot(
        self,
        *,
        sandbox_root: Path,
        assistant_session_id: str,
        argv: list[str],
        proc: subprocess.Popen[bytes],
        log_path: Path,
        label: str = "",
        shell: str = "",
        instance_key: str = "",
        script_text: str = "",
        meta: dict[str, Any] | None = None,
    ) -> ShellSession:
        aid = str(assistant_session_id or "default")
        key = str(instance_key or "").strip() or compute_background_instance_key(
            argv=list(argv),
            meta=meta or {},
            script_text=script_text,
            label=label,
        )
        replaced: list[dict[str, Any]] = []
        if key:
            replaced = self.stop_running_oneshots_by_key(aid, key)
        session_id = f"oneshot-{uuid.uuid4().hex[:10]}"
        session = ShellSession(
            session_id=session_id,
            assistant_session_id=aid,
            kind="oneshot",
            shell=str(shell or ""),
            argv=list(argv),
            cwd=str(sandbox_root),
            pid=int(proc.pid or 0),
            log_path=str(log_path),
            started_at=_now_iso(),
            state="running",
            label=str(label or "").strip(),
            instance_key=key,
            proc=proc,
        )
        self.register(session)
        _reap_later(session)
        # Stash for the caller (session_sandbox) without changing the return type.
        session._replaced_sessions = replaced  # type: ignore[attr-defined]
        return session

    def exec_command(
        self,
        session_id: str,
        command: str,
        *,
        assistant_session_id: str | None = None,
        dangerous_check: Any = None,
    ) -> dict[str, Any]:
        _session, ownership_error = self._owned_session(session_id, assistant_session_id)
        if ownership_error is not None:
            return ownership_error
        text = str(command or "")
        if not text.strip():
            return {"ok": False, "error": "command is required", "error_code": "invalid_arguments"}
        if dangerous_check:
            reason = dangerous_check(text)
            if reason:
                return {"ok": False, "error": reason, "error_code": "dangerous_shell"}
        # One logical command per exec: reject embedded newlines to keep framing simple.
        if "\n" in text or "\r" in text:
            return {
                "ok": False,
                "error": "command must be a single line; use shell_session_write for raw multi-line input",
                "error_code": "multiline_command",
            }
        return self._write_stdin(
            session_id,
            (text.rstrip("\n\r") + "\n").encode("utf-8"),
            require_interactive=True,
            assistant_session_id=assistant_session_id,
        )

    def write_raw(
        self,
        session_id: str,
        data: str | bytes,
        *,
        assistant_session_id: str | None = None,
        dangerous_check: Any = None,
    ) -> dict[str, Any]:
        _session, ownership_error = self._owned_session(session_id, assistant_session_id)
        if ownership_error is not None:
            return ownership_error
        if isinstance(data, bytes):
            payload = data
            preview = data.decode("utf-8", errors="replace")
        else:
            preview = str(data or "")
            payload = preview.encode("utf-8")
        if not payload:
            return {"ok": False, "error": "data is required", "error_code": "invalid_arguments"}
        if dangerous_check:
            reason = dangerous_check(preview)
            if reason:
                return {"ok": False, "error": reason, "error_code": "dangerous_shell"}
        return self._write_stdin(
            session_id,
            payload,
            require_interactive=True,
            assistant_session_id=assistant_session_id,
        )

    def _write_stdin(
        self,
        session_id: str,
        payload: bytes,
        *,
        require_interactive: bool,
        assistant_session_id: str | None,
    ) -> dict[str, Any]:
        session, ownership_error = self._owned_session(session_id, assistant_session_id)
        if ownership_error is not None:
            return ownership_error
        assert session is not None
        self.refresh(session)
        if require_interactive and session.kind != "interactive":
            return {
                "ok": False,
                "error": "session is oneshot (not an interactive shell); start shell_session_start for exec/write",
                "error_code": "not_interactive",
                "kind": session.kind,
                "session_id": session_id,
            }
        if session.state != "running" or session.proc is None or session._stdin is None:
            return {
                "ok": False,
                "error": f"session is not writable (state={session.state})",
                "error_code": "not_running",
                "state": session.state,
                "session_id": session_id,
            }
        with session._lock:
            try:
                session._stdin.write(payload)
                session._stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self.refresh(session)
                return {
                    "ok": False,
                    "error": f"stdin write failed: {exc}",
                    "error_code": "stdin_closed",
                    "session_id": session_id,
                    "state": session.state,
                }
            session.bytes_written += len(payload)
        return {
            "ok": True,
            "session_id": session_id,
            "kind": session.kind,
            "bytes_written": len(payload),
            "total_bytes_written": session.bytes_written,
            "state": session.state,
            "hint": "Command sent. Use shell_session_read to poll new output (look for __ARGUSAI_CMD_DONE__).",
        }

    def read(
        self,
        session_id: str,
        *,
        assistant_session_id: str | None = None,
        offset: int = 0,
        max_chars: int = _DEFAULT_READ_CHARS,
    ) -> dict[str, Any]:
        session, ownership_error = self._owned_session(session_id, assistant_session_id)
        if ownership_error is not None:
            return ownership_error
        assert session is not None
        self.refresh(session)
        off = max(0, int(offset or 0))
        limit = max(1, min(int(max_chars or _DEFAULT_READ_CHARS), _MAX_READ_CHARS))
        text, next_offset = _read_log(Path(session.log_path), off, limit)
        return {
            "ok": True,
            "session_id": session_id,
            "kind": session.kind,
            "state": session.state,
            "pid": session.pid,
            "returncode": session.returncode,
            "offset": off,
            "next_offset": next_offset,
            "eof": next_offset >= _log_size(Path(session.log_path)),
            "text": text,
            "chars": len(text),
            "cmd_done_marker": CMD_DONE_MARKER,
            "log_path": session.log_path,
        }

    def stop(
        self,
        session_id: str,
        *,
        assistant_session_id: str | None = None,
    ) -> dict[str, Any]:
        session, ownership_error = self._owned_session(session_id, assistant_session_id)
        if ownership_error is not None:
            return ownership_error
        assert session is not None
        self.refresh(session)
        if session.state in {"exited", "stopped", "failed"} and (session.proc is None or session.proc.poll() is not None):
            return {
                "ok": True,
                "session_id": session_id,
                "state": session.state,
                "returncode": session.returncode,
                "already_stopped": True,
            }
        # Ask interactive shells to exit cleanly first.
        if session.kind == "interactive" and session._stdin is not None and session.state == "running":
            try:
                with session._lock:
                    session._stdin.write((EXIT_MARKER + "\n").encode("utf-8"))
                    session._stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if session.proc is None or session.proc.poll() is not None:
                    break
                time.sleep(0.05)

        _kill_process_tree(session)
        self.refresh(session)
        if session.state == "running":
            session.state = "stopped"
        elif session.returncode not in (0, None) and session.state == "exited":
            session.state = "stopped"
        else:
            session.state = "stopped"
        _close_session_handles(session)
        return {
            "ok": True,
            "session_id": session_id,
            "state": session.state,
            "returncode": session.returncode,
            "pid": session.pid,
        }


_MANAGER = ShellSessionManager()


def get_shell_session_manager() -> ShellSessionManager:
    return _MANAGER


def assistant_session_id_from_sandbox(root: Path) -> str:
    name = Path(root).name
    if name.endswith(".sandbox"):
        return name[: -len(".sandbox")] or "default"
    return name or "default"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _public_session(session: ShellSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "assistant_session_id": session.assistant_session_id,
        "kind": session.kind,
        "shell": session.shell,
        "pid": session.pid,
        "cwd": session.cwd,
        "state": session.state,
        "started_at": session.started_at,
        "returncode": session.returncode,
        "label": session.label,
        "instance_key": session.instance_key,
        "bytes_written": session.bytes_written,
        "log_path": session.log_path,
        "argv_preview": " ".join(session.argv[:6]),
    }


_PY_ENTRY_RE = re.compile(
    r"(?is)(?:^|[;&|\n])\s*(?:python(?:w)?|py(?:\s+-3)?)\s+"
    r"(?:-[^\s]+\s+)*"
    r"(?:\"([^\"]+?\.py)\"|'([^']+?\.py)'|(\S+?\.py))"
)
_CD_RE = re.compile(
    r"(?is)(?:^|[;&|\n])\s*cd\s+(?:/d\s+)?(?:\"([^\"]+)\"|'([^']+)'|(\S+))"
)


def compute_background_instance_key(
    *,
    argv: list[str] | None = None,
    meta: dict[str, Any] | None = None,
    script_text: str = "",
    label: str = "",
) -> str:
    """Stable key so relaunching the same GUI/app replaces the previous oneshot.

    Returns empty string when no reliable identity is found (no auto-replace).
    """
    meta = meta or {}
    text = str(script_text or "")
    if not text and argv:
        for part in reversed(list(argv)):
            low = str(part).lower()
            if low.endswith((".cmd", ".bat", ".ps1", ".sh", ".py")):
                try:
                    text = Path(part).read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    text = ""
                break

    py_match = _PY_ENTRY_RE.search(text) if text else None
    if py_match:
        py_path = (py_match.group(1) or py_match.group(2) or py_match.group(3) or "").strip()
        cd_match = _CD_RE.search(text)
        cwd_hint = ""
        if cd_match:
            cwd_hint = (cd_match.group(1) or cd_match.group(2) or cd_match.group(3) or "").strip()
        if py_path:
            return f"py:{cwd_hint.lower()}:{py_path.replace(chr(92), '/').lower()}"

    rel = str(meta.get("relative_path") or meta.get("path") or label or "").strip()
    if rel and ".tmp/" not in rel.replace("\\", "/") and "snippet-" not in rel:
        return f"path:{rel.replace(chr(92), '/').lower()}"

    # Direct python script argv (no shell wrapper).
    for part in list(argv or []):
        low = str(part).replace("\\", "/").lower()
        if low.endswith(".py") and "snippet-" not in low:
            return f"path:{low}"
    return ""


def _rel_to_sandbox(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_log(path: Path, offset: int, max_chars: int) -> tuple[str, int]:
    if not path.exists():
        return "", offset
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, offset))
            raw = fh.read(max_chars * 4)
            next_off = fh.tell()
    except OSError:
        return "", offset
    text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars]
        # Keep next_offset consistent with what we return (approx).
        next_off = offset + len(text.encode("utf-8", errors="replace"))
    return text, next_off


def _close_session_handles(session: ShellSession) -> None:
    if session._stdin is not None:
        try:
            session._stdin.close()
        except OSError:
            pass
        session._stdin = None
    if session._log_handle is not None:
        try:
            session._log_handle.close()
        except OSError:
            pass
        session._log_handle = None


def _reap_later(session: ShellSession) -> None:
    proc = session.proc
    if proc is None:
        return

    def _wait() -> None:
        try:
            proc.wait()
        except Exception:
            pass
        session.returncode = proc.returncode
        if session.state == "running":
            session.state = "exited" if proc.returncode == 0 else "failed"
        _close_session_handles(session)

    threading.Thread(target=_wait, name=f"argusai-shell-reap-{session.pid}", daemon=True).start()


def _kill_process_tree(session: ShellSession) -> None:
    proc = session.proc
    pid = session.pid
    if proc is None and not pid:
        return
    if platform.system().lower().startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                **background_subprocess_kwargs(),
            )
        except OSError:
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
        return
    # POSIX: kill process group when started with start_new_session.
    try:
        if pid:
            os.killpg(pid, 15)
            time.sleep(0.2)
            os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass


def _resolve_shell_name(shell: str) -> str:
    name = (shell or "").strip().lower()
    is_win = platform.system().lower().startswith("win")
    if not name:
        return "powershell" if is_win else "bash"
    if name in {"powershell", "windows-powershell"}:
        return "powershell"
    if name in {"pwsh", "powershell7"}:
        return "pwsh"
    if name in {"cmd", "command"}:
        return "cmd"
    if name in {"bash", "sh"}:
        return name
    return "powershell" if is_win else "bash"


def _driver_ext(shell_name: str) -> str:
    if shell_name in {"powershell", "pwsh"}:
        return ".ps1"
    if shell_name == "cmd":
        return ".cmd"
    return ".sh"


def _driver_script(shell_name: str) -> str:
    if shell_name in {"powershell", "pwsh"}:
        # Must use OpenStandardInput — [Console]::In ignores redirected pipes
        # and would hang waiting on the real console.
        return (
            "$ErrorActionPreference = 'Continue'\r\n"
            "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\r\n"
            "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
            "$reader = New-Object System.IO.StreamReader([Console]::OpenStandardInput())\r\n"
            "while ($true) {\r\n"
            "  $line = $reader.ReadLine()\r\n"
            "  if ($null -eq $line) { break }\r\n"
            f"  if ($line -eq '{EXIT_MARKER}') {{ break }}\r\n"
            "  try {\r\n"
            "    Invoke-Expression $line 2>&1 | ForEach-Object { $_ }\r\n"
            "  } catch {\r\n"
            "    $_ | Out-String | Write-Output\r\n"
            "  }\r\n"
            f"  Write-Output '{CMD_DONE_MARKER}'\r\n"
            "}\r\n"
        )
    if shell_name == "cmd":
        # cmd cannot easily loop on stdin; use powershell driver via cmd fallback path
        # by spawning powershell when cmd is requested — handled in argv.
        return "@echo off\r\necho cmd interactive driver unused\r\n"
    return (
        "#!/bin/sh\n"
        "set +e\n"
        "while IFS= read -r line || [ -n \"$line\" ]; do\n"
        f"  [ \"$line\" = \"{EXIT_MARKER}\" ] && break\n"
        "  eval \"$line\"\n"
        f"  printf '%s\\n' '{CMD_DONE_MARKER}'\n"
        "done\n"
    )


def _interactive_argv(shell_name: str, driver_path: Path) -> list[str]:
    if shell_name == "powershell":
        exe = shutil.which("powershell") or "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver_path)]
    if shell_name == "pwsh":
        exe = shutil.which("pwsh") or "pwsh"
        return [exe, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver_path)]
    if shell_name == "cmd":
        # Use PowerShell driver under the hood — cmd has no clean stdin REPL.
        exe = shutil.which("powershell") or "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver_path)]
    if shell_name == "sh":
        exe = shutil.which("sh") or "sh"
        return [exe, str(driver_path)]
    exe = shutil.which("bash") or "bash"
    return [exe, str(driver_path)]


__all__ = [
    "CMD_DONE_MARKER",
    "ShellSession",
    "ShellSessionManager",
    "assistant_session_id_from_sandbox",
    "get_shell_session_manager",
]
