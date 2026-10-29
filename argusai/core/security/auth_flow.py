"""Access-control and business-logic helper tools."""

from __future__ import annotations

import re
from typing import Any

import httpx

from argusai.core.infra.command import in_scope, normalize_scope
from argusai.core.security.evidence import curl_repro, evidence_record, response_excerpt


ID_PATTERNS = [
    re.compile(r"(?P<prefix>/[A-Za-z0-9._/-]+/)(?P<id>\d+)(?P<suffix>[/#?].*)?$"),
    re.compile(r"(?P<key>[?&](?:id|user_id|uid|account|order_id)=)(?P<id>\d+)", re.I),
]


def auth_session_inventory(headers_a: dict[str, str] | None = None, headers_b: dict[str, str] | None = None) -> dict[str, Any]:
    def summarize(headers: dict[str, str] | None) -> dict[str, Any]:
        h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        return {
            "has_authorization": "authorization" in h,
            "has_cookie": "cookie" in h,
            "cookie_names": [p.split("=", 1)[0].strip() for p in h.get("cookie", "").split(";") if "=" in p][:20],
            "auth_scheme": h.get("authorization", "").split(" ", 1)[0] if h.get("authorization") else "",
        }
    return {"ok": True, "session_a": summarize(headers_a), "session_b": summarize(headers_b)}


def mutate_object_id(url: str, step: int = 1) -> dict[str, Any]:
    for pat in ID_PATTERNS:
        m = pat.search(url)
        if not m:
            continue
        try:
            current = int(m.group("id"))
        except Exception:
            continue
        replacement = str(max(0, current + int(step or 1)))
        start, end = m.span("id")
        return {"ok": True, "url": url[:start] + replacement + url[end:], "original_id": current, "new_id": replacement}
    return {"ok": False, "error": "no numeric object id found", "url": url}


async def idor_compare_request(url: str, *, scope: str | list[str] | None = None, headers_a: dict[str, str] | None = None, headers_b: dict[str, str] | None = None, alt_url: str = "") -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    target_url = alt_url or mutate_object_id(url).get("url") or ""
    if not target_url or not in_scope(target_url, normalize_scope(scope)):
        return {"ok": False, "error": "alternate URL missing or out of scope", "url": target_url}
    async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=True) as client:
        try:
            own = await client.get(url, headers=headers_a or None)
            other = await client.get(target_url, headers=headers_b or headers_a or None)
        except Exception as e:
            return {"ok": False, "error": str(e), "url": url}
    suspected = other.status_code == 200 and other.text and other.text != own.text
    return {
        "ok": True,
        "suspected": bool(suspected),
        "url": url,
        "alt_url": target_url,
        "status_a": own.status_code,
        "status_b": other.status_code,
        "len_a": len(own.text),
        "len_b": len(other.text),
        "evidence": evidence_record(
            kind="idor",
            surface="object_id",
            url=target_url,
            status_code=other.status_code,
            response_excerpt=response_excerpt(other.text),
            confidence="suspected" if suspected else "negative",
            test_case=f"{curl_repro(url)}\n{curl_repro(target_url)}",
        ),
    }


async def role_matrix_probe(urls: list[str] | str, *, scope: str | list[str] | None = None, roles_json: str = "{}") -> dict[str, Any]:
    import json

    url_list = [u.strip() for u in (urls if isinstance(urls, list) else str(urls).splitlines()) if u.strip()]
    try:
        roles = json.loads(roles_json or "{}")
    except Exception:
        roles = {}
    results = []
    async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=False) as client:
        for url in url_list[:20]:
            if not in_scope(url, normalize_scope(scope)):
                results.append({"url": url, "ok": False, "error": "out of scope"})
                continue
            row = {"url": url, "roles": {}}
            for role, headers in (roles or {"anonymous": {}}).items():
                try:
                    r = await client.get(url, headers=headers or None)
                    row["roles"][role] = {"status": r.status_code, "length": len(r.text)}
                except Exception as e:
                    row["roles"][role] = {"error": str(e)}
            results.append(row)
    return {"ok": True, "results": results}


def business_state_probe(steps_json: str = "[]") -> dict[str, Any]:
    import json

    try:
        steps = json.loads(steps_json or "[]")
    except Exception:
        steps = []
    return {
        "ok": True,
        "steps": steps,
        "checklist": [
            "Replay steps out of order.",
            "Repeat terminal steps such as pay/redeem/submit.",
            "Change object ids between steps.",
            "Compare roles before and after state transitions.",
        ],
    }

