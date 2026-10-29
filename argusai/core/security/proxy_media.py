"""Lightweight MIME / URL classification for captured proxy flows."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_VIDEO_EXT = frozenset({".mp4", ".m3u8", ".ts", ".webm", ".mov", ".mkv", ".flv"})
_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"})
_SCRIPT_EXT = frozenset({".js", ".mjs", ".cjs"})
_DOC_EXT = frozenset({".html", ".htm", ".pdf", ".json", ".xml", ".txt", ".css"})


def classify_flow(
    *,
    url: str,
    response_headers: dict[str, str] | None = None,
    method: str = "",
) -> dict[str, str]:
    headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    path = (urlparse(url).path or "").lower()
    ext = ""
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1]

    disposition = headers.get("content-disposition", "")
    filename_hint = _filename_from_disposition(disposition) or _basename(path)

    kind = "other"
    if content_type.startswith("video/") or ext in _VIDEO_EXT or path.endswith(".m3u8"):
        kind = "video"
    elif content_type.startswith("image/") or ext in _IMAGE_EXT:
        kind = "image"
    elif (
        content_type.startswith("application/javascript")
        or content_type.startswith("text/javascript")
        or ext in _SCRIPT_EXT
    ):
        kind = "script"
    elif (
        content_type.startswith("application/json")
        or "+json" in content_type
        or "graphql" in content_type
        or (method.upper() in {"POST", "PUT", "PATCH"} and content_type.startswith("application/"))
    ):
        kind = "api"
    elif content_type.startswith("text/html") or ext in {".html", ".htm"}:
        kind = "document"
    elif content_type.startswith("text/css") or ext == ".css":
        kind = "stylesheet"
    elif ext in _DOC_EXT:
        kind = "document"

    return {
        "content_type": content_type,
        "resource_kind": kind,
        "filename_hint": filename_hint,
    }


def _basename(path: str) -> str:
    name = (path or "").rstrip("/").rsplit("/", 1)[-1]
    return name[:180] if name else ""


def _filename_from_disposition(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', text, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()[:180]
