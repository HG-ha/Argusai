"""Tests for the Feishu/DingTalk stream sidecar helpers."""

from __future__ import annotations

import json
import sys
import types

from argusai.core.channels import stream_sidecar as sidecar
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
