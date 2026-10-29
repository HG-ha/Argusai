"""Task CRUD + report path resolution exposed to the frontend."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class TasksMixin:
    """Wraps :class:`argusai.core.tasks.TaskManager` for the bridge.

    The frontend ``api.js`` calls map 1:1 onto the public methods below;
    keep the names stable. Internal logic should stay in
    :class:`TaskManager`, not here.
    """

    def list_tasks(self) -> list[dict[str, Any]]:
        return self.tasks.list()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if not task:
            return None
        return {
            "meta": task.meta.to_dict(),
            "findings": [f.to_dict() for f in task.findings],
            "event_count": task.event_count(),
            "is_running": task.is_running(),
            "reports": self._report_paths(task),
        }

    def get_task_events(self, task_id: str, since: int = 0) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if not task:
            return {"events": [], "total": 0, "status": "missing"}
        # In-memory bus has finer granularity than disk; prefer it while the
        # task is still running and fall back to replay for finished tasks.
        if task.is_running():
            events = task.bus.since(since)
            return {
                "events": [e.to_dict() for e in events],
                "total": task.bus.count,
                "status": task.status,
            }
        events = task.load_events(since=since)
        total = since + len(events)
        return {"events": events, "total": total, "status": task.status}

    def create_task(self, params: dict[str, Any]) -> dict[str, Any]:
        if not params.get("url"):
            return {"ok": False, "error": "url is required"}
        params = dict(params)
        params.setdefault("locale", self._locale)
        try:
            import argusai.checks  # noqa: F401 - ensure built-in checks are registered
            from argusai.core.check import Registry

            if params.get("checks"):
                params["checks"] = Registry.canonical_ids(params.get("checks"))
        except KeyError as e:
            return {"ok": False, "error": str(e).strip("'")}

        from argusai.core.runner import Runner
        from argusai.core.tasks import Task

        def factory(task: Task) -> Runner:
            from argusai.core.context import ContextSettings

            settings = ContextSettings(
                headless=bool(params.get("headless", True)),
                cdp_url=params.get("cdp_url") or None,
                browser_slow_mo=int(params.get("browser_slow_mo") or 0),
                xss_observe_delay_ms=int(params.get("xss_observe_delay_ms") or 0),
            )
            app_settings = self.settings_mgr.get()
            if app_settings.ai_enabled and app_settings.ai_endpoints:
                selected = str(params.get("ai_endpoint") or "").strip()
                endpoints = [ep for ep in app_settings.ai_endpoints if getattr(ep, "enabled", True)]
                if selected:
                    endpoints = [
                        ep for idx, ep in enumerate(endpoints)
                        if selected in {str(idx), ep.name, f"{ep.name}|{ep.base_url}|{ep.model}"}
                    ]
                settings.ai_endpoints = endpoints
            return Runner(bus=task.bus, work_dir=task.root, settings=settings)

        task = self.tasks.create(params, runner_factory=factory)
        return {"ok": True, "id": task.id, "meta": task.meta.to_dict()}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        return {"ok": self.tasks.cancel(task_id)}

    def delete_task(self, task_id: str) -> dict[str, Any]:
        return {"ok": self.tasks.delete(task_id)}

    def rescan_task(self, task_id: str) -> dict[str, Any]:
        """Spawn a new task that reuses the source task's parameters."""
        src = self.tasks.get(task_id)
        if not src:
            return {"ok": False, "error": f"task {task_id} not found"}
        params = dict(src.meta.params or {})
        base_name = params.get("name") or src.meta.name or params.get("url", "")
        params["name"] = f"{base_name} (rescan)"
        return self.create_task(params)

    def _report_paths(self, task: Any) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for kind, fname in (("html", "report.html"), ("md", "report.md"), ("json", "report.json")):
            p = task.root / fname
            out[kind] = str(p) if p.exists() else None
        out["dir"] = str(task.root)
        out["findings"] = str(task.findings_path) if task.findings_path.exists() else None
        return out
