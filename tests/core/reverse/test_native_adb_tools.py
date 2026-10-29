"""Integration tests for adb_* agent tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from argusai.core.tools.native_reverse import adb_devices, adb_logcat, adb_pull


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.artifacts: list[dict] = []

    def add_artifact(self, job_id: str, *, content: bytes | str, kind: str, filename: str = "", tool: str = "", note: str = "", **kwargs):
        rec = {"id": f"art-{len(self.artifacts)+1}", "job_id": job_id, "kind": kind, "tool": tool, "note": note}
        self.artifacts.append(rec)
        return rec

    def get(self, job_id: str):
        return {"_dir": str(self.root / job_id)}


@patch("argusai.core.tools.native_reverse.run_adb_devices")
@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
def test_adb_devices_delegates(_preflight: MagicMock, mock_run: MagicMock) -> None:
    mock_run.return_value = {"ok": True, "devices_text": "emulator-5554 device"}
    api = MagicMock()
    out = adb_devices(api)
    assert out["ok"] is True
    mock_run.assert_called_once()


@patch("argusai.core.tools.native_reverse.run_adb_pull")
@patch("argusai.core.tools.native_reverse._resolve_reverse_store")
@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
def test_adb_pull_attaches_small_file(
    _preflight: MagicMock,
    mock_store_fn: MagicMock,
    mock_pull: MagicMock,
    tmp_path: Path,
) -> None:
    job_id = "rev-test"
    job_dir = tmp_path / job_id
    job_dir.mkdir(parents=True)
    pulled = job_dir / "adb" / "notes.txt"
    pulled.parent.mkdir(parents=True)
    pulled.write_text("secret", encoding="utf-8")
    store = _FakeStore(tmp_path)
    mock_store_fn.return_value = store
    mock_pull.return_value = {"ok": True, "local_path": str(pulled), "remote_path": "/sdcard/notes.txt"}

    api = MagicMock()
    out = adb_pull(api, remote_path="/sdcard/notes.txt", job_id=job_id)
    assert out["ok"] is True
    assert out["attached"]["artifact_ref"].startswith("rev://")
    assert store.artifacts[0]["tool"] == "adb_pull"


@patch("argusai.core.tools.native_reverse.run_adb_logcat")
@patch("argusai.core.tools.native_reverse._resolve_reverse_store")
@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
def test_adb_logcat_attaches_json(
    _preflight: MagicMock,
    mock_store_fn: MagicMock,
    mock_logcat: MagicMock,
    tmp_path: Path,
) -> None:
    store = _FakeStore(tmp_path)
    mock_store_fn.return_value = store
    mock_logcat.return_value = {"ok": True, "logcat_text": "line", "line_count": 1}

    api = MagicMock()
    out = adb_logcat(api, job_id="rev-test", duration_seconds=5)
    assert out["ok"] is True
    assert store.artifacts[0]["tool"] == "adb_logcat"
