"""Unit tests for ACP configOptions helpers and channel model routing."""

from __future__ import annotations

from argusai.core.agent_runtimes.acp_config import (
    model_config_options,
    normalize_config_options,
)
from argusai.core.channels.cards import card_text
from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelEvent, ChannelReply
from argusai.core.channels.store import ChannelStore


def test_normalize_and_filter_model_config_options() -> None:
    options = normalize_config_options([
        {
            "id": "mode",
            "name": "Mode",
            "type": "select",
            "category": "mode",
            "currentValue": "code",
            "options": [{"value": "ask", "name": "Ask"}, {"value": "code", "name": "Code"}],
        },
        {
            "id": "model",
            "name": "Model",
            "type": "select",
            "category": "model",
            "currentValue": "m1",
            "options": [
                {"value": "m1", "name": "Model One"},
                {"options": [{"value": "m2", "name": "Model Two"}]},
            ],
        },
    ])
    models = model_config_options(options)
    assert [item["id"] for item in models] == ["model"]
    assert [row["value"] for row in models[0]["options"]] == ["m1", "m2"]


def _paired(tmp_path):
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({"id": "webhook-test", "provider": "webhook", "name": "Test"})
    pairing = store.create_pairing(channel.id, bound_session_id="acp-session")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    identity = store.set_identity_permissions(
        identity.id,
        ["chat", "configure", "status", "send", "confirm", "cancel"],
    )
    return store, channel, identity


def _event(channel_id: str, *, token: str = "", message_id: str = "message-1") -> ChannelEvent:
    return ChannelEvent(
        channel_id=channel_id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id=message_id,
        text="",
        event_kind="action" if token else "message",
        action_token=token,
    )


def _find_action(card: dict, label: str) -> str:
    for action in card.get("actions") or []:
        if action.get("label") == label:
            return str(action["token"])
    raise AssertionError(f"missing action: {label}")


def _body(reply: ChannelReply) -> str:
    return card_text(
        reply.text,
        reply.blocks,
        include_actions=False,
        locale=reply.locale,
    )


def test_gateway_lists_and_switches_acp_models(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    models = [
        {
            "index": 0,
            "ordinal": 1,
            "name": "Model One",
            "model": "m1",
            "host": "Model",
            "active": True,
            "config_id": "model",
            "value": "m1",
        },
        {
            "index": 1,
            "ordinal": 2,
            "name": "Model Two",
            "model": "m2",
            "host": "Model",
            "active": False,
            "config_id": "model",
            "value": "m2",
        },
    ]
    activated: list[dict] = []

    def endpoint_handler(action: str, target: dict) -> dict:
        assert target.get("session_id") == "acp-session"
        rows = list(models)
        if action == "list":
            return {"ok": True, "kind": "acp_model", "endpoints": rows}
        match = next((row for row in rows if row["index"] == target.get("index")), None)
        if match is None:
            return {"ok": False, "code": "acp_model_not_found"}
        if action == "activate":
            activated.append(match)
            for row in rows:
                row["active"] = row["value"] == match["value"]
            return {"ok": True, "kind": "acp_model", "endpoint": {**match, "active": True}}
        return {"ok": True, "kind": "acp_model", "endpoint": match}

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
        endpoint_handler=endpoint_handler,
    )

    def command(text: str, message_id: str) -> ChannelReply:
        return gateway.handle(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=message_id,
            text=text,
        ))

    listing = command("/model", "list")
    assert "1. Model One · m1 (in use)" in _body(listing)
    assert "2. Model Two · m2" in _body(listing)
    assert "ACP models" in listing.blocks[0]["title"] or "ACP 模型" in listing.blocks[0]["title"]

    prompt = gateway.handle(_event(
        channel.id,
        token=_find_action(listing.blocks[0], "2"),
        message_id="pick",
    ))
    assert activated == []
    assert "Model Two" in (prompt.blocks[0].get("body") or "")

    applied = gateway.handle(_event(
        channel.id,
        token=_find_action(prompt.blocks[0], "Switch"),
        message_id="apply",
    ))
    assert "Model Two" in applied.text
    assert [row["value"] for row in activated] == ["m2"]
