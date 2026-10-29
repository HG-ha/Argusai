"""Deterministic acceptance closed loop (Loom22 §8.5)."""

from __future__ import annotations

from argusai.core.assistant.acceptance import (
    build_repair_prompt,
    unfinished_validation_todos,
    verify_validation_plan,
)


def test_verify_passes_without_validation_todos() -> None:
    report = verify_validation_plan([{"id": "t1", "status": "completed", "kind": "phase"}])
    assert report["passed"] is True
    assert report["checked"] == 0


def test_verify_fails_open_validation_todo() -> None:
    todos = [{
        "id": "validate.xss",
        "kind": "validation",
        "status": "pending",
        "done_when": "proof",
    }]
    assert unfinished_validation_todos(todos)
    report = verify_validation_plan(todos)
    assert report["passed"] is False
    assert report["failures"][0]["check"] == "validation_complete"


def test_verify_fails_completed_without_evidence() -> None:
    todos = [{
        "id": "validate.xss",
        "kind": "validation",
        "status": "completed",
    }]
    report = verify_validation_plan(todos)
    assert report["passed"] is False
    assert report["failures"][0]["check"] == "evidence_present"


def test_verify_passes_with_evidence_refs() -> None:
    todos = [{
        "id": "validate.xss",
        "kind": "validation",
        "status": "completed",
        "evidence_refs": ["ev://1"],
    }]
    report = verify_validation_plan(todos)
    assert report["passed"] is True


def test_repair_prompt_lists_failures() -> None:
    text = build_repair_prompt(
        [{"todo_id": "validate.xss", "check": "evidence_present", "reason": "missing"}],
        attempt=1,
        max_attempts=2,
    )
    assert "validate.xss" in text
    assert "acceptance-repair" in text
