"""Official MCP Registry normalization and safe-import contracts."""

from __future__ import annotations

from argusai.core.infra.mcp_registry import (
    get_modelscope_marketplace_entry,
    get_registry_marketplace_entry,
    normalize_modelscope_entry,
    normalize_registry_entry,
    prepare_marketplace_config,
    search_modelscope_marketplace,
    search_mcp_registry,
)
from argusai.core.infra import mcp_registry
from argusai.core.tools.case_mcp import (
    mcp_marketplace_get,
    mcp_marketplace_search,
)


def _entry(**server_overrides):
    server = {
        "name": "io.example/weather",
        "title": "Weather",
        "description": "Read weather forecasts.",
        "version": "1.2.3",
        "repository": {
            "url": "https://github.com/example/weather",
            "source": "github",
        },
        **server_overrides,
    }
    return {
        "server": server,
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        },
    }


def test_secure_remote_builds_disabled_high_risk_import() -> None:
    row = normalize_registry_entry(
        _entry(
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://mcp.example.com/mcp?token=must-not-survive",
                    "headers": [
                        {
                            "name": "Authorization",
                            "isRequired": True,
                            "isSecret": True,
                        }
                    ],
                }
            ]
        )
    )

    assert row is not None
    assert row["remotes"][0]["url"] == "https://mcp.example.com/mcp"
    config = row["config"]
    assert config["enabled"] is False
    assert config["transport"] == "streamable_http"
    assert config["headers"] == {}
    assert config["policy"]["default_risk"] == "high"
    assert config["policy"]["require_confirmation_for_all"] is True
    assert config["provenance"]["source"] == "mcp_registry"
    assert "must-not-survive" not in repr(row)


def test_exact_npm_package_builds_disabled_review_config() -> None:
    row = normalize_registry_entry(
        _entry(
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "@example/weather-mcp",
                    "version": "1.2.3",
                    "runtimeHint": "npx",
                    "transport": {"type": "stdio"},
                    "environmentVariables": [
                        {"name": "API_KEY", "isRequired": True, "isSecret": True}
                    ],
                }
            ]
        )
    )

    assert row is not None
    assert row["config"]["command"] == "npx"
    assert row["config"]["args"] == ["@example/weather-mcp@1.2.3"]
    assert row["config"]["env"] == {}
    assert "API_KEY" in row["config"]["notes"]


def test_mutable_npm_and_insecure_remote_are_not_auto_importable() -> None:
    row = normalize_registry_entry(
        _entry(
            remotes=[{"type": "sse", "url": "http://127.0.0.1:3000/sse"}],
            packages=[
                {
                    "registryType": "npm",
                    "identifier": "@example/weather-mcp",
                    "version": "latest",
                    "transport": {"type": "stdio"},
                }
            ],
        )
    )

    assert row is not None
    assert row["config"] is None


def test_search_uses_latest_filter_and_normalizes_cursor() -> None:
    seen: list[str] = []

    def requester(url: str):
        seen.append(url)
        return {
            "servers": [_entry(remotes=[{"type": "sse", "url": "https://x.test/sse"}])],
            "metadata": {"nextCursor": "io.example/weather:1.2.3"},
        }

    result = search_mcp_registry(
        "weather api",
        "previous:1.0.0",
        500,
        requester=requester,
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["next_cursor"] == "io.example/weather:1.2.3"
    assert "version=latest" in seen[0]
    assert "search=weather+api" in seen[0]
    assert "cursor=previous%3A1.0.0" in seen[0]
    assert "limit=50" in seen[0]


def test_official_detail_requires_an_exact_registry_id() -> None:
    result = get_registry_marketplace_entry(
        "io.example/weather",
        requester=lambda _url: {
            "servers": [
                _entry(),
                _entry(name="io.example/weather-plus"),
            ],
            "metadata": {},
        },
    )

    assert result["ok"] is True
    assert result["source"] == "official"
    assert result["server"]["name"] == "io.example/weather"


def test_default_request_falls_back_to_verified_system_curl(monkeypatch) -> None:
    payload = b'{"servers":[],"metadata":{"count":0}}'
    seen: list[list[str]] = []

    monkeypatch.setattr(
        mcp_registry,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("TLS chain failed")),
    )
    monkeypatch.setattr(mcp_registry.shutil, "which", lambda _name: "curl.exe")

    class _Completed:
        returncode = 0
        stdout = payload
        stderr = b""

    def fake_run(args, **_kwargs):
        seen.append(list(args))
        return _Completed()

    monkeypatch.setattr(mcp_registry.subprocess, "run", fake_run)
    out = mcp_registry._default_request(
        "https://registry.modelcontextprotocol.io/v0.1/servers?limit=1"
    )

    assert out["metadata"]["count"] == 0
    assert seen
    assert "--proto" in seen[0]
    assert "=https" in seen[0]
    assert "--max-filesize" in seen[0]


def _modelscope_entry(**overrides):
    return {
        "id": "@example/weather",
        "name": "Weather",
        "chinese_name": "天气查询",
        "description": "查询天气。",
        "publisher": "@example/weather",
        "categories": ["location-services"],
        "tags": ["天气"],
        "view_count": 12345,
        "locales": {
            "zh": {"name": "天气查询", "description": "查询中国城市天气。"},
            "en": {"name": "Weather", "description": "Read weather."},
        },
        **overrides,
    }


def test_modelscope_summary_prefers_chinese_and_keeps_discovery_metadata() -> None:
    row = normalize_modelscope_entry(_modelscope_entry())

    assert row is not None
    assert row["catalog_source"] == "modelscope"
    assert row["name"] == "@example/weather"
    assert row["title"] == "天气查询"
    assert row["description"] == "查询中国城市天气。"
    assert row["english_title"] == "Weather"
    assert row["categories"] == ["location-services"]
    assert row["view_count"] == 12345
    assert row["config"] is None


def test_modelscope_detail_builds_disabled_high_risk_config() -> None:
    row = normalize_modelscope_entry(
        _modelscope_entry(
            is_verified=True,
            source_url="https://github.com/example/weather?token=drop-me",
            env_schema={"required": ["WEATHER_API_KEY"]},
            server_config=[
                {
                    "mcpServers": {
                        "weather": {
                            "command": "uvx",
                            "args": ["weather-mcp"],
                            "env": {"WEATHER_API_KEY": "must-not-survive"},
                        }
                    }
                }
            ],
        )
    )

    assert row is not None
    config = row["config"]
    assert config["enabled"] is False
    assert config["command"] == "uvx"
    assert config["args"] == ["weather-mcp"]
    assert config["env"] == {}
    assert config["policy"]["default_risk"] == "high"
    assert config["policy"]["require_confirmation_for_all"] is True
    assert config["provenance"]["source"] == "modelscope"
    assert "WEATHER_API_KEY" in config["notes"]
    assert "must-not-survive" not in repr(row)


def test_modelscope_search_sends_filters_and_uses_bounded_page_cursor() -> None:
    seen: list[tuple[str, dict]] = []

    def requester(url: str, body: dict):
        seen.append((url, body))
        return {
            "success": True,
            "data": {
                "mcp_server_list": [
                    _modelscope_entry(id=f"@example/weather-{index}")
                    for index in range(20)
                ],
                "total_count": 87,
            },
        }

    result = search_modelscope_marketplace(
        "天气",
        "2",
        20,
        "location-services",
        True,
        requester=requester,
    )

    assert result["ok"] is True
    assert result["source"] == "modelscope"
    assert result["total"] == 87
    assert result["next_cursor"] == "3"
    assert seen[0][0].endswith("/mcp/servers")
    assert seen[0][1] == {
        "page_number": 2,
        "page_size": 20,
        "search": "天气",
        "filter": {"category": "location-services", "is_hosted": True},
    }


def test_modelscope_detail_is_fetched_on_demand_with_encoded_id() -> None:
    seen: list[str] = []

    def requester(url: str):
        seen.append(url)
        return {
            "success": True,
            "data": _modelscope_entry(
                server_config=[
                    {
                        "mcpServers": {
                            "weather": {"command": "npx", "args": ["weather-mcp"]}
                        }
                    }
                ]
            ),
        }

    result = get_modelscope_marketplace_entry(
        "@example/weather",
        requester=requester,
        npm_requester=lambda _url: {
            "name": "weather-mcp",
            "version": "1.2.3",
            "dist": {"integrity": "sha512-YWJjZA=="},
        },
    )

    assert result["ok"] is True
    assert result["server"]["config"]["command"] == "npx"
    assert result["server"]["config"]["args"] == ["weather-mcp@1.2.3"]
    assert seen[0].endswith("/%40example%2Fweather")


def test_modelscope_npx_config_is_safely_pinned_before_import() -> None:
    seen: list[str] = []

    def requester(url: str):
        seen.append(url)
        return {
            "name": "bazi-mcp",
            "version": "0.1.0",
            "dist": {"integrity": "sha512-YWJjZA=="},
            "license": "MIT",
        }

    prepared = prepare_marketplace_config(
        {
            "command": "npx",
            "args": ["-y", "bazi-mcp"],
            "provenance": {
                "source": "modelscope",
                "source_url": "https://modelscope.cn/mcp/servers/bazi-mcp",
            },
        },
        requester=requester,
    )

    assert prepared["args"] == ["bazi-mcp@0.1.0"]
    assert prepared["provenance"]["version"] == "0.1.0"
    assert prepared["provenance"]["package"]["name"] == "bazi-mcp"
    assert seen == ["https://registry.npmjs.org/bazi-mcp/latest"]


def test_exact_modelscope_npx_config_needs_no_registry_lookup() -> None:
    prepared = prepare_marketplace_config(
        {
            "command": "npx",
            "args": ["--yes", "bazi-mcp@0.1.0"],
            "provenance": {"source": "modelscope"},
        },
        requester=lambda _url: (_ for _ in ()).throw(
            AssertionError("registry lookup not expected")
        ),
    )

    assert prepared["args"] == ["bazi-mcp@0.1.0"]


def test_agent_marketplace_search_is_bounded_and_read_only(monkeypatch) -> None:
    seen: list[tuple] = []

    def fake_search(*args):
        seen.append(args)
        return {
            "ok": True,
            "source": "modelscope",
            "source_label": "魔搭 ModelScope",
            "count": 1,
            "total": 1,
            "next_cursor": "",
            "servers": [normalize_modelscope_entry(_modelscope_entry())],
        }

    monkeypatch.setattr(mcp_registry, "search_mcp_marketplace", fake_search)
    result = mcp_marketplace_search(
        None,
        query="天气",
        source="modelscope",
        limit=500,
        hosted="local",
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["servers"][0]["server_id"] == "@example/weather"
    assert seen == [("modelscope", "天气", "", 20, "", False)]


def test_agent_marketplace_detail_redacts_secret_values(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_registry,
        "get_mcp_marketplace_entry",
        lambda _source, _server_id: {
            "ok": True,
            "source": "modelscope",
            "server": {
                **_modelscope_entry(),
                "config": {
                    "id": "weather",
                    "name": "Weather",
                    "enabled": False,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["weather-mcp@1.2.3"],
                    "env": {"WEATHER_API_KEY": "must-not-survive"},
                    "headers": {"Authorization": "must-not-survive"},
                    "policy": {"default_risk": "high"},
                    "provenance": {"source": "modelscope"},
                },
            },
        },
    )

    result = mcp_marketplace_get(None, "@example/weather", "modelscope")
    preview = result["server"]["config_preview"]

    assert result["read_only"] is True
    assert preview["env_keys"] == ["WEATHER_API_KEY"]
    assert preview["header_keys"] == ["Authorization"]
    assert "must-not-survive" not in repr(result)
