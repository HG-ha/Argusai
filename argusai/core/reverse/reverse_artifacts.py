"""Artifact helpers for Reverse Agent Runtime.

Wraps :class:`ReverseJobStore` with stable ``artifact_ref`` identifiers,
external file import, and LLM-safe previews (Loom22 §10 / §21).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from argusai.core.reverse.reverse_constants import ARTIFACT_KINDS
from argusai.core.reverse.reverse_jobs import ReverseJobStore

_ARTIFACT_REF_PREFIX = "rev://"


def artifact_ref(job_id: str, artifact_id: str) -> str:
    return f"{_ARTIFACT_REF_PREFIX}{job_id}/{artifact_id}"


def parse_artifact_ref(ref: str) -> tuple[str, str] | None:
    text = str(ref or "").strip()
    if not text.startswith(_ARTIFACT_REF_PREFIX):
        return None
    rest = text[len(_ARTIFACT_REF_PREFIX):]
    if "/" not in rest:
        return None
    job_id, artifact_id = rest.split("/", 1)
    if not job_id or not artifact_id:
        return None
    return job_id, artifact_id


def preview_for_llm(
    store: ReverseJobStore,
    job_id: str,
    artifact_id: str,
    *,
    max_bytes: int = 4096,
) -> dict[str, Any]:
    """Return a compact artifact summary suitable for tool/LLM context."""
    data = store.read_artifact(job_id, artifact_id, max_bytes=max_bytes)
    if data is None:
        return {"ok": False, "error": "artifact not found"}
    content = str(data.get("content") or "")
    truncated = bool(data.get("truncated"))
    preview = content if len(content) <= 2000 else content[:2000] + "\n…"
    return {
        "ok": True,
        "artifact_ref": artifact_ref(job_id, artifact_id),
        "job_id": job_id,
        "artifact_id": artifact_id,
        "kind": data.get("kind"),
        "size": data.get("size") or data.get("total_size"),
        "sha256": data.get("sha256"),
        "preview": preview,
        "truncated": truncated,
        "read_more": {
            "tool": "reverse_artifact_read",
            "arguments": {
                "job_id": job_id,
                "artifact_id": artifact_id,
                "max_bytes": max_bytes,
            },
        },
    }


def resolve_artifact_path(store: ReverseJobStore, job_id: str, artifact_id: str) -> Path:
    """Return the absolute on-disk path of an artifact, with containment check.

    Used by GUI reveal/copy/export flows so callers don't repeat the
    job_dir / artifacts/ resolve + ``startswith(job_dir)`` boundary
    enforcement. Raises ``FileNotFoundError`` if the job or artifact
    record is missing, or ``ValueError`` if the recorded ``path`` would
    escape its job directory.
    """
    job = store.get(str(job_id or ""), include_trashed=True)
    if job is None:
        raise FileNotFoundError(f"reverse job not found: {job_id}")
    target = next(
        (a for a in (job.get("artifacts") or []) if a.get("id") == artifact_id),
        None,
    )
    if target is None:
        raise FileNotFoundError(f"artifact not found: {artifact_id}")
    job_dir = Path(str(job.get("_dir") or "")).resolve()
    rel = str(target.get("path") or "")
    if not rel:
        raise FileNotFoundError(f"artifact has no path: {artifact_id}")
    full = (job_dir / rel).resolve()
    if not str(full).startswith(str(job_dir)):
        raise ValueError(f"artifact path escapes job directory: {artifact_id}")
    if not full.is_file():
        raise FileNotFoundError(f"artifact file missing on disk: {artifact_id}")
    return full


class ReverseArtifactStore:
    """Thin facade over ``ReverseJobStore`` artifact operations."""

    def __init__(self, store: ReverseJobStore) -> None:
        self.store = store

    def register_path(
        self,
        job_id: str,
        *,
        source_path: str | Path,
        kind: str,
        filename: str = "",
        url: str = "",
        from_artifact: str = "",
        tool: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        path = Path(source_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"artifact source not found: {path}")
        payload = path.read_bytes()
        kind_norm = str(kind or "other").strip().lower()
        if kind_norm not in ARTIFACT_KINDS:
            kind_norm = "other"
        record = self.store.add_artifact(
            job_id,
            content=payload,
            kind=kind_norm,
            filename=filename or path.name,
            url=url,
            from_artifact=from_artifact,
            tool=tool,
            note=note,
        )
        record["artifact_ref"] = artifact_ref(job_id, str(record.get("id") or ""))
        return record

    def import_copy(
        self,
        job_id: str,
        *,
        source_path: str | Path,
        kind: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Copy an external file into the job artifact store."""
        return self.register_path(job_id, source_path=source_path, kind=kind, **kwargs)

    def preview(self, job_id: str, artifact_id: str, *, max_bytes: int = 4096) -> dict[str, Any]:
        return preview_for_llm(self.store, job_id, artifact_id, max_bytes=max_bytes)

    def copy_to(
        self,
        job_id: str,
        artifact_id: str,
        destination: str | Path,
    ) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            raise FileNotFoundError(f"reverse job not found: {job_id}")
        target = next((a for a in job.get("artifacts") or [] if a.get("id") == artifact_id), None)
        if target is None:
            raise FileNotFoundError(f"artifact not found: {artifact_id}")
        job_dir = Path(str(job.get("_dir") or ""))
        src = (job_dir / str(target.get("path") or "")).resolve()
        if not str(src).startswith(str(job_dir.resolve())) or not src.is_file():
            raise FileNotFoundError(f"artifact file missing: {artifact_id}")
        dst = Path(destination).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return {"ok": True, "path": str(dst), "size": dst.stat().st_size}
