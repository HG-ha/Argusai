"""Managed hosted JavaScript runtime for reverse-engineering workflows.

This module keeps a Node.js process warm and talks to it over a JSONL
protocol. It is intentionally separate from ``js_python.run_js``: that module
is one-shot execution, while this one preserves JS contexts, hooks, logs and
oracle records across many tool calls.
"""

from __future__ import annotations

import json
import contextlib
import logging
import subprocess
import threading
import time
import hashlib
from pathlib import Path
from typing import Any

from argusai.core.reverse import runtime_manager

log = logging.getLogger("argusai.js_runtime")

REQUEST_TIMEOUT_SECONDS = 180
SOURCE_LIMIT = 20_000_000
PREVIEW_LIMIT = 200_000


def _runner_path() -> Path:
    return runtime_manager._vendor_dir() / "runners" / "hosted_js_runtime.mjs"  # noqa: SLF001 - same package runtime helper.


class HostedJSRuntime:
    """A single warm Node process hosting multiple JS sessions."""

    def __init__(
        self,
        *,
        api: Any | None = None,
        name: str = "default",
        sandbox_root: Path | None = None,
    ) -> None:
        self.api = api
        self.name = name
        self.sandbox_root = sandbox_root
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._started_at = 0.0
        self._last_op: dict[str, Any] = {}
        self._last_session_summary: list[dict[str, Any]] = []

    def start(self) -> dict[str, Any]:
        """Start the backing Node process if needed."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return {"ok": True, "running": True, "reused": True, **self.status()}

            runner = _runner_path()
            if not runner.exists():
                return {"ok": False, "error": f"hosted JS runner missing at {runner}"}
            try:
                node = runtime_manager.node_bin(self.api)
            except Exception as e:
                return {"ok": False, "error": str(e)}

            try:
                cwd = self.sandbox_root or _runtime_sandbox_root(self.api, self.name)
                self._proc = subprocess.Popen(
                    [node, str(runner)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(cwd),
                    env=runtime_manager.node_process_env(cwd),
                    **runtime_manager.quiet_subprocess_kwargs(),
                )
                self._started_at = time.time()
                ping = self.request({"op": "ping"}, timeout_seconds=10)
                if not ping.get("ok"):
                    self.close()
                    return ping
                return {"ok": True, "running": True, "reused": False, **self.status()}
            except Exception as e:
                self._proc = None
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def status(self) -> dict[str, Any]:
        proc = self._proc
        running = proc is not None and proc.poll() is None
        return {
            "name": self.name,
            "running": running,
            "pid": proc.pid if proc is not None else None,
            "age_ms": int((time.time() - self._started_at) * 1000) if running else 0,
            "last_op": self._last_op,
            "last_session_summary": self._last_session_summary,
        }

    def close(self) -> dict[str, Any]:
        """Terminate the process."""
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return {"ok": True, "closed": False}
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
            # A Node process that survived the kill is still holding its port
            # and its temp files; reporting it closed sends the caller on to a
            # restart that will collide with it.
            return {"ok": True, "closed": proc.poll() is not None}

    def request(self, payload: dict[str, Any], *, timeout_seconds: int = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Send one JSON request and wait for one JSON response."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                started = self.start()
                if not started.get("ok"):
                    return started
            assert self._proc is not None
            if self._proc.stdin is None or self._proc.stdout is None:
                return {"ok": False, "error": "runtime pipes unavailable"}

            self._next_id += 1
            req_id = self._next_id
            req = {"id": req_id, **payload}
            self._last_op = {
                "id": req_id,
                "op": payload.get("op"),
                "session_id": payload.get("session_id", ""),
                "ts": time.time(),
            }
            line = json.dumps(req, ensure_ascii=False, default=str) + "\n"
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except Exception as e:
                return {"ok": False, "error": f"write to runtime failed: {e}"}

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                try:
                    out = self._proc.stdout.readline()
                except Exception as e:
                    return {"ok": False, "error": f"read from runtime failed: {e}"}
                if not out:
                    if self._proc.poll() is not None:
                        return {
                            "ok": False,
                            "error": f"runtime exited with code {self._proc.returncode}",
                            "stderr": self._read_stderr_preview(),
                            "last_op": self._last_op,
                            "last_session_summary": self._last_session_summary,
                        }
                    time.sleep(0.01)
                    continue
                try:
                    res = json.loads(out)
                except json.JSONDecodeError:
                    return {"ok": False, "error": "runtime emitted invalid JSON", "raw": out[:1000]}
                if res.get("id") == req_id:
                    self._remember_summary(payload, res)
                    return res
            return {"ok": False, "error": f"timeout after {timeout_seconds}s", "last_op": self._last_op}

    def _remember_summary(self, payload: dict[str, Any], res: dict[str, Any]) -> None:
        if payload.get("op") == "status" and isinstance(res.get("sessions"), list):
            self._last_session_summary = list(res.get("sessions") or [])
            return
        sid = str(payload.get("session_id") or "")
        if not sid:
            return
        current = [s for s in self._last_session_summary if s.get("id") != sid]
        summary = {
            "id": sid,
            "last_op": payload.get("op"),
            "ok": bool(res.get("ok")),
            "source": res.get("source"),
            "oracle_count": res.get("remaining"),
            "updated_at": time.time(),
        }
        if payload.get("op") == "close":
            self._last_session_summary = current
        else:
            current.append(summary)
            self._last_session_summary = current[-50:]

    def _read_stderr_preview(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            return proc.stderr.read(4000)
        except Exception:
            return ""


_RUNTIMES: dict[str, HostedJSRuntime] = {}
_RUNTIMES_LOCK = threading.RLock()


def _runtime_sandbox_root(api: Any | None, name: str) -> Path:
    from argusai.core.paths import PathManager

    try:
        data_root = api.settings_mgr.get().data_root if api is not None else None
    except Exception:
        data_root = None
    base = PathManager(data_root=data_root).sessions_dir
    digest = hashlib.sha256(str(name or "default").encode("utf-8")).hexdigest()[:16]
    root = base / f"js-runtime-{digest}.sandbox"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_runtime(api: Any | None = None, name: str = "default") -> HostedJSRuntime:
    key = str(name or "default").strip() or "default"
    with _RUNTIMES_LOCK:
        rt = _RUNTIMES.get(key)
        if rt is None:
            rt = HostedJSRuntime(api=api, name=key, sandbox_root=_runtime_sandbox_root(api, key))
            _RUNTIMES[key] = rt
        elif api is not None:
            rt.api = api
        return rt


def runtime_status(api: Any | None = None, name: str = "") -> dict[str, Any]:
    """Return process and hosted-session status."""
    if name:
        rt = get_runtime(api, name)
        base = rt.status()
        if not base.get("running"):
            return {"ok": True, "runtimes": [base]}
        details = rt.request({"op": "status"}, timeout_seconds=10)
        base["hosted"] = details.get("sessions", []) if details.get("ok") else []
        base["error"] = details.get("error", "")
        return {"ok": True, "runtimes": [base]}
    out = []
    with _RUNTIMES_LOCK:
        items = list(_RUNTIMES.items())
    for _, rt in items:
        base = rt.status()
        if base.get("running"):
            details = rt.request({"op": "status"}, timeout_seconds=10)
            base["hosted"] = details.get("sessions", []) if details.get("ok") else []
            base["error"] = details.get("error", "")
        out.append(base)
    return {"ok": True, "runtimes": out}


def runtime_stop(name: str = "default") -> dict[str, Any]:
    """Stop a warm Node runtime process."""
    with _RUNTIMES_LOCK:
        rt = _RUNTIMES.pop(str(name or "default").strip() or "default", None)
    if rt is None:
        return {"ok": True, "closed": False}
    return rt.close()


def runtime_stop_all() -> dict[str, Any]:
    """Stop every warm hosted Node runtime process."""
    with _RUNTIMES_LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    closed = 0
    for rt in runtimes:
        try:
            res = rt.close()
            if res.get("closed"):
                closed += 1
        except Exception:
            pass
    return {"ok": True, "closed": closed}


def create_session(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    env: str = "browser",
    page_url: str = "",
    user_agent: str = "",
    globals_: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    started = rt.start()
    if not started.get("ok"):
        return started
    return rt.request({
        "op": "create",
        "session_id": str(session_id or "default"),
        "env": str(env or "browser").lower(),
        "page_url": str(page_url or ""),
        "user_agent": str(user_agent or ""),
        "globals": globals_ or {},
        "profile": profile or {},
    }, timeout_seconds=10)


def close_session(*, api: Any | None = None, runtime_name: str = "default", session_id: str = "default") -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({"op": "close", "session_id": str(session_id or "default")}, timeout_seconds=10)


def _read_source(source: str = "", path: str = "") -> tuple[str, str]:
    if source:
        text = str(source)
        if len(text) > SOURCE_LIMIT:
            raise ValueError(f"source too large ({len(text)} chars > {SOURCE_LIMIT})")
        return text, "<inline>"
    if not path:
        raise ValueError("source or path is required")
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"file not found: {p}")
    if p.stat().st_size > SOURCE_LIMIT * 4:
        raise ValueError(f"file too large: {p.stat().st_size} bytes")
    return p.read_text(encoding="utf-8", errors="replace"), str(p)


def load_code(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    source: str = "",
    path: str = "",
    artifact_path: str = "",
    filename: str = "",
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    try:
        text, origin = _read_source(source, path or artifact_path)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    rt = get_runtime(api, runtime_name)
    res = rt.request({
        "op": "load",
        "session_id": str(session_id or "default"),
        "source": text,
        "filename": filename or Path(origin).name or "bundle.js",
        "timeout_ms": int(timeout_ms or 30_000),
    }, timeout_seconds=max(5, min(REQUEST_TIMEOUT_SECONDS, int(timeout_ms / 1000) + 10)))
    if res.get("ok"):
        res["origin"] = origin
        res["source_chars"] = len(text)
    return res


def list_sources(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({"op": "sources", "session_id": str(session_id or "default")}, timeout_seconds=10)


def list_patches(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({"op": "patches", "session_id": str(session_id or "default")}, timeout_seconds=10)


def list_hooks(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({"op": "hooks", "session_id": str(session_id or "default")}, timeout_seconds=10)


def patch_source(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    source_id: str = "",
    mode: str = "string",
    search: str = "",
    pattern: str = "",
    flags: str = "g",
    start: int = 0,
    end: int = 0,
    replacement: str = "",
    max_replacements: int = 1,
    reevaluate: bool = True,
    timeout_ms: int = 30_000,
    note: str = "",
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "patch",
        "session_id": str(session_id or "default"),
        "source_id": str(source_id or ""),
        "mode": str(mode or "string"),
        "search": str(search or ""),
        "pattern": str(pattern or ""),
        "flags": str(flags or "g"),
        "start": int(start or 0),
        "end": int(end or 0),
        "replacement": str(replacement or ""),
        "max_replacements": int(max_replacements or 1),
        "reevaluate": bool(reevaluate),
        "timeout_ms": int(timeout_ms or 30_000),
        "note": str(note or ""),
    }, timeout_seconds=max(5, min(REQUEST_TIMEOUT_SECONDS, int(timeout_ms / 1000) + 10)))


def instrument_functions(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    targets: list[str] | None = None,
    target: str = "",
    hook_id: str = "",
    filter_src: str = "",
    tag: str = "",
    capture_stack: bool = True,
) -> dict[str, Any]:
    chosen = [str(t) for t in (targets or []) if str(t or "").strip()]
    if target:
        chosen.append(str(target))
    if not chosen:
        return {"ok": False, "error": "target or targets is required"}
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "instrument",
        "session_id": str(session_id or "default"),
        "targets": chosen,
        "hook_id": str(hook_id or ""),
        "filter_src": str(filter_src or ""),
        "tag": str(tag or ""),
        "capture_stack": bool(capture_stack),
    }, timeout_seconds=10)


def snapshot(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({"op": "snapshot", "session_id": str(session_id or "default")}, timeout_seconds=10)


def eval_expression(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    expression: str = "",
    inputs: Any = None,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    if not str(expression or "").strip():
        return {"ok": False, "error": "expression is required"}
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "eval",
        "session_id": str(session_id or "default"),
        "expression": str(expression),
        "inputs": inputs,
        "timeout_ms": int(timeout_ms or 5000),
    }, timeout_seconds=max(5, min(REQUEST_TIMEOUT_SECONDS, int(timeout_ms / 1000) + 10)))


def call_function(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    target: str = "",
    args: list[Any] | None = None,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    if not str(target or "").strip():
        return {"ok": False, "error": "target is required"}
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "call",
        "session_id": str(session_id or "default"),
        "target": str(target),
        "args": list(args or []),
        "timeout_ms": int(timeout_ms or 5000),
    }, timeout_seconds=max(5, min(REQUEST_TIMEOUT_SECONDS, int(timeout_ms / 1000) + 10)))


def hook_function(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    target: str = "",
    hook_id: str = "",
    filter_src: str = "",
    capture_stack: bool = True,
) -> dict[str, Any]:
    if not str(target or "").strip():
        return {"ok": False, "error": "target is required"}
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "hook",
        "session_id": str(session_id or "default"),
        "target": str(target),
        "hook_id": str(hook_id or ""),
        "filter_src": str(filter_src or ""),
        "capture_stack": bool(capture_stack),
    }, timeout_seconds=10)


def drain_oracles(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    limit: int = 200,
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "drain",
        "session_id": str(session_id or "default"),
        "limit": int(limit or 200),
    }, timeout_seconds=10)


def read_logs(
    *,
    api: Any | None = None,
    runtime_name: str = "default",
    session_id: str = "default",
    limit: int = 100,
) -> dict[str, Any]:
    rt = get_runtime(api, runtime_name)
    return rt.request({
        "op": "logs",
        "session_id": str(session_id or "default"),
        "limit": int(limit or 100),
    }, timeout_seconds=10)
