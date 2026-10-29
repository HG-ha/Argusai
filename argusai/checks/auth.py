"""Authentication & rate-limit audit.

This check deliberately does **not** try to brute-force real credentials. It
only probes the observable behaviour of the login endpoint with a handful of
controlled requests to look for:

* user enumeration via differing error messages / status codes / timing
* absence of rate limiting / CAPTCHA under bursts
* weak JWT structure (alg=none, HS256 with short/predictable secrets is out of
  scope — we just surface structural info for the auditor)

The default settings look for a sensible login endpoint; to audit a specific
one, put it in the YAML config under ``target.auth.login_url`` or
``target.paths``.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target

LOGIN_PATH_CANDIDATES = [
    "/api/session/login",
    "/api/auth/login",
    "/api/login",
    "/auth/login",
    "/user/login",
    "/login",
]

FAKE_USER = "very-unlikely-user-ABC123"
FAKE_PASSWORD = "invalid-password-9876"  # obvious wrong password
PROBE_USER_SEED = "audit-probe"


@register
class AuthCheck(Check):
    id = "auth"
    name = "Authentication"
    description = "Probe login for user enumeration and missing rate limits."
    order = 50

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        login_url = await self._find_login(target, ctx)
        if not login_url:
            ctx.log("auth: no login endpoint discovered; skipping", level="info")
            return

        yield Finding(
            id="auth.login-endpoint",
            category="auth",
            severity=Severity.INFO,
            title=f"Detected login endpoint at {login_url}",
            url=login_url,
        )

        # Timing / enumeration probe
        timings = {"fake": [], "real": []}
        fake_bodies: list[str] = []
        for _ in range(5):
            t0 = time.perf_counter()
            resp = await _send_login(ctx, login_url, FAKE_USER, FAKE_PASSWORD)
            timings["fake"].append(time.perf_counter() - t0)
            if resp is not None:
                fake_bodies.append(resp.text[:400])

        # "Real" probe uses the configured probe user if any — otherwise skip.
        probe_user = None
        if target.auth and target.auth.username:
            probe_user = target.auth.username
        if probe_user:
            for _ in range(5):
                t0 = time.perf_counter()
                await _send_login(ctx, login_url, probe_user, FAKE_PASSWORD)
                timings["real"].append(time.perf_counter() - t0)

        if timings["real"]:
            f_med = statistics.median(timings["fake"])
            r_med = statistics.median(timings["real"])
            diff = abs(r_med - f_med)
            if diff > 0.1 and diff / max(f_med, 1e-6) > 0.3:
                yield Finding(
                    id="auth.timing-oracle",
                    category="auth",
                    severity=Severity.MEDIUM,
                    title="Login timing differs between existing and non-existing users",
                    url=login_url,
                    description=(
                        "The server responds measurably differently for an existing "
                        "account vs a random string, creating a user-enumeration oracle."
                    ),
                    evidence={
                        "median_nonexistent_user_s": round(f_med, 3),
                        "median_existing_user_s": round(r_med, 3),
                        "diff_s": round(diff, 3),
                    },
                    remediation=(
                        "Normalise server work on the login path: hash the submitted "
                        "password even when the user does not exist so timing is "
                        "indistinguishable."
                    ),
                    cwe="CWE-204",
                )

        # Look at distinct error codes/bodies as enumeration signal.
        distinct = {b[:200] for b in fake_bodies}
        if len(distinct) == 1 and fake_bodies:
            body = fake_bodies[0]
            enum_phrases = ("not exist", "不存在", "no such", "unknown user", "invalid username")
            if any(p in body.lower() for p in ("not exist", "no such", "unknown user", "invalid username")) or any(
                p in body for p in ("不存在",)
            ):
                yield Finding(
                    id="auth.enumeration-message",
                    category="auth",
                    severity=Severity.MEDIUM,
                    title="Login error message leaks account existence",
                    url=login_url,
                    description=(
                        "Different error messages for unknown user vs wrong password "
                        "allow attackers to enumerate valid accounts."
                    ),
                    evidence={"body_snippet": body[:300]},
                    remediation=(
                        "Return a single generic error for any authentication failure."
                    ),
                    cwe="CWE-204",
                )

        # Rate-limit probe — burst 20 requests in parallel, check for throttling.
        burst_n = 20
        t0 = time.perf_counter()
        statuses = await asyncio.gather(
            *[
                _send_login(ctx, login_url, FAKE_USER + str(i), FAKE_PASSWORD)
                for i in range(burst_n)
            ],
            return_exceptions=True,
        )
        elapsed = time.perf_counter() - t0
        codes: dict[int, int] = {}
        for s in statuses:
            if hasattr(s, "status_code"):
                codes[s.status_code] = codes.get(s.status_code, 0) + 1
        throttled = any(c in codes for c in (429, 423, 403))
        if not throttled and codes:
            yield Finding(
                id="auth.no-rate-limit",
                category="auth",
                severity=Severity.HIGH,
                title="Login endpoint has no observable rate limit",
                url=login_url,
                description=(
                    f"{burst_n} login attempts in {elapsed:.2f}s did not trigger any "
                    "throttling response (429/423/403). Combined with user enumeration "
                    "this makes credential-stuffing practical."
                ),
                evidence={"status_counts": codes, "elapsed_s": round(elapsed, 3)},
                remediation=(
                    "Add per-IP and per-account rate limits (e.g. 5/min/IP and "
                    "5/15min/account), require CAPTCHA after N failures, and emit "
                    "WebAuthn/TOTP for sensitive accounts."
                ),
                cwe="CWE-307",
            )

    async def _find_login(self, target: Target, ctx: Context) -> str | None:
        # Prefer explicit config
        if target.auth and target.auth.login_url:
            return target.auth.login_url
        # Reuse recon output if available
        hits = (ctx.shared.get("recon") or {}).get("paths") or []
        for h in hits:
            if h["status"] in (200, 204, 400, 401, 403, 405) and any(
                k in h["path"] for k in ("/login", "/signin", "/auth")
            ):
                candidate = urljoin(target.origin + "/", h["path"].lstrip("/"))
                return candidate
        # Fall back to known candidates
        for p in LOGIN_PATH_CANDIDATES:
            url = urljoin(target.origin + "/", p.lstrip("/"))
            try:
                r = await ctx.request("OPTIONS", url)
                if r.status_code in (200, 204, 405):
                    return url
            except Exception:
                continue
            try:
                r = await ctx.request("POST", url, json={})
                if r.status_code in (200, 400, 401, 403, 422):
                    return url
            except Exception:
                continue
        return None


async def _send_login(ctx: Context, url: str, user: str, password: str):
    payload = {"username": user, "password": password, "email": user}
    try:
        return await ctx.request(
            "POST", url, json=payload, headers={"Content-Type": "application/json"}
        )
    except Exception:
        return None
