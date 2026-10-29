"""Findings / reproduce mixin for ``ReverseJobStore``."""

from __future__ import annotations

import json
from typing import Any

from argusai.core.reverse.reverse_job_io import (
    atomic_write_text as _atomic_write_text,
    new_sub_id as _new_sub_id,
    norm_finding_kind as _norm_finding_kind,
    now_iso as _now_iso,
    slug as _slug,
    slug_tags as _slug_tags,
)


class ReverseJobFindingMixin:
    """Markdown findings and reproduce scripts."""

    def add_finding(
        self,
        job_id: str,
        *,
        title: str,
        body: str = "",
        kind: str = "note",
        confidence: str = "probable",
        tags: list[Any] | None = None,
        coverage_id: str = "",
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        finding_id = _new_sub_id("f")
        kind_norm = _norm_finding_kind(kind)
        slug = _slug(title) or finding_id
        relative = f"findings/{finding_id}-{slug}.md"
        frontmatter = (
            "---\n"
            f"id: {finding_id}\n"
            f"kind: {kind_norm}\n"
            f"title: {json.dumps(str(title)[:200], ensure_ascii=False)}\n"
            f"confidence: {confidence}\n"
            f"tags: {json.dumps(_slug_tags(tags or []))}\n"
            f"coverage_id: {coverage_id or ''}\n"
            f"created_at: {_now_iso()}\n"
            "---\n\n"
        )
        _atomic_write_text(job_dir / relative, frontmatter + (body or ""))

        record = {
            "id": finding_id,
            "kind": kind_norm,
            "title": str(title)[:200],
            "path": relative,
            "confidence": confidence,
            "tags": _slug_tags(tags or []),
            "coverage_id": coverage_id or "",
            "created_at": _now_iso(),
        }
        data.setdefault("findings", []).append(record)
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "finding_added", {"id": finding_id, "kind": kind_norm})
        return record

    def remove_finding(self, job_id: str, finding_id: str) -> bool:
        job_dir, data = self._load_or_raise(job_id)
        findings = data.get("findings") or []
        target = next((f for f in findings if f.get("id") == finding_id), None)
        if target is None:
            return False
        rel = str(target.get("path") or "")
        full = (job_dir / rel).resolve()
        if str(full).startswith(str(job_dir.resolve())) and full.exists():
            try:
                full.unlink()
            except OSError:
                pass
        data["findings"] = [f for f in findings if f.get("id") != finding_id]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "finding_removed", {"id": finding_id})
        return True

    def read_finding(self, job_id: str, finding_id: str) -> dict[str, Any] | None:
        job_dir, data = self._load_or_raise(job_id)
        target = next((f for f in data.get("findings") or [] if f.get("id") == finding_id), None)
        if target is None:
            return None
        rel = str(target.get("path") or "")
        full = (job_dir / rel).resolve()
        if not str(full).startswith(str(job_dir.resolve())) or not full.exists():
            return None
        return {**target, "body": full.read_text(encoding="utf-8")}

    def add_reproduce(
        self,
        job_id: str,
        *,
        name: str,
        content: str,
        lang: str = "python",
        from_finding: str = "",
        verified: bool = False,
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        safe = _slug(name) or _new_sub_id("rep")
        ext = ".py" if lang.lower() == "python" else f".{_slug(lang) or 'txt'}"
        rep_id = _new_sub_id("r")
        relative = f"reproduce/{rep_id}-{safe}{ext}"
        _atomic_write_text(job_dir / relative, content)
        record = {
            "id": rep_id,
            "name": str(name)[:200],
            "lang": str(lang)[:32],
            "path": relative,
            "from_finding": from_finding or "",
            "verified": bool(verified),
            "created_at": _now_iso(),
        }
        data.setdefault("reproduce_scripts", []).append(record)
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "reproduce_added", {"id": rep_id, "lang": record["lang"]})
        return record

    def read_reproduce(self, job_id: str, rep_id: str) -> dict[str, Any] | None:
        job_dir, data = self._load_or_raise(job_id)
        target = next((r for r in data.get("reproduce_scripts") or [] if r.get("id") == rep_id), None)
        if target is None:
            return None
        rel = str(target.get("path") or "")
        full = (job_dir / rel).resolve()
        if not str(full).startswith(str(job_dir.resolve())) or not full.exists():
            return None
        return {**target, "content": full.read_text(encoding="utf-8")}
