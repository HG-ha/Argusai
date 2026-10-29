"""Task management: each scan is an independently trackable Task.

Design highlights:

* A :class:`Task` owns its own :class:`~argusai.core.events.EventBus` and a
  persistent directory ``tasks/<id>/`` containing ``meta.json``,
  ``findings.jsonl``, ``events.jsonl`` and the rendered reports.
* :class:`TaskManager` spawns each running task in its own daemon thread which
  in turn drives an independent ``asyncio`` event loop. Multiple tasks run
  truly in parallel (I/O bound so GIL is fine).
* Completed tasks are rehydrated from disk on manager start, so the GUI can
  show history across process restarts.

The TaskManager is intentionally decoupled from pywebview so the CLI can
use the same model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from argusai.core.events import Event, EventBus
from argusai.core.finding import Finding, Severity
from argusai.core.target import AuthConfig, Target

log = logging.getLogger("argusai.tasks")


TaskStatus = str  # "pending" | "running" | "done" | "error" | "cancelled"


@dataclass
class TaskMeta:
    """Serialisable task metadata persisted to ``tasks/<id>/meta.json``."""

    id: str
    name: str
    target_url: str
    status: TaskStatus = "pending"
    checks: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    severity: dict[str, int] = field(
        default_factory=lambda: {s.value: 0 for s in Severity}
    )
    total: int = 0
    # Coarse progress, driven by check start/done events.
    progress: float = 0.0
    checks_total: int = 0
    checks_done: int = 0
    current_check: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskMeta":
        return cls(**d)


# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op. Lets the task list / detail views refresh the
# instant a scan changes state instead of polling.
_UI_NOTIFY: Callable[[str, dict[str, Any]], None] | None = None


def set_ui_notify(callback: "Callable[[str, dict[str, Any]], None] | None") -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


def _notify_tasks_ui(task_id: str) -> None:
    cb = _UI_NOTIFY
    if cb is None:
        return
    try:
        cb("tasks", {"task_id": str(task_id or "")})
    except Exception:
        pass


class Task:
    """One scan instance, addressable by id, backed by a directory on disk."""

    def __init__(self, root: Path, meta: TaskMeta) -> None:
        self.root = root
        self.meta = meta
        self.bus = EventBus()
        self.findings: list[Finding] = []
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_event = threading.Event()
        self._persist_event_unsub: Callable[[], None] | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def status(self) -> TaskStatus:
        return self.meta.status

    # -- Disk layout -----------------------------------------------------
    @property
    def meta_path(self) -> Path:
        return self.root / "meta.json"

    @property
    def findings_path(self) -> Path:
        return self.root / "findings.jsonl"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    def save_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_findings(self) -> list[Finding]:
        if not self.findings_path.exists():
            return []
        out: list[Finding] = []
        for line in self.findings_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Finding.from_dict(json.loads(line)))
            except Exception:
                continue
        return out

    def load_events(self, since: int = 0, limit: int | None = None) -> list[dict]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        chunk = lines[since:]
        if limit is not None:
            chunk = chunk[:limit]
        out: list[dict] = []
        for line in chunk:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def event_count(self) -> int:
        if not self.events_path.exists():
            return 0
        return sum(1 for _ in self.events_path.open("r", encoding="utf-8"))

    # -- Execution -------------------------------------------------------
    def start(self, runner_factory: Callable[[], Any]) -> None:
        """Kick off a background thread that runs the scan."""
        if self._thread is not None and self._thread.is_alive():
            return

        def _on_event(evt: Event) -> None:
            # Persist every event to events.jsonl so the GUI can replay them.
            try:
                with self.events_path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(evt.to_dict(), ensure_ascii=False) + "\n")
            except Exception:
                pass
            payload = evt.payload or {}
            # Accumulate findings in memory so get_task() sees them live.
            if evt.type == "finding":
                raw = payload.get("finding")
                if raw:
                    try:
                        self.findings.append(Finding.from_dict(raw))
                    except Exception:
                        pass
            # Update coarse progress from check lifecycle events.
            elif evt.type == "run.start":
                checks = payload.get("checks") or []
                self.meta.checks_total = len(checks)
                self.meta.checks_done = 0
                self.meta.progress = 0.0
                self.meta.current_check = None
            elif evt.type == "check.start":
                self.meta.current_check = payload.get("check")
            elif evt.type == "check.done":
                # Runner emits check.done for every check (errors included),
                # so we only listen to this one to avoid double-counting.
                self.meta.checks_done += 1
                if self.meta.checks_total:
                    self.meta.progress = round(
                        100.0 * self.meta.checks_done / self.meta.checks_total, 1
                    )
                self.meta.current_check = None
            elif evt.type == "run.done":
                self.meta.progress = 100.0
                self.meta.current_check = None
            # Signal the UI that this task changed so the list/detail views
            # refresh without waiting for their fallback poll. Debounced on the
            # frontend, so chatty finding/check events are cheap.
            _notify_tasks_ui(self.id)

        self._persist_event_unsub = self.bus.subscribe(_on_event)
        self._thread = threading.Thread(
            target=self._runner_body, args=(runner_factory,), daemon=True,
            name=f"task-{self.id}",
        )
        self._thread.start()

    def _runner_body(self, runner_factory: Callable[[], Any]) -> None:
        """Body of the dedicated scan thread — owns its own asyncio loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.meta.status = "running"
        self.meta.started_at = time.time()
        self.save_meta()
        self.bus.emit(
            "task.start",
            id=self.id,
            name=self.meta.name,
            target=self.meta.target_url,
        )
        try:
            runner = runner_factory()
            coro = self._do_run(runner)
            scan_task = self._loop.create_task(coro)
            # Watch cancel event in parallel so we can cancel the scan cleanly.
            watcher = self._loop.create_task(self._watch_cancel(scan_task))
            try:
                self._loop.run_until_complete(scan_task)
            finally:
                if not watcher.done():
                    watcher.cancel()
                    try:
                        self._loop.run_until_complete(watcher)
                    except (asyncio.CancelledError, Exception):
                        pass
            if self._cancel_event.is_set():
                self.meta.status = "cancelled"
            else:
                self.meta.status = "done"
        except asyncio.CancelledError:
            self.meta.status = "cancelled"
        except Exception as e:
            self.meta.status = "error"
            self.meta.error = f"{e}"
            self.bus.emit("task.error", error=str(e), trace=traceback.format_exc())
            log.exception("task %s crashed", self.id)
        finally:
            self.meta.finished_at = time.time()
            # Recompute severity counts from the findings list
            counts = {s.value: 0 for s in Severity}
            for f in self.findings:
                counts[f.severity.value] += 1
            self.meta.severity = counts
            self.meta.total = len(self.findings)
            self.save_meta()
            self.bus.emit(
                "task.done",
                id=self.id,
                status=self.meta.status,
                total=self.meta.total,
                severity=self.meta.severity,
            )
            if self._persist_event_unsub:
                self._persist_event_unsub()
                self._persist_event_unsub = None
            try:
                self._loop.close()
            except Exception:
                pass

    async def _watch_cancel(self, scan_task: asyncio.Task) -> None:
        while not scan_task.done():
            if self._cancel_event.is_set():
                scan_task.cancel()
                return
            await asyncio.sleep(0.25)

    async def _do_run(self, runner: Any) -> None:
        # runner is a Runner instance configured with self.bus; we pin the
        # run_dir to this Task's own directory so findings.jsonl and reports
        # live next to meta.json.
        target = _build_target(self.meta.params)
        result = await runner.run(
            target, checks=self.meta.checks or None, run_dir=self.root
        )
        self.findings = list(result.findings)
        # Render report artefacts synchronously — cheap and keeps the Task
        # self-contained so CLI, GUI and library users all get reports.
        try:
            self._render_reports(result)
        except Exception as e:
            self.bus.emit("reports.error", error=str(e))

    def _render_reports(self, result: Any) -> None:
        from argusai.report import render_html, render_json, render_markdown

        locale = str((self.meta.params or {}).get("locale") or "en")
        render_html(result, self.root / "report.html", locale=locale)
        render_markdown(result, self.root / "report.md", locale=locale)
        render_json(result, self.root / "report.json")
        self.bus.emit(
            "reports.ready",
            html=str(self.root / "report.html"),
            md=str(self.root / "report.md"),
            json=str(self.root / "report.json"),
            dir=str(self.root),
        )

    def request_cancel(self) -> None:
        self._cancel_event.set()
        self.bus.emit("task.cancel-requested", id=self.id)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def _build_target(params: dict[str, Any]) -> Target:
    """Construct a Target from raw params dict (GUI or CLI)."""
    auth_raw = params.get("auth") or None
    auth = AuthConfig(**auth_raw) if auth_raw else None
    return Target(
        url=params["url"],
        name=params.get("name") or "",
        scope=params.get("scope") or [],
        paths=params.get("paths") or [],
        headers=params.get("headers") or {},
        tags=params.get("tags") or [],
        notes=params.get("notes") or "",
        auth=auth,
    )


class TaskManager:
    """Keeps track of all tasks, runs them concurrently, rehydrates on boot."""

    def __init__(self, root: Path | str = "tasks") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._load_persisted()

    # -- Persistence -----------------------------------------------------
    def _load_persisted(self) -> None:
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = TaskMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
            # If a task was still "running" when the process died, mark as error.
            if meta.status == "running":
                meta.status = "error"
                meta.error = "process exited before task finished"
                meta.finished_at = meta.finished_at or time.time()
            task = Task(root=d, meta=meta)
            task.findings = task.load_findings()
            self._tasks[meta.id] = task

    # -- Public API ------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [t.meta.to_dict() for t in self._tasks.values()]
        items.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
        return items

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def create(
        self,
        params: dict[str, Any],
        runner_factory: Callable[["Task"], Any],
    ) -> Task:
        """Create a task dir, persist meta, spawn runner thread."""
        params = dict(params)
        if params.get("checks"):
            import argusai.checks  # noqa: F401 - ensure built-in checks are registered
            from argusai.core.check import Registry

            params["checks"] = Registry.canonical_ids(params.get("checks"))
        task_id = _new_task_id(params.get("name") or params.get("url") or "task")
        root = self.root / task_id
        meta = TaskMeta(
            id=task_id,
            name=params.get("name") or params.get("url") or task_id,
            target_url=params.get("url", ""),
            checks=list(params.get("checks") or []),
            params=params,
        )
        task = Task(root=root, meta=meta)
        task.save_meta()
        with self._lock:
            self._tasks[task_id] = task
        task.start(lambda: runner_factory(task))
        return task

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or not task.is_running():
            return False
        task.request_cancel()
        return True

    def delete(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        if task.is_running():
            task.request_cancel()
            if task._thread:
                task._thread.join(timeout=3.0)
        import shutil

        try:
            shutil.rmtree(task.root, ignore_errors=True)
        except Exception:
            return False
        with self._lock:
            self._tasks.pop(task_id, None)
        return True


def _new_task_id(hint: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", hint).strip("-").lower() or "task"
    slug = slug[:30]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid.uuid4().hex[:6]}"
