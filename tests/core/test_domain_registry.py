from __future__ import annotations

from argusai.core.workspace.assets import normalize_asset_ref
from argusai.core.workspace.domain_registry import DOMAIN_CAPABILITIES, domain_inventory


def test_full_domain_inventory_exposes_extended_security_domains() -> None:
    result = domain_inventory()
    assert result["ok"] is True
    assert {
        "web", "network", "mobile", "binary", "cloud", "directory",
        "supply_chain", "wireless", "iot", "hardware",
    } <= set(result["domains"])


def test_extended_domains_define_tools_workflows_and_boundaries() -> None:
    for name in ("cloud", "directory", "supply_chain", "wireless", "iot", "hardware"):
        capability = DOMAIN_CAPABILITIES[name]
        assert capability["target_types"], name
        assert capability["artifact_kinds"], name
        assert capability["tool_groups"] == [name]
        assert capability["implemented_tools"], name

    assert "password spray" in DOMAIN_CAPABILITIES["directory"]["forbidden_by_default"]
    assert "deauthentication" in DOMAIN_CAPABILITIES["wireless"]["forbidden_by_default"]
    assert "flash" in DOMAIN_CAPABILITIES["hardware"]["forbidden_by_default"]


def test_unknown_domain_returns_stable_error() -> None:
    result = domain_inventory("not-a-domain")
    assert result["ok"] is False
    assert "unknown domain" in result["error"]
    assert "cloud" in result["domains"]


def test_extended_asset_reference_types_are_preserved() -> None:
    assert normalize_asset_ref("cluster:dev-cluster").type == "cluster"
    assert normalize_asset_ref("sbom:art-123").type == "sbom"
    assert normalize_asset_ref("uart_capture:session.log").type == "uart_capture"
