"""macOS notifications under the running app's identity, never osascript's.

No authorization prompts are triggered by status reads or background sends.
UserNotifications requires an application bundle; source/CLI hosts report that
limitation instead of calling an API that may raise an Objective-C exception.
"""

from __future__ import annotations

import sys
import threading
import uuid
from typing import Any

_request_pending = False
_request_lock = threading.Lock()


def _notification_center() -> Any:
    from Foundation import NSBundle

    bundle = NSBundle.mainBundle()
    identifier = str(bundle.bundleIdentifier() or "")
    if not identifier or identifier.startswith("org.python.") or not str(bundle.bundlePath()).endswith(".app"):
        raise RuntimeError("macOS notifications require an application bundle")
    from UserNotifications import UNUserNotificationCenter

    return UNUserNotificationCenter.currentNotificationCenter()


def notification_status() -> str:
    if sys.platform != "darwin":
        return "unavailable"
    try:
        center = _notification_center()
        result: list[int] = []
        done = threading.Event()

        def received(settings: Any) -> None:
            result.append(int(settings.authorizationStatus()))
            done.set()

        center.getNotificationSettingsWithCompletionHandler_(received)
        if not done.wait(2):
            return "unknown"
        return {0: "not_determined", 1: "denied", 2: "granted", 3: "granted", 4: "granted"}.get(result[0], "unknown")
    except Exception:
        return "unavailable"


def request_notifications() -> dict[str, Any]:
    global _request_pending
    status = notification_status()
    if status == "granted":
        return {"ok": True, "status": status, "granted": True}
    if status != "not_determined":
        return {"ok": False, "status": status, "error_code": "macos_notifications_unavailable" if status == "unavailable" else "macos_notifications_denied"}
    try:
        from UserNotifications import UNAuthorizationOptionAlert, UNAuthorizationOptionSound

        center = _notification_center()
        with _request_lock:
            if _request_pending:
                return {"ok": True, "status": status, "pending": True}
            _request_pending = True

        def completed(_granted: bool, _error: Any) -> None:
            global _request_pending
            with _request_lock:
                _request_pending = False

        center.requestAuthorizationWithOptions_completionHandler_(
            UNAuthorizationOptionAlert | UNAuthorizationOptionSound, completed,
        )
        return {"ok": True, "status": status, "pending": True}
    except Exception:
        with _request_lock:
            _request_pending = False
        return {"ok": False, "status": "unknown", "error_code": "macos_permission_request_failed"}


def send_notification(title: str, message: str) -> dict[str, Any]:
    status = notification_status()
    if status != "granted":
        return {"ok": False, "sent": False, "reason": "notifications_permission_required",
                "error_code": "macos_notifications_permission_required", "permission_status": status,
                "error": "Enable ArgusAI notifications in Settings → macOS permissions."}
    try:
        from UserNotifications import UNMutableNotificationContent, UNNotificationRequest, UNNotificationSound

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(message)
        content.setSound_(UNNotificationSound.defaultSound())
        request_id = uuid.uuid4().hex
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(request_id, content, None)
        done = threading.Event()
        errors: list[str] = []

        def completed(error: Any) -> None:
            if error is not None:
                errors.append(str(error))
            done.set()

        _notification_center().addNotificationRequest_withCompletionHandler_(request, completed)
        if not done.wait(3):
            return {"ok": False, "sent": False, "error_code": "macos_notification_timeout",
                    "error": "Timed out waiting for notification acceptance"}
        if errors:
            return {"ok": False, "sent": False, "error": errors[0][:500]}
        return {"ok": True, "sent": True, "delivery": "submitted", "message_id": request_id}
    except Exception as exc:
        return {"ok": False, "sent": False, "error": str(exc)[:500]}
