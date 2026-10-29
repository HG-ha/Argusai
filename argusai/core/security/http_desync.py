"""Low-frequency HTTP desync/cache-poisoning probes and raw request helper."""

from __future__ import annotations

import asyncio
import ssl
import time
from typing import Any
from urllib.parse import urlparse

from argusai.core.infra.command import in_scope, normalize_scope


def _target(url: str) -> tuple[str, int, bool, str]:
    u = urlparse(url)
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise ValueError("raw HTTP tools require http(s) URL")
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    return u.hostname, port, u.scheme == "https", path


async def raw_http_send(url: str, raw_request: str = "", *, scope: str | list[str] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    host, port, use_tls, path = _target(url)
    if not raw_request:
        raw_request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    started = time.time()
    try:
        ssl_ctx = ssl.create_default_context() if use_tls else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=host if use_tls else None), timeout=timeout)
        writer.write(raw_request.encode("iso-8859-1", errors="replace"))
        await writer.drain()
        data = await asyncio.wait_for(reader.read(12000), timeout=timeout)
        writer.close()
        await writer.wait_closed()
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}
    return {"ok": True, "url": url, "elapsed_ms": int((time.time() - started) * 1000), "response_excerpt": data.decode("iso-8859-1", errors="replace")[:12000]}


def _desync_templates(host: str, path: str) -> dict[str, str]:
    return {
        "cl_te": (
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: 4\r\n"
            "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n0\r\n\r\n"
        ),
        "te_cl": (
            f"POST {path} HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: chunked\r\n"
            "Content-Length: 6\r\nConnection: close\r\n\r\n0\r\n\r\nX"
        ),
    }


async def http_desync_probe(url: str, *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    host, _, _, path = _target(url)
    results = []
    for name, raw in _desync_templates(host, path).items():
        r = await raw_http_send(url, raw, scope=scope, timeout=6)
        r["variant"] = name
        results.append(r)
    suspected = any(r.get("ok") and ("400" not in r.get("response_excerpt", "")[:80]) for r in results)
    return {
        "ok": True,
        "url": url,
        "suspected": suspected,
        "confidence": "suspected" if suspected else "negative",
        "results": results,
        "note": "HTTP desync probes are heuristic and require manual confirmation with a controlled backend/proxy chain.",
    }


async def cache_poison_probe(url: str, header_name: str = "X-Forwarded-Host", header_value: str = "argusai.invalid", *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    import httpx

    async with httpx.AsyncClient(timeout=10, verify=False, follow_redirects=False) as client:
        try:
            base = await client.get(url)
            probe = await client.get(url, headers={header_name: header_value})
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}
    reflected = header_value in probe.text or header_value in " ".join(probe.headers.values())
    return {
        "ok": True,
        "url": url,
        "header": header_name,
        "value": header_value,
        "reflected": reflected,
        "status_base": base.status_code,
        "status_probe": probe.status_code,
        "note": "Reflection of cache-related headers is a cache poisoning lead, not proof by itself.",
    }

