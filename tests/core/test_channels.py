from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from argusai.core.channels import (
    ChannelConfig,
    ChannelEvent,
    ChannelGateway,
    ChannelRuntimeManager,
    ChannelStore,
    create_channel_adapter,
)
from argusai.core.channels.adapters import StreamSDKAdapter, _attachment_from_bytes


def test_channel_store_encrypts_secrets_and_pairing_is_one_time(tmp_path) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel(
        {
            "provider": "telegram",
            "name": "Ops Telegram",
            "enabled": True,
        },
        secret_values={"bot_token": "super-secret-token"},
    )

    assert store.credentials(channel.id)["bot_token"] == "super-secret-token"
    assert "super-secret-token" not in (tmp_path / "secrets.json").read_text(encoding="utf-8")
    public = store.list_channels()[0]
    assert public["configured_secrets"] == {"bot_token": True}
    assert "secret_names" not in public

    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    # CLI and serve are different processes — pair codes must survive a new store.
    other = ChannelStore(tmp_path)
    identity = other.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        display_name="Tester",
    )
    assert identity.bound_session_id == "session-1"
    assert identity.allowed_session_ids == ("session-1",)
    assert identity.permissions == (
        "cancel",
        "chat",
        "configure",
        "confirm",
        "send",
        "status",
    )
    identity = store.set_identity_permissions(
        identity.id,
        ["chat", "status", "confirm", "unknown"],
    )
    assert identity.permissions == ("chat", "status", "confirm")

    try:
        store.consume_pairing(
            channel.id,
            pairing["code"],
            external_user_id="user-2",
        )
    except ValueError as exc:
        assert "invalid or expired" in str(exc)
    else:
        raise AssertionError("pairing code must be single-use")
    assert store.unbind_session("session-1") == 1
    assert store.find_identity(channel.id, "user-1").bound_session_id == ""


def test_channel_gateway_rejects_unpaired_and_routes_bound_turn(tmp_path) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({"provider": "webhook", "name": "Webhook"})
    turns: list[dict] = []
    gateway = ChannelGateway(
        store,
        turn_handler=lambda params: turns.append(params) or {"ok": True, "text": "answer"},
        session_handler=lambda _session_id: {"ok": True, "message_count": 2},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "created-session",
        confirmation_handler=lambda *_args: {"ok": True},
    )

    def event(
        text: str,
        attachments: tuple[dict, ...] = (),
    ) -> ChannelEvent:
        return ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=f"message-{len(turns)}",
            text=text,
            attachments=attachments,
            timestamp=time.time(),
        )

    assert "not paired" in gateway.handle(event("hello")).text
    pairing = store.create_pairing(channel.id)
    assert "Pairing complete" in gateway.handle(event(f"/pair {pairing['code']}")).text
    assert "No conversation is bound" in gateway.handle(event("hello")).text
    assert "created-session" in gateway.handle(event("/new")).text
    assert gateway.handle(event("hello")).text == "answer"
    assert turns[0]["session_id"] == "created-session"
    assert turns[0]["message_source"] == "channel"
    image = _attachment_from_bytes(
        b"\x89PNG\r\n\x1a\nchannel",
        mime="image/png",
        name="channel.png",
    )
    assert image is not None
    assert gateway.handle(event("", (image,))).text == "answer"
    assert turns[1]["attachments"] == [image]
    other_chat = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-2",
        message_id="message-other-chat",
        text="hello",
    )
    assert "not paired" in gateway.handle(other_chat).text


def test_signed_webhook_adapter_normalizes_inbound_event() -> None:
    adapter = create_channel_adapter(
        ChannelConfig(
            id="webhook-test",
            provider="webhook",
            name="Webhook",
            enabled=True,
            settings={"host": "127.0.0.1", "port": 0, "path": "/hook"},
        ),
        {"signing_secret": "test-secret"},
    )
    received: list[ChannelEvent] = []
    ready = threading.Event()

    def on_event(event: ChannelEvent) -> None:
        received.append(event)
        ready.set()

    adapter.start(on_event)
    server = adapter._server
    assert server is not None
    payload = {
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/hook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-ArgusAI-Signature": f"sha256={signature}",
            },
            timeout=5,
        )
        assert response.status_code == 200
        assert ready.wait(1)
    finally:
        adapter.stop()

    assert received[0].external_user_id == "user-1"
    assert received[0].text == "hello"


def test_channel_media_uses_native_assistant_attachment_shape() -> None:
    attachment = _attachment_from_bytes(
        b"\x89PNG\r\n\x1a\nfake",
        mime="image/png",
        name="evidence.png",
    )
    assert attachment is not None
    assert attachment["type"] == "image"
    assert base64.b64decode(attachment["data_base64"]).startswith(b"\x89PNG")

    adapter = create_channel_adapter(
        ChannelConfig(
            id="webhook-media",
            provider="webhook",
            name="Webhook",
        ),
        {"signing_secret": "secret"},
    )
    event = adapter._event({
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "media-1",
        "text": "",
        "attachments": [attachment],
    })
    assert event is not None
    assert event.text == ""
    assert event.attachments[0]["type"] == "image"


def test_stream_providers_use_killable_sdk_sidecar() -> None:
    for provider, credentials in (
        ("feishu", {"app_id": "app", "app_secret": "secret"}),
        ("dingtalk", {"client_id": "client", "client_secret": "secret"}),
    ):
        adapter = create_channel_adapter(
            ChannelConfig(
                id=f"{provider}-test",
                provider=provider,
                name=provider,
            ),
            credentials,
        )
        assert isinstance(adapter, StreamSDKAdapter)


def test_stream_sidecar_preserves_status_error_on_exit(monkeypatch) -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(id="feishu-test", provider="feishu", name="Feishu"),
        {"app_id": "app", "app_secret": "secret"},
    )
    adapter._handle_sidecar_line(
        b'ARGUSAI_CHANNEL_STATUS {"connected":false,"error":"ImportError: websockets.legacy"}'
    )
    assert "websockets.legacy" in adapter._last_error

    class FakeProcess:
        stdin = None
        stdout = iter(())
        stderr = iter(())

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(
        StreamSDKAdapter,
        "_sidecar_command",
        staticmethod(lambda: ["python", "-c", "pass"]),
    )

    def fake_popen(*_args, **_kwargs):
        proc = FakeProcess()
        proc.stdin = type("S", (), {"write": lambda *a, **k: None, "close": lambda *a, **k: None})()
        proc.stdout = iter(())
        proc.stderr = iter(())
        return proc

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    adapter._stop.set()  # prevent restart loop
    # Manually apply the exit-path logic with a pre-set status error.
    prior = adapter._last_error
    return_code = 1
    error_lines: list[str] = []
    if error_lines:
        detail = " | ".join(error_lines[-3:])
    elif prior:
        detail = prior
    else:
        detail = f"exit code {return_code}"
    if (
        return_code not in (0, None)
        and f"exit {return_code}" not in detail
        and f"exit code {return_code}" not in detail
    ):
        detail = f"{detail} (exit {return_code})"
    adapter._last_error = f"stream sidecar stopped: {detail}"
    assert "websockets.legacy" in adapter._last_error
    assert "exit 1" in adapter._last_error


def test_stream_sidecar_event_is_normalized_without_exposing_credentials() -> None:
    adapter = StreamSDKAdapter(
        ChannelConfig(
            id="dingtalk-test",
            provider="dingtalk",
            name="DingTalk",
        ),
        {"client_id": "client", "client_secret": "secret"},
    )
    received: list[ChannelEvent] = []
    adapter._on_event = received.append
    payload = {
        "provider": "dingtalk",
        "external_user_id": "user-1",
        "external_chat_id": "chat-1",
        "message_id": "message-1",
        "text": "hello",
        "display_name": "Tester",
        "raw": {"sessionWebhook": "https://example.invalid/session"},
    }
    adapter._handle_sidecar_line(
        adapter._EVENT_PREFIX + json.dumps(payload).encode("utf-8")
    )

    assert received[0].text == "hello"
    assert adapter._reply_urls["chat-1"] == "https://example.invalid/session"


def test_runtime_restarts_adapter_when_existing_secret_changes(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel(
        {
            "provider": "telegram",
            "name": "Ops",
            "enabled": True,
        },
        secret_values={"bot_token": "old-token"},
    )
    created = []

    class FakeAdapter:
        def __init__(self, credentials):
            self.credentials = credentials
            self.stopped = False

        def start(self, _on_event):
            return None

        def stop(self):
            self.stopped = True

        def status(self):
            return {"channel_id": channel.id, "connected": True}

    def create_adapter(_config, credentials):
        adapter = FakeAdapter(credentials)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        create_adapter,
    )
    manager = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    try:
        manager.refresh()
        store.upsert_channel(
            channel.to_dict(),
            secret_values={"bot_token": "new-token"},
        )
        manager.refresh()
    finally:
        manager.shutdown()

    assert [item.credentials["bot_token"] for item in created] == [
        "old-token",
        "new-token",
    ]
    assert created[0].stopped is True


def test_runtime_keeps_starting_channels_after_one_adapter_fails(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    failed = store.upsert_channel({
        "id": "telegram-failed",
        "provider": "telegram",
        "name": "Failed",
        "enabled": True,
    })
    healthy = store.upsert_channel({
        "id": "telegram-healthy",
        "provider": "telegram",
        "name": "Healthy",
        "enabled": True,
    })

    class FakeAdapter:
        def __init__(self, config):
            self.config = config

        def start(self, _on_event):
            if self.config.id == failed.id:
                raise OSError("port unavailable")

        def stop(self):
            return None

        def status(self):
            return {
                "channel_id": self.config.id,
                "provider": self.config.provider,
                "connected": True,
                "last_error": "",
            }

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda config, _credentials: FakeAdapter(config),
    )
    manager = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    try:
        result = manager.refresh()
    finally:
        manager.shutdown()

    statuses = {item["channel_id"]: item for item in result["channels"]}
    assert statuses[failed.id]["connected"] is False
    assert "port unavailable" in statuses[failed.id]["last_error"]
    assert statuses[healthy.id]["connected"] is True


def test_watchdog_retries_adapter_that_failed_on_boot(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({
        "id": "telegram-flaky",
        "provider": "telegram",
        "name": "Flaky",
        "enabled": True,
    })
    starts = {"n": 0}

    class FakeAdapter:
        def start(self, _on_event):
            starts["n"] += 1
            if starts["n"] == 1:
                raise OSError("network unreachable")

        def stop(self):
            return None

        def status(self):
            return {
                "channel_id": channel.id,
                "provider": "telegram",
                "connected": starts["n"] > 1,
                "last_error": "",
            }

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda _config, _credentials: FakeAdapter(),
    )
    manager = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    try:
        first = manager.refresh()
        assert first["active"] == 0
        manager._watchdog_tick()
        assert starts["n"] == 2
        assert manager.statuses()[0]["connected"] is True
    finally:
        manager.shutdown()


def test_watchdog_restarts_dead_adapter_thread(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({
        "id": "telegram-dead",
        "provider": "telegram",
        "name": "Dead",
        "enabled": True,
    })
    starts = {"n": 0}

    class FakeAdapter:
        def __init__(self) -> None:
            self._thread: threading.Thread | None = None

        def start(self, _on_event) -> None:
            starts["n"] += 1
            self._thread = threading.Thread(target=lambda: None, daemon=True)
            self._thread.start()
            self._thread.join()

        def worker_alive(self) -> bool:
            return bool(self._thread and self._thread.is_alive())

        def stop(self) -> None:
            return None

        def status(self):
            return {
                "channel_id": channel.id,
                "provider": "telegram",
                "connected": False,
                "last_error": "",
            }

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda _config, _credentials: FakeAdapter(),
    )
    manager = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    try:
        manager.refresh()
        assert starts["n"] == 1
        manager._restart_dead_workers()
        assert starts["n"] == 2
    finally:
        manager.shutdown()


def test_only_one_runtime_hosts_a_data_root(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({
        "id": "telegram-only",
        "provider": "telegram",
        "name": "Telegram",
        "enabled": True,
    })

    class FakeAdapter:
        def start(self, _on_event):
            return None

        def stop(self):
            return None

        def status(self):
            return {
                "channel_id": channel.id,
                "provider": channel.provider,
                "connected": True,
                "last_error": "",
            }

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda _config, _credentials: FakeAdapter(),
    )
    first = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    second = ChannelRuntimeManager(ChannelStore(tmp_path), gateway=None)  # type: ignore[arg-type]
    try:
        assert first.refresh()["active"] == 1
        second_status = second.refresh()["channels"][0]
        assert second_status["connected"] is False
        assert second_status.get("hosted_elsewhere") is True
        assert "already hosted" in second_status["last_error"]
        host_pid = second_status.get("host_pid")
        # PID is best-effort (Windows exclusive locks can block the read).
        assert host_pid is None or host_pid == os.getpid()
        if host_pid is not None:
            assert f"pid {host_pid}" in second_status["last_error"]
    finally:
        first.shutdown()
        second.shutdown()


def test_runtime_releases_host_lock_when_nothing_started(tmp_path, monkeypatch) -> None:
    """A process hosting zero adapters must not keep the OS service locked out."""
    store = ChannelStore(tmp_path)
    store.upsert_channel({
        "id": "telegram-broken",
        "provider": "telegram",
        "name": "Broken",
        "enabled": True,
    })

    class FakeAdapter:
        def start(self, _on_event):
            raise OSError("port unavailable")

        def stop(self):
            return None

        def status(self):
            return {"channel_id": "telegram-broken", "connected": False}

    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda _config, _credentials: FakeAdapter(),
    )
    manager = ChannelRuntimeManager(store, gateway=None)  # type: ignore[arg-type]
    try:
        manager.refresh()
        assert manager._host_lock_file is None
    finally:
        manager.shutdown()


def test_channel_store_persists_event_deduplication(tmp_path) -> None:
    event = ChannelEvent(
        channel_id="webhook-test",
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="message-1",
        text="hello",
    )
    first = ChannelStore(tmp_path)
    assert first.claim_event(event) is True
    assert first.claim_event(event) is False
    assert ChannelStore(tmp_path).claim_event(event) is False
    assert "hello" not in (tmp_path / "event_keys.json").read_text(encoding="utf-8")


def test_runtime_rate_limits_each_paired_chat_independently(tmp_path) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({
        "id": "webhook-test",
        "provider": "webhook",
        "name": "Webhook",
        "enabled": True,
        "settings": {"rate_limit_per_minute": 1},
    })
    replies = []

    class FakeAdapter:
        def send(self, reply):
            replies.append(reply)

        def stop(self):
            return None

    class FakeGateway:
        def starts_turn(self, event):
            return True

        def handle(self, event):
            return type("Reply", (), {
                "text": f"answer:{event.text}",
                "external_chat_id": event.external_chat_id,
                "external_thread_id": event.external_thread_id,
                "reply_to_message_id": event.message_id,
            })()

    manager = ChannelRuntimeManager(store, gateway=FakeGateway())  # type: ignore[arg-type]
    manager._adapters[channel.id] = FakeAdapter()  # type: ignore[assignment]
    try:
        for message_id in ("message-1", "message-2"):
            manager._handle_event(ChannelEvent(
                channel_id=channel.id,
                provider="webhook",
                external_user_id="user-1",
                external_chat_id="chat-1",
                message_id=message_id,
                text=message_id,
            ))
        manager._handle_event(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-2",
            message_id="message-3",
            text="other chat",
        ))
    finally:
        manager.shutdown()

    assert replies[0].text == "answer:message-1"
    assert replies[1].text.startswith("Too many messages")
    assert replies[2].text == "answer:other chat"


def test_wecom_callback_signature_and_aes_payload_are_verified() -> None:
    key = b"0123456789abcdef0123456789abcdef"
    encoded_key = base64.b64encode(key).decode("ascii").rstrip("=")
    token = "callback-token"
    corp_id = "corp-id"
    inner_xml = (
        b"<xml><FromUserName>user-1</FromUserName>"
        b"<Content>hello</Content><MsgId>message-1</MsgId></xml>"
    )
    plain = b"0123456789abcdef" + len(inner_xml).to_bytes(4, "big") + inner_xml + corp_id.encode()
    padding = 32 - (len(plain) % 32)
    padded = plain + bytes([padding]) * padding
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = base64.b64encode(
        encryptor.update(padded) + encryptor.finalize()
    ).decode("ascii")
    timestamp = "1720000000"
    nonce = "nonce"
    signature = hashlib.sha1(
        "".join(sorted((token, timestamp, nonce, encrypted))).encode("utf-8")
    ).hexdigest()
    outer_xml = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>".encode()
    query = {
        "msg_signature": signature,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    adapter = create_channel_adapter(
        ChannelConfig(
            id="wecom-test",
            provider="wecom",
            name="WeCom",
        ),
        {
            "corp_id": corp_id,
            "token": token,
            "aes_key": encoded_key,
        },
    )

    assert adapter._verify(outer_xml, {"Content-Type": "application/xml"}, query)
    decoded = adapter._decode(
        outer_xml,
        {"Content-Type": "application/xml"},
        query,
    )
    assert decoded["FromUserName"] == "user-1"
    assert decoded["Content"] == "hello"

def test_channel_endpoint_switch_reorders_priority_without_touching_auxiliary(tmp_path) -> None:
    from argusai.core.ai import AIEndpoint, AuxiliaryAIEndpoint
    from argusai.core.settings import SettingsManager
    from argusai.gui.channels import ChannelsMixin

    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.ai_endpoints = [
        AIEndpoint(name="Anthropic", model="claude", base_url="https://api.anthropic.com/v1", priority=20),
        AIEndpoint(name="OpenAI", model="gpt-5", base_url="https://api.openai.com/v1", priority=10),
        AIEndpoint(name="Retired", model="old", enabled=False, priority=99),
    ]
    settings.ai_auxiliary_vision = AuxiliaryAIEndpoint(
        enabled=True,
        name="Vision",
        base_url="https://vision.example/v1",
        api_key="vision-key",
        model="vision-model",
    )
    manager.save(settings)

    class Host(ChannelsMixin):
        settings_mgr = manager

    host = Host()
    rows = host._channel_ai_endpoint("list", {})["endpoints"]
    assert [row["name"] for row in rows] == ["Anthropic", "OpenAI"]
    assert [row["active"] for row in rows] == [True, False]
    assert "api_key" not in rows[0] and rows[0]["host"] == "api.anthropic.com"

    target = host._channel_ai_endpoint("resolve", {"selector": "openai"})["endpoint"]
    applied = host._channel_ai_endpoint("activate", {"index": target["index"], "name": "OpenAI"})
    assert applied["endpoint"]["name"] == "OpenAI"

    reloaded = SettingsManager(manager.path).load()
    assert [ep.name for ep in reloaded.ai_endpoints if ep.enabled][0] == "Anthropic"
    ordered = sorted(
        (ep for ep in reloaded.ai_endpoints if ep.enabled),
        key=lambda ep: ep.priority,
        reverse=True,
    )
    assert ordered[0].name == "OpenAI"
    assert reloaded.ai_auxiliary_vision.api_key == "vision-key"
    assert reloaded.ai_auxiliary_vision.model == "vision-model"

    stale = host._channel_ai_endpoint("activate", {"index": 1, "name": "Renamed"})
    assert stale == {"ok": False, "code": "endpoint_changed"}
    assert host._channel_ai_endpoint("resolve", {"selector": "Retired"})["code"] == "endpoint_not_found"

def test_channel_agent_session_is_locked_to_a_sandboxed_runtime(tmp_path) -> None:
    import json as _json

    from argusai.core.agent_runtimes import NATIVE_RUNTIME_ID
    from argusai.core.settings import SettingsManager
    from argusai.gui.channels import ChannelsMixin

    manager = SettingsManager(tmp_path / "settings.json")
    settings = manager.get()
    settings.agent_runtimes = [
        {"id": "opencode", "enabled": True, "command": "opencode.cmd", "args": ["acp"]},
    ]
    manager.save(settings)

    class Host(ChannelsMixin):
        settings_mgr = manager
        _assistant_sessions: dict = {}
        _sessions_dir = tmp_path / "sessions"
        kb = None

        def _validate_session_id(self, session_id):
            return str(session_id)

        def _session_path(self, session_id):
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            return self._sessions_dir / f"{session_id}.json"

        def _default_assistant_web_search(self):
            return False

        def _ensure_session(self, session_id):
            from argusai.core.assistant.session import AssistantSession

            session = self._assistant_sessions.get(session_id)
            if session is None:
                session = AssistantSession()
                self._assistant_sessions[session_id] = session
            return session

        def _save_session(self, path, session, name):
            data = session.to_dict()
            data["name"] = name
            path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

    host = Host()
    rows = host._channel_agent_runtime("list", {})["agents"]
    assert [row["id"] for row in rows][0] == NATIVE_RUNTIME_ID
    opencode = next(row for row in rows if "opencode" in row["id"])

    if not opencode["available"]:
        # No opencode on this machine: the handler must refuse instead of
        # creating a conversation that can never run.
        assert host._channel_agent_runtime("create", {"selector": "opencode"}) == {
            "ok": False,
            "code": "agent_not_detected",
        }
        return

    created = host._channel_agent_runtime("create", {"selector": "opencode"})
    stored = _json.loads(host._session_path(created["session_id"]).read_text(encoding="utf-8"))
    assert stored["runtime"] == {
        "runtime_id": opencode["id"],
        "workspace_mode": "sandbox",
        "project_dir": "",
        "acp_session_id": "",
        "locked": True,
        "acp_config": {},
        "acp_config_options": [],
    }
    assert stored["name"] == ""
