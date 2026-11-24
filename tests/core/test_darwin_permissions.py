"""darwin_permissions unit tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("status", ["not_determined", "granted"])
def test_microphone_without_purpose_string_never_calls_capture_api(monkeypatch, status) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "microphone_status", lambda: status)
    monkeypatch.setattr(dp, "_microphone_usage_declared", lambda: False)
    monkeypatch.setattr(dp, "_audio_capture_api", lambda: pytest.fail("must not request without purpose string"))
    assert dp.request_microphone()["error_code"] == "macos_usage_description_missing"


def test_microphone_prompt_deduplicates_concurrent_requests(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "_MIC_REQUEST_PENDING", False)
    monkeypatch.setattr(dp, "microphone_status", lambda: "not_determined")
    monkeypatch.setattr(dp, "_microphone_usage_declared", lambda: True)
    callbacks = []
    device = SimpleNamespace(requestAccessForMediaType_completionHandler_=lambda media, callback: callbacks.append(callback))
    monkeypatch.setattr(dp, "_audio_capture_api", lambda: (device, "audio"))
    assert dp.request_microphone()["pending"]
    assert dp.request_microphone()["pending"]
    assert len(callbacks) == 1
    callbacks[0](False)
    assert dp._MIC_REQUEST_PENDING is False


def test_denied_microphone_does_not_prompt_again(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "microphone_status", lambda: "denied")
    monkeypatch.setattr(dp, "_audio_capture_api", lambda: pytest.fail("denied access must use Settings"))
    assert dp.request_microphone()["status"] == "denied"


def test_permission_center_preserves_unknown_and_does_not_change_desktop_readiness(monkeypatch) -> None:
    from argusai.core.workspace import darwin_notifications as dn

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(dp, "screen_capture_granted", lambda: True)
    monkeypatch.setattr(dp, "listen_event_status", lambda: "unknown")
    monkeypatch.setattr(dp, "microphone_status", lambda: "denied")
    monkeypatch.setattr(dn, "notification_status", lambda: "not_determined")
    monkeypatch.setattr(dp, "request_microphone", lambda: pytest.fail("status reads must not prompt"))
    monkeypatch.setattr(dn, "request_notifications", lambda: pytest.fail("status reads must not prompt"))
    snap = dp.permission_center_snapshot()
    rows = {row["id"]: row for row in snap["permissions"]}
    assert dp.check_all()["all_granted"] is True
    assert rows["microphone"]["granted"] is False
    assert rows["listen_event"]["granted"] is None
    for key in ("automation", "files_and_folders", "local_network", "full_disk_access"):
        assert rows[key]["granted"] is None
        assert rows[key]["requestable"] is False
    assert rows["notifications"]["requestable"] is True


@pytest.mark.parametrize("pane", ["microphone", "notifications", "automation", "files_and_folders", "full_disk_access", "local_network"])
def test_system_settings_supports_additional_panes(monkeypatch, pane) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(dp.subprocess, "run", lambda args, **kw: calls.append(args))
    result = dp.open_privacy_settings(pane)
    assert result["ok"] and result["pane"] == pane
    assert calls == [["open", dp._SETTINGS_URLS[pane][0]]]


def test_invalid_privacy_pane_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected settings launch"))
    assert not dp.open_privacy_settings("unknown?x=anything")["ok"]


@pytest.mark.parametrize("pane", ["automation", "files_and_folders", "local_network", "full_disk_access", "camera"])
def test_per_resource_permissions_never_probe_for_authorization(monkeypatch, pane) -> None:
    monkeypatch.setattr(dp.subprocess, "run", lambda *a, **kw: pytest.fail("must not trigger by probing"))
    assert dp.request_permission(pane)["error_code"] == "macos_permission_requires_settings"
