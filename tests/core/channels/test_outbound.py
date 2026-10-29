from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from argusai.core.assistant.streaming import _CONFIRM_TOOLS, _WRITE_TOOLS
from argusai.core.assistant.tools import build_tool_registry
from argusai.core.channels.cards import card_text
from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelDeliveryError, ChannelEvent
from argusai.core.channels.runtime import ChannelRuntimeManager
from argusai.core.channels.store import ChannelStore
from argusai.core.tools.channel_tools import (
    channel_delivery_status,
    channel_list_targets,
    channel_send_file,
    channel_send_message,
)


def _gateway(store: ChannelStore) -> ChannelGateway:
    return ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )


def _paired(tmp_path):
    store = ChannelStore(tmp_path / "channels")
    channel = store.upsert_channel({
        "id": "webhook-test",
        "provider": "webhook",
        "name": "Test",
        "enabled": True,
    })
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    identity = store.set_identity_permissions(
        identity.id,
        ["chat", "status", "send"],
    )
    return store, channel, identity


def test_outbox_is_identity_bound_leased_and_retried(tmp_path) -> None:
    store, _channel, identity = _paired(tmp_path)
    job = store.enqueue_outbound(
        identity_id=identity.id,
        session_id="session-1",
        text="hello",
    )

    claimed = store.claim_outbound()
    assert [row["id"] for row in claimed] == [job["id"]]
    assert store.claim_outbound() == []

    retry = store.finish_outbound(job["id"], error="temporary")
    assert retry["state"] == "pending"
    retry["next_attempt_at"] = 0
    state = store._read_outbox()
    state["jobs"][0]["next_attempt_at"] = 0
    store._write_outbox(state)
    claimed = store.claim_outbound()
    assert claimed[0]["attempts"] == 2
    sent = store.finish_outbound(job["id"])
    assert sent["state"] == "sent"


def test_outbox_cross_session_delivery_requires_explicit_internal_flag(tmp_path) -> None:
    store, _channel, identity = _paired(tmp_path)
    store.bind_identity(identity.id, "session-channel")

    try:
        store.enqueue_outbound(
            identity_id=identity.id,
            session_id="session-desktop",
            text="hello",
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    job = store.enqueue_outbound(
        identity_id=identity.id,
        session_id="session-desktop",
        text="hello",
        allow_cross_session=True,
    )
    assert job["session_id"] == "session-desktop"
    assert job["recipient_session_id"] == "session-channel"
    assert store.get_identity(identity.id).bound_session_id == "session-channel"


def test_runtime_delivers_confirmed_cross_session_message_without_rebinding(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    store.bind_identity(identity.id, "session-channel")
    sent = []
    synced = []

    class Adapter:
        def send(self, reply):
            sent.append(reply)

    runtime = ChannelRuntimeManager(
        store,
        _gateway(store),
        outbound_delivered_handler=synced.append,
    )
    runtime._adapters[channel.id] = Adapter()
    try:
        runtime.enqueue_outbound(
            identity_id=identity.id,
            session_id="session-gui",
            text="hello from GUI",
            allow_cross_session=True,
        )
        assert runtime.drain_outbox_once() == 1
    finally:
        runtime.shutdown()

    assert [reply.text for reply in sent] == ["hello from GUI"]
    assert [job["text"] for job in synced] == ["hello from GUI"]
    assert store.list_outbound(session_id="session-gui")[0]["state"] == "sent"
    assert store.get_identity(identity.id).bound_session_id == "session-channel"


def test_outbox_replays_expired_lease_across_store_instances(tmp_path) -> None:
    store, _channel, identity = _paired(tmp_path)
    job = store.enqueue_outbound(
        identity_id=identity.id,
        session_id="session-1",
        text="replay",
    )
    assert store.claim_outbound()[0]["id"] == job["id"]

    restarted = ChannelStore(store.root)
    assert restarted.claim_outbound() == []
    with store._outbox_process_lock():
        state = store._read_outbox()
        state["jobs"][0]["lease_until"] = 0
        store._write_outbox(state)

    replayed = restarted.claim_outbound()
    assert replayed[0]["id"] == job["id"]
    assert replayed[0]["attempts"] == 2


def test_runtime_delivers_proactive_reply_without_inbound_event(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    sent = []

    class Adapter:
        def send(self, reply):
            sent.append(reply)

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    runtime = ChannelRuntimeManager(store, gateway)
    runtime._adapters[channel.id] = Adapter()
    try:
        runtime.enqueue_outbound(
            identity_id=identity.id,
            session_id="session-1",
            text="delivery",
            attachments=[{"type": "file", "name": "report.txt", "text": "ok"}],
        )
        assert runtime.drain_outbox_once() == 1
    finally:
        runtime.shutdown()
    # Text and file travel as separate provider calls so one can be retried
    # without re-posting the other.
    assert [reply.text for reply in sent] == ["delivery", ""]
    assert sent[0].external_chat_id == "chat-1"
    assert sent[0].attachments == ()
    assert sent[1].attachments[0]["name"] == "report.txt"


def test_failed_attachment_does_not_repost_the_text_that_already_arrived(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    sent = []

    class Adapter:
        def send(self, reply):
            if reply.attachments:
                raise ChannelDeliveryError("Feishu file upload failed: HTTP 400 timeout")
            sent.append(reply)

    runtime = ChannelRuntimeManager(store, _gateway(store))
    runtime._adapters[channel.id] = Adapter()
    try:
        job = runtime.enqueue_outbound(
            identity_id=identity.id,
            session_id="session-1",
            text="here is the screenshot",
            attachments=[{"type": "file", "name": "shot.txt", "text": "ok"}],
        )
        runtime.drain_outbox_once()
        state = store._read_outbox()
        state["jobs"][0]["next_attempt_at"] = 0
        store._write_outbox(state)
        runtime.drain_outbox_once()
    finally:
        runtime.shutdown()

    # The caption is delivered once; only the attachment part is retried, and
    # the failure notice reaches the user rather than disappearing into a log.
    assert [reply.text for reply in sent].count("here is the screenshot") == 1
    assert store.list_outbound(session_id="session-1")[0]["delivered_parts"] == 1
    assert job["id"]


def test_permission_failures_fail_at_once_instead_of_retrying(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    attempts = []

    class Adapter:
        def send(self, reply):
            attempts.append(reply)
            raise ChannelDeliveryError(
                "Feishu file upload failed: [99991672] Access denied. "
                "One of the following scopes is required: [im:resource]",
                permanent=True,
            )

    runtime = ChannelRuntimeManager(store, _gateway(store))
    runtime._adapters[channel.id] = Adapter()
    try:
        runtime.enqueue_outbound(
            identity_id=identity.id,
            session_id="session-1",
            text="deliverable",
        )
        runtime.drain_outbox_once()
    finally:
        runtime.shutdown()

    job = store.list_outbound(session_id="session-1")[0]
    assert job["state"] == "failed"
    assert job["attempts"] == 1
    assert "im:resource" in job["last_error"]
    # One send for the payload, one for the user-visible failure notice.
    assert len(attempts) == 2
    assert "投递失败" in attempts[1].text or "Delivery failed" in attempts[1].text


def test_delivery_status_reports_the_provider_reason(tmp_path) -> None:
    store, _channel, identity = _paired(tmp_path)
    job = store.enqueue_outbound(
        identity_id=identity.id,
        session_id="session-1",
        text="deliverable",
        attachments=[{"type": "image", "name": "shot.jpg", "text": "x"}],
    )
    store.claim_outbound()
    store.finish_outbound(job["id"], error="missing scope im:resource", permanent=True)

    api = SimpleNamespace(_channels_store=lambda: store)
    report = channel_delivery_status(api, _session_id="session-1")
    row = report["deliveries"][0]
    assert row["state"] == "failed"
    assert row["last_error"] == "missing scope im:resource"
    assert row["attachments"][0]["kind"] == "image"


def test_gateway_file_list_and_delivery_require_send_permission(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)

    def files(_sid: str, selector: str) -> dict:
        catalog = [
            {"relative_path": f"note-{index}.md", "name": f"note-{index}.md", "size": index}
            for index in range(1, 8)
        ]
        low = str(selector or "").strip().lower()
        if not low or low == "list" or low.startswith("search "):
            query = selector[7:].strip().casefold() if low.startswith("search ") else ""
            rows = [
                row for row in catalog
                if not query or query in row["relative_path"].casefold()
            ]
            return {"ok": True, "files": rows, "query": query}
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(catalog):
                name = catalog[index]["name"]
                return {
                    "ok": True,
                    "attachment": {
                        "type": "file",
                        "name": name,
                        "mime": "text/markdown",
                        "text": name,
                    },
                }
        name = Path(selector).name
        return {
            "ok": True,
            "attachment": {
                "type": "file",
                "name": name,
                "mime": "text/markdown",
                "text": name,
            },
        }

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
        session_file_handler=files,
    )

    def event(text: str = "", *, token: str = "", message_id: str = "m") -> ChannelEvent:
        return ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id=identity.external_user_id,
            external_chat_id=identity.external_chat_id,
            message_id=message_id,
            text=text,
            event_kind="action" if token else "message",
            action_token=token,
        )

    listing = gateway.handle(event("/file list"))
    body = card_text(listing.text, listing.blocks, locale=listing.locale)
    assert "page 1/2" in body
    assert "note-1.md" in body
    assert "note-5.md" in body
    assert "note-6.md" not in body
    assert any("Next" in str(action.get("label") or "") for action in listing.blocks[0]["actions"])

    page2_token = next(
        action["token"]
        for action in listing.blocks[0]["actions"]
        if "Next" in str(action.get("label") or "")
    )
    page2 = gateway.handle(event(token=page2_token, message_id="page2"))
    body2 = card_text(page2.text, page2.blocks, locale=page2.locale)
    assert "page 2/2" in body2
    assert "note-6.md" in body2

    searched = gateway.handle(event("/file search note-7"))
    search_body = card_text(searched.text, searched.blocks, locale=searched.locale)
    assert "note-7.md" in search_body
    assert "note-1.md" not in search_body

    download_token = next(
        action["token"]
        for action in searched.blocks[0]["actions"]
        if action.get("style") != "primary"
    )
    downloaded = gateway.handle(event(token=download_token, message_id="dl"))
    assert downloaded.attachments[0]["name"] == "note-7.md"

    assert gateway.handle(event("/file 1")).attachments[0]["name"] == "note-1.md"
    store.set_identity_permissions(identity.id, ["chat"])
    assert "not allowed" in gateway.handle(event("/file list")).text


def test_gateway_result_reply_stays_silent_when_empty(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "", "blocks": []},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id=identity.external_user_id,
        external_chat_id=identity.external_chat_id,
        message_id="empty",
        text="什么",
    ))
    assert reply.text == ""
    assert reply.blocks == ()
    gateway.queue_result_delivery(identity.id, {"ok": True, "text": "", "blocks": []})
    assert store.claim_outbound(limit=8) == []


def test_gateway_strips_hollow_done_filler(tmp_path) -> None:
    """After /live pushed the real content, a trailing "完成。" is noise."""
    store, channel, identity = _paired(tmp_path)
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "完成。",
            "blocks": [],
            "channel_live_delivered": True,
        },
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id=identity.external_user_id,
        external_chat_id=identity.external_chat_id,
        message_id="done-only",
        text="你好",
    ))
    assert reply.text == ""
    assert reply.blocks == ()


def test_gateway_keeps_short_answer_without_a_live_push(tmp_path) -> None:
    """Without /live the same words are the whole answer, not filler."""
    store, channel, identity = _paired(tmp_path)
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "完成。", "blocks": []},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id=identity.external_user_id,
        external_chat_id=identity.external_chat_id,
        message_id="done-only",
        text="你好",
    ))
    assert reply.text == "完成。"


def test_gateway_queues_live_segment_and_card(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True},
        session_handler=lambda _sid: {"ok": True},
        cancel_handler=lambda _sid: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    gateway.queue_live_update(identity.id, text="step one: scanning")
    gateway.queue_live_update(
        identity.id,
        raw_blocks=[{
            "type": "user_decision",
            "question": "Continue?",
            "options": ["Yes", "No"],
        }],
    )
    pending = store.claim_outbound(limit=8)
    assert len(pending) == 2
    assert pending[0]["text"] == "step one: scanning"
    assert pending[1]["blocks"][0]["type"] == "user_decision"
    assert pending[1]["blocks"][0]["title"] == "Continue?"


def test_agent_tools_list_and_queue_sandbox_file(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    sandbox = tmp_path / "data" / "assistant_sessions" / "session-1.sandbox"
    sandbox.mkdir(parents=True)
    (sandbox / "report.md").write_text("# report", encoding="utf-8")

    class Runtime:
        def statuses(self):
            return [{"channel_id": channel.id, "connected": True}]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
    )
    targets = channel_list_targets(api, _session_id="session-1")
    assert targets["targets"][0]["identity_id"] == identity.id
    assert channel_send_message(
        api,
        "hello",
        _session_id="session-1",
    )["queued"]
    result = channel_send_file(
        api,
        "report.md",
        _session_id="session-1",
    )
    assert result["queued"]
    assert result["file"]["sha256"]


def test_channel_send_explains_when_bound_to_other_session(tmp_path) -> None:
    store = ChannelStore(tmp_path / "channels")
    identities = []
    for index, provider in enumerate(("telegram", "feishu")):
        channel = store.upsert_channel({
            "id": f"{provider}-test",
            "provider": provider,
            "name": provider,
            "enabled": True,
        })
        pairing = store.create_pairing(channel.id, bound_session_id=f"session-{provider}")
        identity = store.consume_pairing(
            channel.id,
            pairing["code"],
            external_user_id=f"user-{index}",
            external_chat_id=f"chat-{index}",
        )
        identities.append(identity)
    with store._lock:
        payload = store._read()
        rows = []
        for raw in payload.get("identities") or []:
            raw = dict(raw)
            raw["last_seen_at"] = time.time() - 72 * 3600
            rows.append(raw)
        payload["identities"] = rows
        store._write(payload)
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: SimpleNamespace(statuses=lambda: []),
    )
    listed = channel_list_targets(api, _session_id="session-gui")
    assert listed["count"] == 0
    assert listed["other_session_targets"]
    assert listed["auto_targets"] == []
    try:
        channel_send_message(api, "hi", _session_id="session-gui")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "identity_id" in str(exc)


def test_channel_send_uses_only_recipient_without_hijacking_inbound(tmp_path) -> None:
    """The sole recipient can receive a confirmed send without being rebound.

    This lets a fresh desktop conversation proactively contact the user's one
    paired chat while that chat's incoming messages keep their own history.
    """
    store, channel, identity = _paired(tmp_path)
    store.bind_identity(identity.id, "session-tg")
    store.touch_identity(identity.id)

    class Runtime:
        def statuses(self):
            return [{"channel_id": channel.id, "connected": True}]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
    )
    automatic = channel_send_message(api, "hi", _session_id="session-gui")
    assert automatic["queued"] is True
    assert automatic["route_mode"] == "auto_only_recipient"
    assert store.get_identity(identity.id).bound_session_id == "session-tg"

    result = channel_send_message(
        api,
        "hi",
        identity.id,
        _session_id="session-gui",
    )
    assert result["queued"] is True
    assert result["route_mode"] == "explicit_cross_session"
    assert store.get_identity(identity.id).bound_session_id == "session-tg"
    job = store.list_outbound(session_id="session-gui", limit=1)[0]
    assert job["recipient_session_id"] == "session-tg"


def test_channel_send_uses_clearly_most_recent_recipient(tmp_path) -> None:
    store, channel, recent = _paired(tmp_path)
    store.bind_identity(recent.id, "session-recent")
    older_channel = store.upsert_channel({
        "id": "feishu-older",
        "provider": "feishu",
        "name": "older",
        "enabled": True,
    })
    pairing = store.create_pairing(
        older_channel.id,
        bound_session_id="session-older",
    )
    older = store.consume_pairing(
        older_channel.id,
        pairing["code"],
        external_user_id="older-user",
        external_chat_id="older-chat",
    )
    with store._lock:
        payload = store._read()
        for raw in payload.get("identities") or []:
            if raw.get("id") == recent.id:
                raw["last_seen_at"] = time.time()
            elif raw.get("id") == older.id:
                raw["last_seen_at"] = time.time() - 2 * 3600
        store._write(payload)

    class Runtime:
        def statuses(self):
            return [{"channel_id": channel.id, "connected": True}]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
    )
    result = channel_send_message(api, "hi", _session_id="session-desktop")
    assert result["route_mode"] == "auto_recent_recipient"
    assert result["identity_id"] == recent.id
    assert store.get_identity(recent.id).bound_session_id == "session-recent"
    assert store.get_identity(older.id).bound_session_id == "session-older"


def test_channel_send_adopts_an_unbound_identity(tmp_path) -> None:
    """An identity nobody owns follows the conversation that used it."""
    store, channel, identity = _paired(tmp_path)
    store.bind_identity(identity.id, "")
    store.touch_identity(identity.id)

    class Runtime:
        def statuses(self):
            return [{"channel_id": channel.id, "connected": True}]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
    )
    assert channel_send_message(api, "hi", _session_id="session-gui")["queued"]
    assert store.get_identity(identity.id).bound_session_id == "session-gui"


def test_channel_send_prefers_turn_source_over_fanout(tmp_path) -> None:
    """A Telegram-authored turn must send back to Telegram, not every bound app."""
    store = ChannelStore(tmp_path / "channels")
    tg = store.upsert_channel({"id": "tg", "provider": "telegram", "name": "TG", "enabled": True})
    fs = store.upsert_channel({"id": "fs", "provider": "feishu", "name": "FS", "enabled": True})
    identities = []
    for channel, user in ((tg, "u-tg"), (fs, "u-fs")):
        pairing = store.create_pairing(channel.id, bound_session_id="session-1")
        identities.append(store.consume_pairing(
            channel.id,
            pairing["code"],
            external_user_id=user,
            external_chat_id=f"chat-{user}",
        ))
    tg_identity, fs_identity = identities

    class Runtime:
        def statuses(self):
            return [
                {"channel_id": tg.id, "connected": True},
                {"channel_id": fs.id, "connected": True},
            ]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    session = SimpleNamespace(
        _current_turn_source="channel",
        _current_turn_source_ref=tg_identity.id,
    )
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
        _assistant_sessions={"session-1": session},
    )
    result = channel_send_message(api, "only telegram", _session_id="session-1")
    assert result["route_mode"] == "turn_source"
    assert result["delivery_count"] == 1
    assert result["identity_id"] == tg_identity.id
    assert result["provider"] == "telegram"
    assert fs_identity.id not in {item["identity_id"] for item in result["deliveries"]}


def test_channel_send_fans_out_to_all_session_targets(tmp_path) -> None:
    store = ChannelStore(tmp_path / "channels")
    tg = store.upsert_channel({"id": "tg", "provider": "telegram", "name": "TG", "enabled": True})
    fs = store.upsert_channel({"id": "fs", "provider": "feishu", "name": "FS", "enabled": True})
    identities = []
    for channel, user in ((tg, "u-tg"), (fs, "u-fs")):
        pairing = store.create_pairing(channel.id, bound_session_id="session-1")
        identities.append(store.consume_pairing(
            channel.id,
            pairing["code"],
            external_user_id=user,
            external_chat_id=f"chat-{user}",
        ))
    store.set_identity_permissions(identities[0].id, ["chat", "status", "send"])
    store.set_identity_permissions(identities[1].id, ["chat", "status", "send"])

    class Runtime:
        def statuses(self):
            return [
                {"channel_id": tg.id, "connected": True},
                {"channel_id": fs.id, "connected": True},
            ]

        def enqueue_outbound(self, **kwargs):
            return store.enqueue_outbound(**kwargs)

    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(tmp_path / "data")),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: Runtime(),
    )
    result = channel_send_message(api, "ping both", _session_id="session-1")
    assert result["route_mode"] == "session_fanout"
    assert result["delivery_count"] == 2
    providers = {item["provider"] for item in result["deliveries"]}
    assert providers == {"telegram", "feishu"}


def test_agent_file_tool_rejects_sandbox_escape(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    data_root = tmp_path / "data"
    outside = tmp_path / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(data_root=str(data_root)),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: SimpleNamespace(
            statuses=lambda: [{"channel_id": channel.id, "connected": True}],
        ),
    )
    result = channel_send_file(
        api,
        str(outside),
        _session_id="session-1",
    )
    assert result["ok"] is False
    assert "sandbox" in result["error"]


def test_channel_tools_have_stable_schemas_and_confirmation_policy(tmp_path) -> None:
    store, _channel, _identity = _paired(tmp_path)
    api = SimpleNamespace(
        settings_mgr=SimpleNamespace(
            get=lambda: SimpleNamespace(
                data_root=str(tmp_path / "data"),
                assistant_enable_web_search=False,
            ),
        ),
        _channels_store=lambda: store,
        _channels_runtime=lambda: SimpleNamespace(statuses=lambda: []),
    )
    registry = build_tool_registry(api, session_id="session-1", allow_web_search=False)
    target_tool = registry.get("channel_list_targets")
    message_tool = registry.get("channel_send_message")
    file_tool = registry.get("channel_send_file")

    assert target_tool is not None
    assert target_tool.risk == "low"
    assert message_tool is not None
    assert message_tool.required == ["text"]
    assert file_tool is not None
    assert file_tool.required == ["path"]
    assert {"channel_send_message", "channel_send_file"} <= _WRITE_TOOLS
    assert {"channel_send_message", "channel_send_file"} <= _CONFIRM_TOOLS
