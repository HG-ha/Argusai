"""Web-domain request parsing, replay, mutation and response diff primitives."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from argusai.core.infra.command import in_scope, normalize_scope


@dataclass
class WebRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    interaction_id: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["method"] = (data.get("method") or "GET").upper()
        return data


def parse_request(
    *,
    url: str = "",
    method: str = "GET",
    headers: dict[str, str] | str | None = None,
    body: str = "",
    raw: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Parse a raw HTTP request or explicit fields into a canonical request."""
    if raw:
        parsed = _parse_raw_http(raw, fallback_url=url)
        if parsed.get("ok"):
            parsed["request"]["source"] = source or "raw"
            return parsed
        return parsed
    req = WebRequest(
        url=str(url or ""),
        method=str(method or "GET").upper(),
        headers=_parse_headers(headers),
        body=str(body or ""),
        source=source,
    )
    return {"ok": True, "request": req.to_dict(), "injection_points": discover_injection_points(req.to_dict())}


def discover_injection_points(request: dict[str, Any]) -> list[dict[str, Any]]:
    url = str(request.get("url") or "")
    headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
    body = str(request.get("body") or "")
    points: list[dict[str, Any]] = []
    parsed = urlparse(url)
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        points.append({"location": "query", "name": key})
    for key in headers:
        if key.lower() in {"user-agent", "referer", "origin", "x-forwarded-for", "x-forwarded-host"}:
            points.append({"location": "header", "name": key})
    cookie = headers.get("Cookie") or headers.get("cookie") or ""
    for item in cookie.split(";"):
        if "=" in item:
            points.append({"location": "cookie", "name": item.split("=", 1)[0].strip()})
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if body and "json" in content_type.lower():
        try:
            obj = json.loads(body)
            points.extend({"location": "json", "name": p} for p in _json_paths(obj))
        except Exception:
            pass
    elif body and ("x-www-form-urlencoded" in content_type.lower() or "=" in body):
        for key, _value in parse_qsl(body, keep_blank_values=True):
            points.append({"location": "form", "name": key})
    path_parts = [p for p in parsed.path.split("/") if p]
    for idx, part in enumerate(path_parts):
        if any(ch.isdigit() for ch in part):
            points.append({"location": "path", "name": str(idx), "value": part})
    return points


def mutate_request(request: dict[str, Any], *, location: str, name: str, value: str) -> dict[str, Any]:
    """Return a mutated copy of a canonical request."""
    req = {
        "url": str(request.get("url") or ""),
        "method": str(request.get("method") or "GET").upper(),
        "headers": dict(request.get("headers") or {}),
        "body": str(request.get("body") or ""),
        "source": request.get("source") or "",
        "metadata": dict(request.get("metadata") or {}),
    }
    loc = str(location or "").lower()
    if loc == "query":
        req["url"] = _mutate_query(req["url"], name, value)
    elif loc == "header":
        req["headers"][name] = value
    elif loc == "cookie":
        req["headers"]["Cookie"] = _mutate_cookie(req["headers"].get("Cookie") or req["headers"].get("cookie") or "", name, value)
    elif loc == "json":
        req["body"] = _mutate_json(req["body"], name, value)
        req["headers"].setdefault("Content-Type", "application/json")
    elif loc == "form":
        req["body"] = _mutate_form(req["body"], name, value)
        req["headers"].setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif loc == "path":
        req["url"] = _mutate_path(req["url"], name, value)
    else:
        return {"ok": False, "error": f"unsupported mutation location: {location}", "request": req}
    return {"ok": True, "request": req, "mutation": {"location": loc, "name": name, "value": value}}


async def replay_request(request: dict[str, Any], *, scope: str | list[str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """Replay a canonical request with scope enforcement."""
    url = str(request.get("url") or "")
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=max(1.0, min(float(timeout or 20.0), 120.0)), verify=False, follow_redirects=True) as client:
            resp = await client.request(
                str(request.get("method") or "GET").upper(),
                url,
                headers=dict(request.get("headers") or {}),
                content=str(request.get("body") or "").encode("utf-8") if request.get("body") else None,
            )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}
    elapsed = time.time() - started
    return {
        "ok": True,
        "request": {
            "url": url,
            "method": str(request.get("method") or "GET").upper(),
            "headers": dict(request.get("headers") or {}),
            "body_len": len(str(request.get("body") or "")),
        },
        "response": {
            "status_code": resp.status_code,
            "url": str(resp.url),
            "headers": dict(resp.headers),
            "body": resp.text[:20000],
            "body_len": len(resp.text),
            "elapsed_ms": round(elapsed * 1000, 2),
        },
    }


def diff_responses(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ra = a.get("response") if "response" in a else a
    rb = b.get("response") if "response" in b else b
    body_a = str(ra.get("body") or "")
    body_b = str(rb.get("body") or "")
    len_a = int(ra.get("body_len") or len(body_a))
    len_b = int(rb.get("body_len") or len(body_b))
    status_a = ra.get("status_code")
    status_b = rb.get("status_code")
    length_delta = len_b - len_a
    ratio = 0.0 if max(len_a, len_b) == 0 else abs(length_delta) / max(len_a, len_b)
    common_prefix = 0
    for ca, cb in zip(body_a, body_b):
        if ca != cb:
            break
        common_prefix += 1
    signals = []
    if status_a != status_b:
        signals.append("status_changed")
    if ratio > 0.2:
        signals.append("length_delta_gt_20pct")
    if body_a and body_b and common_prefix < min(len(body_a), len(body_b), 200):
        signals.append("early_body_difference")
    return {
        "ok": True,
        "status_a": status_a,
        "status_b": status_b,
        "len_a": len_a,
        "len_b": len_b,
        "length_delta": length_delta,
        "length_delta_ratio": round(ratio, 4),
        "common_prefix_chars": common_prefix,
        "signals": signals,
        "interesting": bool(signals),
    }


def request_to_curl(request: dict[str, Any]) -> dict[str, Any]:
    parts = ["curl", "-i", "-X", str(request.get("method") or "GET").upper()]
    for key, value in (request.get("headers") or {}).items():
        parts += ["-H", json.dumps(f"{key}: {value}")]
    body = str(request.get("body") or "")
    if body:
        parts += ["--data-binary", json.dumps(body)]
    parts.append(json.dumps(str(request.get("url") or "")))
    return {"ok": True, "curl": " ".join(parts)}


def _parse_headers(headers: dict[str, str] | str | None) -> dict[str, str]:
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    if isinstance(headers, str) and headers.strip():
        try:
            raw = json.loads(headers)
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items()}
        except Exception:
            pass
        out = {}
        for line in headers.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                out[k.strip()] = v.strip()
        return out
    return {}


def _parse_raw_http(raw: str, fallback_url: str = "") -> dict[str, Any]:
    lines = raw.replace("\r\n", "\n").split("\n")
    if not lines or " " not in lines[0]:
        return {"ok": False, "error": "raw HTTP request must start with a request line"}
    method, target, *_ = lines[0].split()
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines[1:]:
        if in_body:
            body_lines.append(line)
            continue
        if line == "":
            in_body = True
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    host = headers.get("Host") or headers.get("host") or ""
    url = target
    if not url.startswith(("http://", "https://")):
        if fallback_url.startswith(("http://", "https://")):
            base = urlparse(fallback_url)
            scheme = base.scheme
            host = host or base.netloc
        else:
            scheme = "https"
        url = f"{scheme}://{host}{target}" if host else target
    req = WebRequest(url=url, method=method.upper(), headers=headers, body="\n".join(body_lines))
    return {"ok": True, "request": req.to_dict(), "injection_points": discover_injection_points(req.to_dict())}


def _mutate_query(url: str, name: str, value: str) -> str:
    p = urlparse(url)
    params = parse_qsl(p.query, keep_blank_values=True)
    found = False
    new_params = []
    for k, v in params:
        if k == name:
            new_params.append((k, value))
            found = True
        else:
            new_params.append((k, v))
    if not found:
        new_params.append((name, value))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(new_params), p.fragment))


def _mutate_cookie(cookie: str, name: str, value: str) -> str:
    parts = []
    found = False
    for item in cookie.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" in item and item.split("=", 1)[0].strip() == name:
            parts.append(f"{name}={value}")
            found = True
        else:
            parts.append(item)
    if not found:
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _mutate_form(body: str, name: str, value: str) -> str:
    params = parse_qsl(body, keep_blank_values=True)
    found = False
    out = []
    for k, v in params:
        if k == name:
            out.append((k, value))
            found = True
        else:
            out.append((k, v))
    if not found:
        out.append((name, value))
    return urlencode(out)


def _mutate_path(url: str, name: str, value: str) -> str:
    p = urlparse(url)
    parts = p.path.split("/")
    idx = int(name)
    non_empty_positions = [i for i, part in enumerate(parts) if part]
    if 0 <= idx < len(non_empty_positions):
        parts[non_empty_positions[idx]] = value
    return urlunparse((p.scheme, p.netloc, "/".join(parts), p.params, p.query, p.fragment))


def _json_paths(obj: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            p = f"{prefix}.{key}" if prefix else str(key)
            paths.append(p)
            paths.extend(_json_paths(value, p))
    elif isinstance(obj, list):
        for idx, value in enumerate(obj[:20]):
            p = f"{prefix}[{idx}]"
            paths.append(p)
            paths.extend(_json_paths(value, p))
    return paths


def _mutate_json(body: str, path: str, value: str) -> str:
    obj = json.loads(body or "{}")
    if not path:
        return json.dumps(value)
    parts = path.replace("[", ".[").split(".")
    cur = obj
    for part in parts[:-1]:
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            cur = cur[int(part[1:-1])]
        else:
            cur = cur.setdefault(part, {})
    last = parts[-1]
    if last.startswith("[") and last.endswith("]"):
        cur[int(last[1:-1])] = value
    else:
        cur[last] = value
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
