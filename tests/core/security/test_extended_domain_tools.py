"""Contracts for cloud, mobile DAST, and directory safety wrappers."""

from __future__ import annotations

import json
import zipfile

from argusai.core.assistant.streaming import _CONFIRM_TOOLS, _NO_AUTO_APPROVE_TOOLS
from argusai.core.assistant.tools import _tool_risk
from argusai.core.security.ad_tools import certipy_find_plan, parse_sharphound_report
from argusai.core.security.cloud_tools import kubectl_readonly_plan, trivy_plan_scan
from argusai.core.security.mobile_dast_tools import mobile_dast_plan
from argusai.core.tools import EXECUTORS


def test_cloud_plans_are_bounded_and_mutation_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.security.cloud_tools.shutil.which",
        lambda name: f"/tools/{name}",
    )
    plan = trivy_plan_scan("registry.example/app:1.2.3", offline=True)
    assert plan["ok"] is True
    assert "--skip-db-update" in plan["args"]
    assert plan["args"][-1] == "registry.example/app:1.2.3"

    blocked = kubectl_readonly_plan("delete", resource="pods")
    assert blocked["blocked"] is True
    assert blocked["reason"] == "unsafe_operation"
    blocked_secret = kubectl_readonly_plan("get", resource="secrets")
    assert blocked_secret["blocked"] is True


def test_mobile_plan_rejects_remote_and_destructive_capabilities(tmp_path) -> None:
    package = tmp_path / "sample.apk"
    package.write_bytes(b"APK")
    remote = mobile_dast_plan(
        "android",
        app_path=package,
        server_url="http://appium.example.test:4723",
    )
    assert remote["blocked"] is True
    assert remote["reason"] == "unsafe_operation"

    destructive = mobile_dast_plan(
        "android",
        app_path=package,
        capabilities={"appium:fullReset": True},
    )
    assert destructive["blocked"] is True
    assert destructive["reason"] == "unsafe_operation"


def test_directory_plan_never_accepts_password_arguments(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.security.ad_tools.shutil.which",
        lambda name: "/tools/certipy" if name == "certipy" else None,
    )
    plan = certipy_find_plan("example.test", dc_host="192.0.2.10", username="auditor")
    assert plan["ok"] is True
    assert "-k" in plan["args"]
    assert "-no-pass" in plan["args"]
    assert not any("password" in item.lower() for item in plan["args"])


def test_sharphound_import_returns_only_aggregate_identity_data(tmp_path) -> None:
    archive_path = tmp_path / "collection.zip"
    payload = {
        "meta": {"type": "users"},
        "data": [
            {
                "Properties": {"name": "sensitive-user@example.test"},
                "Aces": [{"RightName": "GenericAll"}],
            }
        ],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("20260731_users.json", json.dumps(payload))

    result = parse_sharphound_report(str(archive_path))
    assert result["ok"] is True
    assert result["object_type_counts"] == {"users": 1}
    assert result["relationship_counts"] == {"aces": 1}
    assert result["identities_redacted"] is True
    assert "sensitive-user" not in json.dumps(result)


def test_extended_executors_and_confirmation_contracts_are_registered() -> None:
    expected = {
        "cloud_tool_inventory",
        "trivy_cloud_scan",
        "kubectl_readonly_run",
        "mobile_dast_plan",
        "appium_status",
        "ad_sharphound_import",
        "ad_certipy_find_run",
        "mcp_github_candidate",
        "mcp_provision_plan",
        "external_tool_inventory",
    }
    assert expected <= EXECUTORS.keys()
    assert {
        "trivy_cloud_scan",
        "kubectl_readonly_run",
        "appium_status",
        "ad_certipy_find_run",
    } <= _CONFIRM_TOOLS
    assert {"kubectl_readonly_run", "ad_certipy_find_run"} <= _NO_AUTO_APPROVE_TOOLS
    assert _tool_risk("kubectl_readonly_run") == "high"
    assert _tool_risk("ad_certipy_find_run") == "high"
