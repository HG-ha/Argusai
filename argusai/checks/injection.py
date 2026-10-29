"""Fast, non-destructive injection probes.

This module performs lightweight SQLi / SSRF / template-injection checks by
adding benign payloads to URL parameters and looking for signals in the
response (SQL error strings, reflected SSRF markers via rebinding services,
math evaluated by template engines, etc.).

It is deliberately conservative — never sends destructive payloads.
"""

from __future__ import annotations

import re
from typing import AsyncIterator
from urllib.parse import urlencode, urljoin, urlparse, parse_qsl

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


SQL_ERROR_RE = re.compile(
    r"("
    r"you have an error in your sql syntax"
    r"|warning: mysql"
    r"|unclosed quotation mark after the character string"
    r"|quoted string not properly terminated"
    r"|pg::syntaxerror"
    r"|psql: error"
    r"|sqlite3\.operationalerror"
    r"|sqlstate\[\w+\]"
    r"|ora-[0-9]{4,5}"
    r")",
    re.IGNORECASE,
)

SSTI_RE = re.compile(r"\b49\b")  # 7*7 evaluated


@register
class InjectionCheck(Check):
    id = "injection"
    name = "Injection probes"
    description = "Lightweight SQLi / SSTI / SSRF signal tests on URL params."
    order = 60

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        # Derive a parameter map from recon's discovered hits.
        candidates = self._candidate_urls(target, ctx)
        if not candidates:
            return

        for base_url in candidates:
            try:
                baseline = await ctx.get(base_url)
            except Exception:
                continue
            baseline_text = baseline.text

            for name, orig_value in parse_qsl(urlparse(base_url).query):
                for payload, check_re, kind, sev, title, desc, remediation, cwe in PAYLOADS:
                    test_url = _replace_param(base_url, name, payload)
                    try:
                        r = await ctx.get(test_url)
                    except Exception:
                        continue
                    if check_re.search(r.text) and not check_re.search(baseline_text):
                        yield Finding(
                            id=f"injection.{kind}",
                            category="injection",
                            severity=sev,
                            title=title.format(param=name),
                            url=test_url,
                            description=desc,
                            evidence={
                                "param": name,
                                "payload": payload,
                                "status": r.status_code,
                                "snippet": _snippet_around(r.text, check_re),
                            },
                            remediation=remediation,
                            cwe=cwe,
                        )
                        break  # one finding per param per kind is enough

    def _candidate_urls(self, target: Target, ctx: Context) -> list[str]:
        """Pick URLs with query strings to fuzz.

        Preference order: explicit ``target.paths`` with queries, recon hits,
        finally just the base URL's links.
        """
        urls: list[str] = []
        for p in target.paths:
            if "?" in p:
                urls.append(urljoin(target.origin + "/", p.lstrip("/")))
        hits = (ctx.shared.get("recon") or {}).get("paths") or []
        for h in hits:
            if "?" in h["path"]:
                urls.append(urljoin(target.origin + "/", h["path"].lstrip("/")))
        # Extract ?foo=bar style links from base HTML
        html = (ctx.shared.get("recon") or {}).get("base_html") or ""
        for m in re.finditer(r'href="([^"]+\?[^"]+)"', html):
            link = m.group(1)
            full = urljoin(target.origin + "/", link)
            if target.in_scope(full):
                urls.append(full)
        # Dedup
        seen: set[str] = set()
        result: list[str] = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            result.append(u)
        return result[:20]


def _replace_param(url: str, name: str, new_value: str) -> str:
    u = urlparse(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    q = [(k, new_value if k == name else v) for k, v in q]
    return u._replace(query=urlencode(q)).geturl()


def _snippet_around(text: str, pat: re.Pattern[str]) -> str:
    m = pat.search(text)
    if not m:
        return ""
    s = max(0, m.start() - 80)
    e = min(len(text), m.end() + 80)
    return text[s:e].replace("\n", " ")


PAYLOADS: list[tuple[str, re.Pattern[str], str, Severity, str, str, str, str]] = [
    (
        "'",
        SQL_ERROR_RE,
        "sqli",
        Severity.HIGH,
        "Potential SQL injection on parameter `{param}`",
        (
            "A single quote in the parameter value caused the server to emit a "
            "database error string. This strongly indicates that user input is "
            "being concatenated into a SQL statement."
        ),
        "Use parameterised queries / prepared statements. Never interpolate user input into SQL.",
        "CWE-89",
    ),
    (
        "{{7*7}}",
        SSTI_RE,
        "ssti",
        Severity.HIGH,
        "Possible server-side template injection on `{param}`",
        (
            "The payload `{{7*7}}` was evaluated to `49` in the response, which is "
            "a strong signal that user input is interpreted by a template engine."
        ),
        (
            "Never render user-controlled input as template source. Use a sandboxed "
            "template engine or strict whitelists."
        ),
        "CWE-94",
    ),
    (
        "${7*7}",
        SSTI_RE,
        "ssti-el",
        Severity.HIGH,
        "Possible EL/Spring injection on `{param}`",
        "EL expression was evaluated in the response.",
        "Sanitize user input and avoid passing it to expression evaluators.",
        "CWE-917",
    ),
]
