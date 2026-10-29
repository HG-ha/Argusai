from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from argusai.core.assistant.attachments import (
    _materialize_binary_attachments,
    _sanitize_attachments,
)
from argusai.core.channels import ChannelAttachment, ChannelConfig, ChannelReply
from argusai.core.channels.adapters import (
    DiscordAdapter,
    SlackAdapter,
    StreamSDKAdapter,
    TelegramAdapter,
    WebhookPlatformAdapter,
    _discord_inbound_text,
    _telegram_inbound_text,
    _wecom_inbound_text,
)
from argusai.core.channels.media import (
    ChannelMediaError,
    attachment_bytes,
    attachment_from_bytes,
    normalize_attachment,
    safe_filename,
)


class _Response:
    def __init__(self, payload=None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")


def _file_reply() -> ChannelReply:
    return ChannelReply(
        text="",
        external_chat_id="cid-chat",
        attachments=({
            "type": "file",
            "name": "report.pdf",
            "mime": "application/pdf",
            "data_base64": base64.b64encode(b"%PDF-test").decode(),
        },),
    )


def test_media_contract_preserves_common_binary_as_inert_file(tmp_path) -> None:
    path = tmp_path / "sample.exe"
    path.write_bytes(b"MZ\x00\x00payload")
    attachment = normalize_attachment({
        "type": "file",
        "name": "../../sample.exe",
        "path": str(path),
    })
    assert attachment.kind == "file"
    assert attachment.name == "sample.exe"
    assert attachment.mime == "application/vnd.microsoft.portable-executable"
    assert attachment.sha256
    assert attachment_bytes(attachment).startswith(b"MZ")
    assert safe_filename("../../CON.exe").startswith("_")


def test_media_contract_rejects_oversize_and_pathless_content() -> None:
    with pytest.raises(ChannelMediaError, match="no bytes"):
        normalize_attachment({"type": "file", "name": "empty.bin"})
    with pytest.raises(ChannelMediaError, match="limit"):
        attachment_from_bytes(
            b"x" * (4 * 1024 * 1024 + 1),
            name="large.txt",
            mime="text/plain",
        )


def test_telegram_surfaces_oversize_inbound_attachment() -> None:
    adapter = TelegramAdapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "token"},
    )
    attachments = adapter._message_attachments(
        object(),
        {
            "document": {
                "file_id": "large",
                "file_name": "large.pdf",
                "mime_type": "application/pdf",
                "file_size": 32 * 1024 * 1024 + 1,
            },
        },
    )

    assert attachments[0]["type"] == "text"
    assert "exceeds" in attachments[0]["text"]
    assert "large.pdf" in attachments[0]["text"]


def test_telegram_http_uses_proxy_and_custom_api_base(monkeypatch) -> None:
    adapter = TelegramAdapter(
        ChannelConfig(
            id="telegram-test",
            provider="telegram",
            name="Telegram",
            settings={
                "proxy": "http://127.0.0.1:7890",
                "api_base": "https://tg.example.local",
            },
        ),
        {"bot_token": "secret-token"},
    )
    assert adapter._base == "https://tg.example.local/botsecret-token"
    captured: dict[str, Any] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["proxy"] = kwargs.get("proxy")
        return _Response({"ok": True, "result": {}}, status_code=200)

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", fake_post)
    response = adapter._post_message({"chat_id": "1", "text": "hi"})
    assert response.is_success
    assert captured["url"] == "https://tg.example.local/botsecret-token/sendMessage"
    assert captured["proxy"] == "http://127.0.0.1:7890"


def test_telegram_transport_hint_mentions_proxy() -> None:
    hint = TelegramAdapter._transport_hint(
        httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF")
    )
    assert "proxy" in hint
    assert "api_base" in hint


def test_telegram_normalizes_inbound_document(monkeypatch) -> None:
    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return _Response({"ok": True, "result": {"file_path": "docs/report.pdf"}})

    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment",
        lambda *_args, **kwargs: {
            "type": "file",
            "name": kwargs["name"],
            "mime": kwargs["mime"],
        },
    )
    adapter = TelegramAdapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "token"},
    )

    attachments = adapter._message_attachments(Client(), {
        "document": {
            "file_id": "document-id",
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
            "file_size": 120,
        },
    })

    assert attachments == [{
        "type": "file",
        "name": "report.pdf",
        "mime": "application/pdf",
    }]


def test_telegram_accepts_stickers_video_notes_and_locations(monkeypatch) -> None:
    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return _Response({"ok": True, "result": {"file_path": "stickers/a.webp"}})

    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment",
        lambda *_args, **kwargs: {
            "type": "image",
            "name": kwargs["name"],
            "mime": kwargs["mime"],
        },
    )
    adapter = TelegramAdapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "token"},
    )
    sticker = {
        "sticker": {"file_id": "sticker-id", "emoji": "😀", "file_size": 80},
    }
    attachments = adapter._message_attachments(Client(), sticker)
    assert attachments[0]["mime"] == "image/webp"
    assert _telegram_inbound_text(sticker) == "😀"
    assert _telegram_inbound_text({
        "video_note": {"file_id": "note"},
    }) == ""
    notes = adapter._message_attachments(Client(), {
        "video_note": {"file_id": "note", "file_size": 80},
    })
    assert notes[0]["name"] == "telegram-video-note.mp4"
    assert _telegram_inbound_text({
        "location": {"latitude": 31.2, "longitude": 121.4},
        "venue": {"title": "Office"},
    }) == "Office"


def test_discord_downloads_stickers_and_reads_embeds(monkeypatch) -> None:
    async def fake_download(_client, url, **kwargs):
        return {
            "type": "image",
            "name": kwargs["name"],
            "mime": kwargs["mime"],
            "url": url,
        }

    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment_async",
        fake_download,
    )
    discord = DiscordAdapter(
        ChannelConfig(id="discord-test", provider="discord", name="Discord"),
        {"bot_token": "token"},
    )
    message = {
        "content": "",
        "embeds": [{"title": "Finding", "description": "Open redirect"}],
        "sticker_items": [
            {"id": "111", "name": "shipit", "format_type": 1},
            {"id": "222", "name": "lottie", "format_type": 3},
        ],
    }
    assert _discord_inbound_text(message) == "Finding\nOpen redirect\n:shipit: :lottie:"
    files = asyncio.run(discord._message_attachments(message))
    assert len(files) == 1
    assert files[0]["name"] == "shipit.png"
    assert "stickers/111.png" in files[0]["url"]


def test_wecom_location_and_link_become_text() -> None:
    wecom = WebhookPlatformAdapter(
        ChannelConfig(id="wecom-test", provider="wecom", name="WeCom"),
        {"corp_id": "corp", "corp_secret": "secret", "agent_id": "1"},
    )
    location = wecom._event({
        "MsgType": "location",
        "FromUserName": "user-1",
        "Label": "Office",
        "Location_X": "31.2",
        "Location_Y": "121.4",
    })
    assert location is not None
    assert location.text == "Office"
    link = wecom._event({
        "MsgType": "link",
        "FromUserName": "user-1",
        "Title": "Advisory",
        "Description": "CVE notes",
        "Url": "https://example.test/cve",
    })
    assert link is not None
    assert "Advisory" in link.text
    assert "https://example.test/cve" in link.text
    assert _wecom_inbound_text({"MsgType": "text", "Content": "hi"}) == "hi"


def test_feishu_http_callback_reads_post_messages() -> None:
    adapter = WebhookPlatformAdapter(
        ChannelConfig(id="feishu-http", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    event = adapter._event({
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "post",
                "content": json.dumps({
                    "title": "Hello",
                    "content": [[{"tag": "text", "text": "from HTTP"}]],
                }),
            },
        },
    })
    assert event is not None
    assert event.text == "Hello\nfrom HTTP"


def test_discord_and_slack_normalize_inbound_files(monkeypatch) -> None:
    async def fake_download(_client, _url, **kwargs):
        return {
            "type": "file",
            "name": kwargs["name"],
            "mime": kwargs["mime"],
        }

    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment_async",
        fake_download,
    )
    discord = DiscordAdapter(
        ChannelConfig(id="discord-test", provider="discord", name="Discord"),
        {"bot_token": "token"},
    )
    slack = SlackAdapter(
        ChannelConfig(id="slack-test", provider="slack", name="Slack"),
        {"bot_token": "xoxb", "app_token": "xapp"},
    )

    discord_files = asyncio.run(discord._message_attachments({
        "attachments": [{
            "filename": "discord.pdf",
            "content_type": "application/pdf",
            "size": 120,
            "url": "https://cdn.example/discord.pdf",
        }],
    }))
    slack_files = asyncio.run(slack._message_attachments({
        "files": [{
            "name": "slack.pdf",
            "mimetype": "application/pdf",
            "size": 120,
            "url_private_download": "https://cdn.example/slack.pdf",
        }],
    }))

    assert discord_files[0]["name"] == "discord.pdf"
    assert slack_files[0]["name"] == "slack.pdf"


def test_feishu_dingtalk_and_wecom_normalize_inbound_files(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment",
        lambda *_args, **kwargs: {
            "type": "file",
            "name": kwargs["name"],
            "mime": kwargs["mime"],
        },
    )
    feishu = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(feishu, "_get_feishu_token", lambda: "token")
    assert feishu._feishu_attachments(
        {"message_id": "message-id"},
        {
            "message_type": "file",
            "content": {"file_key": "file-key", "file_name": "feishu.pdf"},
        },
    )[0]["name"] == "feishu.pdf"
    assert feishu._feishu_attachments(
        {"message_id": "message-id"},
        {
            "message_type": "sticker",
            "content": {"file_key": "sticker-key"},
        },
    )[0]["name"] == "feishu-sticker.png"

    class DownloadResponse:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"downloadUrl": "https://download.example/dingtalk.pdf"}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def post(*_args, **_kwargs):
            return DownloadResponse()

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.Client", Client)
    dingtalk = StreamSDKAdapter(
        ChannelConfig(id="dingtalk-test", provider="dingtalk", name="DingTalk"),
        {"client_id": "app", "client_secret": "secret"},
    )
    monkeypatch.setattr(dingtalk, "_get_dingtalk_token", lambda: "token")
    assert dingtalk._dingtalk_attachments({
        "download_codes": ["download-code"],
        "robotCode": "robot-code",
        "msgtype": "file",
        "content": {"fileName": "dingtalk.pdf"},
    })[0]["name"] == "dingtalk.pdf"

    wecom = WebhookPlatformAdapter(
        ChannelConfig(id="wecom-test", provider="wecom", name="WeCom"),
        {"corp_id": "corp", "corp_secret": "secret", "agent_id": "1"},
    )
    monkeypatch.setattr(wecom, "_get_wecom_token", lambda: "token")
    assert wecom._wecom_attachments({
        "MsgType": "file",
        "MediaId": "media-id",
        "FileName": "wecom.pdf",
    })[0]["name"] == "wecom.pdf"


def test_generic_inbound_binary_is_materialized_without_raw_bytes(tmp_path) -> None:
    raw = [{
        "type": "file",
        "name": "../../sample.exe",
        "mime": "application/octet-stream",
        "data_base64": base64.b64encode(b"MZpayload").decode(),
    }]
    sanitized = _sanitize_attachments(raw)
    materialized, note = _materialize_binary_attachments(sanitized, tmp_path)
    assert len(materialized) == 1
    assert "data_base64" not in materialized[0]
    assert materialized[0]["relative_path"].startswith("_attachments/")
    assert ".." not in materialized[0]["relative_path"]
    assert (tmp_path / materialized[0]["relative_path"]).read_bytes() == b"MZpayload"
    assert materialized[0]["sha256"] in note


def test_telegram_uses_native_document_upload(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "argusai.core.channels.adapters.httpx.post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response({"ok": True}),
    )
    TelegramAdapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "token"},
    ).send(_file_reply())
    assert calls[0][0].endswith("/sendDocument")
    assert calls[0][1]["files"]["document"][0] == "report.pdf"


def test_discord_uses_multipart_attachment(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "argusai.core.channels.adapters.httpx.post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )
    DiscordAdapter(
        ChannelConfig(id="discord-test", provider="discord", name="Discord"),
        {"bot_token": "token"},
    ).send(_file_reply())
    assert "payload_json" in calls[0][1]["data"]
    assert calls[0][1]["files"][0][0] == "files[0]"


def test_slack_uses_external_upload_flow(monkeypatch) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("files.getUploadURLExternal"):
            return _Response({"ok": True, "upload_url": "https://upload.invalid", "file_id": "F1"})
        if url.endswith("files.completeUploadExternal"):
            return _Response({"ok": True})
        raise AssertionError(url)

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    monkeypatch.setattr(
        "argusai.core.channels.adapters.httpx.put",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )
    SlackAdapter(
        ChannelConfig(id="slack-test", provider="slack", name="Slack"),
        {"bot_token": "xoxb", "app_token": "xapp"},
    ).send(_file_reply())
    assert any(url.endswith("files.getUploadURLExternal") for url, _ in calls)
    assert any(url.endswith("files.completeUploadExternal") for url, _ in calls)


def test_feishu_uploads_then_sends_file(monkeypatch) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/im/v1/files"):
            return _Response({"code": 0, "data": {"file_key": "file-key"}})
        return _Response({"code": 0})

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    adapter.send(_file_reply())
    assert any(url.endswith("/im/v1/files") for url, _ in calls)
    assert any(
        kwargs.get("json", {}).get("msg_type") == "file"
        for _url, kwargs in calls
    )


def test_dingtalk_uploads_then_sends_proactive_file(monkeypatch) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if "media/upload" in url:
            return _Response({"errcode": 0, "media_id": "@media"})
        return _Response({})

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    adapter = StreamSDKAdapter(
        ChannelConfig(
            id="dingtalk-test",
            provider="dingtalk",
            name="DingTalk",
            settings={"robot_code": "robot"},
        ),
        {"client_id": "app", "client_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_dingtalk_token", lambda: "token")
    adapter.send(_file_reply())
    proactive = next(
        kwargs["json"]
        for url, kwargs in calls
        if "groupMessages/send" in url
    )
    assert proactive["msgKey"] == "sampleFile"
    assert json.loads(proactive["msgParam"])["mediaId"] == "@media"


def test_wecom_uploads_then_sends_media(monkeypatch) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if "media/upload" in url:
            return _Response({"errcode": 0, "media_id": "media-id"})
        return _Response({"errcode": 0})

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    adapter = WebhookPlatformAdapter(
        ChannelConfig(id="wecom-test", provider="wecom", name="WeCom"),
        {"corp_id": "corp", "corp_secret": "secret", "agent_id": "1"},
    )
    monkeypatch.setattr(adapter, "_get_wecom_token", lambda: "token")
    adapter.send(_file_reply())
    assert any("media/upload" in url for url, _ in calls)
    assert any(
        kwargs.get("json", {}).get("msgtype") == "file"
        for url, kwargs in calls
        if "message/send" in url
    )


def test_generic_webhook_embeds_normalized_attachment(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "argusai.core.channels.adapters.httpx.post",
        lambda url, **kwargs: calls.append((url, kwargs)) or _Response(),
    )
    adapter = WebhookPlatformAdapter(
        ChannelConfig(
            id="webhook-test",
            provider="webhook",
            name="Webhook",
            settings={"outbound_url": "https://receiver.invalid/hook"},
        ),
        {"signing_secret": "secret"},
    )
    adapter.send(_file_reply())
    attachment = calls[0][1]["json"]["attachments"][0]
    assert attachment["name"] == "report.pdf"
    assert base64.b64decode(attachment["data_base64"]).startswith(b"%PDF")


def test_generic_webhook_attachment_round_trip_smoke() -> None:
    delivered = []
    delivery_ready = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            delivered.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            delivery_ready.set()

        def log_message(self, _format, *_args):
            return

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    adapter = WebhookPlatformAdapter(
        ChannelConfig(
            id="webhook-e2e",
            provider="webhook",
            name="Webhook",
            enabled=True,
            settings={
                "host": "127.0.0.1",
                "port": 0,
                "path": "/hook",
                "outbound_url": (
                    f"http://127.0.0.1:{receiver.server_port}/delivery"
                ),
            },
        ),
        {"signing_secret": "smoke-secret"},
    )
    inbound_ready = threading.Event()

    def on_event(event):
        adapter.send(ChannelReply(
            text="",
            external_chat_id=event.external_chat_id,
            attachments=event.attachments,
        ))
        inbound_ready.set()

    adapter.start(on_event)
    payload = {
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "text": "",
        "attachments": [{
            "type": "file",
            "name": "evidence.pdf",
            "mime": "application/pdf",
            "data_base64": base64.b64encode(b"%PDF-round-trip").decode(),
        }],
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(b"smoke-secret", body, hashlib.sha256).hexdigest()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{adapter._server.server_port}/hook",
            content=body,
            headers={"X-ArgusAI-Signature": f"sha256={signature}"},
            timeout=5,
        )
        assert response.status_code == 200
        assert inbound_ready.wait(2)
        assert delivery_ready.wait(2)
    finally:
        adapter.stop()
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=2)

    attachment = delivered[0]["attachments"][0]
    assert attachment["name"] == "evidence.pdf"
    assert base64.b64decode(attachment["data_base64"]) == b"%PDF-round-trip"
