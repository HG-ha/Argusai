"""Load raster images for assistant vision — any readable path, not sandbox-only.

Conversation attachments, tool screenshots, project files, and ordinary host
paths share one loader so the model can inspect an image the same way a
normal coding agent reads a PNG.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

# Raster stills the chat / auxiliary vision rails can actually consume.
# SVG stays text (source), not a vision payload.
RASTER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# Match user-attachment cap in attachments.py so a pasted-size file on disk
# can still reach a native vision model (desktop_observe keeps its own 4 MiB cap).
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_ATTACH_BYTES = 8 * 1024 * 1024

_WIN_ABS = re.compile(r"^[a-zA-Z]:[\\/]")


def is_raster_image_path(path: str | Path) -> bool:
    name = str(path or "").strip()
    if not name:
        return False
    if name.lower().startswith("file:"):
        try:
            parsed = urlparse(name)
            name = unquote(parsed.path or "")
            if os.name == "nt" and name.startswith("/") and _WIN_ABS.match(name[1:]):
                name = name[1:]
        except ValueError:
            return False
    suffix = Path(name.split("?", 1)[0]).suffix.lower()
    return suffix in RASTER_IMAGE_EXTS


def image_mime_for_path(path: str | Path) -> str:
    return _IMAGE_MIME.get(Path(str(path)).suffix.lower(), "image/png")


def _is_absolute_path_text(text: str) -> bool:
    candidate = Path(text)
    return (
        candidate.is_absolute()
        or bool(_WIN_ABS.match(text))
        or text.startswith(("\\\\", "//"))
    )


def _normalize_path_text(raw: str) -> str:
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path or "")
        if os.name == "nt" and text.startswith("/") and _WIN_ABS.match(text[1:]):
            text = text[1:]
    return os.path.expandvars(os.path.expanduser(text))


def resolve_inspectable_image(
    raw: str,
    *,
    search_roots: Iterable[Path | str] | None = None,
) -> Path | None:
    """Resolve an image path the way a normal agent Read() would.

    Absolute / ``file:`` paths win as-is. Relative names are tried against
    each search root (sandbox, bound project, codebase, cwd) in order.
    """
    text = _normalize_path_text(raw)
    if not text:
        return None
    try:
        if _is_absolute_path_text(text):
            target = Path(text).resolve()
            return target if target.is_file() else None
    except OSError:
        return None

    cleaned = text.lstrip("./").lstrip(".\\")
    if not cleaned:
        return None
    seen: set[str] = set()
    for root in search_roots or ():
        try:
            base = Path(root).expanduser().resolve()
        except OSError:
            continue
        key = str(base).lower() if os.name == "nt" else str(base)
        if key in seen:
            continue
        seen.add(key)
        try:
            candidate = (base / cleaned).resolve()
        except OSError:
            continue
        try:
            candidate.relative_to(base)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def load_image_file(path: Path | str) -> dict[str, Any]:
    """Read image bytes from disk. Does not require a sandbox root."""
    try:
        raw = str(path)
        text = _normalize_path_text(raw) if not isinstance(path, Path) else str(path)
        target = Path(text or raw).expanduser().resolve()
    except OSError as exc:
        return {"ok": False, "error": f"bad path: {exc}"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"file not found: {target}", "path": str(target)}
    if not is_raster_image_path(target):
        return {
            "ok": False,
            "error": f"not a raster image ({target.suffix or 'no extension'})",
            "path": str(target),
        }
    try:
        size = target.stat().st_size
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(target)}
    if size > MAX_IMAGE_BYTES:
        return {
            "ok": False,
            "error": f"image too large ({size} bytes > {MAX_IMAGE_BYTES})",
            "path": str(target),
            "size": size,
        }
    try:
        data = target.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}", "path": str(target)}
    mime = image_mime_for_path(target)
    return {
        "ok": True,
        "path": str(target),
        "name": target.name,
        "mime": mime,
        "size": len(data),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def load_image_from_attachment(att: dict[str, Any]) -> dict[str, Any]:
    """Prefer inline bytes; fall back to a stored sandbox/host path."""
    if not isinstance(att, dict):
        return {"ok": False, "error": "invalid attachment"}
    data = att.get("data_base64") or att.get("data") or ""
    mime = str(att.get("mime") or "image/png")
    name = str(att.get("name") or "image")
    if isinstance(data, str) and data.strip():
        encoded = data.strip()
        if encoded.startswith("data:"):
            marker = encoded.find(",")
            if marker >= 0:
                encoded = encoded[marker + 1:]
        encoded = "".join(encoded.split())
        try:
            payload = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"invalid image base64: {exc}"}
        if len(payload) > MAX_IMAGE_BYTES:
            return {"ok": False, "error": f"image too large ({len(payload)} bytes > {MAX_IMAGE_BYTES})"}
        return {
            "ok": True,
            "name": name,
            "mime": mime or "image/png",
            "size": len(payload),
            "data_base64": base64.b64encode(payload).decode("ascii"),
            "path": str(att.get("host_path") or att.get("sandbox_path") or att.get("path") or ""),
        }
    for key in ("host_path", "sandbox_path", "path"):
        raw = str(att.get(key) or "").strip()
        if not raw:
            continue
        loaded = load_image_file(raw)
        if loaded.get("ok"):
            if name and name != "image":
                loaded["name"] = name
            if mime and mime != "image/png":
                loaded["mime"] = mime
            return loaded
    return {"ok": False, "error": "image has no bytes or readable path"}


def assistant_image_attachment(loaded: dict[str, Any]) -> dict[str, Any] | None:
    if not loaded.get("ok") or not loaded.get("data_base64"):
        return None
    size = int(loaded.get("size") or 0)
    if size > MAX_IMAGE_ATTACH_BYTES:
        return None
    return {
        "type": "image",
        "mime": loaded.get("mime") or "image/png",
        "name": loaded.get("name") or "image",
        "data_base64": loaded["data_base64"],
    }


def image_read_tool_result(path: Path | str) -> dict[str, Any]:
    """Payload for sandbox_read_file / codebase_read / read_text_file on an image."""
    loaded = load_image_file(path)
    if not loaded.get("ok"):
        return loaded
    attachment = assistant_image_attachment(loaded)
    out: dict[str, Any] = {
        "ok": True,
        "kind": "image",
        "path": loaded.get("path") or str(path),
        "name": loaded.get("name") or Path(str(path)).name,
        "mime": loaded.get("mime") or "image/png",
        "size": loaded.get("size") or 0,
        "attached_to_model": bool(attachment),
        "hint": (
            "Image file loaded in place — you do not need to copy it into the session sandbox. "
            "Native vision models see the pixels on the next turn. "
            "Call vision_inspect_image(path=...) if you need a textual description."
        ),
    }
    if attachment:
        out["_assistant_attachments"] = [attachment]
    else:
        out["attachment_skipped_reason"] = (
            f"image too large for model attachment ({loaded.get('size')} bytes > {MAX_IMAGE_ATTACH_BYTES})"
        )
    return out
