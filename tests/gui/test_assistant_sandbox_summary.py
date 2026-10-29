"""Tests for the read-only assistant sandbox summary API."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.gui.assistant_sessions import AssistantSessionsMixin
from argusai.gui.assistant_tools import AssistantToolsMixin


class _SettingsMgr:
    def __init__(self, sessions_dir: Path) -> None:
        self._settings = SimpleNamespace(sessions_dir=str(sessions_dir))

    def get(self):
        return self._settings


class _Api(AssistantSessionsMixin, AssistantToolsMixin):
    def __init__(self, sessions_dir: Path) -> None:
        self.settings_mgr = _SettingsMgr(sessions_dir)
        self._assistant_sessions = {}
        self.kb = None


def test_sandbox_summary_does_not_create_missing_sandbox(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    api = _Api(sessions_dir)
    sandbox = sessions_dir / "missing.sandbox"

    result = api.get_assistant_sandbox_summary("missing")

    assert result == {
        "ok": True,
        "session_id": "missing",
        "exists": False,
        "directory": str(sandbox.resolve()),
        "file_count": 0,
        "dir_count": 0,
        "total_size": 0,
        "latest_mtime": 0.0,
        "root_items": [],
    }
    assert not sandbox.exists()


def test_sandbox_summary_reports_recursive_stats_and_excludes_venv(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    root = sessions_dir / "sess-a.sandbox"
    nested = root / "nested"
    empty = root / "empty"
    venv = root / ".venv" / "Lib"
    nested.mkdir(parents=True)
    empty.mkdir()
    venv.mkdir(parents=True)
    top_file = root / "top.txt"
    nested_file = nested / "data.bin"
    top_file.write_bytes(b"abc")
    nested_file.write_bytes(b"12345")
    (venv / "dependency.py").write_bytes(b"x" * 100)
    os.utime(top_file, (100.0, 100.0))
    os.utime(nested_file, (200.0, 200.0))

    result = _Api(sessions_dir).get_assistant_sandbox_summary("sess-a")

    assert result["ok"] is True
    assert result["exists"] is True
    assert result["directory"] == str(root.resolve())
    assert result["file_count"] == 2
    assert result["dir_count"] == 2
    assert result["total_size"] == 8
    assert result["latest_mtime"] == pytest.approx(
        max(top_file.stat().st_mtime, nested_file.stat().st_mtime)
    )
    assert {item["relative_path"] for item in result["root_items"]} == {
        "empty",
        "nested",
        "top.txt",
    }


def test_sandbox_summary_rejects_session_id_path_escape(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    outside = tmp_path / "escape.sandbox"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    api = _Api(sessions_dir)

    result = api.get_assistant_sandbox_summary("../escape")

    assert result["ok"] is False
    assert result["error"] == "invalid session_id"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not sessions_dir.exists()
