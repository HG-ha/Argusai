"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *

def browser_profile_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List the built-in browser fingerprint profiles you can pass to browser_probe / browser_evaluate / browser_trace as profile=<name>. Each profile sets UA, locale, timezone, viewport, navigator.platform/vendor, hardwareConcurrency and applies a stealth init script (navigator.webdriver, plugins, permissions.query, WebGL vendor mask). Use this to pick a fingerprint matching the audited app's expected user."""
    from argusai.core.browser.browser_profiles import list_profiles

    profiles = list_profiles()
    return {
        "ok": True,
        "profiles": profiles,
        "random_pool": [
            p["name"] for p in profiles
            if p["name"] in ("desktop_chrome_win", "desktop_chrome_mac", "desktop_chrome_zh", "mobile_android_chrome")
        ],
        "note": "Pass profile='random' to let argusai pick one from random_pool. Pass profile='default' (or omit) to keep Playwright defaults.",
    }


def network_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List installed network tools and platform-specific fallbacks."""
    from argusai.core.security.network_tools import tool_inventory

    return tool_inventory()


async def ping_host(api: Any, target: str = "", scope: str = "", count: int = 4, **kwargs: Any) -> dict[str, Any]:
    """Run scoped ping against an authorized target."""
    from argusai.core.security.network_tools import ping_host as _ping_host

    return await _ping_host(target, scope=scope, count=count)


async def dns_lookup(api: Any, target: str = "", scope: str = "", record_type: str = "A", **kwargs: Any) -> dict[str, Any]:
    """Run scoped DNS lookup for an authorized target."""
    from argusai.core.security.network_tools import dns_lookup as _dns_lookup

    return await _dns_lookup(target, scope=scope, record_type=record_type)


async def trace_route(api: Any, target: str = "", scope: str = "", max_hops: int = 20, **kwargs: Any) -> dict[str, Any]:
    """Run scoped traceroute/mtr/tracert against an authorized target."""
    from argusai.core.security.network_tools import trace_route as _trace_route

    return await _trace_route(target, scope=scope, max_hops=max_hops)


async def http_curl(
    api: Any,
    target: str = "",
    scope: str = "",
    method: str = "GET",
    data: str = "",
    headers: Any = None,
    cookie: str = "",
    follow_redirects: bool = True,
    insecure: bool = False,
    timeout_seconds: int = 30,
    user_agent: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run scoped curl for an authorized URL with optional body, headers and cookies."""
    from argusai.core.security.network_tools import http_curl as _http_curl

    result = await _http_curl(
        target,
        scope=scope,
        method=method,
        data=data,
        headers=headers,
        cookie=cookie,
        follow_redirects=bool(follow_redirects),
        insecure=bool(insecure),
        timeout_seconds=_safe_int(timeout_seconds, 30, 1, 120),
        user_agent=user_agent,
    )
    if isinstance(result, dict):
        body = str(result.get("body") or result.get("text") or result.get("stdout_excerpt") or result.get("body_preview") or "")
        status = result.get("status_code") or result.get("status")
        try:
            status_i = int(status) if status is not None else None
        except Exception:
            status_i = None
        result = _attach_failure_evidence(
            result,
            api=api,
            kwargs=kwargs,
            url=str(result.get("url") or target),
            status_code=status_i,
            body=body,
            headers=result.get("headers") or {},
        )
    return result


async def http_request_impersonate(
    api: Any,
    target: str = "",
    scope: str = "",
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
    **kwargs: Any,
) -> dict[str, Any]:
    """Send an HTTP request mimicking a real browser's TLS+HTTP/2 fingerprint via curl_cffi (JA3/JA4 evasion)."""
    from argusai.core.security.http_impersonate import http_request_impersonate as _impl

    result = await _impl(
        target,
        scope=scope,
        method=method,
        impersonate=impersonate,
        headers=headers,
        cookies=cookies,
        data=data,
        params=params,
        timeout_seconds=float(timeout_seconds or 30.0),
        follow_redirects=bool(follow_redirects),
        verify=bool(verify),
        proxy=proxy,
        user_agent=user_agent,
    )
    if isinstance(result, dict):
        body = str(result.get("body") or result.get("text") or result.get("content") or result.get("body_preview") or "")
        status = result.get("status_code") or result.get("status")
        try:
            status_i = int(status) if status is not None else None
        except Exception:
            status_i = None
        result = _attach_failure_evidence(
            result,
            api=api,
            kwargs=kwargs,
            url=str(result.get("url") or target),
            status_code=status_i,
            body=body,
            headers=result.get("headers") or {},
        )
    return result


def http_impersonate_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report whether curl_cffi (TLS fingerprint impersonation) is available and which profiles are supported."""
    from argusai.core.security.http_impersonate import is_available

    return is_available()


def _redact_headers_for_evidence(headers: Any) -> dict[str, Any]:
    if not isinstance(headers, dict):
        return {}
    sensitive = {"cookie", "authorization", "proxy-authorization", "set-cookie", "x-api-key"}
    out: dict[str, Any] = {}
    for k, v in headers.items():
        key = str(k)
        out[key] = "[redacted]" if key.lower() in sensitive else v
    return out


def _classify_http_failure(url: str, status_code: int | None, body: str = "", error: str = "") -> tuple[str, str]:
    u = str(url or "").lower()
    b = str(body or "").lower()
    e = str(error or "").lower()
    if status_code == 404:
        if "challenge" in u or "risk" in u or "captcha" in u:
            return "challenge_404", "challenge endpoint returned 404; target route likely changed or service is unavailable"
        return "entry_404", "entry URL returned 404; do not treat this as a crypto/reproduction failure"
    if status_code in {401, 403}:
        return "missing_cookie_or_header", "request was rejected as unauthorized/forbidden; cookies, auth headers or browser state may be missing"
    if status_code in {400, 409, 422} and any(x in b or x in e for x in ("sign", "signature", "token", "decrypt", "encrypt", "invalid parameter", "校验", "签名")):
        return "crypto_mismatch", "server rejected signed/encrypted parameters; compare layer oracles before blaming transport"
    if any(x in b for x in ("账号或密码", "account or password", "invalid password", "incorrect password", "user not found")):
        return "auth_business_error", "authentication reached business logic; transport and anti-bot parameters likely passed"
    if error:
        return "transport_error", "request failed before a usable HTTP response was available"
    return "", ""


def _attach_failure_evidence(
    result: dict[str, Any],
    *,
    api: Any,
    kwargs: dict[str, Any],
    url: str,
    status_code: int | None = None,
    body: str = "",
    headers: Any = None,
) -> dict[str, Any]:
    failure_class, summary = _classify_http_failure(url, status_code, body, str(result.get("error") or ""))
    if not failure_class:
        return result
    evidence: dict[str, Any] = {
        "url": str(url or ""),
        "status_code": status_code,
        "headers": _redact_headers_for_evidence(headers),
    }
    text = str(body or "")
    if len(text) <= 8000:
        evidence["body_preview"] = text
    else:
        try:
            p, _kind, _root = _resolve_session_output_path(api, kwargs, "", f"http-evidence-{abs(hash((url, status_code))) & 0xfffffff:x}.txt")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8", errors="replace")
            evidence["body_path"] = str(p)
            evidence["body_size"] = len(text.encode("utf-8", errors="replace"))
        except Exception:
            evidence["body_preview"] = text[:8000]
            evidence["body_truncated"] = True
    result["failure_class"] = failure_class
    result["summary"] = summary
    result["evidence"] = evidence
    return result


async def web_search(api: Any, query: str = "", max_results: int = 5, region: str = "wt-wt", **kwargs: Any) -> dict[str, Any]:
    """Search the public internet (Bing first, Baidu in China, DuckDuckGo fallback): news, writeups, docs, CVE references."""
    from argusai.core.security.web_tools import web_search as _web_search

    return await _web_search(query, max_results=_safe_int(max_results, 5, 1, 10), region=region)


def _github_token_from_env_or_settings(api: Any) -> str:
    """Resolve an optional GitHub token: settings first, then env vars."""
    try:
        mgr = getattr(api, "settings_mgr", None)
        if mgr is not None:
            s = mgr.get()
            for attr in ("github_token", "gh_token"):
                tok = str(getattr(s, attr, "") or "").strip()
                if tok:
                    return tok
    except Exception:
        pass
    import os

    return str(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


async def github_search(
    api: Any,
    query: str = "",
    search_type: str = "repositories",
    max_results: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Search GitHub (repositories/code/issues) via the official API. More precise than web_search for PoCs, vulnerability writeups, affected hosts, and reference code. code search needs a token (GITHUB_TOKEN); repositories/issues work without one."""
    from argusai.core.security.web_tools import github_search as _github_search

    return await _github_search(
        query,
        search_type=search_type,
        max_results=_safe_int(max_results, 10, 1, 30),
        token=_github_token_from_env_or_settings(api),
    )


async def fetch_page_source(api: Any, url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Fetch raw HTML/source for an authorized URL."""
    from argusai.core.security.web_tools import fetch_page_source as _fetch_page_source

    return await _fetch_page_source(url, scope=scope)


async def extract_page_text(api: Any, url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Extract text, links, forms, and scripts from an authorized URL."""
    from argusai.core.security.web_tools import extract_page_text as _extract_page_text

    return await _extract_page_text(url, scope=scope)


def hex_view(api: Any, text: str = "", input_format: str = "text",
             width: int = 16, max_bytes: int = 4096, **kwargs: Any) -> dict[str, Any]:
    from argusai.core.security.web_tools import hex_view as _hex_view

    return _hex_view(
        text=text,
        input_format=input_format,
        width=_safe_int(width, 16, 1, 64),
        max_bytes=_safe_int(max_bytes, 4096, 16, 65536),
    )


def codec_transform(api: Any, text: str = "", operation: str = "base64_decode", **kwargs: Any) -> dict[str, Any]:
    """Encode/decode base64, URL, HTML, and hex strings."""
    from argusai.core.security.web_tools import codec_transform as _codec_transform

    return _codec_transform(text=text, operation=operation)


def hash_text(api: Any, text: str = "", algorithm: str = "sha256", **kwargs: Any) -> dict[str, Any]:
    """Hash text with md5/sha1/sha256/sha512/etc."""
    from argusai.core.security.web_tools import hash_text as _hash_text

    return _hash_text(text=text, algorithm=algorithm)


def hmac_text(api: Any, text: str = "", key: str = "", algorithm: str = "sha256", **kwargs: Any) -> dict[str, Any]:
    """Compute HMAC for text."""
    from argusai.core.security.web_tools import hmac_text as _hmac_text

    return _hmac_text(text=text, key=key, algorithm=algorithm)


def jwt_decode(api: Any, token: str = "", **kwargs: Any) -> dict[str, Any]:
    """Decode JWT header and payload without verifying signature."""
    from argusai.core.security.web_tools import jwt_decode as _jwt_decode

    return _jwt_decode(token=token)


# === Web reverse engineering tools ===

async def js_fetch_asset(
    api: Any,
    url: str = "",
    scope: str = "",
    offset: Any = 1,
    limit: Any = None,
    meta_only: Any = False,
    full: Any = False,
    line_trunc: Any = None,
    save_to_job: str = "",
    artifact_kind: str = "chunk",
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch a scoped JS/CSS/source-map/WASM asset.

    By default this returns ONLY metadata + the first 200 lines of the
    body (line-numbered, like ``read_text_file``). Minified JS chunks
    are routinely 100-500 KB; inlining the whole file into the prompt
    is what makes the assistant slow down over a long session.

    Use the paged form deliberately:

    - ``meta_only=true`` — just size, content_type, sha256, line count.
    - ``offset=<line>, limit=<lines>`` — paginate through the body.
    - ``full=true`` — return the whole body (still capped server-side at
      200 KB chars and at 200 KB by ``content_truncated``). Use sparingly,
      and only after you've used ``meta_only`` to confirm the file is
      reasonably sized.

    For grep-style searches across an asset, fetch with ``meta_only=true``
    first, then use ``js_search`` on the cached body or call
    ``grep_text`` after caching to disk.
    """
    import hashlib
    import time
    import httpx
    from argusai.core.infra.command import in_scope, normalize_scope
    from argusai.core.infra import text_io

    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=25.0, verify=False, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}
    raw = resp.content
    text = raw.decode(resp.encoding or "utf-8", errors="replace")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    meta_only_b = bool(meta_only)
    full_b = bool(full)
    off = text_io._coerce_int(offset, 1, minimum=-max(total_lines, 1) - 1)
    lim = text_io._coerce_int(
        limit,
        text_io.DEFAULT_LIMIT_LINES,
        minimum=1,
        maximum=text_io.MAX_LINES_PER_CALL,
    )
    trunc = text_io._coerce_int(
        line_trunc,
        text_io.DEFAULT_LINE_TRUNC,
        minimum=80,
        maximum=2000,
    )

    base: dict[str, Any] = {
        "ok": 200 <= resp.status_code < 400,
        "url": str(resp.url),
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "elapsed_ms": int((time.time() - started) * 1000),
        "byte_length": len(raw),
        "char_length": len(text),
        "line_count": total_lines,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not base["ok"]:
        _attach_failure_evidence(
            base,
            api=api,
            kwargs=kwargs,
            url=str(resp.url),
            status_code=resp.status_code,
            body=text,
            headers=dict(resp.headers),
        )
    if save_to_job:
        store = _resolve_reverse_store(api)
        if store is not None:
            try:
                art = store.add_artifact(
                    str(save_to_job),
                    content=raw,
                    kind=str(artifact_kind or "chunk"),
                    filename=str(resp.url).rsplit("/", 1)[-1] or "asset.js",
                    url=str(resp.url),
                    tool="js_fetch_asset",
                )
                base["artifact"] = art
                base["saved_to_job"] = str(save_to_job)
            except Exception as e:
                base["save_error"] = str(e)

    if meta_only_b:
        base["mode"] = "meta_only"
        base["hint"] = (
            "Metadata only. Re-call with full=true for small files, or with "
            "offset/limit for paging, or use grep_text on a cached copy."
        )
        return base

    if full_b:
        body = text[:200_000]
        base["mode"] = "full"
        base["content"] = body
        base["content_truncated"] = len(text) > 200_000
        return base

    if total_lines == 0:
        base["mode"] = "paged"
        base["content"] = ""
        base["start_line"] = 0
        base["end_line"] = 0
        base["more"] = False
        return base

    if off < 0:
        start_idx = max(0, total_lines + off)
    elif off == 0:
        start_idx = 0
    else:
        start_idx = off - 1
    start_idx = min(start_idx, max(0, total_lines - 1))
    end_idx = min(total_lines, start_idx + lim)
    sliced = all_lines[start_idx:end_idx]

    content = text_io._format_line_numbered(sliced, start_idx + 1, line_trunc=trunc)
    char_truncated = False
    if len(content) > text_io.MAX_CHARS_PER_CALL:
        content = content[: text_io.MAX_CHARS_PER_CALL] + f"\n...[truncated {len(content) - text_io.MAX_CHARS_PER_CALL} chars]"
        char_truncated = True

    more = end_idx < total_lines
    base["mode"] = "paged"
    base["content"] = content
    base["start_line"] = start_idx + 1
    base["end_line"] = end_idx
    base["more"] = more
    base["truncated"] = char_truncated
    base["hint"] = (
        f"showed lines {start_idx + 1}-{end_idx} of {total_lines}. "
        + (f"Call again with offset={end_idx + 1} for more." if more else "End of file.")
    )
    return base


def js_asset_inventory(api: Any, html: str = "", code: str = "", source_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Inventory scripts, chunks, source maps, workers, WASM, and captcha assets from HTML/JS."""
    import re
    from urllib.parse import urljoin
    from argusai.core.reverse.js_reverse_tools import captcha_detect, discover_source_maps

    text = "\n".join([html or "", code or ""])
    assets = []
    for m in re.finditer(r"""(?:src|href)\s*=\s*['"]([^'"]+)['"]|['"]([^'"]+\.(?:js|css|map|wasm))['"]""", text, re.I):
        raw = m.group(1) or m.group(2) or ""
        url = urljoin(source_url, raw) if source_url else raw
        kind = "wasm" if raw.endswith(".wasm") else "source_map" if raw.endswith(".map") else "style" if raw.endswith(".css") else "script"
        assets.append({"url": url, "kind": kind})
    return {
        "ok": True,
        "source_url": source_url,
        "assets": assets[:200],
        "source_maps": discover_source_maps(source_url, code).get("candidates", []),
        "captcha": captcha_detect(html=html, code=code, source_url=source_url),
    }


def js_bundle_analyze(api: Any, code: str = "", source_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Analyze a JS bundle for bundler type, chunks, source maps, crypto, captcha and obfuscation hints."""
    from argusai.core.reverse.js_reverse_tools import analyze_js_bundle

    return analyze_js_bundle(code=code, source_url=source_url)


def js_beautify(api: Any, code: str = "", indent_size: int = 2, max_chars: int = 200000, **kwargs: Any) -> dict[str, Any]:
    """Beautify minified JavaScript for inspection."""
    from argusai.core.reverse.js_reverse_tools import beautify_js

    return beautify_js(code, indent_size=_safe_int(indent_size, 2, 1, 8), max_chars=_safe_int(max_chars, 200000, 1000, 1000000))


def js_extract_strings(api: Any, code: str = "", max_items: int = 200, **kwargs: Any) -> dict[str, Any]:
    """Extract URLs, parameters, encoded blobs and useful string literals from JavaScript."""
    from argusai.core.reverse.js_reverse_tools import extract_strings

    return extract_strings(code, max_items=_safe_int(max_items, 200, 1, 1000))


def js_search(api: Any, code: str = "", pattern: str = "", case_sensitive: bool = False, limit: int = 50, **kwargs: Any) -> dict[str, Any]:
    """Search a JS bundle with regex/plain text and return contextual windows."""
    from argusai.core.reverse.js_reverse_tools import search_code

    return search_code(code, pattern, case_sensitive=case_sensitive, limit=_safe_int(limit, 50, 1, 200))


def js_slice_context(api: Any, code: str = "", offset: int = 0, symbol: str = "", pattern: str = "", context: int = 1600, **kwargs: Any) -> dict[str, Any]:
    """Slice a bounded JS context window by offset, symbol, or regex pattern."""
    from argusai.core.reverse.js_reverse_tools import slice_context

    return slice_context(code, offset=_safe_int(offset, 0, 0, 1000000), symbol=symbol, pattern=pattern, context=_safe_int(context, 1600, 100, 10000))


def source_map_discover(api: Any, source_url: str = "", code: str = "", **kwargs: Any) -> dict[str, Any]:
    """Discover sourceMappingURL references and common .map candidates."""
    from argusai.core.reverse.js_reverse_tools import discover_source_maps

    return discover_source_maps(source_url=source_url, code=code)


def source_map_recover(api: Any, map_text: str = "", source_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse a source map and list original files, embedded source content, and path leaks."""
    from argusai.core.reverse.js_reverse_tools import recover_sourcemap

    return recover_sourcemap(map_text=map_text, source_url=source_url)


def webpack_modules_extract(api: Any, code: str = "", source_url: str = "", max_modules: int = 80, **kwargs: Any) -> dict[str, Any]:
    """Extract approximate webpack module inventory with offsets, previews and tags."""
    from argusai.core.reverse.js_reverse_tools import extract_webpack_modules

    return extract_webpack_modules(code, source_url=source_url, max_modules=_safe_int(max_modules, 80, 1, 500))


def webpack_module_read(api: Any, code: str = "", module_id: str = "", offset: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Read and beautify one webpack module by id or offset."""
    from argusai.core.reverse.js_reverse_tools import read_webpack_module

    return read_webpack_module(code, module_id=module_id, offset=offset)


def js_deobfuscate(api: Any, code: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run safe static deobfuscation passes and return decoded preview plus hints."""
    from argusai.core.reverse.js_reverse_tools import deobfuscate_js

    return deobfuscate_js(code)


def js_decode_literal(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Decode a JavaScript string literal including unicode, hex, URL and base64 layers."""
    from argusai.core.reverse.js_reverse_tools import decode_literal

    return decode_literal(text)


def js_string_array_map(api: Any, code: str = "", max_items: int = 200, **kwargs: Any) -> dict[str, Any]:
    """Map common obfuscator string arrays and their indexed references."""
    from argusai.core.reverse.js_reverse_tools import string_array_map

    return string_array_map(code, max_items=_safe_int(max_items, 200, 1, 1000))


def js_constant_fold_preview(api: Any, expression: str = "", **kwargs: Any) -> dict[str, Any]:
    """Preview safe literal/arithmetic constant folding without running arbitrary JS."""
    from argusai.core.reverse.js_reverse_tools import constant_fold_preview

    return constant_fold_preview(expression)


def js_control_flow_hints(api: Any, code: str = "", **kwargs: Any) -> dict[str, Any]:
    """Detect obfuscation hints such as string arrays, eval packers, debugger traps and flattening."""
    from argusai.core.reverse.js_reverse_tools import control_flow_hints

    return control_flow_hints(code)


async def js_eval_unpack(api: Any, code: str = "", expression: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run a bounded no-network unpack/eval preview in the JS sandbox."""
    from argusai.core.reverse.js_sandbox import sandbox_eval_js as _sandbox_eval_js

    return await _sandbox_eval_js(code=code, expression=expression or "undefined", timeout_ms=5000)


def js_api_extract(api: Any, code: str = "", source_url: str = "", max_items: int = 120, **kwargs: Any) -> dict[str, Any]:
    """Extract fetch/XHR/axios API candidates from JavaScript."""
    from argusai.core.reverse.js_reverse_tools import api_extract

    return api_extract(code, source_url=source_url, max_items=_safe_int(max_items, 120, 1, 500))


def js_signature_candidates(api: Any, code: str = "", source_url: str = "", max_items: int = 80, **kwargs: Any) -> dict[str, Any]:
    """Locate likely sign/token/nonce/hash/HMAC/encryption call sites."""
    from argusai.core.reverse.js_reverse_tools import signature_candidates

    return signature_candidates(code, source_url=source_url, max_items=_safe_int(max_items, 80, 1, 300))


def js_dependency_trace(api: Any, code: str = "", symbol: str = "", context: int = 2400, **kwargs: Any) -> dict[str, Any]:
    """Trace nearby definitions and references for a variable or function symbol."""
    from argusai.core.reverse.js_reverse_tools import search_code, slice_context

    hits = search_code(code, rf"\b{__import__('re').escape(symbol)}\b", limit=20) if symbol else {"matches": []}
    first = hits.get("matches", [{}])[0].get("offset", 0) if hits.get("matches") else 0
    return {"ok": True, "symbol": symbol, "references": hits.get("matches", []), "context": slice_context(code, offset=first, context=context)}


def js_crypto_detect(api: Any, code: str = "", source_url: str = "", max_items: int = 80, **kwargs: Any) -> dict[str, Any]:
    """Detect client-side crypto libraries and algorithm call sites."""
    from argusai.core.reverse.js_reverse_tools import crypto_detect

    return crypto_detect(code, source_url=source_url, max_items=_safe_int(max_items, 80, 1, 300))


def js_request_rebuild_hint(api: Any, code: str = "", trace_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a replay plan from static JavaScript and optional runtime trace JSON."""
    import json
    from argusai.core.reverse.js_reverse_tools import request_rebuild_hint

    try:
        trace = json.loads(trace_json) if trace_json else {}
    except Exception:
        trace = {}
    return request_rebuild_hint(trace=trace, code=code)


async def browser_trace(api: Any, url: str = "", scope: str = "", headless: bool = True,
                        wait_until: str = "networkidle", wait_for_selector: str = "",
                        extra_wait_ms: int = 0, click_selector: str = "",
                        fresh_context: bool = False, engine: str = "chromium",
                        profile: str = "",
                        channel: str = "",
                        executable_path: str = "",
                        out_path: str = "",
                        save_to_job: str = "",
                        max_events: int = 250,
                        **kwargs: Any) -> dict[str, Any]:
    """Trace runtime network, storage, crypto, canvas and DOM activity in a scoped browser page. Pass profile=..., channel=..., executable_path=... like browser_probe to control the fingerprint and the actual browser binary."""
    from argusai.core.security.probes import browser_trace as _browser_trace

    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    result = await _browser_trace(
        url,
        scope=scope,
        headless=headless,
        wait_until=wait_until,
        wait_for_selector=wait_for_selector,
        extra_wait_ms=_safe_int(extra_wait_ms, 0, 0, 10000),
        click_selector=click_selector,
        fresh_context=fresh_context,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
        max_events=_safe_int(max_events, 250, 1, 100_000),
    )
    if isinstance(result, dict):
        status = result.get("status_code")
        try:
            status_i = int(status) if status is not None else None
        except Exception:
            status_i = None
        if (status_i and status_i >= 400) or not result.get("ok"):
            if status_i and status_i >= 400:
                result["ok"] = False
            _attach_failure_evidence(
                result,
                api=api,
                kwargs=kwargs,
                url=str(result.get("final_url") or result.get("url") or url),
                status_code=status_i,
                body="",
                headers={},
            )
    if result.get("ok") and (out_path or save_to_job):
        import json
        def _trace_jsonl(res: dict[str, Any]) -> str:
            rows: list[dict[str, Any]] = [{
                "kind": "meta",
                "url": res.get("url"),
                "final_url": res.get("final_url"),
                "status_code": res.get("status_code"),
                "elapsed_ms": res.get("elapsed_ms"),
                "state": res.get("state"),
            }]
            rows.extend({"kind": "request", **r} for r in res.get("requests") or [])
            rows.extend({"kind": "response", **r} for r in res.get("responses") or [])
            rows.extend({"kind": "runtime", **r} for r in res.get("trace") or [])
            rows.extend({"kind": "console", "message": m} for m in res.get("console") or [])
            return "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows) + "\n"
        payload = _trace_jsonl(result)
        if out_path:
            try:
                p, _kind, _root = _resolve_session_output_path(api, kwargs, out_path, "browser_trace.jsonl")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(payload, encoding="utf-8")
                result["out_path"] = str(p)
            except Exception as e:
                result["out_path_error"] = str(e)
        if save_to_job:
            store = _resolve_reverse_store(api)
            if store is not None:
                try:
                    result["artifact"] = store.add_artifact(
                        str(save_to_job),
                        content=payload,
                        kind="trace",
                        filename="browser_trace.jsonl",
                        url=url,
                        tool="browser_trace",
                    )
                except Exception as e:
                    result["save_error"] = str(e)
    return result


async def browser_trace_crypto(api: Any, url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Trace runtime crypto/codec/nonce activity in a scoped browser page."""
    result = await browser_trace(api, url=url, scope=scope, extra_wait_ms=_safe_int(kwargs.get("extra_wait_ms", 2000), 2000, 0, 10000), fresh_context=kwargs.get("fresh_context", False))
    if result.get("ok"):
        result["crypto_trace"] = [t for t in result.get("trace", []) if t.get("kind") in {"webcrypto", "codec"}]
    return result


async def browser_trace_form(api: Any, url: str = "", scope: str = "", click_selector: str = "", **kwargs: Any) -> dict[str, Any]:
    """Trace forms, hidden fields, submits and frontend validation in a scoped page."""
    result = await browser_trace(api, url=url, scope=scope, click_selector=click_selector, extra_wait_ms=_safe_int(kwargs.get("extra_wait_ms", 2000), 2000, 0, 10000), fresh_context=kwargs.get("fresh_context", False))
    if result.get("ok"):
        result["form_trace"] = [t for t in result.get("trace", []) if t.get("kind") in {"event_listener", "dom_mutation", "fetch", "xhr"}]
    return result


async def browser_snapshot_state(api: Any, url: str = "", scope: str = "", expression: str = "", **kwargs: Any) -> dict[str, Any]:
    """Snapshot window globals, storage, cookies and hydration state from a scoped page."""
    expr = expression or """() => ({
      title: document.title,
      url: location.href,
      globals: Object.keys(window).filter(k => /^(__|webpack|vue|react|nuxt|next|captcha|gt|grecaptcha|hcaptcha|turnstile|app|store)/i.test(k)).slice(0, 120),
      localStorage: Object.fromEntries(Object.entries(localStorage || {}).slice(0, 80)),
      sessionStorage: Object.fromEntries(Object.entries(sessionStorage || {}).slice(0, 80)),
      cookies: document.cookie,
      scripts: Array.from(document.scripts).map(s => s.src || '').filter(Boolean).slice(0, 100)
    })"""
    return await browser_evaluate(api, url=url, scope=scope, expression=expr, fresh_context=kwargs.get("fresh_context", False), extra_wait_ms=_safe_int(kwargs.get("extra_wait_ms", 0), 0, 0, 10000))


async def browser_instrument_click(api: Any, url: str = "", scope: str = "", click_selector: str = "", **kwargs: Any) -> dict[str, Any]:
    """Click a scoped page selector and return network/DOM/runtime trace after the action."""
    return await browser_trace(api, url=url, scope=scope, click_selector=click_selector, extra_wait_ms=_safe_int(kwargs.get("extra_wait_ms", 3000), 3000, 0, 10000), fresh_context=kwargs.get("fresh_context", False))


def browser_network_replay_plan(api: Any, trace_json: str = "", code: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a replay template from browser_trace output and optional JavaScript source."""
    return js_request_rebuild_hint(api, code=code, trace_json=trace_json)


def captcha_detect(api: Any, html: str = "", code: str = "", source_url: str = "", runtime_trace_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Detect captcha and anti-automation surfaces from HTML/JS/runtime trace."""
    import json
    from argusai.core.reverse.js_reverse_tools import captcha_detect as _captcha_detect

    try:
        trace = json.loads(runtime_trace_json) if runtime_trace_json else {}
    except Exception:
        trace = {}
    return _captcha_detect(html=html, code=code, source_url=source_url, runtime_trace=trace)


def captcha_script_analyze(api: Any, html: str = "", code: str = "", source_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Locate captcha scripts, sitekeys, callbacks and frontend validation clues."""
    from argusai.core.reverse.js_reverse_tools import captcha_detect, search_code

    return {
        "ok": True,
        "captcha": captcha_detect(html=html, code=code, source_url=source_url),
        "contexts": search_code("\n".join([html, code]), "captcha|recaptcha|hcaptcha|turnstile|geetest|sitekey|callback", limit=40).get("matches", []),
    }


async def captcha_runtime_trace(api: Any, url: str = "", scope: str = "", click_selector: str = "", **kwargs: Any) -> dict[str, Any]:
    """Trace captcha token generation and related network/DOM events in a scoped page."""
    return await browser_trace(api, url=url, scope=scope, click_selector=click_selector, extra_wait_ms=3000, fresh_context=kwargs.get("fresh_context", False))


def anti_bot_fingerprint_detect(api: Any, code: str = "", html: str = "", **kwargs: Any) -> dict[str, Any]:
    """Detect webdriver/canvas/webgl/audio/devtools/timezone/plugin fingerprinting code."""
    from argusai.core.reverse.js_reverse_tools import anti_bot_fingerprint_detect as _detect

    return {"ok": True, **_detect(code=code, html=html)}


def captcha_report(api: Any, html: str = "", code: str = "", runtime_trace_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Summarize captcha evidence, key parameters, frontend-only signals and manual test steps."""
    cap = captcha_detect(api, html=html, code=code, runtime_trace_json=runtime_trace_json)
    return {
        "ok": True,
        "type": "captcha_report",
        "detected": cap.get("detected", []),
        "sitekeys": cap.get("sitekeys", []),
        "callbacks": cap.get("callbacks", []),
        "anti_bot": cap.get("anti_bot", {}),
        "manual_steps": [
            "Identify provider script and sitekey/appid.",
            "Trace callback/token assignment with browser_trace or captcha_runtime_trace.",
            "Check whether server verifies the token; do not assume frontend-only validation is bypassable.",
        ],
    }


async def sandbox_eval_js(api: Any, code: str = "", expression: str = "", inputs_json: str = "", timeout_ms: int = 5000, **kwargs: Any) -> dict[str, Any]:
    """Evaluate JavaScript in a no-network lightweight browser sandbox."""
    import json
    from argusai.core.reverse.js_sandbox import sandbox_eval_js as _sandbox_eval_js

    try:
        inputs = json.loads(inputs_json) if inputs_json else None
    except Exception:
        inputs = inputs_json
    return await _sandbox_eval_js(code=code, expression=expression, inputs=inputs, timeout_ms=_safe_int(timeout_ms, 5000, 1000, 30000))


async def sandbox_test_function(api: Any, code: str = "", function_name: str = "", cases_json: str = "[]", timeout_ms: int = 5000, **kwargs: Any) -> dict[str, Any]:
    """Batch-test a named JavaScript function in a no-network lightweight browser sandbox."""
    import json
    from argusai.core.reverse.js_sandbox import sandbox_test_function as _sandbox_test_function

    try:
        cases = json.loads(cases_json or "[]")
    except Exception:
        cases = []
    return await _sandbox_test_function(code=code, function_name=function_name, cases=cases, timeout_ms=_safe_int(timeout_ms, 5000, 1000, 30000))


async def sandbox_compare_outputs(api: Any, code: str = "", function_name: str = "", cases_json: str = "[]", repeats: int = 2, **kwargs: Any) -> dict[str, Any]:
    """Run a JS function repeatedly to check whether outputs are stable or nonce/time dependent."""
    import json
    from argusai.core.reverse.js_sandbox import sandbox_compare_outputs as _sandbox_compare_outputs

    try:
        cases = json.loads(cases_json or "[]")
    except Exception:
        cases = []
    return await _sandbox_compare_outputs(code=code, function_name=function_name, cases=cases, repeats=_safe_int(repeats, 2, 1, 5))


async def sandbox_load_library(api: Any, library_code: str = "", code: str = "", expression: str = "", inputs_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Load a JS library snippet in the no-network sandbox and evaluate an expression against it."""
    import json
    from argusai.core.reverse.js_sandbox import sandbox_eval_js as _sandbox_eval_js

    try:
        inputs = json.loads(inputs_json) if inputs_json else None
    except Exception:
        inputs = inputs_json
    return await _sandbox_eval_js(
        code=code,
        expression=expression or "undefined",
        inputs=inputs,
        libraries=[library_code] if library_code else [],
        timeout_ms=_safe_int(kwargs.get("timeout_ms", 5000), 5000, 1000, 30000),
    )


def sandbox_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report lightweight JS sandbox status."""
    from argusai.core.reverse.js_sandbox import sandbox_status as _sandbox_status

    return _sandbox_status()


async def sandbox_reset(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Reset the shared browser context used by the lightweight JS sandbox."""
    from argusai.core.reverse.js_sandbox import sandbox_reset as _sandbox_reset

    return await _sandbox_reset()


def json_path_extract(api: Any, text: str = "", path: str = "$", **kwargs: Any) -> dict[str, Any]:
    """Extract a value from JSON using a simple dot/bracket path."""
    from argusai.core.reverse.js_reverse_tools import json_path_extract as _json_path_extract

    return _json_path_extract(text, path)


def querystring_parse_build(api: Any, text: str = "", update_key: str = "", update_value: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse or rebuild a query string/form body."""
    from argusai.core.reverse.js_reverse_tools import parse_querystring

    return parse_querystring(text, update_key=update_key, update_value=update_value)


def header_cookie_parse(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse HTTP headers, Cookie and Set-Cookie values."""
    from argusai.core.reverse.js_reverse_tools import parse_headers_and_cookies

    return parse_headers_and_cookies(text)


def wasm_inventory(api: Any, data: str = "", input_format: str = "base64", **kwargs: Any) -> dict[str, Any]:
    """Inventory a WASM binary from base64 or hex input."""
    from argusai.core.reverse.js_reverse_tools import wasm_inventory as _wasm_inventory

    return _wasm_inventory(data, input_format=input_format)


def har_import_analyze(api: Any, har_text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Analyze browser HAR requests for APIs, signatures and replay hints."""
    from argusai.core.reverse.js_reverse_tools import har_import_analyze as _har_import_analyze

    return _har_import_analyze(har_text)


def js_ast_inventory(api: Any, code: str = "", **kwargs: Any) -> dict[str, Any]:
    """Inventory JavaScript functions, imports, calls, APIs and crypto signals."""
    from argusai.core.reverse.js_reverse_tools import js_ast_inventory as _tool

    return _tool(code)


def js_ast_string_array_decode(api: Any, code: str = "", max_items: int = 200, **kwargs: Any) -> dict[str, Any]:
    """Decode simple JavaScript string-array obfuscation references."""
    from argusai.core.reverse.js_reverse_tools import js_ast_string_array_decode as _tool

    return _tool(code, max_items=_safe_int(max_items, 200, 1, 500))


def js_function_dependency_graph(api: Any, code: str = "", function_name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build a heuristic JavaScript function dependency graph."""
    from argusai.core.reverse.js_reverse_tools import js_function_dependency_graph as _tool

    return _tool(code, function_name=function_name)


def js_hook_template_generate(api: Any, function_name: str = "", expression: str = "", **kwargs: Any) -> dict[str, Any]:
    """Generate a DevTools hook template for a client-side function."""
    from argusai.core.reverse.js_reverse_tools import js_hook_template_generate as _tool

    return _tool(function_name=function_name, expression=expression)


def wasm_decompile_hint(api: Any, data: str = "", input_format: str = "base64", **kwargs: Any) -> dict[str, Any]:
    """Analyze WASM bytes and suggest a decompilation workflow."""
    from argusai.core.reverse.js_reverse_tools import wasm_decompile_hint as _tool

    return _tool(data=data, input_format=input_format)


# ---------------------------------------------------------------------------
# Path-based WASM tools — full P5 toolchain. These all take a filesystem
# path to a .wasm file and emit artifacts to disk so megabyte-sized
# disassembly never has to ride the chat context.
#
# Typical flow:
#   1) browser_save_wasm(out_path='...', expression='globalThis.__argusai_wasm_samples[0]')
#      — captures the bytes from a hooked WebAssembly.instantiate.
#   2) wasm_inspect(path) — instant metadata triage.
#   3) wasm_disassemble(path) and/or wasm_decompile(path) — full text
#      view written next to the source.
#   4) wasm_strings(path) — find embedded strings.
#   5) wasm_call_graph(path) — find dispatchers / hotspots.
# ---------------------------------------------------------------------------


def wasm_inspect(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Triage probe for a .wasm file on disk. Pure Python — never runs
    WABT. Returns magic / version / sha256 / size, section table,
    import / export inventory, and counts for everything else. Always
    cheap to call.

    Use this BEFORE wasm_disassemble / wasm_decompile to confirm the
    file is actually a WASM module and to see what's worth looking at
    (e.g. zero imports → likely standalone; many imports under module
    "env" → emscripten-shaped).
    """
    from argusai.core.reverse import wasm_tools as _wt

    try:
        resolved_path = _resolve_session_input_path(api, kwargs, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return _wt.wasm_inspect(str(resolved_path))


def wasm_disassemble(
    api: Any,
    path: str = "",
    out_path: str = "",
    fold_exprs: bool = True,
    no_check: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Disassemble a .wasm file to WAT (text format) via the vendored
    wasm2wat tool. Writes the WAT next to the input (or to ``out_path``)
    and returns a preview + path — never the full body.

    ``fold_exprs=true`` (default) folds the stack-machine into nested
    expressions, which is much easier to read. ``no_check=true``
    accepts modules that fail strict validation — useful for
    obfuscated bundles.

    For the readable C-like view prefer ``wasm_decompile``; WAT is the
    canonical low-level form, decomp is much friendlier for serious RE.
    """
    from argusai.core.reverse import wasm_tools as _wt

    try:
        resolved_path = _resolve_session_input_path(api, kwargs, path)
        resolved_out = _resolve_session_output_path(api, kwargs, out_path)[0] if out_path else None
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return _wt.wasm_disassemble(
        str(resolved_path),
        out_path=(str(resolved_out) if resolved_out else None),
        fold_exprs=bool(fold_exprs),
        no_check=bool(no_check),
        api=api,
    )


def wasm_decompile(
    api: Any,
    path: str = "",
    out_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Decompile a .wasm file to a C-like surface form via the vendored
    wasm-decompile tool. Writes the result next to the input (or to
    ``out_path``) and returns a preview + path.

    This is usually the FIRST view you want for any non-trivial module
    — WAT is a fine ground truth but decompiled output is what makes
    a reverse-engineering session productive.
    """
    from argusai.core.reverse import wasm_tools as _wt

    try:
        resolved_path = _resolve_session_input_path(api, kwargs, path)
        resolved_out = _resolve_session_output_path(api, kwargs, out_path)[0] if out_path else None
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return _wt.wasm_decompile(
        str(resolved_path),
        out_path=(str(resolved_out) if resolved_out else None),
        api=api,
    )


def wasm_strings(
    api: Any,
    path: str = "",
    min_len: int = 6,
    max_results: int = 200,
    where: str = "data",
    out_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Pull printable strings out of a .wasm file — strings(1)-style
    for WASM. Pure Python; no WABT dependency.

    ``where='data'`` (default) only walks the data section — these are
    the actual string constants the module ships. Use ``where='all'``
    to scan every byte (more noise, but useful if the binary uses an
    unusual encoder).

    Full string list is written to ``<wasm>.strings.txt`` if the result
    set hits ``max_results``; the chat response only shows the first
    ``STRINGS_PREVIEW`` (100) entries.
    """
    from argusai.core.reverse import wasm_tools as _wt

    try:
        resolved_path = _resolve_session_input_path(api, kwargs, path)
        resolved_out = _resolve_session_output_path(api, kwargs, out_path)[0] if out_path else None
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return _wt.wasm_strings(
        str(resolved_path),
        min_len=_safe_int(min_len, 6, 2, 256),
        max_results=_safe_int(max_results, 200, 1, 5000),
        where=str(where or "data"),
        out_path=(str(resolved_out) if resolved_out else None),
    )


def wasm_call_graph(
    api: Any,
    path: str = "",
    out_path: str = "",
    max_edges_preview: int = 80,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a function-level call graph for a .wasm file. Pure Python —
    walks the code section and emits caller→callee edges.

    Names are resolved when possible:

    - Imported functions → ``<module>.<name>`` from the import section.
    - Exported functions → ``export:<name>``.
    - Other functions → ``func:<global_index>``.
    - Indirect calls → ``indirect:?``.

    Writes the full graph as JSON next to the input plus a human-readable
    markdown summary (hotspots, exports, imports). The chat response
    only carries a small preview + the artifact paths.
    """
    from argusai.core.reverse import wasm_tools as _wt

    try:
        resolved_path = _resolve_session_input_path(api, kwargs, path)
        resolved_out = _resolve_session_output_path(api, kwargs, out_path)[0] if out_path else None
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return _wt.wasm_call_graph(
        str(resolved_path),
        out_path=(str(resolved_out) if resolved_out else None),
        max_edges_preview=_safe_int(max_edges_preview, 80, 5, 500),
    )


async def sqli_probe_param(api: Any, url: str = "", param: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Probe a URL parameter for SQL error-based injection signals."""
    from argusai.core.security.sqli_tools import sqli_probe_param as _tool

    return await _tool(url, param, scope=scope)


async def sqli_boolean_diff(api: Any, url: str = "", param: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Compare boolean SQL injection true/false payload response differences."""
    from argusai.core.security.sqli_tools import sqli_boolean_diff as _tool

    return await _tool(url, param, scope=scope)


async def sqli_time_probe(api: Any, url: str = "", param: str = "", scope: str = "", delay: int = 3, **kwargs: Any) -> dict[str, Any]:
    """Probe a URL parameter for short time-based SQL injection signals."""
    from argusai.core.security.sqli_tools import sqli_time_probe as _tool

    return await _tool(url, param, scope=scope, delay=_safe_int(delay, 3, 1, 5))


def sqlmap_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report managed sqlmap checkout status."""
    from argusai.core.security.sqlmap_integration import sqlmap_status as _tool

    return _tool()


async def sqlmap_install_update(api: Any, update: bool = True, **kwargs: Any) -> dict[str, Any]:
    """Clone or update ArgusAI's managed sqlmap checkout from GitHub."""
    from argusai.core.security.sqlmap_integration import install_or_update_sqlmap

    return await install_or_update_sqlmap(update=bool(update))


def sqlmap_plan_scan(api: Any, url: str = "", scope: str = "", parameter: str = "", method: str = "GET",
                     data: str = "", cookie: str = "", headers: str = "", extra_args: list[str] | str | None = None,
                     allow_enumeration: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Build a controlled managed-sqlmap command plan without executing it."""
    from argusai.core.security.sqlmap_integration import sqlmap_plan_scan as _tool

    return _tool(
        url,
        scope=scope,
        parameter=parameter,
        method=method,
        data=data,
        cookie=cookie,
        headers=headers,
        extra_args=extra_args,
        allow_enumeration=allow_enumeration,
    )


async def sqlmap_run_scan(api: Any, url: str = "", scope: str = "", parameter: str = "", method: str = "GET",
                          data: str = "", cookie: str = "", headers: str = "", extra_args: list[str] | str | None = None,
                          allow_enumeration: bool = False, timeout_seconds: int = 180, **kwargs: Any) -> dict[str, Any]:
    """Run ArgusAI's managed sqlmap checkout with safety policy and output parsing."""
    from argusai.core.security.sqlmap_integration import sqlmap_run_scan as _tool

    return await _tool(
        url,
        scope=scope,
        parameter=parameter,
        method=method,
        data=data,
        cookie=cookie,
        headers=headers,
        extra_args=extra_args,
        allow_enumeration=allow_enumeration,
        timeout_seconds=_safe_int(timeout_seconds, 180, 10, 1800),
    )


def web_scan_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report nuclei/ffuf availability and bundled discovery wordlists."""
    from argusai.core.security.web_scan_tools import web_scan_tool_inventory as _tool

    return _tool()


def nuclei_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report whether nuclei is installed on PATH."""
    from argusai.core.security.web_scan_tools import nuclei_status as _tool

    return _tool()


def ffuf_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report whether ffuf is installed on PATH."""
    from argusai.core.security.web_scan_tools import ffuf_status as _tool

    return _tool()


def nuclei_plan_scan(
    api: Any,
    url: str = "",
    scope: str = "",
    tags: str = "",
    severity: str = "medium,high,critical",
    rate_limit: int = 50,
    extra_args: list[str] | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a scoped nuclei command plan without executing it."""
    from argusai.core.security.web_scan_tools import nuclei_plan_scan as _tool

    return _tool(
        url,
        scope=scope,
        tags=tags,
        severity=severity,
        rate_limit=_safe_int(rate_limit, 50, 1, 150),
        extra_args=extra_args,
    )


async def nuclei_run_scan(
    api: Any,
    url: str = "",
    scope: str = "",
    tags: str = "",
    severity: str = "medium,high,critical",
    rate_limit: int = 50,
    extra_args: list[str] | str | None = None,
    timeout_seconds: int = 180,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run nuclei against one authorized URL with JSONL output parsing."""
    from argusai.core.security.web_scan_tools import nuclei_run_scan as _tool

    return await _tool(
        url,
        scope=scope,
        tags=tags,
        severity=severity,
        rate_limit=_safe_int(rate_limit, 50, 1, 150),
        extra_args=extra_args,
        timeout_seconds=_safe_int(timeout_seconds, 180, 30, 900),
    )


def ffuf_plan_scan(
    api: Any,
    url: str = "",
    scope: str = "",
    wordlist: str = "",
    match_codes: str = "200,204,301,302,307,401,403,405",
    rate: int = 50,
    extra_args: list[str] | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a scoped ffuf content-discovery plan without executing it."""
    from argusai.core.security.web_scan_tools import ffuf_plan_scan as _tool

    return _tool(
        url,
        scope=scope,
        wordlist=wordlist,
        match_codes=match_codes,
        rate=_safe_int(rate, 50, 1, 150),
        extra_args=extra_args,
    )


async def ffuf_run_scan(
    api: Any,
    url: str = "",
    scope: str = "",
    wordlist: str = "",
    match_codes: str = "200,204,301,302,307,401,403,405",
    rate: int = 50,
    extra_args: list[str] | str | None = None,
    timeout_seconds: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run ffuf directory/content discovery against one authorized URL."""
    from argusai.core.security.web_scan_tools import ffuf_run_scan as _tool

    return await _tool(
        url,
        scope=scope,
        wordlist=wordlist,
        match_codes=match_codes,
        rate=_safe_int(rate, 50, 1, 150),
        extra_args=extra_args,
        timeout_seconds=_safe_int(timeout_seconds, 120, 30, 900),
    )


def sqli_union_plan(api: Any, max_columns: int = 8, **kwargs: Any) -> dict[str, Any]:
    """Generate safe UNION SELECT column-count payloads."""
    from argusai.core.security.sqli_tools import sqli_union_plan as _tool

    return _tool(_safe_int(max_columns, 8, 1, 20))


def nosql_injection_probe(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Return NoSQL injection payload inventory and testing guidance."""
    from argusai.core.security.sqli_tools import nosql_payloads

    return nosql_payloads()


def oob_payload_generate(api: Any, category: str = "generic", callback_base: str = "", probe_id: str = "", request_ref: str = "", case_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Generate ready-to-use out-of-band callback payloads.

    In ``auto`` mode (default), the host returned is a live Interactsh
    subdomain (e.g. ``ab1c2d3e.cmw7xy.oast.fun``). Any DNS lookup, HTTP/HTTPS
    request, or SMTP connection to that host gets logged as an event and can
    be read back with ``oob_check_events(token=<the short label>)``. Use this
    for blind SSRF / blind SQLi (LOAD_FILE, xp_dirtree, dblink) / blind XXE /
    blind RCE / blind XSS where there is no in-band feedback.

    Auto mode is lazy — the first call to this tool will silently register
    a session with Project Discovery's Interactsh service. If that fails, the
    tool falls back to legacy placeholder behaviour and reports the reason.
    """
    from argusai.core.security.oob import oob_payload_generate as _tool

    return _tool(category=category, callback_base=callback_base, probe_id=probe_id, request_ref=request_ref, case_id=case_id)


def oob_check_events(
    api: Any,
    token: str = "",
    probe_id: str = "",
    events_json: str = "",
    wait_seconds: float = 0.0,
    since: float = 0.0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read OOB callback events that have arrived.

    ``token`` should be the short ``token`` / subdomain label returned by
    ``oob_payload_generate``; only events whose JSON contains it are returned
    under ``hits``. ``wait_seconds > 0`` (e.g. 15) blocks until at least one
    matching event lands or the timeout expires — convenient right after
    firing a payload. ``events_json`` is only used when auto mode is off, for
    manual correlation with events copied from Burp Collaborator etc.
    """
    from argusai.core.security.oob import oob_check_events as _tool

    return _tool(
        token=token,
        probe_id=probe_id,
        events_json=events_json,
        wait_seconds=wait_seconds,
        since=since,
    )


def oob_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report OOB client state: mode, server, callback domain, event count."""
    from argusai.core.security.oob import oob_status as _tool

    return _tool()


def oob_start(api: Any, force: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Explicitly initialize the Interactsh session.

    Registers a fresh correlation id on Project Discovery's public
    Interactsh service and starts the background poll thread. Returns the
    callback ``domain`` so the AI can surface it to the user up-front. Calling
    this is optional — ``oob_payload_generate`` will lazily start the session
    on first use. Set ``force=True`` to deregister the current session and
    start over with a new correlation id.
    """
    from argusai.core.security.oob import oob_start as _tool

    return _tool(force=bool(force))


def oob_stop(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Deregister and stop the OOB polling thread.

    Use when the assessment is finished and you want to release the
    correlation id back to the Interactsh server.
    """
    from argusai.core.security.oob import oob_stop as _tool

    return _tool()


def auth_session_inventory(api: Any, headers_a: dict[str, str] | None = None, headers_b: dict[str, str] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Summarize supplied session headers/cookies for role comparison."""
    from argusai.core.security.auth_flow import auth_session_inventory as _tool

    return _tool(headers_a=headers_a, headers_b=headers_b)


async def idor_compare_request(api: Any, url: str = "", scope: str = "", headers_a: dict[str, str] | None = None, headers_b: dict[str, str] | None = None, alt_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Compare object access across adjacent URLs or roles for IDOR signals."""
    from argusai.core.security.auth_flow import idor_compare_request as _tool

    return await _tool(url, scope=scope, headers_a=headers_a, headers_b=headers_b, alt_url=alt_url)


async def role_matrix_probe(api: Any, urls: list[str] | str = "", scope: str = "", roles_json: str = "{}", **kwargs: Any) -> dict[str, Any]:
    """Probe URLs with multiple role header sets and compare access matrix."""
    from argusai.core.security.auth_flow import role_matrix_probe as _tool

    return await _tool(urls, scope=scope, roles_json=roles_json)


def business_state_probe(api: Any, steps_json: str = "[]", **kwargs: Any) -> dict[str, Any]:
    """Build a business-logic state-machine test checklist from steps."""
    from argusai.core.security.auth_flow import business_state_probe as _tool

    return _tool(steps_json=steps_json)


async def upload_surface_discover(api: Any, url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Discover upload forms on an authorized page."""
    from argusai.core.security.upload_tools import upload_surface_discover as _tool

    return await _tool(url, scope=scope)


def upload_payload_matrix(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Return safe upload test payload matrix."""
    from argusai.core.security.upload_tools import upload_payload_matrix as _tool

    return _tool()


async def upload_verify_access(api: Any, url: str = "", marker: str = "argusai-upload-probe", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Verify whether an uploaded marker file is accessible or executable."""
    from argusai.core.security.upload_tools import upload_verify_access as _tool

    return await _tool(url, marker=marker, scope=scope)


async def ssrf_probe_url_param(api: Any, url: str = "", param: str = "", payload: str = "http://127.0.0.1", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Probe a URL parameter for SSRF response differences."""
    from argusai.core.security.ssrf_tools import ssrf_probe_url_param as _tool

    return await _tool(url, param=param, payload=payload, scope=scope)


async def ssrf_redirect_chain_test(api: Any, url: str = "", redirect_url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Plan SSRF redirect-chain validation using a controlled redirect URL."""
    from argusai.core.security.ssrf_tools import ssrf_redirect_chain_test as _tool

    return await _tool(url, redirect_url, scope=scope)


def ssrf_metadata_template(api: Any, provider: str = "aws", **kwargs: Any) -> dict[str, Any]:
    """Return cloud/local metadata SSRF payload templates."""
    from argusai.core.security.ssrf_tools import ssrf_metadata_template as _tool

    return _tool(provider=provider)


async def cors_matrix_probe(api: Any, url: str = "", scope: str = "", origins: list[str] | str = "", **kwargs: Any) -> dict[str, Any]:
    """Probe CORS origin reflection and credentials behavior."""
    from argusai.core.security.identity_tools import cors_matrix_probe as _tool

    return await _tool(url, scope=scope, origins=origins)


async def cors_exploit_verify(
    api: Any,
    url: str = "",
    scope: str = "",
    attacker_origin: str = "https://evil.example",
    method: str = "GET",
    headers_json: str = "",
    data: str = "",
    cookie: str = "",
    cookie_same_site: str = "None",
    sensitive_regex: str = "",
    max_body_chars: int = 2000,
    fresh_context: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """CORS verification / CORS 漏洞验证 with a real browser credentialed fetch. Use instead of local HTML PoC pages."""
    from argusai.core.security.identity_tools import cors_exploit_verify as _tool

    return await _tool(
        url,
        scope=scope,
        attacker_origin=attacker_origin,
        method=method,
        headers_json=headers_json,
        data=data,
        cookie=cookie,
        cookie_same_site=cookie_same_site,
        sensitive_regex=sensitive_regex,
        max_body_chars=max_body_chars,
        fresh_context=fresh_context,
    )


def csrf_poc_generate(api: Any, action_url: str = "", method: str = "POST", fields_json: str = "{}", **kwargs: Any) -> dict[str, Any]:
    """Generate a minimal CSRF PoC HTML form."""
    from argusai.core.security.identity_tools import csrf_poc_generate as _tool

    return _tool(action_url=action_url, method=method, fields_json=fields_json)


def oauth_flow_analyze(api: Any, url: str = "", html: str = "", **kwargs: Any) -> dict[str, Any]:
    """Analyze OAuth/OIDC flow URLs and page HTML for common weaknesses."""
    from argusai.core.security.identity_tools import oauth_flow_analyze as _tool

    return _tool(url=url, html=html)


def jwt_attack_surface(api: Any, token: str = "", **kwargs: Any) -> dict[str, Any]:
    """Analyze JWT header/payload attack surface."""
    from argusai.core.security.identity_tools import jwt_attack_surface as _tool

    return _tool(token=token)


def saml_surface_analyze(api: Any, xml_or_html: str = "", **kwargs: Any) -> dict[str, Any]:
    """Analyze SAML response/page text for signature and assertion signals."""
    from argusai.core.security.identity_tools import saml_surface_analyze as _tool

    return _tool(xml_or_html=xml_or_html)


async def http_desync_probe(api: Any, url: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run low-frequency CL.TE/TE.CL desync heuristics against an authorized URL."""
    from argusai.core.security.http_desync import http_desync_probe as _tool

    return await _tool(url, scope=scope)


async def cache_poison_probe(api: Any, url: str = "", header_name: str = "X-Forwarded-Host", header_value: str = "argusai.invalid", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Probe cache poisoning header reflection leads."""
    from argusai.core.security.http_desync import cache_poison_probe as _tool

    return await _tool(url, header_name=header_name, header_value=header_value, scope=scope)


async def raw_http_send(api: Any, url: str = "", raw_request: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Send a raw HTTP/1 request to an authorized host."""
    from argusai.core.security.http_desync import raw_http_send as _tool

    return await _tool(url, raw_request=raw_request, scope=scope)


def create_poc_page(api: Any, html_content: str = "", title: str = "ArgusAI PoC",
                    filename: str = "", wrap: bool = True, open_browser: bool = False,
                    **kwargs: Any) -> dict[str, Any]:
    """Write a local HTML PoC page for XSS, opener/name, form, and UI demos. file:// pages produce Origin:null."""
    from argusai.core.security.poc_pages import create_poc_page as _create_poc_page

    return _create_poc_page(
        html_content=html_content,
        title=title,
        filename=filename,
        wrap=wrap,
        open_browser=open_browser,
    )


def open_poc_page(api: Any, path_or_uri: str = "", **kwargs: Any) -> dict[str, Any]:
    """Open a generated local PoC page in the user's browser."""
    from argusai.core.security.poc_pages import open_poc_page as _open_poc_page

    return _open_poc_page(path_or_uri)
