"""darwin_permissions unit tests."""

from __future__ import annotations

import sys
from unittest.mock import patch

from argusai.core.workspace import darwin_permissions as dp


def test_check_all_non_darwin() -> None:
    with patch.object(sys, "platform", "win32"):
        snap = dp.check_all()
    assert snap["ok"] is True
    assert snap["permissions"] == []


def test_check_all_darwin_structure() -> None:
    with patch.object(sys, "platform", "darwin"), patch.object(
        dp, "accessibility_trusted", return_value=False
    ), patch.object(dp, "screen_capture_granted", return_value=True):
        snap = dp.check_all()
    assert snap["platform"] == "darwin"
    assert "accessibility" in snap["missing"]
    ids = [p["id"] for p in snap["permissions"]]
    assert "accessibility" in ids
    assert "screen_capture" in ids


def test_open_privacy_settings_non_mac() -> None:
    with patch.object(sys, "platform", "linux"):
        r = dp.open_privacy_settings("accessibility")
    assert r["ok"] is False


def test_permission_denied_payload() -> None:
    with patch.object(sys, "platform", "darwin"), patch.object(
        dp, "check_all", return_value={
            "permissions": [
                {"id": "accessibility", "label": "辅助功能", "granted": False, "hint": "h", "settings_url": "x://a"},
            ],
        }
    ):
        payload = dp.permission_denied_payload("accessibility")
    assert payload["id"] == "accessibility"
    assert "辅助功能" in payload["user_message"]


def test_request_accessibility_prompt_once(monkeypatch) -> None:
    dp._AX_PROMPT_ATTEMPTED = False
    calls: list[int] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "accessibility_trusted", lambda: False)
    monkeypatch.setattr(dp, "_ax_prompt_in_process", lambda: calls.append(1) or False)
    assert dp.request_accessibility_prompt() is False
    assert dp.request_accessibility_prompt() is False
    assert calls == [1]


def test_listen_event_denied_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(dp, "screen_capture_granted", lambda: True)
    monkeypatch.setattr(dp, "listen_event_status", lambda: "denied")
    snap = dp.check_all()
    assert "listen_event" in snap["missing"]
