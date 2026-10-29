"""Settings → logging diagnostics bridge methods."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from argusai.core.infra.logging_setup import LoggingSettings
from argusai.core.paths import SUBDIR_LOGS
from argusai.gui.files import FilesMixin
from argusai.gui.settings import SettingsMixin


class _Api(SettingsMixin, FilesMixin):
    def __init__(self, data_root: Path, logging: LoggingSettings | None = None) -> None:
        settings = SimpleNamespace(
            data_root=str(data_root),
            logging=logging or LoggingSettings(),
        )
        self.settings_mgr = SimpleNamespace(get=lambda: settings)


@pytest.fixture(autouse=True)
def _uninitialised_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the settings-derived fallback instead of this process's handlers."""
    monkeypatch.setattr("argusai.core.infra.logging_setup.is_initialised", lambda: False)
    monkeypatch.setattr("argusai.core.paths.default_app_root", lambda: tmp_path / "app")
    monkeypatch.delenv("ARGUSAI_LOG_DIR", raising=False)


def test_logging_status_reports_dir_under_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "storage"
    log_dir = data_root / SUBDIR_LOGS
    log_dir.mkdir(parents=True)
    (log_dir / "argusai.log").write_text("line", encoding="utf-8")

    status = _Api(data_root).logging_status()

    assert status["ok"] is True
    assert status["log_dir"] == str(log_dir)
    assert [item["name"] for item in status["files"]] == ["argusai.log"]


def test_logging_status_follows_explicit_override(tmp_path: Path) -> None:
    explicit = tmp_path / "somewhere-else"
    api = _Api(tmp_path / "storage", LoggingSettings(log_dir=str(explicit)))
    assert api.logging_status()["log_dir"] == str(explicit)


def test_open_log_dir_creates_and_reveals_active_dir(tmp_path: Path) -> None:
    data_root = tmp_path / "storage"
    api = _Api(data_root)
    expected = data_root / SUBDIR_LOGS

    with patch.object(FilesMixin, "open_folder", return_value={"ok": True}) as open_folder:
        result = api.open_log_dir()

    assert result == {"ok": True, "path": str(expected)}
    assert expected.is_dir()
    assert open_folder.call_args.args[0] == str(expected)


def test_open_log_dir_surfaces_open_failure(tmp_path: Path) -> None:
    api = _Api(tmp_path / "storage")

    with patch.object(FilesMixin, "open_folder", return_value={"ok": False, "error": "boom"}):
        result = api.open_log_dir()

    assert result["ok"] is False
    assert result["error"] == "boom"
    assert result["path"].endswith(SUBDIR_LOGS)


def test_default_dirs_exposes_logs_subdir(tmp_path: Path) -> None:
    dirs = _Api(tmp_path).default_dirs()
    assert dirs["subdir_names"]["logs"] == SUBDIR_LOGS
    assert dirs["logs_dir"] == str(Path(dirs["data_root"]) / SUBDIR_LOGS)
    assert SUBDIR_LOGS not in dirs["persistent_subdirs"]
