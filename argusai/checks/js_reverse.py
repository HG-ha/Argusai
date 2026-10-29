"""JS/Frontend reverse engineering — extract API endpoints, params, crypto from JS bundles.

Reads ``ctx.shared["recon"]["scripts"]`` collected by the recon check,
performs deep analysis, and writes results to ``ctx.shared["js_reverse"]``.

Order = 15 (after recon at 10, before headers/tls/secrets/etc).
"""

from __future__ import annotations

import re
from typing import AsyncIterator

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.reverse.js_reverse_tools import (
    analyze_js_bundle,
    captcha_detect,
    control_flow_hints,
    extract_webpack_modules,
    signature_candidates,
)
from argusai.core.target import Target


# ---------------------------------------------------------------------------
# Endpoint extraction patterns
# ---------------------------------------------------------------------------

# fetch("/api/...") or fetch(`...`)
_FETCH_RE = re.compile(
    r'''fetch\s*\(\s*[`'"](\/[^'"`\s]+)[`'"]''',
    re.DOTALL,
)
# axios.get/post/put/delete("/api/...")
_AXIOS_RE = re.compile(
    r'''axios\s*\.\s*(?:get|post|put|delete|patch|request)\s*\(\s*[`'"](\/[^'"`\s]+)[`'"]''',
    re.DOTALL,
)
# $.ajax({ url: "/api/..." }) or $.get/post
_JQUERY_AJAX_RE = re.compile(
    r'''\$\s*\.\s*(?:ajax|get|post)\s*\(\s*(?:\{[^}]*url\s*:\s*[`'"]|[^,]*,\s*[`'"])(\/[^'"`\s]+)[`'"]''',
    re.DOTALL,
)
# new Request("/api/...")
_REQUEST_RE = re.compile(
    r'''new\s+Request\s*\(\s*[`'"](\/[^'"`\s]+)[`'"]''',
)
# new URL("/api/...", ...)
_NEW_URL_RE = re.compile(
    r'''new\s+URL\s*\(\s*[`'"](\/[^'"`\s]+)[`'"]''',
)
# XMLHttpRequest open("GET", "/api/...")
_XHR_RE = re.compile(
    r'''\.open\s*\(\s*[`'"](?:GET|POST|PUT|DELETE|PATCH)[`'"]\s*,\s*[`'"](\/[^'"`\s]+)[`'"]''',
    re.IGNORECASE,
)
# Template literal endpoints: `/api/users/${id}`
_TEMPLATE_URL_RE = re.compile(
    r'''[`'"](\/[a-zA-Z][\w/.-]*\$\{[^}]+}[a-zA-Z/\w.-]*)[`'"]''',
)
# Hardcoded full URLs pointing to same origin APIs
_FULL_API_RE = re.compile(
    r'''[`'"]https?://[^'"`\s]+(/api/[^'"`\s]+)[`'"]''',
)


# ---------------------------------------------------------------------------
# Crypto / signing detection patterns
# ---------------------------------------------------------------------------

_CRYPTO_KEYWORDS = re.compile(
    r'('
    r'CryptoJS|crypto\.subtle|SubtleCrypto'
    r'|HMAC|hmac'
    r'|\bAES\b|\bDES\b|\bRSA\b|\bECDSA\b'
    r'|\bMD5\b|\bSHA(?:1|256|512)?\b'
    r'|pbkdf2|scrypt|argon2'
    r'|\bsign\(|\.sign\s*\(|verify\(|\.verify\s*\('
    r'|\bhash\(|\.hash\s*\(|digest\(|\.digest\s*\('
    r'|createCipher|createDecipher|createHmac|createHash'
    r'|encrypt\(|decrypt\('
    r')',
    re.IGNORECASE,
)

_SIGNING_PATTERN = re.compile(
    r'(?:'
    r'(?:(?:param|key|field|item|k)[s]?)\s*'
    r'(?:\.sort\(|\.sortBy\(|\.orderBy\()'
    r')'
    r'|'
    r'(?:'
    r'(?:join|concat)\s*\(\s*[`\'"][&=,][`\'"]\s*\)'
    r'.*?'
    r'(?:sign|hmac|hash|secret|key)'
    r')',
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Route extraction patterns
# ---------------------------------------------------------------------------

# React Router: { path: "/xxx", ... }
_REACT_ROUTE_RE = re.compile(
    r'''path\s*:\s*[`'"](/[^`'"]*)[`'"]''',
)
# Vue Router: { path: '/xxx', ... } or routes: [{ ... }]
_VUE_ROUTE_RE = re.compile(
    r'''(?:path|redirect)\s*:\s*[`'"](/[^`'"]*)[`'"]''',
)


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------

# Object keys in request body contexts
_BODY_PARAM_RE = re.compile(
    r'''(?:body|data|payload|params)\s*(?:[:,=]|\.)\s*\{([^}]{10,500})\}''',
    re.DOTALL,
)
_PARAM_KEY_RE = re.compile(r'''[`'"](\w[\w.-]*)[`'"]\s*:''')

# FormData.append("key", ...)
_FORM_DATA_RE = re.compile(
    r'''\.append\s*\(\s*[`'"](\w[\w.-]*)[`'"]''',
)


# ---------------------------------------------------------------------------
# API pattern reconstruction
# ---------------------------------------------------------------------------

_BASE_URL_RE = re.compile(
    r'''(?:baseURL|base_url|BASE_URL|baseUrl|apiUrl|API_URL|apiBase)\s*[:=]\s*[`'"]([^`'"]+)[`'"]''',
    re.IGNORECASE,
)

_AUTH_HEADER_RE = re.compile(
    r'''(?:Authorization|authorization|AUTH|authHeader)\s*[:=]\s*[`'"]([^`'"]{5,200})[`'"]''',
)

_INTERCEPTOR_RE = re.compile(
    r'''(?:interceptors?\.\w+\.\w+|beforeSend|request\.use|response\.use)''',
    re.IGNORECASE,
)


@register
class JSReverseCheck(Check):
    id = "js_reverse"
    name = "JS Reverse Engineering"
    description = "Extract API endpoints, parameters, and crypto patterns from JS bundles."
    needs_browser = False
    order = 15

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        scripts = (ctx.shared.get("recon") or {}).get("scripts") or {}
        if not scripts:
            ctx.log("js_reverse: no scripts from recon; skipping")
            return

        ctx.log(f"js_reverse: analyzing {len(scripts)} JS bundles")

        analysis: dict = {
            "api_endpoints": [],
            "params": set(),
            "crypto_functions": [],
            "routes": [],
            "api_patterns": [],
            "signing_logic": [],
            "bundles": [],
            "source_maps": [],
            "webpack_modules": [],
            "deobfuscation_hints": [],
            "captcha_surfaces": [],
            "signing_candidates": [],
        }

        for url, code in scripts.items():
            self._extract_endpoints(code, url, analysis)
            self._extract_params(code, analysis)
            self._extract_crypto(code, url, analysis)
            self._extract_routes(code, url, analysis)
            self._extract_api_patterns(code, url, analysis)
            bundle = analyze_js_bundle(code, source_url=url)
            analysis["bundles"].append({
                "source": url,
                "size": bundle.get("size"),
                "sha256": bundle.get("sha256"),
                "bundler": bundle.get("bundler"),
                "bundle_hints": bundle.get("bundle_hints", []),
                "framework_hints": bundle.get("framework_hints", []),
                "chunk_references": bundle.get("chunk_references", [])[:20],
            })
            analysis["source_maps"].extend(
                [{"source": url, **item} for item in bundle.get("source_maps", [])]
            )
            modules = extract_webpack_modules(code, source_url=url, max_modules=60).get("modules", [])
            analysis["webpack_modules"].extend(modules)
            analysis["deobfuscation_hints"].extend(
                [{"source": url, **item} for item in control_flow_hints(code).get("hints", [])]
            )
            captcha = captcha_detect(code=code, source_url=url)
            if captcha.get("detected") or captcha.get("anti_bot", {}).get("hits"):
                analysis["captcha_surfaces"].append(captcha)
            analysis["signing_candidates"].extend(
                signature_candidates(code, source_url=url, max_items=40).get("candidates", [])
            )

        # Convert sets to lists for JSON serialization
        analysis["params"] = sorted(analysis["params"])

        # Yield findings for sensitive discoveries
        if analysis["crypto_functions"]:
            yield Finding(
                id="js_reverse.crypto-detected",
                category="recon",
                severity=Severity.LOW,
                title=f"Client-side crypto detected ({len(analysis['crypto_functions'])} instances)",
                url=target.url,
                description=(
                    "Cryptographic operations were found in client-side JavaScript. "
                    "This may indicate signing logic, token generation, or encryption "
                    "that could be reversed by an attacker."
                ),
                evidence={
                    "crypto_count": len(analysis["crypto_functions"]),
                    "samples": analysis["crypto_functions"][:10],
                },
                remediation=(
                    "Review client-side crypto for secret key exposure. "
                    "Move signing and encryption to server-side where possible."
                ),
                cwe="CWE-321",
            )

        if analysis["signing_logic"]:
            yield Finding(
                id="js_reverse.signing-logic",
                category="recon",
                severity=Severity.MEDIUM,
                title="Client-side request signing logic detected",
                url=target.url,
                description=(
                    "JavaScript code constructs signed requests by sorting parameters "
                    "and computing a hash/HMAC. The signing secret or algorithm may be "
                    "extractable from the bundle, allowing request forgery."
                ),
                evidence={"patterns": analysis["signing_logic"][:5]},
                remediation=(
                    "Never embed signing secrets in client-side code. "
                    "Use server-side request signing or HMAC with server-held keys."
                ),
                cwe="CWE-798",
            )

        if analysis["source_maps"]:
            yield Finding(
                id="js_reverse.sourcemap-exposed",
                category="recon",
                severity=Severity.MEDIUM,
                title=f"JavaScript source map references discovered ({len(analysis['source_maps'])})",
                url=target.url,
                description=(
                    "JavaScript bundles reference source maps. If accessible, source maps may expose "
                    "original frontend source code, internal routes, comments, and signing logic."
                ),
                evidence={"source_maps": analysis["source_maps"][:20]},
                remediation="Do not publish production source maps unless they are access-controlled and intentionally exposed.",
                cwe="CWE-540",
            )

        if analysis["signing_candidates"] and not analysis["signing_logic"]:
            yield Finding(
                id="js_reverse.signing-candidates",
                category="recon",
                severity=Severity.LOW,
                title=f"Possible client-side signing/token logic ({len(analysis['signing_candidates'])} hints)",
                url=target.url,
                description="Potential sign/token/nonce/hash generation code was found in frontend bundles.",
                evidence={"candidates": analysis["signing_candidates"][:10]},
                remediation="Review whether trust decisions depend on client-generated signatures or tokens.",
                cwe="CWE-345",
            )

        if analysis["captcha_surfaces"]:
            yield Finding(
                id="js_reverse.captcha-surface",
                category="recon",
                severity=Severity.INFO,
                title="Captcha or anti-automation frontend logic detected",
                url=target.url,
                description=(
                    "Captcha, anti-bot, or browser fingerprinting scripts were detected in frontend code. "
                    "Runtime tracing may be needed to understand token generation and server-side verification."
                ),
                evidence={"captcha_surfaces": analysis["captcha_surfaces"][:10]},
                remediation="Ensure captcha tokens are verified server-side and anti-bot logic does not rely on client-only checks.",
                cwe="CWE-602",
            )

        if analysis["deobfuscation_hints"]:
            yield Finding(
                id="js_reverse.obfuscation-hints",
                category="recon",
                severity=Severity.INFO,
                title="Obfuscated or packed JavaScript patterns detected",
                url=target.url,
                description="The frontend contains patterns such as string arrays, eval packers, debugger traps, or control-flow flattening.",
                evidence={"hints": analysis["deobfuscation_hints"][:10]},
                remediation="Avoid relying on client-side obfuscation for security. Move secrets and authorization decisions server-side.",
                cwe="CWE-656",
            )

        hidden_endpoints = [
            ep for ep in analysis["api_endpoints"]
            if not self._is_linked_from_html(ep["path"], ctx)
        ]
        if hidden_endpoints:
            yield Finding(
                id="js_reverse.hidden-endpoints",
                category="recon",
                severity=Severity.LOW,
                title=f"{len(hidden_endpoints)} API endpoints found only in JS (not linked from HTML)",
                url=target.url,
                description=(
                    "These API paths appear in JavaScript bundles but are not linked "
                    "from the base HTML page. They may expose internal or undocumented "
                    "functionality."
                ),
                evidence={"endpoints": hidden_endpoints[:20]},
                cwe="CWE-200",
            )

        # Store analysis for downstream checks
        ctx.shared["js_reverse"] = analysis
        ctx.log(
            f"js_reverse: found {len(analysis['api_endpoints'])} endpoints, "
            f"{len(analysis['params'])} params, "
            f"{len(analysis['crypto_functions'])} crypto, "
            f"{len(analysis['routes'])} routes, "
            f"{len(analysis['webpack_modules'])} modules"
        )

    # ---- Endpoint extraction ----

    def _extract_endpoints(self, code: str, source_url: str, analysis: dict) -> None:
        seen = set()
        for regex in (_FETCH_RE, _AXIOS_RE, _JQUERY_AJAX_RE, _REQUEST_RE,
                      _NEW_URL_RE, _XHR_RE, _TEMPLATE_URL_RE, _FULL_API_RE):
            for m in regex.finditer(code):
                path = m.group(1) if regex != _FULL_API_RE else m.group(1)
                if path in seen or len(path) < 2:
                    continue
                seen.add(path)
                analysis["api_endpoints"].append({
                    "path": path,
                    "source": source_url,
                    "type": self._guess_endpoint_type(path),
                })

    def _extract_params(self, code: str, analysis: dict) -> None:
        # From object literals near body/data/payload/params
        for m in _BODY_PARAM_RE.finditer(code):
            block = m.group(1)
            for km in _PARAM_KEY_RE.finditer(block):
                analysis["params"].add(km.group(1))
        # From FormData.append
        for m in _FORM_DATA_RE.finditer(code):
            analysis["params"].add(m.group(1))
        # From URL query params in the code
        for m in re.finditer(r'[?&]([a-zA-Z_]\w*)=', code):
            analysis["params"].add(m.group(1))

    def _extract_crypto(self, code: str, source_url: str, analysis: dict) -> None:
        for m in _CRYPTO_KEYWORDS.finditer(code):
            # Get surrounding context (80 chars each side)
            start = max(0, m.start() - 80)
            end = min(len(code), m.end() + 80)
            snippet = code[start:end].replace("\n", " ").strip()
            analysis["crypto_functions"].append({
                "keyword": m.group(0),
                "source": source_url,
                "context": snippet,
            })
        # Check for signing patterns
        for m in _SIGNING_PATTERN.finditer(code):
            start = max(0, m.start() - 120)
            end = min(len(code), m.end() + 120)
            snippet = code[start:end].replace("\n", " ").strip()
            analysis["signing_logic"].append({
                "source": source_url,
                "context": snippet,
            })

    def _extract_routes(self, code: str, source_url: str, analysis: dict) -> None:
        seen = set()
        for regex in (_REACT_ROUTE_RE, _VUE_ROUTE_RE):
            for m in regex.finditer(code):
                path = m.group(1)
                if path in seen or len(path) < 2:
                    continue
                seen.add(path)
                analysis["routes"].append({
                    "path": path,
                    "source": source_url,
                })

    def _extract_api_patterns(self, code: str, source_url: str, analysis: dict) -> None:
        # Base URL configuration
        for m in _BASE_URL_RE.finditer(code):
            analysis["api_patterns"].append({
                "type": "base_url",
                "value": m.group(1),
                "source": source_url,
            })
        # Auth header patterns
        for m in _AUTH_HEADER_RE.finditer(code):
            analysis["api_patterns"].append({
                "type": "auth_header",
                "value": m.group(1)[:100],
                "source": source_url,
            })
        # Interceptors
        for m in _INTERCEPTOR_RE.finditer(code):
            start = max(0, m.start() - 60)
            end = min(len(code), m.end() + 120)
            snippet = code[start:end].replace("\n", " ").strip()
            analysis["api_patterns"].append({
                "type": "interceptor",
                "context": snippet,
                "source": source_url,
            })

    # ---- Helpers ----

    @staticmethod
    def _guess_endpoint_type(path: str) -> str:
        p = path.lower()
        if "/api/" in p or p.startswith("/v"):
            return "rest_api"
        if "/graphql" in p:
            return "graphql"
        if "/ws" in p or "/socket" in p:
            return "websocket"
        if any(ext in p for ext in (".json", ".xml", ".csv")):
            return "data_file"
        return "unknown"

    @staticmethod
    def _is_linked_from_html(path: str, ctx: Context) -> bool:
        """Check if an endpoint path appears in the base HTML."""
        html = (ctx.shared.get("recon") or {}).get("base_html") or ""
        return path in html
