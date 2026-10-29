"""Per-assistant-session file sandbox and controlled script execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import (
    background_subprocess_kwargs,
    devnull_stdin_fd,
    ensure_valid_stdio,
    quiet_subprocess_kwargs,
)
from argusai.core.managed_runtimes import (
    managed_runtime_required,
    python_executable as managed_python_executable,
    python_venv_health as managed_python_venv_health,
    runtime_status as managed_runtime_status,
    tools_dir_from_sandbox,
)

log = logging.getLogger("argusai.session_sandbox")


ALLOWED_TEXT_EXTS = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".html", ".htm", ".css", ".js",
    ".py", ".ps1", ".bat", ".cmd", ".sh",
}
IMAGE_PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
PYTHON_EXTS = {".py"}
SHELL_EXTS = {".ps1", ".bat", ".cmd", ".sh"}
MAX_FILE_CHARS = 500_000
MAX_IMAGE_PREVIEW_BYTES = 5 * 1024 * 1024
# Above this size, ``read_file`` streams a window instead of loading the whole file.
_LARGE_TEXT_READ_BYTES = 256 * 1024
_MAX_UI_READ_CHARS = 200_000
OUTPUT_LIMIT = 20_000
PROMPT_STDOUT_LIMIT = 6_144
PROMPT_STDERR_LIMIT = 4_096
AUDIT_FILE = "audit.jsonl"
TMP_DIR = ".tmp"
PY_VENV_DIR = ".venv"

BLOCKED_PATH_PARTS = {"", ".", ".."}
BLOCKED_ARGUMENT_TOKENS = {"|", ">", "<", ">>", "&&", "||", ";", "&", "`", "$("}
DANGEROUS_SHELL_PATTERNS = [
    r"\brm\s+-rf\s+[/~]",
    r"\bdel\s+/[sq]\s+%?(userprofile|home|systemroot)%?",
    r"\brmdir\s+/[sq]\s+%?(userprofile|home|systemroot)%?",
    r"\bformat\s+[a-z]:",
    r"\bshutdown\b",
    r"\breg\s+(add|delete)\b.*\\(run|runonce|services)\\",
    r"\bset-executionpolicy\b",
    r"\bdownloadstring\s*\(",
    r"\binvoke-expression\b|\biex\b",
    r"\bcurl\b.+\|\s*(sh|bash|powershell|pwsh)",
    r"\bwget\b.+\|\s*(sh|bash|powershell|pwsh)",
    r"\bstop-service\b.+(defender|windefend|security)",
]

# Patterns that indicate an attempt to invoke a host interpreter from sandbox
# shell/snippet tools. Host execution must go through run_host_python_* instead.
_HOST_INTERPRETER_ESCAPE_PATTERNS: list[tuple[str, str]] = [
    (r"[a-z]:\\.*\\python\d*\.exe", "absolute Windows python.exe path"),
    (r"[a-z]:\\.*\\py\.exe", "absolute Windows py launcher path"),
    (r"\bpy\s+-[\d.]+", "Windows py launcher version selector"),
    (r"/(?:usr/(?:local/)?bin|opt/[^/\s]+/)python[\d.]*", "absolute Unix python path"),
    (
        r"(?:program\s+files|%localappdata%|\$env:localappdata)[^\\s\"'\r\n]*python",
        "host Python install location reference",
    ),
    (r"(?:anaconda|miniconda)[^\\s\"'\r\n]*[/\\]python", "conda python path"),
]

_HOST_ESCALATION_MIN_CHARS = 12

# Host escalation must not become a shortcut for packaging or pip installs.
_HOST_ESCALATION_DISALLOWED_PATTERNS: list[tuple[str, str]] = [
    (
        r"\bpyinstaller\b",
        "PyInstaller must run in the sandbox .venv (sandbox_pip_install + run_python_script). "
        "Do not build exes on host/Conda Python — stdlib bundling breaks (e.g. missing urllib). "
        "If sandbox packaging fails, give the user a manual `python -m venv` command instead.",
    ),
    (
        r"(?:^|\s)(?:pip3?|python(?:3)?(?:\.exe)?)\s+-m\s+pip\s+install\b",
        "Install packages with sandbox_pip_install or run_python_snippet using sys.executable -m pip in the sandbox .venv.",
    ),
    (
        r"(?:^|\s)pip3?\s+install\b|'pip'\s*,\s*'install'|\"pip\"\s*,\s*\"install\"",
        "Install packages with sandbox_pip_install or run_python_snippet using sys.executable -m pip in the sandbox .venv.",
    ),
]


@dataclass
class Sandbox:
    sessions_dir: Path
    session_id: str

    @property
    def root(self) -> Path:
        return sandbox_dir_for_session(self.sessions_dir, self.session_id)


def _sandbox_path_for_session(sessions_dir: str | Path, session_id: str) -> tuple[str, Path, Path]:
    """Resolve a sandbox path without creating the sessions or sandbox directory."""
    safe = validate_session_id(session_id)
    base = Path(sessions_dir).expanduser().resolve()
    root = (base / f"{safe}.sandbox").resolve()
    if root.parent != base:
        raise ValueError("invalid sandbox path")
    return safe, base, root


def sandbox_dir_for_session(sessions_dir: str | Path, session_id: str) -> Path:
    _safe, base, root = _sandbox_path_for_session(sessions_dir, session_id)
    base.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return root


def remove_session_storage(sessions_dir: str | Path, session_id: str) -> dict[str, bool]:
    """Delete one assistant session's persisted JSON and storage directories."""
    safe = validate_session_id(session_id)
    base = Path(sessions_dir).expanduser().resolve()
    removed = {"json": False, "sandbox": False, "runs": False, "agents": False}
    json_path = (base / f"{safe}.json").resolve()
    if json_path.parent == base and json_path.is_file():
        json_path.unlink()
        removed["json"] = True
    sandbox_path = (base / f"{safe}.sandbox").resolve()
    if sandbox_path.parent == base and sandbox_path.exists():
        shutil.rmtree(sandbox_path, ignore_errors=True)
        removed["sandbox"] = not sandbox_path.exists()
    runs_path = (base / f"{safe}.runs").resolve()
    if runs_path.parent == base and runs_path.exists():
        shutil.rmtree(runs_path, ignore_errors=True)
        removed["runs"] = not runs_path.exists()
    agents_path = (base / "_agents" / safe).resolve()
    agents_root = (base / "_agents").resolve()
    if agents_path.parent == agents_root and agents_path.exists():
        shutil.rmtree(agents_path, ignore_errors=True)
        removed["agents"] = not agents_path.exists()
    return removed


def validate_session_id(session_id: str) -> str:
    sid = (session_id or "default").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not sid or len(sid) > 128 or sid in {".", ".."} or any(ch not in allowed for ch in sid):
        raise ValueError("invalid session_id")
    return sid


def _audit_path(root: Path) -> Path:
    return root / AUDIT_FILE


def audit_event(root: Path, event: str, **data: Any) -> dict[str, Any]:
    item = {"ts": time.time(), "event": event, **data}
    path = _audit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    return item


def validate_relative_path(path: str, allowed_exts: set[str] | None = None) -> Path:
    raw = _clean_tool_path(path).replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    p = Path(raw)
    if p.is_absolute() or re.match(r"^[a-zA-Z]:", raw) or raw.startswith("//"):
        raise ValueError("absolute paths are not allowed")
    parts = p.parts
    if any(part in BLOCKED_PATH_PARTS for part in parts):
        raise ValueError("path traversal is not allowed")
    if any(str(part).startswith(".") for part in parts if part != TMP_DIR):
        raise ValueError("hidden paths are not allowed")
    ext = p.suffix.lower()
    if allowed_exts is not None and ext not in allowed_exts:
        raise ValueError(f"extension not allowed: {ext or '(none)'}")
    return p


def _clean_tool_path(path: str) -> str:
    r"""Normalize path strings copied out of tool cards.

    Tool results show both `path` and `relative_path`. Models sometimes copy
    those values with surrounding quotes/backticks from the UI, which turns an
    otherwise valid sandbox path into `"C:\...\script.py"` or `` `script.py` ``.
    Strip only wrapping presentation quotes; real path validation still happens
    in `validate_relative_path` / `resolve_in_sandbox`.
    """
    raw = str(path or "").strip()
    changed = True
    while changed and len(raw) >= 2:
        changed = False
        pairs = {('"', '"'), ("'", "'"), ("`", "`")}
        for left, right in pairs:
            if raw.startswith(left) and raw.endswith(right):
                raw = raw[1:-1].strip()
                changed = True
                break
    return raw


def resolve_in_sandbox(root: Path, path: str, allowed_exts: set[str] | None = None) -> Path:
    raw = _clean_tool_path(path)
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlparse

        raw = unquote(urlparse(raw).path).lstrip("/") if platform.system().lower().startswith("win") else unquote(urlparse(raw).path)

    root_resolved = root.resolve()
    normalized = raw.replace("\\", "/")
    is_absolute = Path(normalized).is_absolute() or bool(re.match(r"^[a-zA-Z]:", normalized)) or normalized.startswith("//")
    if is_absolute:
        resolved = Path(normalized).resolve()
        if root_resolved not in resolved.parents and resolved != root_resolved:
            raise ValueError("path escapes sandbox")
        rel = resolved.relative_to(root_resolved)
        validate_relative_path(rel.as_posix(), allowed_exts=allowed_exts)
        return resolved

    rel = validate_relative_path(raw, allowed_exts=allowed_exts)
    resolved = (root_resolved / rel).resolve()
    if root_resolved not in resolved.parents and resolved != root_resolved:
        raise ValueError("path escapes sandbox")
    return resolved


def write_file(root: Path, path: str, content: str, file_type: str = "") -> dict[str, Any]:
    from argusai.core.infra import text_io

    try:
        target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    result = text_io.write_text_file_resolved(target, content)
    if not result.get("ok"):
        err = str(result.get("error") or "write failed")
        raise ValueError(err)
    rel = target.relative_to(root).as_posix()
    result = {
        **result,
        "relative_path": rel,
        "directory": str(root),
        "file_type": file_type or target.suffix.lower().lstrip("."),
        "next_path_hint": (
            f"Use relative_path={rel!r} for run_python_script/run_shell_script "
            "rather than copying the absolute path."
        ),
    }
    audit_event(root, "write_file", **{k: v for k, v in result.items() if k != "ok"})
    return result


def _line_number_for_offset(text: str, offset: int) -> int:
    if offset <= 0:
        return 1
    return text.count("\n", 0, min(offset, len(text))) + 1


def _format_numbered_content(content: str, start_line: int) -> str:
    if not content:
        return ""
    lines = content.splitlines()
    width = max(3, len(str(start_line + len(lines) - 1)))
    return "\n".join(f"{start_line + idx:{width}d}|{line}" for idx, line in enumerate(lines))


def patch_file(
    root: Path,
    path: str,
    old_string: str,
    new_string: str = "",
    *,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Apply an exact substring replacement inside the sandbox."""
    from argusai.core.infra import text_io

    try:
        target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    result = text_io.patch_text_file_resolved(
        target,
        old_string,
        new_string,
        replace_all=replace_all,
    )
    if not result.get("ok"):
        if result.get("error_code") == "patch_old_string_not_found":
            result["suggested_fix"] = (
                "Re-read with sandbox_read_file and copy the exact substring from content_raw, "
                "or from content when read_mode='chars'. Then retry sandbox_patch_file."
            )
        return result
    rel = target.relative_to(root).as_posix()
    result = {
        **result,
        "relative_path": rel,
        "hint": (
            "Applied exact substring replacement. Prefer sandbox_patch_file for small edits; "
            "use sandbox_write_file only for new files or full rewrites."
        ),
    }
    audit_event(root, "patch_file", **{k: v for k, v in result.items() if k != "ok"})
    return result


def _content_raw_from_numbered(content: str) -> str:
    from argusai.core.infra.text_io import content_raw_from_numbered

    return content_raw_from_numbered(content)


def stat_file(root: Path, path: str) -> dict[str, Any]:
    """Cheap metadata probe for a sandbox text file before reading/grepping."""
    target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS | {".jsonl"})
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    size = target.stat().st_size
    line_count: int | None = None
    if size <= 20_000_000:
        try:
            with target.open("rb") as fh:
                line_count = sum(1 for _ in fh)
        except Exception:
            line_count = None
    sample_bytes = b""
    try:
        with target.open("rb") as fh:
            sample_bytes = fh.read(512)
    except Exception:
        pass
    from argusai.core.infra.text_io import _detect_encoding

    enc, bom = _detect_encoding(sample_bytes, None)
    rel = target.relative_to(root).as_posix()
    large = size > _LARGE_TEXT_READ_BYTES
    return {
        "ok": True,
        "path": str(target),
        "relative_path": rel,
        "size_bytes": size,
        "line_count": line_count,
        "encoding_hint": enc,
        "bom": bom,
        "binary_hint": b"\x00" in sample_bytes,
        "large_file": large,
        "hint": (
            "Call sandbox_grep_file to locate interesting lines, then sandbox_read_file(offset=<line>, limit=...) "
            "for a small window. Use read_mode='chars' with offset_chars only for minified single-line files."
            if large
            else "Use sandbox_read_file(offset, limit) for line windows; sandbox_patch_file(content_raw) for edits."
        ),
    }


def _read_text_window(path: Path, offset_chars: int, max_chars: int) -> dict[str, Any]:
    """Read a UTF-8 text window without loading multi-MB files entirely when possible."""
    offset_chars = max(0, int(offset_chars or 0))
    max_chars = max(1, min(int(max_chars or 20_000), _MAX_UI_READ_CHARS))
    size_bytes = path.stat().st_size

    if size_bytes <= _LARGE_TEXT_READ_BYTES:
        text = path.read_text(encoding="utf-8", errors="replace")
        total_chars = len(text)
        start = min(offset_chars, total_chars)
        end = min(total_chars, start + max_chars)
        return {
            "content": text[start:end],
            "offset_chars": start,
            "end_offset": end,
            "total_chars": total_chars,
            "truncated": end < total_chars,
        }

    # Large / minified files: byte-offset window (good enough for sandbox JS bundles).
    byte_start = offset_chars
    read_bytes = min(size_bytes - byte_start, max(max_chars * 4, max_chars) + 32)
    if read_bytes <= 0:
        return {
            "content": "",
            "offset_chars": offset_chars,
            "end_offset": offset_chars,
            "total_chars": size_bytes,
            "truncated": False,
        }
    with path.open("rb") as fh:
        fh.seek(byte_start)
        raw = fh.read(read_bytes)
    chunk = raw.decode("utf-8", errors="replace")
    if len(chunk) > max_chars:
        chunk = chunk[:max_chars]
    end_offset = offset_chars + len(chunk)
    truncated = byte_start + len(raw) < size_bytes and len(chunk) >= max_chars
    return {
        "content": chunk,
        "offset_chars": offset_chars,
        "end_offset": end_offset,
        "total_chars": size_bytes,
        "truncated": truncated,
    }


def read_file(
    root: Path,
    path: str,
    max_chars: int = 20_000,
    as_base64: bool = False,
    offset_chars: int = 0,
    line_numbers: bool = False,
) -> dict[str, Any]:
    allowed_exts = IMAGE_PREVIEW_EXTS if as_base64 else ALLOWED_TEXT_EXTS | {".jsonl"}
    target = resolve_in_sandbox(root, path, allowed_exts=allowed_exts)
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}

    if as_base64:
        import base64
        import mimetypes

        size = target.stat().st_size
        if size > MAX_IMAGE_PREVIEW_BYTES:
            return {
                "ok": False,
                "error": f"image too large for inline preview; max {MAX_IMAGE_PREVIEW_BYTES} bytes",
                "path": str(target),
                "size": size,
            }
        try:
            data = target.read_bytes()
            mime_type, _ = mimetypes.guess_type(target.name)
            if not mime_type or not mime_type.startswith("image/"):
                return {"ok": False, "error": "file is not an image preview type", "path": str(target)}

            b64_data = base64.b64encode(data).decode("ascii")
            data_uri = f"data:{mime_type};base64,{b64_data}"
            return {
                "ok": True,
                "path": str(target),
                "relative_path": target.relative_to(root).as_posix(),
                "content": data_uri,
                "is_base64": True,
                "mime_type": mime_type,
                "size": size,
            }
        except Exception as e:
            return {"ok": False, "error": f"failed to read as base64: {e}", "path": str(target)}

    window = _read_text_window(target, offset_chars, max_chars)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(target),
        "relative_path": target.relative_to(root).as_posix(),
        "content": window["content"],
        "truncated": bool(window["truncated"]),
        "more": bool(window["truncated"]),
        "offset_chars": window["offset_chars"],
        "end_offset": window["end_offset"],
        "total_chars": window["total_chars"],
        "size": target.stat().st_size,
    }
    if line_numbers and window["content"]:
        size_bytes = target.stat().st_size
        if size_bytes <= _LARGE_TEXT_READ_BYTES:
            full_text = target.read_text(encoding="utf-8", errors="replace")
            start_line = _line_number_for_offset(full_text, int(window["offset_chars"]))
            end_line = start_line + window["content"].count("\n")
            result["start_line"] = start_line
            result["end_line"] = end_line
            result["content_numbered"] = _format_numbered_content(window["content"], start_line)
            result["hint"] = (
                "Use content (exact text) or line numbers for navigation. "
                "For edits, call sandbox_patch_file with old_string copied verbatim from content."
            )
        else:
            result["line_numbers_skipped"] = True
            result["hint"] = (
                "Line numbers omitted for large files. Use offset_chars/end_offset to page, "
                "or sandbox_patch_file with a unique old_string copied from content."
            )
    elif line_numbers:
        result["start_line"] = _line_number_for_offset("", 0)
        result["end_line"] = result["start_line"]
    return result


def list_directory(root: Path, path: str = "") -> dict[str, Any]:
    """List immediate children for the sandbox file browser."""
    root_resolved = root.resolve()
    if str(path or "").strip():
        target = resolve_in_sandbox(root_resolved, path)
    else:
        target = root_resolved
    if not target.exists() or not target.is_dir():
        return {"ok": False, "error": "directory not found", "path": str(target)}

    entries: list[dict[str, Any]] = []
    total_size = 0
    file_count = 0
    dir_count = 0
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            rel = child.relative_to(root_resolved).as_posix()
        except ValueError:
            continue
        if rel == PY_VENV_DIR or rel.startswith(f"{PY_VENV_DIR}/"):
            continue
        if any(part.startswith(".") and part != TMP_DIR for part in child.relative_to(root_resolved).parts):
            continue
        stat = child.stat()
        item: dict[str, Any] = {
            "path": str(child),
            "relative_path": rel,
            "name": child.name,
            "mtime": stat.st_mtime,
        }
        if child.is_dir():
            dir_count += 1
            item["kind"] = "dir"
            item["previewable"] = False
        elif child.is_file():
            file_count += 1
            total_size += stat.st_size
            item["kind"] = "file"
            item["previewable"] = child.suffix.lower() in (ALLOWED_TEXT_EXTS | {".jsonl"} | IMAGE_PREVIEW_EXTS)
            item["size"] = stat.st_size
        else:
            continue
        entries.append(item)

    rel_dir = "" if target == root_resolved else target.relative_to(root_resolved).as_posix()
    return {
        "ok": True,
        "directory": str(target),
        "relative_path": rel_dir,
        "items": entries,
        "file_count": file_count,
        "dir_count": dir_count,
        "total_size": total_size,
    }


# Extensions safe to hand off to the OS shell for the user to view. We
# deliberately exclude executables, scripts, archives, and anything that
# could surprise the user if their default app behaves unexpectedly.
OPENABLE_EXTS: set[str] = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".html", ".htm", ".css", ".js", ".jsonl", ".log", ".xml",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    ".pdf",
}


def open_file(root: Path, path: str) -> dict[str, Any]:
    """Hand a sandbox file to the user's OS shell so their default app opens it.

    Scope is strictly the per-session sandbox — the path is validated against
    ``root`` first, so the AI cannot point this at e.g. ``C:\\Windows\\...``.
    Extensions are limited to the ``OPENABLE_EXTS`` allowlist; we never
    "open" an executable / script / archive on the user's behalf because the
    OS shell would then *run* it, not just preview it.
    """

    target = resolve_in_sandbox(root, path, allowed_exts=OPENABLE_EXTS)
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    system = platform.system().lower()
    try:
        if system.startswith("win"):
            # os.startfile is the Windows-native equivalent of "double-click";
            # it hands off to the user's registered default handler for the
            # file extension and returns immediately without blocking.
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif system == "darwin":
            subprocess.Popen(["open", str(target)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(target)], close_fds=True)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"OS shell open failed: {exc}",
            "path": str(target),
            "platform": system,
        }
    rel = target.relative_to(root).as_posix()
    audit_event(root, "open_file", relative_path=rel, platform=system)
    return {
        "ok": True,
        "path": str(target),
        "relative_path": rel,
        "platform": system,
        "size": target.stat().st_size,
        "hint": "The file was handed to the OS default handler. The application window opens out-of-process and is not awaited.",
    }


def reveal_in_folder(root: Path, path: str = "") -> dict[str, Any]:
    """Open the sandbox folder (or the given file's folder) in the OS file manager.

    Pass an empty ``path`` to open the sandbox root itself. Delegates the
    platform launch to :func:`argusai.core.infra.explorer.reveal_path` so
    Windows does not hide Explorer behind ``quiet_subprocess_kwargs``.
    """
    from argusai.core.infra.explorer import reveal_path

    if not path or not str(path).strip():
        target = root.resolve()
    else:
        target = resolve_in_sandbox(root, path)
        if not target.exists():
            return {"ok": False, "error": "path not found", "path": str(target)}
    result = reveal_path(target)
    if result.get("ok"):
        result.setdefault("platform", platform.system().lower())
    else:
        result["error"] = f"OS shell reveal failed: {result.get('error') or 'unknown'}"
        result.setdefault("platform", platform.system().lower())
    return result


def list_files(root: Path) -> dict[str, Any]:
    files = []
    directories: set[str] = set()
    total = 0
    for p in sorted(root.rglob("*")):
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        # The Python venv can contain thousands of dependency files. Keep it
        # out of normal sandbox listings while preserving it for execution.
        if rel == PY_VENV_DIR or rel.startswith(f"{PY_VENV_DIR}/"):
            continue
        if p.is_dir():
            directories.add(rel)
            continue
        if not p.is_file():
            continue
        size = p.stat().st_size
        total += size
        files.append({"path": str(p), "relative_path": rel, "size": size, "mtime": p.stat().st_mtime})
    return {
        "ok": True,
        "directory": str(root),
        "files": files,
        "directories": sorted(directories),
        "root_items": list_directory(root).get("items", []),
        "file_count": len(files),
        "dir_count": len(directories),
        "total_size": total,
    }


def get_session_sandbox_summary(
    sessions_dir: str | Path,
    session_id: str,
) -> dict[str, Any]:
    """Return a read-only recursive summary for one session sandbox."""
    safe, _base, root = _sandbox_path_for_session(sessions_dir, session_id)
    result: dict[str, Any] = {
        "ok": True,
        "session_id": safe,
        "exists": root.is_dir(),
        "directory": str(root),
        "file_count": 0,
        "dir_count": 0,
        "total_size": 0,
        "latest_mtime": 0.0,
        "root_items": [],
    }
    if not result["exists"]:
        return result

    listing = list_files(root)
    files = listing.get("files", [])
    latest_mtime = max(
        (float(item.get("mtime") or 0.0) for item in files if isinstance(item, dict)),
        default=0.0,
    )
    result.update(
        file_count=int(listing.get("file_count") or 0),
        dir_count=int(listing.get("dir_count") or 0),
        total_size=int(listing.get("total_size") or 0),
        latest_mtime=latest_mtime,
        root_items=listing.get("root_items", []),
    )
    return result


def delete_file(root: Path, path: str) -> dict[str, Any]:
    target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS | {".jsonl"})
    if target.name == AUDIT_FILE:
        raise ValueError("audit log cannot be deleted")
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    size = target.stat().st_size
    target.unlink()
    audit_event(root, "delete_file", path=str(target), relative_path=target.relative_to(root).as_posix(), size=size)
    return {"ok": True, "path": str(target), "relative_path": target.relative_to(root).as_posix(), "deleted": True}


def reset_sandbox(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    removed = 0
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        if p == _audit_path(root):
            continue
        if p.is_file():
            p.unlink()
            removed += 1
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    audit_event(root, "reset_sandbox", removed_files=removed)
    return {"ok": True, "directory": str(root), "removed_files": removed}


def recent_audit(root: Path, limit: int = 20) -> list[dict[str, Any]]:
    path = _audit_path(root)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, int(limit or 20)):]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def sandbox_status(root: Path) -> dict[str, Any]:
    listing = list_files(root)
    vpy = _venv_python(root)
    python_env = _probe_sandbox_python_env(root)
    return {
        "ok": True,
        "directory": str(root),
        "file_count": listing.get("file_count", 0),
        "total_size": listing.get("total_size", 0),
        "python_venv": {
            "exists": vpy.exists(),
            "venv_dir": str(root / PY_VENV_DIR),
            "python": str(vpy) if vpy.exists() else "",
            **python_env,
        },
        "recent_audit": recent_audit(root, 10),
    }


def _venv_site_packages_dir(root: Path) -> Path | None:
    venv_root = root / PY_VENV_DIR
    if platform.system().lower().startswith("win"):
        candidate = venv_root / "Lib" / "site-packages"
    else:
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        candidate = venv_root / "lib" / pyver / "site-packages"
    return candidate if candidate.is_dir() else None


def _probe_sandbox_python_env(root: Path) -> dict[str, Any]:
    """Read-only probe of sandbox Python/pip readiness (does not create a venv)."""
    vpy = _venv_python(root)
    managed = _vendored_python(root)
    runtime_status = _managed_python_status(root)
    base_cmd = [managed] if managed else []
    runtime_health = (
        _vendored_venv_health(base_cmd)
        if runtime_status.get("healthy")
        else {"ok": False, "error": runtime_status.get("error") or "managed Python is unavailable"}
    )
    out: dict[str, Any] = {
        "pip_available": False,
        "pip_version": "",
        "installed_package_count": 0,
        "managed_runtime": managed or "",
        "managed_runtime_ok": bool(runtime_status.get("healthy")),
        # Backward-compatible fields for old session cards.
        "bundled_runtime": managed or "",
        "bundled_runtime_ok": bool(runtime_health.get("ok")),
        "runtime_health": runtime_health if not runtime_health.get("ok") else {"ok": True},
    }
    if vpy.exists():
        pip_probe = _run_python_module(vpy, root, "pip", ["--version"], timeout=60)
        out["pip_available"] = bool(pip_probe.get("ok"))
        pip_lines = str(pip_probe.get("output") or "").strip().splitlines()
        out["pip_version"] = pip_lines[0] if pip_lines else ""
        site = _venv_site_packages_dir(root)
        if site is not None:
            try:
                out["installed_package_count"] = sum(
                    1 for p in site.iterdir() if p.name.endswith(".dist-info")
                )
            except Exception:
                out["installed_package_count"] = 0
        if out["pip_available"]:
            out["hint"] = (
                "Sandbox .venv is ready. Install packages with sandbox_pip_install or "
                "run_python_snippet using [sys.executable, '-m', 'pip', 'install', pkg]."
            )
        else:
            out["hint"] = (
                "Sandbox .venv exists but pip is missing. Delete .venv or sandbox_reset_files, "
                "then repair the managed Python sandbox environment and retry."
            )
    elif not runtime_health.get("ok"):
        out["hint"] = (
            "The managed Python sandbox environment is missing or damaged. "
            "Install/repair it from the confirmation card or Settings."
        )
    else:
        out["hint"] = (
            "No .venv yet — the first Python sandbox tool creates it from the shared managed runtime."
        )
    return out


async def sandbox_pip_install(
    root: Path,
    packages: str | list[str] | None = None,
    *,
    upgrade: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Install Python packages into the session sandbox .venv via pip."""
    pkg_list = _parse_pip_packages(packages)
    if not pkg_list:
        return {
            "ok": False,
            "error": "packages is required (space/comma-separated names or PEP508 specs)",
            "error_code": "pip_packages_empty",
            "retryable": True,
        }
    venv = ensure_python_venv(root)
    if not venv.get("ok"):
        probe = _probe_sandbox_python_env(root)
        return {
            "ok": False,
            "error": venv.get("error") or "sandbox venv is not available",
            "error_code": venv.get("error_code") or "sandbox_venv_unavailable",
            "runtime": venv.get("runtime"),
            "blocked": bool(venv.get("blocked")),
            "retryable": True,
            "venv": {k: v for k, v in venv.items() if k != "ok"},
            "python_env": probe,
            "hint": probe.get("hint") or venv.get("hint"),
        }
    py = str(venv["python"])
    argv = [py, "-m", "pip", "install", *pkg_list]
    if upgrade:
        argv.append("--upgrade")
    result = await _run_exec(
        argv,
        root,
        _timeout(timeout_seconds),
        root,
        "sandbox_pip_install",
        {
            "packages": pkg_list,
            "python": py,
            "venv_dir": venv.get("venv_dir", ""),
            "venv_created": bool(venv.get("created")),
            "upgrade": bool(upgrade),
        },
        env=_venv_env(root),
    )
    result["execution_tier"] = "sandbox"
    result["packages"] = pkg_list
    if result.get("ok"):
        probe = _probe_sandbox_python_env(root)
        result["pip_version"] = probe.get("pip_version", "")
        result["installed_package_count"] = probe.get("installed_package_count", 0)
        result["hint"] = (
            f"Installed into sandbox .venv. Run scripts with run_python_script / run_python_snippet "
            f"(sys.executable → {py})."
        )
    else:
        result["hint"] = (
            "Fix pip/venv first (session_sandbox_status), then retry. Do not escalate to host Python for pip install."
        )
    return result


def _normalize_pip_package_token(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    # Strip JSON-ish wrapping the model sometimes emits as one token.
    for _ in range(3):
        stripped = text.strip().strip('"').strip("'")
        if stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1].strip().strip('"').strip("'")
        if stripped == text:
            break
        text = stripped
    return text.strip().strip('"').strip("'")


def _parse_pip_packages(packages: Any) -> list[str]:
    if packages is None:
        return []
    if isinstance(packages, list):
        return [t for t in (_normalize_pip_package_token(p) for p in packages) if t]
    text = str(packages or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        import json

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [t for t in (_normalize_pip_package_token(p) for p in parsed) if t]
        except Exception:
            inner = text[1:-1].strip()
            if inner:
                parts = re.split(r"\s*,\s*", inner)
                return [t for t in (_normalize_pip_package_token(p) for p in parts) if t]
    parts = re.split(r"[\s,;]+", text)
    return [t for t in (_normalize_pip_package_token(p) for p in parts) if t]


def _run_version(command: list[str], timeout: float = 5.0) -> str:
    try:
        import subprocess

        # Run without text= so we get raw bytes; on Windows the system codepage
        # (e.g. GBK on zh-CN) breaks for tools that emit UTF-16 BOM or UTF-8.
        r = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=timeout,
            **quiet_subprocess_kwargs(),
        )
        raw = (r.stdout or b"") + (r.stderr or b"")
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            text = raw.decode("utf-16", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        line = text.strip().splitlines()
        return (line[0] if line else "")[:300]
    except Exception as e:
        return f"unavailable: {e}"


def _is_wsl_bash_stub(path: str | Path) -> bool:
    """True for the Windows Store / System32 WSL ``bash.exe`` launchers.

    Those stubs are often first on PATH but cannot run local ``.sh`` scripts the
    way Git Bash can; preferring them is why agents report "bash unavailable"
    or hang after a Chinese WSL install prompt.
    """
    p = str(path or "").replace("/", "\\").lower()
    return p.endswith("\\system32\\bash.exe") or "\\windowsapps\\bash.exe" in p


def _git_bash_candidate_paths() -> list[Path]:
    paths: list[Path] = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(key, "").strip()
        if not base:
            continue
        root = Path(base)
        if key == "LOCALAPPDATA":
            root = root / "Programs"
        paths.extend(
            [
                root / "Git" / "bin" / "bash.exe",
                root / "Git" / "usr" / "bin" / "bash.exe",
            ]
        )
    home = os.environ.get("USERPROFILE", "").strip()
    if home:
        paths.append(Path(home) / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe")
    return paths


def find_bash_executable() -> str | None:
    """Resolve a usable bash binary, preferring Git Bash over WSL stubs on Windows."""
    if platform.system().lower().startswith("win"):
        for candidate in _git_bash_candidate_paths():
            if candidate.is_file():
                return str(candidate)
        for name in ("bash", "sh"):
            found = shutil.which(name)
            if found and not _is_wsl_bash_stub(found):
                return found
        return None
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def discover_shell_environments() -> dict[str, Any]:
    candidates: list[tuple[str, list[str]]] = []
    if platform.system().lower().startswith("win"):
        candidates.extend([
            ("powershell", ["powershell", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]),
            ("pwsh", ["pwsh", "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"]),
            ("cmd", ["cmd", "/c", "ver"]),
            ("wsl", ["wsl", "--version"]),
        ])
    else:
        candidates.extend([
            ("bash", ["bash", "--version"]),
            ("sh", ["sh", "--version"]),
            ("zsh", ["zsh", "--version"]),
        ])
    shells = []
    for name, version_cmd in candidates:
        exe = shutil.which(version_cmd[0])
        shells.append({
            "name": name,
            "path": exe,
            "available": bool(exe),
            "version": _run_version(version_cmd) if exe else "",
        })
    if platform.system().lower().startswith("win"):
        bash_exe = find_bash_executable()
        shells.append({
            "name": "git-bash" if bash_exe and "\\git\\" in bash_exe.replace("/", "\\").lower() else "bash",
            "path": bash_exe,
            "available": bool(bash_exe),
            "version": _run_version([bash_exe, "--version"]) if bash_exe else "",
        })
    return {"ok": True, "platform": platform.system(), "shells": shells}


_SHELL_CAP_NOTE_CACHE: dict[str, str] = {}


def clear_shell_capability_note_cache() -> None:
    _SHELL_CAP_NOTE_CACHE.clear()


def _build_shell_capability_note(default_shell: str = "auto") -> str:
    from argusai.core.settings import normalize_default_shell

    pref = normalize_default_shell(default_shell)
    info = discover_shell_environments()
    shells = [s for s in info.get("shells", []) if s.get("available")]
    if not shells:
        return ""

    def _clean_ver(s: dict[str, Any]) -> str:
        ver = (s.get("version") or "").strip()
        return "" if (not ver or ver.startswith("unavailable")) else ver

    is_win = str(info.get("platform", "")).lower().startswith("win")
    ps_ver = ""
    has_pwsh = False
    has_bash = False
    # Only PowerShell's version matters for the &&/|| story; cmd/bash/wsl
    # version strings come back localized / UTF-16 and just add garbled noise,
    # so the "Available" list shows names only.
    available = ", ".join(f"`{s.get('name')}`" for s in shells if s.get("name"))
    for s in shells:
        if s.get("name") == "powershell":
            ps_ver = ".".join(_clean_ver(s).split(".")[:2])  # 5.1.x.y -> 5.1
        if s.get("name") == "pwsh":
            has_pwsh = True
        if s.get("name") in {"bash", "sh", "git-bash"}:
            has_bash = True

    if pref == "auto":
        effective = "powershell" if is_win else "bash"
        if is_win and has_pwsh:
            effective = "pwsh"
    else:
        effective = pref

    platform_name = str(info.get("platform") or ("Windows" if is_win else "POSIX"))
    parts = [
        f"- Host platform: **{platform_name}**. Sandbox `run_shell_snippet` / `run_shell_script` "
        f"run on **this ArgusAI machine**, not on a remote server the user mentioned.\n",
        f"- Settings default shell: `{pref}` (effective when `shell` is omitted: `{effective}`). "
        f"Available: {available}.\n",
    ]
    if is_win:
        if effective == "powershell" and not has_pwsh:
            parts.append(
                f"- Windows PowerShell {ps_ver or '5.1'} does NOT support `&&` / `||`. "
                "Chain with `;`, or pass `shell=\"cmd\"` / install `pwsh`. "
                "Snippets using `&&`/`||` under PowerShell auto-route to `pwsh`/`cmd`.\n"
            )
        elif effective == "pwsh":
            parts.append(
                "- PowerShell 7 (`pwsh`) is the effective shell and supports `&&` / `||` chaining.\n"
            )
        bash_state = (
            "available (Git Bash preferred over WSL stubs)"
            if has_bash
            else "NOT installed — refuse to auto-route bash scripts to cmd/PowerShell"
        )
        parts.append(
            "- Bash syntax (`#!/bin/bash`, heredoc `<<EOF`, Linux idioms) requires "
            f'`shell="bash"` and a bash binary ({bash_state}). '
            "Do not wrap bash/heredoc inside PowerShell quoting.\n"
        )
        parts.append(
            "- Remote Linux from Windows: prefer `shell_session_start` with "
            "`ssh -T user@host`, then `shell_session_exec` for each remote command. "
            "Or `sandbox_write_file` a `.sh` + `scp` + `ssh -n -o BatchMode=yes user@host "
            "'bash ~/script.sh'`. Always use `ssh -n` / `</dev/null` so stdin cannot hang. "
            "Never pipe multi-line scripts through PowerShell into ssh.\n"
        )
    return "".join(parts)


def shell_capability_note(default_shell: str = "auto") -> str:
    """Cached summary of the host shells for the system prompt.

    Tells the agent the configured default shell, platform, and bash rules so it
    does not discover Windows PowerShell 5.1 / cmd mismatches by trial-and-error.
    """
    from argusai.core.settings import normalize_default_shell

    key = normalize_default_shell(default_shell)
    cached = _SHELL_CAP_NOTE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        note = _build_shell_capability_note(key)
    except Exception:
        note = ""
    _SHELL_CAP_NOTE_CACHE[key] = note
    return note


def discover_python_environments() -> dict[str, Any]:
    commands = []
    vendored = _vendored_python()
    if vendored:
        commands.append(("bundled", [vendored, "--version"]))
    if platform.system().lower().startswith("win"):
        commands.extend([
            ("current", [sys.executable, "--version"]),
            ("python", ["python", "--version"]),
            ("python3", ["python3", "--version"]),
        ])
        commands.append(("py-3", ["py", "-3", "--version"]))
    else:
        commands.extend([
            ("current", [sys.executable, "--version"]),
            ("python3", ["python3", "--version"]),
            ("python", ["python", "--version"]),
        ])
    seen = set()
    pythons = []
    for name, cmd in commands:
        exe = cmd[0] if name == "current" else shutil.which(cmd[0])
        if not exe or exe in seen:
            continue
        seen.add(exe)
        pythons.append({
            "name": name,
            "path": exe,
            "available": True,
            "version": _run_version([exe, *cmd[1:]]) if name != "py-3" else _run_version(cmd),
            "is_current": exe == sys.executable,
            "is_venv": str(exe).lower().startswith(str(sys.prefix).lower()),
        })
    return {"ok": True, "pythons": pythons, "current": sys.executable}


def _looks_like_python_executable(path: str) -> bool:
    name = Path(path).name.lower()
    return name.startswith("python") or name.startswith("pypy")


def _base_python_command(python_env: str = "", root: Path | None = None) -> list[str]:
    """Resolve only ArgusAI's managed Python for normal sandbox execution."""
    if python_env and python_env != "bundled":
        return []
    managed = _vendored_python(root)
    return [managed] if managed else []


def _python_subprocess_env(root: Path | None = None) -> dict[str, str]:
    """Return a clean environment for spawning an external Python runtime."""
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env.setdefault("PYTHONUTF8", "1")
    _prepend_bundled_runtime_path(env, root)
    return env


def _prepend_bundled_runtime_path(env: dict[str, str], root: Path | None = None) -> None:
    """Ensure managed CPython can load DLLs when cwd is the session sandbox.

    Anaconda-derived runtimes often keep support DLLs under ``DLLs/`` and
    ``Library/bin/``. Windows resolves dependent DLLs from the process cwd
    before PATH; when cwd is an empty per-session sandbox directory, bare
    ``python.exe`` fails with exit ``3221225781`` (``0xC0000135`` /
    ``STATUS_DLL_NOT_FOUND``). Prepend the managed runtime directories to
    PATH for every sandbox subprocess.
    """
    if not platform.system().lower().startswith("win"):
        return
    bundled = _vendored_python(root)
    if not bundled:
        return
    runtime_dir = Path(bundled).resolve().parent
    prefixes: list[str] = []
    for candidate in (runtime_dir / "DLLs", runtime_dir / "Library" / "bin", runtime_dir):
        if candidate.is_dir():
            prefixes.append(str(candidate))
    if not prefixes:
        return
    existing = env.get("PATH", "")
    parts = prefixes + ([existing] if existing else [])
    env["PATH"] = os.pathsep.join(parts)


def _windows_process_exit_hint(exit_code: int) -> str:
    if int(exit_code or 0) == 3221225781:
        return (
            "Windows STATUS_DLL_NOT_FOUND (0xC0000135): the managed Python runtime "
            "could not load a required DLL for this working directory. "
            "Repair Python under Settings → Sandbox runtimes, then restart and "
            "retry sandbox_pip_install."
        )
    return ""


def _ensure_python_sitecustomize(root: Path) -> Path:
    """Install a tiny startup patch for sandboxed Python scripts.

    We run assistant scripts with UTF-8 mode so our own stdout/stderr capture is
    deterministic. On Windows, tools such as nslookup may still emit bytes in
    the console/OEM codepage. If model-generated scripts call
    ``subprocess.run(..., text=True)`` without ``errors=...``, Python's reader
    thread can raise ``UnicodeDecodeError`` before the script can handle it.
    This sitecustomize keeps text-mode subprocess calls lossy-but-alive by
    defaulting missing ``errors`` to ``replace``.
    """
    site_dir = root / ".python-sitecustomize"
    site_dir.mkdir(parents=True, exist_ok=True)
    patch = site_dir / "sitecustomize.py"
    patch.write_text(
        """\
from __future__ import annotations

import subprocess as _subprocess

_orig_popen_init = _subprocess.Popen.__init__


def _argusai_popen_init(self, *args, **kwargs):
    text_mode = kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding") is not None
    if text_mode and kwargs.get("errors") is None:
        kwargs["errors"] = "replace"
    return _orig_popen_init(self, *args, **kwargs)


if getattr(_subprocess.Popen.__init__, "__name__", "") != "_argusai_popen_init":
    _subprocess.Popen.__init__ = _argusai_popen_init
""",
        encoding="utf-8",
        newline="\n",
    )
    return site_dir


def _with_python_sitecustomize(root: Path, env: dict[str, str]) -> dict[str, str]:
    site_dir = _ensure_python_sitecustomize(root)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(site_dir) + (os.pathsep + existing if existing else "")
    return env


def _decode_process_output(stdout: bytes | None, stderr: bytes | None) -> str:
    raw = (stdout or b"") + (stderr or b"")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _platform_subdir() -> str:
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    if sys_name.startswith("win"):
        os_label = "win"
    elif sys_name == "darwin":
        os_label = "macos"
    else:
        os_label = "linux"
    if machine in {"x86_64", "amd64"}:
        arch_label = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch_label = "arm64"
    elif machine.startswith("i"):
        arch_label = "x86"
    else:
        arch_label = machine
    return f"{os_label}-{arch_label}"


def _managed_tools_dir(root: Path | None = None) -> Path:
    if root is not None:
        try:
            return tools_dir_from_sandbox(root)
        except ValueError:
            pass
    from argusai.core.paths import PathManager

    return PathManager().tools_dir


def _managed_python_status(root: Path | None = None) -> dict[str, Any]:
    return managed_runtime_status("python", _managed_tools_dir(root))


def _vendored_python(root: Path | None = None) -> str:
    """Backward-compatible name for the shared managed Python executable."""
    status = _managed_python_status(root)
    if not status.get("healthy"):
        return ""
    candidate = managed_python_executable(_managed_tools_dir(root))
    return str(candidate) if candidate.is_file() else ""


def _vendored_python_runtime_dir(base_cmd: list[str]) -> Path | None:
    if not base_cmd:
        return None
    try:
        exe = Path(base_cmd[0]).resolve()
    except Exception:
        return None
    if not exe.exists():
        return None
    if exe.parent.name.lower() == "scripts" and exe.parent.parent.name == PY_VENV_DIR:
        return None
    return exe.parent


def _vendored_venv_health(base_cmd: list[str]) -> dict[str, Any]:
    """Check files Windows venv needs before spawning a noisy subprocess."""
    runtime_dir = _vendored_python_runtime_dir(base_cmd)
    if runtime_dir is None:
        return {"ok": True}
    result = managed_python_venv_health(runtime_dir)
    if not result.get("ok"):
        result["hint"] = (
            "The managed Python runtime is missing files required by venv. "
            "Repair it from Settings → Sandbox runtimes."
        )
    return result


def _venv_python(root: Path) -> Path:
    if platform.system().lower().startswith("win"):
        return root / PY_VENV_DIR / "Scripts" / "python.exe"
    return root / PY_VENV_DIR / "bin" / "python"


def _venv_bin_dir(root: Path) -> Path:
    if platform.system().lower().startswith("win"):
        return root / PY_VENV_DIR / "Scripts"
    return root / PY_VENV_DIR / "bin"


def _venv_pip(root: Path) -> Path:
    if platform.system().lower().startswith("win"):
        return root / PY_VENV_DIR / "Scripts" / "pip.exe"
    return root / PY_VENV_DIR / "bin" / "pip"


def _run_python_module(
    python: Path,
    root: Path,
    module: str,
    args: list[str] | None = None,
    *,
    timeout: int = 180,
) -> dict[str, Any]:
    argv = [str(python), "-m", module, *(args or [])]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(root),
            env=_python_subprocess_env(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=timeout,
            **quiet_subprocess_kwargs(),
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "argv": argv,
            "output": _decode_process_output(proc.stdout, proc.stderr)[:4000],
        }
    except Exception as exc:
        return {
            "ok": False,
            "argv": argv,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _write_venv_pip_shims(root: Path) -> list[str]:
    """Create lightweight `pip` command shims when ensurepip only installs the module.

    Some embedded/bundled runtimes can successfully run `python -m pip` but do
    not materialise `pip.exe` into Scripts/.  The assistant often generates
    shell snippets that call `pip` directly, so PATH-visible shims are still
    required.
    """

    vpy = _venv_python(root)
    bin_dir = _venv_bin_dir(root)
    bin_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    command = subprocess.list2cmdline([str(vpy)])
    if platform.system().lower().startswith("win"):
        for name in ("pip.cmd", "pip3.cmd"):
            path = bin_dir / name
            path.write_text(f"@echo off\r\n{command} -m pip %*\r\n", encoding="utf-8", newline="")
            written.append(str(path))
    else:
        for name in ("pip", "pip3"):
            path = bin_dir / name
            path.write_text(f"#!/bin/sh\nexec {command} -m pip \"$@\"\n", encoding="utf-8", newline="\n")
            try:
                path.chmod(0o755)
            except OSError:
                pass
            written.append(str(path))
    return written


def _ensure_venv_pip(root: Path, *, upgrade: bool = False) -> dict[str, Any]:
    """Ensure the sandbox venv has a working pip command.

    Older sessions may contain venvs created by the previous fallback path
    (`python -m venv --without-pip`).  Reusing those venvs without checking
    pip made Python tooling look healthy while `pip install` was impossible.
    This function repairs those venvs in-place with ensurepip, then verifies
    `python -m pip --version`.
    """

    vpy = _venv_python(root)
    pip_exe = _venv_pip(root)
    if not vpy.exists():
        return {"ok": False, "error": "venv python is missing", "python": str(vpy)}

    before = _run_python_module(vpy, root, "pip", ["--version"], timeout=60)
    if before.get("ok") and pip_exe.exists():
        result: dict[str, Any] = {
            "ok": True,
            "installed": False,
            "pip": str(pip_exe),
            "pip_command_available": True,
            "version": before.get("output", "").strip(),
        }
        if upgrade:
            # Best-effort only.  The important invariant is that pip exists;
            # failure to upgrade pip/setuptools/wheel should not make the
            # venv unusable.
            result["upgrade"] = _run_python_module(
                vpy,
                root,
                "pip",
                ["install", "--upgrade", "pip", "setuptools", "wheel"],
                timeout=240,
            )
        return result

    ensure_args = ["--upgrade", "--default-pip"]
    ensure = _run_python_module(vpy, root, "ensurepip", ensure_args, timeout=180)
    after = _run_python_module(vpy, root, "pip", ["--version"], timeout=60)
    shims: list[str] = []
    if after.get("ok") and not pip_exe.exists():
        shims = _write_venv_pip_shims(root)
    result = {
        "ok": bool(after.get("ok")),
        "installed": bool(after.get("ok")),
        "pip": str(pip_exe),
        "pip_command_available": pip_exe.exists() or bool(shims),
        "before": before,
        "ensurepip": ensure,
        "version": after.get("output", "").strip() if after.get("ok") else "",
    }
    if shims:
        result["shims"] = shims
    if after.get("ok") and upgrade:
        result["upgrade"] = _run_python_module(
            vpy,
            root,
            "pip",
            ["install", "--upgrade", "pip", "setuptools", "wheel"],
            timeout=240,
        )
    elif not after.get("ok"):
        result["after"] = after
    return result


def ensure_python_venv(root: Path, python_env: str = "") -> dict[str, Any]:
    """Create/reuse the per-sandbox Python venv used by Python execution tools."""
    if python_env and python_env != "bundled":
        return {
            "ok": False,
            "error": "python_env cannot select a host interpreter for sandbox execution",
            "error_code": "sandbox_host_escape_blocked",
            "retryable": False,
        }
    vpy = _venv_python(root)
    if vpy.exists():
        pip_result = _ensure_venv_pip(root)
        return {
            "ok": bool(pip_result.get("ok")),
            "created": False,
            "python": str(vpy),
            "venv_dir": str(root / PY_VENV_DIR),
            "pip_available": bool(pip_result.get("ok")),
            "pip_bootstrap": pip_result,
            **({} if pip_result.get("ok") else {
                "error": "sandbox venv exists but pip could not be bootstrapped",
                "hint": (
                    "Delete the session .venv directory or run sandbox_reset_files, then retry. "
                    "If repair still fails, repair the managed Python sandbox environment in Settings."
                ),
            }),
        }
    tools_dir = _managed_tools_dir(root)
    runtime = managed_runtime_status("python", tools_dir)
    if not runtime.get("healthy"):
        return {
            **managed_runtime_required("python", tools_dir),
            "venv_dir": str(root / PY_VENV_DIR),
        }
    base_cmd = _base_python_command(python_env, root)
    if not base_cmd:
        return {
            **managed_runtime_required("python", tools_dir),
            "venv_dir": str(root / PY_VENV_DIR),
        }
    health = _vendored_venv_health(base_cmd)
    if not health.get("ok"):
        return {
            "ok": False,
            "error": "managed Python runtime is incomplete for venv creation",
            "error_code": "managed_runtime_required",
            "runtime": "python",
            "blocked": True,
            "argv": [*base_cmd, "-m", "venv", str(root / PY_VENV_DIR)],
            "venv_dir": str(root / PY_VENV_DIR),
            "runtime_health": health,
        }
    root.mkdir(parents=True, exist_ok=True)
    venv_dir = root / PY_VENV_DIR
    cmd = [*base_cmd, "-m", "venv", str(venv_dir)]
    env = _python_subprocess_env(root)
    try:
        r = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=120,
            **quiet_subprocess_kwargs(),
        )
    except Exception as e:
        return {
            "ok": False,
            "error": f"create sandbox venv failed: {type(e).__name__}: {e}",
            "argv": cmd,
            "venv_dir": str(root / PY_VENV_DIR),
        }
    if r.returncode != 0 or not vpy.exists():
        output = _decode_process_output(r.stdout, r.stderr)
        shutil.rmtree(venv_dir, ignore_errors=True)
        hint = _windows_process_exit_hint(int(r.returncode or 0))
        return {
            "ok": False,
            "error": f"managed Python could not create the sandbox venv (exit {r.returncode})",
            "error_code": "managed_runtime_required",
            "runtime": "python",
            "blocked": True,
            "argv": cmd,
            "output": output[:4000],
            "venv_dir": str(venv_dir),
            "hint": hint or "Repair the Python sandbox environment from Settings, then retry.",
        }
    pip_result = _ensure_venv_pip(root)
    audit_event(
        root,
        "create_python_venv",
        base_python=base_cmd,
        venv_dir=str(root / PY_VENV_DIR),
        python=str(vpy),
        ensurepip=pip_result,
    )
    return {
        "ok": bool(pip_result.get("ok")),
        "created": True,
        "python": str(vpy),
        "venv_dir": str(root / PY_VENV_DIR),
        "base_python": base_cmd,
        "pip_available": bool(pip_result.get("ok")),
        "pip_bootstrap": pip_result,
        **({} if pip_result.get("ok") else {
            "error": "sandbox venv was created but pip could not be bootstrapped",
            "hint": (
                "The selected Python can create venvs but cannot provide pip via ensurepip. "
                "Use discover_python_environments and retry with a full CPython install, or rebuild "
                "the packaged runtime with Lib/ensurepip included."
            ),
        }),
    }


def _has_blocked_args(parts: list[str]) -> bool:
    return any(token in part for part in parts for token in BLOCKED_ARGUMENT_TOKENS)


def _host_interpreter_escape_reason(text: str) -> str:
    body = str(text or "")
    if not body.strip():
        return ""
    lower = body.lower()
    for pat, label in _HOST_INTERPRETER_ESCAPE_PATTERNS:
        if re.search(pat, lower, re.IGNORECASE):
            return (
                f"blocked host interpreter escape ({label}). "
                "Sandbox shell tools may only use bare python/pip on PATH (the session .venv). "
                "To run on a host interpreter, call run_host_python_snippet or run_host_python_script "
                "with reason and sandbox_attempt_summary after sandbox execution fails."
            )
    return ""


def _reject_sandbox_python_env(python_env: str) -> dict[str, Any] | None:
    env = str(python_env or "").strip()
    if not env:
        return None
    return {
        "ok": False,
        "error": "python_env is not allowed for sandbox execution tools",
        "error_code": "sandbox_host_escape_blocked",
        "retryable": False,
        "suggested_fix": (
            "Omit python_env and run inside the session sandbox .venv only. "
            "If the sandbox truly cannot satisfy the task (e.g. stdlib bound to host Python such as tkinter), "
            "call run_host_python_snippet or run_host_python_script with reason and sandbox_attempt_summary."
        ),
    }


def _validate_host_escalation_disallowed_use(text: str) -> dict[str, Any] | None:
    body = str(text or "")
    if not body.strip():
        return None
    lower = body.lower()
    for pat, message in _HOST_ESCALATION_DISALLOWED_PATTERNS:
        if re.search(pat, lower, re.IGNORECASE):
            return {
                "ok": False,
                "error": message,
                "error_code": "host_escalation_disallowed_use",
                "retryable": True,
                "suggested_fix": (
                    "Call session_sandbox_status, then sandbox_pip_install for dependencies and "
                    "run_python_script for execution inside the sandbox .venv."
                ),
            }
    return None


def _validate_host_escalation_fields(reason: str, sandbox_attempt_summary: str) -> dict[str, Any] | None:
    why = str(reason or "").strip()
    summary = str(sandbox_attempt_summary or "").strip()
    if len(why) < _HOST_ESCALATION_MIN_CHARS:
        return {
            "ok": False,
            "error": (
                f"reason is required ({_HOST_ESCALATION_MIN_CHARS}+ chars): explain why the sandbox .venv "
                "cannot satisfy this task"
            ),
            "error_code": "host_escalation_reason_required",
            "retryable": True,
        }
    if len(summary) < _HOST_ESCALATION_MIN_CHARS:
        return {
            "ok": False,
            "error": (
                f"sandbox_attempt_summary is required ({_HOST_ESCALATION_MIN_CHARS}+ chars): describe what you "
                "already tried in the sandbox and the failure evidence"
            ),
            "error_code": "host_escalation_summary_required",
            "retryable": True,
        }
    return None


def _dangerous_shell_reason(script: str) -> str:
    lower = script.lower()
    for pat in DANGEROUS_SHELL_PATTERNS:
        if re.search(pat, lower, re.IGNORECASE):
            return f"blocked dangerous shell pattern: {pat}"
    return _host_interpreter_escape_reason(script)


def _background_settle_seconds(timeout: int) -> float:
    """Brief wait used only to catch immediate spawn crashes before detach."""
    return min(2.0, max(0.5, float(timeout)))


def _spawn_detached_popen(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    """Spawn a long-lived child with sync Popen (no asyncio transports).

    Codex/Cursor keep background shells in a process manager outside the
    request event loop. We follow the same rule: never leave an
    ``asyncio`` subprocess transport orphaned — that yields
    ``RuntimeError: Event loop is closed`` on app shutdown.
    """
    ensure_valid_stdio()
    log_fh = open(log_path, "wb")  # noqa: SIM115 — closed after child inherits
    try:
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            **background_subprocess_kwargs(),
        )
    finally:
        try:
            log_fh.close()
        except OSError:
            pass


def _reap_popen_later(proc: subprocess.Popen[bytes]) -> None:
    """Reap exited children on a daemon thread (POSIX zombies); never on asyncio."""

    def _wait() -> None:
        try:
            proc.wait()
        except Exception:
            pass

    threading.Thread(target=_wait, name=f"argusai-reap-{proc.pid}", daemon=True).start()


async def _run_exec_background(
    argv: list[str],
    cwd: Path,
    timeout: int,
    audit_root: Path,
    event: str,
    meta: dict[str, Any],
    env: dict[str, str] | None,
    *,
    started: float,
    output_id: str,
    output_dir: Path,
) -> dict[str, Any]:
    bg_log_path = output_dir / f"{output_id}.background.log"
    try:
        proc = await asyncio.to_thread(_spawn_detached_popen, argv, cwd, env, bg_log_path)
    except FileNotFoundError as exc:
        log.warning(
            "sandbox.run_exec_spawn_failed event=%s argv0=%s cwd=%s error=%s",
            event,
            argv[0] if argv else "",
            cwd,
            exc,
        )
        raise

    settle = _background_settle_seconds(timeout)
    deadline = time.monotonic() + settle
    exit_code: int | None = None
    while True:
        exit_code = await asyncio.to_thread(proc.poll)
        if exit_code is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Still running → success. Hand off reaping; do not keep asyncio handles.
            from argusai.core.workspace.shell_sessions import (
                assistant_session_id_from_sandbox,
                get_shell_session_manager,
            )

            _reap_popen_later(proc)
            sess = get_shell_session_manager().register_oneshot(
                sandbox_root=audit_root,
                assistant_session_id=assistant_session_id_from_sandbox(audit_root),
                argv=argv,
                proc=proc,
                log_path=bg_log_path,
                label=str(meta.get("relative_path") or meta.get("path") or event or ""),
                shell=str(meta.get("shell") or ""),
                meta=meta,
            )
            replaced = list(getattr(sess, "_replaced_sessions", []) or [])
            rel_log = bg_log_path.relative_to(audit_root).as_posix()
            replace_note = ""
            if replaced:
                ids = ", ".join(str(item.get("session_id") or "") for item in replaced[:5])
                replace_note = (
                    f" Replaced {len(replaced)} prior oneshot(s) with the same instance_key "
                    f"({sess.instance_key}): {ids}."
                )
            result = {
                **meta,
                "ok": True,
                "argv": argv,
                "cwd": str(cwd),
                "exit_code": None,
                "pid": proc.pid,
                "duration_ms": int((time.time() - started) * 1000),
                "stdout_chars": 0,
                "stderr_chars": 0,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "truncated": False,
                "timed_out": False,
                "timeout_seconds": timeout,
                "background": True,
                "detached": True,
                "background_log": rel_log,
                "session_id": sess.session_id,
                "kind": "oneshot",
                "instance_key": sess.instance_key,
                "replaced_sessions": replaced,
                "hint": (
                    f"Process is still running in the background (pid={proc.pid}, "
                    f"session_id={sess.session_id}). "
                    "This is SUCCESS for GUI apps / servers — do not treat it as a timeout "
                    "and do not re-launch unless the window/process is actually missing. "
                    "Use shell_session_read/list/stop (oneshot cannot exec). "
                    "Check background_log or use desktop_observe to verify."
                    + replace_note
                ),
            }
            audit_event(audit_root, event, **{k: v for k, v in result.items() if k != "ok"})
            log.info(
                "sandbox.run_exec_detached event=%s argv0=%s pid=%s session_id=%s duration_ms=%d",
                event,
                argv[0] if argv else "",
                proc.pid,
                sess.session_id,
                result["duration_ms"],
            )
            return result
        await asyncio.sleep(min(0.1, remaining))

    out = ""
    if bg_log_path.exists():
        try:
            out = bg_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out = ""
    result = {
        "ok": exit_code == 0,
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "pid": proc.pid,
        "duration_ms": int((time.time() - started) * 1000),
        "stdout_chars": len(out),
        "stderr_chars": 0,
        "stdout_excerpt": out[:OUTPUT_LIMIT],
        "stderr_excerpt": "",
        "truncated": len(out) > OUTPUT_LIMIT,
        "timed_out": False,
        "timeout_seconds": timeout,
        "background": True,
        "detached": False,
        "background_log": bg_log_path.relative_to(audit_root).as_posix(),
        **meta,
    }
    if exit_code != 0:
        result["hint"] = (
            "Background launch exited during the short settle window — the process did not "
            "stay up. Inspect stdout_excerpt / background_log before retrying. "
            "For long-lived GUI/servers use background=true (returns session_id) or shell_session_start."
        )
        log.warning(
            "sandbox.run_exec_background_early_exit event=%s argv0=%s exit_code=%s duration_ms=%d",
            event,
            argv[0] if argv else "",
            exit_code,
            result["duration_ms"],
        )
    audit_event(audit_root, event, **{k: v for k, v in result.items() if k != "ok"})
    return result


async def _run_exec(
    argv: list[str],
    cwd: Path,
    timeout: int,
    audit_root: Path,
    event: str,
    meta: dict[str, Any],
    env: dict[str, str] | None = None,
    *,
    background: bool = False,
) -> dict[str, Any]:
    started = time.time()
    ensure_valid_stdio()
    argv_preview = " ".join(argv[:6])
    if len(argv) > 6:
        argv_preview += f" …(+{len(argv) - 6} args)"
    log.info(
        "sandbox.run_exec start event=%s argv0=%s argc=%d cwd=%s timeout=%d background=%s preview=%s",
        event,
        argv[0] if argv else "",
        len(argv),
        cwd,
        timeout,
        background,
        argv_preview[:200],
    )
    output_id = f"run-output-{int(started)}-{uuid.uuid4().hex[:8]}"
    output_dir = audit_root / TMP_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if background:
        return await _run_exec_background(
            argv,
            cwd,
            timeout,
            audit_root,
            event,
            meta,
            env,
            started=started,
            output_id=output_id,
            output_dir=output_dir,
        )

    stdin_fd = devnull_stdin_fd()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=stdin_fd if stdin_fd is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **quiet_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        log.warning(
            "sandbox.run_exec_spawn_failed event=%s argv0=%s cwd=%s error=%s",
            event,
            argv[0] if argv else "",
            cwd,
            exc,
        )
        raise
    finally:
        if stdin_fd is not None:
            try:
                os.close(stdin_fd)
            except OSError:
                pass

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        timed_out = False
    except asyncio.TimeoutError:
        # Try gentle termination first so Python / shell can flush buffers,
        # then escalate to kill. We collect whatever the process managed to
        # write before being killed so the caller (and the AI) can still
        # use partial output instead of treating the run as a total loss.
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
            except asyncio.TimeoutError:
                stdout, stderr = b"", b""
        timed_out = True
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""
    result = {
        "ok": proc.returncode == 0 and not timed_out,
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": proc.returncode,
        "duration_ms": int((time.time() - started) * 1000),
        "stdout_chars": len(out),
        "stderr_chars": len(err),
        "stdout_excerpt": out[:OUTPUT_LIMIT],
        "stderr_excerpt": err[:OUTPUT_LIMIT],
        "truncated": len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        **meta,
    }
    if len(out) > PROMPT_STDOUT_LIMIT:
        stdout_path = output_dir / f"{output_id}.stdout.txt"
        stdout_path.write_text(out, encoding="utf-8", errors="replace")
        result["stdout_path"] = stdout_path.relative_to(audit_root).as_posix()
        result["stdout_hint"] = (
            "Full stdout was saved because the model prompt only receives an excerpt. "
            "Use sandbox_read_file/read_text_file on stdout_path for exact content."
        )
    if len(err) > PROMPT_STDERR_LIMIT:
        stderr_path = output_dir / f"{output_id}.stderr.txt"
        stderr_path.write_text(err, encoding="utf-8", errors="replace")
        result["stderr_path"] = stderr_path.relative_to(audit_root).as_posix()
        result["stderr_hint"] = (
            "Full stderr was saved because the model prompt only receives an excerpt. "
            "Use sandbox_read_file/read_text_file on stderr_path for exact content."
        )
    if timed_out:
        has_partial = bool(out or err)
        result["partial_output"] = has_partial
        gui_hint = (
            " If you intentionally launched a long-lived GUI app or server that already "
            "started, this timeout does NOT mean launch failed — the runner waited for "
            "exit and then killed the process. Re-run with background=true so the tool "
            "detaches immediately without killing the child."
        )
        if has_partial:
            result["hint"] = (
                f"Process was killed after {timeout}s timeout but partial output was captured "
                f"({len(out)} bytes stdout / {len(err)} bytes stderr). "
                "Mine the captured bytes first — if they already contain what you need, use them "
                "directly. Only re-run with a larger timeout_seconds (max 1800) or a narrower / "
                "chunked scope when the missing tail is actually required."
                + gui_hint
            )
        else:
            result["hint"] = (
                f"Process was killed after {timeout}s timeout and no output was captured "
                "(the child likely had not flushed yet, or it is a GUI that never writes "
                "stdout). Re-run with a larger timeout_seconds (max 1800), narrower scope, "
                "smaller batch size, break the work into checkpointed chunks, "
                "or use background=true for GUI/servers that should keep running."
                + gui_hint
            )
    audit_event(audit_root, event, **{k: v for k, v in result.items() if k != "ok"})
    if timed_out:
        log.warning(
            "sandbox.run_exec_timeout event=%s argv0=%s duration_ms=%d stdout_chars=%d stderr_chars=%d",
            event,
            argv[0] if argv else "",
            result["duration_ms"],
            result["stdout_chars"],
            result["stderr_chars"],
        )
    elif proc.returncode != 0:
        log.warning(
            "sandbox.run_exec_nonzero event=%s argv0=%s exit_code=%s duration_ms=%d stdout_chars=%d stderr_chars=%d stderr_head=%r",
            event,
            argv[0] if argv else "",
            proc.returncode,
            result["duration_ms"],
            result["stdout_chars"],
            result["stderr_chars"],
            err[:160],
        )
    else:
        log.info(
            "sandbox.run_exec_ok event=%s argv0=%s duration_ms=%d stdout_chars=%d stderr_chars=%d",
            event,
            argv[0] if argv else "",
            result["duration_ms"],
            result["stdout_chars"],
            result["stderr_chars"],
        )
    return result



def _timeout(value: int | str | None) -> int:
    try:
        n = int(value or 30)
    except Exception:
        n = 30
    return max(1, min(1800, n))


def _parse_args(args: str | list[str] | None) -> list[str]:
    if args is None:
        return []
    if isinstance(args, list):
        return [str(x) for x in args]
    import shlex

    return shlex.split(str(args), posix=not platform.system().lower().startswith("win"))


def _python_path(python_env: str = "") -> str:
    if python_env:
        candidate = shutil.which(python_env) or python_env
        if Path(candidate).exists() or shutil.which(candidate):
            return candidate
    return sys.executable


def _venv_env(root: Path) -> dict[str, str]:
    env = _python_subprocess_env(root)
    bin_dir = str(_venv_bin_dir(root))
    env["VIRTUAL_ENV"] = str(root / PY_VENV_DIR)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return _with_python_sitecustomize(root, env)


def _write_direct_python_shims(root: Path, base_cmd: list[str]) -> Path | None:
    if not base_cmd:
        return None
    shim_dir = root / ".python-shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    command = subprocess.list2cmdline(base_cmd)
    if platform.system().lower().startswith("win"):
        python_cmd = f"@echo off\r\n{command} %*\r\n"
        pip_cmd = f"@echo off\r\n{command} -m pip %*\r\n"
        py_cmd = (
            "@echo off\r\n"
            "setlocal EnableDelayedExpansion\r\n"
            "if \"%~1\"==\"-3\" shift /1\r\n"
            "if \"%~1\"==\"-2\" shift /1\r\n"
            "set \"ARGUSAI_PY_ARGS=\"\r\n"
            ":argusai_py_loop\r\n"
            "if \"%~1\"==\"\" goto argusai_py_run\r\n"
            "set ARGUSAI_PY_ARGS=!ARGUSAI_PY_ARGS! \"%~1\"\r\n"
            "shift /1\r\n"
            "goto argusai_py_loop\r\n"
            ":argusai_py_run\r\n"
            f"{command} !ARGUSAI_PY_ARGS!\r\n"
        )
        for name, content in {
            "python.cmd": python_cmd,
            "python3.cmd": python_cmd,
            "pip.cmd": pip_cmd,
            "pip3.cmd": pip_cmd,
            "py.cmd": py_cmd,
        }.items():
            (shim_dir / name).write_text(content, encoding="utf-8", newline="")
        return shim_dir
    python_sh = f"#!/bin/sh\nexec {command} \"$@\"\n"
    pip_sh = f"#!/bin/sh\nexec {command} -m pip \"$@\"\n"
    for name, content in {"python": python_sh, "python3": python_sh, "pip": pip_sh, "pip3": pip_sh}.items():
        path = shim_dir / name
        path.write_text(content, encoding="utf-8", newline="\n")
        try:
            path.chmod(0o755)
        except OSError:
            pass
    return shim_dir


def _direct_python_env(root: Path, base_cmd: list[str] | None = None) -> dict[str, str]:
    """Sandbox-ish environment for direct Python fallback when venv fails."""
    env = _python_subprocess_env()
    base_cmd = base_cmd or _direct_python_command("")
    user_base = root / ".python-user"
    user_bin = user_base / ("Scripts" if platform.system().lower().startswith("win") else "bin")
    shim_dir = _write_direct_python_shims(root, base_cmd)
    env["PYTHONUSERBASE"] = str(user_base)
    env["PIP_USER"] = "1"
    path_parts = [str(user_bin)]
    if shim_dir is not None:
        path_parts.insert(0, str(shim_dir))
    env["PATH"] = os.pathsep.join(path_parts + [env.get("PATH", "")])
    if base_cmd:
        env["PYTHON"] = subprocess.list2cmdline(base_cmd)
    return _with_python_sitecustomize(root, env)


def _direct_python_command(python_env: str = "") -> list[str]:
    """Resolve a host interpreter only for explicit host-escalation tools."""
    if python_env:
        if python_env == "py-3" and platform.system().lower().startswith("win"):
            py = shutil.which("py")
            return [py, "-3"] if py else []
        candidate = shutil.which(python_env) or python_env
        if Path(candidate).exists() or shutil.which(candidate):
            return [candidate]
    if _looks_like_python_executable(sys.executable):
        return [sys.executable]
    for name in (("python", "python3") if platform.system().lower().startswith("win") else ("python3", "python")):
        candidate = shutil.which(name)
        if candidate:
            return [candidate]
    if platform.system().lower().startswith("win"):
        py = shutil.which("py")
        if py:
            return [py, "-3"]
    return []


async def _run_python_direct_fallback(
    root: Path,
    target: Path,
    argv_args: list[str],
    python_env: str,
    timeout_seconds: int,
    venv_error: dict[str, Any],
) -> dict[str, Any]:
    base_cmd = _direct_python_command(python_env)
    if not base_cmd:
        return {"ok": False, **venv_error, "direct_fallback_available": False}
    result = await _run_exec(
        [*base_cmd, str(target), *argv_args],
        root,
        _timeout(timeout_seconds),
        root,
        "run_python_script",
        {
            "path": str(target),
            "relative_path": target.relative_to(root).as_posix(),
            "python": " ".join(base_cmd),
            "venv_dir": str(root / PY_VENV_DIR),
            "venv_created": False,
            "direct_python_fallback": True,
            "venv_error": {k: v for k, v in venv_error.items() if k in {"error", "output", "fallback_output", "runtime_health"}},
            "warning": (
                "Sandbox venv creation failed, so the script ran with the base Python interpreter. "
                "pip is forced to --user under sandbox .python-user when honored by pip."
            ),
        },
        env=_direct_python_env(root, base_cmd),
    )
    result["direct_python_fallback"] = True
    result["venv_error"] = {k: v for k, v in venv_error.items() if k in {"error", "output", "fallback_output", "runtime_health"}}
    if result.get("ok"):
        result["warning"] = (
            "Python ran via direct fallback because sandbox venv creation failed. "
            "Use output normally, but package isolation is weaker than a real .venv."
        )
    return result


def _script_uses_python_tooling(script: str) -> bool:
    return bool(re.search(r"\b(py|python|python3|pip|pip3)\b", script or "", re.IGNORECASE))


async def run_python_script(
    root: Path,
    path: str,
    python_env: str = "",
    args: str | list[str] | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> dict[str, Any]:
    blocked = _reject_sandbox_python_env(python_env)
    if blocked:
        return blocked
    target = resolve_in_sandbox(root, path, allowed_exts=PYTHON_EXTS)
    if not target.exists():
        return {"ok": False, "error": "script not found", "path": str(target)}
    argv_args = _parse_args(args)
    if _has_blocked_args(argv_args):
        return {"ok": False, "error": "blocked shell metacharacter in args"}
    venv = ensure_python_venv(root, python_env=python_env)
    if not venv.get("ok"):
        return {
            "ok": False,
            **venv,
            "path": str(target),
            "relative_path": target.relative_to(root).as_posix(),
        }
    py = str(venv["python"])
    result = await _run_exec(
        [py, str(target), *argv_args],
        root,
        _timeout(timeout_seconds),
        root,
        "run_python_script",
        {
            "path": str(target),
            "relative_path": target.relative_to(root).as_posix(),
            "python": py,
            "venv_dir": venv.get("venv_dir", ""),
            "venv_created": bool(venv.get("created")),
        },
        env=_venv_env(root),
        background=bool(background),
    )
    return result


def _shell_argv(shell: str, script_path: Path) -> list[str]:
    shell = (shell or "").lower()
    if shell in {"powershell", "windows-powershell"}:
        exe = shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    if shell in {"pwsh", "powershell7"}:
        exe = shutil.which("pwsh") or "pwsh"
        return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    if shell == "cmd":
        exe = shutil.which("cmd") or "cmd"
        return [exe, "/c", str(script_path)]
    bash = find_bash_executable()
    if shell == "sh":
        return [bash or shutil.which("sh") or "sh", str(script_path)]
    return [bash or "bash", str(script_path)]


def _normalize_bash_newlines(script: str) -> str:
    """Force LF endings so heredoc delimiters survive Windows writers."""
    return (script or "").replace("\r\n", "\n").replace("\r", "\n")


_SSH_STDIN_SAFE_RE = re.compile(
    r"(?is)\bssh\b[^\n]*(?:\s-n\b|< ?/dev/null|-o\s*BatchMode(?:=yes)?\b|BatchMode=yes)"
)
_SSH_INVOKE_RE = re.compile(r"(?m)(?:^|[;&|]\s*)ssh\s+")


def _ssh_stdin_risk_hint(script: str) -> str | None:
    """Warn when ssh is invoked without stdin detachment (common hang on Windows)."""
    text = script or ""
    if not _SSH_INVOKE_RE.search(text):
        return None
    if _SSH_STDIN_SAFE_RE.search(text):
        return None
    return (
        "Script invokes ssh without `-n`, `</dev/null`, or `BatchMode=yes`. "
        "On Windows hosts, inherited/piped stdin often makes ssh hang waiting for input. "
        "Prefer `ssh -n -o BatchMode=yes host 'cmd'`, or `shell_session_start` → "
        "`ssh -T user@host` then `shell_session_exec`. Do not pipe multi-line scripts "
        "from PowerShell into ssh."
    )


_BASH_MISSING_HINT = (
    "Install Git for Windows (adds a real bash.exe; ArgusAI ignores the WSL Store stub), "
    "or use WSL only when intentionally targeting a Linux distro. "
    "For remote Linux work: do NOT rewrite bash/heredoc as PowerShell+ssh quoting — use "
    "`shell_session_start(command='ssh -T user@host')` then `shell_session_exec`, "
    "or `sandbox_write_file` a `.sh` + `scp` + `ssh -n host 'bash script.sh'`."
)


async def run_shell_script(
    root: Path,
    path: str,
    shell: str = "",
    args: str | list[str] | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> dict[str, Any]:
    target = resolve_in_sandbox(root, path, allowed_exts=SHELL_EXTS)
    if not target.exists():
        return {"ok": False, "error": "script not found", "path": str(target)}
    argv_args = _parse_args(args)
    if _has_blocked_args(argv_args):
        return {"ok": False, "error": "blocked shell metacharacter in args"}
    script = target.read_text(encoding="utf-8", errors="replace")
    reason = _dangerous_shell_reason(script)
    if reason:
        return {"ok": False, "error": reason, "path": str(target)}
    shell_name = shell or _default_shell_for_ext(target.suffix)
    # Normalize CRLF for bash/sh scripts written on Windows (heredoc-safe).
    if target.suffix.lower() == ".sh" or shell_name in {"bash", "sh"}:
        normalized = _normalize_bash_newlines(script)
        if normalized != script:
            try:
                target.write_text(normalized, encoding="utf-8", newline="")
                script = normalized
            except OSError:
                pass
    env = None
    venv_meta: dict[str, Any] = {}
    if _script_uses_python_tooling(script):
        venv = ensure_python_venv(root)
        if not venv.get("ok"):
            return {
                "ok": False,
                **venv,
                "path": str(target),
                "relative_path": target.relative_to(root).as_posix(),
                "shell": shell_name,
            }
        env = _venv_env(root)
        venv_meta = {
            "venv_dir": venv.get("venv_dir", ""),
            "python": venv.get("python", ""),
            "venv_created": bool(venv.get("created")),
            "python_env_injected": True,
        }
    argv = [*_shell_argv(shell_name, target), *argv_args]
    result = await _run_exec(
        argv,
        root,
        _timeout(timeout_seconds),
        root,
        "run_shell_script",
        {"path": str(target), "relative_path": target.relative_to(root).as_posix(), "shell": shell_name, **venv_meta},
        env=env,
        background=bool(background),
    )
    ssh_hint = _ssh_stdin_risk_hint(script)
    if ssh_hint:
        existing = str(result.get("hint") or "").strip()
        result["hint"] = (existing + "\n\n" + ssh_hint).strip() if existing else ssh_hint
        result["ssh_stdin_risk"] = True
    return result


def _default_shell_for_ext(ext: str) -> str:
    ext = ext.lower()
    if ext == ".ps1":
        return "powershell"
    if ext in {".bat", ".cmd"}:
        return "cmd"
    return "bash"


async def run_python_snippet(
    root: Path,
    code: str,
    python_env: str = "",
    args: str | list[str] | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> dict[str, Any]:
    if not str(code or "").strip():
        return {
            "ok": False,
            "error_code": "invalid_arguments",
            "error": "code is required and must not be empty",
            "retryable": True,
        }
    blocked = _reject_sandbox_python_env(python_env)
    if blocked:
        return blocked
    host_reason = _host_interpreter_escape_reason(code or "")
    if host_reason:
        return {
            "ok": False,
            "error": host_reason,
            "error_code": "sandbox_host_escape_blocked",
            "retryable": False,
            "suggested_fix": (
                "Remove host interpreter paths from the snippet body. Use sys.executable inside the sandbox, "
                "or escalate with run_host_python_snippet when the sandbox .venv truly cannot run this code."
            ),
        }
    name = f"{TMP_DIR}/snippet-{int(time.time())}-{uuid.uuid4().hex[:8]}.py"
    written = write_file(root, name, code, "py")

    # Pre-execution lint: catch fragility patterns (pip install -q with
    # check_call, bare r.json() without try/except, etc.) and bubble them
    # back to the caller / AI so they can self-correct instead of getting
    # a "non-zero exit status 1" with no diagnostic context.
    lint_warnings: list[str] = []
    try:
        from argusai.core.security.python_poc import lint_python_source

        lint_warnings = lint_python_source(code or "", filename="snippet.py")
    except Exception as exc:
        log.debug("python lint failed for snippet: %s", exc)

    result = await run_python_script(
        root,
        written["relative_path"],
        python_env=python_env,
        args=args,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    result["snippet_path"] = written["path"]
    if lint_warnings:
        result["lint_warnings"] = lint_warnings
        if not result.get("ok"):
            # Failure + lint warnings = high chance the warnings explain
            # the failure (especially the pip -q / check_call combo that
            # hides stderr entirely). Promote them to the top-level hint
            # so the model sees them in the prompt without spelunking.
            existing_hint = str(result.get("hint") or "").strip()
            lint_hint = (
                "脚本静态分析发现可能与失败相关的反模式：\n- "
                + "\n- ".join(lint_warnings)
            )
            result["hint"] = (existing_hint + "\n\n" + lint_hint).strip() if existing_hint else lint_hint
    return result


_PS_UTF8_PROLOGUE = (
    "$ErrorActionPreference = 'Continue'\r\n"
    "# Force UTF-8 for everything PowerShell prints, reads back from\r\n"
    "# native binaries, or pipes between cmdlets. Without this the\r\n"
    "# host inherits the OS default ANSI code page (e.g. GBK on zh-CN\r\n"
    "# Windows), which mangles CJK paths and stdout into '\\xe6\\x9c\\xaa…'.\r\n"
    "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\r\n"
    "try { [Console]::InputEncoding  = [System.Text.Encoding]::UTF8 } catch {}\r\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
    "try { chcp 65001 > $null } catch {}\r\n"
)

_CMD_UTF8_PROLOGUE = (
    "@echo off\r\n"
    "rem Force the CMD console code page to UTF-8 so CJK paths /\r\n"
    "rem stdout aren't garbled by the inherited ANSI code page.\r\n"
    "chcp 65001 > nul\r\n"
)


def _is_powershell_shell(name: str) -> bool:
    return (name or "").lower() in {"powershell", "windows-powershell", "pwsh", "powershell7"}


def _script_uses_andor_chaining(script: str) -> bool:
    """True if the snippet chains commands with ``&&`` / ``||`` outside of
    quotes. Windows PowerShell 5.1 rejects these as a syntax error, so models
    writing the bash idiom (``node -v && npm -v || echo missing``) fail there."""
    stripped = re.sub(r"\"(?:[^\"`]|`.)*\"|'[^']*'", "", script or "")
    return "&&" in stripped or "||" in stripped


def _script_looks_like_bash(script: str) -> bool:
    """Detect bash/sh scripts that must not be silently executed by cmd/PowerShell."""
    text = script or ""
    # Covers #!/bin/bash, #!/usr/bin/env bash, #!/bin/sh, etc.
    if re.search(r"(?m)^#!\s*(?:\S*/)?(?:env\s+)?(?:bash|sh)\b", text):
        return True
    if re.search(r"<<-?\s*['\"]?\w+['\"]?", text):
        return True
    if re.search(r"(?m)^\s*(?:set\s+-euo\s+pipefail|ufw\s+|ss\s+-[A-Za-z]*\b)", text):
        return True
    return False


def _resolve_snippet_shell(
    shell: str,
    script: str,
    *,
    default_shell: str = "auto",
) -> tuple[str, str | None, str | None]:
    """Pick the best shell for a snippet.

    Returns ``(shell_name, auto_switch_note, error)``. Bash/heredoc scripts are
    never silently routed to ``cmd``; PowerShell ``&&``/``||`` still upgrade to
    ``pwsh`` or ``cmd`` when appropriate.
    """
    from argusai.core.settings import normalize_default_shell

    is_win = platform.system().lower().startswith("win")
    pref = normalize_default_shell(default_shell)
    requested = str(shell or "").strip().lower()
    if not requested:
        if pref == "auto":
            shell_name = "powershell" if is_win else "bash"
        else:
            shell_name = pref
    else:
        shell_name = requested

    if _script_looks_like_bash(script) and shell_name not in {"bash", "sh"}:
        if find_bash_executable():
            return "bash", f"{shell_name}→bash (bash/heredoc script detected)", None
        return (
            shell_name,
            None,
            "Script looks like bash (shebang/heredoc/Linux idioms) but bash/sh is "
            "not available. " + _BASH_MISSING_HINT + " Refusing to auto-route bash "
            "scripts to cmd/PowerShell.",
        )

    if not is_win or not _is_powershell_shell(shell_name):
        return shell_name, None, None
    if shell_name in {"pwsh", "powershell7"}:
        return shell_name, None, None
    has_pwsh = bool(shutil.which("pwsh"))
    if has_pwsh:
        return "pwsh", "powershell→pwsh (using PowerShell 7 for && / || support)", None
    if _script_uses_andor_chaining(script):
        return "cmd", "powershell→cmd (Windows PowerShell 5.1 does not support && / ||)", None
    return shell_name, None, None


async def run_shell_snippet(
    root: Path,
    script: str,
    shell: str = "",
    timeout_seconds: int = 30,
    background: bool = False,
    default_shell: str = "auto",
) -> dict[str, Any]:
    if not str(script or "").strip():
        return {
            "ok": False,
            "error_code": "invalid_arguments",
            "error": "script is required and must not be empty",
            "retryable": True,
        }
    reason = _dangerous_shell_reason(script)
    if reason:
        return {"ok": False, "error": reason, "cwd": str(root), "shell": shell}
    shell_name, shell_switch_note, shell_error = _resolve_snippet_shell(
        shell,
        str(script or ""),
        default_shell=default_shell,
    )
    if shell_error:
        return {
            "ok": False,
            "error_code": "shell_mismatch",
            "error": shell_error,
            "cwd": str(root),
            "shell": shell_name,
            "hint": _BASH_MISSING_HINT,
            "retryable": True,
        }
    ext = ".ps1" if _is_powershell_shell(shell_name) else (".cmd" if shell_name == "cmd" else ".sh")
    name = f"{TMP_DIR}/snippet-{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}"

    body = str(script or "")
    if _is_powershell_shell(shell_name):
        # Avoid double-injecting if the model already set its own encoding.
        if "[Console]::OutputEncoding" not in body and "chcp 65001" not in body:
            body = _PS_UTF8_PROLOGUE + body
    elif shell_name == "cmd":
        if "chcp 65001" not in body:
            body = _CMD_UTF8_PROLOGUE + body
    elif shell_name in {"bash", "sh"}:
        # CRLF breaks bash heredoc delimiters (`<<'EOF'` never matches `EOF\r`).
        body = _normalize_bash_newlines(body)

    written = write_file(root, name, body, ext.lstrip("."))

    # Windows PowerShell 5.1 reads .ps1 with the OS ANSI code page unless
    # there is a UTF-8 BOM. Re-write the .ps1 with a BOM so Chinese paths
    # in the script body don't get decoded as GBK and explode with
    # ParserError (`+ CategoryInfo : InvalidEndOfLine`). UTF-8 BOM on a
    # .cmd is also safely consumed by modern cmd.exe.
    if _is_powershell_shell(shell_name) or shell_name == "cmd":
        try:
            target_path = Path(written["path"])
            payload = body.encode("utf-8")
            if not payload.startswith(b"\xef\xbb\xbf"):
                target_path.write_bytes(b"\xef\xbb\xbf" + payload)
        except Exception:
            # Best-effort; the prologue alone usually suffices for stdout.
            pass

    result = await run_shell_script(
        root,
        written["relative_path"],
        shell=shell_name,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    result["snippet_path"] = written["path"]
    if shell_switch_note:
        result["shell_auto_switch"] = shell_switch_note
    ssh_hint = _ssh_stdin_risk_hint(str(script or ""))
    if ssh_hint:
        existing = str(result.get("hint") or "").strip()
        result["hint"] = (existing + "\n\n" + ssh_hint).strip() if existing else ssh_hint
        result["ssh_stdin_risk"] = True
    return result


async def run_host_python_script(
    root: Path,
    path: str,
    *,
    reason: str = "",
    sandbox_attempt_summary: str = "",
    python_env: str = "",
    args: str | list[str] | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> dict[str, Any]:
    """Run a sandbox-resident .py file with a host Python interpreter (explicit escalation)."""
    invalid = _validate_host_escalation_fields(reason, sandbox_attempt_summary)
    if invalid:
        return invalid
    target = resolve_in_sandbox(root, path, allowed_exts=PYTHON_EXTS)
    if not target.exists():
        return {"ok": False, "error": "script not found", "path": str(target)}
    try:
        script_body = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        script_body = ""
        log.debug("host script read failed: %s", exc)
    blocked = _validate_host_escalation_disallowed_use(script_body)
    if blocked:
        return blocked
    argv_args = _parse_args(args)
    if _has_blocked_args(argv_args):
        return {"ok": False, "error": "blocked shell metacharacter in args"}
    base_cmd = _direct_python_command(python_env)
    if not base_cmd:
        return {
            "ok": False,
            "error": "no host Python interpreter is available for host escalation",
            "error_code": "host_interpreter_unavailable",
            "retryable": False,
            "hint": "Call discover_python_environments and pass python_env, or ask the user to install Python 3.",
        }
    result = await _run_exec(
        [*base_cmd, str(target), *argv_args],
        root,
        _timeout(timeout_seconds),
        root,
        "run_host_python_script",
        {
            "path": str(target),
            "relative_path": target.relative_to(root).as_posix(),
            "python": " ".join(base_cmd),
            "execution_tier": "host",
            "host_python": True,
            "reason": str(reason).strip(),
            "sandbox_attempt_summary": str(sandbox_attempt_summary).strip(),
        },
        env=None,
        background=bool(background),
    )
    result["execution_tier"] = "host"
    result["host_python"] = True
    result["warning"] = (
        "Script ran with a host Python interpreter outside the sandbox .venv. "
        "Packages and stdlib match the selected host install, not the session sandbox."
    )
    return result


async def run_host_python_snippet(
    root: Path,
    code: str,
    *,
    reason: str = "",
    sandbox_attempt_summary: str = "",
    python_env: str = "",
    args: str | list[str] | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
) -> dict[str, Any]:
    """Materialise code under the sandbox audit tree but execute with host Python."""
    invalid = _validate_host_escalation_fields(reason, sandbox_attempt_summary)
    if invalid:
        return invalid
    blocked = _validate_host_escalation_disallowed_use(code or "")
    if blocked:
        return blocked
    host_reason = _host_interpreter_escape_reason(code or "")
    if host_reason:
        return {
            "ok": False,
            "error": (
                "host snippet body must not hard-code another interpreter path; "
                "python_env selects the host interpreter for this tool"
            ),
            "error_code": "host_snippet_interpreter_conflict",
            "retryable": True,
        }
    name = f"{TMP_DIR}/host-snippet-{int(time.time())}-{uuid.uuid4().hex[:8]}.py"
    written = write_file(root, name, code, "py")
    result = await run_host_python_script(
        root,
        written["relative_path"],
        reason=reason,
        sandbox_attempt_summary=sandbox_attempt_summary,
        python_env=python_env,
        args=args,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    result["snippet_path"] = written["path"]
    return result

