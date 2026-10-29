from __future__ import annotations

import json

import httpx
import pytest

from argusai.core.assistant.confirmation_broker import ConfirmationBroker
from argusai.core.channels.adapters import create_channel_adapter
from argusai.core.channels.cards import (
    card_text,
    feishu_card_elements,
    project_channel_cards,
    render_channel_reply,
)
from argusai.core.channels.gateway import ChannelGateway
from argusai.core.channels.models import ChannelConfig, ChannelEvent, ChannelReply
from argusai.core.channels.runtime import ChannelRuntimeManager
from argusai.core.channels.store import ChannelStore


def _body(reply: ChannelReply) -> str:
    """Reply text as a card-capable provider shows it: body without button rows."""
    return card_text(
        reply.text,
        reply.blocks,
        include_actions=False,
        locale=reply.locale,
    )


def _paired(tmp_path, *, permissions=("chat", "status", "confirm", "configure")):
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({"id": "webhook-test", "provider": "webhook", "name": "Test"})
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    identity = store.set_identity_permissions(identity.id, list(permissions))
    return store, channel, identity


def _grant_sessions(store, identity, session_ids):
    for session_id in session_ids:
        identity = store.grant_identity_session(identity.id, session_id)
    return identity


def _event(channel_id: str, *, token: str = "", message_id: str = "message-1") -> ChannelEvent:
    return ChannelEvent(
        channel_id=channel_id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id=message_id,
        text="",
        event_kind="action" if token else "message",
        action_token=token,
    )


def _find_action(card: dict, label: str) -> str:
    for action in card.get("actions") or []:
        if action.get("label") == label:
            return str(action["token"])
    for question in card.get("questions") or []:
        for action in question.get("actions") or []:
            if action.get("label") == label:
                return str(action["token"])
    raise AssertionError(f"missing action: {label}")


def test_card_projection_allows_sensitive_tools_with_explicit_confirmation() -> None:
    issued = []
    cards = project_channel_cards(
        [{
            "type": "confirm",
            "title": "Run host code?",
            "name": "run_host_python_script",
            "confirm_id": "call-1",
            "pending_id": "pending-1",
            "pending_version": 2,
        }],
        issue_action=lambda payload: issued.append(payload) or f"token-{len(issued)}",
    )

    assert cards[0]["requires_local_confirmation"] is False
    assert [item["label"] for item in cards[0]["actions"]] == ["Allow once", "Deny"]
    assert [item["verdict"] for item in issued] == ["allow", "deny"]


@pytest.mark.parametrize(
    ("provider", "expected_key"),
    [
        ("telegram", "reply_markup"),
        ("discord", "components"),
        ("slack", "blocks"),
        ("feishu", "content"),
        ("dingtalk", "actionCard"),
        ("wecom", "template_card"),
        ("webhook", "actions"),
    ],
)
def test_seven_provider_payloads_keep_text_and_actions(provider: str, expected_key: str) -> None:
    reply = ChannelReply(
        text="Approve this operation",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "Choose",
            "body": "One answer is required.",
            "actions": [{"label": "Continue", "token": "ca_token1234567890", "style": "primary"}],
        },),
    )
    payload = render_channel_reply(provider, reply)
    assert expected_key in payload
    assert "Approve this operation" in json.dumps(payload, ensure_ascii=False)
    if provider == "dingtalk":
        assert "/act ca_token1234567890" in payload["actionCard"]["text"]


def test_button_labels_are_not_repeated_in_the_body() -> None:
    reply = ChannelReply(
        text="Approve?",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "Choose",
            "actions": [
                {"label": "Continue", "token": "ca_token1234567890"},
                {"label": "Stop", "token": "ca_token0987654321"},
            ],
        },),
    )
    telegram = render_channel_reply("telegram", reply)
    assert "Continue" not in telegram["text"]
    assert [
        button["text"]
        for row in telegram["reply_markup"]["inline_keyboard"]
        for button in row
    ] == ["Continue", "Stop"]
    # DingTalk draws no buttons, so the body must keep the actionable tokens.
    assert "Continue" in render_channel_reply("dingtalk", reply)["actionCard"]["text"]


def test_options_beyond_the_button_budget_stay_visible_in_the_body() -> None:
    reply = ChannelReply(
        text="Approve?",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "Choose",
            "actions": [
                {"label": f"Option {index}", "token": f"ca_token{index:013d}"}
                for index in range(8)
            ],
        },),
    )
    assert "Option 7" in render_channel_reply("wecom", reply)["template_card"]["sub_title_text"]
    assert "Option 7" not in render_channel_reply("telegram", reply)["text"]


def test_telegram_uses_full_width_rows_for_long_decision_labels() -> None:
    payload = render_channel_reply(
        "telegram",
        ChannelReply(
            text="Choose",
            external_chat_id="chat-1",
            blocks=({
                "type": "user_decision",
                "title": "请选择",
                "actions": [
                    {
                        "label": "确认：继续抖音 a_bogus 方向",
                        "token": "ca_token1234567890",
                    },
                    {
                        "label": "确认：查看知识库条目",
                        "token": "ca_token0987654321",
                    },
                ],
            },),
        ),
    )
    rows = payload["reply_markup"]["inline_keyboard"]
    assert len(rows) == 2
    assert all(len(row) == 1 for row in rows)


def test_action_token_is_single_use_identity_bound_and_contains_no_payload(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    token = store.issue_action(
        channel_id=channel.id,
        identity_id=identity.id,
        session_id="session-1",
        payload={"kind": "user_decision", "response": "secret answer"},
    )
    persisted = (tmp_path / "channel_actions.json").read_text(encoding="utf-8")
    assert token not in persisted
    assert "secret answer" in persisted
    with pytest.raises(ValueError, match="another identity"):
        store.consume_action(token, channel_id=channel.id, identity_id="identity-other")
    assert store.consume_action(
        token,
        channel_id=channel.id,
        identity_id=identity.id,
    )["response"] == "secret answer"
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_action(token, channel_id=channel.id, identity_id=identity.id)


def test_action_token_expires(monkeypatch, tmp_path) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr("argusai.core.channels.store.time.time", lambda: clock["now"])
    store = ChannelStore(tmp_path)
    token = store.issue_action(
        channel_id="channel",
        identity_id="identity",
        session_id="session",
        payload={"kind": "noop"},
        ttl_seconds=30,
    )
    clock["now"] = 131.0
    with pytest.raises(ValueError, match="invalid or expired"):
        store.consume_action(token, channel_id="channel", identity_id="identity")


def test_gateway_resumes_single_and_batch_user_decisions(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    turns = []

    def turn(params):
        turns.append(params)
        if params["message"] == "ask":
            return {
                "ok": True,
                "text": "[Waiting for your decision]",
                "blocks": [{
                    "type": "user_decision",
                    "question": "Proceed?",
                    "options": ["Yes", "No"],
                }],
            }
        if params["message"] == "batch":
            return {
                "ok": True,
                "text": "",
                "blocks": [{
                    "type": "user_decision_batch",
                    "title": "Choose both",
                    "questions": [
                        {"id": "q1", "question": "First?", "options": ["A", "B"]},
                        {"id": "q2", "question": "Second?", "options": ["C", "D"]},
                    ],
                }],
            }
        return {"ok": True, "text": "resumed"}

    gateway = ChannelGateway(
        store,
        turn_handler=turn,
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    ask_event = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="ask",
        text="ask",
    )
    single = gateway.handle(ask_event)
    token = _find_action(single.blocks[0], "Yes")
    resumed = gateway.handle(_event(channel.id, token=token, message_id="answer"))
    assert resumed.text == "resumed"
    assert turns[-1]["message"] == "Yes"
    assert turns[-1]["hide_user_message"] is True

    batch_event = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="batch",
        text="batch",
    )
    batch = gateway.handle(batch_event)
    incomplete = gateway.handle(_event(
        channel.id,
        token=_find_action(batch.blocks[0], "Submit"),
        message_id="submit-early",
    ))
    assert "incomplete" in incomplete.text
    card = incomplete.blocks[0]
    after_a = gateway.handle(_event(
        channel.id,
        token=_find_action(card, "A"),
        message_id="select-a",
    ))
    after_c = gateway.handle(_event(
        channel.id,
        token=_find_action(after_a.blocks[0], "C"),
        message_id="select-c",
    ))
    submitted = gateway.handle(_event(
        channel.id,
        token=_find_action(after_c.blocks[0], "Submit"),
        message_id="submit",
    ))
    assert submitted.text == "resumed"
    payload = json.loads(turns[-1]["message"])
    assert payload["answers"] == {"q1": "A", "q2": "C"}


def test_new_session_offers_ready_mode_button(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    settings: list[tuple[str, str, str]] = []

    def configure(session_id: str, option: str, action: str) -> dict:
        settings.append((session_id, option, action))
        return {"ok": True, "enabled": True, "message": f"{option}:{action}"}

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "ready-session",
        confirmation_handler=lambda *_args: {"ok": True},
        session_option_handler=configure,
    )
    created = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="new",
        text="/new",
    ))
    assert "ready-session" in created.text
    assert created.blocks[0]["title"] == "Quick enable"
    assert [item["label"] for item in created.blocks[0]["actions"]] == [
        "Enable all",
        "Not now",
    ]
    enabled = gateway.handle(_event(
        channel.id,
        token=_find_action(created.blocks[0], "Enable all"),
        message_id="enable",
    ))
    assert "Ready mode on" in enabled.text
    # Ready mode includes Greenlight but keeps high-risk auto-allow behind
    # the separate /allow all command.
    assert settings == [
        ("ready-session", "tools", "allow_standard"),
        ("ready-session", "live", "on"),
        ("ready-session", "search", "on"),
        ("ready-session", "greenlight", "on"),
    ]


def test_gateway_control_commands_are_allowlisted(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    settings = []
    confirmations = []

    def configure(*args):
        settings.append(args)
        if args[1:] == ("tools", "allow_all"):
            return {
                "ok": True,
                "message": "All tools are allowed for this conversation. Send /allow off to stop.",
            }
        if args[1:] == ("tools", "disable_all"):
            return {"ok": True, "message": "Session-wide tool authorization is off."}
        if args[1:] == ("search", "on"):
            return {"ok": True, "message": "Web search is on.", "enabled": True}
        if args[1] == "think":
            effort = args[2] if len(args) > 2 else "status"
            if effort == "off":
                return {"ok": True, "enabled": False}
            if effort == "status":
                return {"ok": True, "enabled": False, "thinking_effort": "high"}
            return {"ok": True, "enabled": True, "thinking_effort": effort if effort != "on" else "high"}
        if args[1:] == ("greenlight", "on"):
            return {"ok": True, "enabled": True}
        if args[1:] == ("greenlight", "off"):
            return {"ok": True, "enabled": False}
        if args[1:] == ("live", "on"):
            return {"ok": True, "enabled": True}
        if args[1:] == ("live", "off"):
            return {"ok": True, "enabled": False}
        if args[1:] == ("live", "status"):
            return {"ok": True, "enabled": False}
        return {"ok": True, "message": "Web search is on."}

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {
            "ok": True,
            "tools": [{"name": "one"}, {"name": "two"}],
            "pending_tools": [
                {
                    "id": "call-1",
                    "name": "sandbox_read_file",
                    "pending_id": "pending-1",
                    "version": 2,
                },
                {
                    "id": "call-2",
                    "name": "run_host_python_script",
                    "pending_id": "pending-1",
                    "version": 2,
                },
            ],
            "engagement_declaration": None,
        },
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *args: confirmations.append(args) or {"ok": True},
        session_option_handler=configure,
    )

    def command_reply(text: str, message_id: str) -> ChannelReply:
        return gateway.handle(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=message_id,
            text=text,
        ))

    def command(text: str, message_id: str) -> str:
        return command_reply(text, message_id).text

    assert command("/search on", "search") == "Web search is on."
    assert settings == [("session-1", "search", "on")]
    assert command("/think", "think-status") == "Thinking mode is off."
    assert settings[-1] == ("session-1", "think", "status")
    assert command("/think on", "think-on") == (
        "Thinking mode is on (high). Follow-up turns will request model reasoning."
    )
    assert settings[-1] == ("session-1", "think", "on")
    assert command("/thinking off", "think-off") == "Thinking mode is off."
    assert settings[-1] == ("session-1", "think", "off")
    assert command("/think  on", "think-double-space") == (
        "Thinking mode is on (high). Follow-up turns will request model reasoning."
    )
    assert settings[-1] == ("session-1", "think", "on")
    assert command("/think [on]", "think-help-copy") == (
        "Thinking mode is on (high). Follow-up turns will request model reasoning."
    )
    assert command("/think 开", "think-zh") == (
        "Thinking mode is on (high). Follow-up turns will request model reasoning."
    )
    assert command("/think high", "think-high") == (
        "Thinking mode is on (high). Follow-up turns will request model reasoning."
    )
    assert settings[-1] == ("session-1", "think", "high")
    assert command("/think max", "think-max") == (
        "Thinking mode is on (max). Follow-up turns will request model reasoning."
    )
    assert settings[-1] == ("session-1", "think", "max")
    assert "Usage: /think" in command("/think maybe", "think-usage")
    assert "2 tools" in command("/tools", "tools")
    assert "sandbox_read_file" in command("/tools", "tools-2")
    prompt = command_reply("/allow all", "allow-all")
    assert "Confirm session-wide" in prompt.text
    token = _find_action(prompt.blocks[0], "Allow for this session")
    enabled = gateway.handle(_event(channel.id, token=token, message_id="allow-session"))
    assert "All tools are allowed" in enabled.text
    assert settings[-1] == ("session-1", "tools", "allow_all")
    assert confirmations[0][1] == {
        "type": "batch_confirmation_decision",
        "pending_id": "pending-1",
        "version": 2,
        "decisions": {"call-1": "allow", "call-2": "allow"},
    }
    assert command("/allow off", "allow-off") == "Session-wide tool authorization is off."
    assert settings[-1] == ("session-1", "tools", "disable_all")
    assert command("/greenlight status", "greenlight") == "Greenlight is off for this conversation."
    assert command("/greenlight on", "greenlight-on") == "Greenlight is on for this conversation."
    assert settings[-1] == ("session-1", "greenlight", "on")
    assert command("/greenlight off", "greenlight-off") == "Greenlight is off for this conversation."
    assert settings[-1] == ("session-1", "greenlight", "off")
    assert command("/live on", "live-on") == (
        "Live updates are on. Mid-turn replies and cards will be pushed to this chat."
    )
    assert settings[-1] == ("session-1", "live", "on")
    assert command("/live off", "live-off") == (
        "Live updates are off. Only the final reply is sent."
    )
    assert settings[-1] == ("session-1", "live", "off")


def test_gateway_starts_conversations_on_a_local_agent(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    agents = [
        {"index": 0, "ordinal": 1, "id": "argusai-native", "name": "ArgusAI Agent",
         "kind": "native", "available": True},
        {"index": 1, "ordinal": 2, "id": "opencode", "name": "OpenCode",
         "kind": "acp", "available": True},
        {"index": 2, "ordinal": 3, "id": "kimi", "name": "Kimi Code CLI",
         "kind": "acp", "available": False},
    ]
    created: list[dict] = []

    def agent_handler(action: str, target: dict) -> dict:
        if action == "list":
            return {"ok": True, "agents": agents}
        wanted = str(target.get("id") or "")
        selector = str(target.get("selector") or "")
        match = next(
            (
                row for row in agents
                if (wanted and row["id"] == wanted)
                or (selector and (str(row["ordinal"]) == selector
                                  or row["id"] == selector.casefold()
                                  or row["name"].casefold() == selector.casefold()))
            ),
            None,
        )
        if match is None:
            return {"ok": False, "code": "agent_not_found"}
        if not match["available"]:
            return {"ok": False, "code": "agent_not_detected"}
        created.append(match)
        return {"ok": True, "session_id": f"session-{len(created)}", "agent": match}

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "native-session",
        confirmation_handler=lambda *_args: {"ok": True},
        agent_runtime_handler=agent_handler,
    )

    def command(text: str, message_id: str) -> ChannelReply:
        return gateway.handle(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=message_id,
            text=text,
        ))

    listing = command("/agent", "list")
    assert "2. OpenCode · acp" in _body(listing)
    assert "3. Kimi Code CLI · acp (not detected)" in _body(listing)
    assert [item["label"] for item in listing.blocks[0]["actions"]] == [
        "1",
        "2",
    ]

    started = gateway.handle(_event(
        channel.id,
        token=_find_action(listing.blocks[0], "2"),
        message_id="tap",
    ))
    assert "created on OpenCode" in started.text
    assert store.get_identity(identity.id).bound_session_id == "session-1"

    typed = command("/new opencode", "typed")
    assert "created on OpenCode" in typed.text
    assert [row["id"] for row in created] == ["opencode", "opencode"]
    assert store.get_identity(identity.id).bound_session_id == "session-2"

    assert "not detected" in command("/new kimi", "missing-binary").text
    assert "No such agent" in command("/new nope", "missing").text

    # Plain /new keeps the built-in agent and does not consult the handler.
    assert "native-session" in command("/new", "plain").text
    assert len(created) == 2

    # Choosing an execution environment is configuration, not chatting.
    store.set_identity_permissions(identity.id, ["chat", "status"])
    assert "may not read or start" in command("/agent", "denied").text
    assert "may not read or start" in command("/new opencode", "denied-new").text


def test_gateway_lists_and_switches_ai_endpoints(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    endpoints = [
        {"index": 0, "name": "Anthropic", "model": "claude", "host": "api.anthropic.com"},
        {"index": 1, "name": "OpenAI", "model": "gpt-5", "host": "api.openai.com"},
    ]
    activated: list[dict] = []

    def endpoint_handler(action: str, target: dict) -> dict:
        rows = [
            {**row, "ordinal": ordinal, "active": ordinal == 1}
            for ordinal, row in enumerate(endpoints, 1)
        ]
        if action == "list":
            return {"ok": True, "endpoints": rows}
        selector = str(target.get("selector") or "")
        if selector:
            match = next((row for row in rows if str(row["ordinal"]) == selector), None)
        else:
            match = next((row for row in rows if row["index"] == target.get("index")), None)
        if match is None:
            return {"ok": False, "code": "endpoint_not_found"}
        if action == "activate":
            activated.append(match)
            endpoints.insert(0, endpoints.pop(rows.index(match)))
            return {"ok": True, "endpoint": {**match, "ordinal": 1, "active": True}}
        return {"ok": True, "endpoint": match}

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
        endpoint_handler=endpoint_handler,
    )

    def command(text: str, message_id: str) -> ChannelReply:
        return gateway.handle(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=message_id,
            text=text,
        ))

    listing = command("/model", "list")
    assert "1. Anthropic · claude (in use)" in _body(listing)
    assert "2. OpenAI · gpt-5" in _body(listing)
    assert "api.anthropic.com" not in _body(listing)
    assert [item["label"] for item in listing.blocks[0]["actions"]] == ["2"]

    # Selecting from the list only asks; nothing is applied yet.
    prompt = gateway.handle(_event(
        channel.id,
        token=_find_action(listing.blocks[0], "2"),
        message_id="pick",
    ))
    assert "api.openai.com" in prompt.blocks[0]["body"]
    assert activated == []

    applied = gateway.handle(_event(
        channel.id,
        token=_find_action(prompt.blocks[0], "Switch"),
        message_id="apply",
    ))
    assert applied.text == "OpenAI (gpt-5) now answers first."
    assert [row["name"] for row in activated] == ["OpenAI"]

    typed = command("/model 1", "typed")
    assert "already answers first" in typed.text
    assert command("/model nope", "missing").text.startswith("No enabled endpoint")

    # Switching is configuration, so a chat-only identity must be refused.
    store.set_identity_permissions(identity.id, ["chat", "status"])
    assert "may not read or change" in command("/model", "denied").text


def test_gateway_lists_and_switches_conversations(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    identity = _grant_sessions(store, identity, [
        "session-2",
        "session-3",
        "session-4",
        "session-5",
        "session-6",
        "session-7",
    ])
    sessions = [
        {"id": "session-1", "name": "Current audit", "message_count": 8},
        {"id": "session-2", "name": "New target", "message_count": 3},
        {
            "id": "session-3",
            "name": "######## UserQuery: step by step\nwith a very long markdown title that must not break lines",
            "message_count": 1,
        },
        {"id": "session-4", "name": "Fourth", "message_count": 0},
        {"id": "session-5", "name": "Fifth", "message_count": 0},
        {"id": "session-6", "name": "Sixth", "message_count": 0},
        {"id": "session-7", "name": "Needle target", "message_count": 0},
    ]
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-new",
        confirmation_handler=lambda *_args: {"ok": True},
        session_list_handler=lambda: sessions,
    )

    def message(text: str, message_id: str) -> ChannelReply:
        return gateway.handle(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id=message_id,
            text=text,
        ))

    listed = message("/sessions", "sessions")
    assert "Current audit" in _body(listed)
    assert "8 messages" in _body(listed)
    assert "(current)" in _body(listed)
    assert [item["label"] for item in listed.blocks[0]["actions"] if item["label"].isdigit()] == [
        "2", "3", "4", "5",
    ]
    switch_token = _find_action(listed.blocks[0], "2")
    switched = gateway.handle(_event(
        channel.id,
        token=switch_token,
        message_id="switch-action",
    ))
    assert "Switched to New target" in switched.text
    assert store.find_identity(
        channel.id,
        identity.external_user_id,
        identity.external_chat_id,
    ).bound_session_id == "session-2"

    assert "Switched to Current audit" in message("/use 1", "switch-command").text
    assert store.find_identity(
        channel.id,
        identity.external_user_id,
        identity.external_chat_id,
    ).bound_session_id == "session-1"
    assert "not found" in message("/use missing", "switch-missing").text

    next_token = _find_action(listed.blocks[0], "Next ›")
    second_page = gateway.handle(_event(
        channel.id,
        token=next_token,
        message_id="sessions-next",
    ))
    assert "page 2/2" in _body(second_page)
    assert "Sixth" in _body(second_page)
    assert "very long markdown title" not in _body(listed)
    assert "\nwith a very long" not in _body(listed)

    searched = message("/sessions search needle", "sessions-search")
    assert 'search "needle"' in _body(searched)
    assert "Needle target" in _body(searched)
    assert "Sixth" not in _body(searched)


def test_gateway_sessions_count_accepts_gui_msg_count(tmp_path) -> None:
    """list_assistant_sessions exposes msg_count; /sessions must not show 0."""
    store, channel, identity = _paired(tmp_path)
    store.grant_identity_session(identity.id, "session-2")
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-new",
        confirmation_handler=lambda *_args: {"ok": True},
        session_list_handler=lambda: [
            {"id": "session-1", "name": "从前有座山", "msg_count": 12},
            {"id": "session-2", "name": "你好", "msg_count": 4},
        ],
    )
    listed = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="sessions-msg-count",
        text="/sessions",
    ))
    body = _body(listed)
    assert "12 messages" in body
    assert "4 messages" in body
    assert "0 messages" not in body


def test_sessions_telegram_buttons_stay_compact() -> None:
    reply = ChannelReply(
        text="",
        external_chat_id="chat-1",
        blocks=({
            "type": "info",
            "title": "Conversations · page 1/2",
            "sections": [{"title": "", "rows": [
                "1. Current · 8 messages (current)",
                "2. A very long conversation title that used to become a huge button",
            ]}],
            "actions": [
                {"label": "2", "token": "ca_token1234567890"},
                {"label": "Next ›", "token": "ca_token0987654321", "style": "primary"},
            ],
        },),
    )
    payload = render_channel_reply("telegram", reply)
    labels = [
        button["text"]
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert labels == ["2", "Next ›"]
    assert "ca_token" not in payload["text"]
    assert "very long conversation title" in payload["text"]


def test_telegram_callback_reply_keeps_keyboard_when_reply_to_is_invalid(monkeypatch) -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "test-token"},
    )
    sent = []

    class Response:
        def __init__(self, success: bool) -> None:
            self.is_success = success
            self.status_code = 200 if success else 400
            self.text = "ok" if success else "bad reply_to"

        def json(self):
            if self.is_success:
                return {"ok": True}
            return {"description": "Bad Request: message to be replied not found"}

    def post(_url, **kwargs):
        sent.append(dict(kwargs["json"]))
        # First attempt includes the bogus callback id as reply_to and fails;
        # the adapter must retry without reply_to and keep the keyboard.
        if "reply_to_message_id" in kwargs["json"]:
            return Response(False)
        return Response(True)

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    adapter.send(ChannelReply(
        text="page 2",
        external_chat_id="chat-1",
        reply_to_message_id="9876543210987654321",
        blocks=({
            "type": "info",
            "title": "Conversations",
            "actions": [{"label": "6", "token": "ca_token1234567890"}],
        },),
    ))
    assert len(sent) == 2
    assert "reply_to_message_id" in sent[0]
    assert "reply_markup" in sent[0]
    assert "reply_to_message_id" not in sent[1]
    assert "reply_markup" in sent[1]
    assert "/act" not in sent[1]["text"]


def test_telegram_callback_event_message_id_is_not_a_reply_target() -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "test-token"},
    )
    events = []
    adapter._emit = events.append  # type: ignore[method-assign]
    # Simulate the polling loop body for one callback_query update.
    callback = {
        "id": "5123456789012345678",
        "data": "act:ca_token1234567890",
        "from": {"id": 42, "first_name": "Ada"},
        "message": {"chat": {"id": 99}, "message_id": 7, "message_thread_id": ""},
    }
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") or {}
    sender = callback.get("from") or {}
    chat = message.get("chat") or {}
    adapter._emit(ChannelEvent(
        channel_id=adapter.config.id,
        provider="telegram",
        external_user_id=str(sender.get("id") or ""),
        external_chat_id=str(chat.get("id") or ""),
        message_id=f"cb:{callback_id}" if callback_id else "x",
        text="",
        event_kind="action",
        action_token=data.removeprefix("act:"),
        raw=callback,
    ))
    event = events[0]
    assert event.message_id.startswith("cb:")
    assert not event.message_id.isdigit()
    reply = ChannelReply(
        text="ok",
        external_chat_id=event.external_chat_id,
        reply_to_message_id=event.message_id,
    )
    # Gateway copies message_id into reply_to; send must not treat cb:* as int.
    assert not reply.reply_to_message_id.isdigit()


def test_gateway_status_returns_conversation_overview_card(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    files = [{"name": "a.txt", "relative_path": "a.txt", "size": 3}]

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "unused"},
        session_handler=lambda _session_id: {
            "ok": True,
            "session_id": "session-1",
            "name": "Current audit",
            "note": "scope: example.test",
            "message_count": 65,
            "memory_count": 4,
            "todos": [{"content": "one"}, {"content": "two"}],
            "pending_tools": [{"name": "sandbox_read_file"}],
            "auto_allow_tools": ["http_request"],
            "allow_web_search": True,
            "engagement_declaration": {"active": True, "scope": "example.test"},
            "runtime": {"runtime_id": "argusai-native"},
            "active_stream": {"run_id": "run-1"},
        },
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
        session_file_handler=lambda _session_id, _selector: {"ok": True, "files": files},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="status",
        text="/status",
    ))
    body = _body(reply)
    assert reply.blocks[0]["type"] == "info"
    assert "Conversation overview" in body or "会话概览" in body
    assert "Current audit" in body
    assert "65" in body
    assert "sandbox_read_file" in body
    assert "example.test" in body
    assert "a.txt" not in body  # overview counts files, does not dump names
    assert "Sandbox files: 1" in body or "沙盒文件：1" in body


def test_gateway_tool_callback_is_typed_and_replay_safe(tmp_path) -> None:
    store, channel, _identity = _paired(tmp_path)
    confirmations = []
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "",
            "pending_tools": [{
                "id": "call-1",
                "name": "sandbox_read_file",
                "pending_id": "pending-1",
                "version": 3,
            }],
        },
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *args: confirmations.append(args) or {"ok": True},
    )
    message = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="tool",
        text="read it",
    )
    card = gateway.handle(message).blocks[0]
    token = _find_action(card, "Allow once")
    assert gateway.handle(_event(channel.id, token=token, message_id="allow")).text == "Decision forwarded."
    action = confirmations[0][1]
    assert action == {
        "type": "confirmation_decision",
        "pending_id": "pending-1",
        "version": 3,
        "call_id": "call-1",
        "verdict": "allow",
    }
    assert "invalid or expired" in gateway.handle(
        _event(channel.id, token=token, message_id="replay")
    ).text


def test_confirmation_broker_allows_remote_sensitive_tool_once() -> None:
    class Session:
        _pending = {
            "pending_id": "pending-1",
            "version": 1,
            "confirmations": [{
                "tool_call": {
                    "id": "call-1",
                    "function": {"name": "run_host_python_script"},
                },
            }],
        }

        def _ensure_pending_identity(self):
            return "pending-1", 1

    result = ConfirmationBroker().prepare(
        Session(),
        {
            "type": "confirmation_decision",
            "pending_id": "pending-1",
            "version": 1,
            "call_id": "call-1",
            "verdict": "allow",
        },
        source="telegram",
    )
    assert result["ok"] is True
    assert result["decision"]["verdict"] == "allow"


def test_generic_webhook_parses_action_callback() -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="webhook-test", provider="webhook", name="Webhook"),
        {"signing_secret": "secret"},
    )
    event = adapter._event({
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "action-1",
        "action_token": "ca_token1234567890",
    })
    assert event is not None
    assert event.event_kind == "action"
    assert event.action_token == "ca_token1234567890"


def test_telegram_send_omits_null_optional_fields(monkeypatch) -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "test-token"},
    )
    sent = []

    class Response:
        is_success = True
        status_code = 200

    monkeypatch.setattr(
        "argusai.core.channels.adapters.httpx.post",
        lambda _url, **kwargs: sent.append(kwargs["json"]) or Response(),
    )
    adapter.send(ChannelReply(text="hello", external_chat_id="chat-1"))
    assert sent == [{"chat_id": "chat-1", "text": "hello", "parse_mode": "HTML"}]


def test_telegram_send_retries_bad_card_as_actionable_text(monkeypatch) -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "test-token"},
    )
    sent = []

    class Response:
        def __init__(self, success: bool) -> None:
            self.is_success = success
            self.status_code = 200 if success else 400
            self.text = "bad markup"

        def json(self):
            return {"description": "Bad Request: can't parse reply markup"}

    def post(_url, **kwargs):
        sent.append(kwargs["json"])
        return Response(len(sent) > 1)

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    adapter.send(ChannelReply(
        text="Confirm",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "Proceed?",
            "actions": [{"label": "Yes", "token": "ca_token1234567890"}],
        },),
    ))
    assert "reply_markup" in sent[0]
    assert "reply_markup" not in sent[1]
    assert "parse_mode" not in sent[1]
    assert "/act ca_token1234567890" in sent[1]["text"]


def test_telegram_send_retries_transient_transport_errors(monkeypatch) -> None:
    adapter = create_channel_adapter(
        ChannelConfig(id="telegram-test", provider="telegram", name="Telegram"),
        {"bot_token": "test-token"},
    )
    attempts = []

    class Response:
        is_success = True
        status_code = 200

    def post(url, **_kwargs):
        attempts.append(url)
        if len(attempts) < 3:
            raise httpx.ConnectError(
                "temporary TLS EOF",
                request=httpx.Request("POST", url),
            )
        return Response()

    monkeypatch.setattr("argusai.core.channels.adapters.httpx.post", post)
    monkeypatch.setattr(adapter._stop, "wait", lambda _delay: False)
    adapter.send(ChannelReply(text="hello", external_chat_id="chat-1"))
    assert len(attempts) == 3


def test_todo_plan_card_uses_status_icons_instead_of_raw_labels() -> None:
    cards = project_channel_cards(
        [{
            "type": "todo_list",
            "flat": [
                {"content": "Confirm SSH", "status": "completed"},
                {"content": "Read logs", "status": "in_progress"},
                {"content": "Write report", "status": "pending"},
            ],
        }],
        issue_action=lambda _payload: "unused",
        locale="zh",
    )
    assert cards[0]["type"] == "todo_plan"
    assert cards[0]["title"] == "计划"
    assert "已完成 1/3" in cards[0]["body"]
    text = card_text("", cards, include_actions=False, locale="zh")
    assert "[pending]" not in text
    assert "[completed]" not in text
    assert "✅" in text and "~~Confirm SSH~~" in text
    assert "🔵" in text and "**Read logs**" in text
    assert "⬜" in text and "Write report" in text


def test_channel_keeps_only_latest_live_todo_snapshot() -> None:
    cards = project_channel_cards(
        [
            {
                "type": "todo_list",
                "flat": [
                    {"content": "A", "status": "completed"},
                    {"content": "B", "status": "pending"},
                ],
            },
            {
                "type": "todo_list",
                "flat": [
                    {"content": "A", "status": "completed"},
                    {"content": "B", "status": "in_progress"},
                ],
            },
            {
                "type": "todo_list",
                "flat": [
                    {"content": "A", "status": "completed"},
                    {"content": "B", "status": "completed"},
                ],
            },
        ],
        issue_action=lambda _payload: "unused",
        locale="zh",
    )
    # Final snapshot is fully settled → omit the plan spam from the chat reply.
    assert cards == ()


def test_channel_keeps_latest_todo_while_plan_still_open() -> None:
    cards = project_channel_cards(
        [
            {
                "type": "todo_list",
                "plan_pending": True,
                "flat": [
                    {"content": "Old", "status": "pending"},
                ],
            },
            {
                "type": "user_decision",
                "question": "开始执行？",
                "options": ["开始执行", "取消任务"],
            },
            {
                "type": "todo_list",
                "plan_pending": True,
                "flat": [
                    {"content": "Confirm SSH", "status": "pending"},
                    {"content": "Write report", "status": "pending"},
                ],
            },
        ],
        issue_action=lambda payload: f"tok-{payload.get('response') or 'x'}",
        locale="zh",
    )
    assert [card["type"] for card in cards] == ["todo_plan", "user_decision"]
    assert [item["title"] for item in cards[0]["items"]] == [
        "Confirm SSH",
        "Write report",
    ]


def test_channel_drops_settled_plan_even_when_plan_pending() -> None:
    """list_todos of a finished plan must not sit beside a fresh set_todos plan."""
    cards = project_channel_cards(
        [
            {
                "type": "todo_list",
                "plan_pending": True,
                "flat": [
                    {"content": f"Old {i}", "status": "completed"}
                    for i in range(9)
                ],
            },
            {
                "type": "todo_list",
                "plan_pending": True,
                "flat": [
                    {"content": f"New {i}", "status": "pending"}
                    for i in range(6)
                ],
            },
            {
                "type": "user_decision",
                "question": "以上是多步任务计划（6 项待处理）。请确认是否按此计划开始执行？",
                "options": ["开始执行", "修改计划", "取消任务"],
            },
        ],
        issue_action=lambda payload: f"tok-{payload.get('response') or 'x'}",
        locale="zh",
    )
    assert [card["type"] for card in cards] == ["todo_plan", "user_decision"]
    assert cards[0]["body"] == "已完成 0/6 · 待处理 6"
    assert len(cards[0]["items"]) == 6


def test_feishu_keeps_full_option_text_when_button_label_is_truncated() -> None:
    long = "先停 vllm-vlm 再部署，并保留本次评估结论与后续监控配置"
    assert len(long) > 30
    reply = ChannelReply(
        text="",
        external_chat_id="chat-1",
        blocks=({
            "type": "user_decision",
            "title": "选择部署方式？",
            "actions": [
                {"label": "开始部署", "token": "ca_token1234567890"},
                {"label": long, "token": "ca_token0987654321"},
            ],
        },),
        locale="zh",
    )
    payload = render_channel_reply("feishu", reply)
    buttons = [
        column["elements"][0]["text"]["content"]
        for element in feishu_card_elements(payload["content"])
        if element.get("tag") == "column_set"
        for column in element.get("columns") or []
    ]
    assert buttons[0] == "开始部署"
    assert len(buttons[1]) <= 30
    assert long.startswith(buttons[1])
    body = "\n".join(
        str(item.get("content") or "")
        for item in feishu_card_elements(payload["content"])
        if item.get("tag") == "markdown"
    )
    assert long in body


def test_result_reply_recovers_decision_card_from_session_status(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)

    def session(_sid):
        return {
            "ok": True,
            "history": [{
                "role": "assistant",
                "blocks": [{
                    "type": "user_decision",
                    "question": "开始部署？",
                    "options": ["开始部署", "取消"],
                }],
            }],
        }

    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "",
            "status": "awaiting_user_decision",
            "blocks": [],
            "channel_live_delivered": True,
        },
        session_handler=session,
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="ask",
        text="ping",
    ))
    assert reply.blocks
    assert reply.blocks[0]["type"] == "user_decision"
    assert _find_action(reply.blocks[0], "开始部署").startswith("ca_")
    assert identity.id  # pairing still bound


def test_structured_card_projects_to_channel_info() -> None:
    issued = []
    cards = project_channel_cards(
        [{
            "type": "structured_card",
            "title": "核心能力一览",
            "subtitle": "按你常用的技术方向分组",
            "sections": [
                {
                    "type": "list",
                    "title": "逆向与协议",
                    "items": [
                        {"label": "JS/WASM 逆向"},
                        {"label": "Node VM 签名"},
                    ],
                },
                {
                    "type": "kv",
                    "title": "摘要",
                    "items": [{"label": "渠道", "value": "飞书", "note": "已配对"}],
                },
                {
                    "type": "actions",
                    "actions": [
                        {"label": "开始测站", "value": "测这个站点"},
                        {"label": "打开设置", "href": "#/settings"},
                    ],
                },
            ],
        }],
        issue_action=lambda payload: issued.append(payload) or f"tok-{len(issued)}",
        locale="zh",
    )
    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "info"
    assert card["title"] == "核心能力一览"
    assert card["body"] == "按你常用的技术方向分组"
    assert card["sections"][0]["title"] == "逆向与协议"
    assert "- JS/WASM 逆向" in card["sections"][0]["rows"]
    assert any("渠道" in row and "飞书" in row for row in card["sections"][1]["rows"])
    assert len(card["actions"]) == 1
    assert card["actions"][0]["label"] == "开始测站"
    assert issued == [{"kind": "user_decision", "response": "测这个站点"}]
    text = card_text("", cards, include_actions=False, locale="zh")
    assert "核心能力一览" in text
    assert "JS/WASM 逆向" in text
    rendered = render_channel_reply(
        "feishu",
        ChannelReply(text="", external_chat_id="chat-1", blocks=cards, locale="zh"),
    )
    assert rendered["msg_type"] == "interactive"
    assert rendered["content"]["schema"] == "2.0"
    assert "核心能力一览" in rendered["content"]["header"]["title"]["content"]
    elements = feishu_card_elements(rendered["content"])
    markdown = "\n".join(
        str(item.get("content") or "")
        for item in elements
        if item.get("tag") == "markdown"
    )
    assert "JS/WASM 逆向" in markdown
    tables = [item for item in elements if item.get("tag") == "table"]
    assert tables
    labels = {row.get("label") for row in tables[0]["rows"]}
    assert "渠道" in labels
    assert any("飞书" in str(row.get("value") or "") for row in tables[0]["rows"])


def test_data_table_and_file_resource_project_to_channel_info() -> None:
    issued = []
    cards = project_channel_cards(
        [
            {
                "type": "data_table",
                "title": "候选接口",
                "subtitle": "按置信度排序",
                "columns": [
                    {"key": "path", "label": "路径"},
                    {"key": "score", "label": "分"},
                ],
                "rows": [
                    {
                        "cells": {"path": "/api/sign", "score": "0.91"},
                        "actions": [{"label": "分析", "value": "分析 /api/sign"}],
                    },
                ],
            },
            {
                "type": "file_resource_view",
                "title": "沙箱文件",
                "subtitle": "本回合产出",
                "root_label": "session sandbox",
                "items": [
                    {
                        "name": "report.md",
                        "relative_path": "out/report.md",
                        "kind": "file",
                    },
                    {"name": "dumps", "relative_path": "dumps", "kind": "dir"},
                ],
            },
        ],
        issue_action=lambda payload: issued.append(payload) or f"tok-{len(issued)}",
        locale="zh",
    )
    assert [card["type"] for card in cards] == ["info", "info"]
    table, files = cards
    assert table["title"] == "候选接口"
    assert any("/api/sign" in row and "0.91" in row for row in table["sections"][0]["rows"])
    assert table["actions"][0]["label"] == "分析"
    assert issued[0]["response"] == "分析 /api/sign"
    assert table["table"]["columns"][0]["key"] == "path"
    assert table["table"]["rows"][0]["path"] == "/api/sign"
    table_text = card_text("", [table], include_actions=False, locale="zh")
    assert "| 路径 |" in table_text
    assert "/api/sign" in table_text
    slack = render_channel_reply(
        "slack",
        ChannelReply(text="", external_chat_id="chat-1", blocks=[table], locale="zh"),
    )
    slack_table = next(block for block in slack["blocks"] if block.get("type") == "table")
    assert slack_table["rows"][0][0]["text"] == "路径"
    assert slack_table["rows"][1][0]["text"] == "/api/sign"
    rendered = render_channel_reply(
        "feishu",
        ChannelReply(text="", external_chat_id="chat-1", blocks=cards, locale="zh"),
    )
    feishu_tables = [
        item
        for item in feishu_card_elements(rendered["content"])
        if item.get("tag") == "table"
    ]
    assert feishu_tables[0]["columns"][0]["display_name"] == "路径"
    assert feishu_tables[0]["rows"][0]["path"] == "/api/sign"
    assert files["title"] == "沙箱文件"
    file_text = card_text("", [files], include_actions=False, locale="zh")
    assert "report.md" in file_text
    assert "out/report.md" in file_text
    assert "dumps" in file_text


def test_text_fallback_is_deterministic() -> None:
    card = {
        "type": "user_decision",
        "title": "Proceed?",
        "body": "",
        "actions": [{"label": "Yes", "token": "ca_token1234567890"}],
    }
    first = card_text("", [card], include_tokens=True)
    assert first == card_text("", [card], include_tokens=True)
    assert "Proceed?" in first
    assert "/act ca_token1234567890" in first


def test_gateway_runtime_fake_adapter_delivers_card(tmp_path, monkeypatch) -> None:
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({
        "id": "webhook-test",
        "provider": "webhook",
        "name": "Webhook",
        "enabled": True,
    })
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    replies = []

    class FakeAdapter:
        def start(self, on_event):
            self.on_event = on_event

        def send(self, reply):
            replies.append(reply)

        def stop(self):
            return None

        def status(self):
            return {"channel_id": channel.id, "connected": True}

    fake = FakeAdapter()
    monkeypatch.setattr(
        "argusai.core.channels.runtime.create_channel_adapter",
        lambda _config, _credentials: fake,
    )
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "",
            "blocks": [{
                "type": "user_decision",
                "question": "Proceed?",
                "options": ["Yes", "No"],
            }],
        },
        session_handler=lambda _session_id: {"ok": True},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    runtime = ChannelRuntimeManager(store, gateway)
    try:
        runtime.refresh()
        runtime._handle_event(ChannelEvent(
            channel_id=channel.id,
            provider="webhook",
            external_user_id="user-1",
            external_chat_id="chat-1",
            message_id="message-1",
            text="ask",
        ))
    finally:
        runtime.shutdown()
    assert replies[0].blocks[0]["type"] == "user_decision"
    assert _find_action(replies[0].blocks[0], "Yes").startswith("ca_")


def test_confirm_without_permission_is_info_only_not_dead_buttons(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path, permissions=("chat", "status", "send"))
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "ArgusAI is waiting for confirmation.",
            "status": "awaiting_confirmation",
            "pending_tools": [{
                "id": "call-1",
                "pending_id": "pending-1",
                "version": 1,
                "name": "run_shell_snippet",
                "arguments": {"cmd": "ls"},
            }],
        },
        session_handler=lambda _session_id: {"ok": True, "pending_tools": []},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="m1",
        text="run something",
    ))
    assert reply.blocks
    assert all(not list(card.get("actions") or []) for card in reply.blocks)
    body = _body(reply)
    assert "run_shell_snippet" in body
    assert "confirm" in body.lower() or "远程确认" in body or "grant" in body.lower()


def test_wait_cards_already_delivered_skip_sync_confirm_duplicate(tmp_path) -> None:
    store, channel, identity = _paired(tmp_path)
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {
            "ok": True,
            "text": "ArgusAI is waiting for confirmation.",
            "status": "awaiting_confirmation",
            "channel_wait_cards_delivered": True,
            "channel_live_delivered": True,
            "blocks": [{
                "type": "confirm",
                "title": "Run run_shell_snippet?",
                "name": "run_shell_snippet",
                "confirm_id": "call-1",
                "pending_id": "pending-1",
                "pending_version": 1,
            }],
            "pending_tools": [{
                "id": "call-1",
                "pending_id": "pending-1",
                "version": 1,
                "name": "run_shell_snippet",
            }],
        },
        session_handler=lambda _session_id: {"ok": True, "pending_tools": []},
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "session-1",
        confirmation_handler=lambda *_args: {"ok": True},
    )
    reply = gateway.handle(ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id="m1",
        text="run something",
    ))
    assert not any(
        str(card.get("type") or "") in {"tool_confirm", "confirm"}
        for card in reply.blocks
    )
    assert not any(list(card.get("actions") or []) for card in reply.blocks)
