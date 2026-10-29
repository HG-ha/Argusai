"""Hook / oracle mixin for ``ReverseJobStore``."""

from __future__ import annotations

import json
from typing import Any, Iterable

from argusai.core.reverse.reverse_job_io import (
    append_jsonl as _append_jsonl,
    atomic_write_text as _atomic_write_text,
    new_sub_id as _new_sub_id,
    now_iso as _now_iso,
    read_jsonl as _read_jsonl,
    sha256_bytes as _sha256_bytes,
    slug_tags as _slug_tags,
)


class ReverseJobHookMixin:
    """Browser hook definitions, hit streams, and runtime oracle records."""

    def add_hook(
        self,
        job_id: str,
        *,
        target: str,
        kind: str = "function",
        expression: str = "",
        scope: str = "page",
        persisted: bool = True,
        recipe: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        hook_id = _new_sub_id("h")
        record: dict[str, Any] = {
            "id": hook_id,
            "kind": str(kind or "function").strip()[:32],
            "target": str(target or "").strip()[:500],
            "expression": str(expression or "").strip()[:4000],
            "scope": str(scope or "page").strip()[:32],
            "persisted": bool(persisted),
            "recipe": str(recipe or "").strip()[:128],
            "note": str(note or "").strip()[:500],
            "installed_at": _now_iso(),
            "hits_count": 0,
            "hits_dropped": 0,
            "hits_path": f"hooks/{hook_id}.hits.jsonl",
        }
        _atomic_write_text(
            job_dir / "hooks" / f"{hook_id}.json",
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
        )
        data.setdefault("hooks", []).append(record)
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "hook_installed", {"id": hook_id, "target": record["target"]})
        return record

    def remove_hook(self, job_id: str, hook_id: str) -> bool:
        job_dir, data = self._load_or_raise(job_id)
        hooks = data.get("hooks") or []
        target = next((h for h in hooks if h.get("id") == hook_id), None)
        if target is None:
            return False
        for rel in (f"hooks/{hook_id}.json", str(target.get("hits_path") or "")):
            if not rel:
                continue
            p = (job_dir / rel).resolve()
            if str(p).startswith(str(job_dir.resolve())) and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        data["hooks"] = [h for h in hooks if h.get("id") != hook_id]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "hook_removed", {"id": hook_id})
        return True

    def append_hook_hits(
        self,
        job_id: str,
        hook_id: str,
        hits: Iterable[dict[str, Any]],
        *,
        dropped: int = 0,
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        hook = next((h for h in data.get("hooks") or [] if h.get("id") == hook_id), None)
        if hook is None:
            raise FileNotFoundError(f"hook not found: {hook_id}")
        path = job_dir / (hook.get("hits_path") or f"hooks/{hook_id}.hits.jsonl")
        added = 0
        ts = _now_iso()
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            record = {"ts": hit.get("ts") or ts, **hit}
            _append_jsonl(path, record)
            added += 1
        hook["hits_count"] = int(hook.get("hits_count") or 0) + added
        if dropped:
            hook["hits_dropped"] = int(hook.get("hits_dropped") or 0) + int(dropped)
        self._write_job(job_dir, data)
        if added:
            self._emit_event(
                job_dir,
                "hook_hit_drained",
                {"id": hook_id, "added": added, "dropped": int(dropped or 0)},
            )
        return {"added": added, "total": hook["hits_count"], "dropped": hook.get("hits_dropped", 0)}

    def read_hook_hits(
        self,
        job_id: str,
        hook_id: str,
        *,
        tail: int = 200,
    ) -> list[dict[str, Any]]:
        job_dir, data = self._load_or_raise(job_id)
        hook = next((h for h in data.get("hooks") or [] if h.get("id") == hook_id), None)
        if hook is None:
            return []
        path = (job_dir / (hook.get("hits_path") or "")).resolve()
        if not str(path).startswith(str(job_dir.resolve())):
            return []
        return _read_jsonl(path, tail=tail)

    def add_oracle(
        self,
        job_id: str,
        *,
        records: Iterable[dict[str, Any]],
        stage: str = "",
        source: str = "",
        runtime: str = "",
        tags: list[Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Persist runtime input/output evidence for later reproduction checks."""
        job_dir, data = self._load_or_raise(job_id)
        self._ensure_subdirs(job_dir)
        oracle_id = _new_sub_id("orc")
        rel = f"oracles/{oracle_id}.jsonl"
        path = job_dir / rel
        tag_list = _slug_tags(tags or [])
        added = 0
        for item in records:
            if not isinstance(item, dict):
                continue
            rec = {
                "oracle_id": oracle_id,
                "ts": item.get("ts") or _now_iso(),
                "kind": item.get("kind") or "sample",
                "stage": item.get("stage") or stage,
                "source": item.get("source") or source,
                "runtime": item.get("runtime") or runtime,
                "tags": item.get("tags") or tag_list,
                **item,
            }
            _append_jsonl(path, rec)
            added += 1
        digest = _sha256_bytes(path.read_bytes()) if path.exists() else ""
        record: dict[str, Any] = {
            "id": oracle_id,
            "path": rel,
            "count": added,
            "stage": str(stage or "")[:128],
            "source": str(source or "")[:500],
            "runtime": str(runtime or "")[:128],
            "tags": tag_list,
            "note": str(note or "")[:500],
            "sha256": digest,
            "created_at": _now_iso(),
        }
        data.setdefault("oracles", []).append(record)
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "oracle_added", {"id": oracle_id, "count": added, "stage": record["stage"]})
        return record

    def list_oracles(self, job_id: str) -> list[dict[str, Any]]:
        _job_dir, data = self._load_or_raise(job_id)
        return list(data.get("oracles") or [])

    def read_oracle(
        self,
        job_id: str,
        oracle_id: str = "",
        *,
        tail: int = 200,
        stage: str = "",
        kind: str = "",
    ) -> list[dict[str, Any]]:
        job_dir, data = self._load_or_raise(job_id)
        candidates = data.get("oracles") or []
        if oracle_id:
            candidates = [o for o in candidates if o.get("id") == oracle_id]
        out: list[dict[str, Any]] = []
        for oracle in candidates:
            path = (job_dir / str(oracle.get("path") or "")).resolve()
            if not str(path).startswith(str(job_dir.resolve())):
                continue
            for rec in _read_jsonl(path, tail=None):
                if stage and str(rec.get("stage") or "") != str(stage):
                    continue
                if kind and str(rec.get("kind") or "") != str(kind):
                    continue
                out.append(rec)
        if tail and len(out) > tail:
            out = out[-tail:]
        return out

    def compare_oracle_records(
        self,
        job_id: str,
        oracle_id: str = "",
        *,
        actual_field: str = "actual",
        expected_field: str = "expected",
        tail: int = 200,
    ) -> dict[str, Any]:
        rows = self.read_oracle(job_id, oracle_id, tail=tail)
        compared = 0
        matched = 0
        mismatches: list[dict[str, Any]] = []
        for rec in rows:
            if actual_field not in rec or expected_field not in rec:
                continue
            compared += 1
            if rec.get(actual_field) == rec.get(expected_field):
                matched += 1
            elif len(mismatches) < 20:
                mismatches.append({
                    "id": rec.get("id") or rec.get("ts"),
                    "stage": rec.get("stage"),
                    "actual": rec.get(actual_field),
                    "expected": rec.get(expected_field),
                })
        return {
            "ok": True,
            "compared": compared,
            "matched": matched,
            "mismatched": compared - matched,
            "all_matched": compared > 0 and matched == compared,
            "mismatches": mismatches,
        }
