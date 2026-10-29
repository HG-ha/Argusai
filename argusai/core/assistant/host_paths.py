"""Normalize host filesystem paths that ride on assistant attachments.

The session sandbox is the default place for intermediate and generated
artifacts. User-supplied host files stay on disk; tools read them in place.
This module does not gate which paths a tool may open — confirmation cards
already cover risky tool calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

__all__ = [
    "attachment_host_path",
    "normalize_host_path",
]


def normalize_host_path(raw: str) -> str:
    """Return a resolved absolute host path, or ``""`` if it is not usable."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text or "\x00" in text:
        return ""
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        if parsed.scheme.lower() != "file":
            return ""
        text = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc != "localhost":
            text = f"//{parsed.netloc}{text}"
        if len(text) >= 3 and text[0] == "/" and text[2] == ":":
            text = text[1:]
    try:
        path = Path(text).expanduser()
    except (OSError, ValueError):
        return ""
    if not path.is_absolute():
        return ""
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def attachment_host_path(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("host_path", "path"):
        path = normalize_host_path(str(item.get(key) or ""))
        if path:
            return path
    return ""
