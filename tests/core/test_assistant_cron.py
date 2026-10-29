"""Domain-agnostic assistant cron store + scheduler."""

from __future__ import annotations

import time
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.core.assistant.cron import CronScheduler, CronStore, next_run_at, normalize_schedule


def test_normalize_schedule_interval_and_cron() -> None:
    assert normalize_schedule({"kind": "interval", "every_seconds": 120})["every_seconds"] == 120
    assert normalize_schedule({"kind": "interval", "every_seconds": 1})["every_seconds"] == 1
    assert normalize_schedule({"kind": "interval", "every_seconds": 0})["every_seconds"] == 1
    cron = normalize_schedule({"kind": "cron", "expr": "*/5 * * * *"})
    assert cron["kind"] == "cron"
    assert cron["expr"] == "*/5 * * * *"
    assert normalize_schedule({"kind": "webhook"}) == {
        "kind": "webhook",
        "debounce_seconds": 0,
    }
    watched = normalize_schedule({
        "kind": "file_change",
        "path": "/tmp/data",
        "glob": "*.json",
    })
    assert watched["kind"] == "file_change"
    assert watched["glob"] == "*.json"
    assert next_run_at(watched) == 0.0


def test_file_change_requires_path() -> None:
    with pytest.raises(ValueError, match="path is required"):
        normalize_schedule({"kind": "file_change"})


def test_cron_rejects_oversized_context_snapshot(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    with pytest.raises(ValueError, match="cron context exceeds"):
        store.upsert_job({
            "name": "oversized",
            "prompt": "run",
            "context": {"payload": "x" * 40_000},
            "schedule": {"kind": "interval", "every_seconds": 60},
        })


def test_upsert_list_runs_and_delete(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "briefing",
        "prompt": "Run morning briefing",
        "schedule": {"kind": "interval", "every_seconds": 3600},
        "tags": ["custom"],
        "destination": "new_session",
        "origin_session_id": "chat-abc",
    })
    assert job["id"]
    assert job["origin_session_id"] == "chat-abc"
    assert job["notify"]["report_to_origin"] is True
    assert store.list_jobs()[0]["name"] == "briefing"
    run = store.append_run(job["id"], {
        "started_at": time.time(),
        "finished_at": time.time(),
        "status": "completed",
        "summary": "ok",
        "session_id": "cron-test",
    })
    assert run["run_id"]
    assert store.list_runs(job["id"])[0]["status"] == "completed"
    assert store.set_enabled(job["id"], False)["enabled"] is False
    assert store.delete_job(job["id"]) is True
    assert store.list_jobs() == []


def test_job_accepts_job_owned_dedicated_session(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "private history",
        "prompt": "continue",
        "destination": "dedicated_session",
        "schedule": {"kind": "interval", "every_seconds": 60},
    })
    assert job["destination"] == "dedicated_session"
    assert job["session_id"] == ""


def test_scheduler_defers_when_busy(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "patrol",
        "prompt": "check",
        "schedule": {"kind": "interval", "every_seconds": 3600},
        "next_run_at": time.time() - 10,
        "destination": "session",
        "session_id": "busy-session",
    })
    fired: list[str] = []

    def fire(j: dict) -> dict:
        fired.append(j["id"])
        return {"ok": True, "status": "completed", "summary": "done", "session_id": "s1"}

    sched = CronScheduler(store, fire=fire, is_busy=lambda _sid: True, tick_seconds=5)
    results = sched.tick_once()
    assert results and results[0]["status"] == "deferred"
    assert fired == []
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert refreshed["pending_fire"] is True


def test_new_session_job_does_not_reuse_or_wait_on_previous_run(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "standalone",
        "prompt": "check",
        "schedule": {"kind": "interval", "every_seconds": 3600},
        "next_run_at": time.time() - 10,
        "destination": "new_session",
        "session_id": "cron-previous-run",
    })
    busy_calls: list[str] = []

    def busy(session_id: str) -> bool:
        busy_calls.append(session_id)
        return True

    sched = CronScheduler(
        store,
        fire=lambda _job: {
            "ok": True,
            "status": "completed",
            "summary": "done",
            "session_id": "cron-current-run",
        },
        is_busy=busy,
        tick_seconds=5,
    )
    results = sched.tick_once()

    assert results[0]["status"] == "completed"
    assert busy_calls == []
    assert store.list_runs(job["id"])[0]["session_id"] == "cron-current-run"


def test_scheduler_fires_and_records_history(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "patrol",
        "prompt": "check",
        "schedule": {"kind": "interval", "every_seconds": 3600},
        "next_run_at": time.time() - 10,
    })

    def fire(j: dict) -> dict:
        return {
            "ok": True,
            "status": "completed",
            "summary": "all quiet",
            "session_id": "cron-s",
            "assistant_run_job_id": "run-1",
        }

    sched = CronScheduler(store, fire=fire, is_busy=lambda _sid: False, tick_seconds=5)
    results = sched.tick_once()
    assert results[0]["status"] == "completed"
    runs = store.list_runs(job["id"])
    assert len(runs) == 1
    assert runs[0]["summary"] == "all quiet"
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert refreshed["last_status"] == "completed"
    assert float(refreshed["next_run_at"]) > time.time() - 5


def test_scheduler_publishes_running_status_before_fire_finishes(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "visible running state",
        "prompt": "run",
        "schedule": {"kind": "interval", "every_seconds": 60},
        "next_run_at": time.time() - 1,
    })
    entered = threading.Event()
    release = threading.Event()

    def fire(_job):
        entered.set()
        assert release.wait(timeout=2)
        return {"ok": True, "status": "completed", "summary": "done"}

    sched = CronScheduler(store, fire=fire, tick_seconds=5)
    worker = threading.Thread(target=sched.tick_once)
    worker.start()
    assert entered.wait(timeout=2)
    live = store.get_job(job["id"])
    assert live is not None
    assert live["last_status"] == "running"
    assert live["last_run_at"] is not None
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_next_run_interval() -> None:
    after = 1_700_000_000.0
    nxt = next_run_at({"kind": "interval", "every_seconds": 120}, after=after)
    assert nxt == after + 120


def test_cron_honors_day_month_and_minute_boundary() -> None:
    after = time.mktime((2026, 8, 2, 8, 59, 37, 0, 0, -1))
    nxt = next_run_at({"kind": "cron", "expr": "0 9 1 * *"}, after=after)
    local = time.localtime(nxt)
    assert (local.tm_mon, local.tm_mday, local.tm_hour, local.tm_min, local.tm_sec) == (9, 1, 9, 0, 0)


def test_cron_rejects_path_traversal_ids(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    with pytest.raises(ValueError, match="invalid cron job id"):
        store.upsert_job({
            "id": "../../../escaped",
            "name": "bad",
            "prompt": "bad",
            "schedule": {"kind": "interval", "every_seconds": 60},
        })
    with pytest.raises(ValueError, match="invalid cron job id"):
        store.list_runs("../../outside")


def test_scheduler_disables_job_at_max_runs(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "finite",
        "prompt": "tick",
        "max_runs": 2,
        "schedule": {"kind": "interval", "every_seconds": 1},
        "next_run_at": time.time() - 1,
    })
    sched = CronScheduler(
        store,
        fire=lambda _job: {"ok": True, "status": "completed", "summary": "ok"},
        is_busy=lambda _sid: "",
    )
    assert sched.tick_once()[0]["status"] == "completed"
    store.update_job_fields(job["id"], next_run_at=time.time() - 1)
    assert sched.tick_once()[0]["status"] == "completed"
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert refreshed["run_count"] == 2
    assert refreshed["enabled"] is False
    assert refreshed["disabled_reason"] == "max_runs_reached"


def test_cron_stores_share_one_root_lock_without_lost_updates(tmp_path: Path) -> None:
    stores = [CronStore(tmp_path), CronStore(tmp_path)]
    barrier = threading.Barrier(2)

    def writer(index: int) -> None:
        barrier.wait()
        for n in range(10):
            stores[index].upsert_job({
                "id": f"cron-{index}-{n}",
                "name": f"job-{index}-{n}",
                "prompt": "tick",
                "schedule": {"kind": "interval", "every_seconds": 60},
            })

    threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(CronStore(tmp_path).list_jobs()) == 20


def test_scheduler_preserves_typed_busy_reason(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "blocked",
        "prompt": "tick",
        "destination": "session",
        "session_id": "session-a",
        "schedule": {"kind": "interval", "every_seconds": 60},
        "next_run_at": time.time() - 1,
    })
    sched = CronScheduler(
        store,
        fire=lambda _job: pytest.fail("blocked job must not fire"),
        is_busy=lambda _sid: "pending_confirmation",
    )
    result = sched.tick_once()[0]
    assert result["status"] == "deferred"
    assert result["reason"] == "pending_confirmation"
    refreshed = store.get_job(job["id"])
    assert refreshed is not None
    assert "tool confirmation" in refreshed["last_summary"]


def test_scheduler_passes_job_to_permission_aware_busy_callback(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "delegated",
        "prompt": "tick",
        "destination": "session",
        "session_id": "cron-session",
        "schedule": {"kind": "interval", "every_seconds": 60},
        "next_run_at": time.time() - 1,
    })
    seen: list[str] = []

    def busy(_session_id: str | None, current_job: dict | None) -> str:
        seen.append(str((current_job or {}).get("id") or ""))
        return "pending_confirmation"

    sched = CronScheduler(
        store,
        fire=lambda _job: pytest.fail("blocked job must not fire"),
        is_busy=busy,
    )
    assert sched.tick_once()[0]["status"] == "deferred"
    assert seen == [job["id"]]


def test_event_queue_is_durable_deduplicated_and_acked(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "hook",
        "prompt": "handle",
        "schedule": {"kind": "webhook"},
    })
    queued = store.enqueue_event(
        job["id"],
        source="webhook",
        event_id="evt-one",
        payload={"value": 1},
    )
    assert queued["duplicate"] is False
    assert CronStore(tmp_path).pending_event_count(job["id"]) == 1
    duplicate = store.enqueue_event(
        job["id"],
        source="webhook",
        event_id="evt-one",
        payload={"value": 1},
    )
    assert duplicate["duplicate"] is True
    event = store.peek_event(job["id"])
    assert event is not None and event["payload"] == {"value": 1}
    assert store.ack_event(job["id"], "evt-one") is True
    assert store.pending_event_count(job["id"]) == 0
    assert store.enqueue_event(
        job["id"],
        source="webhook",
        event_id="evt-one",
        payload={"value": 1},
    )["duplicate"] is True


def test_scheduler_fires_event_job_once_and_records_trigger(tmp_path: Path) -> None:
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "hook",
        "prompt": "handle",
        "schedule": {"kind": "webhook"},
    })
    store.enqueue_event(
        job["id"],
        source="webhook",
        event_id="evt-fire",
        payload={"value": 2},
    )
    seen: list[dict] = []

    def fire(current: dict) -> dict:
        seen.append(dict(current.get("_trigger_event") or {}))
        return {"ok": True, "status": "completed", "summary": "done"}

    result = CronScheduler(store, fire=fire).tick_once()[0]
    assert result["status"] == "completed"
    assert seen[0]["event_id"] == "evt-fire"
    assert store.pending_event_count(job["id"]) == 0
    assert store.list_runs(job["id"])[0]["trigger"]["payload"] == {"value": 2}


def test_file_change_poller_baselines_then_queues_change(tmp_path: Path) -> None:
    from argusai.core.assistant.cron_triggers import FileChangePoller

    watched = tmp_path / "watched"
    watched.mkdir()
    item = watched / "config.json"
    item.write_text("{}", encoding="utf-8")
    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "watch",
        "prompt": "inspect",
        "schedule": {
            "kind": "file_change",
            "path": str(watched),
            "glob": "*.json",
            "recursive": False,
            "poll_seconds": 1,
            "debounce_seconds": 0,
        },
    })
    poller = FileChangePoller(store)
    assert poller.poll_once() == []
    item.write_text('{"changed": true}', encoding="utf-8")
    poller._last_poll[job["id"]] = 0  # noqa: SLF001
    queued = poller.poll_once()
    assert len(queued) == 1
    event = store.peek_event(job["id"])
    assert event is not None
    assert event["source"] == "file_change"
    assert event["payload"]["changes"][0]["kind"] == "modified"


def test_signed_webhook_enqueues_and_replay_is_deduplicated(tmp_path: Path) -> None:
    import json
    import urllib.request

    from argusai.core.assistant.cron_triggers import (
        CronWebhookServer,
        ensure_webhook_secret,
        webhook_signature,
    )

    store = CronStore(tmp_path)
    job = store.upsert_job({
        "name": "hook",
        "prompt": "inspect",
        "schedule": {"kind": "webhook"},
    })
    secret, _ = ensure_webhook_secret(store.root, job["id"])
    server = CronWebhookServer(store, host="127.0.0.1", port=0)
    server.start()
    try:
        body = json.dumps({"event": "deploy"}).encode()
        timestamp = str(time.time())
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.listen_port}/api/cron/webhook/{job['id']}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-ArgusAI-Timestamp": timestamp,
                "X-ArgusAI-Event-Id": "deploy-1",
                "X-ArgusAI-Signature": "sha256="
                + webhook_signature(secret, timestamp, body),
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 202
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert store.pending_event_count(job["id"]) == 1
    finally:
        server.stop()


def test_ai_cron_tool_binds_current_conversation_and_builds_events(tmp_path: Path) -> None:
    from argusai.core.tools.assistant_cron_tools import cron_upsert

    settings = SimpleNamespace(
        data_root=str(tmp_path),
        assistant_cron_webhook_host="127.0.0.1",
        assistant_cron_webhook_port=8765,
    )
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(get=lambda: settings),
        get_assistant_session=lambda sid: {"ok": sid == "origin"},
    )
    fixed = cron_upsert(
        api,
        name="continue",
        prompt="continue work",
        destination="session",
        _session_id="origin",
    )
    assert fixed["ok"] is True
    assert fixed["job"]["session_id"] == "origin"
    assert fixed["job"]["origin_session_id"] == "origin"

    hook = cron_upsert(
        api,
        name="deploy",
        prompt="inspect deploy",
        schedule_kind="webhook",
        _session_id="origin",
    )
    assert hook["ok"] is True
    assert hook["job"]["schedule"]["kind"] == "webhook"
    assert hook["webhook"]["url"].endswith(hook["job"]["id"])
    assert "signing_secret" not in hook["webhook"]
