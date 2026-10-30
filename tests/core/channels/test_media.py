from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import threading
import zipfile
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
    pack_forwarded_media,
    safe_filename,
    to_assistant_dict,
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


def test_forward_text_archive_limits_bytes_and_marks_partial_records() -> None:
    from argusai.core.channels.media import TEXT_LIMIT, prepare_forwarded_text

    text, attachments = prepare_forwarded_text(
        "转发正文" * (TEXT_LIMIT // 6), provider="qq", message_id="oversize",
    )
    assert "不是完整记录" in text
    assert len(attachments) == 1
    record = attachments[0]
    assert record["size"] <= TEXT_LIMIT
    assert record["size"] == len(record["text"].encode("utf-8"))
    assert "不是完整记录" in record["text"]
    assert "\ufffd" not in record["text"]
    assert _sanitize_attachments(attachments)


def test_forward_text_archive_names_do_not_overwrite_other_messages() -> None:
    from argusai.core.channels.media import prepare_forwarded_text

    body = "完整转发记录" * 3000
    _, first = prepare_forwarded_text(body, provider="qq", message_id="one")
    _, second = prepare_forwarded_text(body, provider="qq", message_id="two")
    assert first[0]["name"] != second[0]["name"]
    assert first[0]["text"] == second[0]["text"] == body


def test_forward_media_archive_preserves_all_images_and_files(tmp_path) -> None:
    payloads = [b"\x89PNG\r\n\x1a\n" + bytes([index]) for index in range(10)]
    values = [
        to_assistant_dict(attachment_from_bytes(data, name=f"image-{index}.png"))
        for index, data in enumerate(payloads)
    ]
    payloads.append(b"%PDF-forwarded-file")
    values.append(to_assistant_dict(attachment_from_bytes(payloads[-1], name="report.pdf")))
    values[0]["metadata"] = {"download_url": "https://example.test/private?token=secret"}
    values[0]["source_message_id"] = "child-message-1"
    values[1]["source_message_id"] = "m" * 150
    values[2]["source_message_id"] = "  "

    packed = pack_forwarded_media(values, provider="qq", message_id="message-1")
    assert packed["type"] == "file"
    assert packed["mime"] == "application/zip"
    sanitized = _sanitize_attachments([packed])
    materialized, _note = _materialize_binary_attachments(sanitized, tmp_path)
    assert "data_base64" not in materialized[0]
    with zipfile.ZipFile(materialized[0]["sandbox_path"]) as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        assert len(manifest["files"]) == 11
        assert manifest["total_bytes"] == sum(map(len, payloads))
        assert b"token=secret" not in manifest_bytes
        assert manifest["files"][0]["source_message_id"] == "child-message-1"
        assert manifest["files"][1]["source_message_id"] == "m" * 128
        assert "source_message_id" not in manifest["files"][2]
        assert "source_message_id" not in manifest["files"][3]
        for entry, expected in zip(manifest["files"], payloads, strict=True):
            assert archive.read(entry["path"]) == expected
            assert entry["size"] == len(expected)
            assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())


def test_forward_media_archive_uses_safe_unique_paths_and_stable_names() -> None:
    names = ["../../same.png", r"..\same.png", "same.png", "CON.exe"]
    values = [{
        "type": "file", "name": name, "mime": "application/octet-stream", "bytes": b"data",
    } for name in names]
    packed = pack_forwarded_media(values, provider="qq", message_id="same-message")
    again = pack_forwarded_media(values, provider="qq", message_id="same-message")
    other = pack_forwarded_media(values, provider="qq", message_id="other-message")
    assert packed == again
    assert packed["name"] != other["name"]
    with zipfile.ZipFile(io.BytesIO(attachment_bytes(packed))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        paths = [entry["path"] for entry in manifest["files"]]
        assert len(set(paths)) == len(names)
        assert all(path.startswith("files/") and ".." not in path and "\\" not in path for path in paths)
        assert [entry["original_name"] for entry in manifest["files"]] == names


def test_forward_media_archive_rejects_count_and_total_size_overflow(monkeypatch) -> None:
    from argusai.core.channels import media

    value = {"type": "file", "name": "sample.bin", "bytes": b"1234"}
    with pytest.raises(ChannelMediaError, match="file limit"):
        pack_forwarded_media([value] * (media.FORWARD_MEDIA_MAX_COUNT + 1), provider="qq", message_id="many")
    monkeypatch.setattr(media, "FORWARD_MEDIA_MAX_BYTES", 7)
    with pytest.raises(ChannelMediaError, match="byte limit"):
        pack_forwarded_media([value, value], provider="qq", message_id="large")


def test_forward_media_archive_counts_zip_metadata_against_size_limit(monkeypatch) -> None:
    from argusai.core.channels import media

    monkeypatch.setattr(media, "GLOBAL_FILE_LIMIT", 100)
    with pytest.raises(ChannelMediaError, match="size limit"):
        pack_forwarded_media(
            [{"type": "file", "name": "sample.bin", "bytes": b"1234"}],
            provider="qq", message_id="metadata-overflow",
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
        lambda *_args, **kwargs: to_assistant_dict(attachment_from_bytes(
            b"%PDF-test", name=kwargs["name"], mime=kwargs["mime"],
        )),
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

    assert attachments == [to_assistant_dict(attachment_from_bytes(
        b"%PDF-test", name="report.pdf", mime="application/pdf",
    ))]


def test_telegram_accepts_stickers_video_notes_and_locations(monkeypatch) -> None:
    class Client:
        @staticmethod
        def get(*_args, **_kwargs):
            return _Response({"ok": True, "result": {"file_path": "stickers/a.webp"}})

    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment",
        lambda *_args, **kwargs: to_assistant_dict(attachment_from_bytes(
            b"media-bytes", name=kwargs["name"], mime=kwargs["mime"],
        )),
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
    urls = []

    async def fake_download(_client, url, **kwargs):
        urls.append(url)
        return to_assistant_dict(attachment_from_bytes(
            b"image-bytes", name=kwargs["name"], mime=kwargs["mime"],
        ))

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
    assert "stickers/111.png" in urls[0]


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
        return to_assistant_dict(attachment_from_bytes(
            b"%PDF-test", name=kwargs["name"], mime=kwargs["mime"],
        ))

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


@pytest.mark.parametrize("provider", ["discord", "slack"])
@pytest.mark.parametrize("count", [10, 70])
def test_channel_many_media_are_archived_with_explicit_count_limits(monkeypatch, provider, count) -> None:
    calls = []

    async def fake_download(_client, url, **kwargs):
        calls.append(url)
        return to_assistant_dict(attachment_from_bytes(
            url.encode(), name=kwargs["name"], mime=kwargs["mime"],
        ))

    monkeypatch.setattr("argusai.core.channels.adapters._download_attachment_async", fake_download)
    cls = DiscordAdapter if provider == "discord" else SlackAdapter
    adapter = cls(ChannelConfig(id="many", provider=provider, name=provider), {"bot_token": "test"})
    items = [{
        "id": str(index), "name": f"file-{index}.bin", "filename": f"file-{index}.bin",
        "content_type": "application/octet-stream", "mimetype": "application/octet-stream",
        "url": f"https://cdn.example/{index}", "url_private": f"https://cdn.example/{index}",
    } for index in range(count)]

    result = asyncio.run(adapter._message_attachments({
        "id": "message", "ts": "message",
        "attachments" if provider == "discord" else "files": items,
    }))

    assert len(calls) == min(count, 64)
    assert result[0]["mime"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(attachment_bytes(result[0]))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["files"]) == min(count, 64)
        for entry, url in zip(manifest["files"], calls, strict=True):
            assert archive.read(entry["path"]) == url.encode()
    if count > 64:
        assert len(result) == 2
        assert "6 attachments exceed" in result[1]["text"]
    else:
        assert len(result) == 1


def test_discord_forward_snapshot_text_and_media_are_preserved(monkeypatch) -> None:
    async def fake_download(_client, _url, **kwargs):
        return to_assistant_dict(attachment_from_bytes(
            b"snapshot-image", name=kwargs["name"], mime=kwargs["mime"],
        ))

    monkeypatch.setattr("argusai.core.channels.adapters._download_attachment_async", fake_download)
    adapter = DiscordAdapter(ChannelConfig(id="d", provider="discord", name="Discord"), {})
    message = {
        "id": "forward", "content": "看看这个",
        "message_reference": {"type": 1, "message_id": "original"},
        "message_snapshots": [{"message": {
            "content": "第一行\n第二行",
            "attachments": [{"filename": "image.png", "content_type": "image/png", "url": "https://cdn.example/image"}],
        }}],
    }
    text, records = adapter._message_content(message)
    result = asyncio.run(adapter._message_attachments(message))
    assert "看看这个" in text and "第一行\n第二行" in text
    assert "用户转发内容" in text and "original" in text
    assert not records
    with zipfile.ZipFile(io.BytesIO(attachment_bytes(result[0]))) as archive:
        entry = json.loads(archive.read("manifest.json"))["files"][0]
        assert entry["source_message_id"] == "original"
        assert archive.read(entry["path"]) == b"snapshot-image"
    missing, _ = adapter._message_content({"message_reference": {"type": 1}})
    assert "未携带消息快照" in missing


def test_slack_shared_event_content_and_files_need_no_link_fetching(monkeypatch) -> None:
    calls = []

    async def fake_download(_client, url, **kwargs):
        calls.append(url)
        return to_assistant_dict(attachment_from_bytes(
            b"%PDF-shared", name=kwargs["name"], mime=kwargs["mime"],
        ))

    monkeypatch.setattr("argusai.core.channels.adapters._download_attachment_async", fake_download)
    adapter = SlackAdapter(ChannelConfig(id="s", provider="slack", name="Slack"), {})
    event = {"ts": "forward", "text": "共享给你", "attachments": [{
        "from_url": "https://example.test/must-not-fetch",
        "message": {"ts": "original", "text": "共享正文", "files": [{
            "id": "file", "name": "shared.pdf", "mimetype": "application/pdf",
            "url_private": "https://files.slack.test/shared.pdf",
        }]},
    }]}
    text, _records = adapter._message_content(event)
    result = asyncio.run(adapter._message_attachments(event))
    assert "共享正文" in text and "用户转发内容" in text
    assert calls == ["https://files.slack.test/shared.pdf"]
    with zipfile.ZipFile(io.BytesIO(attachment_bytes(result[0]))) as archive:
        entry = json.loads(archive.read("manifest.json"))["files"][0]
        assert entry["source_message_id"] == "original"
        assert archive.read(entry["path"]) == b"%PDF-shared"
    missing, _ = adapter._message_content({"attachments": [{"from_url": "https://example.test/private"}]})
    assert "未携带可读取的正文或文件" in missing
    assert asyncio.run(adapter._message_attachments({"attachments": [{"from_url": "https://example.test/private"}]})) == []
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["discord", "slack"])
def test_channel_media_download_failures_and_byte_budget_are_visible(monkeypatch, provider) -> None:
    from argusai.core.channels import adapters

    calls = []

    async def fake_download(_client, url, **kwargs):
        calls.append(url)
        if url.endswith("failure"):
            raise httpx.ConnectError("private-url-must-not-leak")
        return to_assistant_dict(attachment_from_bytes(b"1234", name=kwargs["name"], mime=kwargs["mime"]))

    monkeypatch.setattr(adapters, "_download_attachment_async", fake_download)
    monkeypatch.setattr(adapters, "FORWARD_MEDIA_MAX_BYTES", 6)
    cls = DiscordAdapter if provider == "discord" else SlackAdapter
    adapter = cls(ChannelConfig(id="failure", provider=provider, name=provider), {})
    items = [{
        "name": f"{name}.bin", "filename": f"{name}.bin",
        "url": f"https://cdn.example/{name}" if name != "missing" else "",
        "url_private": f"https://cdn.example/{name}" if name != "missing" else "",
    } for name in ["first", "over-budget", "failure", "missing"]]
    result = asyncio.run(adapter._message_attachments({
        "attachments" if provider == "discord" else "files": items,
    }))
    assert attachment_bytes(result[0]) == b"1234"
    error = result[-1]["text"]
    assert "byte budget" in error and "ConnectError" in error and "URL" in error
    assert "private-url-must-not-leak" not in error
    assert len(calls) == 3


@pytest.mark.parametrize("provider", ["discord", "slack"])
def test_channel_media_deadline_stops_remaining_downloads(monkeypatch, provider) -> None:
    from types import SimpleNamespace
    from argusai.core.channels import adapters

    ticks = iter([0, 61])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    cls = DiscordAdapter if provider == "discord" else SlackAdapter
    adapter = cls(ChannelConfig(id="deadline", provider=provider, name=provider), {})
    result = asyncio.run(adapter._message_attachments({
        "attachments" if provider == "discord" else "files": [{"name": "item.bin", "filename": "item.bin"}],
    }))
    assert len(result) == 1 and "60 second" in result[0]["text"]


@pytest.mark.parametrize("payload", [{"ok": False}, {"ok": True, "result": {}}])
def test_telegram_missing_file_download_data_is_visible(payload) -> None:
    class Client:
        def get(self, *_args, **_kwargs):
            return _Response(payload)

    adapter = TelegramAdapter(ChannelConfig(id="t", provider="telegram", name="Telegram"), {})
    result = adapter._message_attachments(Client(), {"document": {"file_id": "file", "file_name": "item.pdf"}})
    assert len(result) == 1
    assert "getFile" in result[0]["text"] and "not received" in result[0]["text"]


def test_telegram_get_file_exception_is_visible() -> None:
    class Client:
        def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("private-url-must-not-leak")

    adapter = TelegramAdapter(ChannelConfig(id="t", provider="telegram", name="Telegram"), {})
    result = adapter._message_attachments(Client(), {"document": {"file_id": "file", "file_name": "item.pdf"}})
    assert "ConnectError" in result[0]["text"]
    assert "private-url-must-not-leak" not in result[0]["text"]


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


@pytest.mark.parametrize("failure", ["missing", "token", "empty", "zero"])
def test_wecom_file_failures_do_not_drop_the_whole_event(monkeypatch, failure) -> None:
    wecom = WebhookPlatformAdapter(
        ChannelConfig(id="wecom-test", provider="wecom", name="WeCom"), {},
    )
    payload = {"MsgType": "file", "FromUserName": "user", "MsgId": "file-1", "FileName": "report.pdf"}
    if failure != "missing":
        payload["MediaId"] = "media-id"
    def token():
        if failure == "token":
            raise RuntimeError("unavailable")
        return "test-token"
    monkeypatch.setattr(wecom, "_get_wecom_token", token)
    monkeypatch.setattr(
        "argusai.core.channels.adapters._download_attachment",
        lambda *_args, **_kwargs: {"size": 0} if failure == "zero" else None,
    )
    event = wecom._event(payload)
    assert event is not None
    assert len(event.attachments) == 1
    assert event.attachments[0]["type"] == "text"
    assert "was not received" in event.attachments[0]["text"]
    assert "report.pdf" in event.attachments[0]["text"]


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
