"""Output protocol contracts. All provider requests stay in MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

from argusai.core.channels.adapters import (
    DiscordAdapter, SlackAdapter, StreamSDKAdapter, TelegramAdapter,
    WebhookPlatformAdapter, WeChatILinkAdapter,
)
from argusai.core.channels.models import ChannelConfig, ChannelReply


def adapter(provider, **settings):
    cls = {"telegram": TelegramAdapter, "discord": DiscordAdapter,
           "slack": SlackAdapter, "feishu": StreamSDKAdapter,
           "dingtalk": StreamSDKAdapter, "webhook": WebhookPlatformAdapter,
           "wecom": WebhookPlatformAdapter, "wechat": WeChatILinkAdapter}[provider]
    return cls(ChannelConfig(id="test", name="Test", provider=provider, settings=settings),
               {"bot_token": "test", "client_id": "test"})


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def handle(request):
        body = json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"ok": True, "code": 0, "id": f"m{len(calls)}",
            "ts": f"ts{len(calls)}", "data": {"card_id": "card1"},
            "result": {"message_id": 123}})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "request", client.request)
    monkeypatch.setattr(httpx, "post", client.post)
    yield calls
    client.close()


def update(a, text, final=False, chat="user", source="inbound", thread=""):
    # Advance the per-stream throttle without real sleeping.
    for state in a._output_streams.states.values():
        state["pushed"] = 0
    return a.stream_update(chat_id=chat, source_message_id=source,
                           thread_id=thread, text=text, final=final)


def test_telegram_draft_is_persisted_once_and_respects_thread(wire):
    a = adapter("telegram")
    update(a, "first", chat="123", source="7", thread="9")
    update(a, "first second", chat="123", source="7", thread="9")
    update(a, "first second", final=True, chat="123", source="7", thread="9")
    update(a, "first second", final=True, chat="123", source="7", thread="9")
    assert [c[1].rsplit("/", 1)[-1] for c in wire] == ["sendMessageDraft", "sendMessageDraft", "sendMessage"]
    assert wire[0][2]["draft_id"] == wire[1][2]["draft_id"]
    assert wire[0][2]["message_thread_id"] == 9
    assert wire[-1][2]["text"] == "first second"


def test_slack_appends_only_new_text_and_finishes(wire):
    a = adapter("slack")
    update(a, "hello")
    update(a, "hello world")
    update(a, "hello world", final=True)
    assert [c[1] for c in wire] == ["/api/chat.startStream", "/api/chat.appendStream", "/api/chat.stopStream"]
    assert wire[1][2]["markdown_text"] == " world"
    assert wire[0][2]["thread_ts"] == "inbound"
    assert wire[1][2]["ts"] == wire[2][2]["ts"]


def test_slack_rolls_over_at_server_limit(wire):
    a = adapter("slack")
    update(a, "a" * 11999)
    update(a, "a" * 11999 + "bc", final=True)
    texts = [c[2].get("markdown_text", "") for c in wire]
    assert "".join(texts) == "a" * 11999 + "bc"
    assert [c[1] for c in wire].count("/api/chat.startStream") == 2
    assert [c[1] for c in wire].count("/api/chat.stopStream") == 2


def test_discord_updates_same_message_and_overflow(wire):
    a = adapter("discord")
    update(a, "hello")
    update(a, "hello " + "x" * 2000, final=True)
    assert [c[0] for c in wire] == ["POST", "PATCH", "POST"]
    assert wire[1][1].endswith("/m1")
    assert "".join(c[2]["content"] for c in wire[1:]) == "hello " + "x" * 2000
    assert wire[0][2]["allowed_mentions"] == {"parse": []}


def test_feishu_card_entity_sequence_and_close(wire, monkeypatch):
    a = adapter("feishu")
    monkeypatch.setattr(a, "_get_feishu_token", lambda: "token")
    update(a, "first")
    update(a, "first second", final=True)
    assert [c[0] for c in wire] == ["POST", "POST", "PUT", "PATCH"]
    assert json.loads(wire[0][2]["data"])["config"]["streaming_mode"] is True
    assert json.loads(wire[1][2]["content"])["data"]["card_id"] == "card1"
    assert wire[2][2]["content"] == "first second"
    assert wire[2][2]["sequence"] < wire[3][2]["sequence"]
    assert json.loads(wire[-1][2]["settings"]) == {"streaming_mode": False}


def test_ding_template_delivery_targets_staff_id(wire, monkeypatch):
    a = adapter("dingtalk", card_template_id="template", card_content_key="answer")
    monkeypatch.setattr(a, "_get_dingtalk_token", lambda: "token")
    a._output_streams.peer_users["conversation"] = "staff1"
    update(a, "hello", chat="conversation")
    update(a, "hello world", final=True, chat="conversation")
    assert wire[0][2]["cardTemplateId"] == "template"
    assert wire[1][2]["openSpaceId"] == "dtv1.card//IM_ROBOT.staff1"
    assert wire[2][2]["isFull"] is True
    assert wire[-1][2]["isFinalize"] is True
    assert wire[-1][2]["key"] == "answer"


def test_webhook_opt_in_replace_protocol_and_session_isolation(wire):
    a = adapter("webhook", outbound_url="https://example.test/reply", stream_protocol="v1")
    update(a, "a", thread="one")
    update(a, "b", thread="two")
    update(a, "ab", thread="one")
    update(a, "ab", final=True, thread="one")
    assert [c[2]["type"] for c in wire] == ["stream_start", "stream_start", "stream_delta", "stream_end"]
    assert wire[0][2]["stream_id"] == wire[2][2]["stream_id"]
    assert wire[0][2]["stream_id"] != wire[1][2]["stream_id"]
    assert wire[-1][2]["text"] == "ab"


@pytest.mark.parametrize("provider", ["wecom", "wechat", "webhook", "dingtalk"])
def test_unsupported_or_unconfigured_uses_staged_fallback(provider, wire):
    assert update(adapter(provider), "text") is False
    assert wire == []


def test_throttle_coalesces_but_final_never_drops_tail(wire):
    a = adapter("discord")
    update(a, "a")
    a.stream_update(chat_id="user", source_message_id="inbound", text="ab")
    assert len(wire) == 1
    a.stream_update(chat_id="user", source_message_id="inbound", text="abc", final=True)
    assert wire[-1][2]["content"] == "abc"


def test_failed_stream_does_not_claim_final_delivery(monkeypatch):
    a = adapter("slack")
    monkeypatch.setattr(httpx, "request", lambda *a, **k: httpx.Response(429, json={"ok": False, "error": "ratelimited"}))
    with pytest.raises(Exception, match="429"):
        update(a, "hello")
    assert update(a, "hello world", final=True) is False


def test_discord_emoji_respects_utf16_message_limit(wire):
    update(adapter("discord"), "😀" * 1500, final=True)
    assert len(wire) == 2
    assert all(len(c[2]["content"].encode("utf-16-le")) <= 4000 for c in wire)
    assert "".join(c[2]["content"] for c in wire) == "😀" * 1500


def test_slack_long_idle_continues_after_server_auto_finalize(wire):
    a = adapter("slack")
    update(a, "first")
    state = next(iter(a._output_streams.states.values()))
    state["started"] -= 601
    update(a, "first second", final=True)
    assert [c[1] for c in wire] == ["/api/chat.startStream", "/api/chat.startStream", "/api/chat.stopStream"]
    assert wire[1][2]["markdown_text"] == " second"


@pytest.mark.parametrize(
    ("provider", "settings"),
    [
        ("telegram", {}),
        ("slack", {}),
        ("discord", {}),
        ("feishu", {}),
        ("dingtalk", {"card_template_id": "template"}),
        ("webhook", {"stream_protocol": "v1"}),
    ],
)
def test_completed_channel_reply_uses_each_supported_provider_stream(
    provider,
    settings,
):
    a = adapter(provider, **settings)
    frames = []
    a.stream_update = lambda **frame: frames.append(frame) or True

    delivered = a.send_streamed_reply(
        ChannelReply(
            text="Agent authored this complete bubble",
            external_chat_id="chat-1",
            external_thread_id="thread-1",
        ),
        source_message_id="message-1",
        frame_delay=0,
    )

    assert delivered is True
    assert len(frames) > 1
    assert frames[-1]["text"] == "Agent authored this complete bubble"
    assert frames[-1]["final"] is True
    assert all(frame["final"] is False for frame in frames[:-1])
    if provider != "qq":
        assert all(frame["thread_id"] == "thread-1" for frame in frames)


def test_completed_channel_reply_respects_streaming_off_setting():
    a = adapter("telegram", streaming=False)
    a.stream_update = lambda **_frame: pytest.fail("disabled stream must not run")

    assert a.send_streamed_reply(
        ChannelReply(text="normal delivery", external_chat_id="chat-1"),
        source_message_id="message-1",
        frame_delay=0,
    ) is False


@pytest.mark.parametrize("provider", ["telegram", "slack", "discord", "feishu", "dingtalk", "webhook"])
@pytest.mark.parametrize("texts", [("OK", "OK"), ("The first complete bubble", "The second complete bubble")])
def test_reply_bubbles_have_independent_streams_and_retry_only_once(provider, texts, wire, monkeypatch):
    a = adapter(provider, card_template_id="template", stream_protocol="v1",
                outbound_url="https://example.test/reply")
    monkeypatch.setattr(a, "_get_feishu_token", lambda: "token", raising=False)
    monkeypatch.setattr(a, "_get_dingtalk_token", lambda: "token", raising=False)
    a._output_streams.peer_users["123"] = "staff1"

    for index, text in enumerate(texts):
        assert a.send_streamed_reply(
            ChannelReply(text=text, external_chat_id="123"),
            source_message_id="7", delivery_id=f"job-{index}:0", frame_delay=0,
        )

    create_path = {
        "telegram": "/sendMessage", "slack": "/chat.startStream",
        "discord": "/messages", "feishu": "/cardkit/v1/cards",
        "dingtalk": "/v1.0/card/instances",
    }.get(provider)
    if create_path:
        assert sum(method == "POST" and path.endswith(create_path)
                   for method, path, _body in wire) == 2
    else:
        assert len({body["stream_id"] for _method, _path, body in wire}) == 2
        assert all(body["reply_to_message_id"] == "7" for _method, _path, body in wire)
    if provider == "telegram":
        assert all(body["reply_to_message_id"] == 7
                   for _method, path, body in wire if path.endswith("/sendMessage"))
    if provider == "slack":
        assert all(body["thread_ts"] == "7"
                   for _method, path, body in wire if path.endswith("/chat.startStream"))

    request_count = len(wire)
    assert a.send_streamed_reply(
        ChannelReply(text=texts[1], external_chat_id="123"),
        source_message_id="7", delivery_id="job-1:0", frame_delay=0,
    )
    assert len(wire) == request_count


def test_completed_reply_without_delivery_id_still_creates_separate_bubbles(wire):
    a = adapter("discord")
    for _ in range(2):
        assert a.send_streamed_reply(
            ChannelReply(text="OK", external_chat_id="123"),
            source_message_id="7", frame_delay=0,
        )
    assert [method for method, _path, _body in wire] == ["POST", "POST"]
