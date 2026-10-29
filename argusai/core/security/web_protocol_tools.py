"""Web API protocol helpers for AI-driven workflows.

The tools here are intentionally conservative: they focus on inventory,
read-only GraphQL probes, WebSocket handshake validation, and gRPC-Web /
protobuf surface analysis. Deeper fuzzing should be driven by the assistant
with explicit scope and evidence handling.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from dataclasses import dataclass, asdict, field
from typing import Any
from urllib.parse import urlparse

import httpx

from argusai.core.security.business_surface import classify_endpoint
from argusai.core.infra.command import in_scope, normalize_scope


INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types { name kind }
  }
}
""".strip()


@dataclass
class ApiEndpoint:
    url: str
    method: str = "GET"
    source: str = ""
    protocol: str = "http"
    content_type: str = ""
    status_code: int | None = None
    tags: list[str] = field(default_factory=list)
    business_candidates: dict[str, Any] = field(default_factory=dict)
    # Compatibility fields. These are candidate summaries, not confirmed facts.
    business_asset: str = ""
    business_operation: str = ""
    business_risks: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.method.upper()} {self.url}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["method"] = self.method.upper()
        return data


def api_inventory_build(
    *,
    items: Any = None,
    items_json: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Build a normalized API inventory from traces, HAR-like data or lists."""
    raw = _coerce_items(items, items_json)
    endpoints: dict[str, ApiEndpoint] = {}

    def add(url: str, method: str = "GET", item_source: str = "", **extra: Any) -> None:
        if not url:
            return
        business = classify_endpoint(str(url), str(method or "GET"), body=extra.get("body"), text=extra.get("text", ""))
        tags = sorted(set(list(extra.get("tags") or []) + list(business.get("tags") or [])))
        ep = ApiEndpoint(
            url=str(url),
            method=str(method or "GET").upper(),
            source=item_source or source,
            protocol=_guess_protocol(str(url), extra),
            content_type=str(extra.get("content_type") or ""),
            status_code=extra.get("status_code"),
            tags=tags,
            business_candidates=dict(business.get("business_candidates") or {}),
            business_asset=str(business.get("asset") or ""),
            business_operation=str(business.get("operation") or ""),
            business_risks=[str(r) for r in business.get("risks") or []],
            evidence=dict(extra.get("evidence") or {}),
        )
        key = ep.key()
        if key in endpoints:
            old = endpoints[key]
            old.tags = sorted(set(old.tags + ep.tags))
            old.business_candidates = _merge_business_candidates(old.business_candidates, ep.business_candidates)
            old.evidence.update({k: v for k, v in ep.evidence.items() if v not in ("", None, [], {})})
            if ep.status_code is not None:
                old.status_code = ep.status_code
            if ep.content_type:
                old.content_type = ep.content_type
            if ep.source and ep.source not in old.source:
                old.source = ",".join(x for x in [old.source, ep.source] if x)
            return
        endpoints[key] = ep

    for obj in raw:
        if isinstance(obj, str):
            add(obj, item_source=source or "text")
            continue
        if not isinstance(obj, dict):
            continue
        # Browser trace / proxy flow / HAR / custom normalized shapes.
        url = (
            obj.get("url") or obj.get("request", {}).get("url") or
            obj.get("response", {}).get("url") or obj.get("href") or
            obj.get("endpoint") or ""
        )
        method = obj.get("method") or obj.get("request", {}).get("method") or "GET"
        headers = obj.get("headers") or obj.get("response_headers") or obj.get("request", {}).get("headers") or {}
        content_type = _header_value(headers, "content-type")
        status = obj.get("status") or obj.get("status_code") or obj.get("response", {}).get("status_code")
        body = obj.get("body") or obj.get("postData") or obj.get("request", {}).get("body") or obj.get("request", {}).get("postData") or ""
        text = " ".join(str(obj.get(k) or "") for k in ("title", "label", "name", "text", "description"))
        tags = []
        if _looks_graphql(url, content_type, obj):
            tags.append("graphql")
        if _looks_grpc_web(content_type, obj):
            tags.append("grpc-web")
        if str(url).startswith(("ws://", "wss://")):
            tags.append("websocket")
        add(
            str(url),
            str(method),
            item_source=str(obj.get("source") or source or obj.get("type") or ""),
            content_type=content_type,
            status_code=_safe_int_or_none(status),
            tags=tags,
            body=body,
            text=text,
            evidence={"raw_type": obj.get("type") or obj.get("kind") or ""},
        )

    grouped: dict[str, int] = {}
    for ep in endpoints.values():
        grouped[ep.protocol] = grouped.get(ep.protocol, 0) + 1
        for tag in ep.tags:
            grouped[tag] = grouped.get(tag, 0) + 1
    out = [ep.to_dict() for ep in sorted(endpoints.values(), key=lambda e: e.key())]
    return {
        "ok": True,
        "count": len(out),
        "by_protocol_or_tag": grouped,
        "endpoints": out,
        "business_summary": _business_summary(out),
        "recommended_next": _inventory_next_actions(out),
    }


def api_inventory_diff(old_json: str = "{}", new_json: str = "{}") -> dict[str, Any]:
    old = _inventory_set(old_json)
    new = _inventory_set(new_json)
    added = sorted(new - old)
    removed = sorted(old - new)
    unchanged = sorted(old & new)
    return {"ok": True, "added": added, "removed": removed, "unchanged_count": len(unchanged)}


def api_request_select(inventory_json: str = "{}", protocol: str = "", tag: str = "", method: str = "", contains: str = "", limit: int = 20) -> dict[str, Any]:
    try:
        inv = json.loads(inventory_json or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"invalid inventory_json: {exc}"}
    endpoints = inv.get("endpoints") if isinstance(inv, dict) else inv
    if not isinstance(endpoints, list):
        return {"ok": False, "error": "inventory_json must contain endpoints list"}
    selected = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        if protocol and str(ep.get("protocol") or "").lower() != protocol.lower():
            continue
        if tag and tag not in (ep.get("tags") or []):
            continue
        if method and str(ep.get("method") or "").upper() != method.upper():
            continue
        if contains and contains not in str(ep.get("url") or ""):
            continue
        selected.append(ep)
        if len(selected) >= max(1, min(int(limit or 20), 100)):
            break
    return {"ok": True, "count": len(selected), "endpoints": selected}


_SPEC_MAX_ENDPOINTS = 800
_SPEC_SCHEMA_MAX_DEPTH = 5
_SPEC_SCHEMA_MAX_PROPS = 40
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


def _spec_typed_placeholder(schema: dict[str, Any]) -> Any:
    for key in ("example", "default"):
        if key in schema and schema[key] not in (None, ""):
            return schema[key]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = str(schema.get("type") or "").lower()
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    fmt = str(schema.get("format") or "").lower()
    if fmt in ("date-time", "date"):
        return "2024-01-01T00:00:00Z"
    return "string"


def _spec_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _spec_resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _spec_example_from_schema(root: dict[str, Any], schema: Any, *, depth: int = 0, seen: set[str] | None = None) -> Any:
    if not isinstance(schema, dict) or depth > _SPEC_SCHEMA_MAX_DEPTH:
        return {}
    seen = seen or set()
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {}
        seen = seen | {ref}
        return _spec_example_from_schema(root, _spec_resolve_ref(root, ref), depth=depth, seen=seen)
    for combiner in ("allOf", "oneOf", "anyOf"):
        parts = schema.get(combiner)
        if isinstance(parts, list) and parts:
            if combiner == "allOf":
                merged: dict[str, Any] = {}
                for part in parts:
                    value = _spec_example_from_schema(root, part, depth=depth, seen=seen)
                    if isinstance(value, dict):
                        merged.update(value)
                return merged or _spec_typed_placeholder(schema)
            return _spec_example_from_schema(root, parts[0], depth=depth, seen=seen)
    if "example" in schema and schema["example"] not in (None, ""):
        return schema["example"]
    kind = str(schema.get("type") or "").lower()
    if kind == "object" or "properties" in schema:
        out: dict[str, Any] = {}
        props = schema.get("properties")
        if isinstance(props, dict):
            for name, sub in list(props.items())[:_SPEC_SCHEMA_MAX_PROPS]:
                out[str(name)] = _spec_example_from_schema(root, sub, depth=depth + 1, seen=seen)
        return out
    if kind == "array":
        return [_spec_example_from_schema(root, schema.get("items") or {}, depth=depth + 1, seen=seen)]
    return _spec_typed_placeholder(schema)


def _spec_openapi_base(spec: dict[str, Any], override: str) -> str:
    if override:
        return override.rstrip("/")
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        url = str(servers[0].get("url") or "").rstrip("/")
        variables = servers[0].get("variables")
        if isinstance(variables, dict):
            for name, var in variables.items():
                if isinstance(var, dict) and var.get("default") is not None:
                    url = url.replace("{" + str(name) + "}", str(var["default"]))
        if url.startswith(("http://", "https://")):
            return url
        if url:
            return url  # relative server; the Agent supplies base_url if it matters
    scheme = "https"
    schemes = spec.get("schemes")
    if isinstance(schemes, list) and schemes:
        scheme = str(schemes[0])
    host = str(spec.get("host") or "").rstrip("/")
    base_path = str(spec.get("basePath") or "").rstrip("/")
    return f"{scheme}://{host}{base_path}" if host else base_path


def _spec_operation_request(
    root: dict[str, Any],
    base: str,
    path: str,
    method: str,
    operation: dict[str, Any],
    shared_params: list[Any],
) -> dict[str, Any]:
    from urllib.parse import urlencode

    url_path = path
    query: dict[str, str] = {}
    headers: dict[str, str] = {}
    parameters: list[dict[str, Any]] = []
    all_params = list(shared_params) + list(operation.get("parameters") or [])
    for raw_param in all_params:
        param = raw_param
        if isinstance(param, dict) and isinstance(param.get("$ref"), str):
            param = _spec_resolve_ref(root, param["$ref"])
        if not isinstance(param, dict) or not param.get("name"):
            continue
        name = str(param["name"])
        where = str(param.get("in") or "query").lower()
        schema = param.get("schema") if isinstance(param.get("schema"), dict) else param
        sample = _spec_typed_placeholder(schema if isinstance(schema, dict) else {})
        parameters.append({
            "name": name,
            "in": where,
            "required": bool(param.get("required")),
            "type": str((schema or {}).get("type") or param.get("type") or ""),
        })
        if where == "path":
            url_path = url_path.replace("{" + name + "}", _spec_str(sample))
        elif where == "query" and (param.get("required") or "example" in (schema or {})):
            query[name] = _spec_str(sample)
        elif where == "header" and param.get("required"):
            headers[name] = _spec_str(sample)
    url = f"{base}{url_path}" if base else url_path
    if query:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(query)}"
    body = ""
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            if "application/json" in content:
                schema = content["application/json"].get("schema")
                headers["Content-Type"] = "application/json"
                body = json.dumps(_spec_example_from_schema(root, schema or {}), ensure_ascii=False)
            elif "application/x-www-form-urlencoded" in content:
                schema = content["application/x-www-form-urlencoded"].get("schema")
                example = _spec_example_from_schema(root, schema or {})
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                if isinstance(example, dict):
                    body = urlencode({k: str(v) for k, v in example.items()})
    else:
        body_param = next(
            (p for p in all_params if isinstance(p, dict) and str(p.get("in")) == "body"),
            None,
        )
        if body_param and isinstance(body_param.get("schema"), dict):
            headers["Content-Type"] = "application/json"
            body = json.dumps(_spec_example_from_schema(root, body_param["schema"]), ensure_ascii=False)
    return {
        "url": url,
        "method": method.upper(),
        "headers": headers,
        "body": body,
        "parameters": parameters,
        "operation_id": str(operation.get("operationId") or ""),
        "summary": str(operation.get("summary") or operation.get("description") or "")[:200],
        "secured": bool(operation.get("security") or root.get("security")),
    }


def _spec_items_from_openapi(spec: dict[str, Any], base_url: str) -> list[dict[str, Any]]:
    base = _spec_openapi_base(spec, base_url)
    out: list[dict[str, Any]] = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return out
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            req = _spec_operation_request(spec, base, str(path), method, operation, shared)
            out.append(req)
            if len(out) >= _SPEC_MAX_ENDPOINTS:
                return out
    return out


def _spec_postman_url(url: Any, variables: dict[str, str]) -> str:
    raw = ""
    if isinstance(url, str):
        raw = url
    elif isinstance(url, dict):
        raw = str(url.get("raw") or "")
        if not raw:
            host = url.get("host")
            host_s = ".".join(host) if isinstance(host, list) else str(host or "")
            path = url.get("path")
            path_s = "/".join(str(p) for p in path) if isinstance(path, list) else str(path or "")
            raw = f"{host_s}/{path_s}" if host_s else path_s
    for name, value in variables.items():
        raw = raw.replace("{{" + name + "}}", value)
    return raw


def _spec_items_from_postman(spec: dict[str, Any], base_url: str) -> list[dict[str, Any]]:
    variables: dict[str, str] = {}
    for var in spec.get("variable") or []:
        if isinstance(var, dict) and var.get("key") is not None:
            variables[str(var["key"])] = str(var.get("value") or "")
    out: list[dict[str, Any]] = []

    def walk(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("item"), list):
                walk(item["item"])
                continue
            request = item.get("request")
            if not isinstance(request, dict):
                continue
            method = str(request.get("method") or "GET")
            url = _spec_postman_url(request.get("url"), variables)
            if base_url and not url.startswith(("http://", "https://")):
                url = f"{base_url.rstrip('/')}/{url.lstrip('/')}"
            headers: dict[str, str] = {}
            for header in request.get("header") or []:
                if isinstance(header, dict) and header.get("key") and not header.get("disabled"):
                    headers[str(header["key"])] = str(header.get("value") or "")
            body = ""
            body_obj = request.get("body")
            if isinstance(body_obj, dict):
                mode = str(body_obj.get("mode") or "")
                if mode == "raw":
                    body = str(body_obj.get("raw") or "")
                elif mode == "urlencoded":
                    from urllib.parse import urlencode

                    body = urlencode({
                        str(p.get("key")): str(p.get("value") or "")
                        for p in body_obj.get("urlencoded") or []
                        if isinstance(p, dict) and p.get("key") and not p.get("disabled")
                    })
                    headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            out.append({
                "url": url,
                "method": method.upper(),
                "headers": headers,
                "body": body,
                "parameters": [],
                "operation_id": "",
                "summary": str(item.get("name") or "")[:200],
                "secured": bool(request.get("auth")),
            })
            if len(out) >= _SPEC_MAX_ENDPOINTS:
                return

    walk(spec.get("item"))
    return out


def api_spec_import(spec: str = "", *, base_url: str = "", source: str = "") -> dict[str, Any]:
    """Parse an OpenAPI (v2/v3, JSON or YAML) or Postman collection into an API inventory.

    Returns the same shape as ``api_inventory_build`` (so ``api_request_select``,
    ``web_request_replay`` and the fuzzer can consume it directly), with each
    endpoint carrying a replayable ``request_template`` and a ``parameters`` list.
    """
    text = (spec or "").strip()
    if not text:
        return {"ok": False, "error": "spec is required (OpenAPI JSON/YAML or Postman collection)"}
    doc: Any
    try:
        doc = json.loads(text)
    except Exception:
        try:
            import yaml

            doc = yaml.safe_load(text)
        except Exception as exc:
            return {"ok": False, "error": f"spec is not valid JSON or YAML: {exc}"}
    if not isinstance(doc, dict):
        return {"ok": False, "error": "spec must be a JSON/YAML object"}

    if doc.get("openapi") or doc.get("swagger"):
        kind = "openapi"
        requests = _spec_items_from_openapi(doc, base_url)
    elif isinstance(doc.get("item"), list) and isinstance(doc.get("info"), dict):
        kind = "postman"
        requests = _spec_items_from_postman(doc, base_url)
    else:
        return {"ok": False, "error": "unrecognized spec: expected OpenAPI (openapi/swagger) or Postman (info+item)"}

    if not requests:
        return {"ok": True, "spec_kind": kind, "count": 0, "endpoints": [], "note": "no operations found in spec"}

    items = [
        {
            "url": req["url"],
            "method": req["method"],
            "headers": req["headers"],
            "body": req["body"],
            "content_type": req["headers"].get("Content-Type", ""),
            "source": source or kind,
            "text": req["summary"],
            "type": kind,
        }
        for req in requests
    ]
    inventory = api_inventory_build(items=items, source=source or kind)
    templates = {f"{req['method']} {req['url']}": req for req in requests}
    for endpoint in inventory.get("endpoints", []):
        req = templates.get(endpoint.get("key") or f"{endpoint.get('method')} {endpoint.get('url')}")
        if not req:
            continue
        endpoint["request_template"] = {
            "url": req["url"],
            "method": req["method"],
            "headers": req["headers"],
            "body": req["body"],
            "source": source or kind,
        }
        endpoint["parameters"] = req["parameters"]
        if req["operation_id"]:
            endpoint["operation_id"] = req["operation_id"]
        endpoint["secured"] = req["secured"]
    inventory["spec_kind"] = kind
    inventory["note"] = (
        "Endpoints carry request_template (replay with web_request_replay) and parameters "
        "(fuzz/scan targets). Confirm base_url and auth headers before sending."
    )
    return inventory


async def graphql_probe(
    endpoint: str,
    *,
    scope: str | list[str] | None = None,
    headers: dict[str, str] | None = None,
    introspect: bool = True,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Run read-only GraphQL probes against an authorized endpoint."""
    if not in_scope(endpoint, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {endpoint}", "endpoint": endpoint}
    probes = [{"name": "typename", "query": "query { __typename }"}]
    if introspect:
        probes.append({"name": "introspection", "query": INTROSPECTION_QUERY})
    results = []
    async with httpx.AsyncClient(timeout=max(1.0, min(float(timeout or 20), 60.0)), verify=False, follow_redirects=True) as client:
        for probe in probes:
            started = time.time()
            try:
                resp = await client.post(endpoint, headers=headers or {}, json={"query": probe["query"]})
            except Exception as exc:
                results.append({"name": probe["name"], "ok": False, "error": str(exc)})
                continue
            body = _json_or_text(resp)
            results.append({
                "name": probe["name"],
                "ok": True,
                "status_code": resp.status_code,
                "elapsed_ms": int((time.time() - started) * 1000),
                "content_type": resp.headers.get("content-type", ""),
                "has_data": isinstance(body, dict) and "data" in body,
                "has_errors": isinstance(body, dict) and "errors" in body,
                "schema_type_count": _schema_type_count(body),
                "body_preview": _preview(body),
            })
    return {
        "ok": True,
        "endpoint": endpoint,
        "introspection_enabled": any(r.get("name") == "introspection" and r.get("schema_type_count", 0) > 0 for r in results),
        "results": results,
        "recommended_next": [
            "If introspection is enabled, save the schema preview as an artifact.",
            "Use web_request_parse/replay for concrete GraphQL operations captured from traffic.",
            "Treat GraphQL batching or mutation tests as active validation requiring explicit scope.",
        ],
    }


async def graphql_batch_probe(endpoint: str, *, scope: str | list[str] | None = None, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Check whether a GraphQL endpoint accepts JSON array batching."""
    if not in_scope(endpoint, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {endpoint}", "endpoint": endpoint}
    payload = [{"query": "query { __typename }"}, {"query": "query { __typename }"}]
    try:
        async with httpx.AsyncClient(timeout=max(1.0, min(float(timeout or 20), 60.0)), verify=False, follow_redirects=True) as client:
            resp = await client.post(endpoint, headers=headers or {}, json=payload)
    except Exception as exc:
        return {"ok": False, "endpoint": endpoint, "error": str(exc)}
    body = _json_or_text(resp)
    accepts_batch = isinstance(body, list) or (isinstance(body, dict) and "batch" in str(body).lower())
    return {
        "ok": True,
        "endpoint": endpoint,
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "accepts_batch": bool(accepts_batch),
        "body_preview": _preview(body),
    }


def websocket_handshake_probe(url: str, *, scope: str | list[str] | None = None, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """Perform a scoped WebSocket HTTP Upgrade handshake without sending frames."""
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"}:
        return {"ok": False, "error": "url must start with ws:// or wss://", "url": url}
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    key = base64.b64encode(os.urandom(16)).decode()
    request_headers = {
        "Host": parsed.netloc,
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": "13",
        "User-Agent": "ArgusAI-WebSocket-Probe",
        **(headers or {}),
    }
    raw = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"
    started = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=max(1.0, min(float(timeout or 10), 30.0)))
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(max(1.0, min(float(timeout or 10), 30.0)))
        sock.sendall(raw.encode("ascii", errors="ignore"))
        data = sock.recv(8192)
        sock.close()
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    text = data.decode("iso-8859-1", errors="replace")
    status_line = text.splitlines()[0] if text.splitlines() else ""
    return {
        "ok": True,
        "url": url,
        "elapsed_ms": int((time.time() - started) * 1000),
        "status_line": status_line,
        "upgraded": " 101 " in f" {status_line} " or status_line.endswith(" 101"),
        "response_headers": text.split("\r\n\r\n", 1)[0][:4000],
        "recommended_next": [
            "Use reverse_recipe_apply(recipe_id='websocket') plus browser_hook_drain to capture frames from a real session.",
            "Save captured send/receive frames as artifacts before attempting replay or fuzzing.",
        ],
    }


def websocket_replay_plan(messages_json: str = "[]", url: str = "") -> dict[str, Any]:
    """Build a safe replay plan from captured WebSocket messages."""
    try:
        raw = json.loads(messages_json or "[]")
    except Exception as exc:
        return {"ok": False, "error": f"invalid messages_json: {exc}"}
    messages = raw if isinstance(raw, list) else [raw]
    normalized = []
    for idx, msg in enumerate(messages[:100]):
        if isinstance(msg, dict):
            if "data" in msg:
                data = msg.get("data")
            elif msg.get("args"):
                data = msg.get("args", [""])[0]
            else:
                data = msg.get("message", "")
            direction = msg.get("direction") or msg.get("tag") or "unknown"
        else:
            data = msg
            direction = "unknown"
        normalized.append({
            "index": idx,
            "direction": str(direction),
            "type": "json" if _is_jsonish(data) else ("binary" if isinstance(data, (bytes, bytearray)) else "text"),
            "preview": _preview(data, limit=300),
            "mutation_hints": _ws_mutation_hints(data),
        })
    return {
        "ok": True,
        "url": url,
        "message_count": len(normalized),
        "messages": normalized,
        "workflow": [
            "Confirm the handshake and authentication context first.",
            "Replay only non-destructive client-to-server messages.",
            "Mutate one field at a time and compare server replies.",
            "Attach raw frames and response deltas as artifacts/evidence.",
        ],
    }


def websocket_frame_analyze(messages_json: str = "[]") -> dict[str, Any]:
    """Normalize captured WebSocket frames from hooks/HAR/proxy output."""
    try:
        raw = json.loads(messages_json or "[]")
    except Exception as exc:
        return {"ok": False, "error": f"invalid messages_json: {exc}"}
    messages = raw if isinstance(raw, list) else [raw]
    frames = []
    event_counts: dict[str, int] = {}
    sensitive_keys: set[str] = set()
    directions: dict[str, int] = {}
    for idx, msg in enumerate(messages[:300]):
        data, direction, meta = _ws_extract_message(msg)
        kind = "json" if _is_jsonish(data) else ("binary" if isinstance(data, (bytes, bytearray)) else "text")
        parsed = _try_json(data)
        event = _ws_event_name(parsed)
        if event:
            event_counts[event] = event_counts.get(event, 0) + 1
        directions[direction] = directions.get(direction, 0) + 1
        for key in _sensitive_json_keys(parsed):
            sensitive_keys.add(key)
        frames.append({
            "index": idx,
            "direction": direction,
            "type": kind,
            "event": event,
            "preview": _preview(data, limit=300),
            "size": len(data) if isinstance(data, (str, bytes, bytearray)) else 0,
            "metadata": meta,
            "mutation_hints": _ws_mutation_hints(data),
        })
    return {
        "ok": True,
        "frame_count": len(frames),
        "directions": directions,
        "events": dict(sorted(event_counts.items())),
        "sensitive_keys": sorted(sensitive_keys),
        "frames": frames,
        "recommended_next": [
            "Identify non-destructive client-to-server messages before replay.",
            "Compare the same message under two sessions for per-message authorization.",
            "Do not fuzz high-volume subscriptions or state-changing actions without explicit approval.",
        ],
    }


def websocket_connect_probe(
    url: str,
    *,
    scope: str | list[str] | None = None,
    headers: dict[str, str] | None = None,
    origins: list[str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Probe WebSocket connection behavior without sending application frames."""
    if not origins:
        return websocket_handshake_probe(url, scope=scope, headers=headers, timeout=timeout)
    results = []
    for origin in origins[:10]:
        merged = dict(headers or {})
        if origin:
            merged["Origin"] = origin
        result = websocket_handshake_probe(url, scope=scope, headers=merged, timeout=timeout)
        results.append({
            "origin": origin,
            "ok": result.get("ok"),
            "upgraded": result.get("upgraded"),
            "status_line": result.get("status_line"),
            "error": result.get("error", ""),
        })
    accepted = [r.get("origin") for r in results if r.get("upgraded")]
    return {
        "ok": True,
        "url": url,
        "tested_origins": len(results),
        "accepted_origins": accepted,
        "results": results,
        "risk_notes": _ws_origin_risk_notes(results),
    }


def websocket_send_receive(
    url: str,
    *,
    scope: str | list[str] | None = None,
    headers: dict[str, str] | None = None,
    messages: list[Any] | None = None,
    timeout: float = 10.0,
    receive_count: int = 3,
    delay_seconds: float = 1.0,
    authorization_phrase: str = "",
) -> dict[str, Any]:
    """Send a tiny number of authorized WebSocket frames and capture replies."""
    if authorization_phrase.strip() != "AUTHORIZED":
        return {"ok": False, "error": "websocket_send_receive requires authorization_phrase=AUTHORIZED"}
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    payloads = list(messages or [])[:5]
    if not payloads:
        return {"ok": False, "error": "messages are required"}
    started = time.time()
    try:
        with _RawWebSocket(url, headers=headers or {}, timeout=max(1.0, min(float(timeout or 10), 30.0))) as ws:
            received = []
            for idx, payload in enumerate(payloads):
                if idx:
                    time.sleep(max(0.0, min(float(delay_seconds or 1.0), 10.0)))
                ws.send(payload)
                for _ in range(max(0, min(int(receive_count or 3), 10))):
                    frame = ws.recv()
                    if frame is None:
                        break
                    received.append(frame)
            ws.close()
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}
    return {
        "ok": True,
        "url": url,
        "sent_count": len(payloads),
        "received_count": len(received),
        "elapsed_ms": int((time.time() - started) * 1000),
        "sent": [_ws_safe_payload_summary(p, idx) for idx, p in enumerate(payloads)],
        "received": received,
        "warning": "Active WebSocket replay can change server state. Keep messages low-volume and non-destructive.",
    }


def websocket_authz_probe(
    url: str,
    *,
    scope: str | list[str] | None = None,
    baseline_headers: dict[str, str] | None = None,
    comparison_headers: dict[str, str] | None = None,
    message: Any = None,
    timeout: float = 10.0,
    authorization_phrase: str = "",
) -> dict[str, Any]:
    """Compare one WebSocket message under two authorization contexts."""
    if message in (None, ""):
        return {"ok": False, "error": "message is required"}
    base = websocket_send_receive(
        url,
        scope=scope,
        headers=baseline_headers or {},
        messages=[message],
        timeout=timeout,
        receive_count=3,
        authorization_phrase=authorization_phrase,
    )
    comp = websocket_send_receive(
        url,
        scope=scope,
        headers=comparison_headers or {},
        messages=[message],
        timeout=timeout,
        receive_count=3,
        authorization_phrase=authorization_phrase,
    )
    return {
        "ok": bool(base.get("ok") or comp.get("ok")),
        "url": url,
        "baseline": _ws_authz_summary(base),
        "comparison": _ws_authz_summary(comp),
        "difference": _ws_compare_ws_results(base, comp),
        "finding_hints": _ws_authz_hints(base, comp),
    }


def grpc_web_analyze(headers: dict[str, str] | str | None = None, body: str = "", body_format: str = "text") -> dict[str, Any]:
    """Analyze a gRPC-Web response/request surface and protobuf-like body."""
    header_map = _parse_headers(headers)
    content_type = _header_value(header_map, "content-type")
    grpc_status = _header_value(header_map, "grpc-status")
    grpc_message = _header_value(header_map, "grpc-message")
    raw = _decode_body(body, body_format)
    frames = _parse_grpc_web_frames(raw)
    fields = _protobuf_field_preview(frames[0]["message"] if frames else raw)
    return {
        "ok": True,
        "content_type": content_type,
        "looks_grpc_web": "grpc-web" in content_type.lower() or bool(frames),
        "grpc_status": grpc_status,
        "grpc_message": grpc_message,
        "body_bytes": len(raw),
        "frames": [{k: v for k, v in frame.items() if k != "message"} for frame in frames[:20]],
        "protobuf_field_preview": fields,
        "recommended_next": [
            "Find .proto descriptors or generated client stubs in JS bundles.",
            "Save raw frames as artifacts; decode with schema when available.",
            "Use web_request_replay for captured gRPC-Web HTTP requests before mutation.",
        ],
    }


def protobuf_field_preview(data: str = "", input_format: str = "base64") -> dict[str, Any]:
    raw = _decode_body(data, input_format)
    return {"ok": True, "byte_length": len(raw), "fields": _protobuf_field_preview(raw)}


def _coerce_items(items: Any, items_json: str) -> list[Any]:
    if items is not None:
        if isinstance(items, list):
            return items
        if isinstance(items, dict):
            return _extract_nested_items(items)
        return [items]
    if not items_json:
        return []
    try:
        parsed = json.loads(items_json)
    except Exception:
        return [line.strip() for line in items_json.splitlines() if line.strip()]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return _extract_nested_items(parsed)
    return [parsed]


def _extract_nested_items(obj: dict[str, Any]) -> list[Any]:
    for key in ("endpoints", "requests", "entries", "flows", "traffic", "items"):
        val = obj.get(key)
        if isinstance(val, list):
            return val
    if "log" in obj and isinstance(obj["log"], dict):
        entries = obj["log"].get("entries")
        if isinstance(entries, list):
            out = []
            for e in entries:
                req = e.get("request", {}) if isinstance(e, dict) else {}
                resp = e.get("response", {}) if isinstance(e, dict) else {}
                out.append({
                    "url": req.get("url"),
                    "method": req.get("method"),
                    "status": resp.get("status"),
                    "headers": {h.get("name"): h.get("value") for h in resp.get("headers", []) if isinstance(h, dict)},
                    "source": "har",
                })
            return out
    return [obj]


def _guess_protocol(url: str, extra: dict[str, Any]) -> str:
    if url.startswith(("ws://", "wss://")):
        return "websocket"
    if "grpc-web" in str(extra.get("content_type") or "").lower():
        return "grpc-web"
    if _looks_graphql(url, str(extra.get("content_type") or ""), extra):
        return "graphql"
    return "http"


def _looks_graphql(url: str, content_type: str, obj: dict[str, Any]) -> bool:
    body = str(obj.get("body") or obj.get("postData") or obj.get("request", {}).get("body") or "")
    return "graphql" in url.lower() or "__typename" in body or ("\"query\"" in body and "graphql" in content_type.lower())


def _looks_grpc_web(content_type: str, obj: dict[str, Any]) -> bool:
    return "grpc-web" in content_type.lower() or "x-grpc-web" in json.dumps(obj, default=str).lower()


def _header_value(headers: Any, name: str) -> str:
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == name.lower():
                return str(v)
    return ""


def _safe_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _inventory_next_actions(endpoints: list[dict[str, Any]]) -> list[str]:
    tags = {tag for ep in endpoints for tag in (ep.get("tags") or [])}
    actions = ["Use api_request_select to narrow endpoints before replay."]
    if any(ep.get("business_candidates") for ep in endpoints):
        actions.append("Run business_surface_extract to structure business candidates, then let the Agent create evidence-backed todos.")
    if "graphql" in tags:
        actions.append("Run graphql_probe against candidate GraphQL endpoints.")
    if "websocket" in tags:
        actions.append("Run websocket_handshake_probe or capture frames with the websocket hook recipe.")
    if "grpc-web" in tags:
        actions.append("Run grpc_web_analyze and look for protobuf schemas in JS artifacts.")
    return actions


def _business_summary(endpoints: list[dict[str, Any]]) -> dict[str, Any]:
    assets: dict[str, int] = {}
    operations: dict[str, int] = {}
    risks: dict[str, int] = {}
    for ep in endpoints:
        candidates = ep.get("business_candidates") if isinstance(ep.get("business_candidates"), dict) else {}
        for asset in candidates.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name"):
                name = str(asset["name"])
                assets[name] = assets.get(name, 0) + 1
        for operation in candidates.get("operations") or []:
            if isinstance(operation, dict) and operation.get("name"):
                name = str(operation["name"])
                operations[name] = operations.get(name, 0) + 1
        for risk in candidates.get("risks") or []:
            if isinstance(risk, dict) and risk.get("name"):
                name = str(risk["name"])
                risks[name] = risks.get(name, 0) + 1
    return {
        "note": "Counts are candidate signals for Agent review, not confirmed business facts.",
        "assets": dict(sorted(assets.items(), key=lambda item: (-item[1], item[0]))),
        "operations": dict(sorted(operations.items(), key=lambda item: (-item[1], item[0]))),
        "risks": dict(sorted(risks.items(), key=lambda item: (-item[1], item[0]))),
    }


def _merge_business_candidates(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in ("assets", "operations", "risks"):
        seen: set[str] = set()
        rows = []
        for item in list((a or {}).get(key) or []) + list((b or {}).get(key) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            rows.append(item)
        merged[key] = rows
    return merged


def _inventory_set(text: str) -> set[str]:
    try:
        raw = json.loads(text or "{}")
    except Exception:
        return set()
    endpoints = raw.get("endpoints") if isinstance(raw, dict) else raw
    if not isinstance(endpoints, list):
        return set()
    return {f"{str(e.get('method') or 'GET').upper()} {e.get('url')}" for e in endpoints if isinstance(e, dict)}


def _json_or_text(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text[:4000]


def _schema_type_count(body: Any) -> int:
    try:
        return len(body.get("data", {}).get("__schema", {}).get("types") or [])
    except Exception:
        return 0


def _preview(value: Any, limit: int = 1000) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (bytes, bytearray)):
        text = value[:limit].hex()
    else:
        text = str(value)
    return text[:limit]


def _is_jsonish(value: Any) -> bool:
    if isinstance(value, (dict, list)):
        return True
    if not isinstance(value, str):
        return False
    try:
        json.loads(value)
        return True
    except Exception:
        return False


def _ws_mutation_hints(value: Any) -> list[str]:
    if isinstance(value, dict):
        keys = list(value.keys())[:10]
        return [f"mutate JSON field: {k}" for k in keys]
    if isinstance(value, str) and _is_jsonish(value):
        try:
            return _ws_mutation_hints(json.loads(value))
        except Exception:
            pass
    hints = []
    text = str(value)
    if any(ch.isdigit() for ch in text):
        hints.append("numeric token/id present")
    if len(text) > 0:
        hints.append("replay exact message before mutation")
    return hints


class _RawWebSocket:
    def __init__(self, url: str, *, headers: dict[str, str], timeout: float) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None

    def __enter__(self) -> "_RawWebSocket":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("url must start with ws:// or wss://")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        sock: socket.socket | ssl.SSLSocket = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request_headers = {
            "Host": parsed.netloc,
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": key,
            "Sec-WebSocket-Version": "13",
            "User-Agent": "ArgusAI-WebSocket-Client",
            **self.headers,
        }
        raw = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in request_headers.items()) + "\r\n"
        sock.sendall(raw.encode("ascii", errors="ignore"))
        response = self._recv_until(sock, b"\r\n\r\n", 8192).decode("iso-8859-1", errors="replace")
        status_line = response.splitlines()[0] if response.splitlines() else ""
        if " 101 " not in f" {status_line} " and not status_line.endswith(" 101"):
            sock.close()
            raise RuntimeError(f"websocket upgrade failed: {status_line}")
        accept = _header_value(_headers_from_response(response), "sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept and accept.strip() != expected:
            sock.close()
            raise RuntimeError("websocket upgrade accept header mismatch")
        self.sock = sock

    def send(self, payload: Any) -> None:
        if self.sock is None:
            raise RuntimeError("websocket is not connected")
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            opcode = 0x1
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
            opcode = 0x2
        else:
            data = str(payload).encode("utf-8", errors="replace")
            opcode = 0x1
        self.sock.sendall(_ws_build_frame(data[:65536], opcode=opcode))

    def recv(self) -> dict[str, Any] | None:
        if self.sock is None:
            raise RuntimeError("websocket is not connected")
        try:
            frame = _ws_read_frame(self.sock)
        except socket.timeout:
            return None
        if frame is None:
            return None
        if frame["opcode"] == 0x8:
            return {"direction": "server", "opcode": "close", "preview": _preview(frame["payload"], 200), "size": len(frame["payload"])}
        if frame["opcode"] == 0x9:
            self.sock.sendall(_ws_build_frame(frame["payload"], opcode=0xA))
            return self.recv()
        data = frame["payload"]
        text = data.decode("utf-8", errors="replace") if frame["opcode"] == 0x1 else data.hex()
        return {
            "direction": "server",
            "opcode": "text" if frame["opcode"] == 0x1 else ("binary" if frame["opcode"] == 0x2 else frame["opcode"]),
            "type": "json" if _is_jsonish(text) else ("binary" if frame["opcode"] == 0x2 else "text"),
            "preview": _preview(text, 1000),
            "size": len(data),
        }

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(_ws_build_frame(b"", opcode=0x8))
        except Exception:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    @staticmethod
    def _recv_until(sock: socket.socket | ssl.SSLSocket, marker: bytes, limit: int) -> bytes:
        data = b""
        while marker not in data and len(data) < limit:
            chunk = sock.recv(1024)
            if not chunk:
                break
            data += chunk
        return data


def _ws_build_frame(payload: bytes, *, opcode: int) -> bytes:
    mask = os.urandom(4)
    first = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def _ws_read_frame(sock: socket.socket | ssl.SSLSocket) -> dict[str, Any] | None:
    head = _recv_exact(sock, 2)
    if not head:
        return None
    first, second = head[0], head[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > 1_000_000:
        raise RuntimeError("websocket frame too large")
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return {"opcode": opcode, "payload": payload}


def _recv_exact(sock: socket.socket | ssl.SSLSocket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise RuntimeError("websocket connection closed")
        data += chunk
    return data


def _headers_from_response(text: str) -> dict[str, str]:
    out = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _try_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _ws_extract_message(msg: Any) -> tuple[Any, str, dict[str, Any]]:
    if not isinstance(msg, dict):
        return msg, "unknown", {}
    data = msg.get("data") if "data" in msg else msg.get("message", msg.get("payload", ""))
    if msg.get("args") and not data:
        args = msg.get("args") or []
        data = args[0] if args else ""
    direction = str(msg.get("direction") or msg.get("tag") or msg.get("kind") or "unknown")
    meta = {k: msg.get(k) for k in ("url", "timestamp", "opcode", "type") if k in msg}
    return data, direction, meta


def _ws_event_name(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return ""
    for key in ("type", "event", "action", "method", "op", "cmd", "name"):
        val = parsed.get(key)
        if isinstance(val, str) and val:
            return val[:80]
    return ""


def _sensitive_json_keys(value: Any, prefix: str = "") -> set[str]:
    keys = set()
    if isinstance(value, dict):
        for key, val in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(token in str(key).lower() for token in ("token", "secret", "password", "cookie", "session", "auth")):
                keys.add(path)
            keys.update(_sensitive_json_keys(val, path))
    elif isinstance(value, list):
        for item in value[:20]:
            keys.update(_sensitive_json_keys(item, prefix))
    return keys


def _ws_origin_risk_notes(results: list[dict[str, Any]]) -> list[str]:
    accepted = [r for r in results if r.get("upgraded")]
    if not accepted:
        return ["No tested Origin value completed the WebSocket upgrade."]
    notes = ["At least one tested Origin completed the WebSocket upgrade; verify this is expected."]
    origins = [str(r.get("origin") or "") for r in accepted]
    if any(origin in {"", "null"} or origin.startswith(("http://evil", "https://evil")) for origin in origins):
        notes.append("A suspicious/null Origin appears accepted. Check server-side Origin allowlist enforcement.")
    return notes


def _ws_safe_payload_summary(payload: Any, idx: int) -> dict[str, Any]:
    return {
        "index": idx,
        "type": "json" if isinstance(payload, (dict, list)) or _is_jsonish(payload) else ("binary" if isinstance(payload, (bytes, bytearray)) else "text"),
        "preview": _preview(payload, 300),
        "mutation_hints": _ws_mutation_hints(payload),
    }


def _ws_authz_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "error": result.get("error", ""),
        "sent_count": result.get("sent_count", 0),
        "received_count": result.get("received_count", 0),
        "received_preview": [r.get("preview", "") for r in (result.get("received") or [])[:3] if isinstance(r, dict)],
    }


def _ws_compare_ws_results(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a_prev = [r.get("preview", "") for r in (a.get("received") or []) if isinstance(r, dict)]
    b_prev = [r.get("preview", "") for r in (b.get("received") or []) if isinstance(r, dict)]
    return {
        "baseline_ok": a.get("ok"),
        "comparison_ok": b.get("ok"),
        "same_success_state": bool(a.get("ok")) == bool(b.get("ok")),
        "same_response_preview": a_prev[:3] == b_prev[:3],
        "baseline_received": len(a_prev),
        "comparison_received": len(b_prev),
    }


def _ws_authz_hints(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    hints = []
    if a.get("ok") and b.get("ok"):
        hints.append("Both authorization contexts received WebSocket responses. Confirm whether the comparison identity should have access.")
    if a.get("ok") and not b.get("ok"):
        hints.append("Comparison context failed while baseline succeeded; this may indicate expected authorization enforcement.")
    if not a.get("ok") and b.get("ok"):
        hints.append("Comparison context succeeded while baseline failed; verify headers/session setup and authorization assumptions.")
    diff = _ws_compare_ws_results(a, b)
    if diff.get("same_response_preview") and a.get("ok") and b.get("ok"):
        hints.append("Responses look identical across contexts. Check for cross-user channel or object authorization issues.")
    return hints


def _parse_headers(headers: dict[str, str] | str | None) -> dict[str, str]:
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    if isinstance(headers, str) and headers.strip():
        try:
            parsed = json.loads(headers)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
        out = {}
        for line in headers.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out
    return {}


def _decode_body(body: str, fmt: str) -> bytes:
    if not body:
        return b""
    fmt = (fmt or "text").lower()
    if fmt == "base64":
        return base64.b64decode(body, validate=False)
    if fmt == "hex":
        return bytes.fromhex("".join(ch for ch in body if ch in "0123456789abcdefABCDEF"))
    return body.encode("latin-1", errors="replace")


def _parse_grpc_web_frames(raw: bytes) -> list[dict[str, Any]]:
    frames = []
    pos = 0
    while pos + 5 <= len(raw) and len(frames) < 50:
        flag = raw[pos]
        size = int.from_bytes(raw[pos + 1:pos + 5], "big")
        pos += 5
        if size < 0 or pos + size > len(raw):
            break
        msg = raw[pos:pos + size]
        pos += size
        frames.append({
            "flag": flag,
            "compressed": bool(flag & 0x01),
            "trailer": bool(flag & 0x80),
            "message_length": len(msg),
            "message_preview_hex": msg[:64].hex(),
            "message": msg,
        })
    return frames


def _read_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    shift = 0
    result = 0
    while pos < len(data) and shift < 70:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    return None


def _protobuf_field_preview(data: bytes) -> list[dict[str, Any]]:
    fields = []
    pos = 0
    while pos < len(data) and len(fields) < 50:
        key = _read_varint(data, pos)
        if key is None:
            break
        key_val, pos = key
        field_no = key_val >> 3
        wire = key_val & 0x07
        item: dict[str, Any] = {"field": field_no, "wire_type": wire, "offset": pos}
        if wire == 0:
            val = _read_varint(data, pos)
            if val is None:
                break
            item["value_preview"] = val[0]
            pos = val[1]
        elif wire == 1:
            item["value_preview_hex"] = data[pos:pos + 8].hex()
            pos += 8
        elif wire == 2:
            ln = _read_varint(data, pos)
            if ln is None:
                break
            length, pos = ln
            raw = data[pos:pos + length]
            item["length"] = length
            item["value_preview"] = raw[:80].decode("utf-8", errors="replace")
            item["value_preview_hex"] = raw[:40].hex()
            pos += length
        elif wire == 5:
            item["value_preview_hex"] = data[pos:pos + 4].hex()
            pos += 4
        else:
            item["unsupported"] = True
            break
        fields.append(item)
    return fields
