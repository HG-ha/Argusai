"""Keyframe extraction for video attachments, backed by a local ffmpeg.

Most chat models that accept stills reject video outright (gpt-4o, the whole
Claude family). When the user attaches a clip to one of those endpoints we
degrade it into a handful of evenly spaced stills and push those through the
existing image path — native vision when the model has it, the auxiliary
vision endpoint otherwise.

ffmpeg is deliberately NOT a packaged dependency: it is detected on PATH (or
via ``settings.toolchain.paths['ffmpeg']``) and everything degrades to a plain
text note when it is missing. Nothing here ever runs through a shell.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("argusai.video_frames")

__all__ = [
    "FRAME_MAX_COUNT",
    "extract_keyframes",
    "ffmpeg_available",
    "resolve_ffmpeg",
    "resolve_ffprobe",
]

FRAME_MAX_COUNT = 6
# Frames are re-encoded down to this width before base64; a 1080p still costs
# far more context than it adds for the sort of UI/repro clips users attach.
_FRAME_MAX_WIDTH = 768
_PROBE_TIMEOUT = 20.0
_EXTRACT_TIMEOUT = 90.0

_MIME_EXT = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/x-m4v": ".m4v",
}


def _toolchain_override(name: str) -> str:
    try:
        from argusai.core.reverse.toolchain import get_path_overrides

        return str(get_path_overrides().get(name) or "").strip()
    except Exception:
        return ""


def _resolve(name: str) -> str | None:
    override = _toolchain_override(name)
    if override and Path(override).exists():
        return override
    return shutil.which(name)


def resolve_ffmpeg() -> str | None:
    return _resolve("ffmpeg")


def resolve_ffprobe() -> str | None:
    """ffprobe, falling back to a sibling of the configured ffmpeg.

    Settings only expose an ``ffmpeg`` path field; the two binaries ship in the
    same directory, so pointing at one is enough to find the other even when
    neither is on PATH.
    """
    found = _resolve("ffprobe")
    if found:
        return found
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return None
    binary = Path(ffmpeg)
    sibling = binary.with_name(binary.name.replace("ffmpeg", "ffprobe", 1))
    return str(sibling) if sibling != binary and sibling.exists() else None


def ffmpeg_available() -> bool:
    return bool(resolve_ffmpeg())


def _quiet_kwargs() -> dict[str, Any]:
    try:
        from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

        return quiet_subprocess_kwargs()
    except Exception:
        return {}


async def _run(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_quiet_kwargs(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        await asyncio.gather(proc.wait(), return_exceptions=True)
        raise
    return int(proc.returncode or 0), stdout or b"", stderr or b""


async def _probe_duration(source: Path) -> float:
    ffprobe = resolve_ffprobe()
    if not ffprobe:
        return 0.0
    try:
        code, stdout, _ = await _run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            timeout=_PROBE_TIMEOUT,
        )
    except Exception:
        return 0.0
    if code != 0:
        return 0.0
    try:
        return max(0.0, float(stdout.decode("utf-8", "ignore").strip()))
    except (TypeError, ValueError):
        return 0.0


def _format_offset(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


async def extract_keyframes(
    *,
    path: str = "",
    data_base64: str = "",
    mime: str = "video/mp4",
    count: int = 4,
) -> dict[str, Any]:
    """Sample up to ``count`` evenly spaced stills from a clip.

    Returns ``{"ok": True, "frames": [{"data_base64", "mime", "label"}],
    "duration_s": float}``. On failure returns ``{"ok": False, "reason": ...,
    "error": ...}`` where ``reason`` is ``no_ffmpeg`` / ``no_source`` /
    ``timeout`` / ``failed`` so callers can word the fallback note precisely.
    """
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        return {"ok": False, "reason": "no_ffmpeg", "error": "ffmpeg not found"}

    count = max(1, min(int(count or 1), FRAME_MAX_COUNT))
    workdir = Path(tempfile.mkdtemp(prefix="argusai-frames-"))
    source = Path(path) if path else None
    try:
        if not (source and source.is_file()):
            if not data_base64:
                return {"ok": False, "reason": "no_source", "error": "no video payload"}
            source = workdir / f"input{_MIME_EXT.get((mime or '').lower(), '.mp4')}"
            try:
                source.write_bytes(base64.b64decode(data_base64))
            except Exception as exc:
                return {"ok": False, "reason": "no_source", "error": str(exc)}

        duration = await _probe_duration(source)
        if duration > 0:
            select = f"fps={count}/{duration:.3f}"
        else:
            # No ffprobe: `thumbnail` picks the most representative frame out
            # of each batch, which beats grabbing the first N frames.
            select = "thumbnail=100"

        args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-an", "-sn", "-dn",
            "-vf", f"{select},scale='min({_FRAME_MAX_WIDTH},iw)':-2",
            "-frames:v", str(count),
            "-q:v", "4",
            str(workdir / "frame-%03d.jpg"),
        ]
        try:
            code, _, stderr = await _run(args, timeout=_EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            return {"ok": False, "reason": "timeout", "error": "ffmpeg timed out"}
        except Exception as exc:
            return {"ok": False, "reason": "failed", "error": str(exc)}

        files = sorted(workdir.glob("frame-*.jpg"))
        if not files:
            detail = stderr.decode("utf-8", "ignore").strip()[:300]
            log.info("keyframe extraction produced nothing (rc=%s): %s", code, detail)
            return {"ok": False, "reason": "failed", "error": detail or f"ffmpeg exit {code}"}

        step = (duration / len(files)) if duration > 0 else 0.0
        frames: list[dict[str, str]] = []
        for i, item in enumerate(files):
            try:
                payload = item.read_bytes()
            except Exception:
                continue
            frames.append({
                "data_base64": base64.b64encode(payload).decode("ascii"),
                "mime": "image/jpeg",
                "label": _format_offset(i * step) if step else f"#{i + 1}",
            })
        if not frames:
            return {"ok": False, "reason": "failed", "error": "no readable frames"}
        return {"ok": True, "frames": frames, "duration_s": duration}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
