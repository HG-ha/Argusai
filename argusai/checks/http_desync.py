"""HTTP desync/cache poisoning lead checks."""

from __future__ import annotations

from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.security.http_desync import cache_poison_probe
from argusai.core.target import Target


@register
class HTTPDesyncCheck(Check):
    id = "http_desync"
    name = "HTTP Desync / Cache Poisoning"
    description = "Low-risk cache poisoning header reflection leads; raw desync remains an assistant-confirmed tool."
    order = 86

    async def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        result = await cache_poison_probe(target.url, scope=target.scope)
        if not result.get("reflected"):
            return
        yield Finding(
            id="http_desync.cache-header-reflection",
            category="protocol",
            severity=Severity.LOW,
            title="Cache-related header reflected",
            url=target.url,
            description="A cache/proxy-related header value was reflected. This is a cache poisoning lead and requires manual confirmation.",
            evidence=result,
            remediation="Do not incorporate untrusted forwarding headers into cache keys, redirects, links or HTML without validation.",
            cwe="CWE-444",
        )

