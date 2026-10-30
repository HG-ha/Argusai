from __future__ import annotations

import base64
import json

import httpx
import pytest

from argusai.core.channels import ChannelConfig, ChannelEvent, create_channel_adapter
from argusai.core.channels.cards import render_channel_reply
from argusai.core.channels.models import ChannelDeliveryError, ChannelReply
from argusai.core.channels.adapters import (
    ILinkSessionExpired,
    WeChatILinkAdapter,
    ilink_check,
    ilink_decrypt,
    ilink_encrypt,
    ilink_media_key,
    ilink_split_text,
    ilink_uin,
)


def _adapter(**settings) -> WeChatILinkAdapter:
    return WeChatILinkAdapter(
        ChannelConfig(
            id="wechat-test",
            provider="wechat",
            name="WeChat",
            settings={"user_id": "u@im.wechat", "bot_id": "b@im.bot", **settings},
        ),
        {"bot_token": "ilinkbot_test"},
    )


def _mock_client(monkeypatch, handler):
    """Route every httpx.Client the adapter opens through one mock transport."""
    calls: list[httpx.Request] = []
    real_client = httpx.Client

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    def factory(*_args, **_kwargs):
        return real_client(transport=httpx.MockTransport(record))

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.Client", factory)
    return calls


def test_provider_routes_to_the_ilink_adapter() -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="wechat-1", provider="wechat", name="WeChat"),
        {"bot_token": "t"},
    )
    assert isinstance(adapter, WeChatILinkAdapter)


def test_uin_header_is_base64_of_a_decimal_uint32() -> None:
    decoded = base64.b64decode(ilink_uin()).decode("ascii")
    assert decoded.isdigit()
    assert 0 <= int(decoded) <= 0xFFFFFFFF


def test_session_expiry_is_distinct_from_an_ordinary_rejection() -> None:
    assert ilink_check({"ret": 0}, "x") == {"ret": 0}
    with pytest.raises(ILinkSessionExpired):
        ilink_check({"ret": -14, "errmsg": "session timeout"}, "poll")
    with pytest.raises(ChannelDeliveryError):
        ilink_check({"ret": -2, "errmsg": "bad params"}, "send")


def test_media_key_accepts_both_encodings_tencent_ships() -> None:
    raw = bytes.fromhex("00112233445566778899aabbccddeeff")
    as_bytes = base64.b64encode(raw).decode("ascii")
    as_hex_text = base64.b64encode(raw.hex().encode("ascii")).decode("ascii")

    assert ilink_media_key({"aes_key": as_bytes}) == raw
    assert ilink_media_key({"aes_key": as_hex_text}) == raw
    # An image item's bare hex `aeskey` wins over the media block.
    assert ilink_media_key({"aes_key": as_bytes}, raw.hex()) == raw


def test_media_round_trips_through_aes_ecb() -> None:
    key = bytes(range(16))
    payload = b"argusai wechat attachment"
    encrypted = ilink_encrypt(payload, key)
    assert len(encrypted) % 16 == 0
    assert len(encrypted) == ((len(payload) + 1 + 15) // 16) * 16
    assert ilink_decrypt(encrypted, key) == payload


def test_long_text_splits_on_paragraph_boundaries() -> None:
    body = ("a" * 1200) + "\n\n" + ("b" * 1200)
    chunks = ilink_split_text(body)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 1200
    assert chunks[1] == "b" * 1200
    assert ilink_split_text("short") == ["short"]
    assert ilink_split_text("   ") == []


def test_every_line_is_spaced_because_wechat_eats_single_breaks() -> None:
    """An iLink bubble folds a lone newline into a space, like a soft wrap.

    Without widening, a card title runs into its body and a bullet list lands
    as one run-on sentence.
    """
    reply = ChannelReply(
        external_chat_id="chat-1",
        text="",
        blocks=[{
            "type": "info",
            "title": "Quick enable",
            "body": "For this conversation:\n• tools\n• live",
            "actions": [{"label": "Enable", "token": "ca_1"}],
        }],
    )

    text = render_channel_reply("wechat", reply)["text"]

    assert text.splitlines() == [
        "Quick enable",
        "",
        "For this conversation:",
        "",
        "• tools",
        "",
        "• live",
        "",
        "• Enable — /act ca_1",
    ]
    # The title keeps its plain form: markdown emphasis would show literally.
    assert "**" not in text


def test_paragraph_spacing_is_not_widened_twice() -> None:
    reply = ChannelReply(
        external_chat_id="chat-1",
        text="First paragraph.\n\n\nSecond paragraph.",
        blocks=[],
    )

    assert render_channel_reply("wechat", reply)["text"] == (
        "First paragraph.\n\nSecond paragraph."
    )


def test_inbound_message_caches_the_context_token(monkeypatch) -> None:
    adapter = _adapter()
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))

    adapter._emit_message(client, {
        "message_type": 1,
        "from_user_id": "u@im.wechat",
        "message_id": 42,
        "context_token": "CTX-1",
        "create_time_ms": 1774158905123,
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    })

    assert [event.text for event in received] == ["hello"]
    assert received[0].external_chat_id == "u@im.wechat"
    assert adapter._context_token("u@im.wechat") == "CTX-1"


def test_the_bots_own_echo_is_not_replayed_as_user_input() -> None:
    adapter = _adapter()
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))

    adapter._emit_message(client, {
        "message_type": 2,
        "from_user_id": "b@im.bot",
        "context_token": "CTX-1",
        "item_list": [{"type": 1, "text_item": {"text": "my own reply"}}],
    })

    assert received == []


def test_many_wechat_files_are_saved_in_one_readable_archive(tmp_path) -> None:
    import zipfile
    from argusai.core.assistant.attachments import _materialize_binary_attachments, _sanitize_attachments

    adapter = _adapter()
    received = []
    adapter._on_event = received.append
    key = bytes(range(16))
    payloads = {str(index): f"file-{index}".encode() for index in range(12)}
    def respond(request):
        value = payloads[request.url.params["encrypted_query_param"]]
        return httpx.Response(200, content=ilink_encrypt(value, key))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter._emit_message(client, {
            "from_user_id": "user", "message_id": "many-media",
            "item_list": [{"type": 4, "file_item": {
                "file_name": "same.bin", "media": {
                    "encrypt_query_param": index,
                    "aes_key": base64.b64encode(key).decode(),
                },
            }} for index in payloads],
        })
    event = received[0]
    assert "manifest.json" in event.text
    assert len(event.attachments) == 1
    stored, _ = _materialize_binary_attachments(_sanitize_attachments(list(event.attachments)), tmp_path)
    with zipfile.ZipFile(stored[0]["sandbox_path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["files"]) == 12
        assert [archive.read(item["path"]) for item in manifest["files"]] == list(payloads.values())


@pytest.mark.parametrize("limit_kind", ["count", "bytes", "time"])
def test_wechat_media_limits_report_omitted_files(monkeypatch, limit_kind) -> None:
    import zipfile
    import io
    from argusai.core.channels import adapters
    from argusai.core.channels.media import attachment_from_bytes, to_assistant_dict

    adapter = _adapter()
    received = []
    calls = []
    clock = {"now": 0.0}
    adapter._on_event = received.append
    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock["now"])
    if limit_kind == "bytes":
        monkeypatch.setattr(adapters, "FORWARD_MEDIA_MAX_BYTES", 8)

    def download(_client, _item, _kind, name, *, timeout, max_bytes):
        calls.append((timeout, max_bytes))
        if limit_kind == "time":
            clock["now"] += 61
        return to_assistant_dict(attachment_from_bytes(b"data", name=name))
    monkeypatch.setattr(adapter, "_download_media", download)
    count = 65 if limit_kind == "count" else 3
    adapter._emit_message(None, {
        "from_user_id": "user", "message_id": "limited",
        "item_list": [{"type": 4, "file_item": {"file_name": f"{i}.bin"}} for i in range(count)],
    })
    assert len(calls) == {"count": 64, "bytes": 2, "time": 1}[limit_kind]
    assert all(0 < timeout <= 60 for timeout, _size in calls)
    assert "未留存" in received[0].text
    if limit_kind == "count":
        payload = base64.b64decode(received[0].attachments[0]["data_base64"])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(json.loads(archive.read("manifest.json"))["files"]) == 64
    else:
        assert len(received[0].attachments) == len(calls)


def test_wechat_empty_media_remains_a_visible_failure() -> None:
    adapter = _adapter()
    received = []
    adapter._on_event = received.append
    key = bytes(range(16))
    with httpx.Client(transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=ilink_encrypt(b"", key)),
    )) as client:
        adapter._emit_message(client, {
            "from_user_id": "user", "message_id": "empty-media",
            "item_list": [{"type": 4, "file_item": {
                "file_name": "empty.bin", "media": {
                    "encrypt_query_param": "empty", "aes_key": base64.b64encode(key).decode(),
                },
            }}],
        })
    assert len(received) == 1
    assert "下载失败" in received[0].text
    assert "empty media" in received[0].attachments[0]["text"]


def test_group_messages_are_not_accepted() -> None:
    adapter = _adapter()
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))

    adapter._emit_message(client, {
        "message_type": 1,
        "from_user_id": "u@im.wechat",
        "group_id": "g@im.chatroom",
        "context_token": "CTX-G",
        "item_list": [{"type": 1, "text_item": {"text": "hi all"}}],
    })

    assert received == []
    assert adapter._context_token("g@im.chatroom") == "CTX-G"


def test_send_without_a_context_token_fails_permanently() -> None:
    adapter = _adapter()
    with pytest.raises(ChannelDeliveryError) as excinfo:
        adapter.send(ChannelReply(text="hi", external_chat_id="u@im.wechat"))
    assert excinfo.value.permanent is True


def test_send_echoes_the_context_token_and_varies_client_ids(monkeypatch) -> None:
    adapter = _adapter()
    adapter._contexts["u@im.wechat"] = "CTX-1"
    calls = _mock_client(monkeypatch, lambda _r: httpx.Response(200, json={"ret": 0}))

    adapter.send(ChannelReply(
        text=("x" * 1500) + "\n\n" + ("y" * 1500),
        external_chat_id="u@im.wechat",
    ))

    bodies = [json.loads(request.content) for request in calls]
    assert len(bodies) == 2
    assert {body["msg"]["context_token"] for body in bodies} == {"CTX-1"}
    assert {body["msg"]["message_type"] for body in bodies} == {2}
    assert {body["msg"]["message_state"] for body in bodies} == {2}
    # A shared client_id makes WeChat collapse the parts into one bubble.
    assert len({body["msg"]["client_id"] for body in bodies}) == 2
    headers = calls[0].headers
    assert headers["authorizationtype"] == "ilink_bot_token"
    assert headers["authorization"] == "Bearer ilinkbot_test"
    assert headers["x-wechat-uin"]


def test_polling_backs_off_instead_of_hammering_an_expired_session(monkeypatch) -> None:
    adapter = _adapter()
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getconfig"):
            return httpx.Response(200, json={"ret": -14, "errmsg": "session timeout"})
        return httpx.Response(200, json={"ret": 0, "msgs": []})

    _mock_client(monkeypatch, handler)

    def fake_wait(timeout=None):
        waits.append(float(timeout or 0))
        adapter._stop.set()
        return True

    monkeypatch.setattr(adapter._stop, "wait", fake_wait)
    adapter._loop()

    assert waits == [300]
    assert "Scan the QR code again" in adapter._last_error
    assert adapter._connected is False


def test_polling_stores_the_cursor_and_emits_messages(monkeypatch) -> None:
    adapter = _adapter()
    received: list[ChannelEvent] = []
    adapter._on_event = received.append

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getconfig"):
            return httpx.Response(200, json={"ret": 0})
        body = json.loads(request.content)
        assert body["base_info"]["channel_version"]
        if body.get("get_updates_buf"):
            adapter._stop.set()
            return httpx.Response(200, json={"ret": 0, "msgs": []})
        return httpx.Response(200, json={
            "ret": 0,
            "get_updates_buf": "CURSOR-2",
            "msgs": [{
                "message_type": 1,
                "from_user_id": "u@im.wechat",
                "context_token": "CTX-1",
                "item_list": [{"type": 1, "text_item": {"text": "ping"}}],
            }],
        })

    calls = _mock_client(monkeypatch, handler)
    adapter._loop()

    assert adapter._cursor == "CURSOR-2"
    assert [event.text for event in received] == ["ping"]
    assert any(request.url.path.endswith("/getconfig") for request in calls)


def test_loop_handshakes_before_polling(monkeypatch) -> None:
    adapter = _adapter()
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        order.append(path.rsplit("/", 1)[-1])
        adapter._stop.set()
        return httpx.Response(200, json={"ret": 0, "msgs": []})

    _mock_client(monkeypatch, handler)
    adapter._loop()
    assert order[0] == "getconfig"
    assert "getupdates" in order


def test_file_upload_encrypts_and_reports_the_ciphertext_size(monkeypatch) -> None:
    adapter = _adapter()
    adapter._contexts["u@im.wechat"] = "CTX-1"
    payload = b"report bytes"
    uploaded: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getuploadurl"):
            body = json.loads(request.content)
            assert body["rawsize"] == len(payload)
            assert body["filesize"] == ((len(payload) + 1 + 15) // 16) * 16
            assert len(body["aeskey"]) == 32
            uploaded["key"] = bytes.fromhex(body["aeskey"])
            return httpx.Response(200, json={"ret": 0, "upload_param": "UP-1"})
        if request.url.path.endswith("/upload"):
            uploaded["cipher"] = request.content
            return httpx.Response(200, headers={"x-encrypted-param": "ENC-1"})
        body = json.loads(request.content)
        uploaded["item"] = body["msg"]["item_list"][0]
        return httpx.Response(200, json={"ret": 0})

    _mock_client(monkeypatch, handler)
    adapter.send(ChannelReply(
        text="",
        external_chat_id="u@im.wechat",
        attachments=({
            "kind": "file",
            "name": "report.bin",
            "mime": "application/octet-stream",
            "data_base64": base64.b64encode(payload).decode("ascii"),
        },),
    ))

    assert ilink_decrypt(uploaded["cipher"], uploaded["key"]) == payload
    item = uploaded["item"]
    assert item["type"] == 4
    assert item["file_item"]["file_name"] == "report.bin"
    assert item["file_item"]["len"] == str(len(payload))
    # Tencent's client base64s the hex text of the key, not the raw bytes.
    assert base64.b64decode(item["file_item"]["media"]["aes_key"]).decode() == uploaded["key"].hex()


def test_qr_request_draws_the_code_because_tencent_only_returns_a_link(monkeypatch) -> None:
    """``qrcode_img_content`` is the URL to encode, not a picture of one."""
    from argusai.core.channels import adapters

    link = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=abc123&bot_type=3"
    monkeypatch.setattr(
        adapters.httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json={"ret": 0, "qrcode": "abc123", "qrcode_img_content": link},
            request=httpx.Request("GET", "https://ilinkai.weixin.qq.com"),
        ),
    )

    result = adapters.ilink_request_qrcode()

    assert result["qrcode"] == "abc123"
    assert result["link"] == link
    # Handing the raw link through as "image" is what left the dialog blank.
    assert result["image"].startswith("data:image/svg+xml")
    assert link not in result["image"]


def test_a_missing_encoder_degrades_to_the_bare_link() -> None:
    from argusai.core.channels.adapters import ilink_qrcode_image

    assert ilink_qrcode_image("") == ""


def _login_host(tmp_path):
    """A ChannelsMixin wired to a real store, for the QR sign-in wrappers."""
    from types import SimpleNamespace

    from argusai.core.channels import ChannelStore
    from argusai.gui.channels import ChannelsMixin

    store = ChannelStore(tmp_path / "channels")

    class Host(ChannelsMixin):
        settings_mgr = SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        )

        def _channels_store(self):
            return store

        def _channels_runtime(self):
            return SimpleNamespace(refresh=lambda: None)

    return Host(), store


def test_qr_sign_in_stores_the_token_and_the_returned_identity(tmp_path, monkeypatch) -> None:
    host, store = _login_host(tmp_path)
    # Saved disabled, because at save time there is no token to enable with.
    store.upsert_channel({
        "id": "wechat-1",
        "provider": "wechat",
        "name": "My WeChat",
        "enabled": False,
        "settings": {"cdn_url": "https://keep.me"},
    })
    monkeypatch.setattr(
        "argusai.gui.channels.ilink_qrcode_status",
        lambda _code: {
            "status": "confirmed",
            "bot_token": "ilinkbot_secret",
            "ilink_bot_id": "b@im.bot",
            "ilink_user_id": "u@im.wechat",
            "baseurl": "https://ilinkai.weixin.qq.com",
        },
    )

    result = host.poll_wechat_login("QR-1", "wechat-1")

    assert result["ok"] is True and result["status"] == "confirmed"
    # The token belongs in the encrypted store, never in the reply to the webview.
    assert "ilinkbot_secret" not in json.dumps(result)
    assert store.secrets.get_many("wechat-1")["bot_token"] == "ilinkbot_secret"
    settings = store.get_channel("wechat-1").settings
    assert settings["bot_id"] == "b@im.bot"
    assert settings["user_id"] == "u@im.wechat"
    assert settings["cdn_url"] == "https://keep.me"
    # Nothing polls a disabled channel, so WeChat would keep showing the bot as
    # unreachable and /pair would never arrive.
    assert store.get_channel("wechat-1").enabled is True


def test_an_unconfirmed_scan_only_reports_progress(tmp_path, monkeypatch) -> None:
    host, store = _login_host(tmp_path)
    store.upsert_channel({"id": "wechat-1", "provider": "wechat", "name": "My WeChat"})
    monkeypatch.setattr(
        "argusai.gui.channels.ilink_qrcode_status",
        lambda _code: {"status": "scanned"},
    )

    assert host.poll_wechat_login("QR-1", "wechat-1") == {"ok": True, "status": "scanned"}
    assert store.secrets.get_many("wechat-1") == {}


def test_sign_in_refuses_a_channel_of_another_provider(tmp_path, monkeypatch) -> None:
    host, store = _login_host(tmp_path)
    store.upsert_channel({"id": "tg-1", "provider": "telegram", "name": "TG"})
    monkeypatch.setattr(
        "argusai.gui.channels.ilink_qrcode_status",
        lambda _code: {"status": "confirmed", "bot_token": "ilinkbot_secret"},
    )

    assert host.poll_wechat_login("QR-1", "tg-1")["ok"] is False
    assert host.poll_wechat_login("QR-1", "")["ok"] is False
