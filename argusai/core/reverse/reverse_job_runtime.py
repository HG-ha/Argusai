"""Runtime session / cross-link mixin for ``ReverseJobStore``."""

from __future__ import annotations

from typing import Any

from argusai.core.reverse.reverse_constants import RUNTIME_SESSION_KINDS, RUNTIME_SESSION_STATES
from argusai.core.reverse.reverse_job_io import (
    new_sub_id as _new_sub_id,
    norm_dict as _norm_dict,
    now_iso as _now_iso,
)


class ReverseJobRuntimeMixin:
    """Toolchain snapshots, Frida/runtime sessions, evidence/replay links."""

    def record_toolchain_scan(self, job_id: str, scan: dict[str, Any]) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        status = dict(data.get("toolchain_status") or {})
        status["scanned_at"] = _now_iso()
        status["tools"] = dict(scan.get("tools") or {})
        data["toolchain_status"] = status
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "toolchain_scanned", {"available": list(scan.get("available") or [])})
        return status

    def start_runtime_session(
        self,
        job_id: str,
        *,
        kind: str,
        target: str = "",
        tool: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        kind_norm = str(kind or "other").strip().lower()
        if kind_norm not in RUNTIME_SESSION_KINDS:
            kind_norm = "other"
        session_id = _new_sub_id("rt")
        record: dict[str, Any] = {
            "id": session_id,
            "kind": kind_norm,
            "state": "running",
            "target": str(target or "").strip()[:500],
            "tool": str(tool or "").strip()[:128],
            "meta": _norm_dict(meta or {}),
            "started_at": _now_iso(),
            "stopped_at": "",
        }
        sessions = list(data.get("runtime_sessions") or [])
        sessions.append(record)
        data["runtime_sessions"] = sessions[-32:]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "runtime_session_started", {"id": session_id, "kind": kind_norm})
        return record

    def stop_runtime_session(
        self,
        job_id: str,
        session_id: str,
        *,
        state: str = "stopped",
        error: str = "",
    ) -> dict[str, Any] | None:
        job_dir, data = self._load_or_raise(job_id)
        state_norm = str(state or "stopped").strip().lower()
        if state_norm not in RUNTIME_SESSION_STATES:
            state_norm = "stopped"
        sessions = list(data.get("runtime_sessions") or [])
        target = next((s for s in sessions if isinstance(s, dict) and s.get("id") == session_id), None)
        if target is None:
            return None
        target["state"] = state_norm
        target["stopped_at"] = _now_iso()
        if error:
            target["error"] = str(error)[:500]
        data["runtime_sessions"] = sessions
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "runtime_session_stopped", {"id": session_id, "state": state_norm})
        return target

    def link_evidence_ref(
        self,
        job_id: str,
        *,
        ref_type: str,
        ref_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        record = {
            "id": _new_sub_id("ev"),
            "type": str(ref_type or "").strip()[:64],
            "ref_id": str(ref_id or "").strip()[:256],
            "note": str(note or "").strip()[:500],
            "linked_at": _now_iso(),
        }
        refs = list(data.get("evidence_refs") or [])
        refs.append(record)
        data["evidence_refs"] = refs[-64:]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "evidence_linked", {"id": record["id"], "type": record["type"]})
        return record

    def link_replay_ref(
        self,
        job_id: str,
        *,
        ref_type: str,
        ref_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        job_dir, data = self._load_or_raise(job_id)
        record = {
            "id": _new_sub_id("rp"),
            "type": str(ref_type or "").strip()[:64],
            "ref_id": str(ref_id or "").strip()[:256],
            "note": str(note or "").strip()[:500],
            "linked_at": _now_iso(),
        }
        refs = list(data.get("replay_refs") or [])
        refs.append(record)
        data["replay_refs"] = refs[-64:]
        self._write_job(job_dir, data)
        self._emit_event(job_dir, "replay_linked", {"id": record["id"], "type": record["type"]})
        return record
