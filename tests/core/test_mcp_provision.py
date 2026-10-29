"""Security contracts for third-party MCP candidate admission."""

from __future__ import annotations

import base64

from argusai.core.security.mcp_provision import (
    build_provision_plan,
    build_revoke_plan,
    lint_mcp_launch,
    normalize_provenance,
    parse_candidate_metadata,
)


def _candidate() -> dict:
    integrity = "sha512-" + base64.b64encode(b"a" * 64).decode("ascii")
    return parse_candidate_metadata(
        {
            "full_name": "example/safe-mcp",
            "html_url": "https://github.com/example/safe-mcp",
            "archived": False,
            "disabled": False,
        },
        release={
            "tag_name": "v1.2.3",
            "html_url": "https://github.com/example/safe-mcp/releases/tag/v1.2.3",
            "draft": False,
            "prerelease": False,
        },
        license_metadata={
            "license": {
                "spdx_id": "Apache-2.0",
                "name": "Apache License 2.0",
            }
        },
        package_metadata={
            "name": "@example/safe-mcp",
            "version": "1.2.3",
            "dist": {
                "integrity": integrity,
                "tarball": "https://registry.npmjs.org/@example/safe-mcp/-/safe-mcp-1.2.3.tgz",
            },
        },
    )


def test_mcp_launch_rejects_shells_and_mutable_npx_packages() -> None:
    assert lint_mcp_launch("powershell", ["-Command", "echo ok"])["ok"] is False
    result = lint_mcp_launch("npx", ["-y", "@example/safe-mcp@latest"])
    codes = {item["code"] for item in result["errors"]}
    assert {"npx_auto_install_forbidden", "mutable_version_forbidden"} <= codes


def test_valid_candidate_builds_side_effect_free_integrity_plan() -> None:
    plan = build_provision_plan(
        _candidate(),
        command="npx",
        args=["@example/safe-mcp@1.2.3"],
    )
    assert plan["ok"] is True
    assert plan["version_pin"] == "1.2.3"
    assert plan["trust_anchor"]["kind"] == "npm_integrity"
    assert plan["downloads_performed"] == 0
    assert plan["commands_executed"] == 0
    assert plan["side_effects_performed"] == []


def test_provenance_drops_secrets_and_unrecognized_fields() -> None:
    clean = normalize_provenance({
        **_candidate()["provenance"],
        "token": "secret",
        "headers": {"Authorization": "Bearer secret"},
        "env": {"API_KEY": "secret"},
    })
    rendered = repr(clean)
    assert "secret" not in rendered
    assert "Authorization" not in rendered
    assert "API_KEY" not in rendered


def test_modelscope_is_retained_as_catalogue_provenance() -> None:
    clean = normalize_provenance(
        {
            "source": "modelscope",
            "source_url": "https://modelscope.cn/mcp/servers/%40example%2Fweather",
        }
    )

    assert clean["source"] == "modelscope"
    assert clean["source_url"].startswith("https://modelscope.cn/")


def test_revoke_plan_only_allows_cleanup_below_managed_root(tmp_path) -> None:
    root = tmp_path / "mcp"
    inside = root / "candidate"
    outside = tmp_path / "other"
    inside.mkdir(parents=True)
    outside.mkdir()
    server = {"id": "safe-mcp", "managed_staging_dir": str(inside)}
    assert build_revoke_plan(server, managed_root=str(root))["cleanup_allowed"] is True
    server["managed_staging_dir"] = str(outside)
    assert build_revoke_plan(server, managed_root=str(root))["cleanup_allowed"] is False
