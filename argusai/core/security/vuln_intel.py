"""Offline vulnerability intelligence helpers.

This module deliberately returns candidates, not exploitation instructions.
It maps CPE/product/version evidence to reviewable risk records that can be
attached to findings and reports.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class VulnCandidate:
    id: str
    title: str
    severity: str = "medium"
    confidence: str = "suspected"
    cvss: float | None = None
    epss: float | None = None
    affected: str = ""
    remediation: str = "Review vendor advisories and patch or mitigate if applicable."
    references: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RULES: list[dict[str, Any]] = [
    {
        "id": "CVE-2021-41773",
        "product_patterns": [r"\bapache(?: httpd)?\b"],
        "cpe_patterns": [r"cpe:/a:apache:http_server"],
        "version_regex": r"\b2\.4\.49\b",
        "title": "Apache HTTP Server 2.4.49 path traversal/RCE candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "Apache HTTP Server 2.4.49",
        "references": ["https://httpd.apache.org/security/vulnerabilities_24.html"],
        "remediation": "Upgrade Apache HTTP Server to a fixed release and review exposure.",
    },
    {
        "id": "CVE-2021-42013",
        "product_patterns": [r"\bapache(?: httpd)?\b"],
        "cpe_patterns": [r"cpe:/a:apache:http_server"],
        "version_regex": r"\b2\.4\.50\b",
        "title": "Apache HTTP Server 2.4.50 path traversal/RCE candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "Apache HTTP Server 2.4.50",
        "references": ["https://httpd.apache.org/security/vulnerabilities_24.html"],
        "remediation": "Upgrade Apache HTTP Server to a fixed release and verify rewrite/alias configuration.",
    },
    {
        "id": "CVE-2019-19781",
        "product_patterns": [r"\bcitrix\b", r"\bnetscaler\b", r"\badc\b"],
        "cpe_patterns": [r"citrix"],
        "title": "Citrix ADC/Gateway path traversal candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "Citrix ADC/Gateway vulnerable builds",
        "references": ["https://support.citrix.com/article/CTX267027"],
        "remediation": "Confirm exact build and apply Citrix remediation guidance.",
    },
    {
        "id": "CVE-2022-1388",
        "product_patterns": [r"\bf5\b", r"\bbig-?ip\b"],
        "cpe_patterns": [r"f5:big-ip"],
        "title": "F5 BIG-IP iControl REST authentication bypass candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "F5 BIG-IP vulnerable builds",
        "references": ["https://support.f5.com/csp/article/K23605346"],
        "remediation": "Restrict management interface exposure and patch affected BIG-IP versions.",
    },
    {
        "id": "CVE-2023-34362",
        "product_patterns": [r"\bmoveit\b"],
        "cpe_patterns": [r"moveit"],
        "title": "MOVEit Transfer SQL injection candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "MOVEit Transfer vulnerable builds",
        "references": ["https://community.progress.com/s/article/MOVEit-Transfer-Critical-Vulnerability-31May2023"],
        "remediation": "Patch MOVEit Transfer and review vendor incident guidance.",
    },
    {
        "id": "CVE-2021-22986",
        "product_patterns": [r"\bf5\b", r"\bbig-?ip\b"],
        "cpe_patterns": [r"f5:big-ip"],
        "title": "F5 BIG-IP iControl REST RCE candidate",
        "severity": "critical",
        "cvss": 9.8,
        "affected": "F5 BIG-IP vulnerable builds",
        "references": ["https://support.f5.com/csp/article/K03009991"],
        "remediation": "Patch and restrict management plane access.",
    },
    {
        "id": "CVE-2017-5638",
        "product_patterns": [r"\bstruts\b"],
        "cpe_patterns": [r"apache:struts"],
        "title": "Apache Struts Jakarta multipart parser RCE candidate",
        "severity": "critical",
        "cvss": 10.0,
        "affected": "Apache Struts vulnerable builds",
        "references": ["https://cwiki.apache.org/confluence/display/WW/S2-045"],
        "remediation": "Upgrade Struts and verify multipart parser exposure.",
    },
    {
        "id": "EOL-OPENSSH-LEGACY",
        "product_patterns": [r"\bopenssh\b"],
        "version_regex": r"\b([1-6]\.|7\.[0-4])",
        "title": "Legacy OpenSSH version candidate",
        "severity": "medium",
        "affected": "OpenSSH before modern supported releases",
        "remediation": "Confirm exact distribution backports; upgrade or apply vendor security updates.",
    },
    {
        "id": "EOL-NGINX-LEGACY",
        "product_patterns": [r"\bnginx\b"],
        "version_regex": r"\b0\.|1\.0\.|1\.1\.|1\.2\.|1\.3\.|1\.4\.|1\.5\.|1\.6\.|1\.7\.|1\.8\.",
        "title": "Legacy nginx version candidate",
        "severity": "medium",
        "affected": "Old nginx release line",
        "remediation": "Confirm vendor backports and upgrade to a supported release line.",
    },
]


def _text_from_fingerprint(fp: dict[str, Any]) -> str:
    cpe = " ".join(str(x) for x in fp.get("cpe") or [])
    headers = " ".join(f"{k}: {v}" for k, v in (fp.get("http_headers") or {}).items())
    return " ".join([
        str(fp.get("service") or ""),
        str(fp.get("product") or ""),
        str(fp.get("version") or ""),
        str(fp.get("banner") or ""),
        cpe,
        headers,
    ]).lower()


def match_service_vulns(fingerprint: dict[str, Any]) -> dict[str, Any]:
    text = _text_from_fingerprint(fingerprint)
    matches: list[VulnCandidate] = []
    for rule in RULES:
        product_hit = any(re.search(pat, text, re.I) for pat in rule.get("product_patterns", []))
        cpe_hit = any(re.search(pat, text, re.I) for pat in rule.get("cpe_patterns", []))
        version_regex = rule.get("version_regex")
        version_hit = bool(re.search(version_regex, text, re.I)) if version_regex else True
        if (product_hit or cpe_hit) and version_hit:
            matches.append(VulnCandidate(
                id=rule["id"],
                title=rule.get("title", rule["id"]),
                severity=rule.get("severity", "medium"),
                confidence="suspected" if not cpe_hit else "probable",
                cvss=rule.get("cvss"),
                epss=rule.get("epss"),
                affected=rule.get("affected", ""),
                remediation=rule.get("remediation", VulnCandidate("x", "x").remediation),
                references=list(rule.get("references") or []),
                evidence={"matched_text_excerpt": text[:500], "cpe_hit": cpe_hit, "product_hit": product_hit},
            ))
    return {"ok": True, "matches": [m.to_dict() for m in matches], "count": len(matches)}


def match_profile(profile: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for port in profile.get("ports", []):
        fp = port.get("fingerprint") or {}
        matched = match_service_vulns(fp)
        for item in matched.get("matches", []):
            item["port"] = port.get("port")
            item["service"] = fp.get("service")
            results.append(item)
    return {"ok": True, "target": profile.get("host", ""), "matches": results, "count": len(results)}
