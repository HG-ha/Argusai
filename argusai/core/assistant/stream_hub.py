"""UI-independent fan-out buffers for assistant stream events."""

from __future__ import annotations

import threading
import time
from collections.abc import MutableMapping
from typing import Any

from .ui_protocol import normalize_stream_event


class StreamHub:
    """Thread-safe in-process event buffers keyed by run id.

    The optional shared mapping keeps the historical GUI ``_assistant_streams``
    attribute working while core callers (channels, cron, ACP) use this typed
    service instead of reaching into pywebview state.
    """

    def __init__(
        self,
        records: MutableMapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self._records = records if records is not None else {}
        self._records_lock = threading.RLock()

    @property
    def records(self) -> MutableMapping[str, dict[str, Any]]:
        return self._records

    def create(
        self,
        run_id: str,
        session_id: str,
        **metadata: Any,
    ) -> dict[str, Any]:
        key = str(run_id or "").strip()
        sid = str(session_id or "").strip()
        if not key or not sid:
            raise ValueError("run_id and session_id are required")
        record = {
            "events": [],
            "done": False,
            "lock": threading.RLock(),
            "session_id": sid,
            "created_at": time.time(),
            **metadata,
        }
        with self._records_lock:
            if key in self._records:
                raise ValueError(f"stream already exists: {key}")
            self._records[key] = record
        return record

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._records_lock:
            return self._records.get(str(run_id or ""))

    def publish(self, run_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        normalized = normalize_stream_event(event)
        record = self.get(run_id)
        if record is None:
            return None
        with record["lock"]:
            record["events"].append(normalized)
        return normalized

    def finish(self, run_id: str) -> bool:
        record = self.get(run_id)
        if record is None:
            return False
        with record["lock"]:
            record["done"] = True
        return True

    def read(self, run_id: str, offset: int = 0) -> dict[str, Any]:
        record = self.get(run_id)
        start = max(0, int(offset or 0))
        if record is None:
            return {
                "ok": False,
                "error": "stream not found",
                "events": [],
                "next": start,
                "done": True,
            }
        with record["lock"]:
            events = list(record["events"][start:])
            next_offset = start + len(events)
            done = bool(record.get("done")) and next_offset >= len(record["events"])
            gc_scheduled = bool(record.get("_gc_scheduled"))
        return {
            "ok": True,
            "events": events,
            "next": next_offset,
            "done": done,
            "gc_scheduled": gc_scheduled,
            "record": record,
        }

    def remove(self, run_id: str) -> dict[str, Any] | None:
        with self._records_lock:
            return self._records.pop(str(run_id or ""), None)
