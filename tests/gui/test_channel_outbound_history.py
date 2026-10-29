from __future__ import annotations

import json
import threading

from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
from argusai.core.channels.store import ChannelStore
from argusai.gui.channels import ChannelsMixin


def _make_host(tmp_path, store, sessions, updates):
    class Host(ChannelsMixin):
        def __init__(self) -> None:
            self._assistant_run_lock = threading.Lock()
            self._assistant_running_sessions: set[str] = set()

        def _channels_store(self):
            return store

        def _ensure_session(self, session_id):
            return sessions.setdefault(
                session_id,
                AssistantSession(registry=ToolRegistry()),
            )

        def _session_path(self, session_id):
            return tmp_path / f"{session_id}.json"

        def _save_session(self, path, session, _name):
            path.write_text(
                json.dumps(session.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )

        def _notify_assistant_session_updated(
            self,
            session_id,
            *,
            reason,
            source_ref="",
        ):
            updates.append((session_id, reason, source_ref))

    return Host()


def _make_identity(store):
    channel = store.upsert_channel({
        "id": "feishu-test",
        "provider": "feishu",
        "name": "Feishu",
        "enabled": True,
    })
    pairing = store.create_pairing(channel.id, bound_session_id="session-feishu")
    return store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user",
        external_chat_id="chat",
    )


def test_cross_session_delivery_is_mirrored_into_recipient_history(tmp_path) -> None:
    store = ChannelStore(tmp_path / "channels")
    identity = _make_identity(store)
    sessions: dict[str, AssistantSession] = {}
    updates: list = []
    host = _make_host(tmp_path, store, sessions, updates)
    job = {
        "id": "delivery-1",
        "identity_id": identity.id,
        "session_id": "session-gui",
        "recipient_session_id": "session-feishu",
        "text": "早上好",
        "attachments": [],
        "meta": {
            "origin_session_id": "session-gui",
            "reason": "用户在 GUI 里让我给飞书问个好",
            "trigger_ref": "ts:123.0",
        },
    }

    host._record_channel_outbound_delivery(job)
    host._record_channel_outbound_delivery(job)

    history = sessions["session-feishu"].history
    assert len(history) == 1
    entry = history[0]
    assert entry.role == "assistant"
    assert entry.source == "channel_outbound"
    assert entry.source_ref == "delivery-1"
    assert "早上好" in entry.content
    assert "Origin conversation: session-gui" in entry.content
    assert "用户在 GUI 里让我给飞书问个好" in entry.content
    assert entry.source_meta["origin_session_id"] == "session-gui"
    assert entry.source_meta["delivery_id"] == "delivery-1"
    assert entry.source_meta["reason"] == "用户在 GUI 里让我给飞书问个好"
    assert entry.source_meta["trigger_ref"] == "ts:123.0"
    assert updates == [("session-feishu", "channel_outbound", "delivery-1")]
    persisted = json.loads(
        (tmp_path / "session-feishu.json").read_text(encoding="utf-8")
    )
    assert persisted["history"][0]["source"] == "channel_outbound"
    assert persisted["history"][0]["source_meta"]["reason"] == (
        "用户在 GUI 里让我给飞书问个好"
    )


def test_delivery_while_recipient_running_is_queued_then_flushed(tmp_path) -> None:
    store = ChannelStore(tmp_path / "channels")
    identity = _make_identity(store)
    sessions: dict[str, AssistantSession] = {}
    updates: list = []
    host = _make_host(tmp_path, store, sessions, updates)
    # Recipient conversation is mid-turn: the outbox thread must not write.
    host._assistant_running_sessions.add("session-feishu")
    job = {
        "id": "delivery-2",
        "identity_id": identity.id,
        "session_id": "session-gui",
        "recipient_session_id": "session-feishu",
        "text": "结果来了",
        "attachments": [],
    }

    host._record_channel_outbound_delivery(job)

    # Nothing written yet; it sits in the pending queue.
    assert sessions.get("session-feishu") is None or not sessions["session-feishu"].history
    assert host._pending_channel_history()["session-feishu"][0]["delivery_id"] == "delivery-2"

    # Turn ends: the slot frees and the drain flushes the queued record.
    host._assistant_running_sessions.discard("session-feishu")
    host._drain_channel_interjections("session-feishu")

    history = sessions["session-feishu"].history
    assert len(history) == 1
    assert history[0].source_ref == "delivery-2"
    assert "结果来了" in history[0].content
    assert host._pending_channel_history().get("session-feishu") is None
    assert "session-feishu" not in host._assistant_running_sessions
