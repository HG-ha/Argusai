"""Web search, page source, and crypto/encoding utilities for the assistant."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import locale
import re
import time
from typing import Any
from urllib.parse import quote, unquote

import httpx
from bs4 import BeautifulSoup

from argusai.core.infra.command import in_scope, normalize_scope

BODY_LIMIT = 20000
# DuckDuckGo is often unreachable or empty from mainland China. Prefer Bing,
# then a Baidu HTML fallback, then the remaining ddgs backends.
SEARCH_BACKENDS = ("bing", "auto", "duckduckgo", "brave", "yahoo", "wikipedia")
CN_PRIMARY_BACKENDS = ("bing",)
CN_FALLBACK_BACKENDS = ("duckduckgo", "brave", "yahoo", "wikipedia")
BAIDU_SEARCH_URL = "https://www.baidu.com/s"


def _system_locale_tag() -> str:
    for getter in (locale.getlocale, getattr(locale, "getdefaultlocale", None)):
        if getter is None:
            continue
        try:
            tag = getter()[0] or ""
        except Exception:
            tag = ""
        if tag:
            return str(tag).lower()
    return ""


def prefer_cn_search(region: str = "") -> bool:
    """True when the search should prefer Bing/Baidu over DuckDuckGo."""
    text = str(region or "").strip().lower().replace("_", "-")
    if text.startswith("cn") or text.startswith("zh") or "zh-cn" in text or text == "cn-zh":
        return True
    if text and text not in {"wt-wt", "wt"}:
        return False
    loc = _system_locale_tag().replace("_", "-")
    return loc.startswith("zh") or loc.startswith("cn") or "chinese" in loc


def _resolve_search_region(region: str) -> str:
    text = str(region or "").strip() or "wt-wt"
    if text.lower() in {"wt-wt", "wt", ""} and prefer_cn_search(text):
        return "cn-zh"
    return text


def _parse_baidu_results(html_text: str, max_results: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in soup.select("div.result, div.c-container"):
        link = block.find("a")
        if link is None:
            continue
        url = str(block.get("mu") or link.get("data-landurl") or link.get("href") or "").strip()
        title = link.get_text(" ", strip=True)
        if not title or not url.startswith("http"):
            continue
        if "baidu.com/link" in url and not block.get("mu"):
            continue
        if url in seen:
            continue
        snippet_el = block.select_one(".c-abstract, .content-right_8Zs40, span.content-right_8Zs40")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el is not None else ""
        seen.add(url)
        rows.append({"title": title, "url": url, "snippet": snippet[:400]})
        if len(rows) >= max_results:
            break
    return rows


async def _baidu_html_search(query: str, max_results: int) -> list[dict[str, str]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(
            BAIDU_SEARCH_URL,
            params={"wd": query, "ie": "utf-8", "rn": str(max_results)},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"baidu HTTP {resp.status_code}")
    rows = _parse_baidu_results(resp.text, max_results)
    if not rows:
        raise RuntimeError("baidu returned no parseable results")
    return rows


def _trim(text: str, limit: int = BODY_LIMIT) -> dict[str, Any]:
    return {"text": text[:limit], "truncated": len(text) > limit, "length": len(text)}


def _coerce_search_result(item: dict[str, Any]) -> dict[str, str]:
    """Normalize result fields across ddgs backend/version differences."""
    title = item.get("title") or item.get("heading") or item.get("name") or ""
    url = item.get("href") or item.get("url") or item.get("link") or ""
    snippet = (
        item.get("body")
        or item.get("snippet")
        or item.get("description")
        or item.get("text")
        or item.get("content")
        or ""
    )
    return {
        "title": str(title),
        "url": str(url),
        "snippet": str(snippet),
    }


def _ddgs_collect(
    ddgs: Any,
    query: str,
    region: str,
    max_results: int,
    backends: tuple[str, ...],
    errors: list[str],
) -> tuple[list[dict[str, Any]], str]:
    for backend in backends:
        try:
            rows = list(
                ddgs.text(
                    query,
                    region=region,
                    max_results=max_results,
                    backend=backend,
                )
            )
        except Exception as e:
            errors.append(f"{backend}: {e}")
            continue
        if rows:
            return list(rows), backend
    return [], ""


def _search_success(
    query: str,
    backend: str,
    region: str,
    started: float,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "query": query,
        "backend": backend,
        "region": region,
        "elapsed_ms": int((time.time() - started) * 1000),
        "results": results,
    }


async def web_search(query: str, max_results: int = 5, region: str = "wt-wt") -> dict[str, Any]:
    """Search the web. Bing first; Baidu on Chinese hosts; DuckDuckGo as fallback."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required", "results": []}
    max_results = max(1, min(10, int(max_results or 5)))
    region = _resolve_search_region(region)
    started = time.time()
    errors: list[str] = []
    cn = prefer_cn_search(region)
    results: list[dict[str, Any]] = []
    used_backend = ""
    ddgs_cls: Any = None
    try:
        from ddgs import DDGS as ddgs_cls
    except Exception as e:
        errors.append(f"ddgs: {e}")

    if ddgs_cls is not None:
        try:
            with ddgs_cls() as ddgs:
                primary = CN_PRIMARY_BACKENDS if cn else SEARCH_BACKENDS
                results, used_backend = _ddgs_collect(
                    ddgs, query, region, max_results, primary, errors,
                )
        except Exception as e:
            errors.append(str(e))

    if cn and not results:
        try:
            baidu_rows = await _baidu_html_search(query, max_results)
        except Exception as e:
            errors.append(f"baidu: {e}")
            baidu_rows = []
        if baidu_rows:
            return _search_success(query, "baidu", region, started, baidu_rows)

    if cn and not results and ddgs_cls is not None:
        try:
            with ddgs_cls() as ddgs:
                results, used_backend = _ddgs_collect(
                    ddgs, query, region, max_results, CN_FALLBACK_BACKENDS, errors,
                )
        except Exception as e:
            errors.append(str(e))

    if not results:
        err = "; ".join(errors) or "No results found."
        return {"ok": False, "query": query, "error": err, "results": []}
    return _search_success(
        query,
        used_backend or "ddgs",
        region,
        started,
        [_coerce_search_result(r) for r in results],
    )


GITHUB_SEARCH_TYPES = ("repositories", "code", "issues")


def _coerce_github_item(item: dict[str, Any], search_type: str) -> dict[str, Any]:
    """Flatten a GitHub search hit into a compact, model-friendly record."""
    if search_type == "repositories":
        return {
            "name": item.get("full_name") or item.get("name") or "",
            "url": item.get("html_url") or "",
            "description": (item.get("description") or "")[:300],
            "stars": item.get("stargazers_count", 0),
            "language": item.get("language") or "",
            "updated_at": item.get("updated_at") or "",
        }
    if search_type == "code":
        repo = item.get("repository") or {}
        return {
            "path": item.get("path") or "",
            "repo": repo.get("full_name") or "",
            "url": item.get("html_url") or "",
        }
    # issues / pull requests
    return {
        "title": (item.get("title") or "")[:200],
        "url": item.get("html_url") or "",
        "state": item.get("state") or "",
        "repo": (item.get("repository_url") or "").replace("https://api.github.com/repos/", ""),
        "comments": item.get("comments", 0),
        "updated_at": item.get("updated_at") or "",
    }


async def github_search(
    query: str,
    search_type: str = "repositories",
    max_results: int = 10,
    token: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Search GitHub (repositories / code / issues) via the official REST API.

    Far more precise than scraping a search engine for PoCs, vulnerability writeups,
    affected hosts, and reference implementations. ``code`` search REQUIRES a
    token; ``repositories`` / ``issues`` work unauthenticated (low rate limit).
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required", "results": []}
    search_type = (search_type or "repositories").strip().lower()
    if search_type not in GITHUB_SEARCH_TYPES:
        search_type = "repositories"
    max_results = max(1, min(30, int(max_results or 10)))
    token = (token or "").strip()
    if search_type == "code" and not token:
        return {
            "ok": False,
            "query": query,
            "error": "GitHub code search requires a token. Set GITHUB_TOKEN, or use search_type='repositories'/'issues'.",
            "results": [],
        }
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "argusai",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.time()
    url = f"https://api.github.com/search/{search_type}"
    params = {"q": query, "per_page": max_results}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, params=params)
    except Exception as e:
        return {"ok": False, "query": query, "error": str(e), "results": []}
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        return {
            "ok": False,
            "query": query,
            "error": "GitHub rate limit hit. Set GITHUB_TOKEN to raise the limit.",
            "results": [],
        }
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("message") or "")
        except Exception:
            detail = resp.text[:200]
        return {
            "ok": False,
            "query": query,
            "status_code": resp.status_code,
            "error": f"GitHub API error {resp.status_code}: {detail}",
            "results": [],
        }
    try:
        data = resp.json()
    except Exception as e:
        return {"ok": False, "query": query, "error": f"bad JSON: {e}", "results": []}
    items = data.get("items") or []
    return {
        "ok": True,
        "query": query,
        "search_type": search_type,
        "total_count": data.get("total_count", 0),
        "authenticated": bool(token),
        "elapsed_ms": int((time.time() - started) * 1000),
        "results": [_coerce_github_item(it, search_type) for it in items[:max_results]],
    }


async def fetch_page_source(
    url: str,
    *,
    scope: str | list[str] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch raw HTML/source for an authorized URL."""
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}

    source = resp.text
    trimmed = _trim(source)
    return {
        "ok": True,
        "url": str(resp.url),
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type", ""),
        "elapsed_ms": int((time.time() - started) * 1000),
        "source": trimmed["text"],
        "truncated": trimmed["truncated"],
        "length": trimmed["length"],
    }


async def extract_page_text(url: str, *, scope: str | list[str] | None = None) -> dict[str, Any]:
    """Fetch a page and extract title, links, forms, scripts, and visible text."""
    raw = await fetch_page_source(url, scope=scope)
    if not raw.get("ok"):
        return raw
    soup = BeautifulSoup(raw.get("source") or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    links = [
        {"text": a.get_text(" ", strip=True)[:120], "href": a.get("href", "")}
        for a in soup.find_all("a", href=True)[:100]
    ]
    forms = [
        {
            "method": (form.get("method") or "GET").upper(),
            "action": form.get("action", ""),
            "inputs": [inp.get("name", "") for inp in form.find_all(["input", "textarea", "select"]) if inp.get("name")],
        }
        for form in soup.find_all("form")[:30]
    ]
    scripts = [s.get("src", "") for s in soup.find_all("script", src=True)[:100]]
    trimmed = _trim(text)
    return {
        "ok": True,
        "url": raw.get("url"),
        "status_code": raw.get("status_code"),
        "title": (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "text": trimmed["text"],
        "truncated": trimmed["truncated"],
        "links": links,
        "forms": forms,
        "scripts": scripts,
    }


_HEX_NOISE_RE = re.compile(r"(?i)0x|\\x|[\s,;:_\-]+")


def _clean_hex(text: str) -> str:
    """Strip common formatting noise from a hex string: ``0x`` / ``\\x``
    prefixes, separators (spaces, commas, dashes, underscores, colons,
    semicolons) and line breaks. Left-pads odd-length inputs with a zero
    so callers do not have to worry about trailing single nibbles."""
    cleaned = _HEX_NOISE_RE.sub("", text or "")
    if len(cleaned) % 2 == 1:
        cleaned = "0" + cleaned
    return cleaned


def codec_transform(text: str = "", operation: str = "base64_decode") -> dict[str, Any]:
    """Encode/decode common web strings.

    Hex operations are intentionally tolerant: ``hex_decode`` accepts
    formatting noise like ``0x``/``\\x`` prefixes, spaces, commas and
    line breaks. ``hex_encode`` returns compact, spaced and escaped
    variants together so the model does not have to call the tool
    multiple times to get a copy-pasteable form.
    """
    try:
        if operation == "base64_encode":
            out = base64.b64encode(text.encode()).decode()
            return {"ok": True, "operation": operation, "result": out}
        elif operation == "base64_decode":
            data = base64.b64decode(text.encode(), validate=False)
            return {
                "ok": True,
                "operation": operation,
                "result": data.decode("utf-8", errors="replace"),
                "latin1": data.decode("latin-1", errors="replace"),
                "byte_length": len(data),
            }
        elif operation == "url_encode":
            return {"ok": True, "operation": operation, "result": quote(text, safe="")}
        elif operation == "url_decode":
            return {"ok": True, "operation": operation, "result": unquote(text)}
        elif operation == "html_escape":
            return {"ok": True, "operation": operation, "result": html.escape(text)}
        elif operation == "html_unescape":
            return {"ok": True, "operation": operation, "result": html.unescape(text)}
        elif operation == "hex_encode":
            data = text.encode("utf-8")
            compact = data.hex()
            spaced = " ".join(f"{b:02x}" for b in data)
            escaped = "".join(f"\\x{b:02x}" for b in data)
            return {
                "ok": True,
                "operation": operation,
                "result": compact,
                "spaced": spaced,
                "escaped": escaped,
                "byte_length": len(data),
            }
        elif operation == "hex_decode":
            cleaned = _clean_hex(text)
            try:
                data = bytes.fromhex(cleaned)
            except ValueError as e:
                return {"ok": False, "operation": operation, "error": f"invalid hex: {e}"}
            return {
                "ok": True,
                "operation": operation,
                "result": data.decode("utf-8", errors="replace"),
                "latin1": data.decode("latin-1", errors="replace"),
                "byte_length": len(data),
            }
        else:
            return {"ok": False, "error": f"unsupported operation: {operation}"}
    except Exception as e:
        return {"ok": False, "operation": operation, "error": str(e)}


def hex_view(
    text: str = "",
    input_format: str = "text",
    width: int = 16,
    max_bytes: int = 4096,
) -> dict[str, Any]:
    """Produce a hexdump-style view of an input payload.

    ``input_format`` may be ``"text"`` (UTF-8 encode), ``"hex"`` (parse as
    tolerant hex string) or ``"base64"`` (decode base64). The output
    string layout matches GNU ``hexdump -C``:

        ``00000000  68 65 6c 6c 6f 20 77 6f  72 6c 64 0a              |hello world.|``

    Useful for inspecting non-printable bytes in payloads, JWT raw
    fragments, file uploads, response bodies, shellcode, etc.
    """
    try:
        if input_format == "hex":
            cleaned = _clean_hex(text)
            try:
                data = bytes.fromhex(cleaned)
            except ValueError as e:
                return {"ok": False, "error": f"invalid hex: {e}"}
        elif input_format == "base64":
            try:
                data = base64.b64decode((text or "").encode(), validate=False)
            except Exception as e:
                return {"ok": False, "error": f"invalid base64: {e}"}
        else:  # text / utf-8
            data = (text or "").encode("utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        width = max(1, min(int(width or 16), 64))
        max_bytes = max(16, min(int(max_bytes or 4096), 65536))
    except Exception:
        width, max_bytes = 16, 4096

    total = len(data)
    truncated = total > max_bytes
    if truncated:
        data = data[:max_bytes]

    lines: list[str] = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hex_cells = [f"{b:02x}" for b in chunk]
        # GNU-style: split the row in half with an extra space.
        half = width // 2
        left = " ".join(hex_cells[:half])
        right = " ".join(hex_cells[half:])
        hex_part = f"{left}  {right}" if right else left
        # Pad so the ascii column lines up.
        target_len = width * 3
        hex_part = hex_part.ljust(target_len)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hex_part}  |{ascii_part}|")

    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return {
        "ok": True,
        "input_format": input_format,
        "byte_length": total,
        "rendered_bytes": len(data),
        "truncated": truncated,
        "width": width,
        "printable_ratio": round(printable / len(data), 3) if data else 0.0,
        "dump": "\n".join(lines),
    }


def hash_text(text: str = "", algorithm: str = "sha256") -> dict[str, Any]:
    algo = (algorithm or "sha256").lower()
    try:
        h = hashlib.new(algo)
    except ValueError:
        return {"ok": False, "error": f"unsupported hash algorithm: {algorithm}"}
    h.update(text.encode())
    return {"ok": True, "algorithm": algo, "hex": h.hexdigest()}


def hmac_text(text: str = "", key: str = "", algorithm: str = "sha256") -> dict[str, Any]:
    algo = (algorithm or "sha256").lower()
    try:
        digest = hmac.new(key.encode(), text.encode(), algo).hexdigest()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "algorithm": algo, "hex": digest}


def jwt_decode(token: str = "") -> dict[str, Any]:
    """Decode JWT header/payload without verifying the signature."""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {"ok": False, "error": "JWT must have at least header and payload"}

    def b64url_decode(part: str) -> Any:
        padded = part + "=" * (-len(part) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace")

    try:
        return {
            "ok": True,
            "header": b64url_decode(parts[0]),
            "payload": b64url_decode(parts[1]),
            "has_signature": len(parts) >= 3 and bool(parts[2]),
            "verified": False,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
