"""api_spec_import: OpenAPI v2/v3 and Postman -> replayable API inventory."""

from __future__ import annotations

import json

from argusai.core.security.web_protocol_tools import api_spec_import


def _by_key(result):
    return {f"{e['method']} {e['url']}": e for e in result["endpoints"]}


def test_openapi_v3_builds_replayable_templates() -> None:
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/users/{id}": {
                "get": {
                    "operationId": "getUser",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                        {"name": "verbose", "in": "query", "required": True, "schema": {"type": "boolean"}},
                    ],
                    "security": [{"bearer": []}],
                }
            },
            "/orders": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "number", "example": 42.5}, "sku": {"type": "string"}},
                    }}}},
                }
            },
        },
    }
    result = api_spec_import(json.dumps(spec))
    assert result["ok"] and result["spec_kind"] == "openapi" and result["count"] == 2
    endpoints = _by_key(result)

    get_user = endpoints["GET https://api.example.com/v1/users/0?verbose=true"]
    assert get_user["request_template"]["method"] == "GET"
    assert get_user["secured"] is True
    assert {p["name"] for p in get_user["parameters"]} == {"id", "verbose"}

    create = endpoints["POST https://api.example.com/v1/orders"]
    tmpl = create["request_template"]
    assert tmpl["headers"]["Content-Type"] == "application/json"
    assert json.loads(tmpl["body"]) == {"amount": 42.5, "sku": "string"}


def test_openapi_v3_resolves_refs_without_infinite_recursion() -> None:
    spec = {
        "openapi": "3.0.0",
        "servers": [{"url": "https://api.example.com"}],
        "components": {"schemas": {"Node": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "child": {"$ref": "#/components/schemas/Node"}},
        }}},
        "paths": {"/tree": {"put": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Node"}
        }}}}}},
    }
    result = api_spec_import(json.dumps(spec))
    body = json.loads(result["endpoints"][0]["request_template"]["body"])
    assert body["name"] == "string"
    assert "child" in body  # cycle terminated, not expanded forever


def test_swagger_v2_uses_host_basepath_and_body_param() -> None:
    spec = {
        "swagger": "2.0",
        "host": "legacy.example.com",
        "basePath": "/api",
        "schemes": ["https"],
        "paths": {"/login": {"post": {"parameters": [
            {"name": "body", "in": "body", "schema": {"type": "object", "properties": {"user": {"type": "string"}}}},
        ]}}},
    }
    result = api_spec_import(json.dumps(spec))
    ep = result["endpoints"][0]
    assert ep["url"] == "https://legacy.example.com/api/login"
    assert json.loads(ep["request_template"]["body"]) == {"user": "string"}


def test_base_url_override_applies_to_relative_server() -> None:
    spec = {"openapi": "3.0.0", "paths": {"/ping": {"get": {}}}}
    result = api_spec_import(json.dumps(spec), base_url="https://override.example.com")
    assert result["endpoints"][0]["url"] == "https://override.example.com/ping"


def test_postman_collection_resolves_variables_and_bodies() -> None:
    collection = {
        "info": {"name": "demo", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/"},
        "variable": [{"key": "base", "value": "https://api.example.com"}],
        "item": [
            {"name": "folder", "item": [
                {"name": "login", "request": {
                    "method": "POST",
                    "url": "{{base}}/login",
                    "header": [{"key": "X-K", "value": "1"}, {"key": "Skip", "value": "x", "disabled": True}],
                    "body": {"mode": "raw", "raw": '{"u":"a"}'},
                }},
            ]},
            {"name": "search", "request": {
                "method": "GET",
                "url": {"raw": "https://api.example.com/search?q=1"},
            }},
        ],
    }
    result = api_spec_import(json.dumps(collection))
    assert result["ok"] and result["spec_kind"] == "postman" and result["count"] == 2
    endpoints = _by_key(result)
    login = endpoints["POST https://api.example.com/login"]
    assert login["request_template"]["headers"] == {"X-K": "1"}
    assert login["request_template"]["body"] == '{"u":"a"}'


def test_yaml_spec_is_accepted() -> None:
    spec = "openapi: 3.0.0\nservers: [{url: 'https://y.example.com'}]\npaths:\n  /ping: {get: {summary: ping}}\n"
    result = api_spec_import(spec)
    assert result["ok"] and result["endpoints"][0]["url"] == "https://y.example.com/ping"


def test_invalid_and_unknown_specs_are_rejected() -> None:
    assert api_spec_import("")["ok"] is False
    assert api_spec_import("not: [valid")["ok"] is False
    assert api_spec_import(json.dumps({"hello": "world"}))["ok"] is False


def test_endpoint_count_is_bounded() -> None:
    from argusai.core.security import web_protocol_tools as wpt

    paths = {f"/p{i}": {"get": {}} for i in range(wpt._SPEC_MAX_ENDPOINTS + 50)}
    spec = {"openapi": "3.0.0", "servers": [{"url": "https://x"}], "paths": paths}
    result = api_spec_import(json.dumps(spec))
    assert result["count"] == wpt._SPEC_MAX_ENDPOINTS
