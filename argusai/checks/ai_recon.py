"""AI-guided pre-scan analysis — Strix/Shannon equivalent.

Analyzes recon + JS reverse data and suggests attack strategies to
downstream checks (fuzzer, scanner) via ``ctx.shared["ai_strategy"]``.

Order = 12 (after recon=10 and js_reverse=15 — note: js_reverse writes
its data in order=15 so we check for it at execution time; if absent we
work with recon data only).

Note on ordering: Because this check has order=12 but js_reverse has order=15,
we read from recon data. To get js_reverse data, the Runner runs checks by
order — so ai_recon (12) actually runs BEFORE js_reverse (15). This is
intentional: we do an initial AI pass on raw recon data. A second optional
pass would need a restructure. For simplicity, we just read whatever
``ctx.shared`` has at execution time.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target

SYSTEM_PROMPT = """You are a senior web application security auditor.
You receive information gathered during reconnaissance of a web application:
server fingerprints, discovered paths, JS endpoints, parameters, and
cryptographic patterns found in client-side JavaScript.

Your task: produce a concise JSON attack strategy with:

1. "attack_vectors": list of {category, target, rationale} objects describing
   the most promising attack categories ordered by likelihood of success.
   Categories: sqli, xss, xxe, ssti, ssrf, lfi, rce, nosqli, idor, csrf, auth_bypass.

2. "priority_targets": list of URLs/params that warrant deeper inspection.

3. "priority_categories": list of category slugs (sqli, xss, ssti, lfi, rce, ssrf, nosqli, open_redirect)
   ordered by priority.

4. "notes": a short prose summary of your analysis.

Return ONLY valid JSON. Do not wrap in markdown fences."""

SYSTEM_PROMPT_POC = """You are a penetration testing expert who writes clear, minimal
proof-of-concept exploit scripts in Python using httpx or requests.

Given a vulnerability finding (title, category, evidence, URL, payload), write a
standalone Python PoC script that:
1. Reproduces the vulnerability with the exact payload.
2. Prints the vulnerable URL and the server's response (trimmed to 500 chars).
3. Is safe to run against the target (no destructive actions).

Return ONLY the Python code (no markdown fences, no prose). Keep it under 60 lines."""


@register
class AIReconCheck(Check):
    id = "ai_recon"
    name = "AI-Guided Analysis"
    description = "AI analyzes recon data to suggest attack strategies."
    needs_browser = False
    order = 12

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        if ctx.ai is None:
            ctx.log("ai_recon: no AI provider configured; skipping")
            return

        recon = ctx.shared.get("recon") or {}
        js_rev = ctx.shared.get("js_reverse") or {}

        # Build analysis prompt from whatever data is available
        parts: list[str] = [f"Target: {target.url}"]

        if recon.get("base_url"):
            parts.append(f"Base URL: {recon['base_url']}")

        fingerprints = recon.get("fingerprints") or {}
        if fingerprints:
            parts.append(f"Server fingerprints: {json.dumps(fingerprints)}")

        paths = recon.get("paths") or []
        if paths:
            hits = [p for p in paths if p.get("status") in (200, 301, 302, 403)]
            parts.append(f"Interesting paths ({len(hits)}): {json.dumps(hits[:30])}")

        scripts = recon.get("scripts") or {}
        if scripts:
            parts.append(f"Collected JS bundles: {len(scripts)} files")
            # Include first 2000 chars of each script for analysis
            for url, code in list(scripts.items())[:5]:
                parts.append(f"\n--- {url} (first 2000 chars) ---\n{code[:2000]}")

        if js_rev.get("api_endpoints"):
            parts.append(f"JS-discovered API endpoints: {json.dumps(js_rev['api_endpoints'][:30])}")
        if js_rev.get("crypto_functions"):
            parts.append(f"Crypto functions: {json.dumps(js_rev['crypto_functions'][:10])}")
        if js_rev.get("signing_logic"):
            parts.append(f"Signing logic detected: {json.dumps(js_rev['signing_logic'][:5])}")

        prompt = "\n".join(parts)

        try:
            ctx.log("ai_recon: sending analysis request to AI...")
            result_text = await ctx.ai.analyze(SYSTEM_PROMPT, prompt)
            ctx.log("ai_recon: received strategy from AI")
        except Exception as e:
            ctx.log(f"ai_recon: AI call failed: {e}", level="warn")
            return

        # Parse the AI response as JSON
        strategy = self._parse_json_strategy(result_text)
        if strategy is None:
            ctx.log("ai_recon: could not parse AI response as JSON", level="warn")
            # Store raw text as a note
            strategy = {
                "attack_vectors": [],
                "priority_targets": [],
                "priority_categories": [],
                "notes": result_text[:1000],
            }

        ctx.shared["ai_strategy"] = strategy

        yield Finding(
            id="ai_recon.strategy",
            category="ai",
            severity=Severity.INFO,
            title="AI attack strategy generated",
            url=target.url,
            description=(
                "An AI analyzed the recon data and produced a suggested attack "
                "strategy. This is used to prioritize downstream checks."
            ),
            evidence={
                "attack_vectors": (strategy.get("attack_vectors") or [])[:10],
                "priority_targets": (strategy.get("priority_targets") or [])[:10],
                "priority_categories": strategy.get("priority_categories") or [],
                "notes": (strategy.get("notes") or "")[:500],
            },
        )

    @staticmethod
    def _parse_json_strategy(text: str) -> dict | None:
        """Best-effort parse: strip markdown fences, try json.loads, try
        extracting the first { ... } block."""
        text = text.strip()
        # Strip ```json ... ``` fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        # Try extracting first { ... } block
        depth = 0
        start = None
        for i, c in enumerate(text):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start : i + 1])
                    except (json.JSONDecodeError, ValueError):
                        pass
                    start = None
        return None


@register
class AIPoCCheck(Check):
    id = "ai_poc"
    name = "AI PoC Generation"
    description = "Generate proof-of-concept scripts for high/critical vulnerabilities."
    needs_browser = False
    order = 95

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        if ctx.ai is None:
            ctx.log("ai_poc: no AI provider configured; skipping")
            return

        all_findings = ctx.shared.get("_all_findings") or []
        high_findings = [f for f in all_findings if f.severity.rank >= Severity.HIGH.rank]
        if not high_findings:
            ctx.log("ai_poc: no high/critical findings to generate PoCs for")
            return

        ctx.log(f"ai_poc: generating PoCs for {len(high_findings)} findings")

        for finding in high_findings[:10]:  # cap to avoid long waits
            try:
                poc_code = await ctx.ai.analyze(
                    SYSTEM_PROMPT_POC,
                    self._build_prompt(finding, target),
                )
                poc_code = self._clean_code(poc_code)
                if poc_code:
                    yield Finding(
                        id=f"ai_poc.{finding.id}",
                        category="ai",
                        severity=Severity.INFO,
                        title=f"PoC script for: {finding.title}",
                        url=finding.url or target.url,
                        description=(
                            "An AI-generated proof-of-concept script for the above "
                            "vulnerability. Review and adapt before running."
                        ),
                        evidence={
                            "original_finding_id": finding.id,
                            "original_finding_uid": finding.uid,
                            "poc_code": poc_code,
                        },
                    )
                    ctx.log(f"ai_poc: generated PoC for {finding.id}")
            except Exception as e:
                ctx.log(f"ai_poc: PoC generation failed for {finding.id}: {e}", level="warn")

    @staticmethod
    def _build_prompt(finding: Finding, target: Target) -> str:
        parts = [
            f"Target origin: {target.origin}",
            f"Vulnerability ID: {finding.id}",
            f"Category: {finding.category}",
            f"Severity: {finding.severity.value}",
            f"Title: {finding.title}",
            f"URL: {finding.url or 'N/A'}",
            f"CWE: {finding.cwe or 'N/A'}",
        ]
        if finding.description:
            parts.append(f"Description: {finding.description}")
        if finding.evidence:
            parts.append(f"Evidence: {json.dumps(finding.evidence, indent=2)[:800]}")
        return "\n".join(parts)

    @staticmethod
    def _clean_code(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove ```python and trailing ```
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        return text
