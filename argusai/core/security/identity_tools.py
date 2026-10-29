"""CORS, CSRF, OAuth/OIDC/SAML/JWT analysis helpers."""

from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from argusai.core.infra.command import in_scope, normalize_scope


CSRF_TOKEN_RE = re.compile(r"name=[\"']?(csrf|_csrf|csrf_token|authenticity_token|token)[\"']?", re.I)
_SAFE_CORS_METHODS = {"GET", "POST", "HEAD"}


async def cors_matrix_probe(url: str, *, scope: str | list[str] | None = None, origins: list[str] | str = "") -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    origin_list = origins if isinstance(origins, list) else [o.strip() for o in str(origins or "").splitlines() if o.strip()]
    if not origin_list:
        origin_list = ["https://evil.example", "null"]
    rows = []
    async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=False) as client:
        for origin in origin_list[:10]:
            try:
                r = await client.get(url, headers={"Origin": origin})
            except Exception as e:
                rows.append({"origin": origin, "error": str(e)})
                continue
            acao = r.headers.get("access-control-allow-origin", "")
            acac = r.headers.get("access-control-allow-credentials", "")
            rows.append({
                "origin": origin,
                "status": r.status_code,
                "acao": acao,
                "acac": acac,
                "credentialed_read_risk": acao == origin and acac.lower() == "true",
                "wildcard_with_credentials_invalid": acao == "*" and acac.lower() == "true",
                "risky": acao == origin and acac.lower() == "true",
            })
    return {"ok": True, "url": url, "results": rows, "suspected": any(r.get("risky") for r in rows)}


def _parse_json_object(value: str | dict[str, Any] | None) -> dict[str, str]:
    if isinstance(value, dict):
        data = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
    return {str(k): str(v) for k, v in data.items() if str(k).strip()}


def _origin_url(origin: str) -> tuple[str, str]:
    value = (origin or "").strip()
    if value.lower() == "null":
        return "null", "data:text/html," + quote(_attacker_html())
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        value = "https://evil.example"
        parsed = urlparse(value)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, base + "/__argusai_cors_verify__"


def _attacker_html() -> str:
    return """<!doctype html>
<meta charset="utf-8">
<title>ArgusAI CORS verifier</title>
<h1>ArgusAI CORS verifier</h1>
<p>This local test page simulates an attacker-controlled origin.</p>"""


def _cookie_header_to_cookies(cookie: str, target_url: str, same_site: str = "None") -> list[dict[str, Any]]:
    parsed_url = urlparse(target_url)
    secure = parsed_url.scheme == "https"
    same_site_value = (same_site or "None").strip().capitalize()
    if same_site_value not in {"None", "Lax", "Strict"}:
        same_site_value = "None"
    # Chromium rejects SameSite=None cookies without Secure. For plain HTTP
    # targets, fall back to Lax instead of failing the whole verification run.
    effective_same_site = "Lax" if same_site_value == "None" and not secure else same_site_value
    cookies = []
    for part in str(cookie or "").split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        item: dict[str, Any] = {
            "url": target_url,
            "name": name,
            "value": value.strip(),
            "secure": secure,
            "sameSite": effective_same_site,
        }
        cookies.append(item)
    return cookies[:20]


async def _cors_header_probe(
    url: str,
    *,
    origin: str,
    method: str,
    headers: dict[str, str],
    cookie: str = "",
    data: str = "",
) -> dict[str, Any]:
    request_headers = dict(headers)
    request_headers["Origin"] = origin
    if cookie:
        request_headers.setdefault("Cookie", cookie)
    try:
        async with httpx.AsyncClient(timeout=15, verify=False, follow_redirects=False) as client:
            r = await client.request(method, url, headers=request_headers, content=data if data else None)
    except Exception as e:
        return {"ok": False, "error": str(e), "origin": origin}
    acao = r.headers.get("access-control-allow-origin", "")
    acac = r.headers.get("access-control-allow-credentials", "")
    return {
        "ok": True,
        "origin": origin,
        "status": r.status_code,
        "acao": acao,
        "acac": acac,
        "credentialed_read_risk": acao == origin and acac.lower() == "true",
        "wildcard_with_credentials_invalid": acao == "*" and acac.lower() == "true",
        "content_type": r.headers.get("content-type", ""),
        "content_length": r.headers.get("content-length", ""),
    }


async def cors_exploit_verify(
    url: str,
    *,
    scope: str | list[str] | None = None,
    attacker_origin: str = "https://evil.example",
    method: str = "GET",
    headers_json: str | dict[str, Any] = "",
    data: str = "",
    cookie: str = "",
    cookie_same_site: str = "None",
    sensitive_regex: str = "",
    max_body_chars: int = 2000,
    fresh_context: bool = False,
) -> dict[str, Any]:
    """Verify CORS exploitability with a real browser fetch from an attacker origin."""
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    method = (method or "GET").strip().upper()
    if method not in _SAFE_CORS_METHODS:
        return {"ok": False, "error": f"method must be one of {sorted(_SAFE_CORS_METHODS)}", "url": url}
    if method in {"GET", "HEAD"}:
        data = ""
    headers = _parse_json_object(headers_json)
    max_chars = max(200, min(int(max_body_chars or 2000), 20_000))
    origin, attacker_url = _origin_url(attacker_origin)
    header_probe = await _cors_header_probe(
        url,
        origin=origin,
        method=method,
        headers=headers,
        cookie=cookie,
        data=data,
    )

    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "url": url, "error": f"browser hub unavailable: {e}", "header_probe": header_probe}

    async def _run(page: Any) -> dict[str, Any]:
        context = page.context
        injected = _cookie_header_to_cookies(cookie, url, cookie_same_site)
        if injected:
            await context.add_cookies(injected)
        try:
            target_cookies = await context.cookies(url)
        except Exception:
            target_cookies = []

        route_handler = None
        if origin != "null":
            async def route_handler(route: Any) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=_attacker_html(),
                )
            await context.route(attacker_url, route_handler)
        try:
            await page.goto(attacker_url, wait_until="domcontentloaded", timeout=15000)
            result = await page.evaluate(
                """async (args) => {
                  const out = {
                    readable: false,
                    target_url: args.url,
                    attacker_origin: window.location.origin,
                    document_url: document.URL
                  };
                  try {
                    const opts = {
                      method: args.method,
                      mode: "cors",
                      credentials: "include",
                      cache: "no-store",
                      redirect: "follow"
                    };
                    if (Object.keys(args.headers || {}).length) opts.headers = args.headers;
                    if (args.data && !["GET", "HEAD"].includes(args.method)) opts.body = args.data;
                    const response = await fetch(args.url, opts);
                    const text = await response.text();
                    out.readable = true;
                    out.status = response.status;
                    out.ok = response.ok;
                    out.type = response.type;
                    out.redirected = response.redirected;
                    out.final_url = response.url;
                    out.body_length = text.length;
                    out.body_excerpt = text.slice(0, args.maxBodyChars);
                    out.visible_headers = Array.from(response.headers.entries()).slice(0, 50);
                  } catch (e) {
                    out.error_name = e && e.name ? e.name : "";
                    out.error_message = e && e.message ? e.message : String(e);
                  }
                  return out;
                }""",
                {
                    "url": url,
                    "method": method,
                    "headers": headers,
                    "data": data,
                    "maxBodyChars": max_chars,
                },
            )
        finally:
            if route_handler is not None:
                try:
                    await context.unroute(attacker_url, route_handler)
                except Exception:
                    pass
        body_excerpt = str((result or {}).get("body_excerpt") or "")
        sensitive_match = False
        if sensitive_regex:
            try:
                sensitive_match = bool(re.search(sensitive_regex, body_excerpt, re.I | re.S))
            except re.error:
                sensitive_match = False
        elif body_excerpt:
            sensitive_match = bool(re.search(r"password|token|secret|api[_-]?key|email|user(name)?|account", body_excerpt, re.I))
        cookies_available = bool(target_cookies)
        return {
            "browser_result": result,
            "target_cookie_count": len(target_cookies),
            "cookies_available": cookies_available,
            "cookie_names": [str(c.get("name", "")) for c in target_cookies[:20]],
            "cookie_injected": bool(injected),
            "cookie_same_site": cookie_same_site if injected else "",
            "sensitive_signal": sensitive_match,
            "confirmed_exploitable": bool((result or {}).get("readable") and cookies_available and sensitive_match),
            "readable_with_credentials": bool((result or {}).get("readable") and cookies_available),
        }

    hub = get_browser_hub(headless=True)
    try:
        browser_part = await hub.acall_with_page(_run, fresh_context=bool(fresh_context))
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e), "header_probe": header_probe}
    status = hub.status()
    readable = bool((browser_part.get("browser_result") or {}).get("readable"))
    return {
        "ok": True,
        "url": url,
        "attacker_origin": origin,
        "method": method,
        "header_probe": header_probe,
        "browser_source": status.get("resolved_source", ""),
        "browser_label": status.get("resolved_label", ""),
        "readable": readable,
        "confirmed_exploitable": browser_part.get("confirmed_exploitable", False),
        "readable_with_credentials": browser_part.get("readable_with_credentials", False),
        "sensitive_signal": browser_part.get("sensitive_signal", False),
        "evidence": browser_part,
        "decision": (
            "confirmed_exploitable"
            if browser_part.get("confirmed_exploitable")
            else "browser_readable_with_credentials" if browser_part.get("readable_with_credentials")
            else "browser_readable_without_sensitive_signal" if readable
            else "not_browser_readable"
        ),
        "notes": [
            "This uses a real browser fetch from an attacker-controlled origin with credentials='include'.",
            "If cookie was supplied manually, SameSite attributes are simulated from cookie_same_site; attached-browser sessions give stronger evidence.",
        ],
    }


def csrf_poc_generate(action_url: str, method: str = "POST", fields_json: str = "{}") -> dict[str, Any]:
    try:
        fields = json.loads(fields_json or "{}")
    except Exception:
        fields = {}
    inputs = "\n".join(
        f'  <input type="hidden" name="{str(k)}" value="{str(v)}">'
        for k, v in fields.items()
    )
    html = f"""<!doctype html>
<meta charset="utf-8">
<form id="csrf" action="{action_url}" method="{method.upper()}">
{inputs}
</form>
<script>document.getElementById('csrf').submit()</script>"""
    return {"ok": True, "html": html, "action_url": action_url, "method": method.upper()}


def oauth_flow_analyze(url: str = "", html: str = "") -> dict[str, Any]:
    parsed = urlparse(url or "")
    params = parse_qs(parsed.query)
    findings = []
    if "redirect_uri" in params:
        findings.append("redirect_uri_present")
    if "state" not in params and ("response_type" in params or "client_id" in params):
        findings.append("missing_state_parameter")
    if "nonce" not in params and "openid" in " ".join(params.get("scope", [])).lower():
        findings.append("missing_oidc_nonce")
    if re.search(r"response_type=token|implicit", url, re.I):
        findings.append("implicit_flow")
    if html:
        for m in re.finditer(r"https?://[^\"']*(?:authorize|oauth|saml|callback)[^\"']*", html, re.I):
            findings.append(f"auth_url:{m.group(0)[:160]}")
    return {"ok": True, "url": url, "signals": findings, "suspected": any(x.startswith("missing") for x in findings)}


def jwt_attack_surface(token: str = "") -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {"ok": False, "error": "not a JWT"}
    def dec(part: str) -> Any:
        padded = part + "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace"))
    try:
        header = dec(parts[0])
        payload = dec(parts[1])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    alg = str(header.get("alg", ""))
    signals = []
    if alg.lower() == "none":
        signals.append("alg_none")
    if header.get("jku") or header.get("x5u"):
        signals.append("remote_key_url")
    if header.get("kid") and any(x in str(header.get("kid")) for x in ["..", "/", "\\", "http"]):
        signals.append("suspicious_kid")
    return {"ok": True, "header": header, "payload": payload, "signals": signals}


def saml_surface_analyze(xml_or_html: str = "") -> dict[str, Any]:
    text = xml_or_html or ""
    signals = []
    if "<saml" in text.lower() or "SAMLResponse" in text:
        signals.append("saml_present")
    if "Signature" not in text and signals:
        signals.append("signature_not_observed")
    if "NameID" in text:
        signals.append("nameid_present")
    return {"ok": True, "signals": signals, "suspected": "signature_not_observed" in signals}


def csrf_surface_analyze(html: str = "", url: str = "") -> dict[str, Any]:
    forms = re.findall(r"<form\b[^>]*>(.*?)</form>", html or "", re.I | re.S)
    rows = []
    for body in forms:
        has_token = bool(CSRF_TOKEN_RE.search(body))
        rows.append({"has_token": has_token, "input_count": len(re.findall(r"<input\b", body, re.I))})
    return {"ok": True, "url": url, "forms": rows, "missing_token_count": sum(1 for r in rows if not r["has_token"])}

