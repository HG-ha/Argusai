"""Persistent user-level todos (cross-session), separate from assessment-plan todos."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


STATUSES = ("pending", "in_progress", "completed", "cancelled")


def user_todos_dir(data_root: str | Path) -> Path:
    root = Path(data_root).expanduser().resolve()
    path = (root / "user_todos").resolve()
    if root not in path.parents and path != root:
        raise ValueError("invalid user_todos path")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _store_path(data_root: str | Path) -> Path:
    return user_todos_dir(data_root) / "todos.json"


def _now() -> float:
    return time.time()


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [x.strip() for x in value.replace(",", " ").split() if x.strip()]
    elif isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
    else:
        items = []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item[:80])
    return out[:20]


def _normalize_status(value: Any, *, default: str = "pending") -> str:
    text = str(value or default).strip().lower()
    return text if text in STATUSES else default


def load_all(data_root: str | Path) -> list[dict[str, Any]]:
    path = _store_path(data_root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [dict(x) for x in items if isinstance(x, dict)]


# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op. The Todos panel has no poll, so agent/cron edits
# would otherwise only show on remount.
_UI_NOTIFY = None


def set_ui_notify(callback) -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


def save_all(data_root: str | Path, items: list[dict[str, Any]]) -> None:
    path = _store_path(data_root)
    payload = {"version": 1, "updated_at": _now(), "items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cb = _UI_NOTIFY
    if cb is not None:
        try:
            cb("user_todos", {})
        except Exception:
            pass


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "body": str(item.get("body") or ""),
        "status": _normalize_status(item.get("status")),
        "due_at": item.get("due_at"),
        "tags": list(item.get("tags") or []),
        "source_session_id": str(item.get("source_session_id") or ""),
        "created_by": str(item.get("created_by") or "user"),
        "created_at": float(item.get("created_at") or 0),
        "updated_at": float(item.get("updated_at") or 0),
    }


def list_todos(
    data_root: str | Path,
    *,
    status: str | None = None,
    tag: str | None = None,
    due_before: float | None = None,
    due_after: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    items = [_public_item(x) for x in load_all(data_root)]
    if status:
        want = _normalize_status(status)
        items = [x for x in items if x["status"] == want]
    if tag:
        needle = str(tag).strip().lower()
        items = [x for x in items if needle in {t.lower() for t in x.get("tags") or []}]
    if due_before is not None:
        items = [
            x for x in items
            if x.get("due_at") is not None and float(x["due_at"]) <= float(due_before)
        ]
    if due_after is not None:
        items = [
            x for x in items
            if x.get("due_at") is not None and float(x["due_at"]) >= float(due_after)
        ]
    items.sort(key=lambda x: (
        x.get("due_at") is None,
        float(x.get("due_at") or 0),
        -float(x.get("updated_at") or 0),
    ))
    return items[: max(1, min(int(limit or 50), 200))]


def get_todo(data_root: str | Path, todo_id: str) -> dict[str, Any] | None:
    tid = str(todo_id or "").strip()
    if not tid:
        return None
    for item in load_all(data_root):
        if str(item.get("id") or "") == tid:
            return _public_item(item)
    return None


def create_todo(
    data_root: str | Path,
    *,
    title: str,
    body: str = "",
    due_at: float | None = None,
    tags: list[str] | None = None,
    status: str = "pending",
    source_session_id: str = "",
    created_by: str = "ai",
) -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    now = _now()
    item = {
        "id": uuid.uuid4().hex[:12],
        "title": title[:300],
        "body": str(body or "")[:4000],
        "status": _normalize_status(status),
        "due_at": float(due_at) if due_at is not None else None,
        "tags": _normalize_tags(tags or []),
        "source_session_id": str(source_session_id or "")[:128],
        "created_by": str(created_by or "ai")[:32],
        "created_at": now,
        "updated_at": now,
    }
    items = load_all(data_root)
    items.append(item)
    save_all(data_root, items)
    return _public_item(item)


def update_todo(
    data_root: str | Path,
    todo_id: str,
    *,
    title: str | None = None,
    body: str | None = None,
    due_at: float | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    clear_due_at: bool = False,
) -> dict[str, Any]:
    tid = str(todo_id or "").strip()
    if not tid:
        raise ValueError("todo_id is required")
    items = load_all(data_root)
    found = None
    for item in items:
        if str(item.get("id") or "") != tid:
            continue
        if title is not None:
            text = str(title).strip()
            if text:
                item["title"] = text[:300]
        if body is not None:
            item["body"] = str(body)[:4000]
        if tags is not None:
            item["tags"] = _normalize_tags(tags)
        if status is not None:
            item["status"] = _normalize_status(status)
        if clear_due_at:
            item["due_at"] = None
        elif due_at is not None:
            item["due_at"] = float(due_at)
        item["updated_at"] = _now()
        found = _public_item(item)
        break
    if not found:
        raise ValueError("todo not found")
    save_all(data_root, items)
    return found


def delete_todo(data_root: str | Path, todo_id: str) -> dict[str, Any]:
    tid = str(todo_id or "").strip()
    if not tid:
        raise ValueError("todo_id is required")
    items = load_all(data_root)
    kept: list[dict[str, Any]] = []
    removed = None
    for item in items:
        if str(item.get("id") or "") == tid:
            removed = _public_item(item)
            continue
        kept.append(item)
    if not removed:
        raise ValueError("todo not found")
    save_all(data_root, kept)
    return removed
