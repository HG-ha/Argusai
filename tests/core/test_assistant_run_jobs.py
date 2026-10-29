"""Focused coverage for durable resumable assistant run jobs."""

from __future__ import annotations

import json

import pytest

from argusai.core.assistant import run_jobs as run_jobs_module
from argusai.core.assistant.run_jobs import (
    AssistantRunJobStore,
    AssistantRunState,
    validate_job_id,
)


def test_state_transitions_track_epoch_and_append_events(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("session-1", job_id="run-1", budget={"max_rounds": 25})

    store.transition(job, AssistantRunState.RUNNING, status="Analysing target")
    store.checkpoint(job, progress_current=2, progress_total=5)
    store.transition(job, AssistantRunState.WAITING_USER)
    store.transition(job, AssistantRunState.RUNNING)
    store.transition(job, AssistantRunState.COMPLETED, status="Done")

    assert job.state is AssistantRunState.COMPLETED
    assert job.epoch == 2
    assert job.progress_current == 2
    assert job.progress_total == 5
    assert job.finished_at is not None
    assert [event.kind for event in job.events] == [
        "created",
        "state_changed",
        "checkpoint",
        "state_changed",
        "state_changed",
        "state_changed",
    ]
    assert [event.sequence for event in job.events] == list(range(1, 7))


def test_atomic_snapshot_persists_and_reloads_events(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("session.a", job_id="job.a", status="Queued")
    store.transition(job, "running")
    store.checkpoint(job, status="Halfway", progress_current=1, progress_total=2)

    snapshot = tmp_path / "session.a.runs" / "job.a.json"
    events = tmp_path / "session.a.runs" / "job.a.events.jsonl"
    assert json.loads(snapshot.read_text(encoding="utf-8"))["revision"] == job.revision
    assert not list(snapshot.parent.glob("*.tmp"))
    assert len(events.read_text(encoding="utf-8").splitlines()) == 3

    reloaded = AssistantRunJobStore(tmp_path).load("session.a", "job.a")
    assert reloaded.state is AssistantRunState.PAUSED
    assert reloaded.epoch == 1
    assert reloaded.status == "Recovered after an interrupted process"
    assert reloaded.event_count == 5
    assert [event.kind for event in reloaded.events][-2:] == ["state_changed", "crash_recovered"]


def test_running_job_recovers_to_paused_once(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("resume", job_id="run")
    store.transition(job, "running")

    recovered = store.load("resume", "run")
    loaded_again = store.load("resume", "run")

    assert recovered.state is AssistantRunState.PAUSED
    assert loaded_again.state is AssistantRunState.PAUSED
    assert [event.kind for event in loaded_again.events].count("crash_recovered") == 1


def test_cancellation_request_is_durable_then_cancels(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("cancel", job_id="run")
    store.transition(job, "running")

    store.request_cancellation(job, reason="User pressed stop")
    reloaded = store.load("cancel", "run", recover_running=False)
    assert reloaded.cancellation_requested is True
    assert reloaded.cancellation_requested_at is not None
    assert reloaded.state is AssistantRunState.RUNNING

    store.cancel(job, reason="Stopped by user")
    assert job.state is AssistantRunState.CANCELLED
    assert job.events[-1].kind == "state_changed"


def test_invalid_transition_and_ids_are_rejected(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("safe", job_id="run")

    with pytest.raises(ValueError, match="invalid assistant run transition"):
        store.transition(job, "completed")
    with pytest.raises(ValueError, match="invalid job_id"):
        validate_job_id("../escape")
    with pytest.raises(ValueError, match="invalid session_id"):
        store.create("../escape", job_id="safe")


def test_list_recovers_running_and_skips_corrupt_snapshots(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    healthy = store.create("sess", job_id="healthy")
    store.transition(healthy, "running")
    corrupt_dir = tmp_path / "sess.runs"
    (corrupt_dir / "broken.json").write_text("{not-json", encoding="utf-8")

    jobs = AssistantRunJobStore(tmp_path).list("sess")
    assert [job.job_id for job in jobs] == ["healthy"]
    assert jobs[0].state is AssistantRunState.PAUSED


def test_resume_keeps_job_id_and_increments_epoch(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess", job_id="durable-1")
    store.transition(job, "running")
    paused = store.load("sess", "durable-1")
    store.transition(paused, "running", status="Resumed by user")

    assert paused.job_id == "durable-1"
    assert paused.epoch == 2
    assert paused.state is AssistantRunState.RUNNING


def test_protect_job_ids_skips_live_running_recovery(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess", job_id="live-1")
    store.transition(job, "running", status="still working")

    listed = store.list("sess", protect_job_ids={"live-1"})
    assert listed[0].state is AssistantRunState.RUNNING
    assert [event.kind for event in listed[0].events].count("crash_recovered") == 0

    orphaned = store.list("sess")
    assert orphaned[0].state is AssistantRunState.PAUSED
    assert [event.kind for event in orphaned[0].events].count("crash_recovered") == 1


def test_close_waiting_jobs_resolves_orphans(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    waiting = store.create("sess", job_id="wait-1")
    store.transition(waiting, "running")
    store.transition(waiting, "waiting_user")
    keep = store.create("sess", job_id="keep-1")
    store.transition(keep, "running")
    store.transition(keep, "waiting_user")

    closed = store.close_waiting_jobs("sess", except_job_id="keep-1")
    assert [job.job_id for job in closed] == ["wait-1"]
    assert store.load("sess", "wait-1", recover_running=False).state is AssistantRunState.COMPLETED
    assert store.load("sess", "keep-1", recover_running=False).state is AssistantRunState.WAITING_USER


def test_close_waiting_jobs_also_cancels_paused_orphans(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    paused = store.create("sess", job_id="paused-1")
    store.transition(paused, "running")
    store.transition(paused, "paused", status="Recovered after an interrupted process")
    keep = store.create("sess", job_id="keep-paused")
    store.transition(keep, "running")
    store.transition(keep, "paused")

    closed = store.close_waiting_jobs("sess", except_job_id="keep-paused")
    assert [job.job_id for job in closed] == ["paused-1"]
    assert store.load("sess", "paused-1", recover_running=False).state is AssistantRunState.CANCELLED
    assert store.load("sess", "keep-paused", recover_running=False).state is AssistantRunState.PAUSED


def test_throttled_checkpoints_coalesce_and_flush_before_transition(
    tmp_path,
    monkeypatch,
) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess", job_id="coalesced")
    store.transition(job, "running", status="Starting")
    persisted: list[int] = []
    appended: list[str] = []
    original_persist = store._persist
    original_append = store._append_event

    def track_persist(changed_job) -> None:
        persisted.append(changed_job.revision)
        original_persist(changed_job)

    def track_append(changed_job, event) -> None:
        appended.append(event.kind)
        original_append(changed_job, event)

    monkeypatch.setattr(store, "_persist", track_persist)
    monkeypatch.setattr(store, "_append_event", track_append)
    baseline_revision = job.revision
    baseline_events = job.event_count

    for index in range(50):
        status = f"phase-{index // 10}"
        store.checkpoint_throttled(job, status=status, interval_seconds=60)
        store.checkpoint_throttled(job, status=status, interval_seconds=60)

    assert job.revision == baseline_revision
    assert job.event_count == baseline_events
    assert persisted == []
    assert appended == []

    store.transition(job, "completed", status="Done")

    assert persisted == [baseline_revision + 1, baseline_revision + 2]
    assert appended == ["checkpoint", "state_changed"]
    assert [event.kind for event in job.events[-2:]] == ["checkpoint", "state_changed"]
    assert job.events[-2].data["status"] == "phase-4"
    assert job.state is AssistantRunState.COMPLETED


def test_segment_checkpoint_immediately_merges_pending_status(tmp_path) -> None:
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess", job_id="segment")
    store.transition(job, "running")
    baseline_events = job.event_count

    store.checkpoint_throttled(
        job,
        status="Running tool batch",
        interval_seconds=60,
    )
    store.checkpoint(job, progress_current=3)

    assert job.event_count == baseline_events + 1
    assert job.status == "Running tool batch"
    assert job.progress_current == 3
    assert job.events[-1].kind == "checkpoint"
    assert job.events[-1].data["status"] == "Running tool batch"
    snapshot = json.loads(
        (tmp_path / "sess.runs" / "segment.json").read_text(encoding="utf-8")
    )
    assert snapshot["status"] == "Running tool batch"
    assert snapshot["progress"]["current"] == 3

    store.transition(job, "completed")


def test_event_sidecar_compacts_and_sequence_remains_monotonic(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(run_jobs_module, "MAX_RECENT_EVENTS", 5)
    monkeypatch.setattr(run_jobs_module, "EVENT_SIDECAR_COMPACT_BYTES", 1_200)
    monkeypatch.setattr(run_jobs_module, "EVENT_SIDECAR_TARGET_BYTES", 600)
    monkeypatch.setattr(run_jobs_module, "EVENT_SIDECAR_READ_BYTES", 700)
    store = AssistantRunJobStore(tmp_path)
    job = store.create("sess", job_id="bounded-events")
    store.transition(job, "running")

    for index in range(40):
        store.checkpoint(job, status=f"step-{index}-{'x' * 80}")

    events_path = tmp_path / "sess.runs" / "bounded-events.events.jsonl"
    on_disk = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    sequences = [event["sequence"] for event in on_disk]
    assert 1 <= len(on_disk) <= 5
    assert sequences == sorted(sequences)
    assert sequences[-1] == job.event_count
    assert events_path.stat().st_size <= 1_200

    previous_count = job.event_count
    reloaded = AssistantRunJobStore(tmp_path).load(
        "sess",
        "bounded-events",
        recover_running=False,
    )
    assert reloaded.event_count == previous_count
    assert reloaded.events[-1].sequence == previous_count

    AssistantRunJobStore(tmp_path).checkpoint(reloaded, status="after compaction")
    assert reloaded.event_count == previous_count + 1
    assert reloaded.events[-1].sequence == previous_count + 1
