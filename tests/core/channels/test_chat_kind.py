"""Direct-message-only channel admission."""

from __future__ import annotations

import json

from argusai.core.channels.adapters import (
    TelegramAdapter,
    WebhookPlatformAdapter,
    create_channel_adapter,
)
from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelConfig, ChannelEvent, infer_chat_kind
from argusai.core.channels.store import ChannelStore


def test_infer_chat_kind_marks_provider_groups() -> None:
    assert infer_chat_kind("telegram", raw={"chat": {"type": "private"}}) == "direct"
    assert infer_chat_kind("telegram", raw={"chat": {"type": "supergroup"}}) == "group"
    assert infer_chat_kind(
        "telegram",
        raw={"message": {"chat": {"type": "group"}}},
    ) == "group"
    assert infer_chat_kind("discord", raw={"guild_id": "1"}) == "group"
    assert infer_chat_kind("discord", raw={}) == "direct"
    assert infer_chat_kind("slack", chat_id="D123") == "direct"
    assert infer_chat_kind("slack", chat_id="C123") == "group"
    assert infer_chat_kind("slack", raw={"channel_type": "im"}) == "direct"
    assert infer_chat_kind("slack", raw={"channel_type": "mpim"}) == "group"
    assert infer_chat_kind("feishu", raw={"chat_type": "p2p"}) == "direct"
    assert infer_chat_kind("feishu", raw={"message": {"chat_type": "group"}}) == "group"
    assert infer_chat_kind("dingtalk", raw={"conversationType": "1"}) == "direct"
    assert infer_chat_kind("dingtalk", raw={"conversationType": "2"}) == "group"
    assert infer_chat_kind("dingtalk", chat_id="cidABC") == "group"
    assert infer_chat_kind("wecom", raw={"FromUserName": "u1"}) == "direct"
    assert infer_chat_kind("wecom", raw={"ChatId": "wr1", "FromUserName": "u1"}) == "group"
    assert infer_chat_kind("wechat", raw={"from_user_id": "u"}) == "direct"
    assert infer_chat_kind("wechat", raw={"group_id": "g@im.chatroom"}) == "group"
    assert infer_chat_kind("webhook", raw={"is_group": True}) == "group"
    assert infer_chat_kind("webhook", chat_kind="group") == "group"
    assert infer_chat_kind("webhook", raw={"chat_id": "chat-1"}) == "direct"


def test_gateway_silently_ignores_group_chats_including_pair(tmp_path) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({"provider": "telegram", "name": "TG"})
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "should-not-run"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-new",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    pairing = store.create_pairing(channel.id)
    group = ChannelEvent(
        channel_id=channel.id,
        provider="telegram",
        external_user_id="1001",
        external_chat_id="-100",
        message_id="g1",
        text=f"/pair {pairing['code']}",
        raw={"chat": {"id": -100, "type": "supergroup"}},
    )
    assert gateway.handle(group).text == ""
    assert store.find_identity(channel.id, "1001", "-100") is None

    dm = ChannelEvent(
        channel_id=channel.id,
        provider="telegram",
        external_user_id="1001",
        external_chat_id="1001",
        message_id="d1",
        text=f"/pair {pairing['code']}",
        raw={"chat": {"id": 1001, "type": "private"}},
    )
    assert "Pairing complete" in gateway.handle(dm).text
    assert store.find_identity(channel.id, "1001", "1001") is not None


def test_adapter_emit_drops_group_events() -> None:
    adapter = TelegramAdapter(
        ChannelConfig(id="tg-test", provider="telegram", name="TG"),
        {"bot_token": "token"},
    )
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    adapter._emit(ChannelEvent(
        channel_id="tg-test",
        provider="telegram",
        external_user_id="1001",
        external_chat_id="-100",
        message_id="g1",
        text="hello",
        raw={"chat": {"id": -100, "type": "group"}},
    ))
    assert received == []

    adapter._emit(ChannelEvent(
        channel_id="tg-test",
        provider="telegram",
        external_user_id="1001",
        external_chat_id="1001",
        message_id="d1",
        text="hello",
        raw={"chat": {"id": 1001, "type": "private"}},
    ))
    assert [event.text for event in received] == ["hello"]
    assert received[0].chat_kind == "direct"


def test_webhook_group_payload_is_dropped() -> None:
    adapter = WebhookPlatformAdapter(
        ChannelConfig(id="wh-test", provider="webhook", name="Hook"),
        {"signing_secret": "secret"},
    )
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    group = adapter._event({
        "user_id": "user-1",
        "chat_id": "room-1",
        "message_id": "m1",
        "text": "/pair 123456",
        "is_group": True,
    })
    assert group is not None
    assert group.chat_kind == "group"
    adapter._emit(group)
    assert received == []


def test_dingtalk_sidecar_group_line_is_dropped() -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="dingtalk-test", provider="dingtalk", name="DingTalk"),
        {"client_id": "client", "client_secret": "secret"},
    )
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    payload = {
        "provider": "dingtalk",
        "external_user_id": "user-1",
        "external_chat_id": "cidABC",
        "message_id": "message-1",
        "text": "hello group",
        "raw": {"conversationType": "2", "conversationId": "cidABC"},
    }
    adapter._handle_sidecar_line(
        adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8")
    )
    assert received == []
