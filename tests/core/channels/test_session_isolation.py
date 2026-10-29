"""Paired identities cannot list, switch, or read each other's sessions."""

from __future__ import annotations

from types import SimpleNamespace

from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelEvent
from argusai.core.channels.store import ChannelStore
from argusai.core.tools.kb_tasks import (
    channel_turn_session_allowlist,
    read_assistant_session,
    search_all_memory,
)
from tests.core.channels.test_cards import _body, _paired


def _gateway(store, sessions):
    return ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-new",
        confirmation_handler=lambda *_args: {"ok": True},
        session_list_handler=lambda: list(sessions),
    )


def test_pairing_only_lists_granted_sessions(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    store.grant_identity_session(identity.id, "session-own")
    gateway = _gateway(store, [
        {"id": "session-1", "name": "Mine", "message_count": 2},
        {"id": "session-own", "name": "Also mine", "message_count": 1},
        {"id": "session-secret", "name": "Someone else", "message_count": 9},
    ])
    event = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="list",
        text="/sessions",
    )
    body = _body(gateway.handle(event))
    assert "Mine" in body
    assert "Also mine" in body
    assert "Someone else" not in body
    assert "session-secret" not in body


def test_use_cannot_switch_to_an_ungranted_session(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = _gateway(store, [
        {"id": "session-1", "name": "Mine", "message_count": 2},
        {"id": "session-secret", "name": "Secret", "message_count": 1},
    ])
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="use-secret",
        text="/use session-secret",
    ))
    assert "not found" in reply.text.lower()
    assert store.get_identity(identity.id).bound_session_id == "session-1"


def test_new_grants_the_created_session_only_to_that_pairing(tmp_path) -> None:
    store, channel, first = _paired(tmp_path)
    pairing = store.create_pairing(channel.id, bound_session_id="session-b")
    second = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-2",
        external_chat_id="chat-2",
    )
    gateway = _gateway(store, [
        {"id": "session-1", "name": "First", "message_count": 1},
        {"id": "session-b", "name": "Second", "message_count": 1},
        {"id": "session-new", "name": "Created", "message_count": 0},
    ])
    created = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="new",
        text="/new",
    ))
    assert "session-new" in created.text
    first = store.get_identity(first.id)
    second = store.get_identity(second.id)
    assert first.can_access_session("session-new")
    assert first.can_access_session("session-1")
    assert not first.can_access_session("session-b")
    assert second.can_access_session("session-b")
    assert not second.can_access_session("session-new")
    assert not second.can_access_session("session-1")


def test_owner_bind_grants_without_giving_every_pairing_the_catalog(tmp_path) -> None:
    store, channel, first = _paired(tmp_path)
    pairing = store.create_pairing(channel.id)
    second = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-2",
        external_chat_id="chat-2",
    )
    store.bind_identity(second.id, "session-shared")
    first = store.get_identity(first.id)
    second = store.get_identity(second.id)
    assert first.can_access_session("session-1")
    assert not first.can_access_session("session-shared")
    assert second.can_access_session("session-shared")
    assert not second.can_access_session("session-1")


def test_channel_turn_memory_tools_stay_inside_the_pairing(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    store.grant_identity_session(identity.id, "session-own")
    session = SimpleNamespace(
        _current_turn_source="channel",
        _current_turn_source_ref=identity.id,
    )
    api = SimpleNamespace(
        _assistant_sessions={"session-1": session},
        _channels_store=lambda: store,
        search_all_assistant_memory=lambda query, limit, session_ids=None: {
            "ok": True,
            "query": query,
            "results": [{"session_id": sid} for sid in (session_ids or ["session-1", "secret"])],
            "session_ids": session_ids,
        },
        get_assistant_history=lambda sid: {
            "ok": True,
            "history": [{"role": "assistant", "content": f"from {sid}"}],
        },
    )
    kwargs = {"_session_id": "session-1", "_session": session}
    assert channel_turn_session_allowlist(api, kwargs) == frozenset({
        "session-1",
        "session-own",
    })
    searched = search_all_memory(api, "needle", 10, **kwargs)
    assert set(searched["session_ids"]) == {"session-1", "session-own"}
    denied = read_assistant_session(api, "secret", **kwargs)
    assert denied["ok"] is False
    assert "not granted" in denied["error"]
    allowed = read_assistant_session(api, "session-own", **kwargs)
    assert allowed["ok"] is True
    desktop = channel_turn_session_allowlist(
        api,
        {"_session_id": "session-1", "_session": SimpleNamespace(_current_turn_source="gui")},
    )
    assert desktop is None
