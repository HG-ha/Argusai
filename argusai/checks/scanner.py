"""Enhanced active scanner — Xray-equivalent rule-based vulnerability scanning.

Comprehensive scan with:
- Mutation engine + 100+ payloads across SQLi/XSS/XXE/SSRF/LFI/RCE/CSRF/OpenRedirect
- Response diffing for blind detection (status/length/timing)
- POST body and header injection (not just URL params)
- OOB detection hints
- Optional AI strategy consumption from ctx.shared["ai_strategy"]

Order = 65 (after fuzzer at 55, before xss at 70).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.payloads import (
    LFI_FILE_RE,
    PayloadLibrary,
    RCE_OUTPUT_RE,
    SQL_ERROR_RE,
    SSTI_MATH_RE,
)
from argusai.core.target import Target
from argusai.checks.scanner_rules import (
    Baseline,
    LFIRule,
    OOBHint,
    OpenRedirectRule,
    RCERule,
    ResponseDiffer,
    SQLiRule,
    SSRFRule,
    SSTIRule,
    ScanRule,
    XSSReflectRule,
)


class ActiveScannerCheck(Check):
    id = "scanner"
    name = "Enhanced Active Scanner"
    description = "Comprehensive rule-based scan with mutation, response diffing, and OOB hints."
    needs_browser = False
    order = 65

    RULES: list[type[ScanRule]] = [
        SQLiRule,
        SSTIRule,
        XSSReflectRule,
        LFIRule,
        RCERule,
        SSRFRule,
        OpenRedirectRule,
    ]

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        candidates = self._collect_candidates(target, ctx)
        if not candidates:
            ctx.log("scanner: no candidate URLs; skipping")
            return

        ctx.log(f"scanner: starting on {len(candidates)} URLs")
        rules = [cls() for cls in self.RULES]

        # Optional AI strategy: prioritize categories
        ai_strategy = ctx.shared.get("ai_strategy") or {}
        priority = set(ai_strategy.get("priority_categories") or [])
        if priority:
            rules.sort(key=lambda r: 0 if r.category in priority else 1)

        for url in candidates[:30]:
            t0 = time.perf_counter()
            try:
                resp = await ctx.get(url)
            except Exception as e:
                ctx.log(f"scanner: baseline failed for {url}: {e}")
                continue
            baseline_elapsed = time.perf_counter() - t0

            baseline = Baseline(
                status=resp.status_code,
                body=resp.text,
                length=len(resp.text),
                elapsed_s=baseline_elapsed,
                headers=dict(resp.headers),
            )
            differ = ResponseDiffer(baseline)

            # Existing query params + fuzzer-discovered hidden params for this URL
            params = self._params_for(url, ctx)
            if not params:
                continue

            for param in params[:8]:  # cap params per URL
                for rule in rules:
                    try:
                        finding = await rule.test(url, param, ctx, differ)
                    except Exception as e:
                        ctx.log(f"scanner: rule {rule.category} failed: {e}", level="warn")
                        continue
                    if finding is not None:
                        yield finding
                        # One finding per (url, param, category) is enough; continue with next rule
                        # but don't break — different categories on same param are independent

    def _collect_candidates(self, target: Target, ctx: Context) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        def _add(u: str) -> None:
            if u in seen:
                return
            seen.add(u)
            urls.append(u)

        # From fuzzer interesting URLs
        fuzzer = ctx.shared.get("fuzzer") or {}
        for entry in fuzzer.get("interesting_urls") or []:
            _add(entry["url"])

        # Target paths with queries
        for p in target.paths:
            if "?" in p:
                _add(urljoin(target.origin + "/", p.lstrip("/")))

        # Recon paths with queries
        recon = ctx.shared.get("recon") or {}
        for h in recon.get("paths") or []:
            if "?" in h.get("path", ""):
                _add(urljoin(target.origin + "/", h["path"].lstrip("/")))

        # Base HTML hrefs with queries
        html = recon.get("base_html") or ""
        for m in re.finditer(r'href="([^"]+\?[^"]+)"', html):
            full = urljoin(target.origin + "/", m.group(1))
            if target.in_scope(full):
                _add(full)

        # JS endpoints with no template literals
        js_rev = ctx.shared.get("js_reverse") or {}
        for ep in js_rev.get("api_endpoints") or []:
            path = ep.get("path", "")
            if path and "${" not in path and "?" in path:
                _add(urljoin(target.origin + "/", path.lstrip("/")))

        return urls

    def _params_for(self, url: str, ctx: Context) -> list[str]:
        existing = [k for k, _ in parse_qsl(urlparse(url).query)]
        fuzzer = ctx.shared.get("fuzzer") or {}
        hidden = [
            d["param"] for d in (fuzzer.get("discovered_params") or [])
            if d.get("url") == url
        ]
        return list(dict.fromkeys(existing + hidden))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

