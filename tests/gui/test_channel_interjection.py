from __future__ import annotations

import threading

from argusai.gui.channels import ChannelsMixin


class _Gateway:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, dict]] = []

    def queue_result_delivery(self, identity_id: str, result: dict) -> None:
        self.delivered.append((identity_id, result))


class _Api(ChannelsMixin):
    """Just enough host for the interjection scheduler."""

    def __init__(self, on_turn=None) -> None:
        self._assistant_run_lock = threading.Lock()
        self._assistant_running_sessions: set[str] = set()
        self.gateway = _Gateway()
        self.turns: list[dict] = []
        self.cancelled: list[str] = []
        self._on_turn = on_turn

    def _run_assistant_chat(self, params: dict) -> dict:
        self.turns.append(params)
        if self._on_turn is not None:
            self._on_turn(self, params)
        return {"ok": True, "text": f"answered {params.get('message')}"}

    def cancel_assistant_run(self, session_id: str) -> dict:
        # The real one leaves the busy marker for the running turn's finally.
        self.cancelled.append(session_id)
        return {"ok": True}

    def _channels_gateway(self):
        return self.gateway


def _params(message: str) -> dict:
    return {
        "session_id": "session-1",
        "message": message,
        "message_source": "channel",
        "message_source_ref": "identity-1",
    }


def test_an_idle_conversation_runs_the_turn_and_frees_the_session() -> None:
    api = _Api()
    result = api._channel_turn(_params("hello"))
    assert result["text"] == "answered hello"
    assert api.cancelled == []
    assert api._assistant_running_sessions == set()


def test_a_second_turn_cannot_race_the_first() -> None:
    seen: list[set[str]] = []

    def reentrant(api: _Api, _params) -> None:
        # While this turn runs, another surface must be told the session is busy.
        seen.append(set(api._assistant_running_sessions))

    api = _Api(on_turn=reentrant)
    api._channel_turn(_params("first"))
    assert seen == [{"session-1"}]


def test_a_busy_conversation_interrupts_the_run_and_queues_the_message() -> None:
    api = _Api()
    api._assistant_running_sessions.add("session-1")
    result = api._channel_turn(_params("actually, stop"))
    assert result == {
        "ok": True,
        "queued": True,
        "interrupted": True,
        "position": 1,
        "session_id": "session-1",
    }
    assert api.cancelled == ["session-1"]
    assert api.turns == []


def test_queued_messages_run_in_order_once_the_session_frees_up() -> None:
    api = _Api()
    api._assistant_running_sessions.add("session-1")
    api._channel_turn(_params("first interjection"))
    api._channel_turn(_params("second interjection"))
    # The turn that was holding the session finishes.
    api._assistant_running_sessions.discard("session-1")
    api._drain_channel_interjections("session-1")

    assert [turn["message"] for turn in api.turns] == [
        "first interjection",
        "second interjection",
    ]
    # Their senders already got a receipt, so the answers go out on the outbox.
    assert [text for _identity, text in api.gateway.delivered] == [
        {"ok": True, "text": "answered first interjection"},
        {"ok": True, "text": "answered second interjection"},
    ]
    assert api._assistant_running_sessions == set()


def test_draining_defers_to_whoever_owns_the_session() -> None:
    api = _Api()
    api._assistant_running_sessions.add("session-1")
    api._channel_turn(_params("queued"))
    api._drain_channel_interjections("session-1")
    assert api.turns == []
    assert api.gateway.delivered == []


def test_a_failed_interjection_still_reports_back(tmp_path) -> None:
    def explode(_api, _params):
        raise RuntimeError("provider down")

    api = _Api(on_turn=explode)
    api._assistant_running_sessions.add("session-1")
    api._channel_turn(_params("check the logs"))
    api._assistant_running_sessions.discard("session-1")
    api._drain_channel_interjections("session-1")

    assert api.gateway.delivered[0][1]["ok"] is False
    assert "provider down" in api.gateway.delivered[0][1]["error"]
    assert api._assistant_running_sessions == set()
