"""Save proxy flow bodies and open export directories."""

from __future__ import annotations

import platform
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from argusai.core.security.proxy import TrafficFlow

Part = Literal["response", "request"]


def proxy_exports_dir(ca_dir: Path) -> Path:
    return Path(ca_dir) / "exports"


def proxy_captures_dir(ca_dir: Path) -> Path:
    return Path(ca_dir) / "captures"


def _safe_filename(name: str, *, fallback: str = "asset") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._")[:120]
    return cleaned or fallback


_MIME_TO_EXT: dict[str, str] = {
    "application/json": ".json",
    "application/ld+json": ".json",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/wasm": ".wasm",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/x-tar": ".tar",
    "application/gzip": ".gz",
    "application/x-protobuf": ".pb",
    "text/html": ".html",
    "text/css": ".css",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/mp2t": ".ts",
    "application/vnd.apple.mpegurl": ".m3u8",
    "application/x-mpegurl": ".m3u8",
    "application/dash+xml": ".mpd",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".weba",
    "audio/aac": ".aac",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
}


def _guess_extension(content_type: str, filename_hint: str) -> str:
    if filename_hint and "." in filename_hint:
        return Path(filename_hint).suffix
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _MIME_TO_EXT:
        return _MIME_TO_EXT[ct]
    if ct.endswith("+json"):
        return ".json"
    if ct.endswith("+xml"):
        return ".xml"
    return ".bin"


def save_flow_body(
    flow: TrafficFlow,
    ca_dir: Path,
    *,
    part: Part = "response",
) -> dict[str, Any]:
    data = flow.response_body if part == "response" else flow.request_body
    if not data:
        return {"ok": False, "error": f"empty {part} body"}

    captures = proxy_captures_dir(ca_dir)
    captures.mkdir(parents=True, exist_ok=True)

    base = _safe_filename(flow.filename_hint or flow.host or "flow")
    ext = _guess_extension(flow.content_type if part == "response" else "", flow.filename_hint)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}_flow{flow.id}_{part}_{base}{ext}"
    path = captures / filename
    if path.exists():
        path = captures / f"{stamp}_flow{flow.id}_{part}_{base}_{int(time.time() * 1000)}{ext}"

    path.write_bytes(data)
    return {
        "ok": True,
        "path": str(path),
        "part": part,
        "size_bytes": len(data),
        "flow_id": flow.id,
        "url": flow.url,
    }


def reveal_dir(path: Path) -> dict[str, Any]:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        sys_name = platform.system().lower()
        if sys_name.startswith("win"):
            import os

            os.startfile(str(path))  # noqa: S606
        elif sys_name == "darwin":
            subprocess.run(["open", str(path)], check=False, timeout=10)
        else:
            subprocess.run(["xdg-open", str(path)], check=False, timeout=10)
        return {"ok": True, "path": str(path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
