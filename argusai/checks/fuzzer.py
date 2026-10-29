"""Smart Fuzzer — context-aware payload fuzzing with parameter discovery.

Target Burp AI Fuzzer equivalent. Workflow:

1. Collect candidate URLs from recon, js_reverse, target.paths.
2. Discover hidden parameters using common name list + JS analysis +
   response diffing.
3. For each param, infer context (URL/JSON/Header/Cookie) and select
   appropriate payloads.
4. Apply mutation engine for encoding variants.
5. Send payloads, compare against baseline to detect blind vulnerabilities.
6. Write discovered params and interesting URLs to ``ctx.shared["fuzzer"]``.
7. Optionally consume ``ctx.shared["ai_strategy"]`` for prioritized vectors.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.payloads import (
    COMMON_PARAMS,
    MutationEngine,
    ParameterDiscovery,
    Payload,
    PayloadLibrary,
    SQL_ERROR_RE,
    SSTI_MATH_RE,
)
from argusai.core.target import Target


@register
class FuzzerCheck(Check):
    id = "fuzzer"
    name = "Smart Fuzzer"
    description = "Context-aware payload fuzzing with hidden parameter discovery."
    needs_browser = False
    order = 55

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        # Step 1: Collect candidate URLs
        candidates = self._collect_candidates(target, ctx)
        if not candidates:
            ctx.log("fuzzer: no candidate URLs; skipping")
            return

        ctx.log(f"fuzzer: starting on {len(candidates)} URL candidates")

        discovery = ParameterDiscovery()
        engine = MutationEngine()

        discovered_params: list[dict] = []
        interesting_urls: list[dict] = []
        ai_strategy = ctx.shared.get("ai_strategy") or {}
        ai_priority_categories: set[str] = set(ai_strategy.get("priority_categories", []))

        # Step 2-5: For each candidate URL, discover params and fuzz
        for base_url in candidates[:25]:  # cap at 25 URLs to bound runtime
            try:
                baseline = await ctx.get(base_url)
            except Exception as e:
                ctx.log(f"fuzzer: baseline fetch failed for {base_url}: {e}")
                continue

            # Discover hidden parameters
            try:
                hidden = await discovery.discover_params(
                    base_url,
                    ctx.get,
                    baseline=baseline,
                    max_params=40,
                )
            except Exception:
                hidden = []

            for h in hidden:
                discovered_params.append({
                    "url": base_url,
                    "param": h["name"],
                    "evidence": {
                        "status": h["status"],
                        "length": h["length"],
                    },
                })

            # Param list to fuzz: existing query params + discovered hidden ones
            existing_params = [k for k, _ in parse_qsl(urlparse(base_url).query)]
            hidden_names = [h["name"] for h in hidden][:10]
            params_to_fuzz = list(dict.fromkeys(existing_params + hidden_names))

            if not params_to_fuzz:
                continue

            # Build prioritized category list
            categories = ["sqli", "xss", "ssti", "lfi", "rce", "open_redirect"]
            if ai_priority_categories:
                # Move AI-prioritized categories to the front
                categories = sorted(
                    categories,
                    key=lambda c: 0 if c in ai_priority_categories else 1,
                )

            for param in params_to_fuzz:
                async for f in self._fuzz_param(
                    base_url, param, baseline, ctx, engine, categories
                ):
                    yield f
                    interesting_urls.append({
                        "url": base_url,
                        "param": param,
                        "finding_id": f.id,
                    })

        # Step 6: Write to shared state for downstream checks (scanner)
        ctx.shared["fuzzer"] = {
            "discovered_params": discovered_params,
            "interesting_urls": interesting_urls,
        }

        ctx.log(
            f"fuzzer: discovered {len(discovered_params)} hidden params, "
            f"{len(interesting_urls)} interesting URL+param pairs"
        )

    # ---- Helpers ----

    def _collect_candidates(self, target: Target, ctx: Context) -> list[str]:
        """Combine candidate URLs from recon paths, JS endpoints, and target paths."""
        urls: list[str] = []
        seen: set[str] = set()

        def _add(u: str) -> None:
            if u in seen:
                return
            seen.add(u)
            urls.append(u)

        # Target-declared paths
        for p in target.paths:
            full = urljoin(target.origin + "/", p.lstrip("/"))
            _add(full)

        # Recon-discovered paths (those with query strings are best)
        recon = ctx.shared.get("recon") or {}
        for h in recon.get("paths") or []:
            path = h.get("path", "")
            if not path:
                continue
            full = urljoin(target.origin + "/", path.lstrip("/"))
            _add(full)

        # JS-discovered API endpoints
        js_rev = ctx.shared.get("js_reverse") or {}
        for ep in js_rev.get("api_endpoints") or []:
            path = ep.get("path", "")
            if path:
                full = urljoin(target.origin + "/", path.lstrip("/"))
                # Strip template literals like ${id}
                if "${" not in full:
                    _add(full)

        # Base URL itself
        _add(target.url)

        return urls

    async def _fuzz_param(
        self,
        base_url: str,
        param: str,
        baseline: Any,
        ctx: Context,
        engine: MutationEngine,
        categories: list[str],
    ) -> AsyncIterator[Finding]:
        """Run a small set of payloads per category against a single param."""
        baseline_text = baseline.text if hasattr(baseline, "text") else ""
        baseline_status = baseline.status_code if hasattr(baseline, "status_code") else 200
        baseline_len = len(baseline_text)

        for category in categories:
            payloads = PayloadLibrary.for_category(category)
            # Sample heuristically: take the first 4 base payloads per category
            # The full scanner check will do the comprehensive run.
            for payload in payloads[:4]:
                test_url = self._inject_param(base_url, param, payload.value)
                try:
                    r = await ctx.get(test_url)
                except Exception:
                    continue

                detected = self._detect(payload, r, baseline_text, baseline_status, baseline_len)
                if detected:
                    yield Finding(
                        id=f"fuzzer.{category}.{param}",
                        category="fuzzer",
                        severity=detected["severity"],
                        title=f"Fuzzer signal: {category} on `{param}`",
                        url=test_url,
                        description=detected["description"],
                        evidence={
                            "param": param,
                            "category": category,
                            "payload": payload.value[:200],
                            "signal": detected["signal"],
                            "status": r.status_code if hasattr(r, "status_code") else None,
                        },
                        remediation=detected["remediation"],
                        cwe=detected["cwe"],
                    )
                    # Once a category triggers, move on to the next category for
                    # this param to avoid duplicate findings.
                    break

    @staticmethod
    def _inject_param(url: str, name: str, value: str) -> str:
        u = urlparse(url)
        q = parse_qsl(u.query, keep_blank_values=True)
        if any(k == name for k, _ in q):
            q = [(k, value if k == name else v) for k, v in q]
        else:
            q.append((name, value))
        return u._replace(query=urlencode(q)).geturl()

    @staticmethod
    def _detect(
        payload: Payload,
        response: Any,
        baseline_text: str,
        baseline_status: int,
        baseline_len: int,
    ) -> dict | None:
        """Apply the appropriate detection for the payload's category."""
        body = response.text if hasattr(response, "text") else ""
        status = response.status_code if hasattr(response, "status_code") else 0

        if payload.category == "sqli":
            if SQL_ERROR_RE.search(body) and not SQL_ERROR_RE.search(baseline_text):
                return {
                    "severity": Severity.HIGH,
                    "signal": "sql_error_string",
                    "description": "A SQL error string appeared after injecting the payload — strong SQL injection signal.",
                    "remediation": "Use parameterised queries / prepared statements; never interpolate user input into SQL.",
                    "cwe": "CWE-89",
                }
        elif payload.category == "ssti":
            if SSTI_MATH_RE.search(body) and not SSTI_MATH_RE.search(baseline_text):
                return {
                    "severity": Severity.HIGH,
                    "signal": "ssti_math_evaluated",
                    "description": "Math expression in payload was evaluated server-side — likely template injection.",
                    "remediation": "Never render user-controlled input as template source. Use sandboxed templates.",
                    "cwe": "CWE-94",
                }
        elif payload.category == "xss":
            if payload.value in body and payload.value not in baseline_text:
                return {
                    "severity": Severity.LOW,
                    "signal": "payload_reflected",
                    "description": "Payload reflected in response body without execution — review sanitisation.",
                    "remediation": "Use context-aware HTML escaping when rendering user input.",
                    "cwe": "CWE-79",
                }
        elif payload.category == "lfi":
            if "root:" in body and "0:0:" in body:
                return {
                    "severity": Severity.HIGH,
                    "signal": "passwd_content",
                    "description": "Response contains /etc/passwd content — local file inclusion confirmed.",
                    "remediation": "Validate file paths server-side; never concatenate user input into file paths.",
                    "cwe": "CWE-22",
                }
        elif payload.category == "open_redirect":
            location = response.headers.get("location", "") if hasattr(response, "headers") else ""
            if "evil.com" in location.lower() or "javascript:" in location.lower():
                return {
                    "severity": Severity.MEDIUM,
                    "signal": f"redirect_location:{location[:100]}",
                    "description": "Server redirects to attacker-controlled URL.",
                    "remediation": "Use allow-list for redirect destinations.",
                    "cwe": "CWE-601",
                }

        # Generic response-diff signal (very low confidence)
        if abs(len(body) - baseline_len) > max(500, baseline_len * 0.5) and status == baseline_status:
            return {
                "severity": Severity.INFO,
                "signal": f"response_size_changed:{len(body)}vs{baseline_len}",
                "description": "Response size changed significantly with the payload — worth manual review.",
                "remediation": "Manually inspect this parameter for context-specific vulnerabilities.",
                "cwe": None,
            }

        return None
