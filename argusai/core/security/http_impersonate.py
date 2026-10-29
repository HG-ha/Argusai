"""TLS-fingerprint-impersonating HTTP requests via ``curl_cffi``.

``httpx`` produces a fixed Python/openssl ClientHello, which Cloudflare /
Akamai / DataDome readily flag as a bot. ``curl_cffi`` ships precompiled
libcurl-impersonate builds that mimic real Chrome / Firefox / Safari /
Edge ClientHello + HTTP/2 settings.

The dependency is declared in ``pyproject.toml`` but the module guards
its import so users without ``curl_cffi`` installed still see a friendly
error instead of a hard ImportError mid-tool-call.
"""

from __future__ import annotations

import json as _json
from typing import Any, Iterable

from argusai.core.infra.command import in_scope, normalize_scope

KNOWN_IMPERSONATIONS: tuple[str, ...] = (
    "chrome",
    "chrome99",
    "chrome100",
    "chrome101",
    "chrome104",
    "chrome107",
    "chrome110",
    "chrome116",
    "chrome119",
    "chrome120",
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome131_android",
    "chrome99_android",
    "edge99",
    "edge101",
    "safari15_3",
    "safari15_5",
    "safari17_0",
    "safari17_2_ios",
    "safari18_0",
    "safari18_0_ios",
    "firefox133",
    "firefox135",
)

BODY_LIMIT = 12000


def _coerce_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items() if k}
    if isinstance(headers, str):
        s = headers.strip()
        if not s:
            return {}
        try:
            parsed = _json.loads(s)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if k}
        except Exception:
            pass
        out: dict[str, str] = {}
        for line in s.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out
    if isinstance(headers, Iterable):
        out2: dict[str, str] = {}
        for item in headers:
            if isinstance(item, str) and ":" in item:
                k, v = item.split(":", 1)
                if k.strip():
                    out2[k.strip()] = v.strip()
        return out2
    return {}


def _coerce_cookies(cookies: Any) -> Any:
    if not cookies:
        return None
    if isinstance(cookies, dict):
        return {str(k): str(v) for k, v in cookies.items() if k}
    if isinstance(cookies, str):
        s = cookies.strip()
        if not s:
            return None
        try:
            parsed = _json.loads(s)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if k}
        except Exception:
            pass
        return s  # raw "k=v; k2=v2"
    return None


def _body_param(data: Any) -> tuple[Any, Any]:
    """Return ``(data, json_payload)`` arguments for curl_cffi.request."""
    if data is None or data == "":
        return None, None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data), None
    if isinstance(data, str):
        return data, None
    return None, data  # dict/list -> json


def supported_impersonations() -> list[str]:
    return list(KNOWN_IMPERSONATIONS)


def is_available() -> dict[str, Any]:
    try:
        import curl_cffi  # noqa: F401
        from curl_cffi import requests as _r  # noqa: F401

        return {"ok": True, "available": True, "impersonations": list(KNOWN_IMPERSONATIONS)}
    except Exception as e:
        return {
            "ok": True,
            "available": False,
            "error": str(e),
            "hint": "Install with: uv pip install curl-cffi",
            "impersonations": list(KNOWN_IMPERSONATIONS),
        }


async def http_request_impersonate(
    target: str,
    *,
    scope: str | list[str] | None = None,
    method: str = "GET",
    impersonate: str = "chrome124",
    headers: Any = None,
    cookies: Any = None,
    data: Any = None,
    params: Any = None,
    timeout_seconds: float = 30.0,
    follow_redirects: bool = True,
    verify: bool = True,
    proxy: str = "",
    user_agent: str = "",
) -> dict[str, Any]:
    """Send an HTTP request that mimics a real browser's TLS+HTTP/2 fingerprint.

    Backed by ``curl_cffi.requests.AsyncSession`` with ``impersonate=...``
    so the on-wire ClientHello / ALPN / HTTP/2 settings match a known
    browser build. Useful when the target gates anti-bot decisions on
    JA3/JA4 or HTTP/2 fingerprints (Cloudflare, Akamai, Imperva,
    PerimeterX, DataDome, etc.).
    """
    if not in_scope(target, normalize_scope(scope)):
        return {"ok": False, "url": target, "error": f"target out of authorized scope: {target}"}
    try:
        from curl_cffi import requests as cc_requests
    except Exception as e:
        return {
            "ok": False,
            "url": target,
            "error": f"curl_cffi unavailable: {e}",
            "hint": "Install with: uv pip install curl-cffi",
        }

    impersonate = (impersonate or "chrome124").strip() or "chrome124"
    method = (method or "GET").upper()
    hdrs = _coerce_headers(headers)
    if user_agent:
        hdrs["User-Agent"] = user_agent
    ck = _coerce_cookies(cookies)
    body, json_payload = _body_param(data)
    timeout = max(1.0, min(float(timeout_seconds or 30.0), 120.0))

    request_kwargs: dict[str, Any] = {
        "method": method,
        "url": target,
        "headers": hdrs or None,
        "cookies": ck,
        "data": body,
        "json": json_payload,
        "params": params or None,
        "timeout": timeout,
        "allow_redirects": bool(follow_redirects),
        "verify": bool(verify),
        "impersonate": impersonate,
    }
    if proxy:
        request_kwargs["proxies"] = {"http": proxy, "https": proxy}

    try:
        async with cc_requests.AsyncSession(impersonate=impersonate) as session:
            resp = await session.request(**{k: v for k, v in request_kwargs.items() if v is not None or k in {"data", "json", "cookies", "params", "headers"}})
    except Exception as e:
        return {"ok": False, "url": target, "impersonate": impersonate, "error": str(e)}

    try:
        text = resp.text or ""
    except Exception:
        text = ""
    try:
        resp_headers = dict(resp.headers or {})
    except Exception:
        resp_headers = {}

    return {
        "ok": True,
        "url": target,
        "final_url": str(getattr(resp, "url", target)),
        "status_code": resp.status_code,
        "impersonate": impersonate,
        "elapsed_ms": int(getattr(resp, "elapsed", 0) * 1000) if isinstance(getattr(resp, "elapsed", None), (int, float)) else None,
        "response_headers": {k: str(v) for k, v in resp_headers.items()},
        "snippet": text[:BODY_LIMIT],
        "body_length": len(text),
        "truncated": len(text) > BODY_LIMIT,
        "request": {
            "method": method,
            "headers": hdrs,
            "cookies": ck if isinstance(ck, dict) else (str(ck) if ck else None),
            "data": body if isinstance(body, str) else (None if json_payload is None else json_payload),
        },
    }
