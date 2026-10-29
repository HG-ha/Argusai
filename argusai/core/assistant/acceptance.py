"""Deterministic acceptance closed loop (Loom22 §8.5).

When the model ends a turn without tool calls while validation todos remain
unfinished (or completed without evidence), runtime verifies criteria before
committing ``done``. Failures enter a bounded repair prompt or a user decision.
"""

from __future__ import annotations

from typing import Any

VALIDATOR_VERSION = "argusai.acceptance.v1"
DEFAULT_MAX_REPAIR_ATTEMPTS = 2


def _is_validation_todo(todo: dict[str, Any]) -> bool:
    kind = str(todo.get("kind") or "").strip().lower()
    phase = str(todo.get("phase") or "").strip().lower()
    todo_id = str(todo.get("id") or "").strip().lower()
    return kind == "validation" or phase == "validation" or todo_id.startswith("validate.")


def unfinished_validation_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in todos or []:
        if not isinstance(raw, dict):
            continue
        if not _is_validation_todo(raw):
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status in {"completed", "cancelled", "done"}:
            # Completed still needs evidence check below via verify().
            continue
        if status in {"pending", "in_progress", "blocked", "ready"}:
            out.append(raw)
    return out


def _has_evidence(todo: dict[str, Any]) -> bool:
    if str(todo.get("evidence") or "").strip():
        return True
    for key in ("evidence_refs", "artifact_refs", "finding_refs"):
        refs = todo.get(key)
        if isinstance(refs, list) and any(str(x).strip() for x in refs):
            return True
    return False


def verify_validation_plan(todos: list[dict[str, Any]]) -> dict[str, Any]:
    """Run deterministic checks over validation todos.

    Passes when there are no open validation todos *and* every completed
    validation todo carries at least one evidence/artifact/finding ref (or
    free-text evidence). Open todos always fail.
    """
    failures: list[dict[str, Any]] = []
    checked = 0
    for raw in todos or []:
        if not isinstance(raw, dict) or not _is_validation_todo(raw):
            continue
        checked += 1
        todo_id = str(raw.get("id") or "")
        status = str(raw.get("status") or "pending").strip().lower()
        if status in {"cancelled"}:
            continue
        if status not in {"completed", "done"}:
            failures.append({
                "todo_id": todo_id,
                "check": "validation_complete",
                "reason": f"validation todo still {status or 'pending'}",
            })
            continue
        if not _has_evidence(raw):
            failures.append({
                "todo_id": todo_id,
                "check": "evidence_present",
                "reason": "completed validation todo lacks evidence_refs/artifact_refs/evidence",
            })
    return {
        "ok": not failures,
        "passed": not failures,
        "validator_version": VALIDATOR_VERSION,
        "checked": checked,
        "failures": failures,
    }


def build_repair_prompt(failures: list[dict[str, Any]], *, attempt: int, max_attempts: int) -> str:
    lines = [
        "[acceptance-repair]",
        f"Deterministic validation failed (attempt {attempt}/{max_attempts}, validator={VALIDATOR_VERSION}).",
        "Do NOT claim the task is done. Fix ONLY the failed criteria below:",
    ]
    for item in failures[:12]:
        lines.append(f"- {item.get('todo_id')}: {item.get('reason')} (check={item.get('check')})")
    lines.append(
        "Attach evidence via complete_todo_with_refs (evidence_refs / artifact_refs) "
        "or update_todo with concrete evidence, then continue. Do not restate prior success."
    )
    return "\n".join(lines)
