"""CLI management commands for kb / mcp / reverse / todos / cron."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from argusai.cli.main import _register_commands, app

_register_commands()
runner = CliRunner()


class _FakeApi:
    def __init__(self) -> None:
        self.kb_entries: dict[str, dict[str, Any]] = {
            "kb-demo": {
                "id": "kb-demo",
                "title": "Demo note",
                "body": "# Hello\n\nbody",
                "tags": ["demo"],
                "updated_at": "2026-01-01T00:00:00",
                "confidence": "probable",
            }
        }
        self.kb_trash: dict[str, dict[str, Any]] = {
            "kb-old": {
                "id": "kb-old",
                "title": "Old note",
                "tags": ["old"],
                "updated_at": "2025-01-01T00:00:00",
            }
        }
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self.reverse_jobs: dict[str, dict[str, Any]] = {}
        self.reverse_trash: dict[str, dict[str, Any]] = {}
        self.todos: dict[str, dict[str, Any]] = {}
        self.cron_jobs: dict[str, dict[str, Any]] = {}

    def shutdown_background_services(self) -> None:
        return None

    def kb_list(self, include_superseded: bool = False, include_trashed: bool = False) -> dict[str, Any]:
        return {"ok": True, "entries": list(self.kb_entries.values()), "root": "/tmp/kb"}

    def kb_search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        limit: int = 20,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        q = (query or "").lower()
        rows = []
        for row in self.kb_entries.values():
            hay = f"{row.get('title')} {' '.join(row.get('tags') or [])}".lower()
            if q and q not in hay:
                continue
            rows.append(row)
        return {"ok": True, "entries": rows[:limit], "query": query}

    def kb_get(self, entry_id: str) -> dict[str, Any]:
        row = self.kb_entries.get(entry_id)
        if not row:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "entry": row}

    def kb_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        eid = "kb-new"
        row = {
            "id": eid,
            "title": payload.get("title"),
            "body": payload.get("body"),
            "tags": list(payload.get("tags") or []),
            "updated_at": "2026-01-02T00:00:00",
        }
        self.kb_entries[eid] = row
        return {"ok": True, "entry": row}

    def kb_delete(self, entry_id: str, reason: str = "") -> dict[str, Any]:
        row = self.kb_entries.pop(entry_id, None)
        if row is None:
            return {"ok": False}
        self.kb_trash[entry_id] = row
        return {"ok": True}

    def kb_list_trash(self) -> dict[str, Any]:
        return {
            "ok": True,
            "entries": list(self.kb_trash.values()),
            "count": len(self.kb_trash),
            "total_bytes": 12,
        }

    def kb_restore(self, entry_id: str) -> dict[str, Any]:
        row = self.kb_trash.pop(entry_id, None)
        if not row:
            return {"ok": False, "error": "not_found"}
        self.kb_entries[entry_id] = row
        return {"ok": True, "entry": row}

    def kb_purge_trash(
        self,
        older_than_days: int | None = None,
        ids: list[str] | None = None,
    ) -> dict[str, Any]:
        removed: list[str] = []
        if ids:
            for kid in ids:
                if kid in self.kb_trash:
                    del self.kb_trash[kid]
                    removed.append(kid)
        else:
            removed = list(self.kb_trash.keys())
            self.kb_trash.clear()
        return {"ok": True, "removed": removed, "freed_bytes": len(removed) * 4, "kept": 0}

    def list_mcp_servers(self) -> dict[str, Any]:
        return {"ok": True, "servers": list(self.mcp_servers.values())}

    def save_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        sid = str(payload.get("id") or payload.get("name") or "mcp-1")
        row = {**payload, "id": sid}
        self.mcp_servers[sid] = row
        return {"ok": True, "server": row, "servers": list(self.mcp_servers.values())}

    def delete_mcp_server(self, server_id: str) -> dict[str, Any]:
        if server_id not in self.mcp_servers:
            return {"ok": False, "error": "not found"}
        del self.mcp_servers[server_id]
        return {"ok": True, "servers": list(self.mcp_servers.values())}

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        if server_id not in self.mcp_servers:
            return {"ok": False, "error": "missing"}
        return {"ok": True, "status": "alive", "transport": "stdio", "elapsed_ms": 1}

    def probe_mcp_servers(self) -> dict[str, Any]:
        return {"ok": True}

    def reverse_job_list(
        self, scope: str = "", status: str = "", include_trashed: bool = False
    ) -> dict[str, Any]:
        return {"ok": True, "jobs": list(self.reverse_jobs.values()), "root": "/tmp/rj"}

    def reverse_job_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        jid = "rj-1"
        job = {"id": jid, "status": "open", "artifacts": [], "findings": [], "hooks": [], **payload}
        self.reverse_jobs[jid] = job
        return {"ok": True, "job": job}

    def reverse_job_get(self, job_id: str, include_trashed: bool = False) -> dict[str, Any]:
        job = self.reverse_jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "job": job}

    def reverse_job_trash(self, job_id: str) -> dict[str, Any]:
        job = self.reverse_jobs.pop(job_id, None)
        if job is None:
            return {"ok": False, "error": "not found"}
        self.reverse_trash[job_id] = job
        return {"ok": True, "id": job_id}

    def reverse_job_restore(self, job_id: str) -> dict[str, Any]:
        job = self.reverse_trash.pop(job_id, None)
        if job is None:
            return {"ok": False, "error": "not found"}
        self.reverse_jobs[job_id] = job
        return {"ok": True, "id": job_id}

    def reverse_job_hard_delete(self, job_id: str) -> dict[str, Any]:
        gone = self.reverse_jobs.pop(job_id, None) is not None
        gone = self.reverse_trash.pop(job_id, None) is not None or gone
        if not gone:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "id": job_id}

    def reverse_toolchain_scan(
        self, job_id: str = "", tools: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "available": ["jadx"],
            "missing": [],
            "tools": {"jadx": {"available": True, "path": "/bin/jadx"}},
        }

    def user_todo_list(self, status: str = "", tag: str = "", limit: int = 100) -> dict[str, Any]:
        rows = list(self.todos.values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"ok": True, "items": rows[:limit]}

    def user_todo_create(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(payload or {})
        tid = "todo-1"
        row = {"id": tid, "status": "pending", **data}
        self.todos[tid] = row
        return {"ok": True, "todo": row}

    def user_todo_update(self, todo_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        row = self.todos.get(todo_id)
        if not row:
            return {"ok": False, "error": "not found"}
        row.update(dict(payload or {}))
        return {"ok": True, "todo": row}

    def user_todo_delete(self, todo_id: str) -> dict[str, Any]:
        removed = self.todos.pop(todo_id, None) is not None
        return {"ok": True, "deleted": removed}

    def list_assistant_cron_jobs(self) -> dict[str, Any]:
        return {"ok": True, "jobs": list(self.cron_jobs.values())}

    def save_assistant_cron_job(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(payload or {})
        jid = str(data.get("id") or "cron-1")
        job = {**data, "id": jid, "next_run_at": 1_700_000_000}
        self.cron_jobs[jid] = job
        result = {"ok": True, "job": job}
        if (job.get("schedule") or {}).get("kind") == "webhook":
            result["webhook"] = {
                "url": f"http://127.0.0.1:8765/api/cron/webhook/{jid}",
                "signing_secret": "secret-once",
            }
        return result

    def set_assistant_cron_enabled(self, job_id: str, enabled: bool = True) -> dict[str, Any]:
        job = self.cron_jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        job["enabled"] = bool(enabled)
        return {"ok": True, "job": job}

    def delete_assistant_cron_job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.cron_jobs:
            return {"ok": False, "error": "job not found"}
        del self.cron_jobs[job_id]
        return {"ok": True, "deleted": True}

    def list_assistant_cron_runs(self, job_id: str, limit: int = 20) -> dict[str, Any]:
        return {"ok": True, "job_id": job_id, "runs": []}

    def grant_assistant_cron_high_risk(
        self,
        job_id: str,
        granted: bool = True,
        source: str = "cli",
        session_id: str = "",
    ) -> dict[str, Any]:
        job = self.cron_jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}
        approval = {
            "high_risk_ack": bool(granted),
            "grant_source": source,
            "grant_session_id": session_id,
        }
        job["approval"] = approval
        return {"ok": True, "job": job, "approval": approval}

    def rotate_assistant_cron_webhook_secret(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.cron_jobs:
            return {"ok": False, "error": "job not found"}
        return {
            "ok": True,
            "url": f"http://127.0.0.1:8765/api/cron/webhook/{job_id}",
            "signing_secret": "rotated-secret",
        }


def _patch_api(monkeypatch, module: str, fake: _FakeApi) -> None:
    monkeypatch.setattr(f"argusai.cli.commands.{module}.headless_api", lambda: fake)


def test_kb_list_and_show(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "kb", fake)

    listed = runner.invoke(app, ["kb", "list", "--json"])
    assert listed.exit_code == 0
    assert "kb-demo" in listed.stdout

    shown = runner.invoke(app, ["kb", "show", "kb-demo"])
    assert shown.exit_code == 0
    assert "Demo note" in shown.stdout
    assert "Hello" in shown.stdout

    searched = runner.invoke(app, ["kb", "search", "demo", "--json"])
    assert searched.exit_code == 0
    assert "kb-demo" in searched.stdout


def test_kb_trash_restore_purge(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "kb", fake)

    trashed = runner.invoke(app, ["kb", "trash", "--json"])
    assert trashed.exit_code == 0
    assert "kb-old" in trashed.stdout

    restored = runner.invoke(app, ["kb", "restore", "kb-old", "--json"])
    assert restored.exit_code == 0
    assert "kb-old" in fake.kb_entries
    assert "kb-old" not in fake.kb_trash

    runner.invoke(app, ["kb", "delete", "kb-old", "--json"])
    purged = runner.invoke(app, ["kb", "purge", "--id", "kb-old", "--yes", "--json"])
    assert purged.exit_code == 0
    assert "kb-old" not in fake.kb_trash

    refuse = runner.invoke(app, ["kb", "purge", "--all"])
    assert refuse.exit_code != 0


def test_reverse_trash_restore_purge(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "reverse_cmd", fake)
    fake.reverse_jobs["rj-1"] = {"id": "rj-1", "title": "App"}

    trashed = runner.invoke(app, ["reverse", "trash", "rj-1", "--json"])
    assert trashed.exit_code == 0
    assert "rj-1" in fake.reverse_trash

    restored = runner.invoke(app, ["reverse", "restore", "rj-1", "--json"])
    assert restored.exit_code == 0
    assert "rj-1" in fake.reverse_jobs

    runner.invoke(app, ["reverse", "trash", "rj-1", "--json"])
    purged = runner.invoke(app, ["reverse", "purge", "rj-1", "--yes", "--json"])
    assert purged.exit_code == 0
    assert "rj-1" not in fake.reverse_trash
    assert "rj-1" not in fake.reverse_jobs


def test_mcp_add_list(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "mcp", fake)

    added = runner.invoke(
        app,
        ["mcp", "add", "-n", "Filesystem", "-c", "npx", "-a", "-y", "--json"],
    )
    assert added.exit_code == 0
    listed = runner.invoke(app, ["mcp", "list", "--json"])
    assert listed.exit_code == 0
    assert "Filesystem" in listed.stdout


def test_reverse_create_show(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "reverse_cmd", fake)

    created = runner.invoke(
        app,
        ["reverse", "create", "-T", "App", "--url", "https://ex.test", "--json"],
    )
    assert created.exit_code == 0
    assert "rj-1" in created.stdout
    shown = runner.invoke(app, ["reverse", "show", "rj-1"])
    assert shown.exit_code == 0
    assert "App" in shown.stdout


def test_todos_flow(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "todos", fake)

    added = runner.invoke(app, ["todos", "add", "Ship CLI", "--json"])
    assert added.exit_code == 0
    done = runner.invoke(app, ["todos", "done", "todo-1", "--json"])
    assert done.exit_code == 0
    assert "completed" in done.stdout


def test_cron_add_list(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "cron", fake)

    added = runner.invoke(
        app,
        [
            "cron",
            "add",
            "-n",
            "Morning",
            "-p",
            "summarize overnight findings",
            "--every",
            "3600",
            "--json",
        ],
    )
    assert added.exit_code == 0
    listed = runner.invoke(app, ["cron", "list", "--json"])
    assert listed.exit_code == 0
    assert "Morning" in listed.stdout


def test_cron_event_triggers_and_authorization(monkeypatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, "cron", fake)

    hooked = runner.invoke(app, [
        "cron", "add",
        "-n", "Deploy hook",
        "-p", "inspect deploy",
        "--webhook",
    ])
    assert hooked.exit_code == 0
    assert "Signing secret" in hooked.stdout
    assert fake.cron_jobs["cron-1"]["schedule"]["kind"] == "webhook"

    watched = runner.invoke(app, [
        "cron", "add",
        "--id", "cron-watch",
        "-n", "Watch config",
        "-p", "inspect changes",
        "--watch-path", "/tmp/config",
        "--glob", "*.json",
        "--no-recursive",
    ])
    assert watched.exit_code == 0
    assert fake.cron_jobs["cron-watch"]["schedule"]["kind"] == "file_change"
    assert fake.cron_jobs["cron-watch"]["schedule"]["recursive"] is False

    granted = runner.invoke(app, [
        "cron", "authorize", "cron-watch", "--yes",
    ])
    assert granted.exit_code == 0
    assert fake.cron_jobs["cron-watch"]["approval"]["high_risk_ack"] is True

