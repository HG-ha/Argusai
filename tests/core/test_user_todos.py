"""Tests for user-level persistent todos."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from argusai.core.workspace import user_todos as ut


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path


def test_create_list_update_complete_delete(data_root: Path) -> None:
    item = ut.create_todo(data_root, title="Review SSRF", tags=["web", "ssrf"])
    assert item["id"]
    assert item["status"] == "pending"

    listed = ut.list_todos(data_root, status="pending")
    assert any(x["id"] == item["id"] for x in listed)

    updated = ut.update_todo(data_root, item["id"], status="in_progress", body="check callback")
    assert updated["status"] == "in_progress"

    done = ut.update_todo(data_root, item["id"], status="completed")
    assert done["status"] == "completed"

    removed = ut.delete_todo(data_root, item["id"])
    assert removed["id"] == item["id"]
    assert ut.get_todo(data_root, item["id"]) is None


def test_bulk_delete_requires_label(data_root: Path) -> None:
    from argusai.core.tools.user_todos import user_todo_bulk_delete

    class _Api:
        class _Mgr:
            def get(self):
                class _S:
                    data_root = str(data_root)
                return _S()
        settings_mgr = _Mgr()

    ut.create_todo(data_root, title="a")
    r = user_todo_bulk_delete(_Api(), status="pending", confirm_label="nope")
    assert r["ok"] is False
