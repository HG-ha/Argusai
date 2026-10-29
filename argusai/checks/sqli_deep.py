"""Deeper but safe SQL injection checks."""

from __future__ import annotations

from typing import AsyncIterator
from urllib.parse import parse_qsl, urljoin, urlparse

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.security.sqli_tools import sqli_boolean_diff, sqli_probe_param, sqli_time_probe
from argusai.core.target import Target


@register
class SQLiDeepCheck(Check):
    id = "sqli_deep"
    name = "SQL Injection Deep"
    description = "Error, boolean and short time-based SQL injection probes."
    order = 66

    async def run(self, target: Target, ctx: Context) -> AsyncIterator[Finding]:
        urls = []
        for p in target.paths:
            full = urljoin(target.origin + "/", p.lstrip("/"))
            if "?" in full and target.in_scope(full):
                urls.append(full)
        fuzzer = ctx.shared.get("fuzzer") or {}
        for entry in fuzzer.get("interesting_urls") or []:
            url = entry.get("url", "")
            if "?" in url and target.in_scope(url):
                urls.append(url)
        seen = set()
        for url in urls[:12]:
            if url in seen:
                continue
            seen.add(url)
            params = [k for k, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)][:6]
            for param in params:
                for probe in (
                    lambda: sqli_probe_param(url, param, scope=target.scope),
                    lambda: sqli_boolean_diff(url, param, scope=target.scope),
                    lambda: sqli_time_probe(url, param, scope=target.scope, delay=2),
                ):
                    result = await probe()
                    if not result.get("vulnerable"):
                        continue
                    evidence = result.get("evidence") or result
                    yield Finding(
                        id=f"sqli_deep.{result.get('type', 'signal')}",
                        category="injection",
                        severity=Severity.HIGH,
                        title=f"SQL injection signal on `{param}`",
                        url=result.get("url") or url,
                        description="A deeper SQL injection probe produced a reproducible signal.",
                        evidence=evidence,
                        remediation="Use parameterized queries, typed query builders, and least-privilege database accounts.",
                        cwe="CWE-89",
                    )
                    break

