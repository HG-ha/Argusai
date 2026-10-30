from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from argusai.core.channels.adapters import QQAdapter, create_channel_adapter
from argusai.core.channels.cards import render_channel_reply
from argusai.core.channels.models import ChannelConfig, ChannelReply, infer_chat_kind


def _config() -> ChannelConfig:
    return ChannelConfig(
        id="qq-test",
        provider="qq",
        name="QQ test",
        enabled=True,
    )


def test_qq_provider_factory_and_direct_message_boundary() -> None:
    adapter = create_channel_adapter(
        _config(),
        {"app_id": "app", "client_secret": "secret"},
    )
    assert isinstance(adapter, QQAdapter)
    assert infer_chat_kind("qq", raw={"chat_scope": "c2c"}) == "direct"
    assert infer_chat_kind("qq", raw={"chat_scope": "group"}) == "group"
    assert infer_chat_kind("qq", raw={"chat_scope": "guild"}) == "group"


def test_qq_c2c_event_is_normalized_for_channel_gateway() -> None:
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    captured = []
    adapter._on_event = captured.append
    asyncio.run(adapter._on_message_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "message-1",
            "content": "  帮我看一下  ",
            "timestamp": "2026-10-30T07:00:00+08:00",
            "author": {"user_openid": "user-openid", "username": "Alice"},
        },
    ))

    assert len(captured) == 1
    event = captured[0]
    assert event.provider == "qq"
    assert event.external_user_id == "user-openid"
    assert event.external_chat_id == "user-openid"
    assert event.message_id == "message-1"
    assert event.text == "帮我看一下"
    assert event.display_name == "Alice"
    assert event.chat_kind == "direct"
    assert event.raw["chat_scope"] == "c2c"


def test_qq_merged_forward_is_expanded_for_the_agent() -> None:
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    captured = []
    adapter._on_event = captured.append

    asyncio.run(adapter._on_message_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "forward-1",
            "content": "安全事件讨论",
            "message_type": 102,
            "author": {"user_openid": "user-openid"},
            "msg_elements": [
                {"msg_idx": "1", "content": "=== 消息 1 ===\nAlice: 发现异常登录"},
                {"msg_idx": "2", "content": "=== 消息 2 ===\nBob: 已封禁来源 IP"},
            ],
        },
    ))

    assert len(captured) == 1
    text = captured[0].text
    assert "QQ 合并转发的聊天记录" in text
    assert "安全事件讨论" in text
    assert "Alice: 发现异常登录" in text
    assert "Bob: 已封禁来源 IP" in text
    assert text.endswith("【QQ 转发内容结束】")


def test_qq_composite_forward_without_body_is_explicit_not_silent() -> None:
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    captured = []
    adapter._on_event = captured.append

    asyncio.run(adapter._on_message_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "forward-empty",
            "content": "",
            "message_type": 102,
            "author": {"user_openid": "user-openid"},
            "msg_elements": [],
        },
    ))

    assert "未携带可展开的转发正文" in captured[0].text


def test_qq_large_forward_keeps_all_records_in_a_session_attachment(tmp_path) -> None:
    from argusai.core.assistant.attachments import (
        _materialize_text_attachments,
        _sanitize_attachments,
    )

    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    captured = []
    adapter._on_event = captured.append
    lines = [f"消息 {index}: " + "内容" * 300 for index in range(150)]
    asyncio.run(adapter._on_message_event("C2C_MESSAGE_CREATE", {
        "id": "large-forward",
        "message_type": 102,
        "author": {"user_openid": "user-openid"},
        "msg_elements": [{"content": line} for line in lines],
    }))

    event = captured[0]
    assert "文本附件" in event.text
    assert len(event.text) < 1000
    assert len(event.attachments) == 1
    payload = dict(event.attachments[0])
    cleaned = _sanitize_attachments([payload])
    stored, note = _materialize_text_attachments(cleaned, tmp_path)
    from pathlib import Path
    saved = Path(stored[0]["sandbox_path"]).read_text(encoding="utf-8")
    for line in lines:
        assert line in saved
    assert "read_text_file" in note


def test_qq_nested_forward_beyond_four_levels_is_preserved() -> None:
    from argusai.core.channels.adapters import _qq_inbound_text

    child = {"content": "最深处消息"}
    for _ in range(8):
        child = {"msg_elements": [child]}
    text, _, _ = _qq_inbound_text(
        SimpleNamespace(message_type=102, content=""),
        {"msg_elements": [child]},
    )
    assert "最深处消息" in text


def test_qq_forward_resource_limit_is_explicit(monkeypatch) -> None:
    from argusai.core.channels import adapters

    monkeypatch.setattr(adapters, "_QQ_FORWARD_MAX_NODES", 2)
    text, _, _ = adapters._qq_inbound_text(
        SimpleNamespace(message_type=102, content=""),
        {"msg_elements": [{"content": str(index)} for index in range(3)]},
    )
    assert "当前记录不完整" in text


def test_qq_forward_media_over_eight_files_is_saved_for_later_reading(monkeypatch, tmp_path) -> None:
    import zipfile
    from pathlib import Path

    from argusai.core.assistant.attachments import (
        _materialize_binary_attachments,
        _sanitize_attachments,
    )
    from argusai.core.channels import adapters
    from argusai.core.channels.media import attachment_from_bytes, to_assistant_dict

    downloads = []

    async def download(_client, url, *, mime, name):
        downloads.append(url)
        return to_assistant_dict(attachment_from_bytes(
            b"\x89PNG\r\n\x1a\n" + url.encode(), mime=mime, name=name,
        ))

    monkeypatch.setattr(adapters, "_download_attachment_async", download)
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter._http_client = object()
    captured = []
    adapter._on_event = captured.append
    asyncio.run(adapter._on_message_event("C2C_MESSAGE_CREATE", {
        "id": "media-forward",
        "message_type": 102,
        "author": {"user_openid": "user-openid"},
        "msg_elements": [{
            "content": f"图片 {index}",
            "attachments": [{
                "url": f"https://example.invalid/{index}.png",
                "content_type": "image/png",
                "filename": "same.png",
            }],
        } for index in range(12)],
    }))

    assert len(downloads) == 12
    event = captured[0]
    assert "manifest.json" in event.text
    cleaned = _sanitize_attachments(list(event.attachments))
    stored, _ = _materialize_binary_attachments(cleaned, tmp_path)
    archive = next(item for item in stored if item["name"].endswith(".zip"))
    with zipfile.ZipFile(Path(archive["sandbox_path"])) as bundle:
        files = [name for name in bundle.namelist() if name != "manifest.json"]
        assert len(files) == 12
        assert len(set(files)) == 12
        assert {bundle.read(name) for name in files} == {
            b"\x89PNG\r\n\x1a\n" + url.encode() for url in downloads
        }


def test_qq_forward_attachment_missing_url_is_reported() -> None:
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter._http_client = object()
    captured = []
    adapter._on_event = captured.append
    asyncio.run(adapter._on_message_event("C2C_MESSAGE_CREATE", {
        "id": "missing-file-url",
        "message_type": 102,
        "author": {"user_openid": "user-openid"},
        "msg_elements": [{"attachments": [{"filename": "lost.pdf"}]}],
    }))
    assert "下载失败" in captured[0].text
    assert captured[0].attachments


def test_qq_group_event_never_reaches_argusai() -> None:
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    captured = []
    adapter._on_event = captured.append
    asyncio.run(adapter._on_message_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "message-2",
            "content": "@ArgusAI hello",
            "group_openid": "group-openid",
            "author": {"member_openid": "member-openid"},
        },
    ))
    assert captured == []


def test_qq_reply_uses_markdown_and_inline_keyboard() -> None:
    sent = []

    class FakeApi:
        @staticmethod
        def next_msg_seq(_seed=""):
            return 17

        async def post_c2c_message(self, chat_id, message, *, keyboard=None):
            sent.append((chat_id, message.to_dict(), keyboard.to_dict() if keyboard else None))
            return {"id": str(len(sent))}

        async def send_text(self, chat_type, chat_id, content, **kwargs):
            raise AssertionError("Markdown-capable reply must not downgrade")

    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter._api = FakeApi()
    adapter._http_client = object()
    reply = ChannelReply(
        text="**结论**\n\n已经修复。",
        external_chat_id="user-openid",
        reply_to_message_id="message-1",
        blocks=({
            "type": "info",
            "title": "下一步",
            "actions": [{
                "label": "继续",
                "token": "ca_token1234567890",
                "style": "primary",
            }],
        },),
    )
    rendered = render_channel_reply("qq", reply)
    assert rendered["markdown"] is True
    assert rendered["text_chunks"] == ["**结论**\n\n已经修复。\n\n**下一步**"]
    assert rendered["keyboard"]["rows"][0][0]["data"] == "act:ca_token1234567890"

    asyncio.run(adapter._send_async(reply))
    assert sent[0][0] == "user-openid"
    assert sent[0][1]["msg_type"] == 2
    assert sent[0][1]["msg_id"] == "message-1"
    assert sent[0][1]["markdown"]["content"].startswith("**结论**")
    button = sent[0][2]["content"]["rows"][0]["buttons"][0]
    assert button["action"]["type"] == 1
    assert button["action"]["data"] == "act:ca_token1234567890"


def test_qq_markdown_permission_failure_falls_back_to_actionable_plain_text() -> None:
    plain = []

    class FakeApi:
        @staticmethod
        def next_msg_seq(_seed=""):
            return 18

        async def post_c2c_message(self, *_args, **_kwargs):
            raise RuntimeError("QQ Bot API error [403]: markdown permission denied")

        async def send_text(self, chat_type, chat_id, content, **kwargs):
            plain.append((chat_type, chat_id, content, kwargs))
            return {"id": "plain-1"}

    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter._api = FakeApi()
    adapter._http_client = object()
    reply = ChannelReply(
        text="**结论**",
        external_chat_id="user-openid",
        reply_to_message_id="message-1",
        blocks=({
            "type": "info",
            "title": "请选择",
            "actions": [{"label": "继续", "token": "ca_token1234567890"}],
        },),
    )

    asyncio.run(adapter._send_async(reply))

    assert plain[0][0:2] == ("c2c", "user-openid")
    assert "结论" in plain[0][2]
    assert "继续 — /act ca_token1234567890" in plain[0][2]
    assert plain[0][3]["reply_to"] == "message-1"
    assert plain[0][3]["markdown"] is False


def test_qq_keyboard_interaction_is_acked_and_forwarded_as_action() -> None:
    acked = []
    captured = []

    class FakeApi:
        async def acknowledge_interaction(self, interaction_id):
            acked.append(interaction_id)

    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter._api = FakeApi()
    adapter._on_event = captured.append
    asyncio.run(adapter._on_interaction_event("INTERACTION_CREATE", {
        "id": "interaction-1",
        "type": 11,
        "chat_type": 2,
        "scene": "c2c",
        "user_openid": "user-openid",
        "data": {
            "type": 11,
            "resolved": {"button_data": "act:ca_token1234567890"},
        },
    }))

    assert acked == ["interaction-1"]
    assert len(captured) == 1
    event = captured[0]
    assert event.event_kind == "action"
    assert event.action_token == "ca_token1234567890"
    assert event.external_chat_id == "user-openid"
    assert event.chat_kind == "direct"


def test_qq_native_stream_uses_replace_sequence_and_done_frame() -> None:
    requests = []

    class FakeApi:
        @staticmethod
        def next_msg_seq(_seed=""):
            return 23

        async def request(self, method, path, body):
            requests.append((method, path, dict(body)))
            return {"id": "stream-1"}

    async def run_stream():
        adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
        adapter._api = FakeApi()
        await adapter._stream_update_async(
            chat_id="user-openid",
            source_message_id="message-1",
            text="正在分析",
            final=False,
        )
        await adapter._stream_update_async(
            chat_id="user-openid",
            source_message_id="message-1",
            text="正在分析\n\n**完成**",
            final=True,
        )

    asyncio.run(run_stream())

    assert [item[0:2] for item in requests] == [
        ("POST", "/v2/users/user-openid/stream_messages"),
        ("POST", "/v2/users/user-openid/stream_messages"),
    ]
    first, final = requests[0][2], requests[1][2]
    assert first["input_mode"] == "replace"
    assert first["input_state"] == 1
    assert first["content_type"] == "markdown"
    assert first["msg_id"] == "message-1"
    assert first["msg_seq"] == final["msg_seq"] == 23
    assert first["index"] == 0
    assert final["index"] == 1
    assert final["input_state"] == 10
    assert final["stream_msg_id"] == "stream-1"


def test_qq_completed_channel_reply_replays_as_cumulative_native_frames() -> None:
    frames = []
    adapter = QQAdapter(_config(), {"app_id": "app", "client_secret": "secret"})
    adapter.stream_update = lambda **frame: frames.append(frame) or True

    delivered = adapter.send_streamed_reply(
        ChannelReply(text="这是 Agent 自己决定的一条完整回复", external_chat_id="qq-user"),
        source_message_id="message-9",
        frame_delay=0,
    )

    assert delivered is True
    assert len(frames) > 1
    assert all(frame["source_message_id"] == "message-9" for frame in frames)
    assert all(
        later["text"].startswith(earlier["text"])
        for earlier, later in zip(frames, frames[1:])
    )
    assert frames[-1]["text"] == "这是 Agent 自己决定的一条完整回复"
    assert frames[-1]["final"] is True
    assert all(frame["final"] is False for frame in frames[:-1])


def _login_host(tmp_path):
    from argusai.core.channels import ChannelStore
    from argusai.gui.channels import ChannelsMixin

    store = ChannelStore(tmp_path / "channels")
    refreshes: list[bool] = []

    class Host(ChannelsMixin):
        settings_mgr = SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        )

        def _channels_store(self):
            return store

        def _channels_runtime(self):
            return SimpleNamespace(refresh=lambda: refreshes.append(True))

    return Host(), store, refreshes


def test_official_qq_qr_onboard_saves_secrets_without_crossing_bridge(
    tmp_path,
    monkeypatch,
) -> None:
    from qqbot_agent_sdk import OnboardResult

    host, store, refreshes = _login_host(tmp_path)
    store.upsert_channel({
        "id": "qq-1",
        "provider": "qq",
        "name": "My QQ",
        "enabled": False,
    })

    async def fake_onboard(*, on_qr_ready, **_kwargs):
        on_qr_ready("https://q.qq.com/qqbot/onboard?task=fake")
        await asyncio.sleep(0)
        return OnboardResult(
            app_id="bot-app-id",
            client_secret="bot-client-secret",
            user_openid="user-openid",
        )

    monkeypatch.setattr("qqbot_agent_sdk.start_onboard", fake_onboard)
    started = host.start_qq_login("qq-1")
    assert started["ok"] is True
    login_id = started["login_id"]

    result = {}
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = host.poll_qq_login(login_id, "qq-1")
        if result.get("status") in {"confirmed", "error", "expired"}:
            break
        time.sleep(0.01)

    assert result["status"] == "confirmed"
    assert result["qr_url"].startswith("https://q.qq.com/")
    assert result["image"].startswith("data:image/svg+xml")
    assert "bot-client-secret" not in json.dumps(result)
    assert store.secrets.get_many("qq-1") == {
        "app_id": "bot-app-id",
        "client_secret": "bot-client-secret",
    }
    assert store.get_channel("qq-1").enabled is True
    assert refreshes == [True]


def test_qq_qr_onboard_rejects_unsaved_or_non_qq_channel(tmp_path) -> None:
    host, store, _refreshes = _login_host(tmp_path)
    store.upsert_channel({"id": "telegram-1", "provider": "telegram", "name": "TG"})

    assert host.start_qq_login("")["ok"] is False
    assert host.start_qq_login("missing")["ok"] is False
    assert host.start_qq_login("telegram-1")["ok"] is False
