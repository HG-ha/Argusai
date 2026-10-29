"""User-level persistent todos (cross-session)."""

from __future__ import annotations

import time
from typing import Any

from .common import *


def _user_todos_root(api: Any) -> str:
    return str(api.settings_mgr.get().data_root)


def user_todo_create(
    api: Any,
    title: str = "",
    body: str = "",
    due_at: float | None = None,
    tags: list | None = None,
    status: str = "pending",
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a user-level todo that persists across assistant sessions."""
    from argusai.core.workspace.user_todos import create_todo

    item = create_todo(
        _user_todos_root(api),
        title=title,
        body=body,
        due_at=due_at,
        tags=tags,
        status=status,
        source_session_id=str(kwargs.get("_session_id") or ""),
        created_by="ai",
    )
    return {
        "ok": True,
        "todo": item,
        "ui_hint": "user_todo_card",
        "message": f"已创建待办：{item['title']}",
    }


def user_todo_list(
    api: Any,
    status: str = "",
    tag: str = "",
    limit: int = 50,
    **kwargs: Any,
) -> dict[str, Any]:
    """List user-level todos with optional status/tag filters."""
    from argusai.core.workspace.user_todos import list_todos

    items = list_todos(
        _user_todos_root(api),
        status=status or None,
        tag=tag or None,
        limit=_safe_int(limit, 50, 1, 200),
    )
    return {"ok": True, "items": items, "count": len(items), "ui_hint": "user_todo_list"}


def user_todo_update(
    api: Any,
    todo_id: str = "",
    title: str = "",
    body: str = "",
    due_at: float | None = None,
    tags: list | None = None,
    status: str = "",
    clear_due_at: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update fields on an existing user-level todo."""
    from argusai.core.workspace.user_todos import update_todo

    item = update_todo(
        _user_todos_root(api),
        todo_id,
        title=title or None,
        body=body if body != "" else None,
        due_at=due_at,
        tags=tags,
        status=status or None,
        clear_due_at=bool(clear_due_at),
    )
    return {"ok": True, "todo": item, "ui_hint": "user_todo_card"}


def user_todo_complete(api: Any, todo_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Mark a user-level todo as completed."""
    from argusai.core.workspace.user_todos import update_todo

    item = update_todo(_user_todos_root(api), todo_id, status="completed")
    return {"ok": True, "todo": item, "ui_hint": "user_todo_card"}


def user_todo_delete(api: Any, todo_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a single user-level todo (requires confirmation)."""
    from argusai.core.workspace.user_todos import delete_todo

    removed = delete_todo(_user_todos_root(api), todo_id)
    return {"ok": True, "deleted": removed}


def user_todo_bulk_delete(
    api: Any,
    status: str = "",
    confirm_label: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Delete many user todos matching a status — never silent; needs explicit confirm_label."""
    from argusai.core.workspace.user_todos import delete_todo, list_todos, load_all, save_all

    label = str(confirm_label or "").strip()
    if label != "DELETE_USER_TODOS":
        return {
            "ok": False,
            "error": "bulk delete requires confirm_label='DELETE_USER_TODOS'",
        }
    root = _user_todos_root(api)
    if status:
        victims = list_todos(root, status=status, limit=200)
        ids = {v["id"] for v in victims}
        kept = [x for x in load_all(root) if str(x.get("id") or "") not in ids]
        save_all(root, kept)
        return {"ok": True, "deleted_count": len(ids)}
    return {"ok": False, "error": "status filter is required for bulk delete"}
