"""Safe, provider-neutral channel attachment loading and download helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import mimetypes
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

from .models import ChannelAttachment, ChannelAttachmentKind

MAX_ATTACHMENTS = 8
GLOBAL_FILE_LIMIT = 32 * 1024 * 1024
TEXT_LIMIT = 4 * 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
AUDIO_LIMIT = 8 * 1024 * 1024
VIDEO_LIMIT = 32 * 1024 * 1024
FILE_LIMIT = GLOBAL_FILE_LIMIT

ATTACHMENT_LIMITS: dict[str, int] = {
    "text": TEXT_LIMIT,
    "image": IMAGE_LIMIT,
    "audio": AUDIO_LIMIT,
    "video": VIDEO_LIMIT,
    "file": FILE_LIMIT,
}

_MIME_BY_EXTENSION = {
    # Text, source, and structured data.
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "rst": "text/plain",
    "log": "text/plain",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "jsonl": "application/x-ndjson",
    "ndjson": "application/x-ndjson",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "toml": "application/toml",
    "xml": "application/xml",
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "mjs": "application/javascript",
    "cjs": "application/javascript",
    "ts": "application/typescript",
    "tsx": "application/typescript",
    "jsx": "text/jsx",
    "py": "text/x-python",
    "pyi": "text/x-python",
    "sh": "text/x-shellscript",
    "bash": "text/x-shellscript",
    "zsh": "text/x-shellscript",
    "ps1": "text/plain",
    "bat": "text/plain",
    "cmd": "text/plain",
    "sql": "application/sql",
    "graphql": "application/graphql",
    "gql": "application/graphql",
    "go": "text/x-go",
    "rs": "text/x-rust",
    "java": "text/x-java-source",
    "c": "text/x-c",
    "cc": "text/x-c++",
    "cpp": "text/x-c++",
    "h": "text/x-c",
    "hpp": "text/x-c++",
    "vue": "text/plain",
    "svelte": "text/plain",
    "patch": "text/x-diff",
    "diff": "text/x-diff",
    "har": "application/json",
    # Images, audio, and video.
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "svg": "image/svg+xml",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "m4v": "video/x-m4v",
    # Documents and archives.
    "pdf": "application/pdf",
    "rtf": "application/rtf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "zip": "application/zip",
    "7z": "application/x-7z-compressed",
    "rar": "application/vnd.rar",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "tgz": "application/gzip",
    "bz2": "application/x-bzip2",
    "xz": "application/x-xz",
    "jar": "application/java-archive",
    # Installers and binary-analysis samples. They are inert bytes here.
    "exe": "application/vnd.microsoft.portable-executable",
    "dll": "application/vnd.microsoft.portable-executable",
    "msi": "application/x-msi",
    "apk": "application/vnd.android.package-archive",
    "aab": "application/octet-stream",
    "ipa": "application/octet-stream",
    "dmg": "application/x-apple-diskimage",
    "pkg": "application/octet-stream",
    "deb": "application/vnd.debian.binary-package",
    "rpm": "application/x-rpm",
    "bin": "application/octet-stream",
    "so": "application/x-sharedlib",
    "dylib": "application/x-sharedlib",
    "elf": "application/x-elf",
    "wasm": "application/wasm",
    "dex": "application/vnd.android.dex",
    "class": "application/java-vm",
    "pcap": "application/vnd.tcpdump.pcap",
}

_TEXT_MIMES = {
    "application/graphql",
    "application/javascript",
    "application/json",
    "application/ld+json",
    "application/sql",
    "application/toml",
    "application/typescript",
    "application/x-javascript",
    "application/x-ndjson",
    "application/x-python",
    "application/x-shellscript",
    "application/x-toml",
    "application/x-yaml",
    "application/xml",
    "application/yaml",
    "image/svg+xml",
}

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ChannelMediaError(ValueError):
    """Attachment input is malformed, unsafe, or over its configured cap."""


def safe_filename(
    name: str,
    fallback: str = "attachment.bin",
    *,
    max_length: int = 120,
) -> str:
    """Return a path-free filename while preserving a safe original extension."""

    raw = str(name or "").replace("\x00", "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = raw.strip(" .") or fallback
    cleaned = "".join(
        character
        if character.isalnum() or character in "._-+()[] "
        else "_"
        for character in raw
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .") or fallback
    if cleaned in {".", ".."}:
        cleaned = fallback
    stem = cleaned.rsplit(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        suffix = Path(cleaned).suffix[:20]
        stem_length = max(1, max_length - len(suffix))
        cleaned = cleaned[:stem_length].rstrip(" .") + suffix
    return cleaned or fallback


def _extension(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    return suffix[1:] if suffix.startswith(".") else suffix


def detect_mime(
    mime: str = "",
    name: str = "",
    data: bytes | None = None,
) -> str:
    """Resolve a deterministic MIME from declaration, signature, then suffix."""

    declared = str(mime or "").lower().split(";", 1)[0].strip()
    if declared and declared not in {"application/octet-stream", "binary/octet-stream"}:
        return declared
    sample = data[:16] if data else b""
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"\x1f\x8b", "application/gzip"),
        (b"\x7fELF", "application/x-elf"),
        (b"MZ", "application/vnd.microsoft.portable-executable"),
        (b"\x00asm", "application/wasm"),
    )
    for signature, detected in signatures:
        if sample.startswith(signature):
            return detected
    if sample.startswith(b"RIFF") and sample[8:12] == b"WAVE":
        return "audio/wav"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "image/webp"
    extension = _extension(name)
    if extension in _MIME_BY_EXTENSION:
        return _MIME_BY_EXTENSION[extension]
    guessed, _encoding = mimetypes.guess_type(str(name or ""), strict=False)
    return str(guessed or declared or "application/octet-stream").lower()


def detect_kind(mime: str = "", name: str = "") -> ChannelAttachmentKind:
    normalized_mime = str(mime or "").lower().split(";", 1)[0].strip()
    if normalized_mime.startswith("image/") and normalized_mime != "image/svg+xml":
        return "image"
    if normalized_mime.startswith("video/"):
        return "video"
    if normalized_mime.startswith("audio/"):
        return "audio"
    if normalized_mime.startswith("text/") or normalized_mime in _TEXT_MIMES:
        return "text"
    extension = _extension(name)
    extension_mime = _MIME_BY_EXTENSION.get(extension, "")
    if extension_mime and extension_mime != normalized_mime:
        return detect_kind(extension_mime, "")
    return "file"


def attachment_limit(kind: str) -> int:
    return min(ATTACHMENT_LIMITS.get(str(kind or "file").lower(), FILE_LIMIT), GLOBAL_FILE_LIMIT)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_base64(data: str, *, limit: int = GLOBAL_FILE_LIMIT) -> bytes:
    """Decode base64 exactly, rejecting malformed or oversized input early."""

    if not isinstance(data, str):
        raise ChannelMediaError("attachment data_base64 must be a string")
    encoded = data
    if encoded.startswith("data:"):
        marker = encoded.find(",")
        if marker < 0 or ";base64" not in encoded[:marker].lower():
            raise ChannelMediaError("attachment data URI must use base64")
        encoded = encoded[marker + 1:]
    encoded = "".join(encoded.split())
    if not encoded:
        raise ChannelMediaError("attachment data_base64 is empty")
    padding = len(encoded) - len(encoded.rstrip("="))
    estimated = (len(encoded) // 4) * 3 - min(padding, 2)
    if estimated > limit:
        raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChannelMediaError("attachment data_base64 is invalid") from exc
    if len(decoded) > limit:
        raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
    return decoded


def _read_path(path: str, *, limit: int) -> bytes:
    source = Path(path).expanduser()
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ChannelMediaError(f"attachment path is not readable: {source}") from exc
    if not source.is_file():
        raise ChannelMediaError(f"attachment path is not a file: {source}")
    if size > limit:
        raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
    with source.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
    return data


def _source_bytes(
    value: ChannelAttachment | Mapping[str, Any] | bytes | bytearray | memoryview,
    *,
    provisional_limit: int = GLOBAL_FILE_LIMIT,
    allow_path: bool = True,
) -> tuple[bytes, str, str, str, str, Mapping[str, Any]]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value), "", "", "", "", {}
    mapping: Mapping[str, Any]
    if isinstance(value, ChannelAttachment):
        mapping = value.to_dict()
    elif isinstance(value, Mapping):
        mapping = value
    else:
        raise ChannelMediaError("attachment must be bytes, a mapping, or ChannelAttachment")
    for key in ("url", "download_url", "href"):
        if mapping.get(key):
            raise ChannelMediaError("caller-supplied attachment URLs are not accepted")
    name = str(mapping.get("name") or mapping.get("filename") or "")
    mime = str(mapping.get("mime") or mapping.get("content_type") or "")
    caption = str(mapping.get("caption") or "")
    requested_kind = str(mapping.get("kind") or mapping.get("type") or "")
    raw_data = mapping.get("bytes")
    if isinstance(raw_data, (bytes, bytearray, memoryview)):
        return bytes(raw_data), name, mime, caption, requested_kind, mapping
    raw_data = mapping.get("data_base64")
    if raw_data is None:
        raw_data = mapping.get("data")
    if isinstance(raw_data, (bytes, bytearray, memoryview)):
        return bytes(raw_data), name, mime, caption, requested_kind, mapping
    if isinstance(raw_data, str) and raw_data:
        return (
            decode_base64(raw_data, limit=provisional_limit),
            name,
            mime,
            caption,
            requested_kind,
            mapping,
        )
    if "text" in mapping and isinstance(mapping.get("text"), str):
        return (
            str(mapping.get("text") or "").encode("utf-8"),
            name,
            mime or "text/plain",
            caption,
            requested_kind or "text",
            mapping,
        )
    path = str(
        mapping.get("path")
        or mapping.get("sandbox_path")
        or mapping.get("file_path")
        or ""
    )
    if path:
        if not allow_path:
            raise ChannelMediaError("attachment paths are not accepted from this caller")
        return (
            _read_path(path, limit=provisional_limit),
            name or Path(path).name,
            mime,
            caption,
            requested_kind,
            mapping,
        )
    raise ChannelMediaError("attachment has no bytes, base64, text, or path content")


def attachment_from_bytes(
    data: bytes,
    *,
    mime: str = "",
    name: str = "",
    caption: str = "",
    metadata: Mapping[str, Any] | None = None,
    requested_kind: str = "",
) -> ChannelAttachment:
    if not isinstance(data, bytes):
        data = bytes(data)
    resolved_mime = detect_mime(mime, name, data)
    detected_kind = detect_kind(resolved_mime, name)
    kind = (
        requested_kind.lower()
        if requested_kind.lower() in ATTACHMENT_LIMITS
        else detected_kind
    )
    limit = attachment_limit(kind)
    if len(data) > limit:
        raise ChannelMediaError(f"{kind} attachment exceeds the {limit} byte limit")
    fallback_extension = {
        "text": ".txt",
        "image": ".bin",
        "audio": ".bin",
        "video": ".bin",
        "file": ".bin",
    }[kind]
    safe_name = safe_filename(name, f"attachment{fallback_extension}")
    digest = sha256_bytes(data)
    if kind == "text":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            kind = "file"
        else:
            return ChannelAttachment(
                kind="text",
                name=safe_name,
                mime=resolved_mime,
                size=len(data),
                sha256=digest,
                text=text,
                caption=caption,
                metadata=dict(metadata or {}),
            )
    return ChannelAttachment(
        kind=kind,  # type: ignore[arg-type]
        name=safe_name,
        mime=resolved_mime,
        size=len(data),
        sha256=digest,
        data_base64=base64.b64encode(data).decode("ascii"),
        caption=caption,
        metadata=dict(metadata or {}),
    )


def normalize_attachment(
    value: ChannelAttachment | Mapping[str, Any] | bytes | bytearray | memoryview,
    *,
    allow_path: bool = True,
) -> ChannelAttachment:
    data, name, mime, caption, requested_kind, mapping = _source_bytes(
        value,
        allow_path=allow_path,
    )
    resolved_mime = detect_mime(mime, name, data)
    kind = (
        requested_kind.lower()
        if requested_kind.lower() in ATTACHMENT_LIMITS
        else detect_kind(resolved_mime, name)
    )
    limit = attachment_limit(kind)
    if len(data) > limit:
        raise ChannelMediaError(f"{kind} attachment exceeds the {limit} byte limit")
    expected_hash = str(mapping.get("sha256") or "").lower()
    digest = sha256_bytes(data)
    if expected_hash and not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ChannelMediaError("attachment sha256 must be 64 lowercase hex characters")
    if expected_hash and not hmac.compare_digest(expected_hash, digest):
        raise ChannelMediaError("attachment sha256 does not match its content")
    metadata = mapping.get("metadata")
    return attachment_from_bytes(
        data,
        mime=resolved_mime,
        name=name,
        caption=caption,
        metadata=metadata if isinstance(metadata, Mapping) else {},
        requested_kind=kind,
    )


def normalize_attachments(
    values: Iterable[ChannelAttachment | Mapping[str, Any] | bytes] | None,
    *,
    allow_path: bool = True,
) -> tuple[ChannelAttachment, ...]:
    items = list(values or [])
    if len(items) > MAX_ATTACHMENTS:
        raise ChannelMediaError(f"at most {MAX_ATTACHMENTS} attachments are allowed")
    return tuple(
        normalize_attachment(item, allow_path=allow_path)
        for item in items
    )


def attachment_bytes(value: ChannelAttachment | Mapping[str, Any]) -> bytes:
    data, _name, _mime, _caption, _kind, _mapping = _source_bytes(value)
    normalized = normalize_attachment(value)
    if len(data) != normalized.size:
        raise ChannelMediaError("attachment size changed while loading")
    return data


def to_assistant_dict(attachment: ChannelAttachment) -> dict[str, Any]:
    """Convert the contract into the mutable shape consumed by the assistant."""

    result = {
        "type": attachment.kind,
        "mime": attachment.mime,
        "name": attachment.name,
        "size": attachment.size,
        "sha256": attachment.sha256,
    }
    if attachment.caption:
        result["caption"] = attachment.caption
    if attachment.kind == "text":
        result["text"] = attachment.text
    else:
        result["data_base64"] = attachment.data_base64
    return result


def _content_length(headers: httpx.Headers, limit: int) -> int:
    raw = headers.get("Content-Length")
    if not raw:
        return 0
    try:
        length = int(raw)
    except (TypeError, ValueError) as exc:
        raise ChannelMediaError("attachment response has an invalid Content-Length") from exc
    if length < 0 or length > limit:
        raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
    return length


def download_attachment(
    client: httpx.Client,
    url: str,
    *,
    mime: str = "",
    name: str = "",
    headers: Mapping[str, str] | None = None,
) -> ChannelAttachment:
    """Stream one provider-authenticated URL with pre/post size guards."""

    provisional_kind = detect_kind(detect_mime(mime, name), name)
    limit = attachment_limit(provisional_kind)
    chunks: list[bytes] = []
    total = 0
    with client.stream(
        "GET",
        url,
        headers=dict(headers or {}),
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        response_mime = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
        effective_mime = mime or response_mime
        response_kind = detect_kind(detect_mime(effective_mime, name), name)
        limit = attachment_limit(response_kind)
        _content_length(response.headers, limit)
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit or total > GLOBAL_FILE_LIMIT:
                raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
            chunks.append(chunk)
    return attachment_from_bytes(
        b"".join(chunks),
        mime=effective_mime,
        name=name,
    )


async def download_attachment_async(
    client: httpx.AsyncClient,
    url: str,
    *,
    mime: str = "",
    name: str = "",
    headers: Mapping[str, str] | None = None,
) -> ChannelAttachment:
    provisional_kind = detect_kind(detect_mime(mime, name), name)
    limit = attachment_limit(provisional_kind)
    chunks: list[bytes] = []
    total = 0
    async with client.stream(
        "GET",
        url,
        headers=dict(headers or {}),
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        response_mime = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
        effective_mime = mime or response_mime
        response_kind = detect_kind(detect_mime(effective_mime, name), name)
        limit = attachment_limit(response_kind)
        _content_length(response.headers, limit)
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit or total > GLOBAL_FILE_LIMIT:
                raise ChannelMediaError(f"attachment exceeds the {limit} byte limit")
            chunks.append(chunk)
    return attachment_from_bytes(
        b"".join(chunks),
        mime=effective_mime,
        name=name,
    )


# Straightforward aliases make the helper discoverable without tying callers to
# one naming convention.
attachment_kind = detect_kind
load_attachment = normalize_attachment
download_attachment_sync = download_attachment

__all__ = [
    "ATTACHMENT_LIMITS",
    "AUDIO_LIMIT",
    "ChannelMediaError",
    "FILE_LIMIT",
    "GLOBAL_FILE_LIMIT",
    "IMAGE_LIMIT",
    "MAX_ATTACHMENTS",
    "TEXT_LIMIT",
    "VIDEO_LIMIT",
    "attachment_bytes",
    "attachment_from_bytes",
    "attachment_kind",
    "attachment_limit",
    "decode_base64",
    "detect_kind",
    "detect_mime",
    "download_attachment",
    "download_attachment_async",
    "download_attachment_sync",
    "load_attachment",
    "normalize_attachment",
    "normalize_attachments",
    "safe_filename",
    "sha256_bytes",
    "to_assistant_dict",
]
