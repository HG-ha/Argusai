"""Bundled/managed sqlmap integration.

ArgusAI prefers the bundled sqlmap copy shipped under ``argusai/vendor`` and
falls back to a managed user-data checkout only when the bundle is absent. We
intentionally do not import sqlmap internals: sqlmap is designed as a CLI/API
tool and its repository ships `sqlmap.py` plus `sqlmapapi.py`, not a stable
embeddable SDK.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from argusai.core.infra.command import in_scope, normalize_scope
from argusai.core.security.evidence import evidence_record
from argusai.core.paths import PathManager
from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

log = logging.getLogger("argusai.sqlmap")


SQLMAP_REPO_URL = "https://github.com/sqlmapproject/sqlmap.git"
DEFAULT_TIMEOUT_SECONDS = 600
OUTPUT_LIMIT = 20000

DANGEROUS_OPTIONS = {
    "--dump", "--dump-all", "--dump-format", "--sql-query", "--sql-shell",
    "--os-cmd", "--os-shell", "--os-pwn", "--os-smbrelay", "--os-bof",
    "--file-read", "--file-write", "--file-dest", "--priv-esc",
    "--reg-read", "--reg-add", "--reg-del", "--passwords",
}

ENUMERATION_OPTIONS = {
    "--dbs", "--tables", "--columns", "--schema", "--search", "--users",
    "--roles", "--privileges", "--current-user", "--current-db", "--hostname",
}

BLOCKED_TOKENS = {"|", ">", "<", ">>", "&&", "||", ";", "&", "`", "$("}


def managed_sqlmap_dir(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir else PathManager().tools_dir
    return root / "sqlmap"


def sqlmap_output_dir(base_dir: str | Path | None = None) -> Path:
    root = Path(base_dir) if base_dir else PathManager().tools_dir
    return root / "sqlmap-output"


def bundled_sqlmap_dir() -> Path:
    # argusai/core/security/sqlmap_integration.py → parent.parent.parent = argusai/
    return Path(__file__).resolve().parent.parent.parent / "vendor" / "sqlmap"


def _python_command() -> list[str]:
    # Reuse the session-sandbox resolver so packaged macOS/Linux builds do
    # not try to execute sqlmap.py with ArgusAI.app itself as sys.executable.
    from argusai.core.workspace.session_sandbox import _base_python_command

    cmd = _base_python_command("")
    if cmd:
        return cmd
    if sys.executable:
        return [sys.executable]
    return ["python3" if not platform.system().lower().startswith("win") else "python"]


def _sqlmap_source(base_dir: str | Path | None = None) -> dict[str, Any]:
    bundled = bundled_sqlmap_dir()
    bundled_script = bundled / "sqlmap.py"
    if bundled_script.exists():
        return {
            "source": "bundled",
            "installed": True,
            "directory": str(bundled),
            "script": str(bundled_script),
        }
    managed = managed_sqlmap_dir(base_dir)
    managed_script = managed / "sqlmap.py"
    return {
        "source": "managed",
        "installed": managed_script.exists(),
        "directory": str(managed),
        "script": str(managed_script),
    }


def sqlmap_status(base_dir: str | Path | None = None) -> dict[str, Any]:
    source = _sqlmap_source(base_dir)
    bundled = bundled_sqlmap_dir()
    managed = managed_sqlmap_dir(base_dir)
    return {
        "ok": True,
        "managed": True,
        "bundled": (bundled / "sqlmap.py").exists(),
        "installed": source["installed"],
        "source": source["source"],
        "repo_url": SQLMAP_REPO_URL,
        "directory": source["directory"],
        "script": source["script"],
        "bundled_directory": str(bundled),
        "managed_directory": str(managed),
        "git_available": bool(shutil.which("git")),
        "python": " ".join(_python_command()),
    }


async def install_or_update_sqlmap(base_dir: str | Path | None = None, *, update: bool = True) -> dict[str, Any]:
    if (bundled_sqlmap_dir() / "sqlmap.py").exists():
        return {
            "ok": True,
            "updated": False,
            "message": "Bundled sqlmap is already available; no clone is needed.",
            **sqlmap_status(base_dir),
        }

    root = managed_sqlmap_dir(base_dir)
    git = shutil.which("git")
    if not git:
        return {"ok": False, "error": "git not found; cannot clone managed sqlmap", **sqlmap_status(base_dir)}

    root.parent.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        argv = [git, "clone", "--depth", "1", SQLMAP_REPO_URL, str(root)]
    elif update:
        argv = [git, "-C", str(root), "pull", "--ff-only"]
    else:
        return {"ok": True, "updated": False, **sqlmap_status(base_dir)}

    started = time.time()
    log.info("sqlmap.install_or_update starting argv=%s", argv[:3])
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **quiet_subprocess_kwargs(),
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    duration_ms = int((time.time() - started) * 1000)
    if proc.returncode == 0:
        log.info("sqlmap.install_or_update_ok duration_ms=%d", duration_ms)
    else:
        log.warning(
            "sqlmap.install_or_update_failed exit_code=%s duration_ms=%d stderr_head=%r",
            proc.returncode,
            duration_ms,
            err[:160],
        )
    return {
        "ok": proc.returncode == 0 and (root / "sqlmap.py").exists(),
        "updated": proc.returncode == 0,
        "exit_code": proc.returncode,
        "duration_ms": duration_ms,
        "stdout_excerpt": out[:OUTPUT_LIMIT],
        "stderr_excerpt": err[:OUTPUT_LIMIT],
        **sqlmap_status(base_dir),
    }


def _split_extra_args(extra_args: list[str] | str | None) -> list[str]:
    if extra_args is None:
        return []
    if isinstance(extra_args, list):
        return [str(x) for x in extra_args if str(x).strip()]
    return shlex.split(str(extra_args), posix=not platform.system().lower().startswith("win"))


def _option_name(arg: str) -> str:
    return arg.split("=", 1)[0].strip()


def validate_sqlmap_args(args: list[str], *, allow_enumeration: bool = False) -> dict[str, Any]:
    blocked = []
    joined = " ".join(args)
    if any(token in part for part in args for token in BLOCKED_TOKENS):
        blocked.append("shell metacharacter")
    for arg in args:
        name = _option_name(arg)
        if name in DANGEROUS_OPTIONS:
            blocked.append(name)
        if not allow_enumeration and name in ENUMERATION_OPTIONS:
            blocked.append(name)
    for opt in ("--risk", "--level"):
        for idx, arg in enumerate(args):
            value = ""
            if arg.startswith(opt + "="):
                value = arg.split("=", 1)[1]
            elif arg == opt and idx + 1 < len(args):
                value = args[idx + 1]
            if value:
                try:
                    n = int(value)
                except ValueError:
                    blocked.append(f"{opt}=invalid")
                    continue
                if opt == "--risk" and n > 2:
                    blocked.append("--risk>2")
                if opt == "--level" and n > 3:
                    blocked.append("--level>3")
    return {"ok": not blocked, "blocked": blocked, "display": joined}


def sqlmap_plan_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    parameter: str = "",
    method: str = "GET",
    data: str = "",
    cookie: str = "",
    headers: str = "",
    extra_args: list[str] | str | None = None,
    allow_enumeration: bool = False,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}

    status = sqlmap_status(base_dir)
    out_dir = sqlmap_output_dir(base_dir)
    argv = [
        *_python_command(),
        status["script"],
        "-u",
        url,
        "--batch",
        "--smart",
        "--risk=1",
        "--level=1",
        "--flush-session",
        "--parse-errors",
        "--output-dir",
        str(out_dir),
    ]
    if parameter:
        argv += ["-p", parameter]
    if method and method.upper() != "GET":
        argv += [f"--method={method.upper()}"]
    if data:
        argv += ["--data", data]
    if cookie:
        argv += ["--cookie", cookie]
    if headers:
        argv += ["--headers", headers]
    argv += _split_extra_args(extra_args)

    check = validate_sqlmap_args(argv[2:], allow_enumeration=allow_enumeration)
    return {
        "ok": bool(status.get("installed")) and check["ok"],
        "installed": status.get("installed"),
        "install_hint": "Run sqlmap_install_update first." if not status.get("installed") else "",
        "command": argv[0],
        "args": argv[1:],
        "cwd": status["directory"],
        "output_dir": str(out_dir),
        "target": url,
        "scope": normalize_scope(scope),
        "risk": "high",
        "blocked": check["blocked"],
        "display": " ".join(argv),
        "notes": [
            f"{status.get('source', 'managed')} sqlmap copy is used; system PATH sqlmap is ignored.",
            "Dangerous dump/os-shell/file-read options are blocked.",
            "Enumeration options are blocked unless allow_enumeration=true.",
        ],
    }


def parse_sqlmap_output(output: str) -> dict[str, Any]:
    text = output or ""
    lower = text.lower()
    vulnerable = "sqlmap identified the following injection point" in lower or "is vulnerable" in lower
    not_injectable = "all tested parameters do not appear to be injectable" in lower
    dbms = ""
    m = re.search(r"back-end DBMS:\s*(.+)", text, re.I)
    if m:
        dbms = m.group(1).strip()
    payloads = re.findall(r"Payload:\s*(.+)", text, re.I)
    parameters = re.findall(r"Parameter:\s*(.+)", text, re.I)
    return {
        "vulnerable": bool(vulnerable and not not_injectable),
        "not_injectable": bool(not_injectable),
        "dbms": dbms,
        "parameters": parameters[:20],
        "payloads": payloads[:20],
    }


async def sqlmap_run_scan(
    url: str,
    *,
    scope: str | list[str] | None = None,
    parameter: str = "",
    method: str = "GET",
    data: str = "",
    cookie: str = "",
    headers: str = "",
    extra_args: list[str] | str | None = None,
    allow_enumeration: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    plan = sqlmap_plan_scan(
        url,
        scope=scope,
        parameter=parameter,
        method=method,
        data=data,
        cookie=cookie,
        headers=headers,
        extra_args=extra_args,
        allow_enumeration=allow_enumeration,
        base_dir=base_dir,
    )
    if not plan.get("ok"):
        return plan

    output_dir = Path(plan.get("output_dir") or "")
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    log.info(
        "sqlmap.run_scan_start url=%s parameter=%s extra_args=%s",
        url,
        parameter or "-",
        " ".join(plan.get("args", [])[:4]),
    )
    proc = await asyncio.create_subprocess_exec(
        plan["command"],
        *plan["args"],
        cwd=plan.get("cwd") or None,
        env={**os.environ, "PYTHONUTF8": "1"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **quiet_subprocess_kwargs(),
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max(10, min(int(timeout_seconds), 1800)))
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    duration_ms = int((time.time() - started) * 1000)
    if timed_out:
        log.warning("sqlmap.run_scan_timeout url=%s duration_ms=%d", url, duration_ms)
    elif proc.returncode != 0:
        log.warning(
            "sqlmap.run_scan_nonzero url=%s exit_code=%s duration_ms=%d stderr_head=%r",
            url,
            proc.returncode,
            duration_ms,
            err[:160],
        )
    else:
        log.info(
            "sqlmap.run_scan_ok url=%s duration_ms=%d stdout_chars=%d",
            url,
            duration_ms,
            len(out),
        )
    combined = out + ("\n" + err if err else "")
    parsed = parse_sqlmap_output(combined)
    ev = evidence_record(
        kind="sqlmap",
        surface=f"parameter:{parameter}" if parameter else "request",
        url=url,
        payload=", ".join(parsed.get("payloads") or [])[:1000],
        response_excerpt=combined[:2000],
        confidence="confirmed" if parsed.get("vulnerable") else "negative",
        test_case=plan.get("display", ""),
        notes=f"DBMS: {parsed.get('dbms') or 'unknown'}",
    )
    return {
        "ok": proc.returncode == 0 and not timed_out,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_ms": int((time.time() - started) * 1000),
        "stdout_excerpt": out[:OUTPUT_LIMIT],
        "stderr_excerpt": err[:OUTPUT_LIMIT],
        "truncated": len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT,
        "plan": plan,
        "parsed": parsed,
        "evidence": ev,
    }

