"""The log directory follows the configured data_root."""

from __future__ import annotations

from pathlib import Path

import pytest

from argusai.core.infra.logging_setup import (
    LoggingSettings,
    adopt_legacy_log_files,
    pending_legacy_log_files,
    resolve_log_dir,
)
from argusai.core.paths import SUBDIR_LOGS, PathManager


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the app root away from the developer's real one."""
    app_root = tmp_path / "app"
    monkeypatch.setattr("argusai.core.paths.default_app_root", lambda: app_root)
    monkeypatch.delenv("ARGUSAI_LOG_DIR", raising=False)
    return app_root


def test_resolve_log_dir_derives_from_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "custom-storage"
    assert resolve_log_dir(LoggingSettings(), data_root=str(data_root)) == data_root / SUBDIR_LOGS


def test_resolve_log_dir_defaults_under_default_data_root(tmp_path: Path) -> None:
    assert resolve_log_dir(LoggingSettings()) == PathManager().data_root / SUBDIR_LOGS


def test_explicit_log_dir_setting_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere" / "logs"
    resolved = resolve_log_dir(LoggingSettings(log_dir=str(explicit)), data_root=str(tmp_path / "d"))
    assert resolved == explicit


def test_env_override_wins_over_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = tmp_path / "env-logs"
    monkeypatch.setenv("ARGUSAI_LOG_DIR", str(env_dir))
    assert resolve_log_dir(LoggingSettings(), data_root=str(tmp_path / "d")) == env_dir


def test_resolve_log_dir_reads_persisted_data_root(tmp_path: Path) -> None:
    import json

    data_root = tmp_path / "persisted"
    settings_path = PathManager().settings_path
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"data_root": str(data_root)}), encoding="utf-8")
    assert resolve_log_dir(LoggingSettings()) == data_root / SUBDIR_LOGS


def test_resolve_log_dir_survives_unreadable_settings(tmp_path: Path) -> None:
    settings_path = PathManager().settings_path
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{not json", encoding="utf-8")
    assert resolve_log_dir(LoggingSettings()) == PathManager().data_root / SUBDIR_LOGS


def test_legacy_logs_are_adopted_once(tmp_path: Path, _isolated_roots: Path) -> None:
    legacy = _isolated_roots / SUBDIR_LOGS
    legacy.mkdir(parents=True)
    (legacy / "argusai.log").write_text("old", encoding="utf-8")
    (legacy / "argusai.log.1").write_text("older", encoding="utf-8")
    (legacy / "notes.txt").write_text("keep me", encoding="utf-8")
    target = tmp_path / "data" / SUBDIR_LOGS
    target.mkdir(parents=True)

    moved = adopt_legacy_log_files(target)

    assert sorted(moved) == ["argusai.log", "argusai.log.1"]
    assert (target / "argusai.log").read_text(encoding="utf-8") == "old"
    assert not (legacy / "argusai.log").exists()
    assert (legacy / "notes.txt").is_file()
    assert pending_legacy_log_files(target) == []


def test_adoption_skipped_when_target_already_has_logs(tmp_path: Path, _isolated_roots: Path) -> None:
    legacy = _isolated_roots / SUBDIR_LOGS
    legacy.mkdir(parents=True)
    (legacy / "argusai.log").write_text("old", encoding="utf-8")
    target = tmp_path / "data" / SUBDIR_LOGS
    target.mkdir(parents=True)
    (target / "argusai.log").write_text("current", encoding="utf-8")

    assert adopt_legacy_log_files(target) == []
    assert (target / "argusai.log").read_text(encoding="utf-8") == "current"
    assert pending_legacy_log_files(target) == ["argusai.log"]


def test_adoption_is_a_noop_when_legacy_is_the_target(_isolated_roots: Path) -> None:
    legacy = _isolated_roots / SUBDIR_LOGS
    legacy.mkdir(parents=True)
    (legacy / "argusai.log").write_text("old", encoding="utf-8")
    assert adopt_legacy_log_files(legacy) == []
    assert (legacy / "argusai.log").is_file()


def test_data_root_preview_warns_that_logs_stay_behind(tmp_path: Path) -> None:
    from argusai.core.workspace.data_root_migrate import preview_data_root_change

    src = tmp_path / "old"
    (src / SUBDIR_LOGS).mkdir(parents=True)
    (src / SUBDIR_LOGS / "argusai.log").write_text("history", encoding="utf-8")

    preview = preview_data_root_change(src, tmp_path / "new")

    warning = next(w for w in preview["warnings"] if w["code"] == "log_dir_not_transferred")
    assert str(src / SUBDIR_LOGS) in warning["path"]
    assert warning["code"] == "log_dir_not_transferred"
    assert SUBDIR_LOGS not in [row["name"] for row in preview["subdirs"]]


def test_data_root_preview_silent_without_existing_logs(tmp_path: Path) -> None:
    from argusai.core.workspace.data_root_migrate import preview_data_root_change

    src = tmp_path / "old"
    src.mkdir()
    preview = preview_data_root_change(src, tmp_path / "new")
    assert not [w for w in preview["warnings"] if w["code"] == "log_dir_not_transferred"]
