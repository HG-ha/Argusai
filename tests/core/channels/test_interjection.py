from __future__ import annotations

import threading
import time

from argusai.core.channels.cards import card_text
from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelEvent
from argusai.core.channels.runtime import ChannelRuntimeManager
from argusai.core.channels.store import ChannelStore


def _paired(tmp_path):
    store = ChannelStore(tmp_path / "channels")
    channel = store.upsert_channel({
        "id": "webhook-test",
        "provider": "webhook",
        "name": "Test",
        "enabled": True,
    })
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    identity = store.set_identity_permissions(
        identity.id,
        ["chat", "status", "cancel", "send"],
    )
    return store, channel, identity


def _event(channel_id: str, text: str, message_id: str) -> ChannelEvent:
    return ChannelEvent(
        channel_id=channel_id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id=message_id,
        text=text,
        timestamp=time.time(),
    )


def _gateway(store: ChannelStore, **overrides) -> ChannelGateway:
    kwargs = {
        "turn_handler": lambda _params: {"ok": True, "text": "answer"},
        "session_handler": lambda _sid: {"ok": True, "message_count": 3},
        "cancel_handler": lambda _sid: {"ok": True},
        "new_session_handler": lambda _identity: "session-1",
        "confirmation_handler": lambda *_args: {"ok": True},
    }
    kwargs.update(overrides)
    return ChannelGateway(store, **kwargs)


def _body(reply) -> str:
    return card_text(
        reply.text,
        reply.blocks,
        include_actions=False,
        locale=reply.locale,
    )


def test_only_chat_messages_are_serialized_per_conversation(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    assert ChannelGateway.starts_turn(_event(channel.id, "look at this", "m1"))
    assert not ChannelGateway.starts_turn(_event(channel.id, "/cancel", "m2"))
    assert not ChannelGateway.starts_turn(_event(channel.id, "/btw what now", "m3"))
    action = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="m4",
        text="",
        event_kind="action",
        action_token="ca_token",
    )
    assert not ChannelGateway.starts_turn(action)


def test_cancel_is_answered_while_a_turn_is_still_running(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    running = threading.Event()
    release = threading.Event()
    sent = []

    def slow_turn(_params):
        running.set()
        release.wait(5)
        return {"ok": True, "text": "finished"}

    class Adapter:
        def send(self, reply):
            sent.append(reply)

    runtime = ChannelRuntimeManager(store, _gateway(store, turn_handler=slow_turn))
    runtime._adapters[channel.id] = Adapter()
    worker = threading.Thread(
        target=runtime._handle_event,
        args=(_event(channel.id, "audit this host", "m1"),),
        daemon=True,
    )
    try:
        worker.start()
        assert running.wait(5)
        runtime._handle_event(_event(channel.id, "/cancel", "m2"))
        # The cancel receipt is out before the chat turn was ever released:
        # queueing it behind the run would make the command useless.
        assert len(sent) == 1
        assert "Cancellation requested" in sent[0].text
    finally:
        release.set()
        worker.join(5)
        runtime.shutdown()
    assert [reply.text for reply in sent][-1] == "finished"


def test_a_busy_conversation_answers_with_an_interjection_receipt(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    gateway = _gateway(
        store,
        turn_handler=lambda _params: {"ok": True, "queued": True, "position": 1},
    )
    reply = gateway.handle(_event(channel.id, "stop and check the logs", "m1"))
    assert "wrap up the current step" in reply.text
    assert reply.blocks == ()


def test_queued_answers_travel_back_over_the_outbox(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = _gateway(store)
    gateway.queue_result_delivery(identity.id, {"ok": True, "text": "here is what I found"})
    jobs = store.list_outbound(session_id="session-1")
    assert [job["text"] for job in jobs] == ["here is what I found"]
    assert jobs[0]["identity_id"] == identity.id


def test_side_question_answers_without_starting_a_turn(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    turns = []
    asked = []

    def side(session_id, question):
        asked.append((session_id, question))
        return {"ok": True, "text": "The plan is still step 3 of 5."}

    gateway = _gateway(
        store,
        turn_handler=lambda params: turns.append(params) or {"ok": True},
        side_question_handler=side,
    )
    reply = gateway.handle(_event(channel.id, "/btw where are we", "m1"))
    assert turns == []
    assert asked == [("session-1", "where are we")]
    assert reply.blocks[0]["type"] == "info"
    body = _body(reply)
    assert "The plan is still step 3 of 5." in body
    assert "Not saved to this conversation" in body


def test_side_question_stays_read_only_without_decision_buttons(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)

    def session(_sid):
        return {
            "ok": True,
            "history": [{
                "role": "assistant",
                "content": "[等待用户决定] 请选部署方式",
                "blocks": [{
                    "type": "user_decision",
                    "question": "选择部署方式？",
                    "options": ["开始部署", "取消"],
                }],
            }],
        }

    gateway = _gateway(
        store,
        session_handler=session,
        side_question_handler=lambda *_args: {
            "ok": True,
            "text": "还在。主流程停在 stop_for_user_decision。",
        },
    )
    reply = gateway.handle(_event(channel.id, "/btw 还在运行吗", "m1"))
    assert [block["type"] for block in reply.blocks] == ["info"]
    assert "stop_for_user_decision" in _body(reply)


def test_push_session_interaction_delivers_wait_card_to_bound_identity(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = _gateway(store)
    delivered = gateway.push_session_interaction(
        "session-1",
        [{
            "type": "user_decision",
            "question": "继续？",
            "options": ["是", "否"],
        }],
    )
    assert delivered == 1
    pending = store.list_outbound(limit=10)
    assert len(pending) == 1
    assert pending[0]["identity_id"] == identity.id
    assert pending[0]["blocks"][0]["type"] == "user_decision"
    assert [action["label"] for action in pending[0]["blocks"][0]["actions"]] == ["是", "否"]


def test_side_question_without_a_question_explains_itself(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    gateway = _gateway(store, side_question_handler=lambda *_args: {"ok": True, "text": "x"})
    reply = gateway.handle(_event(channel.id, "/btw", "m1"))
    assert "Usage: /btw" in reply.text


def test_side_question_is_reported_as_unavailable_when_unwired(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    gateway = _gateway(store)
    reply = gateway.handle(_event(channel.id, "/btw anything", "m1"))
    assert "not available" in reply.text
