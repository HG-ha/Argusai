"""Security-header audit."""

from __future__ import annotations

from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


REQUIRED_HEADERS = {
    "content-security-policy": {
        "id": "headers.missing-csp",
        "title": "Missing Content-Security-Policy",
        "severity": Severity.MEDIUM,
        "remediation": (
            "Define a CSP that at minimum restricts default-src, script-src and "
            "frame-ancestors. Start with Report-Only to measure impact."
        ),
        "cwe": "CWE-693",
    },
    "strict-transport-security": {
        "id": "headers.missing-hsts",
        "title": "Missing Strict-Transport-Security (HSTS)",
        "severity": Severity.MEDIUM,
        "remediation": (
            "Send `Strict-Transport-Security: max-age=31536000; includeSubDomains` "
            "on all HTTPS responses. Consider preload once verified."
        ),
        "cwe": "CWE-319",
    },
    "x-content-type-options": {
        "id": "headers.missing-nosniff",
        "title": "Missing X-Content-Type-Options",
        "severity": Severity.LOW,
        "remediation": "Send `X-Content-Type-Options: nosniff`.",
        "cwe": "CWE-430",
    },
    "x-frame-options": {
        "id": "headers.missing-xfo",
        "title": "Missing X-Frame-Options / frame-ancestors",
        "severity": Severity.LOW,
        "remediation": (
            "Send `X-Frame-Options: DENY` or rely on CSP's frame-ancestors directive."
        ),
        "cwe": "CWE-1021",
    },
    "referrer-policy": {
        "id": "headers.missing-referrer-policy",
        "title": "Missing Referrer-Policy",
        "severity": Severity.LOW,
        "remediation": "Send `Referrer-Policy: strict-origin-when-cross-origin`.",
        "cwe": "CWE-200",
    },
    "permissions-policy": {
        "id": "headers.missing-permissions-policy",
        "title": "Missing Permissions-Policy",
        "severity": Severity.INFO,
        "remediation": (
            "Explicitly disable powerful APIs you don't need "
            "(camera, microphone, geolocation, ...)."
        ),
    },
}


@register
class HeadersCheck(Check):
    id = "headers"
    name = "Security headers"
    description = "Audit HTTP response headers on the base URL."
    order = 20

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        try:
            resp = await ctx.get(target.url)
        except Exception as e:
            ctx.log(f"headers: base request failed: {e}", level="warn")
            return

        got = {k.lower(): v for k, v in resp.headers.items()}
        for header, spec in REQUIRED_HEADERS.items():
            if header not in got:
                yield Finding(
                    id=spec["id"],
                    category="headers",
                    severity=spec["severity"],
                    title=spec["title"],
                    url=str(resp.url),
                    description=f"Response does not include the `{header}` header.",
                    evidence={"observed_headers": list(got.keys())},
                    remediation=spec["remediation"],
                    cwe=spec.get("cwe"),
                )

        # Weak / misconfigured CSP
        csp = got.get("content-security-policy", "")
        if csp:
            issues = []
            if "unsafe-inline" in csp:
                issues.append("`unsafe-inline` allows arbitrary inline scripts/styles")
            if "unsafe-eval" in csp:
                issues.append("`unsafe-eval` permits eval() and friends")
            if "*" in csp.split():
                issues.append("wildcard `*` source defeats CSP")
            if issues:
                yield Finding(
                    id="headers.weak-csp",
                    category="headers",
                    severity=Severity.MEDIUM,
                    title="Weak Content-Security-Policy",
                    url=str(resp.url),
                    description="CSP is present but contains directives that materially weaken it.",
                    evidence={"csp": csp, "issues": issues},
                    remediation="Tighten CSP: remove unsafe-inline/unsafe-eval, pin exact sources.",
                    cwe="CWE-693",
                )

        # Cookie attributes
        for ck in resp.cookies.jar:
            flags = _cookie_flags(resp, ck)
            missing = [k for k, v in flags.items() if not v]
            if missing:
                yield Finding(
                    id="headers.cookie-weak",
                    category="headers",
                    severity=Severity.MEDIUM if "secure" in missing else Severity.LOW,
                    title=f"Cookie `{ck.name}` missing attributes",
                    url=str(resp.url),
                    description=(
                        "Cookies without Secure/HttpOnly/SameSite are susceptible to "
                        "network interception, XSS theft, or CSRF."
                    ),
                    evidence={"cookie": ck.name, "missing": missing, "flags": flags},
                    remediation=(
                        "Set `Secure; HttpOnly; SameSite=Lax` (or Strict) on session cookies."
                    ),
                    cwe="CWE-614",
                )


def _cookie_flags(resp, ck) -> dict[str, bool]:
    # httpx's CookieJar (based on http.cookiejar) exposes these attributes.
    raw = ""
    for sc in resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []:
        if sc.startswith(ck.name + "="):
            raw = sc.lower()
            break
    return {
        "secure": bool(ck.secure) or "secure" in raw,
        "httponly": ("httponly" in raw) or ck._rest.get("HttpOnly", False) is not False,
        "samesite": ("samesite" in raw) or bool(ck._rest.get("SameSite")),
    }
