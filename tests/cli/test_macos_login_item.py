"""Unit tests for macOS Login Item helpers (safe off macOS)."""

from __future__ import annotations

import sys

from argusai.cli.macos_login_item import (
    APP_BUNDLE_ID,
    STATUS_ENABLED,
    STATUS_REQUIRES_APPROVAL,
    app_bundle_path,
    available_for_host,
    launched_hidden_at_login,
    login_item_status,
    status_name,
)


def test_bundle_id_matches_packaging() -> None:
    assert APP_BUNDLE_ID == "com.argusai.app"


def test_status_name_mapping() -> None:
    assert status_name(STATUS_ENABLED) == "enabled"
    assert status_name(STATUS_REQUIRES_APPROVAL) == "requires_approval"
    assert status_name(99) == "unknown"


def test_app_bundle_path_detects_enclosing_app(tmp_path) -> None:
    exe = tmp_path / "ArgusAI.app" / "Contents" / "MacOS" / "ArgusAI"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    found = app_bundle_path(exe)
    assert found is not None
    assert found.name == "ArgusAI.app"
    assert app_bundle_path(tmp_path / "python.exe") is None


def test_helpers_are_inert_off_macos() -> None:
    if sys.platform == "darwin":
        return
    assert available_for_host() is False
    assert launched_hidden_at_login() is False
    status = login_item_status()
    assert status["available"] is False
    assert status["registered"] is False
