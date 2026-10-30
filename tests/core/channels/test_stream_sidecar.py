"""Tests for the Feishu/DingTalk stream sidecar helpers."""

from __future__ import annotations

import json
import base64
import io
import sys
import types
import zipfile

import httpx
import pytest

from argusai.core.channels import stream_sidecar as sidecar
from argusai.core.channels import adapters as channel_adapters
from argusai.core.channels.adapters import StreamSDKAdapter
from argusai.core.channels.models import ChannelConfig
from argusai.core.channels.stream_sidecar import (
    _FeishuStreamHandler,
    _dict_to_ns,
    _emit_feishu_card_action,
    _feishu_message_event_payload,
    _load_feishu_ws,
    _patch_feishu_ws_card_frames,
    feishu_inbound_text,
    feishu_media_refs,
)


def test_dict_to_ns_builds_nested_attribute_access() -> None:
    obj = _dict_to_ns({"event": {"message": {"content": '{"text":"hi"}'}}})
    assert obj.event.message.content == '{"text":"hi"}'


def test_feishu_drops_bot_echo_messages() -> None:
    payload = _feishu_message_event_payload(_dict_to_ns({
        "event": {
            "sender": {
                "sender_type": "app",
                "sender_id": {"open_id": "cli_bot"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": '{"text":"/help"}',
                "create_time": "1700000000000",
            },
        },
    }))
    assert payload is None


def test_feishu_keeps_user_text_messages() -> None:
    payload = _feishu_message_event_payload(_dict_to_ns({
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_1"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": '{"text":"@_user_1 /status"}',
                "create_time": "1700000000000",
            },
        },
    }))
    assert payload is not None
    assert payload["text"] == "@_user_1 /status"
    assert payload["external_user_id"] == "ou_1"


def _feishu_user_event(message_type: str, content: dict) -> object:
    return _dict_to_ns({
        "event": {
            "sender": {
                "sender_type": "user",
                "sender_id": {"open_id": "ou_1"},
            },
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": message_type,
                "content": json.dumps(content, ensure_ascii=False),
                "create_time": "1700000000000",
            },
        },
    })


def test_feishu_keeps_post_rich_text_and_stickers() -> None:
    post = _feishu_message_event_payload(_feishu_user_event("post", {
        "zh_cn": {
            "title": "周报",
            "content": [
                [{"tag": "text", "text": "本周完成 "}, {"tag": "at", "user_name": "Ada"}],
                [{"tag": "img", "image_key": "img_1"}],
            ],
        },
    }))
    assert post is not None
    assert "周报" in post["text"]
    assert "本周完成 @Ada" in post["text"]
    assert "[image]" in post["text"]
    assert feishu_media_refs("post", post["raw"]["content"])[0]["file_key"] == "img_1"

    sticker = _feishu_message_event_payload(_feishu_user_event("sticker", {
        "file_key": "sticker_1",
        "emoji_type": "SMILE",
    }))
    assert sticker is not None
    assert sticker["text"] == "SMILE"
    refs = feishu_media_refs("sticker", sticker["raw"]["content"])
    assert refs[0]["resource_type"] == "image"
    assert refs[0]["file_key"] == "sticker_1"
    bare_sticker = _feishu_message_event_payload(_feishu_user_event("sticker", {
        "file_key": "sticker_1",
    }))
    assert bare_sticker is not None
    assert bare_sticker["text"] == ""

    shared = _feishu_message_event_payload(_feishu_user_event("share_chat", {
        "chat_name": "安全群",
    }))
    assert shared is not None
    assert shared["text"] == "安全群"

    unknown = _feishu_message_event_payload(_feishu_user_event("calendar", {}))
    assert unknown is None


def test_feishu_inbound_text_flattens_post_nodes() -> None:
    text = feishu_inbound_text("post", {
        "title": "Note",
        "content": [[{"tag": "text", "text": "hello"}, {"tag": "a", "text": " link"}]],
    })
    assert text == "Note\nhello link"


def test_feishu_merge_forward_expands_only_its_child_messages(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")

    def fake_get(_url, **_kwargs):
        assert _url == "https://open.feishu.cn/open-apis/im/v1/messages/merge-1"
        assert "params" not in _kwargs
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "items": [
                    {
                        "message_id": "child-1",
                        "upper_message_id": "merge-1",
                        "msg_type": "text",
                        "sender": {"id": "Alice"},
                        "body": {"content": '{"text":"发现异常登录"}'},
                    },
                    {
                        "message_id": "nested-1",
                        "upper_message_id": "child-1",
                        "msg_type": "post",
                        "sender": {"id": "Bob"},
                        "body": {"content": '{"title":"处置","content":[[{"tag":"text","text":"已封禁"}]]}'},
                    },
                    {
                        "message_id": "unrelated",
                        "upper_message_id": "other-merge",
                        "msg_type": "text",
                        "body": {"content": '{"text":"不应泄漏"}'},
                    },
                ],
            },
        })

    monkeypatch.setattr(httpx, "get", fake_get)
    text = adapter._feishu_merge_forward_text(
        {
            "message_id": "merge-1",
            "external_chat_id": "oc_chat",
            "text": "聊天记录",
        },
        {"message_type": "merge_forward"},
    )

    assert "飞书合并转发的聊天记录" in text
    assert "[Alice] 发现异常登录" in text
    assert "[Bob] 处置\n已封禁" in text
    assert "不应泄漏" not in text
    assert text.endswith("【飞书转发内容结束】")


def test_feishu_merge_forward_keeps_more_than_one_history_page(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    messages = [{
        "message_id": f"child-{index}",
        "upper_message_id": "merge-1",
        "msg_type": "text",
        "create_time": str(index),
        "body": {"content": json.dumps({"text": f"record-{index}"})},
    } for index in range(120)]
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return httpx.Response(200, json={"code": 0, "data": {"items": messages}})

    monkeypatch.setattr(httpx, "get", fake_get)
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"},
    )
    assert calls == ["https://open.feishu.cn/open-apis/im/v1/messages/merge-1"]
    assert "record-119" in text
    assert text.index("record-9\n") < text.index("record-10\n")


def test_feishu_long_forward_is_archived_without_losing_the_tail(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    monkeypatch.setattr(adapter, "_feishu_attachments", lambda *_args: [])
    body = "第一行\n" + "完整记录\n" * 12_000 + "最后一条重要消息"
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={
        "code": 0,
        "data": {"items": [{
            "message_id": "child-1",
            "upper_message_id": "merge-1",
            "msg_type": "text",
            "body": {"content": json.dumps({"text": body})},
        }]},
    }))
    captured = []
    adapter._on_event = captured.append
    payload = {
        "message_id": "merge-1",
        "external_user_id": "user-1",
        "external_chat_id": "chat-1",
        "raw": {"message_type": "merge_forward", "chat_type": "p2p"},
    }
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8"))

    assert len(captured) == 1
    event = captured[0]
    assert len(event.text) < 12_000
    assert len(event.attachments) == 1
    archived = event.attachments[0]
    assert archived.get("kind", archived.get("type")) == "text"
    assert body in archived["text"]
    assert "截断" not in event.text


def test_feishu_nested_forwards_have_a_visible_network_limit(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    calls = []

    def fake_get(url, **_kwargs):
        parent = url.rsplit("/", 1)[-1]
        calls.append(parent)
        number = len(calls)
        return httpx.Response(200, json={"code": 0, "data": {"items": [{
            "message_id": f"nested-{number}",
            "upper_message_id": parent,
            "msg_type": "merge_forward",
            "body": {"content": '{"text":"nested forward"}'},
        }]}})

    monkeypatch.setattr(httpx, "get", fake_get)
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"},
    )
    assert len(calls) == 16
    assert "内容尚不完整" in text
    assert "展开限额" in text


def test_feishu_archived_partial_forward_keeps_the_warning_visible(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_feishu_attachments", lambda *_args: [])
    partial = "正文\n" * 5_000 + "【部分嵌套转发读取失败，内容尚不完整】"
    monkeypatch.setattr(adapter, "_feishu_merge_forward_text", lambda *_args, **_kwargs: partial)
    captured = []
    adapter._on_event = captured.append
    payload = {
        "message_id": "merge-1",
        "external_user_id": "user-1",
        "external_chat_id": "chat-1",
        "raw": {"message_type": "merge_forward", "chat_type": "p2p"},
    }
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8"))
    assert "不能视为完整记录" in captured[0].text
    assert captured[0].attachments[0]["text"] == partial


def test_feishu_forward_media_uses_child_ids_and_archives_only_descendants(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    messages = [
        {"message_id": "nested", "upper_message_id": "merge-1", "msg_type": "merge_forward", "body": {}},
        {"message_id": "picture", "upper_message_id": "nested", "msg_type": "image",
         "body": {"content": '{"image_key":"img-1"}'}},
        {"message_id": "report", "upper_message_id": "nested", "msg_type": "file",
         "body": {"content": '{"file_key":"file-1","file_name":"report.pdf"}'}},
        {"message_id": "same-picture", "upper_message_id": "merge-1", "msg_type": "image",
         "body": {"content": '{"image_key":"img-1"}'}},
        {"message_id": "unrelated", "upper_message_id": "other-merge", "msg_type": "file",
         "body": {"content": '{"file_key":"private-file","file_name":"private.pdf"}'}},
    ]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, json={"code": 0, "data": {"items": messages}},
    ))
    downloaded = []
    picture = b"\x89PNG\r\n\x1a\nforwarded image"
    report = b"%PDF-1.4 forwarded report"

    def fake_download(_client, url, *, mime, name, headers):
        downloaded.append(url)
        assert headers == {"Authorization": "Bearer token"}
        content = picture if "/img-1?" in url else report
        return channel_adapters._attachment_from_bytes(content, mime=mime, name=name)

    monkeypatch.setattr(channel_adapters, "_download_attachment", fake_download)
    captured = []
    adapter._on_event = captured.append
    payload = {
        "message_id": "merge-1", "external_user_id": "user-1", "external_chat_id": "chat-1",
        "raw": {"message_type": "merge_forward", "chat_type": "p2p"},
    }
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8"))

    assert len(downloaded) == 2
    assert any("/messages/report/resources/file-1?type=file" in url for url in downloaded)
    assert any("/messages/same-picture/resources/img-1?type=image" in url for url in downloaded)
    assert not any("private-file" in url for url in downloaded)
    assert len(captured) == 1
    event = captured[0]
    assert "report.pdf" in event.text
    assert "feishu-image.jpg" in event.text
    assert "manifest.json" in event.text
    assert len(event.attachments) == 1
    archive = event.attachments[0]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(archive["data_base64"]))) as bundle:
        contents = [bundle.read(name) for name in bundle.namelist() if name != "manifest.json"]
        assert picture in contents
        assert report in contents
        assert "report.pdf" in bundle.read("manifest.json").decode("utf-8")


def test_feishu_forward_media_limit_is_explicit(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={
        "code": 0, "data": {"items": [{
            "message_id": "picture", "upper_message_id": "merge-1", "msg_type": "image",
            "body": {"content": '{"image_key":"img-1"}'},
        }]},
    }))
    attachments = []
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"},
        attachments=attachments, media_limit=0,
    )
    assert attachments == []
    assert "未下载" in text
    assert "内容尚不完整" in text


def test_feishu_forward_archives_more_than_eight_images(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    messages = [{
        "message_id": f"picture-{index}", "upper_message_id": "merge-1", "msg_type": "image",
        "body": {"content": json.dumps({"image_key": f"img-{index}"})},
    } for index in range(10)]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, json={"code": 0, "data": {"items": messages}},
    ))
    monkeypatch.setattr(channel_adapters, "_download_attachment", lambda _client, url, **kwargs:
        channel_adapters._attachment_from_bytes(
            b"\x89PNG\r\n\x1a\n" + url.encode("ascii"), mime=kwargs["mime"], name=kwargs["name"],
        ))
    captured = []
    adapter._on_event = captured.append
    payload = {
        "message_id": "merge-1", "external_user_id": "user-1", "external_chat_id": "chat-1",
        "raw": {"message_type": "merge_forward", "chat_type": "p2p"},
    }
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8"))
    assert len(captured) == 1
    assert len(captured[0].attachments) == 1
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(captured[0].attachments[0]["data_base64"]))) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert len(manifest["files"]) == 10
        assert len(set(bundle.namelist())) == 11
    assert "内容尚不完整" not in captured[0].text


def test_feishu_forward_media_total_size_limit_is_explicit(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    monkeypatch.setattr(channel_adapters, "FORWARD_MEDIA_MAX_BYTES", 20)
    messages = [{
        "message_id": f"file-{index}", "upper_message_id": "merge-1", "msg_type": "file",
        "body": {"content": json.dumps({"file_key": f"key-{index}", "file_name": f"file-{index}.pdf"})},
    } for index in range(2)]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, json={"code": 0, "data": {"items": messages}},
    ))
    monkeypatch.setattr(channel_adapters, "_download_attachment", lambda *_args, **kwargs:
        channel_adapters._attachment_from_bytes(b"%PDF-1.4 hello", mime=kwargs["mime"], name=kwargs["name"]))
    attachments = []
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"}, attachments=attachments,
    )
    assert len(attachments) == 1
    assert "总大小限额" in text
    assert "未保留" in text
    assert "内容尚不完整" in text


def test_feishu_forward_media_download_failure_keeps_error_file(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(200, json={
        "code": 0, "data": {"items": [{
            "message_id": "picture", "upper_message_id": "merge-1", "msg_type": "image",
            "body": {"content": '{"image_key":"img-1"}'},
        }]},
    }))
    monkeypatch.setattr(channel_adapters, "_download_attachment", lambda *_args, **kwargs:
        channel_adapters._attachment_error(kwargs["name"], "expired resource"))
    attachments = []
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"}, attachments=attachments,
    )
    assert "附件读取失败" in text
    assert "内容尚不完整" in text
    assert len(attachments) == 1
    assert attachments[0]["name"].endswith(".error.txt")
    assert "expired resource" in attachments[0]["text"]


def test_feishu_forward_media_stops_downloads_after_deadline(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    clock = [0.0]
    monkeypatch.setattr(channel_adapters.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter, "_get_feishu_token", lambda: "token")
    messages = [{
        "message_id": f"file-{index}", "upper_message_id": "merge-1", "msg_type": "file",
        "body": {"content": json.dumps({"file_key": f"key-{index}", "file_name": f"file-{index}.pdf"})},
    } for index in range(3)]
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: httpx.Response(
        200, json={"code": 0, "data": {"items": messages}},
    ))
    calls = []

    def fake_attachments(payload, _raw_event, *, refs_override):
        calls.append(payload["message_id"])
        clock[0] += 61
        return [channel_adapters._attachment_from_bytes(
            b"%PDF-1.4 hello", mime="application/pdf", name=refs_override[0]["name"],
        )]

    monkeypatch.setattr(adapter, "_feishu_attachments", fake_attachments)
    attachments = []
    text = adapter._feishu_merge_forward_text(
        {"message_id": "merge-1"}, {"message_type": "merge_forward"}, attachments=attachments,
    )
    assert calls == ["file-0"]
    assert len(attachments) == 1
    assert "下载时间限额" in text
    assert "内容尚不完整" in text


def _ordinary_stream_media_payload(provider: str, count: int) -> dict:
    if provider == "feishu":
        raw = {
            "message_type": "post", "chat_type": "p2p",
            "content": {"content": [[{"tag": "img", "image_key": f"image-{i}"} for i in range(count)]]},
        }
    else:
        raw = {
            "msgtype": "richText", "conversationType": "1", "robotCode": "robot",
            "download_resources": [
                {"download_code": f"image-{i}", "name": f"image-{i}.png", "mime": "image/png"}
                for i in range(count)
            ],
        }
    return {
        "message_id": "ordinary-1", "external_user_id": "user-1", "external_chat_id": "chat-1",
        "text": "图片", "raw": raw,
    }


def _mock_stream_media_client(monkeypatch, *, failed_code: str = "") -> None:
    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, *, json, **_kwargs):
            if json["downloadCode"] == failed_code:
                raise RuntimeError("download URL unavailable")
            return types.SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"downloadUrl": "https://files.example/" + json["downloadCode"]},
            )

    monkeypatch.setattr(httpx, "Client", Client)


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
def test_ordinary_stream_message_archives_more_than_eight_media(monkeypatch, provider) -> None:
    adapter = StreamSDKAdapter(ChannelConfig(id="test", provider=provider, name="test"), {})
    monkeypatch.setattr(adapter, f"_get_{provider}_token", lambda: "token")
    _mock_stream_media_client(monkeypatch)
    monkeypatch.setattr(channel_adapters, "_download_attachment", lambda _client, url, **kwargs:
        channel_adapters._attachment_from_bytes(
            b"\x89PNG\r\n\x1a\n" + url.encode("ascii"), mime=kwargs["mime"], name=kwargs["name"],
        ))
    captured = []
    adapter._on_event = captured.append
    payload = _ordinary_stream_media_payload(provider, 10)
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode())
    assert len(captured) == 1
    assert len(captured[0].attachments) == 1
    assert "10 个媒体文件" in captured[0].text
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(captured[0].attachments[0]["data_base64"]))) as bundle:
        assert len(json.loads(bundle.read("manifest.json"))["files"]) == 10


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
def test_ordinary_stream_auth_failure_preserves_media_only_event(monkeypatch, provider) -> None:
    adapter = StreamSDKAdapter(ChannelConfig(id="test", provider=provider, name="test"), {})

    def fail_token():
        raise RuntimeError("token unavailable")

    monkeypatch.setattr(adapter, f"_get_{provider}_token", fail_token)
    captured = []
    adapter._on_event = captured.append
    payload = _ordinary_stream_media_payload(provider, 1)
    payload["text"] = ""
    adapter._handle_sidecar_line(adapter._EVENT_PREFIX + json.dumps(payload).encode())
    assert len(captured) == 1
    assert "授权失败" in captured[0].text
    assert captured[0].attachments[0]["name"].endswith(".error.txt")


@pytest.mark.parametrize("provider", ["feishu", "dingtalk"])
@pytest.mark.parametrize("budget", ["count", "bytes", "time"])
def test_ordinary_stream_media_budgets_stop_further_downloads(monkeypatch, provider, budget) -> None:
    adapter = StreamSDKAdapter(ChannelConfig(id="test", provider=provider, name="test"), {})
    monkeypatch.setattr(adapter, f"_get_{provider}_token", lambda: "token")
    _mock_stream_media_client(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(channel_adapters.time, "monotonic", lambda: clock[0])
    image_data = b"\x89PNG\r\n\x1a\npixels"
    if budget == "count":
        monkeypatch.setattr(channel_adapters, "FORWARD_MEDIA_MAX_COUNT", 1)
    elif budget == "bytes":
        monkeypatch.setattr(channel_adapters, "FORWARD_MEDIA_MAX_BYTES", len(image_data))
    calls = []

    def fake_download(_client, url, **kwargs):
        calls.append(url)
        if budget == "time":
            clock[0] += 61
        return channel_adapters._attachment_from_bytes(image_data, mime=kwargs["mime"], name=kwargs["name"])

    monkeypatch.setattr(channel_adapters, "_download_attachment", fake_download)
    payload = _ordinary_stream_media_payload(provider, 2)
    warnings = []
    if provider == "feishu":
        attachments = adapter._feishu_attachments(payload, payload["raw"], warnings=warnings)
    else:
        attachments = adapter._dingtalk_attachments(payload["raw"], warnings=warnings)
    assert len(calls) == 1
    assert len(attachments) == 1
    assert "限额" in "".join(warnings)
    assert "1 个附件未下载" in "".join(warnings)


def test_dingtalk_download_url_failure_does_not_drop_later_resources(monkeypatch) -> None:
    adapter = StreamSDKAdapter(ChannelConfig(id="test", provider="dingtalk", name="test"), {})
    monkeypatch.setattr(adapter, "_get_dingtalk_token", lambda: "token")
    _mock_stream_media_client(monkeypatch, failed_code="first")
    downloaded = []

    def fake_download(_client, _url, **kwargs):
        downloaded.append((kwargs["name"], kwargs["mime"]))
        return channel_adapters._attachment_from_bytes(
            b"%PDF-1.4 report", mime=kwargs["mime"], name=kwargs["name"],
        )

    monkeypatch.setattr(channel_adapters, "_download_attachment", fake_download)
    warnings = []
    attachments = adapter._dingtalk_attachments({
        "robotCode": "robot", "msgtype": "richText",
        "download_resources": [
            {"download_code": "first", "name": "photo.png", "mime": "image/png"},
            {"download_code": "second", "name": "report.pdf", "mime": "application/pdf"},
        ],
        "download_codes": ["first", "second"],
    }, warnings=warnings)
    assert len(attachments) == 2
    assert attachments[0]["name"] == "photo.png.error.txt"
    assert attachments[1]["name"] == "report.pdf"
    assert downloaded == [("report.pdf", "application/pdf")]
    assert "photo.png" in "".join(warnings)


def test_dingtalk_sidecar_keeps_mixed_resource_names_and_types() -> None:
    message = types.SimpleNamespace(get_image_list=lambda: ["picture", "document", "picture"])
    resources = sidecar._dingtalk_media_resources(message, {
        "msgtype": "richText", "content": {"richText": [
            {"type": "picture", "downloadCode": "picture", "fileName": "photo.png", "contentType": "image/png"},
            {"type": "file", "downloadCode": "document", "fileName": "report.pdf", "contentType": "application/pdf"},
        ]},
    })
    assert resources == [
        {"download_code": "picture", "name": "photo.png", "mime": "image/png"},
        {"download_code": "document", "name": "report.pdf", "mime": "application/pdf"},
    ]


def test_feishu_stream_handler_dispatches_im_message() -> None:
    seen: list[object] = []
    handler = _FeishuStreamHandler()
    handler.register("p2.im.message.receive_v1", seen.append)
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "token": "t"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": '{"text":"hello"}',
                "create_time": "1700000000000",
            },
        },
    }
    handler.do_without_validation(json.dumps(payload).encode("utf-8"))
    assert len(seen) == 1
    data = seen[0]
    assert data.event.message.chat_id == "oc_1"
    assert data.event.sender.sender_id.open_id == "ou_1"


def test_feishu_stream_handler_ignores_unknown_events() -> None:
    handler = _FeishuStreamHandler()
    payload = {
        "schema": "2.0",
        "header": {"event_type": "im.chat.updated_v1"},
        "event": {},
    }
    assert handler.do_without_validation(json.dumps(payload).encode("utf-8")) is None


def test_feishu_card_action_reads_namespace_value_and_acks(monkeypatch) -> None:
    emitted: list[dict] = []
    monkeypatch.setattr(sidecar, "_event", emitted.append)
    payload = _dict_to_ns({
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_user"},
            "action": {
                "value": {"argusai_action": "ca_token1234567890"},
                "tag": "button",
            },
            "context": {
                "open_chat_id": "oc_chat",
                "open_message_id": "om_card",
            },
        },
    })
    result = _emit_feishu_card_action(payload)
    assert result == {"toast": {"type": "info", "content": "ok"}}
    assert emitted == [{
        "provider": "feishu",
        "external_user_id": "ou_user",
        "external_chat_id": "oc_chat",
        "message_id": "om_card",
        "text": "",
        "timestamp": 0,
        "event_kind": "action",
        "action_token": "ca_token1234567890",
        "action_value": "",
        "raw": {"value": {"argusai_action": "ca_token1234567890"}},
    }]


def test_feishu_stream_handler_dispatches_card_action() -> None:
    seen: list[object] = []
    handler = _FeishuStreamHandler()
    handler.register("p2.card.action.trigger", lambda data: seen.append(data) or {"toast": {}})
    result = handler.do_without_validation(json.dumps({
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou_1"},
            "action": {"value": {"argusai_action": "ca_x"}},
            "context": {"open_chat_id": "oc_1"},
        },
    }).encode("utf-8"))
    assert len(seen) == 1
    assert result == {"toast": {}}


def test_patch_feishu_ws_card_frames_rewrites_card_to_event() -> None:
    from lark_oapi.ws.const import HEADER_TYPE
    from lark_oapi.ws.enum import MessageType

    calls: list[str] = []

    class FakeClient:
        async def _handle_data_frame(self, frame):
            calls.append(frame.headers[0].value)

    _patch_feishu_ws_card_frames(FakeClient)
    assert FakeClient._argusai_card_frames_patched is True

    header = types.SimpleNamespace(key=HEADER_TYPE, value=MessageType.CARD.value)
    frame = types.SimpleNamespace(headers=[header])

    import asyncio
    asyncio.run(FakeClient()._handle_data_frame(frame))
    assert calls == [MessageType.EVENT.value]
    assert header.value == MessageType.EVENT.value


def test_load_feishu_ws_avoids_generated_api_surface() -> None:
    """Import path used by the sidecar must not pull lark_oapi.api.*."""
    before = {name for name in sys.modules if name.startswith("lark_oapi.api")}
    Client, LogLevel = _load_feishu_ws()
    after = {name for name in sys.modules if name.startswith("lark_oapi.api")}
    assert Client is not None
    assert LogLevel.WARNING is not None
    assert after == before
    assert getattr(Client, "_argusai_card_frames_patched", False) is True
    # websockets.legacy is required by lark_oapi.ws.client (InvalidStatusCode).
    import websockets.legacy  # noqa: F401


def test_a_frozen_build_loads_without_a_package_directory(monkeypatch) -> None:
    """A packaged build compiles lark_oapi in, so its spec has no directory.

    The stubs that skip ``lark_oapi/__init__.py`` used to insist on one, which
    turned a perfectly good frozen build into "lark_oapi is not installed".
    """
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    real_root = Path(
        list(importlib.util.find_spec("lark_oapi").submodule_search_locations)[0]
    )

    class ByNameFinder:
        """Resolve dotted names directly, the way a frozen loader does."""

        @staticmethod
        def find_spec(name, path=None, target=None):
            if not name.startswith("lark_oapi."):
                return None
            parent = real_root.joinpath(*name.split(".")[1:-1])
            return importlib.machinery.PathFinder.find_spec(name, [str(parent)])

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "lark_oapi" or name.startswith("lark_oapi.")
    }
    for name in saved:
        del sys.modules[name]

    pathless = types.SimpleNamespace(submodule_search_locations=None)
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **k: pathless if name == "lark_oapi" else None,
    )
    sys.meta_path.insert(0, ByNameFinder)
    try:
        Client, LogLevel = _load_feishu_ws()
        assert Client is not None
        assert LogLevel.WARNING is not None
        assert sys.modules["lark_oapi"].__path__ == []
        assert not any(name.startswith("lark_oapi.api") for name in sys.modules)
    finally:
        sys.meta_path.remove(ByNameFinder)
        for name in [
            name for name in sys.modules
            if name == "lark_oapi" or name.startswith("lark_oapi.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved)
