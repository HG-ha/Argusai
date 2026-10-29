"""External tool adapter registry for assessment-plan execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolAdapterSpec:
    """Declarative adapter metadata for an external security tool."""

    name: str
    domain: str
    asset_types: list[str]
    risk: str = "medium"
    requires_confirmation: bool = True
    strict_scope: bool = True
    artifact_kinds: list[str] = field(default_factory=list)
    planned: bool = True
    notes: str = ""
    commands: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=lambda: ["windows", "linux", "darwin"])
    license_spdx: str = ""
    install_policy: str = "path_only"
    docs_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ADAPTERS: dict[str, ToolAdapterSpec] = {
    "nuclei": ToolAdapterSpec(
        name="nuclei",
        domain="web",
        asset_types=["url", "host", "api"],
        risk="medium",
        planned=False,
        artifact_kinds=["log", "finding"],
        notes="Template-based vulnerability scan; must enforce target scope and rate controls.",
    ),
    "sqlmap": ToolAdapterSpec(
        name="sqlmap",
        domain="web",
        asset_types=["url", "api"],
        risk="high",
        artifact_kinds=["request", "response", "log", "finding"],
        notes="High-risk active SQLi validation; keep in no-auto-approve policy.",
    ),
    "ffuf": ToolAdapterSpec(
        name="ffuf",
        domain="web",
        asset_types=["url", "host"],
        risk="medium",
        planned=False,
        artifact_kinds=["log"],
        notes="Content discovery; must respect rate limits and scope.",
    ),
    "jadx": ToolAdapterSpec(
        name="jadx",
        domain="mobile",
        asset_types=["apk", "aab"],
        risk="low",
        requires_confirmation=False,
        planned=False,
        artifact_kinds=["decompiled", "source", "log"],
    ),
    "apktool": ToolAdapterSpec(
        name="apktool",
        domain="mobile",
        asset_types=["apk", "aab"],
        risk="low",
        requires_confirmation=False,
        planned=False,
        artifact_kinds=["decompiled", "source", "log"],
    ),
    "capa": ToolAdapterSpec(
        name="capa",
        domain="binary",
        asset_types=["exe", "dll", "elf"],
        risk="low",
        requires_confirmation=False,
        planned=False,
        artifact_kinds=["log", "finding"],
    ),
    "yara": ToolAdapterSpec(
        name="yara",
        domain="binary",
        asset_types=["exe", "dll", "elf", "apk", "ipa", "firmware"],
        risk="low",
        requires_confirmation=False,
        planned=False,
        artifact_kinds=["log", "finding"],
    ),
    "objection": ToolAdapterSpec(
        name="objection",
        domain="mobile",
        asset_types=["apk", "package", "device"],
        risk="low",
        requires_confirmation=False,
        planned=False,
        artifact_kinds=["trace", "log"],
        notes="One-shot objection CLI against a package; requires frida-server on device.",
    ),
    "tshark": ToolAdapterSpec(
        name="tshark",
        domain="network",
        asset_types=["pcap", "wifi_pcap"],
        risk="low",
        requires_confirmation=False,
        strict_scope=False,
        planned=False,
        commands=["tshark"],
        artifact_kinds=["pcap", "network", "har"],
        license_spdx="GPL-2.0-or-later",
        install_policy="managed_or_path",
        docs_url="https://www.wireshark.org/docs/man-pages/tshark.html",
        notes="Offline packet analysis. ArgusAI never installs Npcap or starts live capture automatically.",
    ),
    "trivy": ToolAdapterSpec(
        name="trivy",
        domain="supply_chain",
        asset_types=["repository", "sbom", "container_image", "iac_repo"],
        risk="medium",
        planned=False,
        commands=["trivy"],
        artifact_kinds=["sbom", "vuln_report", "config", "finding"],
        license_spdx="Apache-2.0",
        install_policy="managed_or_path",
        docs_url="https://trivy.dev/",
    ),
    "syft": ToolAdapterSpec(
        name="syft",
        domain="supply_chain",
        asset_types=["repository", "container_image"],
        risk="low",
        planned=False,
        commands=["syft"],
        artifact_kinds=["sbom", "log"],
        license_spdx="Apache-2.0",
        install_policy="managed_or_path",
        docs_url="https://github.com/anchore/syft",
    ),
    "osv-scanner": ToolAdapterSpec(
        name="osv-scanner",
        domain="supply_chain",
        asset_types=["repository", "sbom"],
        risk="medium",
        planned=False,
        commands=["osv-scanner"],
        artifact_kinds=["vuln_report", "finding"],
        license_spdx="Apache-2.0",
        install_policy="managed_or_path",
        docs_url="https://github.com/google/osv-scanner",
    ),
    "checkov": ToolAdapterSpec(
        name="checkov",
        domain="cloud",
        asset_types=["iac_repo", "repository"],
        risk="medium",
        planned=False,
        commands=["checkov"],
        artifact_kinds=["config", "manifest", "policy", "finding"],
        license_spdx="Apache-2.0",
        install_policy="managed_python_or_path",
        docs_url="https://www.checkov.io/",
    ),
    "prowler": ToolAdapterSpec(
        name="prowler",
        domain="cloud",
        asset_types=["cloud_account", "cluster"],
        risk="high",
        planned=False,
        commands=["prowler"],
        artifact_kinds=["config", "policy", "finding", "log"],
        license_spdx="Apache-2.0",
        install_policy="managed_python_or_path",
        docs_url="https://docs.prowler.com/",
        notes="Requires explicit account scope and user-provided least-privilege credentials.",
    ),
    "kubectl": ToolAdapterSpec(
        name="kubectl",
        domain="cloud",
        asset_types=["cluster", "namespace"],
        risk="high",
        planned=False,
        commands=["kubectl"],
        artifact_kinds=["config", "manifest", "policy", "log"],
        license_spdx="Apache-2.0",
        install_policy="path_only",
        docs_url="https://kubernetes.io/docs/reference/kubectl/",
        notes="Only read-only get/api-resources/auth can-i commands are allowed.",
    ),
    "appium": ToolAdapterSpec(
        name="appium",
        domain="mobile",
        asset_types=["device", "package", "apk"],
        risk="high",
        planned=False,
        commands=["appium"],
        artifact_kinds=["trace", "har", "screenshot", "log"],
        license_spdx="Apache-2.0",
        install_policy="managed_node_or_path",
        docs_url="https://appium.io/",
    ),
    "certipy": ToolAdapterSpec(
        name="certipy",
        domain="directory",
        asset_types=["ad_domain", "dc"],
        risk="high",
        planned=False,
        commands=["certipy"],
        artifact_kinds=["ldap", "graph", "finding", "log"],
        license_spdx="MIT",
        install_policy="managed_python_or_path",
        docs_url="https://github.com/ly4k/Certipy",
        notes="Only the read-oriented find command is allowed.",
    ),
    "binwalk": ToolAdapterSpec(
        name="binwalk",
        domain="iot",
        asset_types=["firmware"],
        risk="medium",
        planned=False,
        commands=["binwalk"],
        platforms=["linux"],
        artifact_kinds=["firmware", "filesystem", "log"],
        license_spdx="MIT",
        install_policy="wsl_or_container_or_path",
        docs_url="https://github.com/ReFirmLabs/binwalk",
    ),
    "aircrack-ng": ToolAdapterSpec(
        name="aircrack-ng",
        domain="wireless",
        asset_types=["wifi_pcap"],
        risk="high",
        planned=False,
        commands=["aircrack-ng"],
        platforms=["linux", "darwin"],
        artifact_kinds=["pcap", "wireless", "log"],
        license_spdx="GPL-2.0-or-later",
        install_policy="path_only",
        docs_url="https://www.aircrack-ng.org/",
        notes="Offline metadata only; cracking, injection and deauthentication are not exposed.",
    ),
    "chipsec": ToolAdapterSpec(
        name="chipsec",
        domain="hardware",
        asset_types=["board", "flash_dump"],
        risk="high",
        planned=False,
        commands=["chipsec_main"],
        platforms=["windows", "linux"],
        artifact_kinds=["flash", "log", "finding"],
        license_spdx="GPL-2.0-only",
        install_policy="path_only",
        docs_url="https://github.com/chipsec/chipsec",
        notes="Manual installation and explicit elevation only; no write-capable modules.",
    ),
    "openocd": ToolAdapterSpec(
        name="openocd",
        domain="hardware",
        asset_types=["board"],
        risk="high",
        planned=False,
        commands=["openocd"],
        artifact_kinds=["log", "flash"],
        license_spdx="GPL-2.0-or-later",
        install_policy="path_only",
        docs_url="https://openocd.org/",
        notes="Identification and read-only commands only; program, erase and flash are forbidden.",
    ),
    "sigrok-cli": ToolAdapterSpec(
        name="sigrok-cli",
        domain="hardware",
        asset_types=["uart_capture", "board"],
        risk="medium",
        planned=False,
        commands=["sigrok-cli"],
        artifact_kinds=["uart", "log"],
        license_spdx="GPL-3.0-or-later",
        install_policy="path_only",
        docs_url="https://sigrok.org/wiki/Sigrok-cli",
    ),
}


def adapter_inventory(domain: str = "", asset_type: str = "") -> dict[str, Any]:
    """List adapter specs filtered by domain and/or asset type."""
    domain_filter = str(domain or "").strip().lower()
    asset_filter = str(asset_type or "").strip().lower()
    items = []
    for spec in _ADAPTERS.values():
        if domain_filter and spec.domain != domain_filter:
            continue
        if asset_filter and asset_filter not in spec.asset_types:
            continue
        items.append(spec.to_dict())
    return {"ok": True, "count": len(items), "adapters": items}

