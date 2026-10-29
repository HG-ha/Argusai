"""Controlled weak-credential auditing helpers.

This is not a brute-force engine. It only performs a small number of
explicitly authorized login checks with rate limits and scope validation.
"""

from __future__ import annotations

import asyncio
import base64
import ftplib
import socket
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from argusai.core.security.network_recon import host_in_scope, normalize_host, normalize_scope

SUPPORTED_SERVICES = {"http_basic", "ftp", "redis"}
DEFAULT_CANDIDATES: dict[str, list[dict[str, str]]] = {
    "http_basic": [
        {"username": "admin", "password": "admin"},
        {"username": "admin", "password": "password"},
        {"username": "test", "password": "test"},
    ],
    "ftp": [
        {"username": "anonymous", "password": "anonymous@"},
        {"username": "ftp", "password": "ftp"},
        {"username": "admin", "password": "admin"},
    ],
    "redis": [
        {"username": "", "password": ""},
        {"username": "", "password": "redis"},
        {"username": "", "password": "password"},
    ],
}


@dataclass
class CredentialAttempt:
    service: str
    username: str
    password_label: str
    ok: bool
    status: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def weak_credential_plan(service: str = "", profile: dict[str, Any] | None = None) -> dict[str, Any]:
    service_l = (service or "").strip().lower()
    profile = profile or {}
    candidates = []
    if profile:
        for port in profile.get("ports", []):
            fp = port.get("fingerprint") or {}
            svc = str(fp.get("service") or "").lower()
            port_no = int(port.get("port") or 0)
            if svc in {"http", "https"} or port_no in {80, 443, 8080, 8443}:
                candidates.append({"service": "http_basic", "port": port_no, "reason": "HTTP service may use Basic authentication"})
            if svc == "ftp" or port_no == 21:
                candidates.append({"service": "ftp", "port": port_no, "reason": "FTP supports password login and anonymous access checks"})
            if svc == "redis" or port_no == 6379:
                candidates.append({"service": "redis", "port": port_no, "reason": "Redis may be unauthenticated or use weak AUTH secret"})
            if svc in {"ssh", "rdp", "smb"} or port_no in {22, 3389, 445}:
                candidates.append({
                    "service": svc or {22: "ssh", 3389: "rdp", 445: "smb"}.get(port_no, "remote-access"),
                    "port": port_no,
                    "reason": "High lockout/abuse risk; do not auto-try credentials. Use external approved process.",
                    "supported": False,
                })
    return {
        "ok": True,
        "supported_services": sorted(SUPPORTED_SERVICES),
        "requested_service": service_l,
        "profile_candidates": candidates,
        "rules": [
            "Require explicit authorization and non-empty scope.",
            "Use at most 10 attempts per tool call.",
            "Use low rate and stop at first success unless the user explicitly asks to continue.",
            "Do not test SSH/RDP/SMB automatically because of lockout and abuse risk.",
        ],
    }


def _password_label(password: str) -> str:
    if not password:
        return "(empty)"
    return f"len={len(password)},prefix={password[:1]}***"


def _check_http_basic(url: str, username: str, password: str, timeout: float) -> tuple[bool, str, dict[str, Any]]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8", errors="ignore")).decode("ascii")
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "User-Agent": "ArgusAI-Credential-Audit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            return 200 <= status < 400, f"http:{status}", {"status": status, "url": url}
    except urllib.error.HTTPError as exc:
        return False, f"http:{exc.code}", {"status": exc.code, "url": url}
    except Exception as exc:
        return False, exc.__class__.__name__, {"error": str(exc)[:240], "url": url}


def _check_ftp(host: str, port: int, username: str, password: str, timeout: float) -> tuple[bool, str, dict[str, Any]]:
    try:
        with ftplib.FTP() as ftp:
            ftp.connect(host, int(port or 21), timeout=timeout)
            banner = ftp.getwelcome()
            ftp.login(username or "anonymous", password or "anonymous@")
            return True, "ftp:login-ok", {"banner": banner[:240], "port": port or 21}
    except ftplib.error_perm as exc:
        return False, "ftp:login-denied", {"error": str(exc)[:240], "port": port or 21}
    except Exception as exc:
        return False, exc.__class__.__name__, {"error": str(exc)[:240], "port": port or 21}


def _check_redis(host: str, port: int, username: str, password: str, timeout: float) -> tuple[bool, str, dict[str, Any]]:
    try:
        with socket.create_connection((host, int(port or 6379)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if password:
                if username:
                    cmd = f"*3\r\n$4\r\nAUTH\r\n${len(username)}\r\n{username}\r\n${len(password)}\r\n{password}\r\n".encode()
                else:
                    cmd = f"*2\r\n$4\r\nAUTH\r\n${len(password)}\r\n{password}\r\n".encode()
                sock.sendall(cmd)
                auth = sock.recv(256).decode("utf-8", errors="replace")
                if not auth.startswith("+OK"):
                    return False, "redis:auth-denied", {"reply": auth[:120], "port": port or 6379}
            sock.sendall(b"*1\r\n$4\r\nPING\r\n")
            pong = sock.recv(256).decode("utf-8", errors="replace")
            return pong.startswith("+PONG"), "redis:pong" if pong.startswith("+PONG") else "redis:no-pong", {"reply": pong[:120], "port": port or 6379}
    except Exception as exc:
        return False, exc.__class__.__name__, {"error": str(exc)[:240], "port": port or 6379}


async def weak_credential_check(
    *,
    service: str,
    target: str,
    scope: str | list[str] | None,
    credentials: list[dict[str, str]] | None = None,
    port: int = 0,
    url: str = "",
    use_default_candidates: bool = False,
    max_attempts: int = 6,
    delay_seconds: float = 2.0,
    timeout_seconds: float = 5.0,
    stop_on_success: bool = True,
    authorization_phrase: str = "",
) -> dict[str, Any]:
    service_l = (service or "").strip().lower()
    if service_l not in SUPPORTED_SERVICES:
        return {"ok": False, "error": f"unsupported service: {service}", "supported_services": sorted(SUPPORTED_SERVICES)}
    host = normalize_host(target or url)
    if not host_in_scope(host, scope):
        return {"ok": False, "error": "target out of authorized scope", "target": host, "scope": normalize_scope(scope)}
    if authorization_phrase.strip() != "AUTHORIZED":
        return {"ok": False, "error": "weak credential audit requires authorization_phrase=AUTHORIZED"}
    attempts = list(credentials or [])
    if use_default_candidates:
        attempts.extend(DEFAULT_CANDIDATES.get(service_l, []))
    max_n = max(1, min(int(max_attempts or 6), 10))
    attempts = attempts[:max_n]
    if not attempts:
        return {"ok": False, "error": "credentials are required unless use_default_candidates=true"}
    delay = max(1.0, float(delay_seconds or 2.0))
    timeout = max(1.0, min(float(timeout_seconds or 5.0), 30.0))
    results: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    started = time.time()
    for idx, cred in enumerate(attempts):
        username = str(cred.get("username") or "")
        password = str(cred.get("password") or "")
        if idx:
            await asyncio.sleep(delay)
        if service_l == "http_basic":
            check_url = url or (target if str(target).startswith(("http://", "https://")) else f"http://{host}:{port or 80}/")
            ok, status, evidence = await asyncio.to_thread(_check_http_basic, check_url, username, password, timeout)
        elif service_l == "ftp":
            ok, status, evidence = await asyncio.to_thread(_check_ftp, host, int(port or 21), username, password, timeout)
        else:
            ok, status, evidence = await asyncio.to_thread(_check_redis, host, int(port or 6379), username, password, timeout)
        item = CredentialAttempt(
            service=service_l,
            username=username,
            password_label=_password_label(password),
            ok=ok,
            status=status,
            evidence=evidence,
        ).to_dict()
        results.append(item)
        if ok:
            successes.append(item)
            if stop_on_success:
                break
    return {
        "ok": True,
        "target": host,
        "service": service_l,
        "attempt_count": len(results),
        "success_count": len(successes),
        "successes": successes,
        "attempts": results,
        "duration_ms": int((time.time() - started) * 1000),
        "rate_limit": {"delay_seconds": delay, "max_attempts": max_n, "stop_on_success": stop_on_success},
        "warning": "This is a controlled weak-credential audit, not a brute-force run. Stop if lockout or monitoring risk is unacceptable.",
    }
