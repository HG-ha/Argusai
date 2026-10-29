"""File/folder helpers reachable from the frontend.

Two roles:

1. Open a system path (report file, task folder, arbitrary user path)
   via the OS default handler.
2. Resolve a chat-supplied filename against the per-session sandbox
   plus a handful of well-known data directories so the assistant can
   reference attachments by their bare name.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Any


class FilesMixin:
    """Path resolution + open-with-OS handlers."""

    def open_report(self, path: str) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"not found: {path}"}
        webbrowser.open(p.resolve().as_uri())
        return {"ok": True}

    def open_folder(self, path: str) -> dict[str, Any]:
        from argusai.core.infra.explorer import reveal_path

        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": f"not found: {path}"}
        result = reveal_path(p)
        if result.get("ok"):
            return {"ok": True, "path": result.get("path") or str(p)}
        return {"ok": False, "error": result.get("error") or f"not found: {path}"}

    def path_exists(self, path: str, session_id: str | None = None) -> dict[str, Any]:
        resolved, exists, is_dir = self._resolve_user_path(path, session_id)
        if not exists or resolved is None:
            return {"ok": True, "exists": False, "path": path}
        return {
            "ok": True,
            "exists": True,
            "is_dir": is_dir,
            "resolved": str(resolved),
            "path": path,
        }

    def open_external_url(self, url: str) -> dict[str, Any]:
        """Open an external URL in the OS default browser.

        Called by the chat markdown renderer when the user clicks an
        inline link or image rendered from an AI / MCP response. We
        deliberately do NOT trust the WebView to open the URL itself
        (``target="_blank"`` is unreliable inside pywebview) and we
        sanitise the scheme on the Python side so a malicious MCP
        response can never push ``javascript:`` / ``file://`` through
        ``v-html``-style escape gaps in the renderer.
        """
        raw = str(url or "").strip()
        if not raw or len(raw) > 4096:
            return {"ok": False, "error": "invalid url"}
        lower = raw.lower()
        allowed = ("https://", "http://", "mailto:")
        if not lower.startswith(allowed):
            return {"ok": False, "error": "scheme not allowed"}
        if "\n" in raw or "\r" in raw or "\x00" in raw:
            return {"ok": False, "error": "invalid url"}
        try:
            webbrowser.open(raw)
            return {"ok": True}
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc)}

    def read_media_file(
        self,
        path: str,
        session_id: str | None = None,
        offset_chars: int = 0,
        max_chars: int = 65_536,
    ) -> dict[str, Any]:
        """Return a bounded preview payload for an image/video/audio/pdf/text file.

        This is the single frontend entry point for the global media preview.
        It deliberately reuses ``_resolve_user_path`` so chat path chips,
        session-sandbox files, and data-root artifacts share one path policy.

        Binary previews are returned as ``data:`` URLs. Text is returned as
        UTF-8 with replacement so a weird log file cannot crash the renderer.
        Size caps keep the WebView from trying to hold very large files in
        memory.
        """
        import base64
        import mimetypes

        resolved, exists, is_dir = self._resolve_user_path(path, session_id)
        if not exists or resolved is None:
            return {"ok": False, "error": f"not found: {path}"}
        if is_dir:
            return {"ok": False, "error": "cannot preview a directory", "path": str(resolved)}
        try:
            stat = resolved.stat()
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": str(resolved)}

        ext = resolved.suffix.lower().lstrip(".")
        guessed, _ = mimetypes.guess_type(str(resolved))
        mime = guessed or "application/octet-stream"
        kind = self._media_kind(ext, mime)
        if kind == "unknown":
            return {
                "ok": False,
                "error": f"unsupported media type: {mime}",
                "path": str(resolved),
                "name": resolved.name,
                "size": int(stat.st_size),
                "mime": mime,
                "kind": kind,
            }

        caps = {
            "image": 32 * 1024 * 1024,
            "video": 64 * 1024 * 1024,
            "audio": 64 * 1024 * 1024,
            "pdf": 16 * 1024 * 1024,
            "text": 1024 * 1024,
        }
        cap = caps[kind]
        if stat.st_size > cap and kind != "text":
            return {
                "ok": False,
                "error": f"file too large for preview ({stat.st_size} > {cap} bytes)",
                "path": str(resolved),
                "name": resolved.name,
                "size": int(stat.st_size),
                "mime": mime,
                "kind": kind,
            }

        try:
            if kind == "text":
                from argusai.core.workspace.session_sandbox import _read_text_window

                window = _read_text_window(resolved, offset_chars, max_chars)
                truncated = bool(window["truncated"])
                return {
                    "ok": True,
                    "path": str(resolved),
                    "name": resolved.name,
                    "size": int(stat.st_size),
                    "mime": mime if mime != "application/octet-stream" else "text/plain",
                    "kind": kind,
                    "encoding": "utf-8",
                    "content": window["content"],
                    "truncated": truncated,
                    "more": truncated,
                    "offset_chars": window["offset_chars"],
                    "end_offset": window["end_offset"],
                    "total_chars": window["total_chars"],
                }
            raw = resolved.read_bytes()
            data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            return {
                "ok": True,
                "path": str(resolved),
                "name": resolved.name,
                "size": int(stat.st_size),
                "mime": mime,
                "kind": kind,
                "encoding": "base64",
                "content": data_url,
                "truncated": False,
            }
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": str(resolved)}

    def read_media_url(self, url: str) -> dict[str, Any]:
        """Fetch a remote image as a bounded data URL for canvas-safe annotation.

        WebView canvas export is blocked when a cross-origin image is drawn
        without CORS. For the annotation workflow we proxy only image media
        through Python, cap the response size, and return a ``data:`` URL.
        """
        import base64
        import urllib.request
        from urllib.parse import urlparse

        raw = str(url or "").strip()
        if not raw or len(raw) > 4096 or "\n" in raw or "\r" in raw or "\x00" in raw:
            return {"ok": False, "error": "invalid url"}
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "error": "scheme not allowed"}
        cap = 32 * 1024 * 1024
        try:
            req = urllib.request.Request(raw, headers={"User-Agent": "ArgusAI/MediaPreview"})
            with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310 - user-triggered preview URL
                mime = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if not mime.startswith("image/"):
                    return {"ok": False, "error": f"unsupported remote media type: {mime or 'unknown'}"}
                data = resp.read(cap + 1)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if len(data) > cap:
            return {"ok": False, "error": f"remote image too large (> {cap} bytes)"}
        name = Path(parsed.path).name or "remote-image.png"
        return {
            "ok": True,
            "kind": "image",
            "name": name,
            "url": raw,
            "mime": mime or "image/png",
            "size": len(data),
            "encoding": "base64",
            "content": f"data:{mime or 'image/png'};base64,{base64.b64encode(data).decode('ascii')}",
            "truncated": False,
        }

    def save_media_data(
        self,
        data_url: str,
        filename: str = "image.png",
        session_id: str | None = None,
        mode: str = "dialog",
    ) -> dict[str, Any]:
        """Save a frontend-generated media data URL.

        ``mode='sandbox'`` stores it under the assistant session sandbox
        ``_media/`` folder. ``mode='dialog'`` opens the OS save dialog when
        pywebview exposes one, then writes to the selected path.
        """
        import base64
        import re

        text = str(data_url or "").strip()
        m = re.match(r"^data:([^;,]+);base64,(.+)$", text, re.DOTALL)
        if not m:
            return {"ok": False, "error": "data_url must be a base64 data URL"}
        mime = m.group(1).lower()
        if not mime.startswith("image/") and mime != "application/pdf":
            return {"ok": False, "error": f"unsupported media type: {mime}"}
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception as exc:
            return {"ok": False, "error": f"invalid base64 payload: {exc}"}
        if len(raw) > 64 * 1024 * 1024:
            return {"ok": False, "error": "media payload too large"}

        safe_name = self._safe_download_filename(filename, mime)
        target: Path | None = None
        if mode == "sandbox":
            try:
                from argusai.core.paths import PathManager

                sid = str(session_id or "").strip()
                if not sid:
                    return {"ok": False, "error": "session_id is required for sandbox save"}
                root = PathManager(data_root=self.settings_mgr.get().data_root).assistant_sandbox(sid)
                target_dir = root / "_media"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = self._dedupe_path(target_dir / safe_name)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        else:
            try:
                import webview

                window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
                if window is None:
                    return {"ok": False, "error": "no active window for save dialog"}
                picked = window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=safe_name,
                    file_types=("PNG Images (*.png)", "JPEG Images (*.jpg;*.jpeg)", "WebP Images (*.webp)", "All files (*.*)"),
                )
                if not picked:
                    return {"ok": False, "cancelled": True}
                selected = picked[0] if isinstance(picked, (list, tuple)) else picked
                target = Path(str(selected)).expanduser()
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            return {"ok": True, "path": str(target.resolve()), "name": target.name, "size": len(raw), "mime": mime}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def _safe_download_filename(self, name: str, mime: str) -> str:
        raw = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        allowed = []
        for ch in raw:
            allowed.append(ch if (ch.isalnum() or ch in "._- ()[]+") else "_")
        cleaned = "".join(allowed).strip(" .") or "image"
        ext_map = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
            "image/svg+xml": "svg",
            "application/pdf": "pdf",
        }
        wanted_ext = ext_map.get(mime, "bin")
        if "." not in cleaned:
            cleaned = f"{cleaned}.{wanted_ext}"
        elif cleaned.rsplit(".", 1)[-1].lower() not in set(ext_map.values()):
            cleaned = f"{cleaned}.{wanted_ext}"
        if len(cleaned) > 140:
            stem, _, ext = cleaned.rpartition(".")
            cleaned = f"{stem[:120]}.{ext}" if ext else cleaned[:140]
        return cleaned

    def _dedupe_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        for i in range(2, 1000):
            candidate = parent / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}-{int(__import__('time').time())}{suffix}"

    def _pick_save_file(self, save_filename: str = "export.bin") -> dict[str, Any]:
        """Open the OS save dialog for exporting a single sandbox file."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for save dialog"}
            picked = window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=save_filename,
                file_types=("All files (*.*)",),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            target = Path(os.path.expandvars(str(selected))).expanduser()
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _pick_open_file(
        self,
        *,
        file_types: tuple[str, ...] = ("All files (*.*)",),
    ) -> dict[str, Any]:
        """Open the OS file picker for importing a single file."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for open dialog"}
            picked = window.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=file_types,
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            target = Path(os.path.expandvars(str(selected))).expanduser().resolve()
            if not target.is_file():
                return {"ok": False, "error": f"not a file: {target}"}
            return {"ok": True, "path": str(target)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _pick_export_folder(self) -> dict[str, Any]:
        """Open the OS folder picker for sandbox bulk export."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for folder dialog"}
            picked = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(Path.home()),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            folder = Path(os.path.expandvars(str(selected))).expanduser().resolve()
            if not folder.is_dir():
                return {"ok": False, "error": f"not a directory: {folder}"}
            return {"ok": True, "path": str(folder)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _media_kind(self, ext: str, mime: str) -> str:
        """Classify a file into the preview kinds supported by the WebView."""
        image_exts = {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"}
        video_exts = {"mp4", "webm", "mov", "m4v", "avi", "mkv"}
        audio_exts = {"mp3", "wav", "ogg", "m4a", "flac", "aac"}
        text_exts = {
            "txt", "log", "md", "markdown", "json", "jsonl", "csv", "yaml", "yml",
            "toml", "ini", "cfg", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx",
            "html", "htm", "css", "scss", "xml", "sql", "sh", "ps1", "bat", "cmd",
            "wat", "map",
        }
        m = str(mime or "").lower()
        if ext in image_exts or m.startswith("image/"):
            return "image"
        if ext == "pdf" or m == "application/pdf":
            return "pdf"
        if ext in video_exts or m.startswith("video/"):
            return "video"
        if ext in audio_exts or m.startswith("audio/"):
            return "audio"
        if ext in text_exts or m.startswith("text/"):
            return "text"
        return "unknown"

    def open_user_path(self, path: str, session_id: str | None = None) -> dict[str, Any]:
        resolved, exists, _ = self._resolve_user_path(path, session_id)
        if not exists or resolved is None:
            return {"ok": False, "error": f"not found: {path}"}
        try:
            if os.name == "nt":
                os.startfile(str(resolved))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(resolved)], close_fds=True)
            else:
                import shutil
                import subprocess

                opener = shutil.which("xdg-open")
                if opener:
                    subprocess.Popen([opener, str(resolved)], close_fds=True)
                else:
                    webbrowser.open(resolved.as_uri())
            return {"ok": True, "resolved": str(resolved)}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def _candidate_path_roots(self, session_id: str | None = None) -> list[Path]:
        """Roots searched (in priority order) when a chat references a bare filename."""
        s = self.settings_mgr.get()
        roots: list[Path] = []
        if session_id and isinstance(session_id, str) and session_id.strip():
            try:
                from argusai.core.paths import PathManager
                roots.append(PathManager(data_root=s.data_root).assistant_sandbox(session_id))
            except (ValueError, OSError):
                pass
        for raw in (
            getattr(s, "poc_pages_dir", None),
            getattr(s, "tasks_dir", None),
            getattr(s, "sessions_dir", None),
            getattr(s, "knowledge_base_dir", None),
            getattr(s, "data_root", None),
        ):
            if not raw:
                continue
            try:
                p = Path(raw).expanduser().resolve()
                if p.exists():
                    roots.append(p)
            except (OSError, ValueError):
                continue
        try:
            roots.append(Path.cwd().resolve())
        except OSError:
            pass
        seen: set[str] = set()
        deduped: list[Path] = []
        for r in roots:
            key = str(r).lower() if os.name == "nt" else str(r)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        return deduped

    def _resolve_user_path(
        self, raw_path: str, session_id: str | None = None
    ) -> tuple[Path | None, bool, bool]:
        """Return (resolved_path, exists, is_dir). resolved_path is None if no candidate exists."""
        if not isinstance(raw_path, str):
            return None, False, False
        text = raw_path.strip().strip('"').strip("'")
        if not text:
            return None, False, False
        try:
            direct = Path(text).expanduser()
            if direct.is_absolute():
                if direct.exists():
                    return direct.resolve(), True, direct.is_dir()
                return None, False, False
        except (OSError, ValueError):
            return None, False, False

        cleaned = text.lstrip("./").lstrip(".\\")
        if not cleaned:
            return None, False, False
        for root in self._candidate_path_roots(session_id):
            try:
                cand = (root / cleaned).resolve()
                # Containment check: prevent ``../`` escape into a sibling root.
                if not str(cand).startswith(str(root)):
                    continue
                if cand.exists():
                    return cand, True, cand.is_dir()
            except (OSError, ValueError):
                continue
        return None, False, False
