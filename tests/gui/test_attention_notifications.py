from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from argusai.core.assistant import AssistantSession
from argusai.gui.base import _ApiBase
from argusai.gui.window import WindowMixin


def _api() -> _ApiBase:
    api = object.__new__(_ApiBase)
    api.settings_mgr = SimpleNamespace(
        get=lambda: SimpleNamespace(assistant_desktop_notifications=True)
    )
    api._attention_lock = threading.Lock()
    api._app_attention_state = {
        "focused": True,
        "visible": True,
        "open_session_ids": ["session-1"],
        "reported": True,
        "updated_at": time.time(),
    }
    api._attention_notification_times = {}
    api._attention_notification_counts = {}
    api._assistant_sessions = {}
    return api


def test_attention_notifications_only_fire_while_app_is_backgrounded(monkeypatch) -> None:
    api = _api()
    sent = threading.Event()
    captured: dict = {}

    def _notify(_api, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        sent.set()
        return {"ok": True, "sent": True}

    monkeypatch.setattr(
        "argusai.core.tools.desktop_automation.desktop_notify",
        _notify,
    )

    assert api._notify_attention(
        "focused-event",
        title="Focused",
        message="Do not send",
    ) is False
    api.set_app_attention_state({"focused": False, "visible": True})
    assert api._notify_attention(
        "background-event",
        title="Needs attention",
        message="Return to ArgusAI",
        level="warning",
    ) is True

    assert sent.wait(timeout=1)
    assert captured["title"] == "Needs attention"
    assert captured["level"] == "warning"


def test_losing_focus_notifies_for_existing_pending_confirmation(monkeypatch) -> None:
    api = _api()
    session = AssistantSession()
    session._pending = {
        "pending_id": "pending-1",
        "version": 1,
        "confirmations": [{
            "name": "run_shell_snippet",
            "arguments": {"script": "echo ok"},
            "tool_call": {"id": "call-1"},
        }],
    }
    api._assistant_sessions = {"session-1": session}
    sent = threading.Event()
    captured: dict = {}

    def _notify(_api, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        sent.set()
        return {"ok": True, "sent": True}

    monkeypatch.setattr(
        "argusai.core.tools.desktop_automation.desktop_notify",
        _notify,
    )

    result = api.set_app_attention_state({"focused": False, "visible": True})

    assert result["needs_attention"] is True
    assert sent.wait(timeout=1)
    assert captured["title"] == "ArgusAI 等待工具确认"
    assert "run_shell_snippet" in captured["message"]


def test_an_ignored_confirmation_is_announced_once_and_then_left_alone(
    monkeypatch,
) -> None:
    """Hiding the window re-runs this scan, and the pending never goes away.

    Without a cap, a confirmation the user has decided not to answer arrives
    again on every single hide, for as long as the app runs.
    """
    api = _api()
    session = AssistantSession()
    session._pending = {
        "pending_id": "pending-1",
        "version": 1,
        "confirmations": [{
            "name": "external_agent_tool",
            "arguments": {"agent": "cursor-acp", "kind": "execute"},
            "tool_call": {"id": "call-1"},
        }],
    }
    api._assistant_sessions = {"session-1": session}
    messages: list[str] = []
    delivered = threading.Semaphore(0)

    def _notify(_api, **kwargs):  # noqa: ANN001
        messages.append(kwargs["message"])
        delivered.release()
        return {"ok": True, "sent": True}

    monkeypatch.setattr(
        "argusai.core.tools.desktop_automation.desktop_notify",
        _notify,
    )

    for _ in range(4):
        api.set_app_attention_state({"focused": True, "visible": True})
        api.set_app_attention_state({"focused": False, "visible": False})
        # The cooldown alone would already suppress a rapid repeat.
        api._attention_notification_times.clear()

    assert delivered.acquire(timeout=1)
    assert len(messages) == 1
    # The synthetic name is an implementation detail, not something to show.
    assert "external_agent_tool" not in messages[0]
    assert "本地 Agent 的工具" in messages[0]


def test_a_closed_conversation_no_longer_sends_pending_notifications(
    monkeypatch,
) -> None:
    api = _api()
    session = AssistantSession()
    session._pending = {
        "pending_id": "pending-closed",
        "version": 1,
        "confirmations": [{
            "name": "nmap_scan",
            "arguments": {"target": "127.0.0.1"},
            "tool_call": {"id": "call-closed"},
        }],
    }
    api._assistant_sessions = {"session-1": session}
    sent = threading.Event()

    monkeypatch.setattr(
        "argusai.core.tools.desktop_automation.desktop_notify",
        lambda *_args, **_kwargs: sent.set() or {"ok": True, "sent": True},
    )

    # The front end publishes this immediately after removing the tab.
    result = api.set_app_attention_state({
        "focused": False,
        "visible": False,
        "open_session_ids": [],
    })

    assert result["needs_attention"] is True
    assert not sent.wait(timeout=0.1)


def test_native_minimize_notifies_for_existing_user_decision(monkeypatch) -> None:
    api = _api()
    session = AssistantSession()
    session._user_decision_requested = True
    api._assistant_sessions = {"session-1": session}
    minimized = threading.Event()
    sent = threading.Event()
    captured: dict = {}
    api._window = SimpleNamespace(minimize=minimized.set)

    def _notify(_api, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        sent.set()
        return {"ok": True, "sent": True}

    monkeypatch.setattr(
        "argusai.core.tools.desktop_automation.desktop_notify",
        _notify,
    )

    result = WindowMixin.minimize_window(api)

    assert result["ok"] is True
    assert minimized.is_set()
    assert api._app_attention_state["focused"] is False
    assert api._app_attention_state["visible"] is False
    assert sent.wait(timeout=1)
    assert captured["title"] == "ArgusAI 等待你的决定"
