"""Access-control and IDOR checks."""

from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import urljoin

from argusai.core.security.auth_flow import idor_compare_request, mutate_object_id
from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


@register
class AccessControlCheck(Check):
    id = "access_control"
    name = "Access Control"
    description = "Probe numeric object identifiers for simple IDOR signals."
    order = 82

    async def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        candidates = []
        for p in target.paths:
            full = urljoin(target.origin + "/", p.lstrip("/"))
            if target.in_scope(full) and mutate_object_id(full).get("ok"):
                candidates.append(full)
        for url in candidates[:20]:
            result = await idor_compare_request(url, scope=target.scope)
            if not result.get("suspected"):
                continue
            yield Finding(
                id="access_control.idor",
                category="access_control",
                severity=Severity.HIGH,
                title="Possible IDOR / missing object authorization",
                url=result.get("alt_url"),
                description="A neighbouring object URL returned HTTP 200 with different content.",
                evidence=result.get("evidence") or result,
                remediation="Enforce server-side authorization for every object access and compare the requested object owner to the authenticated principal.",
                cwe="CWE-639",
            )

