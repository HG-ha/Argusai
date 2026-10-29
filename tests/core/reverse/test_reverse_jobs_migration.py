"""Tests for ReverseJob schema migration and runtime fields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argusai.core.reverse.reverse_constants import JOB_SCHEMA_VERSION
from argusai.core.reverse.reverse_job_migrate import migrate_job_data
from argusai.core.reverse.reverse_jobs import ReverseJobStore


@pytest.fixture
def store(tmp_path: Path) -> ReverseJobStore:
    return ReverseJobStore(tmp_path / "reverse_jobs")


def test_migrate_v1_web_job_infers_target_type() -> None:
    raw = {
        "id": "rev-20260523-abcd",
        "title": "legacy web job",
        "target_url": "https://example.com/app.js",
        "status": "active",
        "tags": [],
        "stack": {},
        "artifacts": [],
    }
    migrated, changed = migrate_job_data(raw)
    assert changed is True
    assert migrated["schema_version"] == JOB_SCHEMA_VERSION
    assert migrated["target_type"] == "web_js"
    assert migrated["platform"] == "web"
    assert migrated["runtime_sessions"] == []
    assert migrated["evidence_refs"] == []


def test_migrate_idempotent_when_already_v2() -> None:
    raw = {
        "schema_version": JOB_SCHEMA_VERSION,
        "target_type": "android",
        "platform": "android",
        "target_meta": {},
        "toolchain_status": {"scanned_at": "", "tools": {}},
        "analysis_summary": {},
        "risk_summary": {},
        "runtime_sessions": [],
        "evidence_refs": [],
        "replay_refs": [],
    }
    migrated, changed = migrate_job_data(raw)
    assert changed is False
    assert migrated["target_type"] == "android"


def test_store_loads_legacy_job_json(tmp_path: Path) -> None:
    job_dir = tmp_path / "reverse_jobs" / "rev-20260523-legacy"
    job_dir.mkdir(parents=True)
    (job_dir / "artifacts").mkdir()
    legacy = {
        "id": "rev-20260523-legacy",
        "title": "old job",
        "target_url": "https://example.com/bundle.js",
        "scope": "",
        "status": "active",
        "intent": "",
        "tags": [],
        "stack": {},
        "artifacts": [],
        "hooks": [],
        "oracles": [],
        "findings": [],
        "reproduce_scripts": [],
        "linked_task_id": "",
        "linked_kb_ids": [],
        "created_at": "2026-05-23T00:00:00",
        "updated_at": "2026-05-23T00:00:00",
    }
    (job_dir / "job.json").write_text(json.dumps(legacy), encoding="utf-8")

    store = ReverseJobStore(tmp_path / "reverse_jobs")
    job = store.get("rev-20260523-legacy")
    assert job is not None
    assert job["schema_version"] == JOB_SCHEMA_VERSION
    assert job["target_type"] == "web_js"

    persisted = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert persisted["schema_version"] == JOB_SCHEMA_VERSION


def test_create_android_job_with_meta(store: ReverseJobStore) -> None:
    job = store.create(
        title="ACME Android",
        target_type="android",
        platform="android",
        target_meta={"package": "com.acme.app", "sha256": "abc"},
        tags=["android"],
    )
    assert job["target_type"] == "android"
    assert job["platform"] == "android"
    assert job["target_meta"]["package"] == "com.acme.app"


def test_runtime_session_lifecycle(store: ReverseJobStore) -> None:
    job = store.create(title="dynamic test", target_type="android", platform="android")
    session = store.start_runtime_session(
        job["id"],
        kind="frida",
        target="com.example.app",
        tool="frida",
    )
    assert session["state"] == "running"
    stopped = store.stop_runtime_session(job["id"], session["id"], state="stopped")
    assert stopped is not None
    assert stopped["state"] == "stopped"

    loaded = store.get(job["id"])
    assert loaded is not None
    assert loaded["runtime_sessions"][0]["stopped_at"]


def test_link_evidence_and_replay_refs(store: ReverseJobStore) -> None:
    job = store.create(title="links", target_type="windows", platform="windows")
    ev = store.link_evidence_ref(job["id"], ref_type="proxy_flow", ref_id="flow-123")
    rp = store.link_replay_ref(job["id"], ref_type="web_replay", ref_id="rep-456")
    assert ev["type"] == "proxy_flow"
    assert rp["type"] == "web_replay"
    summary = store.list()[0]
    assert summary["evidence_ref_count"] == 1
    assert summary["replay_ref_count"] == 1
