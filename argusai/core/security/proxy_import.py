"""Import and analyze external HAR / Burp proxy logs."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from argusai.core.reverse.js_reverse_tools import har_import_analyze, request_rebuild_hint

_MAX_READ_BYTES = 20 * 1024 * 1024
_BURP_REQUEST_REGEX = re.compile(r"={10,}\s+([A-Z]{3,} .+?)\s+(={10,}|\Z)", re.I | re.S)
_HTTP_REQUEST_LINE = re.compile(r"\A([A-Z]+) (.+) HTTP/[\d.]+\Z")
_URL_PREVIEW_LIMIT = 500


def _parse_http_request(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text or "HTTP/" not in text:
        return None
    lines = text.splitlines()
    method = ""
    target = ""
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in lines:
        if not in_body:
            match = _HTTP_REQUEST_LINE.match(line.strip()) if not method else None
            if match:
                method = match.group(1)
                target = match.group(2).strip()
                continue
            if line.strip() == "" and method:
                in_body = True
                continue
            if method and ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip()] = value.strip()
        else:
            body_lines.append(line)
    if not method:
        return None
    host = headers.get("Host") or headers.get("host") or ""
    url = target
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    elif host:
        url = f"https://{host}{target if target.startswith('/') else '/' + target}"
    return {
        "method": method,
        "url": url,
        "path": target,
        "host": host,
        "headers": headers,
        "body": "\n".join(body_lines).strip()[:8000],
    }


def parse_burp_proxy_log(text: str) -> dict[str, Any]:
    """Parse Burp Suite plain-text proxy history / saved items."""
    content = str(text or "")
    requests: list[dict[str, Any]] = []
    if _BURP_REQUEST_REGEX.search(content):
        for match in _BURP_REQUEST_REGEX.finditer(content):
            parsed = _parse_http_request(match.group(1))
            if parsed:
                requests.append(parsed)
    elif "HTTP/" in content:
        parsed = _parse_http_request(content)
        if parsed:
            requests.append(parsed)
    if not requests:
        return {"ok": False, "error": "no HTTP requests found in Burp log"}
    return {"ok": True, "request_count": len(requests), "requests": requests[:_URL_PREVIEW_LIMIT]}


def _requests_to_har_entries(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for req in requests[:_URL_PREVIEW_LIMIT]:
        headers = req.get("headers") if isinstance(req.get("headers"), dict) else {}
        header_items = [{"name": str(k), "value": str(v)} for k, v in list(headers.items())[:80]]
        post_data: dict[str, Any] = {}
        body = str(req.get("body") or "")
        if body:
            post_data = {"mimeType": headers.get("Content-Type") or "application/octet-stream", "text": body[:2000]}
        entries.append({
            "request": {
                "method": req.get("method"),
                "url": req.get("url"),
                "headers": header_items,
                "postData": post_data,
            },
            "response": {"status": req.get("status")},
        })
    return entries


def analyze_burp_log_text(text: str) -> dict[str, Any]:
    parsed = parse_burp_proxy_log(text)
    if not parsed.get("ok"):
        return parsed
    requests = []
    for req in parsed.get("requests") or []:
        requests.append({
            "method": req.get("method"),
            "url": req.get("url"),
            "headers": req.get("headers") or {},
            "post_data": str(req.get("body") or "")[:2000],
            "status": req.get("status"),
        })
    hint = request_rebuild_hint({"requests": requests})
    return {
        "ok": True,
        "source": "burp_log",
        "entry_count": len(requests),
        "requests": requests[:200],
        **hint,
    }


def read_text_file(path: Path, *, max_bytes: int = _MAX_READ_BYTES) -> str:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file too large ({size} bytes > {max_bytes})")
    return path.read_text(encoding="utf-8", errors="replace")


def analyze_har_path(path: Path) -> dict[str, Any]:
    try:
        text = read_text_file(path)
    except (OSError, ValueError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}
    result = har_import_analyze(text)
    result["source"] = "har_file"
    result["path"] = str(path.resolve())
    result["filename"] = path.name
    return result


def analyze_burp_log_path(path: Path) -> dict[str, Any]:
    try:
        text = read_text_file(path)
    except (OSError, ValueError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}
    result = analyze_burp_log_text(text)
    if result.get("ok"):
        result["path"] = str(path.resolve())
        result["filename"] = path.name
    return result


def store_import_copy(path: Path, exports_dir: Path, *, kind: str) -> dict[str, Any]:
    """Copy an imported file into the proxy exports directory for later review."""
    exports_dir = exports_dir.expanduser().resolve()
    exports_dir.mkdir(parents=True, exist_ok=True)
    src = path.expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = exports_dir / f"import-{kind}-{stamp}-{src.name}"
    try:
        dest.write_bytes(src.read_bytes())
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "stored_path": str(dest.resolve()),
        "exports_dir": str(exports_dir),
    }


def burp_log_to_har_json(text: str) -> dict[str, Any]:
    parsed = parse_burp_proxy_log(text)
    if not parsed.get("ok"):
        return parsed
    entries = _requests_to_har_entries(parsed.get("requests") or [])
    har = {"log": {"version": "1.2", "creator": {"name": "argusai", "version": "1.0"}, "entries": entries}}
    return {"ok": True, "har": har, "entry_count": len(entries)}
