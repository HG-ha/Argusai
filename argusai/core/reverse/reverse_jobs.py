"""Reverse-engineering job store.

A ``ReverseJob`` represents one reverse-engineering effort against one
target (e.g. "ACME order API signature"). Each job is a folder under
``data_root/reverse_jobs/`` and owns:

* ``job.json`` — metadata, status, stack detection, indexes
* ``artifacts/`` — raw fetched bundles, beautified/deobfuscated outputs
* ``hooks/`` — hook definitions and collected hits (``.jsonl``)
* ``findings/`` — Markdown conclusions written by the AI / user
* ``reproduce/`` — generated Python re-implementations
* ``events.jsonl`` — append-only audit log of every state change

Design choices:

* Job folder uses the *id* as its name (``rev-YYYYMMDD-xxxx``) so the
  filesystem layout matches the user-visible identifier.
* ``job.json`` is the **only** source of truth for indexes. Hits and
  events stream into ``.jsonl`` files; the index just stores a counter
  + the most recent N entries so the panel can render without loading
  multi-megabyte logs.
* Atomic writes: every write goes via ``.tmp`` + ``os.replace``.
* Soft delete: trashing a job moves the folder into ``_trash/``.

Module surface mirrors ``knowledge.py`` so the assistant tools layer
can wire the two stores the same way.
"""

from __future__ import annotations

import json
import logging
import shutil
import time as _time
from pathlib import Path
from typing import Any

from argusai.core.reverse.reverse_constants import (
    EVENT_KINDS,
)
from argusai.core.reverse.reverse_job_artifacts import ReverseJobArtifactMixin
from argusai.core.reverse.reverse_job_findings import ReverseJobFindingMixin
from argusai.core.reverse.reverse_job_hooks import ReverseJobHookMixin
from argusai.core.reverse.reverse_job_runtime import ReverseJobRuntimeMixin
from argusai.core.reverse.reverse_job_io import (
    JOB_ID_RE,
    append_jsonl as _append_jsonl,
    atomic_write_text as _atomic_write_text,
    default_job_runtime_fields as _default_job_runtime_fields,
    dir_size as _dir_size,
    new_job_id as _new_job_id,
    norm_dict as _norm_dict,
    norm_platform as _norm_platform,
    norm_status as _norm_status,
    norm_target_type as _norm_target_type,
    now_iso as _now_iso,
    read_jsonl as _read_jsonl,
    slug_tags as _slug_tags,
    str_list as _str_list,
)
from argusai.core.reverse.reverse_job_migrate import migrate_job_data

log = logging.getLogger("argusai.reverse_jobs")

# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op. Lets the reverse workbench refresh as jobs,
# findings, artifacts, hooks and runtime sessions progress in the background.
from typing import Callable as _Callable

_UI_NOTIFY: "_Callable[[str, dict[str, Any]], None] | None" = None


def set_ui_notify(callback: "_Callable[[str, dict[str, Any]], None] | None") -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


class ReverseJobStore(
    ReverseJobArtifactMixin,
    ReverseJobHookMixin,
    ReverseJobFindingMixin,
    ReverseJobRuntimeMixin,
):
    """File-backed reverse-engineering job store.

    One instance per `data_root/reverse_jobs/` directory. The store keeps an
    in-memory summary index loaded from each ``job.json`` so listing is
    cheap. Writes go through helper methods that update both disk and the
    in-memory index.
    """

    TRASH_DIR = "_trash"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self.TRASH_DIR).mkdir(parents=True, exist_ok=True)
        self._index: dict[str, dict[str, Any]] = {}
        self._scan()

    def _scan(self) -> None:
        self._index = {}
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            if child.name == self.TRASH_DIR:
                continue
            if not JOB_ID_RE.match(child.name):
                continue
            data = self._read_job_json(child)
            if data is None:
                continue
            self._index[child.name] = self._summary(data)

    def _read_job_json(self, folder: Path, *, persist_migration: bool = True) -> dict[str, Any] | None:
        p = folder / "job.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            migrated, changed = migrate_job_data(data)
            if changed and persist_migration:
                _atomic_write_text(
                    p,
                    json.dumps(migrated, ensure_ascii=False, indent=2, default=str),
                )
                if migrated.get("id"):
                    self._emit_event(folder, "migrated", {"schema_version": migrated.get("schema_version")})
            return migrated
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _summary(data: dict[str, Any]) -> dict[str, Any]:
        runtime_sessions = data.get("runtime_sessions") or []
        active_runtime = sum(
            1 for s in runtime_sessions
            if isinstance(s, dict) and str(s.get("state") or "") == "running"
        )
        return {
            "id": data.get("id"),
            "title": data.get("title") or "(untitled)",
            "target_url": data.get("target_url") or "",
            "scope": data.get("scope") or "",
            "status": data.get("status") or "active",
            "tags": list(data.get("tags") or []),
            "stack": dict(data.get("stack") or {}),
            "target_type": data.get("target_type") or "unknown",
            "platform": data.get("platform") or "unknown",
            "artifact_count": len(data.get("artifacts") or []),
            "hook_count": len(data.get("hooks") or []),
            "oracle_count": len(data.get("oracles") or []),
            "finding_count": len(data.get("findings") or []),
            "reproduce_count": len(data.get("reproduce_scripts") or []),
            "runtime_session_count": len(runtime_sessions),
            "active_runtime_sessions": active_runtime,
            "evidence_ref_count": len(data.get("evidence_refs") or []),
            "replay_ref_count": len(data.get("replay_refs") or []),
            "created_at": data.get("created_at") or "",
            "updated_at": data.get("updated_at") or data.get("created_at") or "",
            "linked_task_id": data.get("linked_task_id") or "",
            "linked_kb_ids": list(data.get("linked_kb_ids") or []),
        }

    # -- helpers -----------------------------------------------------------

    def _validate_id(self, job_id: str) -> str:
        jid = str(job_id or "").strip()
        if not JOB_ID_RE.match(jid):
            raise ValueError(f"invalid reverse job id: {job_id!r}")
        return jid

    def _job_dir(self, job_id: str) -> Path:
        jid = self._validate_id(job_id)
        return self.root / jid

    def _trash_dir_for(self, job_id: str) -> Path:
        jid = self._validate_id(job_id)
        return self.root / self.TRASH_DIR / jid

    def _ensure_subdirs(self, job_dir: Path) -> None:
        for sub in ("artifacts", "artifacts/webpack-modules", "hooks", "oracles", "findings", "reproduce"):
            (job_dir / sub).mkdir(parents=True, exist_ok=True)

    def _write_job(self, job_dir: Path, data: dict[str, Any]) -> None:
        data["updated_at"] = _now_iso()
        _atomic_write_text(job_dir / "job.json", json.dumps(data, ensure_ascii=False, indent=2, default=str))
        self._index[data["id"]] = self._summary(data)

    def _emit_event(self, job_dir: Path, kind: str, payload: dict[str, Any] | None = None) -> None:
        if kind not in EVENT_KINDS:
            kind = "note"
        _append_jsonl(
            job_dir / "events.jsonl",
            {"ts": _now_iso(), "kind": kind, "payload": payload or {}},
        )
        cb = _UI_NOTIFY
        if cb is not None:
            try:
                cb("reverse", {"job_id": job_dir.name, "kind": kind})
            except Exception:
                pass

    def _load_or_raise(self, job_id: str) -> tuple[Path, dict[str, Any]]:
        job_dir = self._job_dir(job_id)
        data = self._read_job_json(job_dir)
        if data is None:
            raise FileNotFoundError(f"reverse job not found: {job_id}")
        return job_dir, data

    # -- public CRUD -------------------------------------------------------

    def list(
        self,
        *,
        scope: str = "",
        status: str = "",
        include_trashed: bool = False,
    ) -> list[dict[str, Any]]:
        scope_q = (scope or "").strip().lower()
        status_q = (status or "").strip().lower()
        out: list[dict[str, Any]] = []
        for jid, summary in self._index.items():
            if scope_q and scope_q not in (summary.get("scope") or "").lower():
                continue
            if status_q and (summary.get("status") or "") != status_q:
                continue
            out.append({**summary, "id": jid})
        if include_trashed:
            for child in (self.root / self.TRASH_DIR).iterdir():
                if not child.is_dir():
                    continue
                data = self._read_job_json(child)
                if data is None:
                    continue
                summary = self._summary(data)
                summary["trashed"] = True
                out.append({**summary, "id": data.get("id") or child.name})
        out.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        return out

    def create(
        self,
        *,
        title: str,
        target_url: str = "",
        scope: str = "",
        tags: list[Any] | None = None,
        stack: dict[str, Any] | None = None,
        intent: str = "",
        linked_task_id: str = "",
        target_type: str = "",
        platform: str = "",
        target_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not (title or "").strip():
            raise ValueError("title is required")
        job_id = _new_job_id()
        # Collisions are extremely unlikely but loop just in case.
        while job_id in self._index or (self.root / job_id).exists():
            job_id = _new_job_id()
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_subdirs(job_dir)

        now = _now_iso()
        tt = _norm_target_type(target_type)
        if tt == "unknown" and target_url:
            tt = "web_js"
        plat = _norm_platform(platform)
        if plat == "unknown" and tt == "web_js":
            plat = "web"
        data: dict[str, Any] = {
            "id": job_id,
            "title": str(title).strip()[:200],
            "target_url": str(target_url or "").strip()[:2048],
            "scope": str(scope or "").strip()[:200],
            "status": "active",
            "intent": str(intent or "").strip()[:2000],
            "tags": _slug_tags(tags or []),
            "stack": {
                "framework": str((stack or {}).get("framework") or "").strip()[:64],
                "bundler": str((stack or {}).get("bundler") or "").strip()[:64],
                "obfuscation": str((stack or {}).get("obfuscation") or "").strip()[:128],
                "antibot": _str_list((stack or {}).get("antibot") or [], max_items=8, max_len=64),
            },
            **_default_job_runtime_fields(
                target_type=tt,
                platform=plat,
                target_meta=target_meta,
            ),
            "artifacts": [],
            "hooks": [],
            "oracles": [],
            "findings": [],
            "reproduce_scripts": [],
            "linked_task_id": str(linked_task_id or "").strip()[:128],
            "linked_kb_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "created", {"title": data["title"]})
        return data

    def get(self, job_id: str, *, include_trashed: bool = False) -> dict[str, Any] | None:
        try:
            jid = self._validate_id(job_id)
        except ValueError:
            return None
        job_dir = self.root / jid
        if not job_dir.exists() and include_trashed:
            job_dir = self.root / self.TRASH_DIR / jid
            if not job_dir.exists():
                return None
        data = self._read_job_json(job_dir)
        if data is None:
            return None
        data["_dir"] = str(job_dir)
        data["_trashed"] = job_dir.parent.name == self.TRASH_DIR
        return data

    def update(
        self,
        job_id: str,
        *,
        title: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        tags: list[Any] | None = None,
        stack: dict[str, Any] | None = None,
        intent: str | None = None,
        linked_task_id: str | None = None,
        linked_kb_ids: list[Any] | None = None,
        target_type: str | None = None,
        platform: str | None = None,
        target_meta: dict[str, Any] | None = None,
        toolchain_status: dict[str, Any] | None = None,
        analysis_summary: dict[str, Any] | None = None,
        risk_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        changes: dict[str, Any] = {}
        if title is not None:
            data["title"] = str(title).strip()[:200] or data["title"]
            changes["title"] = data["title"]
        if scope is not None:
            data["scope"] = str(scope).strip()[:200]
            changes["scope"] = data["scope"]
        if status is not None:
            new_status = _norm_status(status)
            if new_status != data.get("status"):
                data["status"] = new_status
                changes["status"] = new_status
                self._emit_event(job_dir, "status_changed", {"to": new_status})
        if tags is not None:
            new_tags = _slug_tags(tags)
            if new_tags != data.get("tags"):
                data["tags"] = new_tags
                self._emit_event(job_dir, "tag_changed", {"tags": new_tags})
        if stack is not None:
            current = dict(data.get("stack") or {})
            for k in ("framework", "bundler", "obfuscation"):
                if k in stack:
                    current[k] = str(stack[k] or "").strip()[:128]
            if "antibot" in stack:
                current["antibot"] = _str_list(stack["antibot"] or [], max_items=8, max_len=64)
            data["stack"] = current
            changes["stack"] = current
        if intent is not None:
            data["intent"] = str(intent).strip()[:2000]
        if linked_task_id is not None:
            data["linked_task_id"] = str(linked_task_id or "").strip()[:128]
        if linked_kb_ids is not None:
            data["linked_kb_ids"] = _str_list(linked_kb_ids, max_items=32, max_len=64)
        if target_type is not None:
            data["target_type"] = _norm_target_type(target_type)
            changes["target_type"] = data["target_type"]
        if platform is not None:
            data["platform"] = _norm_platform(platform)
            changes["platform"] = data["platform"]
        if target_meta is not None:
            merged = dict(data.get("target_meta") or {})
            merged.update(_norm_dict(target_meta))
            data["target_meta"] = merged
            changes["target_meta"] = True
        if toolchain_status is not None:
            data["toolchain_status"] = _norm_dict(toolchain_status, max_keys=128)
            changes["toolchain_status"] = True
        if analysis_summary is not None and isinstance(analysis_summary, dict):
            data["analysis_summary"] = analysis_summary
            changes["analysis_summary"] = True
        if risk_summary is not None:
            data["risk_summary"] = _norm_dict(risk_summary, max_keys=32)
            changes["risk_summary"] = True
        self._write_job(job_dir, data)
        if changes:
            self._emit_event(job_dir, "updated", {"changes": list(changes.keys())})
        return data

    def trash(self, job_id: str) -> bool:
        try:
            jid = self._validate_id(job_id)
        except ValueError:
            return False
        src = self.root / jid
        if not src.exists():
            return False
        dst = self.root / self.TRASH_DIR / jid
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.move(str(src), str(dst))
        self._index.pop(jid, None)
        return True

    def restore(self, job_id: str) -> bool:
        try:
            jid = self._validate_id(job_id)
        except ValueError:
            return False
        src = self.root / self.TRASH_DIR / jid
        if not src.exists():
            return False
        dst = self.root / jid
        if dst.exists():
            return False
        shutil.move(str(src), str(dst))
        data = self._read_job_json(dst)
        if data is not None:
            self._index[jid] = self._summary(data)
        return True

    def hard_delete(self, job_id: str) -> bool:
        try:
            jid = self._validate_id(job_id)
        except ValueError:
            return False
        removed = False
        for base in (self.root / jid, self.root / self.TRASH_DIR / jid):
            if base.exists():
                shutil.rmtree(base, ignore_errors=True)
                removed = True
        self._index.pop(jid, None)
        return removed

    # -- events ------------------------------------------------------------

    def list_events(self, job_id: str, *, tail: int = 200) -> list[dict[str, Any]]:
        job_dir, _ = self._load_or_raise(job_id)
        return _read_jsonl(job_dir / "events.jsonl", tail=tail)

    # -- trash housekeeping ------------------------------------------------

    def list_trash(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        trash = self.root / self.TRASH_DIR
        if not trash.exists():
            return out
        for child in trash.iterdir():
            if not child.is_dir():
                continue
            data = self._read_job_json(child)
            if data is None:
                continue
            out.append({**self._summary(data), "id": data.get("id") or child.name, "trashed": True})
        out.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        return out

    def purge_trash(self, *, older_than_days: int | None = None) -> int:
        trash = self.root / self.TRASH_DIR
        if not trash.exists():
            return 0
        removed = 0
        threshold = None
        if older_than_days and older_than_days > 0:
            threshold = _time.time() - (older_than_days * 86400)
        for child in trash.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if threshold is not None and mtime >= threshold:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        return removed

    # -- size accounting ---------------------------------------------------

    def job_size_bytes(self, job_id: str) -> int:
        try:
            jid = self._validate_id(job_id)
        except ValueError:
            return 0
        for base in (self.root / jid, self.root / self.TRASH_DIR / jid):
            if base.exists():
                return _dir_size(base)
        return 0
