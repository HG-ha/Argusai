"""Secret-scanner for JS bundles, HTML, and JSON responses."""

from __future__ import annotations

import re
from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


PATTERNS: list[tuple[str, str, Severity, re.Pattern[str]]] = [
    (
        "aws-access-key",
        "AWS access key id",
        Severity.HIGH,
        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "aws-secret",
        "AWS secret key (heuristic)",
        Severity.HIGH,
        re.compile(r"(?i)aws[_-]?secret[_-]?access[_-]?key[\"'\s:=]+([A-Za-z0-9/+=]{40})"),
    ),
    (
        "google-api-key",
        "Google API key",
        Severity.HIGH,
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    (
        "stripe-live-key",
        "Stripe live key",
        Severity.CRITICAL,
        re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b"),
    ),
    (
        "stripe-pub-key",
        "Stripe publishable key",
        Severity.INFO,
        re.compile(r"\bpk_live_[0-9a-zA-Z]{24,}\b"),
    ),
    (
        "github-token",
        "GitHub personal token",
        Severity.HIGH,
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    ),
    (
        "slack-token",
        "Slack token",
        Severity.HIGH,
        re.compile(r"\bxox[aboprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "private-key-block",
        "PEM private key block",
        Severity.CRITICAL,
        re.compile(r"-----BEGIN (?:RSA|EC|DSA|OPENSSH|PRIVATE) KEY-----"),
    ),
    (
        "jwt",
        "Embedded JSON Web Token",
        Severity.MEDIUM,
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "generic-password",
        "Hard-coded password assignment",
        Severity.MEDIUM,
        re.compile(
            r"(?i)(?:password|passwd|pwd|secret)\s*[:=]\s*['\"]([^'\"\s]{6,})['\"]"
        ),
    ),
]


@register
class SecretsCheck(Check):
    id = "secrets"
    name = "Secret leakage"
    description = "Scan JS bundles, HTML and exposed JSON for secrets."
    order = 40

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        blobs: dict[str, str] = {}

        # Feed from recon's shared state.
        shared = ctx.shared.get("recon", {})
        if shared.get("base_html"):
            blobs[shared.get("base_url") or target.url] = shared["base_html"]
        for url, code in (shared.get("scripts") or {}).items():
            blobs[url] = code

        # Fallback: fetch base URL if recon didn't run.
        if not blobs:
            try:
                r = await ctx.get(target.url)
                blobs[target.url] = r.text
            except Exception:
                return

        seen: set[tuple[str, str, str]] = set()
        for url, text in blobs.items():
            for pid, name, sev, pat in PATTERNS:
                for m in pat.finditer(text):
                    secret = m.group(0)
                    key = (pid, url, secret[:80])
                    if key in seen:
                        continue
                    seen.add(key)
                    snippet = _snippet(text, m.start(), m.end())
                    yield Finding(
                        id=f"secrets.{pid}",
                        category="secrets",
                        severity=sev,
                        title=f"Possible {name}",
                        url=url,
                        description=(
                            f"A pattern matching `{name}` was found in a client-served "
                            "resource. Verify whether this is a genuine credential — if "
                            "so, rotate it and remove it from the bundle."
                        ),
                        evidence={"match": secret[:120], "context": snippet},
                        remediation=(
                            "Never embed production secrets in code served to browsers. "
                            "Move to a server-side proxy or a scoped public token."
                        ),
                        cwe="CWE-798",
                    )


def _snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e].replace("\n", " ")
