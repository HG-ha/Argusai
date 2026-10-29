"""File upload surface discovery and safe payload planning."""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from argusai.core.infra.command import in_scope, normalize_scope


UPLOAD_INPUT_RE = re.compile(r"<input\b[^>]*type=[\"']?file[\"']?[^>]*>", re.I)
FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.I | re.S)
ATTR_RE = re.compile(r"([a-zA-Z_:.-]+)\s*=\s*[\"']([^\"']*)[\"']")


def _attrs(text: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in ATTR_RE.finditer(text or "")}


def upload_surface_discover_from_html(html: str, base_url: str = "") -> dict[str, Any]:
    surfaces = []
    for m in FORM_RE.finditer(html or ""):
        body = m.group("body")
        if not UPLOAD_INPUT_RE.search(body):
            continue
        attrs = _attrs(m.group("attrs"))
        action = attrs.get("action") or base_url
        surfaces.append({
            "action": urljoin(base_url, action) if base_url else action,
            "method": (attrs.get("method") or "POST").upper(),
            "enctype": attrs.get("enctype") or "application/x-www-form-urlencoded",
            "file_inputs": [_attrs(x) for x in UPLOAD_INPUT_RE.findall(body)],
        })
    return {"ok": True, "surfaces": surfaces, "count": len(surfaces)}


async def upload_surface_discover(url: str, *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
            r = await client.get(url)
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}
    out = upload_surface_discover_from_html(r.text, str(r.url))
    out.update({"url": str(r.url), "status_code": r.status_code})
    return out


def upload_payload_matrix() -> dict[str, Any]:
    samples = [
        {"filename": "probe.txt", "content_type": "text/plain", "body": "argusai-upload-probe"},
        {"filename": "probe.html", "content_type": "text/html", "body": "<script>console.log('argusai-upload-probe')</script>"},
        {"filename": "probe.svg", "content_type": "image/svg+xml", "body": "<svg xmlns=\"http://www.w3.org/2000/svg\"><script>console.log('argusai-upload-probe')</script></svg>"},
        {"filename": "probe.php.txt", "content_type": "text/plain", "body": "<?php echo 'argusai-upload-probe'; ?>"},
        {"filename": "../probe.txt", "content_type": "text/plain", "body": "zip-slip-name-probe"},
    ]
    return {
        "ok": True,
        "payloads": samples,
        "notes": "Use only in authorized upload forms. These are benign markers, not webshells.",
    }


async def upload_verify_access(url: str, marker: str = "argusai-upload-probe", *, scope: str | list[str] | None = None) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
            r = await client.get(url)
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}
    body = r.text
    return {
        "ok": True,
        "url": str(r.url),
        "status_code": r.status_code,
        "content_type": r.headers.get("content-type", ""),
        "marker_found": marker in body,
        "looks_executable_html": "<script" in body.lower() or "image/svg" in r.headers.get("content-type", "").lower(),
        "snippet_b64": base64.b64encode(body[:500].encode(errors="ignore")).decode(),
    }

