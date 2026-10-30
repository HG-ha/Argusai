from __future__ import annotations

import re
import time

from argusai.core.channels import ChannelEvent, ChannelGateway, ChannelStore
from argusai.core.channels.cards import card_text, feishu_card_elements, render_channel_reply
from argusai.core.channels.i18n import (
    BILINGUAL,
    MESSAGES,
    SINGLE_LOCALE_KEYS,
    Frag,
    channel_text,
    detect_system_locale,
    normalize_locale,
)


def _body(reply) -> str:
    """Reply text as a card-capable provider shows it: body without button rows."""
    return card_text(
        reply.text,
        reply.blocks,
        include_actions=False,
        locale=reply.locale,
    )


def _build(tmp_path, locale: str | None, sessions: list | None = None):
    store = ChannelStore(tmp_path)
    channel = store.upsert_channel({"provider": "webhook", "name": "Webhook"})
    gateway = ChannelGateway(
        store,
        turn_handler=lambda _params: {"ok": True, "text": "answer"},
        session_handler=lambda _session_id: {
            "ok": True,
            "message_count": 2,
            "tools": [{"name": "read_file"}],
        },
        cancel_handler=lambda _session_id: {"ok": True},
        new_session_handler=lambda _identity: "created-session",
        confirmation_handler=lambda *_args: {"ok": True},
        session_list_handler=lambda: list(sessions or []),
        locale_provider=(lambda: locale) if locale is not None else None,
    )
    return store, channel, gateway


def _pair(store, channel, gateway, permissions=("chat", "status", "configure")):
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    return store.set_identity_permissions(identity.id, list(permissions))


def _event(channel_id: str, text: str, message_id: str) -> ChannelEvent:
    return ChannelEvent(
        channel_id=channel_id,
        provider="webhook",
        external_user_id="user-1",
        external_chat_id="chat-1",
        message_id=message_id,
        text=text,
        timestamp=time.time(),
    )


def test_detect_system_locale_honors_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("ARGUSAI_LANG", "en")
    monkeypatch.setattr(
        "argusai.core.channels.i18n.macos_preferred_language_tags",
        lambda: ["zh-Hans-CN"],
    )
    assert detect_system_locale() == "en"


def test_detect_system_locale_macos_beats_english_lang(monkeypatch) -> None:
    monkeypatch.delenv("ARGUSAI_LANG", raising=False)
    monkeypatch.delenv("ARGUSAI_LOCALE", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr("argusai.core.channels.i18n._win_is_chinese", lambda: False)
    monkeypatch.setattr(
        "argusai.core.channels.i18n.macos_preferred_language_tags",
        lambda: ["zh-Hans-CN", "en-CN"],
    )
    assert detect_system_locale() == "zh"


def test_macos_plist_preferred_languages(tmp_path, monkeypatch) -> None:
    import plistlib
    from pathlib import Path

    from argusai.core.channels import i18n as i18n_mod

    home = tmp_path / "alice"
    prefs = home / "Library" / "Preferences"
    prefs.mkdir(parents=True)
    with (prefs / ".GlobalPreferences.plist").open("wb") as fh:
        plistlib.dump({"AppleLanguages": ["zh-Hant-TW", "en"]}, fh)
    monkeypatch.setattr(i18n_mod.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert i18n_mod._macos_plist_preferred_languages() == ["zh-Hant-TW", "en"]


def test_detect_system_locale_macos_english_primary(monkeypatch) -> None:
    monkeypatch.delenv("ARGUSAI_LANG", raising=False)
    monkeypatch.delenv("ARGUSAI_LOCALE", raising=False)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setattr("argusai.core.channels.i18n._win_is_chinese", lambda: False)
    monkeypatch.setattr(
        "argusai.core.channels.i18n.macos_preferred_language_tags",
        lambda: ["en-US", "zh-Hans-CN"],
    )
    assert detect_system_locale() == "en"


def test_catalog_covers_every_key_in_both_locales() -> None:
    for key, entry in MESSAGES.items():
        assert set(entry) == {"en", "zh"}, key
        assert entry["en"].strip() and entry["zh"].strip(), key
    assert normalize_locale("zh-CN") == "zh"
    assert normalize_locale("en-US") == "en"
    assert normalize_locale(None) == "en"
    assert normalize_locale("de") == "en"


def test_gateway_defaults_to_english_without_a_locale_provider(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, None)
    assert "not paired" in gateway.handle(_event(channel.id, "hi", "m1")).text
    pairing = store.create_pairing(channel.id)
    paired = gateway.handle(_event(channel.id, f"/pair {pairing['code']}", "m2"))
    assert paired.text.startswith("Pairing complete")
    assert paired.locale == "en"


def test_gateway_renders_chinese_when_the_host_ui_is_chinese(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, "zh")
    assert "尚未配对" in gateway.handle(_event(channel.id, "hi", "m1")).text

    pairing = store.create_pairing(channel.id)
    paired = gateway.handle(_event(channel.id, f"/pair {pairing['code']}", "m2"))
    assert paired.locale == "zh"
    assert "配对完成" in paired.text
    assert "/new" in paired.text

    assert "尚未绑定会话" in gateway.handle(_event(channel.id, "hi", "m3")).text
    assert "已新建并绑定会话" in gateway.handle(_event(channel.id, "/new", "m4")).text
    assert "可用命令" in _body(gateway.handle(_event(channel.id, "/help", "m5")))
    unknown = gateway.handle(_event(channel.id, "/nope", "m6")).text
    assert "未知命令" in unknown
    assert "/nope" in unknown
    # Feishu @Bot /status arrives as ``@_user_1 /status``.
    status = _body(gateway.handle(_event(channel.id, "@_user_1 /status", "m6b")))
    assert "会话概览" in status
    # Mention-only / stray slash palette taps stay silent.
    assert gateway.handle(_event(channel.id, "@_user_1", "m6c")).text == ""
    assert gateway.handle(_event(channel.id, "/", "m6d")).text == ""
    status = _body(gateway.handle(_event(channel.id, "/status", "m7")))
    assert "会话概览" in status
    assert "消息：2" in status
    assert "Token / 缓存：暂无" in status
    assert "没有待确认项" in gateway.handle(_event(channel.id, "/tools", "m8")).text


def test_store_errors_are_translated_through_their_code(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, "zh")
    assert "配对码无效或已过期" in gateway.handle(
        _event(channel.id, "/pair 000000", "m1")
    ).text

    pairing = store.create_pairing(channel.id)
    gateway.handle(_event(channel.id, f"/pair {pairing['code']}", "m2"))
    expired = gateway.handle(_event(channel.id, "/act ca_00000000000000000000", "m3"))
    assert "无效或已过期" in expired.text


def test_gateway_preserves_chat_transcripts_code_and_emoji(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, "zh")
    _pair(store, channel, gateway)
    turns = []

    def capture_turn(params):
        turns.append(params)
        return {"ok": True, "text": "answer"}

    gateway.turn_handler = capture_turn
    message = (
        "【合并转发】\r\n[甲] 请检查这段代码：\r\n"
        "```python\r\nif ready:\r\n    run()\r\n\tfinish()\r\n```\r\n\r\n"
        "[乙] 保留  两个空格，以及 👩‍💻 的连接符。"
    )

    reply = gateway.handle(_event(channel.id, message, "forward-1"))

    assert reply.text == "answer"
    assert len(turns) == 1
    assert turns[0]["message"] == message


def test_gateway_keeps_command_whitespace_normalization_separate(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, "zh")
    _pair(store, channel, gateway)
    turns = []
    gateway.turn_handler = lambda params: turns.append(params) or {"ok": True}

    reply = gateway.handle(_event(channel.id, "\t@_user_1\u200b\n /status  \r\n", "status-1"))

    assert "会话概览" in _body(reply)
    assert turns == []
    assert gateway.handle(_event(channel.id, "@_user_1\u200b \n", "mention-1")).text == ""


def test_confirmation_card_buttons_follow_the_locale(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, "zh")
    pairing = store.create_pairing(channel.id, bound_session_id="session-1")
    identity = store.consume_pairing(
        channel.id,
        pairing["code"],
        external_user_id="user-1",
        external_chat_id="chat-1",
    )
    store.set_identity_permissions(identity.id, ["chat", "status", "confirm"])

    gateway.turn_handler = lambda _params: {
        "ok": True,
        "text": "",
        "pending_tools": [{
            "id": "call-1",
            "pending_id": "pending-1",
            "version": 1,
            "name": "run_command",
            "arguments": {"command": "ls"},
            "risk": "medium",
        }],
    }
    reply = gateway.handle(_event(channel.id, "do it", "m1"))
    assert "正在等待确认" in reply.text
    card = reply.blocks[0]
    assert card["title"] == "运行 run_command？"
    assert [item["label"] for item in card["actions"]] == ["允许一次", "拒绝"]

    payload = render_channel_reply("telegram", reply)
    labels = [
        button["text"]
        for row in payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert labels == ["允许一次", "拒绝"]


def test_card_text_stays_empty_without_inventing_done() -> None:
    assert card_text("", []) == ""
    assert card_text("", [], locale="zh") == ""
    assert card_text("你好", []) == "你好"


def test_bilingual_mode_emits_chinese_first_then_english() -> None:
    single = channel_text("session_not_found", BILINGUAL)
    assert single.splitlines() == [
        channel_text("session_not_found", "zh"),
        channel_text("session_not_found", "en"),
    ]
    # Multi-line entries get a blank line so the two languages stay separable.
    multi = channel_text("auto_allow_body", BILINGUAL)
    assert channel_text("auto_allow_body", "zh") in multi
    assert channel_text("auto_allow_body", "en") in multi
    assert normalize_locale("zh+en") == BILINGUAL


def test_bilingual_never_mixes_languages_inside_one_line() -> None:
    text = channel_text(
        "status_line",
        BILINGUAL,
        session_id="s1",
        count=2,
        suffix=Frag("status_waiting", name="run_command"),
    )
    chinese, english = text.splitlines()
    assert "正在等待确认" in chinese and "Waiting for confirmation" not in chinese
    assert "Waiting for confirmation" in english and "正在等待确认" not in english


def test_button_captions_stay_single_language_in_bilingual_mode() -> None:
    for key in SINGLE_LOCALE_KEYS:
        fields = {
            name: "x"
            for locale in ("zh", "en")
            for name in re.findall(r"{(\w+)}", MESSAGES[key][locale])
        }
        assert "\n" not in channel_text(key, BILINGUAL, **fields), key
    assert channel_text("card_allow_once", BILINGUAL) == channel_text("card_allow_once", "zh")


def test_channels_answer_bilingually_by_default(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, None)
    gateway.default_locale = BILINGUAL
    unpaired = gateway.handle(_event(channel.id, "hi", "m1")).text
    assert "尚未配对" in unpaired
    assert "not paired" in unpaired

    pairing = store.create_pairing(channel.id)
    paired = gateway.handle(_event(channel.id, f"/pair {pairing['code']}", "m2"))
    assert paired.locale == BILINGUAL
    assert "配对完成" in paired.text and "Pairing complete" in paired.text

    help_reply = gateway.handle(_event(channel.id, "/help", "m3"))
    help_text = _body(help_reply)
    # The command reference travels as a card, so providers with a real header
    # render the title there instead of as body text.
    assert help_reply.blocks[0]["type"] == "info"
    feishu = render_channel_reply("feishu", help_reply)
    assert feishu["content"]["header"]["title"]["content"] == "可用命令 / Commands"
    assert "可用命令 / Commands" in help_text
    # One row per command, with both descriptions kept on that row.
    row = next(line for line in help_text.splitlines() if line.startswith("/lang"))
    assert row == "/lang [zh|en|both] — 回复语言 / reply language"
    assert help_text.count("/lang") == 1
    assert "**会话 / Conversation**" in help_text
    # The header carries the title, so it is not repeated in the card body.
    help_body = feishu_card_elements(feishu["content"])[0]["content"]
    assert help_body.startswith("**会话")


def test_session_listing_keeps_rows_single_language(tmp_path) -> None:
    sessions = [
        {"id": f"session-{index}", "name": f"审计 {index}", "message_count": index}
        for index in range(1, 4)
    ]
    store, channel, gateway = _build(tmp_path, None, sessions)
    gateway.default_locale = BILINGUAL
    identity = _pair(store, channel, gateway)
    store.grant_identity_session(identity.id, "session-2")
    store.grant_identity_session(identity.id, "session-3")

    reply = gateway.handle(_event(channel.id, "/sessions", "m1"))
    lines = _body(reply).splitlines()
    # The card header must stay one line, so both languages share it.
    assert "会话列表" in lines[0] and "Conversations" in lines[0]
    rows = [line for line in lines if line.startswith(("1.", "2.", "3."))]
    assert len(rows) == 3
    for row in rows:
        assert "条消息" in row and "messages" not in row
    # A blank line stops Markdown from folding the hint into the last row.
    assert lines[lines.index(rows[-1]) + 1] == ""
    assert "1. 审计 1" in render_channel_reply("telegram", reply)["text"]
    assert identity.locale == ""


def test_lang_command_persists_a_per_identity_preference(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, None)
    gateway.default_locale = BILINGUAL
    identity = _pair(store, channel, gateway)

    usage = gateway.handle(_event(channel.id, "/lang", "m1")).text
    assert "用法" in usage and "Usage" in usage

    switched = gateway.handle(_event(channel.id, "/lang zh", "m2"))
    assert switched.locale == "zh"
    assert switched.text == channel_text("lang_set", "zh", current="zh")
    assert store.get_identity(identity.id).locale == "zh"

    follow_up = gateway.handle(_event(channel.id, "/help", "m3"))
    assert follow_up.locale == "zh"
    assert "Commands:" not in follow_up.text

    english = gateway.handle(_event(channel.id, "/lang en", "m4"))
    assert english.locale == "en"
    assert store.get_identity(identity.id).locale == "en"
    assert "尚未" not in gateway.handle(_event(channel.id, "/help", "m5")).text

    back = gateway.handle(_event(channel.id, "/lang both", "m6"))
    assert back.locale == BILINGUAL
    assert store.get_identity(identity.id).locale == BILINGUAL


def test_identity_locale_does_not_leak_between_users(tmp_path) -> None:
    store, channel, gateway = _build(tmp_path, None)
    gateway.default_locale = BILINGUAL
    identity = _pair(store, channel, gateway)
    store.set_identity_locale(identity.id, "en")

    assert gateway.handle(_event(channel.id, "/help", "m1")).locale == "en"
    other = ChannelEvent(
        channel_id=channel.id,
        provider="webhook",
        external_user_id="user-2",
        external_chat_id="chat-2",
        message_id="m2",
        text="hi",
    )
    reply = gateway.handle(other)
    assert reply.locale == BILINGUAL
    assert "尚未配对" in reply.text
