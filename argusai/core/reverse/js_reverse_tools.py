"""Utilities for JavaScript/frontend reverse engineering.

The functions in this module intentionally avoid executing untrusted code.
They provide small, composable primitives that the scanner and AI assistant can
combine: bundle fingerprinting, source-map inspection, string extraction,
lightweight deobfuscation hints, API/signature discovery, captcha fingerprinting
and request reconstruction helpers.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import string
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse


CODE_LIMIT = 1_000_000
TEXT_PREVIEW = 8000
ITEM_LIMIT = 200


def _clip(text: str, limit: int = TEXT_PREVIEW) -> str:
    text = str(text or "")
    return text[:limit]


def _window(text: str, start: int, end: int, before: int = 120, after: int = 160) -> str:
    return text[max(0, start - before): min(len(text), end + after)].replace("\n", " ")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _safe_code(code: str) -> tuple[str, bool]:
    code = str(code or "")
    truncated = len(code) > CODE_LIMIT
    return code[:CODE_LIMIT], truncated


_URL_RE = re.compile(r"""(?P<q>['"`])((?:https?://|/)[^'"`\s<>{}\\]{2,500})(?P=q)""")
_SOURCE_MAP_RE = re.compile(r"//#\s*sourceMappingURL=([^\s]+)")
_SOURCE_MAP_COMMENT_RE = re.compile(r"/\*#\s*sourceMappingURL=([^*]+)\*/")
_BASE64ISH_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")
_HEXISH_RE = re.compile(r"(?:\\x[0-9a-fA-F]{2}|0x[0-9a-fA-F]{2,}|[0-9a-fA-F]{32,})")
_JS_STRING_RE = re.compile(r"""(?P<q>['"`])(?P<body>(?:\\.|(?!\1).){2,400})(?P=q)""", re.DOTALL)
_PARAM_RE = re.compile(r"""(?:[?&]|['"`])([a-zA-Z_][\w.-]{1,60})=""")
_FETCH_LIKE_RE = re.compile(
    r"""(?P<kind>fetch|axios\.(?:get|post|put|delete|patch|request)|\w+\.open)\s*\((?P<args>.{0,700})""",
    re.IGNORECASE | re.DOTALL,
)
_CRYPTO_RE = re.compile(
    r"CryptoJS|crypto\.subtle|SubtleCrypto|JSEncrypt|jsrsasign|SM[234]|AES|RSA|HMAC|MD5|SHA(?:1|256|512)?|encrypt|decrypt|sign|nonce|timestamp|token",
    re.IGNORECASE,
)
_SIGN_RE = re.compile(
    r"\b(sign|signature|token|nonce|timestamp|ts|secret|salt|hash|hmac|encrypt|decrypt)\b",
    re.IGNORECASE,
)
_WEBPACK_HINT_RE = re.compile(r"__webpack_require__|webpackJsonp|webpackChunk|webpack_require__")
_VITE_HINT_RE = re.compile(r"/@vite/|import\.meta\.env|__vite__|vite/preload-helper")
_ROLLUP_HINT_RE = re.compile(r"System\.register|rollup|__moduleExports")
_PARCEL_HINT_RE = re.compile(r"parcelRequire|__parcel__")
_NEXT_HINT_RE = re.compile(r"__NEXT_DATA__|/_next/static|webpackChunk_N_E")
_NUXT_HINT_RE = re.compile(r"__NUXT__|/_nuxt/")
_OBFUSCATED_ARRAY_RE = re.compile(
    r"(?:var|let|const)\s+([_$a-zA-Z][\w$]*)\s*=\s*\[((?:\s*['\"].{0,160}?['\"]\s*,?){3,})\]",
    re.DOTALL,
)
_STRING_ARRAY_REF_RE = re.compile(r"([_$a-zA-Z][\w$]*)\s*\[\s*(0x[0-9a-fA-F]+|\d+)\s*\]")
_PACKER_RE = re.compile(r"eval\(function\(p,a,c,k,e,(?:r|d)\)")


CAPTCHA_SIGNATURES = {
    "recaptcha": ["www.google.com/recaptcha", "grecaptcha", "g-recaptcha"],
    "hcaptcha": ["hcaptcha.com", "h-captcha", "hcaptcha"],
    "turnstile": ["challenges.cloudflare.com/turnstile", "cf-turnstile", "turnstile"],
    "geetest": ["geetest", "gt.js", "captchaObj", "initGeetest"],
    "tencent": ["tencentcaptcha", "TCaptcha", "captcha.gtimg.com"],
    "aliyun": ["aliyuncaptcha", "aliyun.com/captcha", "nc.js"],
    "slider": ["slider", "slide", "drag", "轨迹", "滑块"],
    "canvas": ["canvas", "toDataURL", "getImageData"],
}


def analyze_js_bundle(code: str, source_url: str = "") -> dict[str, Any]:
    code, truncated = _safe_code(code)
    hints = []
    frameworks = []
    if _WEBPACK_HINT_RE.search(code):
        hints.append("webpack")
    if _VITE_HINT_RE.search(code):
        hints.append("vite")
    if _ROLLUP_HINT_RE.search(code):
        hints.append("rollup")
    if _PARCEL_HINT_RE.search(code):
        hints.append("parcel")
    if _NEXT_HINT_RE.search(code):
        frameworks.append("next")
    if _NUXT_HINT_RE.search(code):
        frameworks.append("nuxt")
    if "React.createElement" in code or "__REACT_DEVTOOLS_GLOBAL_HOOK__" in code:
        frameworks.append("react")
    if "createApp(" in code or "__VUE__" in code or "Vue." in code:
        frameworks.append("vue")
    if "angular" in code.lower():
        frameworks.append("angular")

    imports = sorted(set(re.findall(r"import\(['\"]([^'\"]+)['\"]\)", code)))[:ITEM_LIMIT]
    chunks = sorted(set(re.findall(r"['\"]([^'\"]+\.(?:chunk\.)?js)['\"]", code)))[:ITEM_LIMIT]
    maps = discover_source_maps(source_url, code).get("candidates", [])
    modules = extract_webpack_modules(code, source_url=source_url, max_modules=20)
    strings = extract_strings(code, max_items=80)
    crypto = crypto_detect(code, source_url=source_url, max_items=40)
    captcha = captcha_detect(code=code, source_url=source_url)
    obfuscation = control_flow_hints(code)

    return {
        "ok": True,
        "source_url": source_url,
        "size": len(code),
        "sha256": _hash(code),
        "truncated": truncated,
        "bundler": hints[0] if hints else "unknown",
        "bundle_hints": hints,
        "framework_hints": frameworks,
        "dynamic_imports": imports,
        "chunk_references": chunks,
        "source_maps": maps,
        "webpack_modules": modules.get("modules", []),
        "string_summary": strings.get("summary", {}),
        "crypto_summary": crypto.get("summary", {}),
        "captcha": captcha,
        "obfuscation_hints": obfuscation.get("hints", []),
    }


def beautify_js(code: str, indent_size: int = 2, max_chars: int = 200_000) -> dict[str, Any]:
    """A conservative formatter for minified JS.

    This is not a full parser; it adds line breaks around statement/block
    punctuation and respects quoted strings well enough for inspection.
    """
    code = str(code or "")[: max(1000, min(int(max_chars or 200_000), CODE_LIMIT))]
    indent_size = max(1, min(int(indent_size or 2), 8))
    out: list[str] = []
    indent = 0
    quote = ""
    escaped = False
    pending_space = False

    def newline() -> None:
        while out and out[-1] == " ":
            out.pop()
        if out and out[-1] != "\n":
            out.append("\n")
            out.append(" " * (indent * indent_size))

    for ch in code:
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            if pending_space:
                out.append(" ")
                pending_space = False
            quote = ch
            out.append(ch)
        elif ch in "{[":
            out.append(ch)
            indent += 1
            newline()
        elif ch in "}]":
            indent = max(0, indent - 1)
            newline()
            out.append(ch)
        elif ch == ";":
            out.append(ch)
            newline()
        elif ch == ",":
            out.append(ch)
            if indent > 0:
                newline()
            else:
                out.append(" ")
        elif ch.isspace():
            pending_space = True
        else:
            if pending_space and out and out[-1] not in "\n ({[,:;":
                out.append(" ")
            pending_space = False
            out.append(ch)
    text = "".join(out).strip()
    return {"ok": True, "size": len(code), "beautified": text, "truncated": len(text) >= max_chars}


def discover_source_maps(source_url: str = "", code: str = "") -> dict[str, Any]:
    candidates: list[dict[str, str]] = []
    for regex in (_SOURCE_MAP_RE, _SOURCE_MAP_COMMENT_RE):
        for m in regex.finditer(code or ""):
            raw = m.group(1).strip()
            candidates.append({"kind": "comment", "url": urljoin(source_url, raw) if source_url else raw})
    if source_url and source_url.endswith(".js"):
        candidates.append({"kind": "convention", "url": source_url + ".map"})
    seen = set()
    unique = []
    for item in candidates:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return {"ok": True, "source_url": source_url, "candidates": unique[:50]}


def recover_sourcemap(map_text: str, source_url: str = "") -> dict[str, Any]:
    try:
        data = json.loads(map_text or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid source map JSON: {e}", "source_url": source_url}
    sources = data.get("sources") or []
    contents = data.get("sourcesContent") or []
    files = []
    for idx, src in enumerate(sources[:ITEM_LIMIT]):
        content = contents[idx] if idx < len(contents) else None
        files.append({
            "path": src,
            "has_content": content is not None,
            "size": len(content or ""),
            "preview": _clip(content or "", 1500),
        })
    leaks = [s for s in sources if re.search(r"(^[A-Za-z]:\\|/Users/|/home/|src/|\.env|secret|internal)", str(s), re.I)]
    return {
        "ok": True,
        "source_url": source_url,
        "version": data.get("version"),
        "file": data.get("file"),
        "source_root": data.get("sourceRoot"),
        "source_count": len(sources),
        "sources_with_content": sum(1 for c in contents if c is not None),
        "files": files,
        "path_leaks": leaks[:50],
    }


def _find_balanced_object(text: str, open_pos: int, open_ch: str = "{", close_ch: str = "}") -> tuple[int, int] | None:
    depth = 0
    quote = ""
    escaped = False
    for idx in range(open_pos, len(text)):
        ch = text[idx]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return open_pos, idx + 1
    return None


def extract_webpack_modules(code: str, source_url: str = "", max_modules: int = 80) -> dict[str, Any]:
    code, truncated = _safe_code(code)
    modules: list[dict[str, Any]] = []
    patterns = [
        re.compile(r"\{\s*(\d+)\s*:\s*(?:function|\()"),
        re.compile(r"(\d+)\s*:\s*\(\s*(?:\w+,){0,3}\w*\s*\)\s*=>"),
    ]
    seen: set[str] = set()
    for pat in patterns:
        for m in pat.finditer(code):
            module_id = m.group(1)
            if module_id in seen:
                continue
            start = m.start()
            boundary = code.find("\n", start + 1)
            next_match = pat.search(code, m.end())
            end = next_match.start() if next_match else min(len(code), start + 6000)
            if boundary != -1 and boundary < end and end - start > 12000:
                end = boundary
            snippet = code[start:min(end, start + 4000)]
            seen.add(module_id)
            modules.append({
                "id": module_id,
                "offset": start,
                "size_estimate": max(0, end - start),
                "preview": _clip(snippet, 1200),
                "tags": _module_tags(snippet),
                "source": source_url,
            })
            if len(modules) >= max_modules:
                break
        if len(modules) >= max_modules:
            break
    return {"ok": True, "source_url": source_url, "module_count": len(modules), "modules": modules, "truncated": truncated}


def read_webpack_module(code: str, module_id: str = "", offset: int | None = None) -> dict[str, Any]:
    code, _ = _safe_code(code)
    start = None
    if offset is not None:
        try:
            start = max(0, int(offset))
        except Exception:
            start = None
    if start is None and module_id:
        m = re.search(rf"\b{re.escape(str(module_id))}\s*:\s*(?:function|\()", code)
        if m:
            start = m.start()
    if start is None:
        return {"ok": False, "error": "module not found"}
    next_m = re.search(r"\n\s*\d+\s*:\s*(?:function|\()", code[start + 1:])
    end = start + 1 + next_m.start() if next_m else min(len(code), start + 60_000)
    raw = code[start:end]
    return {"ok": True, "module_id": module_id, "offset": start, "size": len(raw), **beautify_js(raw, max_chars=60_000)}


def _module_tags(snippet: str) -> list[str]:
    tags = []
    checks = {
        "api": r"fetch|axios|XMLHttpRequest|/api/",
        "crypto": _CRYPTO_RE,
        "captcha": r"captcha|recaptcha|hcaptcha|turnstile|geetest|slider|canvas",
        "route": r"path\s*:|createRouter|Routes|Route",
        "storage": r"localStorage|sessionStorage|cookie",
    }
    for tag, pat in checks.items():
        if re.search(pat, snippet, re.I):
            tags.append(tag)
    return tags


def extract_strings(code: str, max_items: int = 200) -> dict[str, Any]:
    code, _ = _safe_code(code)
    strings_found = []
    urls = []
    params = sorted(set(_PARAM_RE.findall(code)))[:max_items]
    encoded = []
    for m in _JS_STRING_RE.finditer(code):
        raw = m.group("body")
        if len(raw) < 3:
            continue
        decoded = decode_literal(raw).get("decoded", raw)
        item = {"offset": m.start(), "value": _clip(decoded, 500)}
        if decoded.startswith(("http://", "https://", "/")):
            urls.append(item)
        elif len(strings_found) < max_items:
            strings_found.append(item)
    for m in _BASE64ISH_RE.finditer(code):
        value = m.group(0)
        try:
            b = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
            if b and sum(c in bytes(string.printable, "ascii") for c in b) / max(1, len(b)) > 0.75:
                encoded.append({"kind": "base64", "offset": m.start(), "value": value[:120], "decoded_preview": _clip(b.decode("utf-8", "replace"), 300)})
        except Exception:
            pass
    for m in _HEXISH_RE.finditer(code):
        encoded.append({"kind": "hex", "offset": m.start(), "value": _clip(m.group(0), 180)})
        if len(encoded) >= max_items:
            break
    return {
        "ok": True,
        "summary": {"strings": len(strings_found), "urls": len(urls), "params": len(params), "encoded": len(encoded)},
        "strings": strings_found[:max_items],
        "urls": urls[:max_items],
        "params": params,
        "encoded": encoded[:max_items],
    }


def search_code(code: str, pattern: str, case_sensitive: bool = False, limit: int = 50, context: int = 120) -> dict[str, Any]:
    code, _ = _safe_code(code)
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        regex = re.compile(re.escape(pattern), flags)
    matches = []
    for m in regex.finditer(code):
        matches.append({"offset": m.start(), "match": _clip(m.group(0), 200), "context": _window(code, m.start(), m.end(), context, context)})
        if len(matches) >= limit:
            break
    return {"ok": True, "pattern": pattern, "count": len(matches), "matches": matches}


def slice_context(code: str, offset: int = 0, symbol: str = "", pattern: str = "", context: int = 1600) -> dict[str, Any]:
    code, _ = _safe_code(code)
    start = 0
    reason = "offset"
    if symbol:
        m = re.search(rf"\b{re.escape(symbol)}\b", code)
        if m:
            start = m.start()
            reason = "symbol"
    elif pattern:
        try:
            m = re.search(pattern, code)
        except re.error:
            m = re.search(re.escape(pattern), code)
        if m:
            start = m.start()
            reason = "pattern"
    else:
        start = max(0, min(int(offset or 0), len(code)))
    begin = max(0, start - context)
    end = min(len(code), start + context)
    return {"ok": True, "reason": reason, "offset": start, "begin": begin, "end": end, "snippet": code[begin:end]}


def decode_literal(text: str) -> dict[str, Any]:
    raw = str(text or "")
    decoded = raw
    errors = []
    try:
        decoded = ast.literal_eval(f"'{raw}'")
    except Exception as e:
        errors.append(str(e))
        try:
            decoded = bytes(raw, "utf-8").decode("unicode_escape")
        except Exception as e2:
            errors.append(str(e2))
    transforms = {"unicode_escape": decoded}
    try:
        transforms["url_unquote"] = __import__("urllib.parse").parse.unquote(decoded)
    except Exception:
        pass
    try:
        b = base64.b64decode(decoded + "=" * (-len(decoded) % 4), validate=False)
        if b:
            transforms["base64"] = b.decode("utf-8", "replace")
    except Exception:
        pass
    return {"ok": True, "input": raw[:500], "decoded": decoded, "transforms": transforms, "errors": errors[:3]}


def string_array_map(code: str, max_items: int = 200) -> dict[str, Any]:
    code, _ = _safe_code(code)
    arrays = []
    for m in _OBFUSCATED_ARRAY_RE.finditer(code):
        name = m.group(1)
        body = m.group(2)
        values = [decode_literal(sm.group("body")).get("decoded", sm.group("body")) for sm in _JS_STRING_RE.finditer(body)]
        refs = []
        for rm in _STRING_ARRAY_REF_RE.finditer(code):
            if rm.group(1) != name:
                continue
            idx_raw = rm.group(2)
            idx = int(idx_raw, 16) if idx_raw.lower().startswith("0x") else int(idx_raw)
            refs.append({"offset": rm.start(), "index": idx, "value": values[idx] if 0 <= idx < len(values) else None})
            if len(refs) >= max_items:
                break
        arrays.append({"name": name, "size": len(values), "values": values[:max_items], "references": refs})
        if len(arrays) >= 20:
            break
    return {"ok": True, "arrays": arrays, "array_count": len(arrays)}


def constant_fold_preview(expression: str) -> dict[str, Any]:
    expr = str(expression or "")[:2000]
    if not re.fullmatch(r"[\s\d+\-*/%().,'\"a-fA-FxX\\u\\n\\r\\t]+", expr):
        return {"ok": False, "error": "expression contains unsafe tokens; only literals/arithmetic are previewed"}
    try:
        node = ast.parse(expr, mode="eval")
        value = ast.literal_eval(node)
    except Exception:
        try:
            value = eval(compile(ast.parse(expr, mode="eval"), "<const>", "eval"), {"__builtins__": {}}, {})
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "expression": expr, "value": value, "value_preview": _clip(value, 1000)}


def control_flow_hints(code: str) -> dict[str, Any]:
    code, _ = _safe_code(code)
    checks = [
        ("packer_eval", _PACKER_RE),
        ("eval_or_function_constructor", re.compile(r"\beval\s*\(|new\s+Function\s*\(")),
        ("debugger_trap", re.compile(r"\bdebugger\b|setInterval\s*\([^)]*debugger", re.I)),
        ("string_array", _OBFUSCATED_ARRAY_RE),
        ("switch_flattening", re.compile(r"while\s*\(\s*!!\[\]\s*\)|switch\s*\([^)]*\)\s*\{", re.I)),
        ("comma_expression_dense", re.compile(r"(?:[,;]\s*){8,}")),
        ("anti_devtools", re.compile(r"devtools|outerWidth\s*-\s*innerWidth|console\.clear|toString\(\)\s*\{", re.I)),
    ]
    hints = []
    for name, regex in checks:
        matches = list(regex.finditer(code))
        if matches:
            hints.append({"type": name, "count": len(matches), "sample": _window(code, matches[0].start(), matches[0].end())})
    return {"ok": True, "hints": hints}


def deobfuscate_js(code: str) -> dict[str, Any]:
    code, truncated = _safe_code(code)
    decoded = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda m: chr(int(m.group(1), 16)) if 32 <= int(m.group(1), 16) <= 126 else m.group(0),
        code,
    )
    decoded = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1), 16)) if int(m.group(1), 16) >= 32 else m.group(0),
        decoded,
    )
    arrays = string_array_map(decoded)
    hints = control_flow_hints(decoded)
    return {
        "ok": True,
        "truncated": truncated,
        "preview": _clip(decoded, 20_000),
        "string_arrays": arrays.get("arrays", [])[:10],
        "hints": hints.get("hints", []),
    }


def api_extract(code: str, source_url: str = "", max_items: int = 120) -> dict[str, Any]:
    code, _ = _safe_code(code)
    endpoints = []
    for m in _FETCH_LIKE_RE.finditer(code):
        ctx = _window(code, m.start(), m.end(), 80, 400)
        urls = [u.group(2) for u in _URL_RE.finditer(ctx)]
        method_m = re.search(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD)\b", ctx, re.I)
        endpoints.append({
            "kind": m.group("kind"),
            "method": method_m.group(1).upper() if method_m else "",
            "urls": urls[:5],
            "offset": m.start(),
            "context": ctx,
            "source": source_url,
        })
        if len(endpoints) >= max_items:
            break
    return {"ok": True, "source_url": source_url, "endpoints": endpoints, "count": len(endpoints)}


def signature_candidates(code: str, source_url: str = "", max_items: int = 80) -> dict[str, Any]:
    code, _ = _safe_code(code)
    candidates = []
    for m in _SIGN_RE.finditer(code):
        ctx = _window(code, m.start(), m.end(), 180, 260)
        if re.search(r"function|=>|return|CryptoJS|crypto\.subtle|MD5|SHA|HMAC|sort|join|Date\.now|Math\.random", ctx, re.I):
            candidates.append({"keyword": m.group(1), "offset": m.start(), "context": ctx, "source": source_url})
        if len(candidates) >= max_items:
            break
    return {"ok": True, "source_url": source_url, "candidates": candidates, "count": len(candidates)}


def crypto_detect(code: str, source_url: str = "", max_items: int = 80) -> dict[str, Any]:
    code, _ = _safe_code(code)
    hits = []
    counts: dict[str, int] = {}
    for m in _CRYPTO_RE.finditer(code):
        key = m.group(0)
        counts[key.lower()] = counts.get(key.lower(), 0) + 1
        if len(hits) < max_items:
            hits.append({"keyword": key, "offset": m.start(), "context": _window(code, m.start(), m.end()), "source": source_url})
    return {"ok": True, "summary": counts, "hits": hits, "count": sum(counts.values())}


def request_rebuild_hint(trace: dict[str, Any] | None = None, code: str = "") -> dict[str, Any]:
    trace = trace or {}
    requests = trace.get("requests") or trace.get("network") or []
    api = api_extract(code).get("endpoints", []) if code else []
    crypto = signature_candidates(code).get("candidates", []) if code else []
    templates = []
    for req in requests[:20]:
        templates.append({
            "url": req.get("url"),
            "method": req.get("method", "GET"),
            "headers": req.get("headers") or {},
            "body_preview": req.get("post_data") or req.get("body") or "",
            "signature_sources": crypto[:5],
        })
    return {"ok": True, "request_templates": templates, "static_api_candidates": api[:20], "signature_candidates": crypto[:10]}


def captcha_detect(html: str = "", code: str = "", source_url: str = "", runtime_trace: dict[str, Any] | None = None) -> dict[str, Any]:
    text = "\n".join([html or "", code or "", json.dumps(runtime_trace or {}, ensure_ascii=False)[:80_000]]).lower()
    detected = []
    for kind, needles in CAPTCHA_SIGNATURES.items():
        hits = [n for n in needles if n.lower() in text]
        if hits:
            detected.append({"type": kind, "signals": hits})
    sitekeys = sorted(set(re.findall(r"(?:sitekey|siteKey|data-sitekey)['\"\s:=]+([A-Za-z0-9_-]{8,100})", html + code)))[:20]
    callbacks = sorted(set(re.findall(r"(?:callback|cb)['\"\s:=]+([_$a-zA-Z][\w$]{1,80})", html + code)))[:20]
    return {
        "ok": True,
        "source_url": source_url,
        "detected": detected,
        "sitekeys": sitekeys,
        "callbacks": callbacks,
        "anti_bot": anti_bot_fingerprint_detect(code=code, html=html),
        "summary": "captcha signals found" if detected else "no obvious captcha signature",
    }


def anti_bot_fingerprint_detect(code: str = "", html: str = "") -> dict[str, Any]:
    text = (code or "") + "\n" + (html or "")
    patterns = {
        "webdriver": r"navigator\.webdriver|webdriver",
        "canvas": r"toDataURL|getImageData|canvas fingerprint",
        "webgl": r"WEBGL_debug_renderer_info|getParameter\(.*UNMASKED",
        "audio": r"AudioContext|OfflineAudioContext",
        "devtools": r"devtools|outerWidth\s*-\s*innerWidth|debugger",
        "timezone": r"Intl\.DateTimeFormat|timeZone|getTimezoneOffset",
        "plugins": r"navigator\.plugins|navigator\.mimeTypes",
    }
    hits = []
    for kind, pat in patterns.items():
        matches = list(re.finditer(pat, text, re.I))
        if matches:
            hits.append({"type": kind, "count": len(matches), "sample": _window(text, matches[0].start(), matches[0].end())})
    return {"hits": hits, "count": len(hits)}


def parse_querystring(text: str, update_key: str = "", update_value: str = "") -> dict[str, Any]:
    parsed_url = urlparse(text)
    query = parsed_url.query if parsed_url.query else text.lstrip("?")
    pairs = parse_qsl(query, keep_blank_values=True)
    if update_key:
        found = False
        new_pairs = []
        for k, v in pairs:
            if k == update_key:
                new_pairs.append((k, update_value))
                found = True
            else:
                new_pairs.append((k, v))
        if not found:
            new_pairs.append((update_key, update_value))
        pairs = new_pairs
    return {"ok": True, "params": [{"key": k, "value": v} for k, v in pairs], "rebuilt": urlencode(pairs)}


def parse_headers_and_cookies(text: str) -> dict[str, Any]:
    headers: dict[str, str] = {}
    cookies = []
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip()
        headers[key] = val
        if key.lower() in {"cookie", "set-cookie"}:
            for part in val.split(";"):
                if "=" in part:
                    ck, cv = part.split("=", 1)
                    cookies.append({"name": ck.strip(), "value": cv.strip()[:300]})
    return {"ok": True, "headers": headers, "cookies": cookies}


def json_path_extract(text: str, path: str = "$") -> dict[str, Any]:
    try:
        data = json.loads(text)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    cur: Any = data
    tokens = [p for p in re.split(r"\.|\[|\]", path.replace("$", "").strip(".")) if p != ""]
    try:
        for tok in tokens:
            if isinstance(cur, list):
                cur = cur[int(tok)]
            elif isinstance(cur, dict):
                cur = cur[tok]
            else:
                raise KeyError(tok)
    except Exception as e:
        return {"ok": False, "error": f"path not found: {e}", "path": path}
    return {"ok": True, "path": path, "value": cur, "preview": _clip(json.dumps(cur, ensure_ascii=False, indent=2), 8000)}


def wasm_inventory(data: str, input_format: str = "base64") -> dict[str, Any]:
    try:
        raw = base64.b64decode(data) if input_format == "base64" else bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", data))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not raw.startswith(b"\0asm"):
        return {"ok": False, "error": "not a wasm binary", "magic": raw[:8].hex()}
    printable = re.findall(rb"[\x20-\x7e]{4,}", raw)
    return {
        "ok": True,
        "version": int.from_bytes(raw[4:8], "little") if len(raw) >= 8 else None,
        "size": len(raw),
        "strings": [s.decode("ascii", "replace") for s in printable[:200]],
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def har_import_analyze(har_text: str) -> dict[str, Any]:
    try:
        har = json.loads(har_text)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    entries = har.get("log", {}).get("entries", [])
    requests = []
    for e in entries[:ITEM_LIMIT]:
        req = e.get("request", {})
        requests.append({
            "method": req.get("method"),
            "url": req.get("url"),
            "headers": {h.get("name"): h.get("value") for h in req.get("headers", [])[:80]},
            "post_data": (req.get("postData") or {}).get("text", "")[:2000],
            "status": (e.get("response") or {}).get("status"),
        })
    return {"ok": True, "entry_count": len(entries), "requests": requests, **request_rebuild_hint({"requests": requests})}


def js_ast_inventory(code: str) -> dict[str, Any]:
    """Heuristic AST-style inventory without executing JavaScript."""
    code, truncated = _safe_code(code)
    functions = []
    for m in re.finditer(r"\b(?:async\s+)?function\s+([_$a-zA-Z][\w$]*)\s*\(([^)]*)\)", code):
        functions.append({"name": m.group(1), "params": [p.strip() for p in m.group(2).split(",") if p.strip()], "offset": m.start()})
    for m in re.finditer(r"\b(?:const|let|var)\s+([_$a-zA-Z][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>", code):
        functions.append({"name": m.group(1), "params": [p.strip() for p in m.group(2).split(",") if p.strip()], "offset": m.start(), "kind": "arrow"})
    imports = re.findall(r"\bimport\s+(?:[^'\";]+from\s+)?['\"]([^'\"]+)['\"]", code)[:ITEM_LIMIT]
    exports = re.findall(r"\bexport\s+(?:default\s+)?(?:function|class|const|let|var)?\s*([_$a-zA-Z][\w$]*)?", code)[:ITEM_LIMIT]
    calls = re.findall(r"\b([_$a-zA-Z][\w$]*(?:\.[_$a-zA-Z][\w$]*)?)\s*\(", code)[:ITEM_LIMIT]
    return {
        "ok": True,
        "truncated": truncated,
        "function_count": len(functions),
        "functions": functions[:ITEM_LIMIT],
        "imports": imports,
        "exports": [e for e in exports if e],
        "top_calls": sorted({c for c in calls})[:ITEM_LIMIT],
        "crypto": crypto_detect(code),
        "apis": api_extract(code),
    }


def js_ast_string_array_decode(code: str, max_items: int = 200) -> dict[str, Any]:
    """Decode simple string-array obfuscation references such as arr[0x1]."""
    code, truncated = _safe_code(code)
    arrays = string_array_map(code, max_items=max_items)
    replacements = []
    for arr in arrays.get("arrays", []):
        name = arr.get("name")
        values = arr.get("values") or []
        if not name:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\s*\[\s*(0x[0-9a-fA-F]+|\d+)\s*\]")
        for m in pattern.finditer(code):
            try:
                idx = int(m.group(1), 16) if m.group(1).lower().startswith("0x") else int(m.group(1))
                if 0 <= idx < len(values):
                    replacements.append({"ref": m.group(0), "value": values[idx], "offset": m.start()})
            except Exception:
                pass
    return {"ok": True, "truncated": truncated, "replacements": replacements[:max(1, min(max_items, ITEM_LIMIT))]}


def js_function_dependency_graph(code: str, function_name: str = "") -> dict[str, Any]:
    code, _ = _safe_code(code)
    inv = js_ast_inventory(code)
    functions = inv.get("functions", [])
    names = {f.get("name") for f in functions if f.get("name")}
    edges = []
    for fn in functions:
        name = fn.get("name")
        if not name:
            continue
        start = int(fn.get("offset") or 0)
        window = code[start:start + 4000]
        for callee in names:
            if callee != name and re.search(rf"\b{re.escape(str(callee))}\s*\(", window):
                edges.append({"from": name, "to": callee})
    if function_name:
        edges = [e for e in edges if e["from"] == function_name or e["to"] == function_name]
    return {"ok": True, "functions": functions[:ITEM_LIMIT], "edges": edges[:ITEM_LIMIT], "focus": function_name}


def js_hook_template_generate(function_name: str = "", expression: str = "") -> dict[str, Any]:
    target = function_name or expression or "targetFunction"
    code = f"""(() => {{
  const name = {json.dumps(target)};
  const original = window[name];
  if (typeof original !== 'function') {{
    console.warn('Target is not a function:', name, original);
    return;
  }}
  window[name] = function(...args) {{
    console.log('[argusai hook]', name, 'args=', args);
    const ret = original.apply(this, args);
    console.log('[argusai hook]', name, 'return=', ret);
    return ret;
  }};
}})();"""
    return {"ok": True, "code": code, "usage": "Paste into DevTools console on an authorized target page."}


def wasm_decompile_hint(data: str = "", input_format: str = "base64") -> dict[str, Any]:
    inv = wasm_inventory(data, input_format=input_format) if data else {"ok": True, "strings": []}
    return {
        "ok": inv.get("ok", True),
        "inventory": inv,
        "recommended_tools": ["wasm2wat", "wasm-objdump", "wasm-decompile"],
        "workflow": [
            "Inventory imports/exports and printable strings.",
            "Convert to WAT with wasm2wat for control-flow review.",
            "Search for crypto/signature/token functions and host imports.",
            "Hook JS glue code before attempting low-level WASM patching.",
        ],
    }

