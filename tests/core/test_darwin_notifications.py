from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from argusai.core.workspace import darwin_notifications as dn


@pytest.mark.parametrize("native,status", [(0, "not_determined"), (1, "denied"), (2, "granted"), (3, "granted"), (99, "unknown")])
def test_notification_status_is_read_only(monkeypatch, native, status) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    center = SimpleNamespace(getNotificationSettingsWithCompletionHandler_=lambda callback: callback(SimpleNamespace(authorizationStatus=lambda: native)))
    monkeypatch.setattr(dn, "_notification_center", lambda: center)
    assert dn.notification_status() == status


@pytest.mark.parametrize("status", ["not_determined", "denied", "unavailable", "unknown"])
def test_background_notification_never_requests_permission(monkeypatch, status) -> None:
    monkeypatch.setattr(dn, "notification_status", lambda: status)
    monkeypatch.setattr(dn, "request_notifications", lambda: pytest.fail("background prompt"))
    monkeypatch.setattr(dn, "_notification_center", lambda: pytest.fail("unauthorized send"))
    result = dn.send_notification("Title", "Body")
    assert result["ok"] is False and result["sent"] is False
    assert result["permission_status"] == status


@pytest.mark.parametrize("native_error", [None, "notification rejected"])
def test_native_notification_has_app_content_and_reports_submission(monkeypatch, native_error) -> None:
    content = {}
    fake_content = SimpleNamespace(setTitle_=lambda v: content.update(title=v), setBody_=lambda v: content.update(body=v), setSound_=lambda v: content.update(sound=v))
    sent = []
    monkeypatch.setattr(dn, "notification_status", lambda: "granted")
    center = SimpleNamespace(addNotificationRequest_withCompletionHandler_=lambda request, done: (sent.append(request), done(native_error)))
    monkeypatch.setattr(dn, "_notification_center", lambda: center)
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(
        UNMutableNotificationContent=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: fake_content)),
        UNNotificationRequest=SimpleNamespace(requestWithIdentifier_content_trigger_=lambda key, value, trigger: (key, value, trigger)),
        UNNotificationSound=SimpleNamespace(defaultSound=lambda: "default"),
    ))
    result = dn.send_notification('Title "quoted"', 'Body $(literal)')
    if native_error:
        assert result["sent"] is False and result["ok"] is False
        assert result["error"] == native_error
        return
    assert result["sent"] is True and result["delivery"] == "submitted"
    assert sent[0][0] == result["message_id"] and sent[0][2] is None
    assert content == {"title": 'Title "quoted"', "body": 'Body $(literal)', "sound": "default"}


def test_explicit_notification_prompt_is_not_repeated(monkeypatch) -> None:
    monkeypatch.setattr(dn, "notification_status", lambda: "not_determined")
    monkeypatch.setattr(dn, "_request_pending", False)
    requests = []
    center = SimpleNamespace(requestAuthorizationWithOptions_completionHandler_=lambda options, callback: requests.append((options, callback)))
    monkeypatch.setattr(dn, "_notification_center", lambda: center)
    monkeypatch.setitem(sys.modules, "UserNotifications", SimpleNamespace(UNAuthorizationOptionAlert=4, UNAuthorizationOptionSound=2))
    assert dn.request_notifications()["pending"]
    assert dn.request_notifications()["pending"]
    assert len(requests) == 1 and requests[0][0] == 6
    requests[0][1](False, None)
    assert dn._request_pending is False


def test_source_launcher_does_not_call_bundle_only_api(monkeypatch) -> None:
    bundle = SimpleNamespace(bundleIdentifier=lambda: "org.python.python", bundlePath=lambda: "/Library/Python.app")
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSBundle=SimpleNamespace(mainBundle=lambda: bundle)))
    with pytest.raises(RuntimeError, match="application bundle"):
        dn._notification_center()


def test_desktop_notify_uses_native_result_instead_of_osascript_success(monkeypatch) -> None:
    from argusai.core.tools import desktop_automation as da

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(da.subprocess, "run", lambda *a, **kw: pytest.fail("must not use osascript"))
    monkeypatch.setattr(dn, "send_notification", lambda title, message: {"ok": False, "sent": False, "error_code": "macos_notifications_permission_required"})
    result = da.desktop_notify(SimpleNamespace(), title="Task", message="Finished")
    assert result["ok"] is False and result["sent"] is False
    assert result["error_code"] == "macos_notifications_permission_required"
