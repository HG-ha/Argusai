"""Tests for assistant sandbox batch file operations."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from argusai.gui.assistant_sessions import AssistantSessionsMixin
from argusai.gui.assistant_tools import AssistantToolsMixin
from argusai.gui.files import FilesMixin


class _SettingsMgr:
    def __init__(self, sessions_dir) -> None:
        self._settings = SimpleNamespace(sessions_dir=str(sessions_dir))

    def get(self):
        return self._settings


class _Api(AssistantSessionsMixin, AssistantToolsMixin, FilesMixin):
    def __init__(self, sessions_dir) -> None:
        self.settings_mgr = _SettingsMgr(sessions_dir)
        self._assistant_sessions = {}
        self.kb = None


def test_export_and_delete_selected_sandbox_items(tmp_path) -> None:
    api = _Api(tmp_path)
    root = api._sessions_dir / "sess-a.sandbox"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "dir").mkdir()
    (root / "dir" / "b.txt").write_text("b", encoding="utf-8")

    out_dir = tmp_path / "exports"
    exported = api.export_assistant_sandbox_items("sess-a", ["a.txt", "dir"], str(out_dir))
    assert exported["ok"]
    assert exported["path"].startswith(str(out_dir))
    assert Path(exported["path"]).exists()

    denied = api.delete_assistant_sandbox_items("sess-a", ["a.txt"], confirm_label="NO")
    assert not denied["ok"]

    deleted = api.delete_assistant_sandbox_items("sess-a", ["a.txt"], confirm_label="DELETE_SANDBOX_ITEMS")
    assert deleted["ok"]
    assert deleted["removed"] == ["a.txt"]
    assert not (root / "a.txt").exists()


def test_move_selected_sandbox_items(tmp_path) -> None:
    api = _Api(tmp_path)
    root = api._sessions_dir / "sess-a.sandbox"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    moved = api.move_assistant_sandbox_items(
        "sess-a",
        ["a.txt"],
        "moved",
        confirm_label="MOVE_SANDBOX_ITEMS",
    )
    assert moved["ok"]
    assert moved["moved"] == [{"from": "a.txt", "to": "moved/a.txt"}]
    assert (root / "moved" / "a.txt").exists()


def test_export_single_sandbox_file(tmp_path) -> None:
    api = _Api(tmp_path)
    root = api._sessions_dir / "sess-a.sandbox"
    root.mkdir()
    (root / "note.txt").write_text("hello", encoding="utf-8")

    out = tmp_path / "out" / "note.txt"
    exported = api.export_assistant_sandbox_file("sess-a", "note.txt", str(out))
    assert exported["ok"]
    assert exported["path"] == str(out.resolve())
    assert out.read_text(encoding="utf-8") == "hello"
