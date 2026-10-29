"""Passive traffic analysis rules."""

from __future__ import annotations

import collections
import re

from argusai.core.events import EventBus
from argusai.core.finding import Finding, Severity
from argusai.core.security.proxy_constants import _SEEN_FINGERPRINT_CAP
from argusai.core.security.proxy_flow import TrafficFlow

_SQL_ERROR_RE = re.compile(
    r"(you have an error in your sql syntax|warning: mysql|unclosed quotation mark"
    r"|sqlite3\.operationalerror|pg::syntaxerror|sqlstate\[\w+\]|ora-[0-9]{4,5})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
_PHONE_RE = re.compile(r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}\b')
_SSN_RE = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')
_CREDIT_CARD_RE = re.compile(r'\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6011)[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b')
_JWT_RE = re.compile(r'\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')


class PassiveAnalyzer:
    """Rule-based analysis of captured traffic flows."""

    def __init__(self, bus: EventBus, *, dedup_cap: int = _SEEN_FINGERPRINT_CAP) -> None:
        self.bus = bus
        # Bounded LRU-style dedup; OrderedDict gives us O(1) move-to-end + popitem.
        self._dedup_cap = max(64, int(dedup_cap))
        self._seen_fingerprints: collections.OrderedDict[str, None] = collections.OrderedDict()

    def analyze(self, flow: TrafficFlow, *, source: str = "") -> list[Finding]:
        """Run all passive rules. Returns findings (not yielded — not async).

        ``source="decrypted"`` marks findings that only exist in plaintext a
        decryption rule recovered.
        """
        findings: list[Finding] = []

        resp_body_str = flow.response_body.decode("utf-8", errors="replace")
        resp_headers = {k.lower(): v for k, v in flow.response_headers.items()}
        set_cookies = [v for k, v in flow.raw_response_header_items if k.lower() == "set-cookie"]

        # 1. Security headers
        findings.extend(self._check_security_headers(flow, resp_headers))
        # 2. Cookie flags (multi-value aware)
        findings.extend(self._check_cookies(flow, set_cookies or self._fallback_set_cookies(resp_headers)))
        # 3. CORS misconfiguration
        findings.extend(self._check_cors(flow, resp_headers))
        # 4. Information disclosure
        findings.extend(self._check_info_disclosure(flow, resp_headers, resp_body_str))
        # 5. SQL error strings
        findings.extend(self._check_sql_errors(flow, resp_body_str))
        # 6. Sensitive data leak
        findings.extend(self._check_sensitive_data(flow, resp_body_str))
        # 7. Auth token patterns
        findings.extend(self._check_auth_tokens(flow, resp_body_str))
        # 8. Cache poisoning signals
        findings.extend(self._check_cache_poisoning(flow, resp_headers))

        unique: list[Finding] = []
        seen = self._seen_fingerprints
        for f in findings:
            fp = f"{flow.host}|{f.id}|{f.url}"
            if fp in seen:
                seen.move_to_end(fp)
                continue
            seen[fp] = None
            if len(seen) > self._dedup_cap:
                seen.popitem(last=False)
            f.metadata.setdefault("flow_id", flow.id)
            if source:
                f.metadata.setdefault("source", source)
            unique.append(f)
            self.bus.emit("finding", finding=f.to_dict())
        return unique

    def reset_dedup(self) -> None:
        self._seen_fingerprints = collections.OrderedDict()

    @staticmethod
    def _fallback_set_cookies(headers: dict[str, str]) -> list[str]:
        joined = headers.get("set-cookie", "")
        if not joined:
            return []
        # Best-effort split when only the flattened dict is available.
        return [part for part in joined.split("\n") if part.strip()]

    def _check_security_headers(self, flow: TrafficFlow, headers: dict) -> list[Finding]:
        f: list[Finding] = []
        missing: list[str] = []
        for h in ("content-security-policy", "strict-transport-security",
                   "x-content-type-options", "x-frame-options", "referrer-policy"):
            if h not in headers:
                missing.append(h)
        if missing:
            f.append(Finding(
                id="proxy.missing-headers",
                category="proxy",
                severity=Severity.LOW,
                title=f"Missing security headers ({len(missing)})",
                url=flow.url,
                evidence={"missing": missing, "host": flow.host},
                remediation="Add the missing security headers at the reverse proxy layer.",
            ))
        return f

    def _check_cookies(self, flow: TrafficFlow, set_cookies: list[str]) -> list[Finding]:
        out: list[Finding] = []
        for raw_cookie in set_cookies:
            if not raw_cookie:
                continue
            lower = raw_cookie.lower()
            missing: list[str] = []
            if "secure" not in lower:
                missing.append("Secure")
            if "httponly" not in lower:
                missing.append("HttpOnly")
            if "samesite" not in lower:
                missing.append("SameSite")
            if not missing:
                continue
            cookie_name = raw_cookie.split("=", 1)[0].strip() or "cookie"
            out.append(Finding(
                id="proxy.cookie-flags",
                category="proxy",
                severity=Severity.LOW,
                title=f"Cookie '{cookie_name}' missing attributes: {', '.join(missing)}",
                url=flow.url,
                evidence={"set_cookie": raw_cookie[:200], "missing": missing, "cookie_name": cookie_name},
                remediation="Set Secure; HttpOnly; SameSite=Lax on session cookies.",
                cwe="CWE-614",
            ))
        return out

    def _check_cors(self, flow: TrafficFlow, headers: dict) -> list[Finding]:
        f: list[Finding] = []
        acao = headers.get("access-control-allow-origin", "")
        if acao == "*":
            f.append(Finding(
                id="proxy.cors-wildcard",
                category="proxy",
                severity=Severity.MEDIUM,
                title="CORS wildcard allows all origins",
                url=flow.url,
                evidence={"acao": acao},
                remediation="Restrict CORS to specific trusted origins.",
                cwe="CWE-346",
            ))
        if flow.request_headers.get("origin", "") and "access-control-allow-credentials" in headers:
            if acao == "*" or acao == flow.request_headers.get("origin", ""):
                # Reflected origin + credentials = trusted third-party CORS
                pass  # This is actually normal for credentialed CORS
        return f

    def _check_info_disclosure(self, flow: TrafficFlow, headers: dict, body: str) -> list[Finding]:
        f: list[Finding] = []
        fp_headers = {}
        for h in ("server", "x-powered-by", "x-aspnet-version", "x-generator"):
            if h in headers:
                fp_headers[h] = headers[h]
        if fp_headers:
            f.append(Finding(
                id="proxy.fingerprint",
                category="proxy",
                severity=Severity.INFO,
                title="Server fingerprint headers in proxy traffic",
                url=flow.url,
                evidence={"headers": fp_headers},
                remediation="Strip Server/X-Powered-By headers.",
                cwe="CWE-200",
            ))
        # Stack trace detection
        if re.search(r'(Traceback \(most recent call|java\.lang\.\w+Exception|at com\.\w+\.\w+\()', body):
            f.append(Finding(
                id="proxy.stack-trace",
                category="proxy",
                severity=Severity.MEDIUM,
                title="Server stack trace leaked in response",
                url=flow.url,
                evidence={"snippet": body[:500]},
                remediation="Disable detailed error pages in production.",
                cwe="CWE-209",
            ))
        return f

    def _check_sql_errors(self, flow: TrafficFlow, body: str) -> list[Finding]:
        if _SQL_ERROR_RE.search(body):
            return [Finding(
                id="proxy.sql-error",
                category="proxy",
                severity=Severity.HIGH,
                title="SQL error string detected in response",
                url=flow.url,
                evidence={"snippet": body[:400]},
                remediation="Investigate the originating parameter; use prepared statements.",
                cwe="CWE-89",
            )]
        return []

    def _check_sensitive_data(self, flow: TrafficFlow, body: str) -> list[Finding]:
        f: list[Finding] = []
        # Only check JSON/API-like responses
        ct = flow.response_headers.get("content-type", "")
        if "json" not in ct and "javascript" not in ct and "html" not in ct:
            return f
        emails = set(_EMAIL_RE.findall(body))
        if len(emails) > 3:
            f.append(Finding(
                id="proxy.email-leak",
                category="proxy",
                severity=Severity.LOW,
                title=f"Multiple email addresses ({len(emails)}) in response",
                url=flow.url,
                evidence={"count": len(emails), "samples": list(emails)[:5]},
                remediation="Audit this endpoint for excessive data exposure.",
                cwe="CWE-200",
            ))
        cc_numbers = _CREDIT_CARD_RE.findall(body)
        if cc_numbers:
            f.append(Finding(
                id="proxy.credit-card-leak",
                category="proxy",
                severity=Severity.CRITICAL,
                title=f"Credit card numbers in response ({len(cc_numbers)})",
                url=flow.url,
                evidence={"count": len(cc_numbers)},
                remediation="This is a PCI-DSS violation. Immediately investigate the endpoint.",
                cwe="CWE-200",
            ))
        return f

    def _check_auth_tokens(self, flow: TrafficFlow, body: str) -> list[Finding]:
        f: list[Finding] = []
        # Authorization is a request header; responses almost never carry one.
        auth = flow.request_headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if len(token.split(".")) == 3:
                f.append(Finding(
                    id="proxy.jwt-in-header",
                    category="proxy",
                    severity=Severity.INFO,
                    title="JWT token observed in Authorization header",
                    url=flow.url,
                    evidence={"token_prefix": token[:50] + "..."},
                    remediation="Ensure tokens are short-lived and bound to audience.",
                ))
        # JWTs in body (SPA token response)
        jwts = _JWT_RE.findall(body)
        if jwts:
            f.append(Finding(
                id="proxy.jwt-in-body",
                category="proxy",
                severity=Severity.INFO,
                title=f"JWT tokens in response body ({len(jwts)})",
                url=flow.url,
                evidence={"tokens": [t[:50] + "..." for t in jwts[:3]]},
            ))
        return f

    def _check_cache_poisoning(self, flow: TrafficFlow, headers: dict) -> list[Finding]:
        f: list[Finding] = []
        cache_control = headers.get("cache-control", "")
        cc_lower = cache_control.lower()
        # Public caching of sensitive-looking endpoints
        if "public" in cc_lower and any(k in flow.url.lower() for k in ("/api/", "/admin", "/user", "/account")):
            if "no-store" not in cc_lower and "private" not in cc_lower:
                f.append(Finding(
                    id="proxy.cache-poisoning",
                    category="proxy",
                    severity=Severity.MEDIUM,
                    title="Sensitive endpoint has public caching enabled",
                    url=flow.url,
                    evidence={"cache_control": cache_control},
                    remediation="Set Cache-Control: no-store on sensitive endpoints.",
                    cwe="CWE-525",
                ))
        return f
