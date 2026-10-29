"""Identity-protocol, CORS and CSRF checks."""

from __future__ import annotations

from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.security.identity_tools import cors_matrix_probe, csrf_surface_analyze, oauth_flow_analyze
from argusai.core.target import Target


@register
class IdentityCheck(Check):
    id = "identity"
    name = "Identity / CORS / CSRF"
    description = "Analyze CORS, CSRF token presence and OAuth/OIDC surface clues."
    order = 84

    async def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        try:
            base = await ctx.get(target.url)
        except Exception:
            return

        cors = await cors_matrix_probe(str(base.url), scope=target.scope)
        if cors.get("suspected"):
            yield Finding(
                id="identity.cors-reflection",
                category="identity",
                severity=Severity.MEDIUM,
                title="Risky CORS origin reflection",
                url=str(base.url),
                description="The target appears to allow attacker-controlled origins with credentials.",
                evidence=cors,
                remediation="Use a strict Origin allowlist and avoid Access-Control-Allow-Credentials for untrusted origins.",
                cwe="CWE-942",
            )

        csrf = csrf_surface_analyze(base.text, str(base.url))
        if csrf.get("missing_token_count"):
            yield Finding(
                id="identity.csrf-token-missing",
                category="identity",
                severity=Severity.LOW,
                title="Forms without obvious CSRF token",
                url=str(base.url),
                description="One or more forms do not contain an obvious CSRF token input. Confirm whether they are state-changing.",
                evidence=csrf,
                remediation="Use per-request CSRF tokens for state-changing forms and APIs; SameSite cookies are defense-in-depth only.",
                cwe="CWE-352",
            )

        oauth = oauth_flow_analyze(str(base.url), base.text)
        if oauth.get("suspected"):
            yield Finding(
                id="identity.oauth-surface",
                category="identity",
                severity=Severity.INFO,
                title="OAuth/OIDC flow surface needs review",
                url=str(base.url),
                description="OAuth/OIDC flow signals suggest state/nonce or redirect URI validation should be reviewed.",
                evidence=oauth,
                remediation="Validate redirect_uri strictly, require state and OIDC nonce, and bind authorization codes to clients.",
            )

