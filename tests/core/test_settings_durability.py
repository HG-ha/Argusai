from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from argusai.core.ai import AuxiliaryAIEndpoint
from argusai.core.settings import (
    SETTINGS_BACKUP_COUNT,
    SettingsCorruptionError,
    SettingsManager,
)
from argusai.gui.settings import SettingsMixin
from argusai.core.tools.user_profile_tools import user_profile_apply_update


def test_settings_atomic_backup_and_corruption_recovery(tmp_path) -> None:
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    settings = manager.get()
    settings.kb_trash_retention_days = 31
    manager.save(settings)
    settings.kb_trash_retention_days = 47
    manager.save(settings)

    assert json.loads(path.read_text(encoding="utf-8"))["kb_trash_retention_days"] == 47
    assert (tmp_path / "settings.json.bak.1").exists()
    assert not list(tmp_path.glob(".settings.json.*.tmp"))

    path.write_text("{broken", encoding="utf-8")
    recovered = SettingsManager(path).load()
    assert recovered.kb_trash_retention_days == 31
    assert json.loads(path.read_text(encoding="utf-8"))["kb_trash_retention_days"] == 31


def test_settings_refuses_unrecoverable_corruption(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SettingsCorruptionError):
        SettingsManager(path).load()


def test_settings_keeps_only_ten_rotating_backups(tmp_path) -> None:
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    settings = manager.get()
    for value in range(SETTINGS_BACKUP_COUNT + 4):
        settings.kb_trash_retention_days = value
        manager.save(settings)

    backups = sorted(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == SETTINGS_BACKUP_COUNT
    assert {item.name for item in backups} == {
        f"settings.json.bak.{index}"
        for index in range(1, SETTINGS_BACKUP_COUNT + 1)
    }


def test_unchanged_settings_do_not_rotate_backups(tmp_path) -> None:
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    settings = manager.get()

    manager.save(settings)
    manager.save(settings)

    assert not list(tmp_path.glob("settings.json.bak.*"))


def test_transactional_updates_merge_latest_state(tmp_path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    manager.get()
    start = threading.Event()

    def update_retention() -> None:
        start.wait()
        manager.update(
            lambda current: (
                setattr(current, "kb_trash_retention_days", 73) or current
            )
        )

    def update_window() -> None:
        start.wait()
        manager.update(
            lambda current: (
                setattr(current, "gui_window_width", 1440) or current
            )
        )

    threads = [
        threading.Thread(target=update_retention),
        threading.Thread(target=update_window),
    ]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join()

    loaded = SettingsManager(manager.path).load()
    assert loaded.kb_trash_retention_days == 73
    assert loaded.gui_window_width == 1440


def test_failed_transaction_does_not_mutate_cached_settings(tmp_path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    manager.get()

    def fail(current):
        current.kb_trash_retention_days = 99
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        manager.update(fail)

    assert manager.get().kb_trash_retention_days == 30
    assert SettingsManager(manager.path).load().kb_trash_retention_days == 30


class _SettingsApi(SettingsMixin):
    def __init__(self, manager: SettingsManager) -> None:
        self.settings_mgr = manager

    @staticmethod
    def _channels_runtime():
        return type("Runtime", (), {"refresh": lambda self: None})()


def test_settings_partial_save_preserves_aux_key_and_window_geometry(tmp_path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.gui_window_x = 100
    settings.gui_window_y = 200
    settings.gui_window_width = 1280
    settings.gui_window_height = 800
    settings.ai_auxiliary_vision = AuxiliaryAIEndpoint(
        enabled=True,
        name="vision",
        base_url="https://vision.example/v1",
        api_key="keep-secret",
        model="vision-model",
    )
    manager.save(settings)
    api = _SettingsApi(manager)

    assert api.save_settings({
        "ai": {
            "auxiliary": {
                "vision": {
                    "enabled": False,
                    "api_key": "",
                },
            },
        },
    })["ok"]
    loaded = SettingsManager(manager.path).load()
    assert loaded.ai_auxiliary_vision.api_key == "keep-secret"
    assert loaded.ai_auxiliary_vision.enabled is False
    assert (
        loaded.gui_window_x,
        loaded.gui_window_y,
        loaded.gui_window_width,
        loaded.gui_window_height,
    ) == (100, 200, 1280, 800)


def test_settings_explicit_aux_key_clear(tmp_path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.ai_auxiliary_asr = AuxiliaryAIEndpoint(
        enabled=True,
        base_url="https://asr.example/v1",
        api_key="remove-me",
        model="asr-model",
    )
    manager.save(settings)

    result = _SettingsApi(manager).save_settings({
        "ai": {
            "auxiliary": {
                "asr": {
                    "clear_api_key": True,
                },
            },
        },
    })
    assert result["ok"]
    assert SettingsManager(manager.path).load().ai_auxiliary_asr.api_key == ""
    assert "clear_api_key" not in manager.path.read_text(encoding="utf-8")


def test_profile_transaction_preserves_auxiliary_endpoint(tmp_path) -> None:
    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.ai_auxiliary_vision = AuxiliaryAIEndpoint(
        enabled=True,
        base_url="https://vision.example/v1",
        api_key="keep-secret",
        model="vision-model",
    )
    manager.save(settings)

    result = user_profile_apply_update(
        SimpleNamespace(settings_mgr=manager),
        patch={"notes": "prefer concise answers"},
        confirm_label="APPLY_USER_PROFILE",
    )

    assert result["ok"]
    loaded = SettingsManager(manager.path).load()
    assert loaded.ai_auxiliary_vision.api_key == "keep-secret"
    assert loaded.ai_auxiliary_vision.model == "vision-model"


def test_settings_api_reports_recovery_and_rejects_stale_version(tmp_path) -> None:
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    settings = manager.get()
    settings.kb_trash_retention_days = 41
    manager.save(settings)
    settings.kb_trash_retention_days = 52
    manager.save(settings)

    path.write_text("{broken", encoding="utf-8")
    api = _SettingsApi(manager)
    loaded = api.get_settings()

    assert loaded["kb_trash_retention_days"] == 41
    assert loaded["_settings_meta"]["recovered"] is True
    stale_version = loaded["_settings_meta"]["version"]

    manager.update(
        lambda current: (
            setattr(current, "assistant_max_agent_rounds", 123) or current
        )
    )
    result = api.save_settings({
        "_settings_version": stale_version,
        "kb_trash_retention_days": 77,
    })

    assert result["ok"] is False
    assert result["code"] == "settings_version_conflict"
    latest = SettingsManager(path).load()
    assert latest.kb_trash_retention_days == 41
    assert latest.assistant_max_agent_rounds == 123


def test_window_geometry_does_not_conflict_with_settings_autosave(tmp_path) -> None:
    """Resizing the main window must not invalidate an open Settings panel."""
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    api = _SettingsApi(manager)
    loaded = api.get_settings()
    version = loaded["_settings_meta"]["version"]

    manager.update(
        lambda current: (
            setattr(current, "gui_window_width", 1600)
            or setattr(current, "gui_window_height", 900)
            or current
        )
    )
    assert manager.revision() == version

    result = api.save_settings({
        "_settings_version": version,
        "kb_trash_retention_days": 77,
    })
    assert result["ok"] is True
    latest = SettingsManager(path).load()
    assert latest.kb_trash_retention_days == 77
    assert latest.gui_window_width == 1600
    assert latest.gui_window_height == 900
