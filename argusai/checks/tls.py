"""TLS / certificate inspection."""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from typing import AsyncIterator
from urllib.parse import urlparse

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


@register
class TLSCheck(Check):
    id = "tls"
    name = "TLS & certificate"
    description = "Inspect TLS version, cert expiry, key type."
    order = 30

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        u = urlparse(target.url)
        if u.scheme != "https":
            yield Finding(
                id="tls.plaintext",
                category="tls",
                severity=Severity.HIGH,
                title="Target served over plaintext HTTP",
                url=target.url,
                description=(
                    "The target is accessed over HTTP; all traffic including credentials "
                    "and tokens can be intercepted or modified in transit."
                ),
                remediation="Serve the site over HTTPS and 301 redirect all HTTP traffic.",
                cwe="CWE-319",
            )
            return

        host = u.hostname or ""
        port = u.port or 443
        try:
            info = _probe_tls(host, port)
        except Exception as e:
            yield Finding(
                id="tls.probe-failed",
                category="tls",
                severity=Severity.INFO,
                title=f"TLS probe failed for {host}:{port}",
                url=target.url,
                description=str(e),
            )
            return

        now = datetime.now(tz=timezone.utc)
        not_after = info["not_after"]
        days_left = (not_after - now).days
        # Replace datetimes with iso strings so evidence is JSON-serialisable.
        info["not_after"] = not_after.isoformat()
        info["not_before"] = info["not_before"].isoformat()
        info["days_left"] = days_left
        if days_left < 0:
            sev = Severity.CRITICAL
            title = f"TLS certificate expired ({-days_left} days ago)"
        elif days_left < 14:
            sev = Severity.HIGH
            title = f"TLS certificate expires soon ({days_left} days)"
        elif days_left < 30:
            sev = Severity.MEDIUM
            title = f"TLS certificate expires in {days_left} days"
        else:
            sev = None
            title = ""
        if sev:
            yield Finding(
                id="tls.cert-expiry",
                category="tls",
                severity=sev,
                title=title,
                url=target.url,
                description="Certificate expiry window is approaching or already past.",
                evidence=info,
                remediation="Renew the certificate. Configure automated renewal (ACME).",
                cwe="CWE-295",
            )

        proto = info.get("protocol", "")
        if proto in ("TLSv1", "TLSv1.1", "SSLv3"):
            yield Finding(
                id="tls.weak-protocol",
                category="tls",
                severity=Severity.HIGH,
                title=f"Legacy TLS version negotiated: {proto}",
                url=target.url,
                description="Legacy TLS versions have known weaknesses and must be disabled.",
                evidence=info,
                remediation="Disable TLS < 1.2 at the edge (nginx/openresty/cloud LB).",
                cwe="CWE-326",
            )

        # Informational finding with the full handshake details.
        yield Finding(
            id="tls.info",
            category="tls",
            severity=Severity.INFO,
            title="TLS handshake details",
            url=target.url,
            evidence=info,
        )


def _probe_tls(host: str, port: int) -> dict:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
            cipher = ssock.cipher()
            return {
                "protocol": ssock.version() or "",
                "cipher": cipher[0] if cipher else "",
                "cipher_bits": cipher[2] if cipher else 0,
                "subject": _flat_dn(cert.get("subject", ())),
                "issuer": _flat_dn(cert.get("issuer", ())),
                "not_before": _parse_cert_time(cert.get("notBefore", "")),
                "not_after": _parse_cert_time(cert.get("notAfter", "")),
                "san": [v for k, v in cert.get("subjectAltName", []) if k == "DNS"],
                "serial": cert.get("serialNumber", ""),
            }


def _flat_dn(dn) -> str:
    parts = []
    for rdn in dn:
        for k, v in rdn:
            parts.append(f"{k}={v}")
    return ",".join(parts)


def _parse_cert_time(s: str):
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
