"""Durable background tool completion (Loom22 v2.4 / §8 / §16.7).

Tools declared with ``completion=background`` return an immediate ``job_ref``
receipt that completes the tool_call pairing. The real work runs off the
agent loop; when finished the result is stored under the job_ref and can be
fetched without forging a second tool_result for the original call id.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("argusai.assistant.background")

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="argusai-bg")


class BackgroundJobStore:
    """In-memory job ledger for one AssistantSession."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        runner: Callable[[], Any],
        turn_id: str = "",
        cancel: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:16]
        job_ref = f"job://{job_id}"
        record = {
            "job_id": job_id,
            "job_ref": job_ref,
            "name": name,
            "arguments": dict(arguments or {}),
            "status": "running",
            "ok": True,
            "result": None,
            "error": None,
            "turn_id": turn_id,
            "started_at": time.time(),
            "finished_at": None,
            "cancel_requested": False,
            "_cancel": cancel,
        }
        with self._lock:
            self._jobs[job_ref] = record

        def _run() -> None:
            try:
                result = runner()
                with self._lock:
                    rec = self._jobs.get(job_ref)
                    if not rec:
                        return
                    was_cancelled = bool(rec.get("cancel_requested"))
                    rec["status"] = "cancelled" if was_cancelled else "done"
                    rec["result"] = result
                    rec["ok"] = (
                        not was_cancelled
                        and not (
                            isinstance(result, dict)
                            and (result.get("error") or result.get("ok") is False)
                        )
                    )
                    rec["finished_at"] = time.time()
            except Exception as exc:
                log.exception("background job %s failed", job_ref)
                with self._lock:
                    rec = self._jobs.get(job_ref)
                    if not rec:
                        return
                    was_cancelled = bool(rec.get("cancel_requested"))
                    rec["status"] = "cancelled" if was_cancelled else "failed"
                    rec["ok"] = False
                    rec["error"] = str(exc)
                    rec["result"] = {"ok": False, "error": str(exc)}
                    rec["finished_at"] = time.time()

        _EXECUTOR.submit(_run)
        return {
            "ok": True,
            "mode": "async",
            "status": "running",
            "job_ref": job_ref,
            "name": name,
            "note": (
                "Background job started. The original tool_call is complete with this receipt. "
                "Use `background_job_status(job_ref=...)` later to read the final result — "
                "do not busy-poll in a tight loop."
            ),
        }

    def status(self, job_ref: str) -> dict[str, Any]:
        with self._lock:
            rec = self._jobs.get(str(job_ref or ""))
            if not rec:
                return {"ok": False, "error": "job not found", "job_ref": job_ref}
            out = {
                "ok": True,
                "job_ref": rec["job_ref"],
                "name": rec["name"],
                "status": rec["status"],
                "started_at": rec["started_at"],
                "finished_at": rec["finished_at"],
            }
            if rec["status"] in {"done", "failed", "cancelled"}:
                out["result"] = rec.get("result")
                if rec.get("error"):
                    out["error"] = rec["error"]
            return out

    def running_count(self, *, name: str = "") -> int:
        name_filter = str(name or "")
        with self._lock:
            return sum(
                1
                for rec in self._jobs.values()
                if rec.get("status") in {"running", "cancelling"}
                and (not name_filter or rec.get("name") == name_filter)
            )

    def cancel(self, job_ref: str) -> dict[str, Any]:
        callback = None
        with self._lock:
            rec = self._jobs.get(str(job_ref or ""))
            if not rec:
                return {"ok": False, "error": "job not found", "job_ref": job_ref}
            if rec.get("status") in {"done", "failed", "cancelled"}:
                return {
                    "ok": True,
                    "job_ref": rec["job_ref"],
                    "status": rec["status"],
                    "already_terminal": True,
                }
            callback = rec.get("_cancel")
            if not callable(callback):
                return {
                    "ok": False,
                    "error": "background job does not support cancellation",
                    "job_ref": rec["job_ref"],
                    "status": rec["status"],
                }
            rec["cancel_requested"] = True
            rec["status"] = "cancelling"
        try:
            callback()
        except Exception as exc:
            log.exception("background job %s cancellation failed", job_ref)
            return {
                "ok": False,
                "error": str(exc),
                "job_ref": job_ref,
                "status": "cancelling",
            }
        return {
            "ok": True,
            "job_ref": job_ref,
            "status": "cancelling",
        }

    def list_jobs(self, *, limit: int = 20) -> dict[str, Any]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda r: float(r.get("started_at") or 0), reverse=True)
            slim = [
                {
                    "job_ref": r["job_ref"],
                    "name": r["name"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                }
                for r in items[: max(1, min(int(limit or 20), 100))]
            ]
        return {"ok": True, "count": len(slim), "jobs": slim}

    def shutdown(self) -> None:
        """Cancel every cancellable background job owned by this session."""
        callbacks: list[Callable[[], Any]] = []
        with self._lock:
            for record in self._jobs.values():
                if record.get("status") not in {"running", "cancelling"}:
                    continue
                record["cancel_requested"] = True
                record["status"] = "cancelling"
                callback = record.get("_cancel")
                if callable(callback):
                    callbacks.append(callback)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.debug("background job shutdown cancellation failed", exc_info=True)


def shutdown_background_executor() -> None:
    """Release background tool worker threads during process shutdown."""
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)
