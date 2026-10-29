"""Assistant runtime helpers split from the former monolithic module."""

from __future__ import annotations

import base64
import binascii
import hashlib
import inspect
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

log = logging.getLogger("argusai.assistant")

__all__ = ['_ATTACHMENT_IMAGE_MIME', '_ATTACHMENT_VIDEO_MIME', '_ATTACHMENT_MAX_BYTES', '_ATTACHMENT_VIDEO_MAX_BYTES', '_ATTACHMENT_FILE_MAX_BYTES', '_ATTACHMENT_MAX_COUNT', '_ATTACHMENT_TEXT_MAX_BYTES', '_ATTACHMENT_TEXT_INLINE_BUDGET', '_ATTACHMENT_TEXT_EXT_ALLOW', '_ATTACHMENT_TEXT_MIME_ALLOW_PREFIXES', '_attachment_text_ext_ok', '_attachment_text_mime_ok', '_safe_attachment_filename', '_materialize_acp_media_attachments', '_materialize_binary_attachments', '_materialize_text_attachments', '_materialize_video', '_materialize_voice', '_sanitize_attachments', '_sanitize_voice']

_ATTACHMENT_IMAGE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
_ATTACHMENT_AUDIO_MIME = {
    "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mpeg", "audio/mp3",
    "audio/ogg", "audio/webm",
    "audio/mp4", "audio/m4a", "audio/x-m4a",
}
_ATTACHMENT_VIDEO_MIME = {
    "video/mp4", "video/webm", "video/quicktime",
    "video/x-matroska", "video/x-msvideo", "video/x-m4v",
}
_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
# Video gets its own, larger budget: 8 MiB is under ten seconds of phone
# capture. It is still spilled to the sandbox after the turn (see
# _materialize_video) so session JSON doesn't carry the base64 forever.
_ATTACHMENT_VIDEO_MAX_BYTES = 32 * 1024 * 1024
_ATTACHMENT_FILE_MAX_BYTES = 32 * 1024 * 1024
_ATTACHMENT_MAX_COUNT = 8
# Text attachments may be large (think minified bundles or trace logs);
# we cap individual file size here and let the upstream materializer
# decide whether to inline a preview vs only reference a sandbox path.
_ATTACHMENT_TEXT_MAX_BYTES = 4 * 1024 * 1024
_ATTACHMENT_TEXT_INLINE_BUDGET = 8 * 1024  # 8 KB inlined verbatim
_ATTACHMENT_TEXT_EXT_ALLOW = {
    "md", "markdown", "mdx", "rst", "txt", "log", "csv", "tsv",
    "json", "jsonl", "ndjson", "yaml", "yml", "toml", "ini", "cfg", "env",
    "xml", "html", "htm", "svg",
    "js", "mjs", "cjs", "jsx", "ts", "tsx",
    "py", "pyi", "pyw",
    "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd",
    "go", "rs", "java", "kt", "kts", "rb", "php", "pl", "cs", "swift",
    "c", "cc", "cpp", "cxx", "h", "hpp",
    "sql", "graphql", "gql",
    "css", "scss", "sass", "less",
    "vue", "svelte",
    "dockerfile", "makefile", "cmake", "gradle",
    "patch", "diff", "lock",
    "har", "pcap",
}
_ATTACHMENT_TEXT_MIME_ALLOW_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/x-javascript",
    "application/typescript",
    "application/x-typescript",
    "application/x-shellscript",
    "application/x-python",
    "application/sql",
    "application/x-toml",
)


def _attachment_text_ext_ok(name: str) -> bool:
    if not name:
        return False
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in _ATTACHMENT_TEXT_EXT_ALLOW


def _attachment_text_mime_ok(mime: str) -> bool:
    if not mime:
        return False
    m = mime.lower()
    return any(m == p or m.startswith(p) for p in _ATTACHMENT_TEXT_MIME_ALLOW_PREFIXES)


def _safe_attachment_filename(
    name: str,
    fallback_idx: int,
    *,
    preserve_extension: bool = False,
) -> str:
    """Coerce a user-supplied filename into something we are willing to drop
    onto disk. We deliberately rebuild rather than sanitize because the
    name flows in from the renderer over the GUI bridge — even on a single
    user's machine we don't want a malicious `.html` blob to land at e.g.
    ``..\\\\system32\\\\evil.cmd`` if pywebview ever decides not to escape.
    """

    raw = (name or "").strip()
    if not raw:
        raw = f"attachment-{fallback_idx}.{'bin' if preserve_extension else 'txt'}"
    # Strip path components, keep last segment only.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # Drop anything not in [A-Za-z0-9 . _ -]; collapse whitespace.
    out_chars: list[str] = []
    for ch in raw:
        if ch.isalnum() or ch in "._- ":
            out_chars.append(ch)
        elif ch in {"+", "(", ")", "[", "]"}:
            out_chars.append(ch)
        else:
            out_chars.append("_")
    cleaned = "".join(out_chars).strip(" .") or (
        f"attachment-{fallback_idx}.{'bin' if preserve_extension else 'txt'}"
    )
    # Force an allowed extension; if none, default to .txt so sandbox
    # write tools (which check ALLOWED_TEXT_EXTS) won't reject the file.
    if "." not in cleaned:
        cleaned += ".bin" if preserve_extension else ".txt"
    elif not preserve_extension:
        ext = cleaned.rsplit(".", 1)[-1].lower()
        if ext not in _ATTACHMENT_TEXT_EXT_ALLOW:
            cleaned = cleaned.rsplit(".", 1)[0] + ".txt"
    # Cap length so we never trip Windows MAX_PATH.
    if len(cleaned) > 120:
        stem, _, ext = cleaned.rpartition(".")
        cleaned = (stem[:100] + "." + ext) if ext else stem[:120]
    return cleaned


def _attachment_host_path(item: dict[str, Any] | None) -> str:
    from .host_paths import attachment_host_path

    return attachment_host_path(item)


def _with_host_path(item: dict[str, Any], host_path: str) -> dict[str, Any]:
    if host_path:
        item["host_path"] = host_path
        item["path"] = host_path
    return item


def _host_path_file_note(name: str, host_path: str, size: int | None = None) -> str:
    size_bit = f" ({size:,} bytes)" if isinstance(size, int) and size > 0 else ""
    return (
        f"- **{name}**{size_bit}\n"
        f"  host path: `{host_path}`\n"
        "  original path — read with `stat_text_file` / `grep_text` / "
        "`read_text_file` / `vision_inspect_image`. Keep generated artifacts "
        "in the session sandbox."
    )


def _materialize_text_attachments(
    attachments: list[dict[str, Any]],
    sandbox_root: Path | str | None,
) -> tuple[list[dict[str, Any]], str]:
    """For each ``type=='text'`` attachment, spill its body into the
    session sandbox under ``_attachments/`` and return:

    - a copy of ``attachments`` where text entries have their ``text``
      field replaced with a sandbox path reference (avoids the huge
      payload sticking around in session JSON forever); and
    - a Markdown-ish addendum to splice onto the user's prompt so the
      model both knows the file is there and gets a free preview for
      small ones.

    If ``sandbox_root`` is None (e.g. unit tests, CLI), we keep the text
    inline up to ``_ATTACHMENT_TEXT_INLINE_BUDGET`` and silently drop the
    rest, but still emit a prompt addendum so the model knows what came
    in. This is the degraded mode — the real product path is GUI →
    sandbox.
    """

    if not attachments:
        return [], ""

    out: list[dict[str, Any]] = []
    notes: list[str] = []
    text_idx = 0
    seen_names: set[str] = set()

    root: Path | None = None
    if sandbox_root is not None:
        root = Path(sandbox_root)
        try:
            (root / "_attachments").mkdir(parents=True, exist_ok=True)
        except Exception:
            root = None

    for att in attachments:
        if (att.get("type") or "").lower() != "text":
            out.append(att)
            continue
        host_path = _attachment_host_path(att)
        text_idx += 1
        text = att.get("text") or ""
        if host_path and not text:
            name = Path(host_path).name or _safe_attachment_filename(att.get("name") or "", text_idx)
            stored = _with_host_path(
                {
                    "type": "text",
                    "name": name,
                    "mime": att.get("mime") or "text/plain",
                    "size": int(att.get("size") or 0),
                },
                host_path,
            )
            notes.append(_host_path_file_note(name, host_path, stored.get("size")))
            out.append(stored)
            continue
        size = int(att.get("size") or len(text.encode("utf-8", errors="ignore")))
        name = _safe_attachment_filename(att.get("name") or "", text_idx)
        # De-dup filenames within a single turn so the user dragging
        # in two `index.html`s doesn't clobber the first one.
        base_name = name
        attempt = 1
        while name in seen_names:
            attempt += 1
            stem, _, ext = base_name.rpartition(".")
            name = f"{stem or base_name}-{attempt}.{ext}" if ext else f"{base_name}-{attempt}"
        seen_names.add(name)

        sandbox_path: str | None = None
        rel_path: str | None = None
        write_error: str | None = None
        if host_path:
            pass
        elif root is not None:
            target = root / "_attachments" / name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="")
                sandbox_path = str(target.resolve())
                rel_path = f"_attachments/{name}"
            except Exception as exc:
                write_error = f"sandbox write failed: {exc}"

        preview = ""
        truncated = False
        if size <= _ATTACHMENT_TEXT_INLINE_BUDGET:
            preview = text
        else:
            preview = text[:_ATTACHMENT_TEXT_INLINE_BUDGET]
            truncated = True

        note_lines = [f"- **{name}** ({size:,} bytes)"]
        if host_path:
            note_lines.append(f"  host path: `{host_path}`")
            note_lines.append("  original path — sandbox copy skipped")
        elif sandbox_path:
            note_lines.append(f"  saved at: `{sandbox_path}`")
            note_lines.append(f"  sandbox path: `{rel_path}`")
        elif write_error:
            note_lines.append(f"  ⚠ {write_error} (content shown inline below only)")
        else:
            note_lines.append("  (no sandbox available; content shown inline below only)")

        if preview:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            fence_lang = {
                "md": "markdown", "markdown": "markdown",
                "py": "python", "js": "javascript", "ts": "typescript",
                "json": "json", "yaml": "yaml", "yml": "yaml",
                "html": "html", "css": "css", "sql": "sql",
                "sh": "bash", "bash": "bash",
                "go": "go", "rs": "rust", "java": "java",
                "rb": "ruby", "php": "php", "c": "c", "cpp": "cpp",
                "toml": "toml", "xml": "xml", "vue": "vue", "svelte": "svelte",
            }.get(ext, "")
            fence = f"```{fence_lang}" if fence_lang else "```"
            note_lines.append("  preview:")
            note_lines.append(f"  {fence}")
            for line in preview.splitlines():
                note_lines.append(f"  {line}")
            note_lines.append("  ```")
            if truncated and sandbox_path:
                note_lines.append(
                    f"  ⚠ preview truncated at {_ATTACHMENT_TEXT_INLINE_BUDGET // 1024} KB — "
                    f"full file is at the sandbox path above; use `read_text_file` / `grep_text` to read more."
                )
            elif truncated:
                note_lines.append(
                    f"  ⚠ preview truncated at {_ATTACHMENT_TEXT_INLINE_BUDGET // 1024} KB; "
                    f"sandbox spill failed so the rest of the file is unavailable this turn."
                )

        notes.append("\n".join(note_lines))

        stored: dict[str, Any] = {
            "type": "text",
            "name": name,
            "mime": att.get("mime") or "text/plain",
            "size": size,
        }
        if host_path:
            stored = _with_host_path(stored, host_path)
        if sandbox_path:
            stored["sandbox_path"] = sandbox_path
            stored["sandbox_rel"] = rel_path
        elif preview:
            # No sandbox — keep the inlined preview around so reloading
            # the session at least shows what was attached.
            stored["text"] = preview
            stored["truncated"] = truncated
        out.append(stored)

    addendum = ""
    if notes:
        addendum = (
            "\n\n---\n"
            "Files attached to this user turn:\n\n"
            + "\n\n".join(notes)
        )

    return out, addendum


def _decode_binary_attachment(data: str, *, limit: int) -> bytes:
    encoded = str(data or "")
    if encoded.startswith("data:"):
        marker = encoded.find(",")
        if marker < 0 or ";base64" not in encoded[:marker].lower():
            raise ValueError("invalid attachment data URI")
        encoded = encoded[marker + 1:]
    encoded = "".join(encoded.split())
    if not encoded:
        raise ValueError("empty attachment data")
    padding = len(encoded) - len(encoded.rstrip("="))
    estimated = (len(encoded) // 4) * 3 - min(padding, 2)
    if estimated > limit:
        raise ValueError("attachment is too large")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid attachment base64") from exc
    if len(payload) > limit:
        raise ValueError("attachment is too large")
    return payload


def _materialize_binary_attachments(
    attachments: list[dict[str, Any]],
    sandbox_root: Path | str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Spill inert generic file bytes under the session ``_attachments`` dir.

    The returned attachment metadata never contains raw/base64 binary. The LLM
    receives only a relative sandbox path, byte count, and SHA-256 digest.
    """

    if not attachments:
        return [], ""
    root = Path(sandbox_root) if sandbox_root is not None else None
    attachment_dir = root / "_attachments" if root is not None else None
    if attachment_dir is not None:
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            attachment_dir = None

    out: list[dict[str, Any]] = []
    notes: list[str] = []
    seen_names: set[str] = set()
    file_index = 0
    for attachment in attachments:
        if str(attachment.get("type") or "").lower() != "file":
            out.append(attachment)
            continue
        host_path = _attachment_host_path(attachment)
        file_index += 1
        raw_data = str(attachment.get("data_base64") or attachment.get("data") or "")
        if host_path and not raw_data:
            name = Path(host_path).name or _safe_attachment_filename(
                str(attachment.get("name") or ""),
                file_index,
                preserve_extension=True,
            )
            stored = _with_host_path(
                {
                    "type": "file",
                    "name": name,
                    "mime": str(attachment.get("mime") or "application/octet-stream"),
                    "size": int(attachment.get("size") or 0),
                },
                host_path,
            )
            if attachment.get("caption"):
                stored["caption"] = str(attachment.get("caption") or "")[:1000]
            notes.append(_host_path_file_note(name, host_path, stored.get("size")))
            out.append(stored)
            continue
        try:
            payload = _decode_binary_attachment(
                raw_data,
                limit=_ATTACHMENT_FILE_MAX_BYTES,
            )
        except ValueError:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        expected_digest = str(attachment.get("sha256") or "").lower()
        if expected_digest and expected_digest != digest:
            continue
        name = _safe_attachment_filename(
            str(attachment.get("name") or ""),
            file_index,
            preserve_extension=True,
        )
        base_name = name
        suffix_index = 1
        while (
            name.lower() in seen_names
            or (
                attachment_dir is not None
                and (attachment_dir / name).exists()
            )
        ):
            suffix_index += 1
            path_name = Path(base_name)
            name = f"{path_name.stem}-{suffix_index}{path_name.suffix}"
        seen_names.add(name.lower())

        stored: dict[str, Any] = {
            "type": "file",
            "name": name,
            "mime": str(attachment.get("mime") or "application/octet-stream"),
            "size": len(payload),
            "sha256": digest,
        }
        if host_path:
            stored = _with_host_path(stored, host_path)
        note = (
            f"- **{name}** ({len(payload):,} bytes)\n"
            f"  SHA-256: `{digest}`"
        )
        if host_path:
            note += (
                f"\n  host path: `{host_path}`\n"
                "  original path — sandbox copy skipped"
            )
        elif attachment_dir is not None:
            target = attachment_dir / name
            try:
                target.write_bytes(payload)
            except Exception:
                note += "\n  ⚠ sandbox write failed; binary content was discarded"
            else:
                relative = f"_attachments/{name}"
                stored["sandbox_path"] = str(target.resolve())
                stored["sandbox_rel"] = relative
                stored["relative_path"] = relative
                note += f"\n  sandbox path: `{relative}`"
        else:
            note += "\n  ⚠ no sandbox available; binary content was discarded"
        if attachment.get("caption"):
            stored["caption"] = str(attachment.get("caption") or "")[:1000]
        out.append(stored)
        notes.append(note)

    addendum = ""
    if notes:
        addendum = (
            "\n\n---\n"
            "Binary files attached to this user turn (inert; do not execute):\n\n"
            + "\n\n".join(notes)
        )
    return out, addendum


def _materialize_voice(
    voice: dict[str, Any] | None,
    sandbox_root: Path | str | None,
) -> dict[str, Any] | None:
    """Spill voice audio into ``_voice/`` under the session sandbox.

    Returns a copy with ``sandbox_rel`` / ``sandbox_path`` and without inline
    ``data_base64`` so session JSON stays small across reload/hydrate cycles.
    """
    if not voice or not isinstance(voice, dict):
        return None
    data = voice.get("data_base64") or voice.get("data") or ""
    if not isinstance(data, str) or not data:
        return dict(voice)
    if sandbox_root is None:
        return dict(voice)
    root = Path(sandbox_root)
    voice_dir = root / "_voice"
    try:
        voice_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return dict(voice)
    mime = (voice.get("mime") or "audio/webm").lower()
    ext = "webm"
    if "ogg" in mime:
        ext = "ogg"
    elif "wav" in mime:
        ext = "wav"
    elif "mp4" in mime or "m4a" in mime:
        ext = "m4a"
    elif "mpeg" in mime or "mp3" in mime:
        ext = "mp3"
    name = f"voice-{int(time.time() * 1000)}.{ext}"
    target = voice_dir / name
    try:
        import base64

        target.write_bytes(base64.b64decode(data))
    except Exception:
        return dict(voice)
    out = {k: v for k, v in voice.items() if k not in ("data_base64", "data")}
    out["sandbox_path"] = str(target.resolve())
    out["sandbox_rel"] = f"_voice/{name}"
    return out


_VIDEO_MIME_EXT = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "video/x-matroska": "mkv",
    "video/x-msvideo": "avi",
    "video/x-m4v": "m4v",
}


def _materialize_video(
    attachments: list[dict[str, Any]],
    sandbox_root: Path | str | None,
) -> list[dict[str, Any]]:
    """Spill video attachments into ``_video/`` under the session sandbox.

    Returns a copy of the list where every successfully written video carries
    ``sandbox_rel`` / ``sandbox_path`` instead of inline ``data_base64``. Same
    reasoning as :func:`_materialize_voice`, only more so: a 32 MiB clip is
    ~43 MiB of base64, which would be re-read and re-serialised on every
    session hydrate and re-sent on every subsequent turn.

    Callers keep the un-spilled list for the current turn so the provider (or
    the keyframe fallback) still has the bytes it needs right now.
    """
    if not attachments:
        return []
    if sandbox_root is None:
        return [dict(att) for att in attachments]

    out: list[dict[str, Any]] = []
    video_dir = Path(sandbox_root) / "_video"
    made_dir = False
    for idx, att in enumerate(attachments):
        if (att.get("type") or "").lower() != "video":
            out.append(dict(att))
            continue
        data = att.get("data_base64") or att.get("data") or ""
        host_path = _attachment_host_path(att)
        if host_path and (not isinstance(data, str) or not data):
            out.append(_with_host_path(dict(att), host_path))
            continue
        if not isinstance(data, str) or not data:
            out.append(dict(att))
            continue
        if host_path:
            spilled = {k: v for k, v in att.items() if k not in ("data_base64", "data")}
            try:
                import base64

                spilled["size"] = len(base64.b64decode(data))
            except Exception:
                pass
            out.append(_with_host_path(spilled, host_path))
            continue
        if not made_dir:
            try:
                video_dir.mkdir(parents=True, exist_ok=True)
                made_dir = True
            except Exception:
                return [dict(a) for a in attachments]
        mime = (att.get("mime") or "video/mp4").lower()
        ext = _VIDEO_MIME_EXT.get(mime, "mp4")
        stem = Path(_safe_attachment_filename(str(att.get("name") or ""), idx)).stem or "video"
        target = video_dir / f"{stem}-{int(time.time() * 1000)}.{ext}"
        try:
            import base64

            payload = base64.b64decode(data)
            target.write_bytes(payload)
        except Exception:
            out.append(dict(att))
            continue
        spilled = {k: v for k, v in att.items() if k not in ("data_base64", "data")}
        spilled["size"] = len(payload)
        spilled["sandbox_path"] = str(target.resolve())
        spilled["sandbox_rel"] = f"_video/{target.name}"
        out.append(spilled)
    return out


def _materialize_acp_media_attachments(
    attachments: list[dict[str, Any]],
    sandbox_root: Path | str | None,
) -> list[dict[str, Any]]:
    """Persist ACP image/audio inputs without changing their prompt payload.

    Images and audio still retain inline bytes in the returned history record
    so existing previews and explicit image references keep working. The
    sandbox copy gives local Agents a stable resource URI and ensures channel
    uploads are never silently lost when a content capability is unavailable.
    """
    if not attachments or sandbox_root is None:
        return [dict(item) for item in attachments]

    root = Path(sandbox_root)
    folder_for = {"image": "_images", "audio": "_audio"}
    default_ext = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
        "audio/mp4": ".m4a",
        "audio/m4a": ".m4a",
        "audio/x-m4a": ".m4a",
    }
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, 1):
        item = dict(attachment)
        kind = str(item.get("type") or "").lower()
        folder_name = folder_for.get(kind)
        data = item.get("data_base64") or item.get("data") or ""
        host_path = _attachment_host_path(item)
        if host_path:
            item = _with_host_path(item, host_path)
            if isinstance(data, str) and data:
                try:
                    payload = _decode_binary_attachment(data, limit=_ATTACHMENT_MAX_BYTES)
                except ValueError:
                    payload = b""
                if payload:
                    item["size"] = len(payload)
                    item["sha256"] = hashlib.sha256(payload).hexdigest()
            out.append(item)
            continue
        if not folder_name or not isinstance(data, str) or not data:
            out.append(item)
            continue
        try:
            payload = _decode_binary_attachment(data, limit=_ATTACHMENT_MAX_BYTES)
        except ValueError:
            out.append(item)
            continue
        folder = root / folder_name
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            out.append(item)
            continue
        mime = str(item.get("mime") or "").lower()
        requested_name = str(item.get("name") or f"{kind}-{index}")
        if not Path(requested_name).suffix:
            requested_name += default_ext.get(mime, ".bin")
        name = _safe_attachment_filename(
            requested_name,
            index,
            preserve_extension=True,
        )
        base = Path(name)
        candidate = base.name
        suffix_index = 1
        while candidate.lower() in seen or (folder / candidate).exists():
            suffix_index += 1
            candidate = f"{base.stem}-{suffix_index}{base.suffix}"
        seen.add(candidate.lower())
        target = folder / candidate
        try:
            target.write_bytes(payload)
        except Exception:
            out.append(item)
            continue
        relative = f"{folder_name}/{candidate}"
        item.update({
            "name": candidate,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "sandbox_path": str(target.resolve()),
            "sandbox_rel": relative,
            "relative_path": relative,
        })
        out.append(item)
    return out


def _sanitize_voice(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a primary voice message payload (mic recording, not a file attachment)."""
    if not isinstance(item, dict):
        return None
    mime = (item.get("mime") or "").lower()
    if mime == "audio/x-wav":
        mime = "audio/wav"
    if mime not in _ATTACHMENT_AUDIO_MIME and not mime.startswith("audio/"):
        return None
    data = item.get("data_base64") or item.get("data") or ""
    if not isinstance(data, str) or not data:
        return None
    if len(data) > int(_ATTACHMENT_MAX_BYTES * 1.4):
        return None
    out: dict[str, Any] = {
        "type": "audio",
        "mime": mime or "audio/webm",
        "data_base64": data,
    }
    duration_ms = item.get("duration_ms")
    if duration_ms is not None:
        try:
            out["duration_ms"] = int(duration_ms)
        except (TypeError, ValueError):
            pass
    return out


def _sanitize_attachments(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    out: list[dict[str, Any]] = []
    for it in items[:_ATTACHMENT_MAX_COUNT]:
        if not isinstance(it, dict):
            continue
        kind = (it.get("type") or "image").lower()
        raw_id = str(it.get("id") or "").strip()[:128]
        identity = (
            {"id": raw_id}
            if raw_id and all(char not in raw_id for char in "\x00\r\n")
            else {}
        )
        if kind == "image":
            mime = (it.get("mime") or "").lower()
            if mime == "image/jpg":
                mime = "image/jpeg"
            if mime not in _ATTACHMENT_IMAGE_MIME:
                continue
            data = it.get("data_base64") or it.get("data") or ""
            host_path = _attachment_host_path(it)
            if not isinstance(data, str) or not data:
                if not host_path:
                    continue
                out.append(_with_host_path({
                    **identity,
                    "type": "image",
                    "mime": mime or "image/png",
                    "name": str(it.get("name") or "")[:120],
                    "size": int(it.get("size") or 0),
                }, host_path))
                continue
            # Rough size guard (base64 expands ~33%)
            if len(data) > int(_ATTACHMENT_MAX_BYTES * 1.4):
                continue
            item = {
                **identity,
                "type": "image",
                "mime": mime,
                "data_base64": data,
                "name": str(it.get("name") or "")[:120],
            }
            if host_path:
                item = _with_host_path(item, host_path)
            out.append(item)
        elif kind == "video":
            mime = (it.get("mime") or "").lower()
            if mime not in _ATTACHMENT_VIDEO_MIME:
                continue
            data = it.get("data_base64") or it.get("data") or ""
            if not isinstance(data, str) or not data:
                continue
            if len(data) > int(_ATTACHMENT_VIDEO_MAX_BYTES * 1.4):
                continue
            item: dict[str, Any] = {
                **identity,
                "type": "video",
                "mime": mime,
                "data_base64": data,
                "name": str(it.get("name") or "")[:120],
            }
            duration_ms = it.get("duration_ms")
            if duration_ms is not None:
                try:
                    item["duration_ms"] = int(duration_ms)
                except (TypeError, ValueError):
                    pass
            out.append(item)
        elif kind == "audio":
            mime = (it.get("mime") or "").lower()
            if mime == "audio/x-wav":
                mime = "audio/wav"
            if mime not in _ATTACHMENT_AUDIO_MIME and not mime.startswith("audio/"):
                continue
            data = it.get("data_base64") or it.get("data") or ""
            if not isinstance(data, str) or not data:
                continue
            if len(data) > int(_ATTACHMENT_MAX_BYTES * 1.4):
                continue
            out.append({
                **identity,
                "type": "audio",
                "mime": mime or "audio/wav",
                "data_base64": data,
                "name": str(it.get("name") or "")[:120],
            })
        elif kind == "text":
            text = it.get("text") or ""
            host_path = _attachment_host_path(it)
            name = str(it.get("name") or "")[:120]
            mime = str(it.get("mime") or "").lower()
            if host_path and not text:
                out.append(_with_host_path({
                    **identity,
                    "type": "text",
                    "mime": mime or "text/plain",
                    "name": name or Path(host_path).name,
                    "size": int(it.get("size") or 0),
                }, host_path))
                continue
            if not isinstance(text, str) or not text:
                continue
            # Size cap on the raw payload, before any sandbox spill — text
            # this big is almost certainly the wrong thing to staple onto a
            # chat turn even if the user could in theory afford it.
            byte_size = len(text.encode("utf-8", errors="ignore"))
            if byte_size > _ATTACHMENT_TEXT_MAX_BYTES:
                continue
            # Either the mime OR the filename must vouch for it being a
            # safe text format. We don't trust the renderer's mime alone
            # (drag-drop often hands us an empty string) and we don't
            # trust the extension alone (browsers send arbitrary mimes).
            if not (_attachment_text_mime_ok(mime) or _attachment_text_ext_ok(name)):
                continue
            stored = {
                **identity,
                "type": "text",
                "mime": mime or "text/plain",
                "name": name,
                "text": text,
                "size": byte_size,
            }
            if host_path:
                stored = _with_host_path(stored, host_path)
            out.append(stored)
        elif kind == "file":
            data = it.get("data_base64") or it.get("data") or ""
            host_path = _attachment_host_path(it)
            if host_path and not data:
                stored = _with_host_path({
                    **identity,
                    "type": "file",
                    "mime": str(it.get("mime") or "application/octet-stream").lower(),
                    "name": str(it.get("name") or Path(host_path).name)[:120],
                    "size": int(it.get("size") or 0),
                }, host_path)
                if it.get("caption"):
                    stored["caption"] = str(it.get("caption") or "")[:1000]
                out.append(stored)
                continue
            if not isinstance(data, str) or not data:
                continue
            try:
                payload = _decode_binary_attachment(
                    data,
                    limit=_ATTACHMENT_FILE_MAX_BYTES,
                )
            except ValueError:
                continue
            digest = hashlib.sha256(payload).hexdigest()
            expected_digest = str(it.get("sha256") or "").lower()
            if expected_digest and expected_digest != digest:
                continue
            item = {
                **identity,
                "type": "file",
                "mime": str(it.get("mime") or "application/octet-stream").lower(),
                "name": str(it.get("name") or "")[:120],
                "data_base64": base64.b64encode(payload).decode("ascii"),
                "size": len(payload),
                "sha256": digest,
            }
            if host_path:
                item = _with_host_path(item, host_path)
            if it.get("caption"):
                item["caption"] = str(it.get("caption") or "")[:1000]
            out.append(item)
        else:
            # Anything else (error chips from the frontend, unknown
            # types) just gets discarded — by the time we're here the
            # frontend already told the user about it.
            continue
    return out
