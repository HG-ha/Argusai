from argusai.core.workspace.tool_adapters import adapter_inventory


def test_adapter_inventory_filters_mobile_apk_tools() -> None:
    result = adapter_inventory(asset_type="apk")
    names = {item["name"] for item in result["adapters"]}

    assert result["ok"] is True
    assert {"jadx", "apktool", "yara"}.issubset(names)


def test_adapter_inventory_exposes_high_risk_sqlmap_policy() -> None:
    result = adapter_inventory(domain="web")
    sqlmap = next(item for item in result["adapters"] if item["name"] == "sqlmap")

    assert sqlmap["risk"] == "high"
    assert sqlmap["requires_confirmation"] is True
    assert sqlmap["strict_scope"] is True


def test_adapter_inventory_marks_nuclei_ffuf_implemented() -> None:
    result = adapter_inventory(domain="web")
    by_name = {item["name"]: item for item in result["adapters"]}
    assert by_name["nuclei"]["planned"] is False
    assert by_name["ffuf"]["planned"] is False

