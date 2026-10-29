"""GUI API for user-level persistent todos."""

from __future__ import annotations

from typing import Any


class UserTodosMixin:
    def user_todo_list(self, status: str = "", tag: str = "", limit: int = 100) -> dict[str, Any]:
        from argusai.core.workspace.user_todos import list_todos

        root = self.settings_mgr.get().data_root
        items = list_todos(root, status=status or None, tag=tag or None, limit=int(limit or 100))
        return {"ok": True, "items": items}

    def user_todo_create(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.workspace.user_todos import create_todo

        data = payload if isinstance(payload, dict) else {}
        item = create_todo(
            self.settings_mgr.get().data_root,
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            due_at=data.get("due_at"),
            tags=data.get("tags"),
            status=str(data.get("status") or "pending"),
            source_session_id=str(data.get("source_session_id") or ""),
            created_by="user",
        )
        return {"ok": True, "todo": item}

    def user_todo_update(self, todo_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.workspace.user_todos import update_todo

        data = payload if isinstance(payload, dict) else {}
        item = update_todo(
            self.settings_mgr.get().data_root,
            todo_id,
            title=data.get("title"),
            body=data.get("body"),
            due_at=data.get("due_at"),
            tags=data.get("tags"),
            status=data.get("status"),
            clear_due_at=bool(data.get("clear_due_at")),
        )
        return {"ok": True, "todo": item}

    def user_todo_delete(self, todo_id: str) -> dict[str, Any]:
        from argusai.core.workspace.user_todos import delete_todo

        removed = delete_todo(self.settings_mgr.get().data_root, todo_id)
        return {"ok": True, "deleted": removed}
