"""Domain-agnostic assistant cron: jobs, run history, and in-process scheduler."""

from __future__ import annotations

import json
import hashlib
import inspect
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from croniter import croniter

from argusai.core.paths import PathManager, SUBDIR_ASSISTANT_CRON, validate_session_id

log = logging.getLogger("argusai.assistant.cron")

MAX_RUN_HISTORY = 50
MAX_LIFETIME_RUNS = 1_000_000
MAX_CONTEXT_JSON_BYTES = 32_768
MAX_TRIGGER_EVENT_BYTES = 32_768
MAX_PENDING_EVENTS_PER_JOB = 100
MAX_SEEN_EVENT_IDS_PER_JOB = 200
DEFAULT_DEFER_SECONDS = 3600
MIN_INTERVAL_SECONDS = 1
MAX_INTERVAL_SECONDS = 60 * 60 * 24 * 30
DEFAULT_TICK_SECONDS = 1.0

_CRON_RE = re.compile(
    r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$"
)
_CRON_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()
TIME_SCHEDULE_KINDS = frozenset({"interval", "cron"})
EVENT_SCHEDULE_KINDS = frozenset({"webhook", "file_change"})


def cron_root(data_root: str | Path | None = None) -> Path:
    return PathManager(data_root=data_root).data_root / SUBDIR_ASSISTANT_CRON


def _jobs_path(root: Path) -> Path:
    return root / "jobs.json"


def _runs_dir(root: Path, job_id: str) -> Path:
    jid = validate_cron_job_id(job_id)
    base = (root / "runs").resolve()
    path = (base / jid).resolve()
    if path.parent != base:
        raise ValueError("invalid cron job id")
    return path


def _events_path(root: Path) -> Path:
    return root / "events.json"


def _watch_state_path(root: Path, job_id: str) -> Path:
    jid = validate_cron_job_id(job_id)
    base = (root / "watch_state").resolve()
    path = (base / f"{jid}.json").resolve()
    if path.parent != base:
        raise ValueError("invalid cron job id")
    return path


def _now() -> float:
    return time.time()


def _new_id(prefix: str = "cron") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def validate_cron_job_id(job_id: str) -> str:
    jid = str(job_id or "").strip()
    if not _CRON_JOB_ID_RE.fullmatch(jid) or jid in {".", ".."}:
        raise ValueError("invalid cron job id")
    return jid


def _root_lock(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root.expanduser().resolve()))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


class _ProcessFileLock:
    """Small stdlib cross-process lock used for one data-root writer/runner."""

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self.path = path
        self.blocking = blocking
        self._handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
                fcntl.flock(handle.fileno(), flags)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _clamp_interval(seconds: Any) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = 3600
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, value))


def _clamp_seconds(value: Any, *, default: int, lo: int = 0, hi: int = 3600) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _optional_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_schedule(raw: Any) -> dict[str, Any]:
    data = dict(raw or {}) if isinstance(raw, dict) else {}
    kind = str(data.get("kind") or "interval").strip().lower()
    if kind not in TIME_SCHEDULE_KINDS | EVENT_SCHEDULE_KINDS:
        kind = "interval"
    out: dict[str, Any] = {"kind": kind}
    if kind == "cron":
        expr = str(data.get("expr") or "").strip()
        if not _CRON_RE.fullmatch(expr) or not croniter.is_valid(expr):
            raise ValueError("cron expr must be a 5-field expression (min hour dom month dow)")
        out["expr"] = expr
    elif kind == "interval":
        raw_every = data.get("every_seconds")
        if raw_every is None:
            raw_every = data.get("interval_seconds")
        if raw_every is None:
            raw_every = 3600
        out["every_seconds"] = _clamp_interval(raw_every)
    elif kind == "webhook":
        out["debounce_seconds"] = _clamp_seconds(
            data.get("debounce_seconds"),
            default=0,
        )
    else:
        watch_path = str(data.get("path") or "").strip()
        if not watch_path:
            raise ValueError("file_change schedule path is required")
        out.update({
            "path": watch_path[:2048],
            "glob": str(data.get("glob") or "*").strip()[:256] or "*",
            "recursive": bool(data.get("recursive", True)),
            "poll_seconds": _clamp_seconds(
                data.get("poll_seconds"),
                default=2,
                lo=1,
                hi=3600,
            ),
            "debounce_seconds": _clamp_seconds(
                data.get("debounce_seconds"),
                default=2,
            ),
        })
    return out


def next_run_at(schedule: dict[str, Any], *, after: float | None = None) -> float:
    """Compute the next fire time using full five-field cron semantics."""
    base = float(after if after is not None else _now())
    kind = str(schedule.get("kind") or "interval")
    if kind in EVENT_SCHEDULE_KINDS:
        return 0.0
    if kind != "cron":
        return base + float(schedule.get("every_seconds") or 3600)
    expr = str(schedule.get("expr") or "").strip()
    if not _CRON_RE.fullmatch(expr) or not croniter.is_valid(expr):
        raise ValueError("invalid cron expression")
    local_base = datetime.fromtimestamp(base).astimezone()
    return float(croniter(expr, local_base).get_next(datetime).timestamp())


def normalize_job(raw: Any, *, data_root: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("job must be an object")
    name = str(raw.get("name") or "").strip() or "Scheduled task"
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    destination = str(raw.get("destination") or "new_session").strip().lower()
    if destination not in {"session", "new_session", "dedicated_session"}:
        destination = "new_session"
    session_id = str(raw.get("session_id") or "").strip()
    if destination == "session":
        session_id = validate_session_id(session_id or "default")
    elif session_id:
        session_id = validate_session_id(session_id)
    origin_raw = str(raw.get("origin_session_id") or "").strip()
    origin_session_id = ""
    if origin_raw:
        try:
            origin_session_id = validate_session_id(origin_raw)
        except ValueError:
            origin_session_id = ""
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    notify_raw = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    approval_raw = raw.get("approval") if isinstance(raw.get("approval"), dict) else {}
    context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    try:
        context_encoded = json.dumps(
            context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cron context must be JSON-serializable") from exc
    if len(context_encoded.encode("utf-8")) > MAX_CONTEXT_JSON_BYTES:
        raise ValueError(
            f"cron context exceeds {MAX_CONTEXT_JSON_BYTES} UTF-8 bytes"
        )
    context = json.loads(context_encoded)
    schedule = normalize_schedule(raw.get("schedule"))
    job_id = validate_cron_job_id(str(raw.get("id") or "").strip() or _new_id())
    created_at = float(raw.get("created_at") or _now())
    next_at = raw.get("next_run_at")
    if next_at is None:
        next_at = next_run_at(schedule, after=created_at - 1)
    try:
        max_runs = max(0, min(MAX_LIFETIME_RUNS, int(raw.get("max_runs") or 0)))
    except (TypeError, ValueError):
        max_runs = 0
    try:
        run_count = max(0, min(MAX_LIFETIME_RUNS, int(raw.get("run_count") or 0)))
    except (TypeError, ValueError):
        run_count = 0
    enabled = bool(raw.get("enabled", True))
    disabled_reason = str(raw.get("disabled_reason") or "")[:200]
    if max_runs and run_count >= max_runs:
        enabled = False
        disabled_reason = disabled_reason or "max_runs_reached"
    return {
        "id": job_id,
        "name": name[:120],
        "enabled": enabled,
        "session_id": session_id,
        "origin_session_id": origin_session_id,
        "destination": destination,
        "schedule": schedule,
        "prompt": prompt[:8000],
        "tags": tags[:16],
        "context": context,
        "notify": {
            "on_success": bool(notify_raw.get("on_success", True)),
            "on_change_only": bool(notify_raw.get("on_change_only", True)),
            "on_failure": bool(notify_raw.get("on_failure", True)),
            # Codex Triage-style: post a short result into the creating chat
            # when the run used a separate destination session.
            "report_to_origin": bool(notify_raw.get("report_to_origin", True)),
        },
        "approval": {
            "mode": "unattended_full",
            "high_risk_ack": bool(approval_raw.get("high_risk_ack", False)),
            "granted_at": _optional_timestamp(approval_raw.get("granted_at")),
            "grant_source": str(approval_raw.get("grant_source") or "")[:32],
            "grant_session_id": str(
                approval_raw.get("grant_session_id") or ""
            )[:128],
        },
        "created_at": created_at,
        "next_run_at": float(next_at),
        "last_run_at": float(raw["last_run_at"]) if raw.get("last_run_at") is not None else None,
        "last_status": str(raw.get("last_status") or ""),
        "last_summary": str(raw.get("last_summary") or "")[:2000],
        "pending_fire": bool(raw.get("pending_fire", False)),
        "pending_since": float(raw["pending_since"]) if raw.get("pending_since") is not None else None,
        "defer_limit_seconds": int(raw.get("defer_limit_seconds") or DEFAULT_DEFER_SECONDS),
        "max_runs": max_runs,
        "run_count": run_count,
        "disabled_reason": disabled_reason,
    }


class CronStore:
    """JSON-backed job + run history store under data_root/assistant_cron."""

    def __init__(self, data_root: str | Path | None = None) -> None:
        self.root = cron_root(data_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = _root_lock(self.root)

    @contextmanager
    def _guard(self):
        with self._lock:
            file_lock = _ProcessFileLock(self.root / ".store.lock")
            if not file_lock.acquire():
                raise OSError("could not acquire cron store lock")
            try:
                yield
            finally:
                file_lock.release()

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._guard():
            return [dict(j) for j in self._load_jobs()]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            for job in self._load_jobs():
                if job.get("id") == jid:
                    return dict(job)
        return None

    def upsert_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._guard():
            jobs = self._load_jobs()
            incoming = normalize_job(payload)
            replaced = False
            for idx, job in enumerate(jobs):
                if job.get("id") == incoming["id"]:
                    # Preserve created_at / pending unless explicitly set.
                    incoming["created_at"] = job.get("created_at") or incoming["created_at"]
                    if payload.get("next_run_at") is None and not payload.get("reschedule"):
                        incoming["next_run_at"] = job.get("next_run_at") or incoming["next_run_at"]
                    incoming["last_run_at"] = job.get("last_run_at")
                    incoming["last_status"] = job.get("last_status") or ""
                    incoming["last_summary"] = job.get("last_summary") or ""
                    incoming["pending_fire"] = bool(job.get("pending_fire"))
                    incoming["pending_since"] = job.get("pending_since")
                    incoming["run_count"] = int(job.get("run_count") or 0)
                    if "max_runs" not in payload:
                        incoming["max_runs"] = int(job.get("max_runs") or 0)
                    incoming["disabled_reason"] = str(job.get("disabled_reason") or "")
                    if (
                        int(incoming.get("max_runs") or 0)
                        and int(incoming.get("run_count") or 0) >= int(incoming.get("max_runs") or 0)
                    ):
                        incoming["enabled"] = False
                        incoming["disabled_reason"] = "max_runs_reached"
                    if not incoming.get("origin_session_id") and job.get("origin_session_id"):
                        incoming["origin_session_id"] = job.get("origin_session_id")
                    if "approval" not in payload:
                        incoming["approval"] = dict(job.get("approval") or {})
                    jobs[idx] = incoming
                    replaced = True
                    break
            if not replaced:
                jobs.append(incoming)
            self._save_jobs(jobs)
            return dict(incoming)

    def set_enabled(self, job_id: str, enabled: bool) -> dict[str, Any] | None:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            jobs = self._load_jobs()
            for idx, job in enumerate(jobs):
                if job.get("id") == jid:
                    job = dict(job)
                    job["enabled"] = bool(enabled)
                    if enabled:
                        if (
                            int(job.get("max_runs") or 0)
                            and int(job.get("run_count") or 0) >= int(job.get("max_runs") or 0)
                        ):
                            job["run_count"] = 0
                        job["disabled_reason"] = ""
                        if (
                            str((job.get("schedule") or {}).get("kind") or "interval")
                            in TIME_SCHEDULE_KINDS
                            and float(job.get("next_run_at") or 0) < _now()
                        ):
                            job["next_run_at"] = next_run_at(job["schedule"], after=_now())
                    else:
                        job["disabled_reason"] = job.get("disabled_reason") or "disabled_by_user"
                    jobs[idx] = job
                    self._save_jobs(jobs)
                    return dict(job)
        return None

    def delete_job(self, job_id: str) -> bool:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            jobs = self._load_jobs()
            kept = [j for j in jobs if j.get("id") != jid]
            if len(kept) == len(jobs):
                return False
            self._save_jobs(kept)
            event_state = self._load_event_state()
            pending = event_state.setdefault("pending", {})
            seen = event_state.setdefault("seen", {})
            pending.pop(jid, None)
            seen.pop(jid, None)
            self._save_event_state(event_state)
            watch_state = _watch_state_path(self.root, jid)
            try:
                watch_state.unlink(missing_ok=True)
            except OSError:
                pass
            runs = _runs_dir(self.root, jid)
            if runs.exists():
                for path in runs.glob("*.json"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
        return True

    def append_run(self, job_id: str, record: dict[str, Any]) -> dict[str, Any]:
        jid = validate_cron_job_id(job_id)
        run = dict(record)
        run["run_id"] = validate_cron_job_id(str(run.get("run_id") or "") or _new_id("run"))
        run["job_id"] = jid
        with self._guard():
            path_dir = _runs_dir(self.root, jid)
            path_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(path_dir / f"{run['run_id']}.json", run)
            files = sorted(path_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in files[MAX_RUN_HISTORY:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
            jobs = self._load_jobs()
            for idx, job in enumerate(jobs):
                if job.get("id") == jid:
                    job = dict(job)
                    job["last_run_at"] = float(run.get("finished_at") or run.get("started_at") or _now())
                    job["last_status"] = str(run.get("status") or "")
                    job["last_summary"] = str(run.get("summary") or "")[:2000]
                    status = str(run.get("status") or "")
                    if status not in {"deferred", "skipped_busy", "running"}:
                        job["run_count"] = int(job.get("run_count") or 0) + 1
                    max_runs = int(job.get("max_runs") or 0)
                    if max_runs and int(job.get("run_count") or 0) >= max_runs:
                        job["enabled"] = False
                        job["disabled_reason"] = "max_runs_reached"
                    jobs[idx] = job
                    self._save_jobs(jobs)
                    break
        return run

    def list_runs(self, job_id: str, limit: int = 20) -> list[dict[str, Any]]:
        path_dir = _runs_dir(self.root, validate_cron_job_id(job_id))
        with self._guard():
            if not path_dir.is_dir():
                return []
            lim = max(1, min(100, int(limit or 20)))
            files = sorted(path_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:lim]
            out: list[dict[str, Any]] = []
            for path in files:
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
            return out

    def update_job_fields(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            jobs = self._load_jobs()
            for idx, job in enumerate(jobs):
                if job.get("id") == jid:
                    job = dict(job)
                    job.update(fields)
                    jobs[idx] = job
                    self._save_jobs(jobs)
                    return dict(job)
        return None

    @staticmethod
    def _event_id(value: Any, payload_text: str) -> str:
        raw = str(value or "").strip()
        if raw and _CRON_JOB_ID_RE.fullmatch(raw):
            return raw
        digest_input = raw or payload_text or uuid.uuid4().hex
        return f"evt-{hashlib.sha256(digest_input.encode('utf-8')).hexdigest()[:24]}"

    def enqueue_event(
        self,
        job_id: str,
        *,
        source: str,
        payload: dict[str, Any] | None = None,
        event_id: str = "",
        ready_at: float | None = None,
        coalesce: bool = False,
    ) -> dict[str, Any]:
        """Durably queue an event trigger with replay-safe event ids."""
        jid = validate_cron_job_id(job_id)
        clean_payload = dict(payload or {})
        try:
            encoded = json.dumps(
                clean_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("trigger payload must be JSON-serializable") from exc
        if len(encoded.encode("utf-8")) > MAX_TRIGGER_EVENT_BYTES:
            raise ValueError(
                f"trigger event exceeds {MAX_TRIGGER_EVENT_BYTES} UTF-8 bytes"
            )
        now = _now()
        eid = self._event_id(event_id, encoded)
        with self._guard():
            job = next(
                (item for item in self._load_jobs() if item.get("id") == jid),
                None,
            )
            if job is None:
                raise ValueError("job not found")
            kind = str((job.get("schedule") or {}).get("kind") or "")
            if kind not in EVENT_SCHEDULE_KINDS:
                raise ValueError("job does not use an event trigger")
            if not job.get("enabled"):
                raise ValueError("job is disabled")

            state = self._load_event_state()
            pending = state.setdefault("pending", {})
            seen = state.setdefault("seen", {})
            queue = list(pending.get(jid) or [])
            known = set(str(item) for item in (seen.get(jid) or []))
            known.update(str(item.get("event_id") or "") for item in queue)
            if eid in known:
                return {
                    "ok": True,
                    "duplicate": True,
                    "event_id": eid,
                    "queued": len(queue),
                }
            event = {
                "event_id": eid,
                "source": str(source or kind)[:32],
                "created_at": now,
                "ready_at": float(ready_at if ready_at is not None else now),
                "payload": clean_payload,
            }
            if coalesce and queue and queue[-1].get("source") == event["source"]:
                previous = queue[-1]
                previous_event_id = str(previous.get("event_id") or "")
                if previous_event_id and previous_event_id != eid:
                    recent = [
                        item for item in list(seen.get(jid) or [])
                        if str(item) != previous_event_id
                    ]
                    recent.append(previous_event_id)
                    seen[jid] = recent[-MAX_SEEN_EVENT_IDS_PER_JOB:]
                previous_payload = (
                    dict(previous.get("payload") or {})
                    if isinstance(previous.get("payload"), dict)
                    else {}
                )
                previous_changes = list(previous_payload.get("changes") or [])
                current_changes = list(clean_payload.get("changes") or [])
                if previous_changes or current_changes:
                    merged: list[Any] = []
                    fingerprints: set[str] = set()
                    for item in previous_changes + current_changes:
                        fingerprint = json.dumps(
                            item,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        )
                        if fingerprint in fingerprints:
                            continue
                        fingerprints.add(fingerprint)
                        merged.append(item)
                    clean_payload["changes"] = merged[-100:]
                previous.update({
                    "event_id": eid,
                    "created_at": min(
                        float(previous.get("created_at") or now),
                        now,
                    ),
                    "ready_at": event["ready_at"],
                    "payload": clean_payload,
                })
                event = previous
            else:
                if len(queue) >= MAX_PENDING_EVENTS_PER_JOB:
                    raise OverflowError("trigger queue is full")
                queue.append(event)
            pending[jid] = queue
            self._save_event_state(state)
            return {
                "ok": True,
                "duplicate": False,
                "event_id": eid,
                "queued": len(queue),
                "event": dict(event),
            }

    def peek_event(
        self,
        job_id: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            state = self._load_event_state()
            queue = list((state.get("pending") or {}).get(jid) or [])
            if not queue:
                return None
            event = dict(queue[0])
            if float(event.get("ready_at") or 0) > float(now if now is not None else _now()):
                return None
            return event

    def ack_event(self, job_id: str, event_id: str) -> bool:
        jid = validate_cron_job_id(job_id)
        eid = str(event_id or "")
        if not eid:
            return False
        with self._guard():
            state = self._load_event_state()
            pending = state.setdefault("pending", {})
            seen = state.setdefault("seen", {})
            queue = list(pending.get(jid) or [])
            index = next(
                (
                    idx
                    for idx, item in enumerate(queue)
                    if str(item.get("event_id") or "") == eid
                ),
                -1,
            )
            if index < 0:
                return False
            queue.pop(index)
            if queue:
                pending[jid] = queue
            else:
                pending.pop(jid, None)
            recent = [
                item for item in list(seen.get(jid) or [])
                if str(item) != eid
            ]
            recent.append(eid)
            seen[jid] = recent[-MAX_SEEN_EVENT_IDS_PER_JOB:]
            self._save_event_state(state)
            return True

    def pending_event_count(self, job_id: str) -> int:
        jid = validate_cron_job_id(job_id)
        with self._guard():
            state = self._load_event_state()
            return len(list((state.get("pending") or {}).get(jid) or []))

    def due_jobs(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = float(now if now is not None else _now())
        due: list[dict[str, Any]] = []
        for job in self.list_jobs():
            if not job.get("enabled"):
                continue
            kind = str((job.get("schedule") or {}).get("kind") or "interval")
            if kind in EVENT_SCHEDULE_KINDS:
                event = self.peek_event(str(job.get("id") or ""), now=ts)
                if event is not None:
                    job["_trigger_event"] = event
                    due.append(job)
                continue
            if job.get("pending_fire") or float(job.get("next_run_at") or 0) <= ts:
                due.append(job)
        return due

    def _load_jobs(self) -> list[dict[str, Any]]:
        path = _jobs_path(self.root)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                out.append(normalize_job(item))
            except Exception:
                continue
        return out

    def _save_jobs(self, jobs: list[dict[str, Any]]) -> None:
        path = _jobs_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(path, jobs)

    def _load_event_state(self) -> dict[str, Any]:
        path = _events_path(self.root)
        if not path.exists():
            return {"pending": {}, "seen": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {"pending": {}, "seen": {}}
        if not isinstance(raw, dict):
            return {"pending": {}, "seen": {}}
        pending = raw.get("pending")
        seen = raw.get("seen")
        return {
            "pending": dict(pending) if isinstance(pending, dict) else {},
            "seen": dict(seen) if isinstance(seen, dict) else {},
        }

    def _save_event_state(self, state: dict[str, Any]) -> None:
        self._atomic_write_json(_events_path(self.root), state)

    @staticmethod
    def _atomic_write_json(path: Path, payload: Any) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass


FireCallback = Callable[[dict[str, Any]], dict[str, Any] | None]
BusyPredicate = Callable[..., bool | str | dict[str, Any]]


class CronScheduler:
    """Background ticker that fires due jobs through a host-provided callback."""

    def __init__(
        self,
        store: CronStore,
        *,
        fire: FireCallback,
        is_busy: BusyPredicate | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        webhook_host: str = "127.0.0.1",
        webhook_port: int = 8765,
    ) -> None:
        self.store = store
        self._fire = fire
        self._is_busy = is_busy or (lambda _sid: False)
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fire_lock = threading.Lock()
        self._firing = False
        self._lease: _ProcessFileLock | None = None
        self._webhook_host = str(webhook_host or "127.0.0.1")
        self._webhook_port = max(0, min(65535, int(webhook_port or 8765)))
        self._webhook_server: Any = None
        self._file_watcher: Any = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        lease = _ProcessFileLock(self.store.root / "scheduler.lock", blocking=False)
        if not lease.acquire():
            log.info("cron.scheduler_lease_held root=%s", self.store.root)
            return
        self._lease = lease
        self._stop.clear()
        try:
            from .cron_triggers import CronWebhookServer, FileChangePoller

            self._file_watcher = FileChangePoller(self.store)
            self._webhook_server = CronWebhookServer(
                self.store,
                host=self._webhook_host,
                port=self._webhook_port,
            )
            self._webhook_server.start()
        except Exception:
            log.exception("cron.trigger_services_start_failed")
        self._thread = threading.Thread(target=self._loop, name="argusai-assistant-cron", daemon=True)
        self._thread.start()

    @property
    def is_active(self) -> bool:
        """True when this process holds the scheduler lease and the tick loop is alive."""
        thread = self._thread
        return bool(self._lease is not None and thread is not None and thread.is_alive())

    def stop(self) -> None:
        self._stop.set()
        server = self._webhook_server
        self._webhook_server = None
        if server is not None:
            try:
                server.stop()
            except Exception:
                log.exception("cron.webhook_stop_failed")
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._tick_seconds + 2.0)
        if self._lease is not None and (not thread or not thread.is_alive()):
            self._lease.release()
            self._lease = None

    def tick_once(self) -> list[dict[str, Any]]:
        """Process due jobs once (also used by tests)."""
        results: list[dict[str, Any]] = []
        if not self._fire_lock.acquire(blocking=False):
            return results
        try:
            if self._firing:
                return results
            self._firing = True
            for job in self.store.due_jobs():
                results.append(self._handle_job(job))
                break  # global max 1 cron run at a time
        finally:
            self._firing = False
            self._fire_lock.release()
        return results

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._tick_seconds):
                try:
                    if self._file_watcher is not None:
                        self._file_watcher.poll_once()
                    self.tick_once()
                except Exception:
                    log.exception("cron.tick_failed")
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    def _busy_reason(
        self,
        session_id: str | None,
        job: dict[str, Any] | None = None,
    ) -> str:
        if not session_id:
            return ""
        try:
            signature = inspect.signature(self._is_busy)
            params = list(signature.parameters.values())
            accepts_job = (
                any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params)
                or len([
                    param for param in params
                    if param.kind in {
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    }
                ]) >= 2
            )
        except (TypeError, ValueError):
            accepts_job = False
        if accepts_job:
            raw = self._is_busy(session_id, job)
        else:
            raw = self._is_busy(session_id)
        if isinstance(raw, dict):
            return str(raw.get("reason") or ("busy" if raw.get("busy") else ""))
        if isinstance(raw, str):
            return raw.strip()
        return "running_target" if raw else ""

    def _handle_job(self, job: dict[str, Any]) -> dict[str, Any]:
        jid = str(job.get("id") or "")
        dest = str(job.get("destination") or "new_session")
        session_id = str(job.get("session_id") or "") or None
        busy_session_id = (
            session_id
            if dest in {"session", "dedicated_session"}
            else None
        )
        trigger_event = (
            dict(job.get("_trigger_event") or {})
            if isinstance(job.get("_trigger_event"), dict)
            else None
        )
        trigger_event_id = str((trigger_event or {}).get("event_id") or "")
        schedule_kind = str((job.get("schedule") or {}).get("kind") or "interval")
        now = _now()

        def next_time(after: float) -> float:
            if schedule_kind in EVENT_SCHEDULE_KINDS:
                return 0.0
            return next_run_at(job["schedule"], after=after)

        def ack_trigger() -> None:
            if trigger_event_id:
                self.store.ack_event(jid, trigger_event_id)

        # Cron runs are serialized by ``_fire_lock``. Interactive work in an
        # unrelated chat must not freeze schedules; only the target/dedicated
        # session can block this job.
        busy_reason = self._busy_reason(busy_session_id, job)
        if busy_reason:
            summary = {
                "pending_confirmation": "Waiting for tool confirmation in the target session",
                "pending_user_decision": "Waiting for a user decision in the target session",
                "running_target": "Target assistant session is currently running",
                "scheduler_busy": "Cron scheduler is processing another job",
            }.get(busy_reason, f"Target session blocked ({busy_reason})")
            pending_since = float(job.get("pending_since") or now)
            if not job.get("pending_fire"):
                self.store.update_job_fields(
                    jid,
                    pending_fire=True,
                    pending_since=now,
                    last_status="deferred",
                    last_summary=summary,
                )
                pending_since = now
            else:
                self.store.update_job_fields(
                    jid,
                    last_status="deferred",
                    last_summary=summary,
                )
            defer_limit = int(job.get("defer_limit_seconds") or DEFAULT_DEFER_SECONDS)
            if now - pending_since >= defer_limit:
                run = self.store.append_run(
                    jid,
                    {
                        "started_at": now,
                        "finished_at": now,
                        "status": "skipped_busy",
                        "session_id": busy_session_id or "",
                        "summary": f"Skipped after defer limit: {summary}",
                        "busy_reason": busy_reason,
                        "trigger": trigger_event or {},
                        "notify_sent": False,
                    },
                )
                self.store.update_job_fields(
                    jid,
                    pending_fire=False,
                    pending_since=None,
                    next_run_at=next_time(now),
                    last_status="skipped_busy",
                    last_summary=run.get("summary") or "",
                    last_run_at=now,
                )
                ack_trigger()
                return {"ok": True, "status": "skipped_busy", "run": run}
            return {"ok": True, "status": "deferred", "job_id": jid, "reason": busy_reason}

        started = _now()
        run_stub = {
            "started_at": started,
            "status": "running",
            "session_id": busy_session_id or "",
            "summary": "",
            "trigger": trigger_event or {},
            "notify_sent": False,
        }
        self.store.update_job_fields(
            jid,
            pending_fire=False,
            pending_since=None,
            last_run_at=started,
            last_status="running",
            last_summary="",
        )
        try:
            result = self._fire(job) or {}
        except Exception as exc:
            log.exception("cron.fire_failed job_id=%s", jid)
            run = self.store.append_run(
                jid,
                {
                    **run_stub,
                    "finished_at": _now(),
                    "status": "failed",
                    "error": str(exc),
                    "summary": f"Failed: {exc}",
                },
            )
            self.store.update_job_fields(
                jid,
                pending_fire=False,
                pending_since=None,
                next_run_at=next_time(_now()),
            )
            ack_trigger()
            return {"ok": False, "status": "failed", "run": run}

        status = str(result.get("status") or ("completed" if result.get("ok", True) else "failed"))
        finished = _now()
        run = self.store.append_run(
            jid,
            {
                **run_stub,
                "finished_at": finished,
                "status": status,
                "session_id": str(result.get("session_id") or busy_session_id or ""),
                "assistant_run_job_id": str(result.get("assistant_run_job_id") or ""),
                "summary": str(result.get("summary") or "")[:2000],
                "error": str(result.get("error") or ""),
                "notify_sent": bool(result.get("notify_sent")),
            },
        )
        self.store.update_job_fields(
            jid,
            pending_fire=False,
            pending_since=None,
            next_run_at=next_time(finished),
            session_id=str(result.get("session_id") or busy_session_id or ""),
        )
        ack_trigger()
        return {"ok": status in {"completed", "started"}, "status": status, "run": run}


_SCHEDULER: CronScheduler | None = None
_SCHEDULER_LOCK = threading.Lock()


def get_cron_store(data_root: str | Path | None = None) -> CronStore:
    return CronStore(data_root)


def start_cron_scheduler(
    *,
    data_root: str | Path | None,
    fire: FireCallback,
    is_busy: BusyPredicate | None = None,
    webhook_host: str = "127.0.0.1",
    webhook_port: int = 8765,
) -> CronScheduler:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        store = CronStore(data_root)
        if _SCHEDULER is not None:
            _SCHEDULER.stop()
        _SCHEDULER = CronScheduler(
            store,
            fire=fire,
            is_busy=is_busy,
            webhook_host=webhook_host,
            webhook_port=webhook_port,
        )
        _SCHEDULER.start()
        return _SCHEDULER


def get_cron_scheduler() -> CronScheduler | None:
    return _SCHEDULER
