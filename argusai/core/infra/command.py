"""Scoped command execution for the AI assistant.

The assistant never receives arbitrary shell access.  Commands are described as
structured argv lists, checked against a small policy, and executed without a
shell so pipes, redirects, and command chaining are not interpreted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import shutil
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from argusai.core.infra.win_stdio import devnull_stdin_fd, ensure_valid_stdio, quiet_subprocess_kwargs

log = logging.getLogger("argusai.command")


# Tokens that suggest the AI is trying to chain commands. Since we always
# invoke via ``asyncio.create_subprocess_exec`` (no shell), these characters
# in argv are NOT a command-injection vector. We only block the unambiguous
# shell-chain operators as a sanity tripwire and let payload-friendly chars
# (``;``, ``&``, ``>``, ``<``) through so cookies/headers/JSON bodies work.
BLOCKED_TOKENS = {"&&", "||", "`", "$("}
OUTPUT_LIMIT = 12000


@dataclass
class CommandRequest:
    command: str
    args: list[str] = field(default_factory=list)
    target: str = ""
    scope: list[str] = field(default_factory=list)
    risk: str = "medium"
    timeout: float = 30.0


def normalize_scope(scope: str | list[str] | None) -> list[str]:
    if not scope:
        return []
    if isinstance(scope, str):
        return [s.strip().lower() for s in scope.split(",") if s.strip()]
    return [str(s).strip().lower() for s in scope if str(s).strip()]


def host_from_target(target: str) -> str:
    raw = (target or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname or raw.split("/")[0].split(":")[0]
    return host.strip("[]").lower()


def in_scope(target: str, scope: list[str]) -> bool:
    host = host_from_target(target)
    if not host:
        return False
    if not scope:
        return True
    for item in scope:
        item = item.lower().lstrip("*.").strip()
        if not item:
            continue
        if host == item or host.endswith("." + item):
            return True
        try:
            if ipaddress.ip_address(host) == ipaddress.ip_address(item):
                return True
        except ValueError:
            pass
    return False


def discover_network_tools() -> dict[str, Any]:
    names = ["nmap", "mtr", "traceroute", "tracert", "ping", "nslookup", "dig", "curl", "whois"]
    found = {name: shutil.which(name) for name in names}
    return {
        "platform": platform.system().lower(),
        "tools": found,
        "preferred_trace": "mtr" if found.get("mtr") else ("traceroute" if found.get("traceroute") else "tracert"),
    }


class CommandRunner:
    """Execute a validated command without invoking a shell."""

    def validate(self, req: CommandRequest) -> dict[str, Any]:
        command_path = shutil.which(req.command)
        if not command_path:
            return {"ok": False, "error": f"tool not found: {req.command}"}
        if not in_scope(req.target, req.scope):
            return {
                "ok": False,
                "error": f"target out of authorized scope: {req.target}",
                "target": req.target,
                "scope": req.scope,
            }
        joined = [req.command, *req.args]
        if any(token in part for part in joined for token in BLOCKED_TOKENS):
            return {"ok": False, "error": "blocked shell metacharacter in command arguments"}
        return {"ok": True, "path": command_path}

    async def run(self, req: CommandRequest) -> dict[str, Any]:
        check = self.validate(req)
        started = time.time()
        if not check.get("ok"):
            return {
                "ok": False,
                "command": req.command,
                "args": req.args,
                "risk": req.risk,
                "target": req.target,
                "error": check.get("error"),
            }

        ensure_valid_stdio()
        stdin_fd = devnull_stdin_fd()
        try:
            proc = await asyncio.create_subprocess_exec(
                check["path"],
                *req.args,
                stdin=stdin_fd if stdin_fd is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **quiet_subprocess_kwargs(),
            )
        finally:
            if stdin_fd is not None:
                try:
                    import os

                    os.close(stdin_fd)
                except OSError:
                    pass
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            timed_out = True
        else:
            timed_out = False

        duration_ms = int((time.time() - started) * 1000)
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if timed_out:
            log.warning(
                "command.run_timeout command=%s target=%s duration_ms=%d stderr_head=%r",
                req.command,
                req.target,
                duration_ms,
                err[:160],
            )
        elif proc.returncode != 0:
            log.warning(
                "command.run_nonzero command=%s target=%s exit_code=%s duration_ms=%d stderr_head=%r",
                req.command,
                req.target,
                proc.returncode,
                duration_ms,
                err[:160],
            )
        else:
            log.info(
                "command.run_ok command=%s target=%s duration_ms=%d stdout_chars=%d stderr_chars=%d",
                req.command,
                req.target,
                duration_ms,
                len(out),
                len(err),
            )
        return {
            "ok": proc.returncode == 0 and not timed_out,
            "command": req.command,
            "args": req.args,
            "risk": req.risk,
            "target": req.target,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "stdout_excerpt": out[:OUTPUT_LIMIT],
            "stderr_excerpt": err[:OUTPUT_LIMIT],
            "truncated": len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT,
            "timed_out": timed_out,
        }
