"""Network reconnaissance primitives for authorized assessments.

The module is intentionally conservative: it performs scoped connect probes,
lightweight protocol handshakes, service fingerprinting, and non-destructive
exposure assessment. Exploit-chain helpers only produce gated plans and
minimal verification guidance; they do not run destructive payloads.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

from argusai.core.finding import Finding, Severity

COMMON_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 443, 445, 465, 587,
    993, 995, 1433, 1521, 2049, 2375, 2376, 3000, 3306, 3389, 5000, 5432,
    5601, 5900, 6379, 8000, 8080, 8443, 9200, 9300, 11211, 27017,
]
HTTP_PORTS = {80, 443, 8000, 8080, 8081, 8443, 8888, 9000}
TLS_PORTS = {443, 465, 636, 993, 995, 2376, 8443}
DATABASE_PORTS = {
    1433: "mssql",
    1521: "oracle",
    3306: "mysql",
    5432: "postgresql",
    6379: "redis",
    9200: "elasticsearch",
    9300: "elasticsearch-transport",
    11211: "memcached",
    27017: "mongodb",
}
MANAGEMENT_PORTS = {
    22: "ssh",
    23: "telnet",
    3389: "rdp",
    5900: "vnc",
    2375: "docker-api",
    2376: "docker-api-tls",
}
PROTOCOL_FAMILIES = {
    21: "ftp",
    22: "ssh",
    23: "telnet",
    25: "smtp",
    53: "dns",
    80: "http",
    110: "pop3",
    135: "msrpc",
    139: "netbios",
    143: "imap",
    389: "ldap",
    443: "https",
    445: "smb",
    465: "smtps",
    587: "smtp-submission",
    636: "ldaps",
    993: "imaps",
    995: "pop3s",
    1433: "mssql",
    2375: "docker-api",
    2376: "docker-api-tls",
    3306: "mysql",
    3389: "rdp",
    5432: "postgresql",
    5601: "kibana",
    5900: "vnc",
    6379: "redis",
    6443: "kubernetes-api",
    8080: "http-alt",
    8443: "https-alt",
    9200: "elasticsearch",
    11211: "memcached",
    27017: "mongodb",
}
DEFAULT_ALLOWED_ACTIONS = [
    "service fingerprinting",
    "non-destructive vulnerability validation",
    "evidence collection",
]
DEFAULT_FORBIDDEN_ACTIONS = [
    "persistence",
    "destructive changes",
    "data exfiltration",
    "unauthorized lateral movement",
]


@dataclass
class ServiceFingerprint:
    service: str = "unknown"
    product: str = ""
    version: str = ""
    banner: str = ""
    protocol: str = "tcp"
    tls: bool = False
    http_status: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    cpe: list[str] = field(default_factory=list)
    confidence: str = "low"
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortProfile:
    port: int
    state: str = "unknown"
    reason: str = ""
    fingerprint: ServiceFingerprint = field(default_factory=ServiceFingerprint)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.fingerprint.to_dict()
        return d


@dataclass
class OsFingerprint:
    name: str = ""
    accuracy: int = 0
    method: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HostProfile:
    host: str
    resolved_ips: list[str] = field(default_factory=list)
    ports: list[PortProfile] = field(default_factory=list)
    os: list[OsFingerprint] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "resolved_ips": self.resolved_ips,
            "ports": [p.to_dict() for p in self.ports],
            "os": [o.to_dict() for o in self.os],
            "findings": self.findings,
            "duration_ms": self.duration_ms,
        }


@dataclass
class PentestEngagement:
    target: str
    scope: list[str]
    objective: str = "authorized validation"
    allowed_actions: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_ACTIONS))
    forbidden_actions: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_ACTIONS))
    rate_limit: str = "conservative"
    time_window: str = ""
    authorization_required: bool = True

    @classmethod
    def from_rules(
        cls,
        target: str,
        scope: str | list[str] | None,
        objective: str,
        rules: dict[str, Any] | None = None,
    ) -> "PentestEngagement":
        raw = dict(rules or {})
        return cls(
            target=normalize_host(target),
            scope=normalize_scope(scope),
            objective=objective or "authorized validation",
            allowed_actions=list(raw.get("allowed_actions") or DEFAULT_ALLOWED_ACTIONS),
            forbidden_actions=list(raw.get("forbidden_actions") or DEFAULT_FORBIDDEN_ACTIONS),
            rate_limit=str(raw.get("rate_limit") or "conservative"),
            time_window=str(raw.get("time_window") or ""),
            authorization_required=raw.get("authorization_required", True) is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_host(target: str) -> str:
    raw = (target or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or raw.split("/")[0].split(":")[0]).strip("[]").lower()


def normalize_scope(scope: str | list[str] | None) -> list[str]:
    if not scope:
        return []
    if isinstance(scope, str):
        return [s.strip().lower() for s in scope.replace("\n", ",").split(",") if s.strip()]
    return [str(s).strip().lower() for s in scope if str(s).strip()]


def host_in_scope(host: str, scope: str | list[str] | None) -> bool:
    host = normalize_host(host)
    if not host:
        return False
    items = normalize_scope(scope)
    if not items:
        return False
    host_ip = None
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    for item in items:
        item = item.lstrip("*.").strip()
        if not item:
            continue
        if host == item or host.endswith("." + item):
            return True
        try:
            net = ipaddress.ip_network(item, strict=False)
            if host_ip and host_ip in net:
                return True
        except ValueError:
            try:
                if host_ip and host_ip == ipaddress.ip_address(item):
                    return True
            except ValueError:
                pass
    return False


def parse_ports(ports: str | list[int] | None, *, max_ports: int = 256) -> list[int]:
    if ports is None or ports == "" or ports == "top-100":
        selected = COMMON_PORTS
    elif isinstance(ports, list):
        selected = [int(p) for p in ports]
    else:
        raw = str(ports).strip().lower()
        if raw in {"common", "top", "default"}:
            selected = COMMON_PORTS
        elif raw in {"all", "-", "1-65535"}:
            selected = list(range(1, min(65535, max_ports) + 1))
        else:
            selected = []
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start_s, end_s = part.split("-", 1)
                    start, end = int(start_s), int(end_s)
                    selected.extend(range(max(1, start), min(65535, end) + 1))
                else:
                    selected.append(int(part))
    deduped = sorted({p for p in selected if 1 <= int(p) <= 65535})
    return deduped[:max_ports]


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    ips = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    return ips


def _read_http(sock: socket.socket, host: str, port: int) -> tuple[str, dict[str, str], str]:
    request = f"HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: ArgusAI-Recon/1.0\r\nConnection: close\r\n\r\n"
    sock.sendall(request.encode("ascii", errors="ignore"))
    data = sock.recv(4096).decode("utf-8", errors="replace")
    lines = data.splitlines()
    status = lines[0].strip() if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    service = "https" if port in TLS_PORTS else "http"
    return service, headers, status


def _probe_port_sync(host: str, port: int, timeout: float) -> PortProfile:
    fp = ServiceFingerprint()
    try:
        raw_sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return PortProfile(port=port, state="closed", reason=exc.__class__.__name__, fingerprint=fp)
    with raw_sock:
        raw_sock.settimeout(timeout)
        sock: socket.socket | ssl.SSLSocket = raw_sock
        if port in TLS_PORTS:
            try:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                sock = context.wrap_socket(raw_sock, server_hostname=host)
                cert = sock.getpeercert(binary_form=False) or {}
                fp.tls = True
                fp.evidence["tls"] = {"version": sock.version(), "cipher": sock.cipher(), "cert": cert}
            except OSError as exc:
                fp.evidence["tls_error"] = str(exc)[:240]
                sock = raw_sock
        try:
            if port in HTTP_PORTS or fp.tls:
                service, headers, status = _read_http(sock, host, port)
                fp.service = service
                fp.http_headers = headers
                fp.http_status = status
                fp.product = headers.get("Server", "")
                fp.version = headers.get("X-Powered-By", "")
                fp.confidence = "medium" if status else "low"
            elif port == 6379:
                sock.sendall(b"PING\r\n")
                banner = sock.recv(256).decode("utf-8", errors="replace").strip()
                fp.service = "redis"
                fp.banner = banner
                fp.confidence = "high" if "PONG" in banner.upper() else "medium"
            elif port == 11211:
                sock.sendall(b"version\r\n")
                banner = sock.recv(256).decode("utf-8", errors="replace").strip()
                fp.service = "memcached"
                fp.banner = banner
                fp.confidence = "high" if banner.lower().startswith("version") else "medium"
            elif port in {21, 22, 25, 110, 143, 587}:
                # These protocols usually send a banner immediately or after a
                # harmless greeting; keep this read-only and short.
                try:
                    banner = sock.recv(512).decode("utf-8", errors="replace").strip()
                except OSError:
                    banner = ""
                fp.banner = banner
                fp.service = PROTOCOL_FAMILIES.get(port, "tcp")
                fp.confidence = "high" if banner else "medium"
            else:
                try:
                    banner = sock.recv(512).decode("utf-8", errors="replace").strip()
                except OSError:
                    banner = ""
                fp.banner = banner
                fp.service = DATABASE_PORTS.get(port) or MANAGEMENT_PORTS.get(port) or "tcp"
                fp.confidence = "medium" if fp.service != "tcp" or banner else "low"
        except OSError as exc:
            fp.evidence["probe_error"] = str(exc)[:240]
    return PortProfile(port=port, state="open", reason="connect", fingerprint=fp)


async def scan_ports(
    target: str,
    scope: str | list[str] | None,
    ports: str | list[int] | None = "common",
    *,
    timeout_seconds: float = 2.5,
    concurrency: int = 64,
    max_ports: int = 256,
) -> dict[str, Any]:
    host = normalize_host(target)
    if not host:
        return {"ok": False, "error": "target is required"}
    if not host_in_scope(host, scope):
        return {"ok": False, "error": f"target out of authorized scope: {host}", "scope": normalize_scope(scope)}
    selected_ports = parse_ports(ports, max_ports=max_ports)
    started = time.time()
    sem = asyncio.Semaphore(max(1, min(int(concurrency or 64), 256)))

    async def run_one(port: int) -> PortProfile:
        async with sem:
            return await asyncio.to_thread(_probe_port_sync, host, port, timeout_seconds)

    profiles = await asyncio.gather(*(run_one(port) for port in selected_ports))
    open_ports = [p for p in profiles if p.state == "open"]
    host_profile = HostProfile(
        host=host,
        resolved_ips=resolve_host(host),
        ports=sorted(open_ports, key=lambda p: p.port),
        duration_ms=int((time.time() - started) * 1000),
    )
    host_profile.findings = assess_exposure(host_profile)
    protocol_assessment = assess_protocols(host_profile)
    return {
        "ok": True,
        "target": host,
        "scope": normalize_scope(scope),
        "scanned_ports": len(selected_ports),
        "open_count": len(open_ports),
        "profile": host_profile.to_dict(),
        "protocol_assessment": protocol_assessment,
    }


def infer_os(profile: HostProfile | dict[str, Any]) -> list[dict[str, Any]]:
    data = profile.to_dict() if isinstance(profile, HostProfile) else profile
    ports = {int(p.get("port", 0)) for p in data.get("ports", [])}
    guesses: list[OsFingerprint] = []
    if {135, 139, 445} & ports or 3389 in ports:
        guesses.append(OsFingerprint("Windows", 55, "port-combination", {"ports": sorted(ports)}))
    if 22 in ports and ({111, 2049} & ports):
        guesses.append(OsFingerprint("Linux/Unix", 50, "port-combination", {"ports": sorted(ports)}))
    if 62078 in ports:
        guesses.append(OsFingerprint("iOS/macOS", 45, "port-combination", {"ports": sorted(ports)}))
    return [g.to_dict() for g in guesses]


def assess_exposure(profile: HostProfile | dict[str, Any]) -> list[dict[str, Any]]:
    data = profile.to_dict() if isinstance(profile, HostProfile) else profile
    host = data.get("host", "")
    findings: list[Finding] = []
    for port in data.get("ports", []):
        p = int(port.get("port", 0) or 0)
        fp = port.get("fingerprint") or {}
        service = fp.get("service") or DATABASE_PORTS.get(p) or MANAGEMENT_PORTS.get(p) or "tcp"
        evidence = {"host": host, "port": p, "service": service, "fingerprint": fp}
        if p == 6379 and "PONG" in str(fp.get("banner", "")).upper():
            findings.append(Finding(
                id="network.redis-unauthenticated",
                category="network exposure",
                severity=Severity.HIGH,
                title="Redis responded to unauthenticated PING",
                target=host,
                domain="network",
                kind="verification",
                confidence="confirmed",
                evidence=evidence,
                remediation="Bind Redis to trusted interfaces and require authentication/TLS.",
            ))
        elif p == 11211 and str(fp.get("banner", "")).lower().startswith("version"):
            findings.append(Finding(
                id="network.memcached-exposed",
                category="network exposure",
                severity=Severity.HIGH,
                title="Memcached version endpoint is reachable",
                target=host,
                domain="network",
                kind="verification",
                confidence="confirmed",
                evidence=evidence,
                remediation="Restrict Memcached to trusted networks and firewall the service.",
            ))
        elif p in DATABASE_PORTS:
            findings.append(Finding(
                id=f"network.{DATABASE_PORTS[p]}-exposed",
                category="network exposure",
                severity=Severity.MEDIUM,
                title=f"{DATABASE_PORTS[p]} service is reachable",
                target=host,
                domain="network",
                kind="observation",
                confidence="probable",
                evidence=evidence,
                remediation="Restrict database access to trusted hosts and require strong authentication.",
            ))
        elif p in {23, 2375}:
            findings.append(Finding(
                id=f"network.{MANAGEMENT_PORTS.get(p, 'management')}-cleartext",
                category="network exposure",
                severity=Severity.HIGH,
                title=f"High-risk management service exposed on port {p}",
                target=host,
                domain="network",
                kind="observation",
                confidence="probable",
                evidence=evidence,
                remediation="Disable the service or expose it only through authenticated private management networks.",
            ))
        elif p in MANAGEMENT_PORTS:
            findings.append(Finding(
                id=f"network.{MANAGEMENT_PORTS[p]}-management-exposed",
                category="network exposure",
                severity=Severity.LOW,
                title=f"Management service reachable on port {p}",
                target=host,
                domain="network",
                kind="observation",
                confidence="suspected",
                evidence=evidence,
                remediation="Confirm this management service is intended to be reachable from the tested network.",
            ))
        headers = {str(k).lower(): str(v) for k, v in (fp.get("http_headers") or {}).items()}
        if headers.get("server"):
            findings.append(Finding(
                id="network.http-server-banner",
                category="network exposure",
                severity=Severity.INFO,
                title="HTTP server banner disclosed",
                target=host,
                domain="network",
                kind="observation",
                confidence="confirmed",
                evidence={**evidence, "server": headers.get("server")},
                remediation="Minimize version disclosure where practical.",
            ))
        for vuln in _vuln_candidates_for_fingerprint(fp):
            findings.append(Finding(
                id=f"network.vuln-intel.{vuln.get('id', 'candidate').lower()}",
                category="vulnerability intelligence",
                severity=Severity(vuln.get("severity", "medium")),
                title=f"Potential vulnerability candidate: {vuln.get('id', 'unknown')}",
                target=host,
                domain="network",
                kind="intelligence",
                confidence=vuln.get("confidence", "suspected"),
                evidence={**evidence, "vulnerability": vuln},
                remediation=vuln.get("remediation", "Review vendor advisories and patch or mitigate if applicable."),
                references=vuln.get("references", []),
            ))
    return [f.to_dict() for f in findings]


def _vuln_candidates_for_fingerprint(fp: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from argusai.core.security.vuln_intel import match_service_vulns

        return match_service_vulns(fp).get("matches", [])
    except Exception:
        return []


def assess_protocols(profile: HostProfile | dict[str, Any]) -> dict[str, Any]:
    """Summarize protocol-specific risks from an already collected profile."""
    data = profile.to_dict() if isinstance(profile, HostProfile) else profile
    results: list[dict[str, Any]] = []
    for port in data.get("ports", []):
        p = int(port.get("port", 0) or 0)
        fp = port.get("fingerprint") or {}
        service = str(fp.get("service") or PROTOCOL_FAMILIES.get(p) or "").lower()
        family = PROTOCOL_FAMILIES.get(p, service or "tcp")
        risk = "info"
        notes: list[str] = []
        recommended: list[str] = []
        if family in {"telnet", "ftp", "smtp", "pop3", "imap"}:
            risk = "medium"
            notes.append("Cleartext protocol may expose credentials or sensitive data if used without TLS.")
            recommended.append("Prefer TLS-protected alternatives and restrict access by network policy.")
        if family in {"smb", "rdp", "ssh", "vnc", "docker-api", "kubernetes-api"}:
            risk = "medium" if family in {"ssh"} else "high"
            notes.append("Management or remote access protocol is reachable.")
            recommended.append("Confirm exposure is intended, require MFA/strong auth where applicable, and limit source networks.")
        if family in {"redis", "memcached", "mongodb", "mysql", "postgresql", "mssql", "elasticsearch"}:
            risk = "medium"
            notes.append("Data service is reachable from the tested network.")
            recommended.append("Require authentication/TLS and restrict to trusted application hosts.")
        if family in {"docker-api"} and p == 2375:
            risk = "critical"
            notes.append("Docker API over cleartext is a high-impact exposure if unauthenticated.")
            recommended.append("Disable TCP Docker API or require TLS client authentication.")
        if notes:
            results.append({
                "port": p,
                "service": service or family,
                "family": family,
                "risk": risk,
                "notes": notes,
                "recommended_actions": recommended,
                "evidence": {
                    "banner": fp.get("banner", ""),
                    "http_status": fp.get("http_status", ""),
                    "http_headers": fp.get("http_headers", {}),
                    "tls": bool(fp.get("tls")),
                },
            })
    return {"ok": True, "target": data.get("host", ""), "protocols": results, "count": len(results)}


def build_pentest_chain(
    target: str,
    scope: str | list[str] | None,
    objective: str = "authorized validation",
    rules_of_engagement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engagement = PentestEngagement.from_rules(target, scope, objective, rules_of_engagement)
    host = engagement.target
    if not host or not host_in_scope(host, engagement.scope):
        return {"ok": False, "error": "target must be inside explicit authorized scope", "scope": engagement.scope}
    return {
        "ok": True,
        "target": host,
        "scope": engagement.scope,
        "objective": engagement.objective,
        "requires_confirmation": True,
        "engagement": engagement.to_dict(),
        "steps": [
            {"id": "scope-confirm", "risk": "low", "action": "Confirm target and scope before network activity."},
            {"id": "network-profile", "risk": "medium", "tool": "network_profile_host", "action": "Build host profile from scoped port and service probes."},
            {"id": "exposure-assess", "risk": "medium", "tool": "network_assess_exposure", "action": "Map open services to non-destructive exposure findings."},
            {"id": "verify-finding", "risk": "high", "tool": "pentest_verify_finding", "action": "Verify one finding at a time with explicit approval."},
            {"id": "report", "risk": "low", "action": "Summarize evidence, impact, confidence, and remediation."},
        ],
    }


def verify_finding_minimal(
    finding: dict[str, Any],
    target: str,
    scope: str | list[str] | None,
    authorization_phrase: str = "",
) -> dict[str, Any]:
    host = normalize_host(target)
    if not host_in_scope(host, scope):
        return {"ok": False, "error": "target out of authorized scope", "scope": normalize_scope(scope)}
    if authorization_phrase.strip() != "AUTHORIZED":
        return {
            "ok": False,
            "requires_authorization_phrase": "AUTHORIZED",
            "error": "pentest verification requires explicit authorization phrase",
        }
    fid = str(finding.get("id") or "")
    evidence = finding.get("evidence") or {}
    return {
        "ok": True,
        "target": host,
        "finding_id": fid,
        "mode": "minimal-non-destructive-verification",
        "verified": bool(evidence),
        "confidence": "confirmed" if evidence else "hypothesis",
        "evidence": evidence,
        "next_step": "Collect additional proof only if the ROE permits it and the user confirms the exact action.",
        "not_performed": [
            "credential guessing",
            "payload exploitation",
            "data extraction",
            "persistence",
            "lateral movement",
        ],
    }
