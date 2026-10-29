"""Artifact CRUD mixin for ``ReverseJobStore``."""

from __future__ import annotations

from typing import Any

from argusai.core.reverse.reverse_job_io import (
    atomic_write_bytes as _atomic_write_bytes,
    new_sub_id as _new_sub_id,
    norm_artifact_kind as _norm_artifact_kind,
    now_iso as _now_iso,
    sha256_bytes as _sha256_bytes,
    slug as _slug,
)


class ReverseJobArtifactMixin:
    """Add / remove / read job artifacts (``artifacts/`` subtree)."""

    def add_artifact(
        self,
        job_id: str,
        *,
        content: bytes | str,
        kind: str,
        filename: str = "",
        url: str = "",
        from_artifact: str = "",
        tool: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        kind_norm = _norm_artifact_kind(kind)
        if isinstance(content, str):
            payload = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            payload = bytes(content)
        else:
            raise TypeError("content must be str or bytes")
        if len(payload) > 50 * 1024 * 1024:
            raise ValueError("artifact too large (>50MB)")

        digest = _sha256_bytes(payload)
        short = digest[:12]

        ext = ""
        if filename:
            safe = _slug(filename)
            if safe and "." in safe:
                ext = "." + safe.rsplit(".", 1)[1].lower()
        if not ext:
            ext = {
                "sourcemap": ".map",
                "wasm": ".wasm",
                "html": ".html",
                "trace": ".jsonl",
                "apk": ".apk",
                "aab": ".aab",
                "ipa": ".ipa",
                "xapk": ".xapk",
                "dex": ".dex",
                "pe": ".exe",
                "elf": ".elf",
                "macho": ".bin",
                "frida_log": ".jsonl",
                "frida_script": ".js",
                "debug_trace": ".jsonl",
                "manifest": ".xml",
                "plist": ".plist",
                "capa_report": ".json",
                "yara_match": ".json",
            }.get(kind_norm, ".js")

        relative = f"artifacts/{short}.{kind_norm}{ext}"
        if kind_norm == "webpack_module":
            relative = f"artifacts/webpack-modules/{short}{ext}"
        target = job_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(target, payload)

        art_id = _new_sub_id("art")
        record: dict[str, Any] = {
            "id": art_id,
            "kind": kind_norm,
            "path": relative,
            "size": len(payload),
            "sha256": digest,
            "created_at": _now_iso(),
        }
        if filename:
            record["filename"] = str(filename)[:200]
        if url:
            record["url"] = str(url)[:2048]
        if from_artifact:
            record["from"] = str(from_artifact)[:64]
        if tool:
            record["tool"] = str(tool)[:128]
        if note:
            record["note"] = str(note)[:500]

        data.setdefault("artifacts", []).append(record)
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "artifact_added", {"id": art_id, "kind": kind_norm, "size": len(payload)})
        return record

    def remove_artifact(self, job_id: str, artifact_id: str) -> bool:
        job_dir, data = self._load_or_raise(job_id)
        artifacts = data.get("artifacts") or []
        target = next((a for a in artifacts if a.get("id") == artifact_id), None)
        if target is None:
            return False
        rel = str(target.get("path") or "")
        full = (job_dir / rel).resolve()
        try:
            if str(full).startswith(str(job_dir.resolve())) and full.exists():
                full.unlink()
        except OSError:
            pass
        data["artifacts"] = [a for a in artifacts if a.get("id") != artifact_id]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "artifact_removed", {"id": artifact_id})
        return True

    def read_artifact(
        self,
        job_id: str,
        artifact_id: str,
        *,
        max_bytes: int = 32_768,
        offset_bytes: int = 0,
    ) -> dict[str, Any] | None:
        """Read an artifact's content slice.

        Defaults are TIGHT: 32 KB starting at byte 0. Larger reads or
        offset reads must be requested explicitly. Callers driven by
        the LLM should prefer ``grep_text(path=…artifact path)`` or
        ``read_text_file(path=…artifact path, offset=…, limit=…)`` for
        line-oriented access; this method exists for in-process Python
        callers that need raw bytes.
        """
        job_dir, data = self._load_or_raise(job_id)
        target = next((a for a in data.get("artifacts") or [] if a.get("id") == artifact_id), None)
        if target is None:
            return None
        rel = str(target.get("path") or "")
        full = (job_dir / rel).resolve()
        if not str(full).startswith(str(job_dir.resolve())) or not full.exists():
            return None
        size = full.stat().st_size
        start = max(0, int(offset_bytes or 0))
        if start > size:
            start = size
        with full.open("rb") as f:
            if start:
                f.seek(start)
            payload = f.read(max(0, int(max_bytes)))
        end = start + len(payload)
        truncated = end < size
        try:
            text = payload.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = payload.decode("latin-1", errors="replace")
            encoding = "latin-1"
        return {
            **target,
            "content": text,
            "encoding": encoding,
            "truncated": truncated,
            "total_size": size,
            "offset_bytes": start,
            "end_bytes": end,
            "more": truncated,
            "path_abs": str(full),
        }
