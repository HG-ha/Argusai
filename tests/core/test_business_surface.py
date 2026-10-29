import json

from argusai.core.security.business_surface import business_surface_extract, business_test_plan
from argusai.core.security.web_protocol_tools import api_inventory_build


def test_business_surface_extracts_evidence_backed_candidates() -> None:
    inventory = [
        {"url": "https://shop.example/api/orders/1001", "method": "GET"},
        {"url": "https://shop.example/api/payment/checkout", "method": "POST", "body": {"amount": 99}},
        {"url": "https://shop.example/api/coupon/apply", "method": "POST"},
    ]

    result = business_surface_extract(items_json=json.dumps(inventory), source="test")

    assert result["ok"] is True
    assert result["evidence_refs"]
    assert result["observations"]
    assets = {asset["name"]: asset for asset in result["candidate_assets"]}
    risks = {risk["name"]: risk for risk in result["candidate_risks"]}
    assert "order" in assets
    assert "payment" in assets
    assert "amount_or_quantity" in risks
    assert assets["payment"]["needs_ai_review"] is True
    assert assets["payment"]["evidence_ids"]


def test_business_test_plan_returns_requirements_not_fixed_todos() -> None:
    surface = business_surface_extract(items_json=json.dumps([
        {"url": "https://saas.example/api/tenant/42/users", "method": "GET"},
        {"url": "https://saas.example/api/admin/export/users", "method": "POST"},
    ]))

    plan = business_test_plan(surface_json=json.dumps(surface), depth="standard")

    assert plan["ok"] is True
    assert plan["planner_role"] == "coverage_requirements_only"
    assert "todos" not in plan
    assert {item["id"] for item in plan["coverage_requirements"]} == {
        "generic_baseline",
        "business_specific",
        "system_network",
    }
    assert plan["evidence_gaps"]


def test_unknown_business_surface_is_not_forced_into_template() -> None:
    result = business_surface_extract(items_json=json.dumps([
        {"url": "https://app.example/api/v1/x7q/raw", "method": "GET"},
    ]), source="test")

    assert result["ok"] is True
    assert result["candidate_assets"] == []
    assert result["unknowns"]
    assert any("browser_trace" in item["tools"] for item in result["suggested_next_discovery"])


def test_api_inventory_adds_business_tags_compatibly() -> None:
    result = api_inventory_build(items_json=json.dumps([
        {"url": "https://app.example/api/workspace/1/invoice/export", "method": "POST"},
    ]), source="test")

    endpoint = result["endpoints"][0]
    assert result["ok"] is True
    assert "business_candidates" in endpoint
    candidate_assets = {item["name"] for item in endpoint["business_candidates"]["assets"]}
    assert candidate_assets & {"tenant", "export_report"}
    # Compatibility summaries remain present, but they are candidate summaries.
    assert endpoint["business_asset"] in {"tenant", "export_report"}
    assert endpoint["business_operation"] == "export"
    assert any(tag.startswith("candidate_asset:") for tag in endpoint["tags"])
    assert "business_summary" in result
    assert "candidate signals" in result["business_summary"]["note"]
