"""Durable state model for resumable assistant runs.

The store deliberately lives beside an assistant session, but is independent
from ``AssistantSession`` and the GUI.  A job has an atomically replaced JSON
snapshot plus a JSONL event sidecar. Events append durably and the sidecar is
occasionally compacted to a bounded recent tail; sequence numbers remain
monotonic so callers can order retained events without trusting the snapshot.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from argusai.core.paths import validate_session_id

_JOB_LOCKS_GUARD = threading.Lock()
_JOB_LOCKS: dict[str, threading.Lock] = {}


JOB_SCHEMA_VERSION = 1
MAX_EVENT_DATA_BYTES = 32_000
MAX_STATUS_CHARS = 2_000
MAX_PROGRESS_TOTAL = 1_000_000_000
MAX_BUDGET_WALL_SECONDS = 604_800
MAX_BUDGET_COUNT = 1_000_000
CHECKPOINT_THROTTLE_SECONDS = 0.75
MAX_RECENT_EVENTS = 512
EVENT_SIDECAR_COMPACT_BYTES = 2_000_000
EVENT_SIDECAR_TARGET_BYTES = 1_000_000
EVENT_SIDECAR_READ_BYTES = EVENT_SIDECAR_COMPACT_BYTES + MAX_EVENT_DATA_BYTES + 65_536
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AssistantRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


_TERMINAL_STATES = {
    AssistantRunState.CANCELLED,
    AssistantRunState.COMPLETED,
    AssistantRunState.FAILED,
}
_ALLOWED_TRANSITIONS = {
    AssistantRunState.QUEUED: {
        AssistantRunState.RUNNING,
        AssistantRunState.PAUSED,
        AssistantRunState.CANCELLED,
    },
    AssistantRunState.RUNNING: {
        AssistantRunState.WAITING_USER,
        AssistantRunState.PAUSED,
        AssistantRunState.CANCELLED,
        AssistantRunState.COMPLETED,
        AssistantRunState.FAILED,
    },
    AssistantRunState.WAITING_USER: {
        AssistantRunState.RUNNING,
        AssistantRunState.PAUSED,
        AssistantRunState.CANCELLED,
        # A later user turn (confirm / continue / new message) resolves the wait
        # without necessarily resuming this exact job id.
        AssistantRunState.COMPLETED,
    },
    AssistantRunState.PAUSED: {
        AssistantRunState.QUEUED,
        AssistantRunState.RUNNING,
        AssistantRunState.CANCELLED,
    },
    AssistantRunState.CANCELLED: set(),
    AssistantRunState.COMPLETED: set(),
    AssistantRunState.FAILED: set(),
}


def validate_job_id(job_id: str) -> str:
    """Validate a user-visible job id before using it in a filesystem path."""
    value = str(job_id or "").strip()
    if not _JOB_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError("invalid job_id")
    return value


def _now() -> float:
    return time.time()


def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _freeze_event_data(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_event_data(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_event_data(item) for item in value)
    return value


def _thaw_event_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_event_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_event_data(item) for item in value]
    return value


@dataclass(frozen=True)
class AssistantRunEvent:
    """An immutable record emitted for every meaningful job mutation."""

    sequence: int
    timestamp: float
    kind: str
    state: AssistantRunState
    epoch: int
    revision: int
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Frozen dataclasses only prohibit field reassignment.  Freeze nested
        # JSON values too, so an event already emitted to the journal cannot
        # be retroactively edited through its ``data`` mapping.
        object.__setattr__(self, "data", _freeze_event_data(dict(self.data)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "state": self.state.value,
            "epoch": self.epoch,
            "revision": self.revision,
            "data": _thaw_event_data(self.data),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssistantRunEvent":
        return cls(
            sequence=max(1, int(raw.get("sequence") or 1)),
            timestamp=float(raw.get("timestamp") or 0),
            kind=str(raw.get("kind") or "unknown")[:120],
            state=AssistantRunState(str(raw.get("state") or AssistantRunState.QUEUED)),
            epoch=max(0, int(raw.get("epoch") or 0)),
            revision=max(0, int(raw.get("revision") or 0)),
            data=copy.deepcopy(raw.get("data") if isinstance(raw.get("data"), dict) else {}),
        )


@dataclass
class AssistantRunJob:
    """JSON-serializable state for one durable assistant run."""

    session_id: str
    job_id: str
    state: AssistantRunState = AssistantRunState.QUEUED
    epoch: int = 0
    revision: int = 0
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    checkpoint_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    status: str = ""
    progress_current: int = 0
    progress_total: int = 0
    cancellation_requested: bool = False
    cancellation_requested_at: float | None = None
    budget: dict[str, int] = field(default_factory=dict)
    event_count: int = 0
    events: list[AssistantRunEvent] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.session_id = validate_session_id(self.session_id)
        self.job_id = validate_job_id(self.job_id)
        self.state = AssistantRunState(self.state)
        self.epoch = _bounded_int(self.epoch, name="epoch", minimum=0, maximum=MAX_BUDGET_COUNT)
        self.revision = _bounded_int(self.revision, name="revision", minimum=0, maximum=MAX_BUDGET_COUNT)
        self.status = str(self.status or "")[:MAX_STATUS_CHARS]
        self.progress_current = _bounded_int(
            self.progress_current, name="progress_current", minimum=0, maximum=MAX_PROGRESS_TOTAL,
        )
        self.progress_total = _bounded_int(
            self.progress_total, name="progress_total", minimum=0, maximum=MAX_PROGRESS_TOTAL,
        )
        if self.progress_total and self.progress_current > self.progress_total:
            raise ValueError("progress_current cannot exceed progress_total")
        self.budget = normalize_budget(self.budget)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": JOB_SCHEMA_VERSION,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "state": self.state.value,
            "epoch": self.epoch,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "checkpoint_at": self.checkpoint_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "progress": {"current": self.progress_current, "total": self.progress_total},
            "cancellation_requested": self.cancellation_requested,
            "cancellation_requested_at": self.cancellation_requested_at,
            "budget": dict(self.budget),
            "event_count": self.event_count,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AssistantRunJob":
        progress = raw.get("progress") if isinstance(raw.get("progress"), dict) else {}
        return cls(
            session_id=str(raw.get("session_id") or ""),
            job_id=str(raw.get("job_id") or ""),
            state=AssistantRunState(str(raw.get("state") or AssistantRunState.QUEUED)),
            epoch=raw.get("epoch", 0),
            revision=raw.get("revision", 0),
            created_at=float(raw.get("created_at") or _now()),
            updated_at=float(raw.get("updated_at") or _now()),
            checkpoint_at=raw.get("checkpoint_at"),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            status=str(raw.get("status") or ""),
            progress_current=progress.get("current", 0),
            progress_total=progress.get("total", 0),
            cancellation_requested=bool(raw.get("cancellation_requested", False)),
            cancellation_requested_at=raw.get("cancellation_requested_at"),
            budget=raw.get("budget") if isinstance(raw.get("budget"), dict) else {},
            event_count=max(0, int(raw.get("event_count") or 0)),
        )


@dataclass
class _PendingCheckpoint:
    """Latest not-yet-durable progress/status values for one job."""

    status: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    timer: threading.Timer | None = field(default=None, repr=False)


def normalize_budget(raw: Mapping[str, Any] | None) -> dict[str, int]:
    """Return bounded resource limits. Zero explicitly means no configured cap."""
    raw = raw or {}
    if not isinstance(raw, Mapping):
        raise ValueError("budget must be an object")
    limits = {
        "max_wall_seconds": MAX_BUDGET_WALL_SECONDS,
        "max_rounds": MAX_BUDGET_COUNT,
        "max_tool_calls": MAX_BUDGET_COUNT,
        "max_input_tokens": MAX_BUDGET_COUNT,
    }
    return {
        name: _bounded_int(raw.get(name, 0), name=name, minimum=0, maximum=maximum)
        for name, maximum in limits.items()
    }


class AssistantRunJobStore:
    """File-backed store rooted in the configured ``assistant_sessions`` directory."""

    def __init__(self, sessions_dir: str | Path) -> None:
        self.sessions_dir = Path(sessions_dir).expanduser().resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._pending_checkpoints: dict[tuple[str, str], _PendingCheckpoint] = {}

    def create(
        self,
        session_id: str,
        *,
        job_id: str | None = None,
        status: str = "",
        budget: Mapping[str, Any] | None = None,
    ) -> AssistantRunJob:
        job = AssistantRunJob(
            session_id=session_id,
            job_id=job_id or f"run-{uuid.uuid4().hex}",
            status=status,
            budget=normalize_budget(budget),
        )
        with self._lock_for(job.session_id, job.job_id):
            self._record(job, "created", {})
            self._persist(job)
        return job

    def _lock_for(self, session_id: str, job_id: str) -> threading.Lock:
        key = f"{validate_session_id(session_id)}::{validate_job_id(job_id)}"
        with _JOB_LOCKS_GUARD:
            lock = _JOB_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _JOB_LOCKS[key] = lock
            return lock

    def load(
        self,
        session_id: str,
        job_id: str,
        *,
        recover_running: bool = True,
        protect_job_ids: set[str] | None = None,
    ) -> AssistantRunJob:
        safe_session = validate_session_id(session_id)
        safe_job = validate_job_id(job_id)
        protected = {validate_job_id(item) for item in (protect_job_ids or set())}
        with self._lock_for(safe_session, safe_job):
            path = self._job_path(safe_session, safe_job)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid assistant run snapshot: {path.name}") from exc
            if not isinstance(raw, dict):
                raise ValueError("invalid assistant run snapshot")
            job = AssistantRunJob.from_dict(raw)
            if job.session_id != safe_session or job.job_id != safe_job:
                raise ValueError("assistant run identity mismatch")
            job.events = self._read_events(job)
            if job.events:
                job.event_count = max(job.event_count, job.events[-1].sequence)
            should_recover = (
                recover_running
                and job.state is AssistantRunState.RUNNING
                and safe_job not in protected
            )
            if should_recover:
                self._transition(job, AssistantRunState.PAUSED, status="Recovered after an interrupted process")
                self._record(job, "crash_recovered", {"from_state": AssistantRunState.RUNNING.value})
                self._persist(job)
            return job

    def list(
        self,
        session_id: str,
        *,
        recover_running: bool = True,
        protect_job_ids: set[str] | None = None,
    ) -> list[AssistantRunJob]:
        """Return valid jobs for one session, newest snapshot first.

        Job files are durable user data. A malformed or manually edited
        snapshot must not stop the GUI from showing the remaining jobs, and
        filenames are validated before they are passed back into ``load``.
        ``protect_job_ids`` skips crash-recovery for jobs still owned by a live
        in-memory stream in the current process.
        """
        safe_session = validate_session_id(session_id)
        folder = self._job_dir(safe_session)
        jobs: list[AssistantRunJob] = []
        for path in folder.glob("*.json"):
            try:
                job_id = validate_job_id(path.stem)
                job = self.load(
                    safe_session,
                    job_id,
                    recover_running=recover_running,
                    protect_job_ids=protect_job_ids,
                )
            except (OSError, ValueError):
                continue
            jobs.append(job)
        return sorted(jobs, key=lambda job: (job.updated_at, job.created_at), reverse=True)

    def transition(
        self,
        job: AssistantRunJob,
        state: AssistantRunState | str,
        *,
        status: str | None = None,
    ) -> AssistantRunJob:
        with self._lock_for(job.session_id, job.job_id):
            # A terminal/state snapshot must never overtake a coalesced status
            # update. This also cancels the pending timer before mutation.
            self._flush_pending_checkpoint_locked(job)
            self._transition(job, AssistantRunState(state), status=status)
            self._persist(job)
        return job

    def close_waiting_jobs(
        self,
        session_id: str,
        *,
        except_job_id: str | None = None,
        status: str = "Continued in a newer turn",
        protect_job_ids: set[str] | None = None,
        include_paused: bool = True,
    ) -> list[AssistantRunJob]:
        """Resolve leftover waiting/paused jobs so a new turn does not leave orphans.

        Crash recovery turns orphaned RUNNING snapshots into PAUSED. Those must
        also be cleared when the user starts a newer turn; otherwise the UI
        keeps showing "已暂停的恢复任务" forever even though the chat moved on.
        """
        closed: list[AssistantRunJob] = []
        keep = validate_job_id(except_job_id) if except_job_id else None
        protected = set(protect_job_ids or set())
        if keep:
            protected.add(keep)
        # Recover interrupted RUNNING snapshots first so a newer turn does not
        # leave stale "running" files behind after a process crash. Live owners
        # must be passed via protect_job_ids.
        for job in self.list(session_id, recover_running=True, protect_job_ids=protected):
            if keep and job.job_id == keep:
                continue
            if job.job_id in protected:
                continue
            if job.state is AssistantRunState.WAITING_USER:
                self.transition(job, AssistantRunState.COMPLETED, status=status)
                closed.append(job)
            elif include_paused and job.state is AssistantRunState.PAUSED:
                # PAUSED cannot transition to COMPLETED; cancel is the allowed
                # terminal path for crash-recovered orphans the user abandoned.
                self.cancel(job, reason=status or "Superseded by a newer turn")
                closed.append(job)
        return closed

    def checkpoint(
        self,
        job: AssistantRunJob,
        *,
        status: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> AssistantRunJob:
        with self._lock_for(job.session_id, job.job_id):
            if job.is_terminal:
                raise ValueError("cannot checkpoint a terminal job")
            self._queue_checkpoint_locked(
                job,
                status=status,
                progress_current=progress_current,
                progress_total=progress_total,
            )
            self._flush_pending_checkpoint_locked(job)
        return job

    def checkpoint_throttled(
        self,
        job: AssistantRunJob,
        *,
        status: str | None = None,
        progress_current: int | None = None,
        progress_total: int | None = None,
        interval_seconds: float = CHECKPOINT_THROTTLE_SECONDS,
    ) -> AssistantRunJob:
        """Coalesce rapid status/progress updates into one durable checkpoint.

        State transitions and ordinary ``checkpoint`` calls flush these values
        synchronously. Otherwise a daemon timer provides bounded persistence
        latency without rewriting the snapshot and fsyncing the journal for
        every status callback.
        """
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_seconds must be a number") from exc
        if not 0 <= interval <= 60:
            raise ValueError("interval_seconds must be between 0 and 60")
        with self._lock_for(job.session_id, job.job_id):
            if job.is_terminal:
                raise ValueError("cannot checkpoint a terminal job")
            pending = self._queue_checkpoint_locked(
                job,
                status=status,
                progress_current=progress_current,
                progress_total=progress_total,
            )
            if pending is None:
                return job
            if interval == 0:
                self._flush_pending_checkpoint_locked(job)
            elif pending.timer is None:
                key = (job.session_id, job.job_id)
                timer = threading.Timer(
                    interval,
                    self._flush_scheduled_checkpoint,
                    args=(job, key, pending),
                )
                timer.daemon = True
                pending.timer = timer
                timer.start()
        return job

    def flush(self, job: AssistantRunJob) -> AssistantRunJob:
        """Synchronously persist a pending throttled checkpoint, if any."""
        with self._lock_for(job.session_id, job.job_id):
            self._flush_pending_checkpoint_locked(job)
        return job

    def request_cancellation(self, job: AssistantRunJob, *, reason: str = "") -> AssistantRunJob:
        with self._lock_for(job.session_id, job.job_id):
            if job.is_terminal:
                raise ValueError("cannot cancel a terminal job")
            self._flush_pending_checkpoint_locked(job)
            if not job.cancellation_requested:
                job.cancellation_requested = True
                job.cancellation_requested_at = _now()
                job.updated_at = job.checkpoint_at = _now()
                job.revision += 1
                self._record(job, "cancellation_requested", {"reason": str(reason)[:MAX_STATUS_CHARS]})
                self._persist(job)
        return job

    def cancel(self, job: AssistantRunJob, *, reason: str = "") -> AssistantRunJob:
        self.request_cancellation(job, reason=reason)
        return self.transition(job, AssistantRunState.CANCELLED, status=reason or "Cancelled")

    def _queue_checkpoint_locked(
        self,
        job: AssistantRunJob,
        *,
        status: str | None,
        progress_current: int | None,
        progress_total: int | None,
    ) -> _PendingCheckpoint | None:
        key = (job.session_id, job.job_id)
        pending = self._pending_checkpoints.get(key)
        effective_status = pending.status if pending and pending.status is not None else job.status
        effective_current = (
            pending.progress_current
            if pending and pending.progress_current is not None
            else job.progress_current
        )
        effective_total = (
            pending.progress_total
            if pending and pending.progress_total is not None
            else job.progress_total
        )
        next_status = effective_status if status is None else str(status)[:MAX_STATUS_CHARS]
        next_current = (
            effective_current
            if progress_current is None
            else _bounded_int(
                progress_current,
                name="progress_current",
                minimum=0,
                maximum=MAX_PROGRESS_TOTAL,
            )
        )
        next_total = (
            effective_total
            if progress_total is None
            else _bounded_int(
                progress_total,
                name="progress_total",
                minimum=0,
                maximum=MAX_PROGRESS_TOTAL,
            )
        )
        if next_total and next_current > next_total:
            raise ValueError("progress_current cannot exceed progress_total")
        if (
            next_status == effective_status
            and next_current == effective_current
            and next_total == effective_total
        ):
            return pending
        if pending is None:
            pending = _PendingCheckpoint()
            self._pending_checkpoints[key] = pending
        if status is not None:
            pending.status = next_status
        if progress_current is not None:
            pending.progress_current = next_current
        if progress_total is not None:
            pending.progress_total = next_total
        return pending

    def _flush_scheduled_checkpoint(
        self,
        job: AssistantRunJob,
        key: tuple[str, str],
        expected: _PendingCheckpoint,
    ) -> None:
        try:
            with self._lock_for(job.session_id, job.job_id):
                pending = self._pending_checkpoints.get(key)
                if pending is not expected:
                    return
                pending.timer = None
                self._flush_pending_checkpoint_locked(job)
        except Exception:
            # A later segment/state transition will retry the still-pending
            # values. Background persistence failure must not kill the run.
            return

    def _flush_pending_checkpoint_locked(self, job: AssistantRunJob) -> bool:
        key = (job.session_id, job.job_id)
        pending = self._pending_checkpoints.get(key)
        if pending is None:
            return False
        if pending.timer is not None:
            pending.timer.cancel()
            pending.timer = None
        changed = self._apply_checkpoint_locked(job, pending)
        self._pending_checkpoints.pop(key, None)
        return changed

    def _apply_checkpoint_locked(
        self,
        job: AssistantRunJob,
        pending: _PendingCheckpoint,
    ) -> bool:
        if job.is_terminal:
            raise ValueError("cannot checkpoint a terminal job")
        status = job.status if pending.status is None else pending.status
        current = (
            job.progress_current
            if pending.progress_current is None
            else pending.progress_current
        )
        total = job.progress_total if pending.progress_total is None else pending.progress_total
        if (
            status == job.status
            and current == job.progress_current
            and total == job.progress_total
        ):
            return False
        job.status = status
        job.progress_current = current
        job.progress_total = total
        job.updated_at = job.checkpoint_at = _now()
        job.revision += 1
        data: dict[str, Any] = {
            "progress_current": job.progress_current,
            "progress_total": job.progress_total,
        }
        if pending.status is not None:
            data["status"] = job.status
        self._record(job, "checkpoint", data)
        self._persist(job)
        return True

    def _transition(self, job: AssistantRunJob, state: AssistantRunState, *, status: str | None) -> None:
        if state not in _ALLOWED_TRANSITIONS[job.state]:
            raise ValueError(f"invalid assistant run transition: {job.state.value} -> {state.value}")
        old_state = job.state
        if state is AssistantRunState.RUNNING and old_state is not AssistantRunState.RUNNING:
            job.epoch += 1
            job.started_at = job.started_at or _now()
        job.state = state
        if status is not None:
            job.status = str(status)[:MAX_STATUS_CHARS]
        if state in _TERMINAL_STATES:
            job.finished_at = _now()
        job.updated_at = job.checkpoint_at = _now()
        job.revision += 1
        self._record(job, "state_changed", {"from_state": old_state.value, "to_state": state.value})

    def _record(self, job: AssistantRunJob, kind: str, data: Mapping[str, Any]) -> None:
        safe_data = json.loads(json.dumps(dict(data), ensure_ascii=False, default=str))
        if len(json.dumps(safe_data, ensure_ascii=False, default=str).encode("utf-8")) > MAX_EVENT_DATA_BYTES:
            safe_data = {"truncated": True}
        event = AssistantRunEvent(
            sequence=job.event_count + 1,
            timestamp=_now(),
            kind=str(kind)[:120],
            state=job.state,
            epoch=job.epoch,
            revision=job.revision,
            data=safe_data,
        )
        job.events.append(event)
        if len(job.events) > MAX_RECENT_EVENTS:
            del job.events[:-MAX_RECENT_EVENTS]
        job.event_count = event.sequence
        self._append_event(job, event)

    def _job_dir(self, session_id: str) -> Path:
        safe_session = validate_session_id(session_id)
        path = (self.sessions_dir / f"{safe_session}.runs").resolve()
        if path.parent != self.sessions_dir:
            raise ValueError("invalid session_id")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _job_path(self, session_id: str, job_id: str) -> Path:
        folder = self._job_dir(session_id)
        path = (folder / f"{validate_job_id(job_id)}.json").resolve()
        if path.parent != folder:
            raise ValueError("invalid job_id")
        return path

    def _events_path(self, job: AssistantRunJob) -> Path:
        return self._job_path(job.session_id, job.job_id).with_suffix(".events.jsonl")

    def _persist(self, job: AssistantRunJob) -> None:
        path = self._job_path(job.session_id, job.job_id)
        payload = json.dumps(job.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write_text(path, payload)

    def _append_event(self, job: AssistantRunJob, event: AssistantRunEvent) -> None:
        path = self._events_path(job)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            should_compact = path.stat().st_size > EVENT_SIDECAR_COMPACT_BYTES
        except OSError:
            should_compact = False
        if should_compact:
            try:
                self._compact_events(job, path)
            except OSError:
                # The just-appended event is already durable. Compaction is
                # opportunistic and can safely retry after a later event.
                pass

    def _read_events(self, job: AssistantRunJob) -> list[AssistantRunEvent]:
        path = self._events_path(job)
        if not path.exists():
            return []
        try:
            size = path.stat().st_size
            start = max(0, size - EVENT_SIDECAR_READ_BYTES)
            with path.open("rb") as handle:
                if start:
                    handle.seek(start - 1)
                    starts_at_boundary = handle.read(1) == b"\n"
                    raw_tail = handle.read()
                    if not starts_at_boundary:
                        _, separator, raw_tail = raw_tail.partition(b"\n")
                        if not separator:
                            raw_tail = b""
                else:
                    raw_tail = handle.read()
        except OSError:
            return []
        events: deque[AssistantRunEvent] = deque(maxlen=MAX_RECENT_EVENTS)
        last_sequence = 0
        for raw_line in raw_tail.splitlines():
            try:
                raw = json.loads(raw_line.decode("utf-8"))
                if isinstance(raw, dict):
                    event = AssistantRunEvent.from_dict(raw)
                    if event.sequence > last_sequence:
                        events.append(event)
                        last_sequence = event.sequence
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                continue
        return list(events)

    def _compact_events(self, job: AssistantRunJob, path: Path) -> None:
        """Atomically retain the newest bounded portion of the event journal."""
        encoded: list[bytes] = []
        total_bytes = 0
        for event in reversed(job.events[-MAX_RECENT_EVENTS:]):
            line = (
                json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            if encoded and total_bytes + len(line) > EVENT_SIDECAR_TARGET_BYTES:
                break
            encoded.append(line)
            total_bytes += len(line)
        payload = b"".join(reversed(encoded)).decode("utf-8")
        _atomic_write_text(path, payload)


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a snapshot atomically, including a best-effort durable flush."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
