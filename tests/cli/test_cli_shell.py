"""CLI local slash-command shell tests."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from argusai.cli.shell import dispatch, looks_like_slash, needs_tty_confirm


class _FakeApi:
    def __init__(self) -> None:
        self.stream_calls: list[dict[str, Any]] = []
        self.option_calls: list[tuple[str, str, str]] = []
        self._sessions = [
            {"id": "cli-default", "name": "CLI", "message_count": 2},
            {"id": "abc123", "name": "Other", "message_count": 5},
        ]

    def get_assistant_session(self, session_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "session_id": session_id,
            "name": "CLI",
            "message_count": 2,
            "todos": [],
            "pending_tools": [],
            "allow_web_search": True,
            "thinking_enabled": False,
            "channel_live": True,
            "engagement_declaration": None,
            "runtime": {"runtime_id": "argusai-native"},
            "active_stream": None,
            "tools": [{"name": "get_settings"}],
        }

    def get_app_snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "ai": {"endpoint_count": 1, "endpoint_names": ["ds"]},
            "proxy": {"running": False},
            "running_count": 0,
            "task_count": 0,
            "checks_available": 17,
            "knowledge": {"entries": []},
        }

    def list_assistant_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions)

    def cancel_assistant_run(self, session_id: str) -> dict[str, Any]:
        return {"ok": True, "session_id": session_id}

    def start_assistant_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        self.stream_calls.append(dict(params))
        return {"ok": True, "run_id": "run-1"}

    def start_assistant_action_stream(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def set_locale(self, locale: str) -> dict[str, Any]:
        self._locale = locale
        return {"ok": True, "locale": locale}

    def _confirm_channel_action(
        self,
        session_id: str,
        action: dict[str, Any],
        source: str,
    ) -> dict[str, Any]:
        self.last_confirm = {"session_id": session_id, "action": action, "source": source}
        return {"ok": True}

    def _channel_session_file(self, session_id: str, selector: str) -> dict[str, Any]:
        if selector in {"list", "search foo"} or selector.startswith("search "):
            return {
                "ok": True,
                "files": [
                    {
                        "index": 1,
                        "name": "notes.txt",
                        "relative_path": "notes.txt",
                        "size": 12,
                    }
                ],
            }
        if selector in {"1", "notes.txt"}:
            return {
                "ok": True,
                "attachment": {
                    "name": "notes.txt",
                    "relative_path": "notes.txt",
                    "path": f"/tmp/{session_id}/notes.txt",
                    "size": 12,
                    "sha256": "abcd" * 8,
                    "mime": "text/plain",
                },
            }
        return {"ok": False, "error": "not found"}

    def assistant_side_question(self, session_id: str, question: str = "") -> dict[str, Any]:
        return {"ok": True, "text": f"side:{question}"}

    def _channel_ai_endpoint(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        rows = [
            {
                "index": 0,
                "ordinal": 1,
                "name": "ds",
                "model": "deepseek-v4-flash",
                "value": "deepseek-v4-flash",
                "active": True,
            },
            {
                "index": 1,
                "ordinal": 2,
                "name": "backup",
                "model": "other-model",
                "value": "other-model",
                "active": False,
            },
        ]
        if action == "list":
            return {"ok": True, "kind": "endpoint", "endpoints": rows}
        if action == "resolve":
            selector = str(target.get("selector") or "")
            match = next(
                (row for row in rows if str(row["ordinal"]) == selector or row["model"] == selector),
                None,
            )
            if match is None:
                return {"ok": False, "code": "endpoint_not_found"}
            return {"ok": True, "kind": "endpoint", "endpoint": match}
        if action == "activate":
            return {
                "ok": True,
                "kind": "endpoint",
                "endpoint": {
                    "name": target.get("name") or "backup",
                    "model": target.get("value") or "other-model",
                },
            }
        return {"ok": False, "code": "endpoint_failed"}

    def _channel_session_option(
        self,
        session_id: str,
        option: str,
        action: str,
    ) -> dict[str, Any]:
        self.option_calls.append((session_id, option, action))
        if action == "status":
            return {"ok": True, "enabled": True}
        return {"ok": True, "enabled": action == "on", "message": f"{option} {action}"}

    def _channel_agent_runtime(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        if action == "list":
            return {
                "ok": True,
                "agents": [
                    {
                        "ordinal": 1,
                        "id": "argusai-native",
                        "name": "ArgusAI",
                        "kind": "native",
                        "available": True,
                    }
                ],
            }
        return {"ok": True, "session_id": "new-agent-session", "agent": {"name": "ArgusAI"}}


def test_looks_like_slash() -> None:
    assert looks_like_slash("/status")
    assert not looks_like_slash("status")
    assert not looks_like_slash("/")


def test_status_is_local_not_agent() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    result = dispatch(api, "cli-default", "/status")
    assert result.handled
    assert api.stream_calls == []
    joined = "\n".join(result.lines)
    assert "cli-default" in joined
    assert "空闲" in joined or "idle" in joined.lower()
    assert "Token / 缓存：暂无" in joined


def test_status_shows_token_cache_hit() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    original = api.get_assistant_session

    def _with_tokens(session_id: str) -> dict[str, Any]:
        payload = original(session_id)
        payload["token_usage"] = {
            "input_tokens": 120_000,
            "output_tokens": 4_200,
            "cache_hit_tokens": 90_000,
            "cache_miss_tokens": 30_000,
            "cache_write_tokens": 1_500,
            "calls": 8,
        }
        payload["cache_hit_rate"] = 75.0
        return payload

    api.get_assistant_session = _with_tokens  # type: ignore[method-assign]
    set_locale("zh")
    result = dispatch(api, "cli-default", "/status")
    joined = "\n".join(result.lines)
    assert "IN 120.0k" in joined
    assert "OUT 4.2k" in joined
    assert "CALLS 8" in joined
    assert "HIT 75.0%" in joined
    assert "H/M 90.0k/30.0k" in joined
    assert "缓存写入：1.5k" in joined


def test_status_shows_context_fill() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    original = api.get_assistant_session

    def _with_context(session_id: str) -> dict[str, Any]:
        payload = original(session_id)
        payload["thinking_enabled"] = True
        payload["thinking_effort"] = "high"
        payload["prompt_tokens"] = 123_400
        payload["context_window"] = 256_000
        return payload

    api.get_assistant_session = _with_context  # type: ignore[method-assign]
    set_locale("zh")
    result = dispatch(api, "cli-default", "/status")
    joined = "\n".join(result.lines)
    assert "上下文：48% · 123.4k / 256.0k" in joined
    assert "思考：开（high）" in joined
    assert "[high]" in joined


def test_unknown_slash_not_forwarded() -> None:
    api = _FakeApi()
    result = dispatch(api, "cli-default", "/not-a-real-cmd")
    assert result.handled
    assert api.stream_calls == []
    assert any("unknown" in line.lower() or "未知" in line for line in result.lines)


def test_plain_text_not_handled() -> None:
    api = _FakeApi()
    result = dispatch(api, "cli-default", "hello agent")
    assert not result.handled


def test_prepare_cli_turn_extracts_quoted_files(tmp_path) -> None:
    from argusai.cli.commands import chat as chat_mod

    text_path = tmp_path / "request notes.txt"
    text_path.write_text("proof", encoding="utf-8")
    binary_path = tmp_path / "sample.bin"
    binary_path.write_bytes(b"\x00\x01")

    message, attachments = chat_mod._prepare_cli_turn(
        f'分析 @"{text_path}" 和 @"{binary_path}"'
    )

    assert message == "分析 和"
    assert [item["type"] for item in attachments] == ["text", "file"]
    assert attachments[0]["text"] == "proof"
    assert attachments[1]["data_base64"] == "AAE="


def test_prepare_cli_turn_keeps_regular_at_mentions() -> None:
    from argusai.cli.commands import chat as chat_mod

    message, attachments = chat_mod._prepare_cli_turn("询问 @github")

    assert message == "询问 @github"
    assert attachments == []


def test_prepare_cli_turn_rejects_oversized_text(tmp_path, monkeypatch) -> None:
    import pytest

    from argusai.cli.commands import chat as chat_mod

    path = tmp_path / "large.txt"
    path.write_text("1234", encoding="utf-8")
    monkeypatch.setattr(chat_mod, "_ATTACHMENT_TEXT_MAX_BYTES", 3)

    with pytest.raises(ValueError, match="file too large"):
        chat_mod._prepare_cli_turn("check", [path])


def test_cli_file_completer_fuzzy_matches_from_launch_root(tmp_path) -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from argusai.cli.prompt import CliFileCompleter

    source = tmp_path / "src"
    source.mkdir()
    (source / "assistant_session.py").write_text("pass", encoding="utf-8")
    (tmp_path / "request notes.txt").write_text("proof", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "assistant-secret.js").write_text("x", encoding="utf-8")
    completer = CliFileCompleter(tmp_path, cache_seconds=0)
    event = CompleteEvent(completion_requested=True)

    fuzzy = list(
        completer.get_completions(
            Document("@asts", cursor_position=5),
            event,
        )
    )
    assert fuzzy
    assert fuzzy[0].text.replace("\\", "/") == "@src/assistant_session.py"
    assert all("node_modules" not in item.text for item in fuzzy)

    spaced = list(
        completer.get_completions(
            Document("@reqnote", cursor_position=8),
            event,
        )
    )
    assert spaced[0].text == '@"request notes.txt"'


def test_team_mention_completes_before_files(tmp_path) -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from argusai.cli.prompt import CliInputCompleter, set_team_mentions

    source = tmp_path / "src"
    source.mkdir()
    (source / "static_notes.py").write_text("pass", encoding="utf-8")
    event = CompleteEvent(completion_requested=True)
    set_team_mentions(["static-analyst", "crypto-spotter"])
    try:
        completer = CliInputCompleter(tmp_path)
        items = list(completer.get_completions(Document("@sta", cursor_position=4), event))
    finally:
        set_team_mentions(None)
    assert items
    assert items[0].text == "@static-analyst"
    assert any("static_notes.py" in item.text for item in items[1:])

    set_team_mentions(None)
    bare = CliInputCompleter(tmp_path)
    files_only = list(bare.get_completions(Document("@sta", cursor_position=4), event))
    assert files_only
    assert all(not item.text.startswith("@static-analyst") for item in files_only)


def test_cli_file_completer_defaults_to_current_directory(tmp_path, monkeypatch) -> None:
    from argusai.cli.prompt import CliFileCompleter

    monkeypatch.chdir(tmp_path)
    completer = CliFileCompleter()

    assert completer.root == tmp_path.resolve()


def test_cli_command_completer_fuzzy_matches_commands_and_options() -> None:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from argusai.cli.prompt import CliCommandCompleter

    completer = CliCommandCompleter()
    event = CompleteEvent(completion_requested=True)

    commands = list(completer.get_completions(Document("/sbar"), event))
    assert commands[0].text == "/sidebar "

    options = list(completer.get_completions(Document("/sidebar a"), event))
    assert options[0].text == "auto"
    assert options[0].start_position == -1

    from argusai.cli.prompt import _slash_completion_query

    think_opts = (
        "on", "off", "status", "auto", "none", "minimal",
        "low", "medium", "high", "xhigh", "max",
    )
    gap = list(completer.get_completions(Document("/think "), event))
    assert {item.text for item in gap} == set(think_opts)
    assert gap[0].text == "on"
    assert gap[0].start_position == 0

    query, width, choices, command_stage = _slash_completion_query(Document("/think on "))
    assert command_stage is False
    assert query == "on"
    assert width == 3
    assert choices == think_opts

    thinking_opts = list(completer.get_completions(Document("/thinking o"), event))
    assert thinking_opts[0].text == "on"

    assert list(completer.get_completions(Document("/btw 继续分析"), event)) == []


def test_cli_completion_escape_restores_typed_query() -> None:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.document import Document

    from argusai.cli.prompt import cancel_input_completion

    buffer = Buffer(document=Document("/sbar"))
    buffer._set_completions(  # noqa: SLF001
        [Completion("/sidebar ", start_position=-5)]
    )
    buffer.go_to_completion(0)
    assert buffer.text == "/sidebar "

    assert cancel_input_completion(buffer) is True
    assert buffer.complete_state is None
    assert buffer.text == "/sbar"


def test_model_list() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    result = dispatch(api, "cli-default", "/model")
    assert result.handled
    joined = "\n".join(result.lines)
    assert "deepseek-v4-flash" in joined
    assert "端点" in joined or "failover" in joined
    assert "/model" in joined


def test_use_switches_session() -> None:
    api = _FakeApi()
    result = dispatch(api, "cli-default", "/use 2")
    assert result.handled
    assert result.session_id == "abc123"


def test_search_status() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    result = dispatch(api, "cli-default", "/search status")
    assert result.handled
    joined = "\n".join(result.lines)
    assert "联网" in joined or "search" in joined.lower()


def test_think_status_and_toggle() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    status = dispatch(api, "cli-default", "/think")
    assert status.handled
    assert api.option_calls[-1] == ("cli-default", "think", "status")
    joined = "\n".join(status.lines)
    assert "思考" in joined
    on = dispatch(api, "cli-default", "/think on")
    assert on.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")
    alias = dispatch(api, "cli-default", "/thinking off")
    assert alias.handled
    assert api.option_calls[-1] == ("cli-default", "think", "off")
    spaced = dispatch(api, "cli-default", "/think  on")
    assert spaced.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")
    bracket = dispatch(api, "cli-default", "/think [on]")
    assert bracket.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")
    chinese = dispatch(api, "cli-default", "/think 开")
    assert chinese.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")
    glued = dispatch(api, "cli-default", "/thinkon")
    assert glued.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")
    before = len(api.option_calls)
    usage = dispatch(api, "cli-default", "/think maybe")
    assert usage.handled
    assert len(api.option_calls) == before
    assert "用法" in "\n".join(usage.lines) or "Usage" in "\n".join(usage.lines)
    telegram = dispatch(api, "cli-default", "/think@Bot on")
    assert telegram.handled
    assert api.option_calls[-1] == ("cli-default", "think", "on")


def test_lang() -> None:
    from argusai.cli.locale import get_locale, set_locale

    api = _FakeApi()
    set_locale("en")
    result = dispatch(api, "cli-default", "/lang zh")
    assert result.handled
    assert get_locale() == "zh"
    assert getattr(api, "_locale", None) == "zh"
    status = dispatch(api, "cli-default", "/status")
    joined = "\n".join(status.lines)
    assert "空闲" in joined or "名称" in joined


def test_lang_en_status() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    dispatch(api, "cli-default", "/lang en")
    status = dispatch(api, "cli-default", "/status")
    joined = "\n".join(status.lines)
    assert "idle" in joined.lower() or "Name:" in joined


def test_file_list_and_resolve() -> None:
    api = _FakeApi()
    listed = dispatch(api, "cli-default", "/file")
    assert listed.handled
    assert any("notes.txt" in line for line in listed.lines)
    resolved = dispatch(api, "cli-default", "/file 1")
    assert resolved.handled
    assert any("sha256" in line for line in resolved.lines)


def test_needs_tty_confirm_for_allow_all_and_model_switch() -> None:
    assert needs_tty_confirm("/allow all")
    assert needs_tty_confirm("/Allow ALL")
    assert not needs_tty_confirm("/allow")
    assert not needs_tty_confirm("/allow pending")
    assert not needs_tty_confirm("/allow off")
    assert not needs_tty_confirm("/deny all")
    assert needs_tty_confirm("/model backup")
    assert needs_tty_confirm("/model other-model")
    assert not needs_tty_confirm("/model")
    assert not needs_tty_confirm("/model list")
    assert not needs_tty_confirm("/status")


def test_allow_all_uses_confirm_callback(monkeypatch) -> None:
    api = _FakeApi()
    prompts: list[tuple[str, bool]] = []

    def _confirm(prompt: str, default: bool) -> bool:
        prompts.append((prompt, default))
        return True

    def _boom(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("Confirm.ask must not run when a TUI confirm is provided")

    monkeypatch.setattr("argusai.cli.shell.Confirm.ask", _boom)
    result = dispatch(api, "cli-default", "/allow all", confirm=_confirm)
    assert result.handled
    assert len(prompts) == 1
    assert prompts[0][1] is False
    assert "[amber]" not in prompts[0][0]
    assert "high-risk" in prompts[0][0].lower() or "高危" in prompts[0][0]
    assert ("cli-default", "tools", "allow_all") in api.option_calls
    joined = "\n".join(result.lines)
    assert "allow" in joined.lower() or "放行" in joined


def test_allow_all_aborted_skips_session_option() -> None:
    api = _FakeApi()
    result = dispatch(
        api, "cli-default", "/allow all", confirm=lambda _prompt, _default: False
    )
    assert result.handled
    assert not any(call[2] == "allow_all" for call in api.option_calls)
    joined = "\n".join(result.lines)
    assert "abort" in joined.lower() or "取消" in joined


def test_model_switch_uses_confirm_callback(monkeypatch) -> None:
    api = _FakeApi()
    prompts: list[tuple[str, bool]] = []

    def _confirm(prompt: str, default: bool) -> bool:
        prompts.append((prompt, default))
        return True

    monkeypatch.setattr(
        "argusai.cli.shell.Confirm.ask",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Confirm.ask must not run when a TUI confirm is provided")
        ),
    )
    result = dispatch(api, "cli-default", "/model other-model", confirm=_confirm)
    assert result.handled
    assert prompts and prompts[0][1] is True
    joined = "\n".join(result.lines)
    assert "backup" in joined.lower() or "switched" in joined.lower() or "切换" in joined


def test_model_switch_aborted_by_confirm() -> None:
    api = _FakeApi()
    result = dispatch(
        api, "cli-default", "/model other-model", confirm=lambda _prompt, _default: False
    )
    assert result.handled
    joined = "\n".join(result.lines)
    assert "abort" in joined.lower() or "取消" in joined


def test_deny_pending_uses_confirm_path() -> None:
    api = _FakeApi()
    # Inject a pending tool into status responses.
    original = api.get_assistant_session

    def _with_pending(session_id: str) -> dict[str, Any]:
        payload = original(session_id)
        payload["pending_tools"] = [
            {
                "id": "call_1",
                "name": "run_shell",
                "pending_id": "p1",
                "version": 1,
            }
        ]
        return payload

    api.get_assistant_session = _with_pending  # type: ignore[method-assign]
    result = dispatch(api, "cli-default", "/deny")
    assert result.handled
    assert getattr(api, "last_confirm", None) is not None
    assert api.last_confirm["action"]["verdict"] == "deny"


def test_quit() -> None:
    result = dispatch(SimpleNamespace(), "cli-default", "/quit")
    assert result.handled
    assert result.quit


def test_help_aligns_bracketed_commands() -> None:
    """Usage brackets like ``[on|off]`` must not break Rich column alignment."""
    from rich.markup import render

    from argusai.cli.locale import set_locale
    from argusai.cli.shell import _HELP_CMD_WIDTH, _help_lines, _help_row

    set_locale("zh")
    # Escape-after-pad: visible desc column starts at indent + width + gap.
    plain = str(render(_help_row("/allow [call-id|pending|all|off]", "允许工具调用")).plain)
    assert plain[2 : 2 + _HELP_CMD_WIDTH].rstrip() == "/allow [call-id|pending|all|off]"
    assert plain[2 + _HELP_CMD_WIDTH + 1 :].startswith("允许工具调用")

    joined = "\n".join(_help_lines())
    assert "/act" not in joined
    assert "/live" not in joined
    assert "/think [on|off|status|effort]" in str(render(joined).plain)
    assert "@<path>" in joined
    assert "--file" in joined
    assert "/sidebar [on|off|auto]" in str(render(joined).plain)


def test_live_not_a_cli_command() -> None:
    result = dispatch(_FakeApi(), "cli-default", "/live on")
    assert result.handled
    assert any("未知" in line or "unknown" in line.lower() for line in result.lines)


def test_tasks_lists_rows() -> None:
    api = _FakeApi()
    api.list_tasks = lambda: [  # type: ignore[method-assign]
        {
            "id": "t1",
            "status": "done",
            "target_url": "https://example.com",
            "finding_count": 3,
        }
    ]
    result = dispatch(api, "cli-default", "/tasks")
    assert result.handled
    joined = "\n".join(result.lines)
    assert "example.com" in joined
    assert "done" in joined


def test_print_reply_markdown_hides_raw_markers() -> None:
    from io import StringIO

    from rich.console import Console

    from argusai.cli.render import print_reply_markdown

    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=80, color_system=None)
    print_reply_markdown(console, "状态：**完成**\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    out = buf.getvalue()
    assert "**" not in out
    assert "|---|" not in out
    assert "完成" in out


def test_reply_stream_flush_on_non_tty() -> None:
    from io import StringIO

    from rich.console import Console

    from argusai.cli.render import ReplyStream

    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=80)
    stream = ReplyStream(console)
    stream.feed("hello **world**")
    assert stream.pending
    body = stream.flush()
    assert body == "hello **world**"
    assert not stream.pending
    assert "world" in buf.getvalue()


def test_tool_detail_from_block() -> None:
    from argusai.cli.render import tool_detail_from_block

    assert tool_detail_from_block(
        {"arguments": {"command": "ls -la /tmp"}}
    ).startswith("command=")
    assert tool_detail_from_block({"summary": "probe done"}) == "probe done"


def test_ops_app_live_reply_updates_in_place() -> None:
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text

    from argusai.cli.ops_app import OpsApp

    app = OpsApp(on_line=lambda _line: None)
    app.append_user_message("请检查这个目标")
    user_text = "".join(
        fragment[1] for fragment in to_formatted_text(ANSI(app._log[-1]))  # noqa: SLF001
    )
    assert "请检查这个目标" in user_text
    assert "│" in user_text
    assert "YOU" in user_text or "你" in user_text

    app.feed_reply("hello **world**")
    assert app.pending_reply
    live_text = "".join(
        fragment[1]
        for fragment in to_formatted_text(ANSI(app._transcript_blob(80)))  # noqa: SLF001
    )
    assert "ARGUSAI" in live_text
    assert "│" in live_text
    assert "world" in live_text
    # Incomplete Markdown stays literal until commit (Codex/Aider live tail).
    assert "**world**" in live_text
    rendered = app._render_center()  # noqa: SLF001
    assert rendered is not None
    app.flush_reply()
    assert not app.pending_reply
    assert app._log  # noqa: SLF001 — committed into transcript
    reply_text = "".join(
        fragment[1] for fragment in to_formatted_text(ANSI(app._log[-1]))  # noqa: SLF001
    )
    assert "ARGUSAI" in reply_text
    assert "world" in reply_text
    assert "│" in reply_text
    assert "**" not in reply_text


def test_ops_app_freezes_complete_live_lines() -> None:
    from argusai.cli.ops_app import OpsApp

    app = OpsApp(on_line=lambda _line: None)
    app.feed_reply("stable line\n")
    first = app._live_transcript_lines(80, app._live_md)  # noqa: SLF001
    cached = app._live_stable_lines  # noqa: SLF001
    assert cached is not None
    app.feed_reply("tail token")
    second = app._live_transcript_lines(80, app._live_md)  # noqa: SLF001
    assert app._live_stable_lines is cached  # noqa: SLF001
    plain = "\n".join("".join(part[1] for part in line) for line in second)
    assert "stable line" in plain
    assert "tail token" in plain
    assert first is not second

    app.feed_reply("\nsecond line\n")
    app._live_transcript_lines(80, app._live_md)  # noqa: SLF001
    grown = app._live_stable_lines  # noqa: SLF001
    assert grown is not None
    assert len(grown) >= len(cached)


def test_ops_app_backs_off_slow_paints() -> None:
    from argusai.cli.ops_app import OpsApp, _PAINT_INTERVAL_S

    app = OpsApp(on_line=lambda _line: None)
    app._note_paint_cost(0.1)  # noqa: SLF001
    assert app._paint_interval == 0.4  # noqa: SLF001
    app._note_paint_cost(0.001)  # noqa: SLF001
    assert app._paint_interval == _PAINT_INTERVAL_S  # noqa: SLF001


def test_ops_app_reuses_committed_transcript_lines() -> None:
    from argusai.cli.ops_app import OpsApp

    app = OpsApp(on_line=lambda _line: None)
    app.append_markup("alpha-line")
    app.append_markup("beta-line")
    first = app._committed_transcript_lines(80)  # noqa: SLF001
    second = app._committed_transcript_lines(80)  # noqa: SLF001
    assert first is second
    plain = "\n".join("".join(part[1] for part in line) for line in first)
    assert "alpha-line" in plain
    assert "beta-line" in plain
    app.append_markup("gamma-line")
    third = app._committed_transcript_lines(80)  # noqa: SLF001
    plain3 = "\n".join("".join(part[1] for part in line) for line in third)
    assert "gamma-line" in plain3


def test_ops_app_coalesces_live_invalidate() -> None:
    from argusai.cli.ops_app import OpsApp

    app = OpsApp(on_line=lambda _line: None)
    calls = {"n": 0}

    class _App:
        def invalidate(self) -> None:
            calls["n"] += 1

    app._app = _App()  # noqa: SLF001
    try:
        for _ in range(30):
            app.feed_reply("word ")
        assert calls["n"] == 1
        assert app._live_md.count("word") == 30  # noqa: SLF001
    finally:
        app._cancel_paint_timer()  # noqa: SLF001


def test_ops_app_skips_sys_sample_when_monitor_off() -> None:
    from argusai.cli.ops_app import OpsApp

    hits = {"n": 0}

    def metrics() -> dict[str, Any]:
        hits["n"] += 1
        return {"ok": True, "cpu_pct": 1.0}

    app = OpsApp(on_line=lambda _line: None, metrics_provider=metrics)
    app.refresh_chrome(force=True)
    assert hits["n"] == 0
    app.set_monitor_mode("sys")
    assert hits["n"] >= 1


def test_ops_app_accepts_input_while_busy() -> None:
    from argusai.cli.ops_app import OpsApp

    seen: list[str] = []
    app = OpsApp(on_line=seen.append)
    app.set_busy(True)
    class _Buf:
        text = "/btw 还在吗"
        def reset(self) -> None:
            self.text = ""

    # False → prompt_toolkit appends history then resets (do not reset early).
    assert app._accept(_Buf()) is False  # noqa: SLF001
    assert seen == ["/btw 还在吗"]


def test_ops_app_empty_enter_keeps_buffer_and_focus() -> None:
    """Blank Enter must not wipe history working-lines or call layout.focus."""
    from argusai.cli.ops_app import OpsApp

    seen: list[str] = []
    app = OpsApp(on_line=seen.append)
    app._focus = "main"  # noqa: SLF001
    focus_calls: list[str] = []

    class _FakeLayout:
        def focus(self, target) -> None:  # noqa: ANN001
            focus_calls.append("focus")

    class _FakeApp:
        layout = _FakeLayout()
        current_buffer = object()

    app._app = _FakeApp()  # noqa: SLF001
    app._input = type("Input", (), {"buffer": object()})()  # noqa: SLF001

    class _Buf:
        text = "   "
        reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1
            self.text = ""

    buf = _Buf()
    assert app._accept(buf) is True  # noqa: SLF001
    assert seen == []
    assert buf.reset_calls == 0
    assert buf.text == "   "
    assert app._focus == "input"  # noqa: SLF001
    assert focus_calls == []


def test_ops_app_panes_never_steal_real_focus() -> None:
    """MAIN/LEFT/RIGHT must not be PT-focusable (Windows echo ghost cursor)."""
    from argusai.cli.ops_app import OpsApp, _TranscriptControl

    app = OpsApp(on_line=lambda _line: None)
    assert _TranscriptControl(app).is_focusable() is False

    focused: list[str] = []

    class _FakeLayout:
        def focus(self, target) -> None:  # noqa: ANN001
            focused.append(type(target).__name__)

    class _FakeApp:
        layout = _FakeLayout()
        current_buffer = object()

    app._app = _FakeApp()  # noqa: SLF001
    app._input = type("Input", (), {"buffer": object()})()  # noqa: SLF001
    app._focus = "main"  # noqa: SLF001
    app._apply_layout_focus()  # noqa: SLF001
    # Even with logical MAIN, real focus is forced onto the input widget.
    assert focused == ["Input"]


def test_monitor_command_sets_mode() -> None:
    from argusai.cli.locale import set_locale

    set_locale("zh")
    result = dispatch(_FakeApi(), "cli-default", "/monitor both")
    assert result.handled
    assert result.monitor_mode == "both"
    assert any("both" in line for line in result.lines)
    tokens = dispatch(_FakeApi(), "cli-default", "/monitor tokens")
    assert tokens.handled
    assert tokens.monitor_mode == "tokens"


def test_sidebar_command_sets_ops_layout_mode() -> None:
    from argusai.cli.commands.chat import _try_shell
    from argusai.cli.ops_app import OpsApp

    result = dispatch(_FakeApi(), "cli-default", "/sidebar off")
    assert result.handled
    assert result.sidebar_mode == "off"

    app = OpsApp(on_line=lambda _line: None)
    handled = _try_shell(
        _FakeApi(),
        ["cli-default"],
        "/sidebar on",
        version="test",
        json_out=False,
        ops=app,
    )
    assert handled is True
    assert app.sidebar_mode == "on"


def test_ops_sidebar_modes_prioritize_available_width(monkeypatch) -> None:
    from argusai.cli import ops_app as ops_mod
    from argusai.cli.ops_app import OpsApp

    width = [100]
    monkeypatch.setattr(ops_mod, "_term_cols", lambda: width[0])
    app = OpsApp(on_line=lambda _line: None)
    app.set_monitor_mode("both")

    assert app.sidebar_mode == "off"
    assert app._layout_show_left() is False  # noqa: SLF001
    assert app._layout_show_right() is True  # noqa: SLF001

    assert app.set_sidebar_mode("auto") == "auto"
    assert app._layout_show_left() is False  # noqa: SLF001
    assert app._layout_show_right() is True  # noqa: SLF001

    assert app.set_sidebar_mode("on") == "on"
    assert app._layout_show_left() is True  # noqa: SLF001
    assert app._layout_show_right() is False  # noqa: SLF001

    width[0] = 130
    assert app._layout_show_left() is True  # noqa: SLF001
    assert app._layout_show_right() is True  # noqa: SLF001

    assert app.set_sidebar_mode("off") == "off"
    assert app._layout_show_left() is False  # noqa: SLF001
    assert app._layout_show_right() is True  # noqa: SLF001


def test_ops_app_sticky_bottom_and_page_scroll() -> None:
    from argusai.cli.ops_app import OpsApp

    def _plain_lines(lines: list) -> str:
        return "\n".join("".join(part[1] for part in line) for line in lines)

    app = OpsApp(on_line=lambda _line: None)
    for i in range(40):
        app.append_markup(f"line-{i:02d}")
    app._viewport_width = 80  # noqa: SLF001
    app._viewport_height = 6  # noqa: SLF001

    bottom = _plain_lines(app._visible_transcript_lines(80, 6))  # noqa: SLF001
    assert "line-39" in bottom
    assert "line-00" not in bottom

    app._scroll_transcript(20)  # noqa: SLF001
    older = _plain_lines(app._visible_transcript_lines(80, 6))  # noqa: SLF001
    assert "line-00" in older or "▲" in older
    assert app._scroll > 0  # noqa: SLF001

    app._scroll_transcript(-10_000)  # noqa: SLF001
    assert app._scroll == 0  # noqa: SLF001
    assert "line-39" in _plain_lines(app._visible_transcript_lines(80, 6))  # noqa: SLF001


def test_ops_app_main_focus_line_scroll() -> None:
    from argusai.cli.ops_app import OpsApp

    app = OpsApp(on_line=lambda _line: None)
    for i in range(40):
        app.append_markup(f"line-{i:02d}")
    app._viewport_width = 80  # noqa: SLF001
    app._viewport_height = 6  # noqa: SLF001
    assert "main" in app._focus_ring()  # noqa: SLF001

    app._focus = "main"  # noqa: SLF001
    before = app._scroll  # noqa: SLF001
    app._scroll_transcript(1)  # noqa: SLF001
    assert app._scroll == before + 1  # noqa: SLF001
    app._scroll_transcript(-1)  # noqa: SLF001
    assert app._scroll == before  # noqa: SLF001

    # Jump to oldest / newest
    app._scroll_transcript(10_000)  # noqa: SLF001
    assert app._scroll > 0  # noqa: SLF001
    app._scroll_transcript(-10_000)  # noqa: SLF001
    assert app._scroll == 0  # noqa: SLF001


def test_mouse_scroll_delta_parses_sgr_and_win32() -> None:
    from argusai.cli.ops_app import _mouse_scroll_delta

    assert _mouse_scroll_delta("\x1b[<64;10;12M") > 0
    assert _mouse_scroll_delta("\x1b[<65;10;12M") < 0
    assert _mouse_scroll_delta("\x1b[<68;10;12M") > 0  # shift+wheel
    assert _mouse_scroll_delta("NONE;SCROLL_UP;1;1") > 0
    assert _mouse_scroll_delta("NONE;SCROLL_DOWN;1;1") < 0
    assert _mouse_scroll_delta("\x1b[<0;10;12M") == 0


def test_ops_output_enables_alt_scroll_without_motion() -> None:
    from argusai.cli.ops_app import _OpsOutput

    class _Fake:
        def __init__(self) -> None:
            self.raw: list[str] = []

        def write_raw(self, text: str) -> None:
            self.raw.append(text)

        def flush(self) -> None:
            return None

        def enable_mouse_support(self) -> None:
            self.raw.append("INNER")

        def enter_alternate_screen(self) -> None:
            self.raw.append("ALT")

    fake = _Fake()
    out = _OpsOutput(fake)
    out.enable_mouse_support()
    blob = "".join(fake.raw)
    assert "1000h" in blob
    assert "1006h" in blob
    assert "1007h" in blob
    assert "1003h" not in blob
    fake.raw.clear()
    out.enter_alternate_screen()
    blob = "".join(fake.raw)
    assert "ALT" in blob
    assert "\x1b[3J" in blob


def test_ops_app_wheel_scrolls_main_transcript() -> None:
    from argusai.cli.ops_app import OpsApp, _mouse_scroll_delta

    app = OpsApp(on_line=lambda _line: None)
    for idx in range(40):
        app.append_markup(f"line-{idx}")
    app._viewport_width = 80  # noqa: SLF001
    app._viewport_height = 8  # noqa: SLF001
    before = app._scroll  # noqa: SLF001
    app._scroll_transcript(_mouse_scroll_delta("\x1b[<64;1;1M"))  # noqa: SLF001
    assert app._scroll > before  # noqa: SLF001


def test_activity_verb_maps_model_wait() -> None:
    from argusai.cli.ops_app import _activity_verb, _format_elapsed

    verb = _activity_verb("正在请求模型…", busy=True)
    assert verb in {"请求模型", "waiting on model"}
    assert _activity_verb("linking context", busy=True) in {
        "整理上下文",
        "linking context",
    }
    assert "ls" in _activity_verb("tool · ls", busy=True)
    assert _format_elapsed(2.4) == "2.4s"
    assert _format_elapsed(18.0) == "18s"


def test_ops_toolbar_shows_spinner_instead_of_raw_stage() -> None:
    from prompt_toolkit.formatted_text import to_formatted_text

    from argusai.cli.ops_app import OpsApp, _SPINNER_FRAMES

    app = OpsApp(on_line=lambda _line: None, toolbar_provider=lambda: "sess · zh")
    app.set_busy(True)
    app.set_status("正在请求模型…")
    app._status_started_at = time.monotonic() - 2.4  # noqa: SLF001
    fragments = list(to_formatted_text(app._toolbar_text()))  # noqa: SLF001
    text = "".join(part[1] for part in fragments)
    styles = [part[0] for part in fragments]
    assert "class:activity" in styles
    assert any(char in text for char in _SPINNER_FRAMES)
    assert "正在请求模型" not in text
    assert "请求模型" in text or "waiting on model" in text
    assert "2.4s" in text
    assert "/btw" in text
    assert "运行中 · 可用" not in text


def test_ops_app_choice_can_tab_to_main_and_submit_with_arrows() -> None:
    from argusai.cli.ops_app import OpsApp, run_ops_worker

    app = OpsApp(on_line=lambda _line: None)
    future = run_ops_worker(
        lambda: app.ask_choice(["第一项", "第二项", "第三项"], "你的选择 › ")
    )
    deadline = time.monotonic() + 1.0
    while not app._choice_options and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.01)

    assert app._focus == "input"  # noqa: SLF001
    app._toggle_choice_focus()  # noqa: SLF001
    assert app._focus == "main"  # noqa: SLF001
    app._move_choice(1)  # noqa: SLF001
    assert "第二项" in app._transcript_blob(80)  # noqa: SLF001
    app._submit_choice()  # noqa: SLF001

    assert future.result(timeout=1.0) == "第二项"
    assert app._focus == "input"  # noqa: SLF001
    assert app._choice_options == []  # noqa: SLF001


def test_ops_app_newest_reply_not_clipped() -> None:
    """Viewport must keep the newest lines when content overflows."""
    from argusai.cli.ops_app import OpsApp, _TranscriptControl

    app = OpsApp(on_line=lambda _line: None)
    for i in range(30):
        app.append_markup(f"hist-{i:02d}")
    app.append_markup("▸ 回复")
    app.append_markup("这是最新回复正文，不应被裁切")

    control = _TranscriptControl(app)
    content = control.create_content(width=60, height=5)
    shown = "\n".join(
        "".join(part[1] for part in content.get_line(i)) for i in range(content.line_count)
    )
    assert "最新回复正文" in shown
    assert "▸ 回复" in shown or "回复" in shown


def test_ops_app_left_focus_still_submits_text() -> None:
    from argusai.cli.ops_app import OpsApp

    seen: list[str] = []
    app = OpsApp(on_line=seen.append)
    app._focus = "left"  # noqa: SLF001

    class _Buf:
        text = "你好世界"

        def reset(self) -> None:
            self.text = ""

    assert app._accept(_Buf()) is False  # noqa: SLF001
    assert seen == ["你好世界"]
    assert app._focus == "input"  # noqa: SLF001


def test_ops_app_echoes_user_message_before_handler() -> None:
    from prompt_toolkit.formatted_text import ANSI, to_formatted_text

    from argusai.cli.ops_app import OpsApp

    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    seen: list[str] = []

    class _Loop:
        def call_soon(self, fn: Any, *args: Any) -> None:
            scheduled.append((fn, args))

    class _App:
        loop = _Loop()

        def invalidate(self) -> None:
            return None

    app = OpsApp(on_line=seen.append)
    app._app = _App()  # noqa: SLF001

    class _Buf:
        text = "我们上次说到哪了"

        def reset(self) -> None:
            self.text = ""

    assert app._accept(_Buf()) is False  # noqa: SLF001
    assert seen == []
    live_text = "".join(
        fragment[1] for fragment in to_formatted_text(ANSI(app._transcript_blob(80)))  # noqa: SLF001
    )
    assert "我们上次说到哪了" in live_text
    fn, args = scheduled[0]
    fn(*args)
    assert seen == ["我们上次说到哪了"]
    assert app.take_pending_echo("我们上次说到哪了") is False


def test_ops_waiting_keeps_user_message_visible() -> None:
    from argusai.cli.ops_app import OpsApp, _SPINNER_FRAMES

    app = OpsApp(on_line=lambda _line: None)
    app.append_user_message("我们上次说到哪了")
    app.set_busy(True)
    app.set_status("正在请求模型…")
    plain = "\n".join(
        "".join(part[1] for part in line)
        for line in app._transcript_lines(80)  # noqa: SLF001
    )
    assert "我们上次说到哪了" in plain
    assert "请求模型" in plain or "waiting on model" in plain
    assert any(char in plain for char in _SPINNER_FRAMES)


def test_ops_app_monitor_and_tab_focus() -> None:
    from argusai.cli.ops_app import OpsApp

    allowed: list[str] = []
    app = OpsApp(
        on_line=lambda _line: None,
        on_allow=allowed.append,
        session_provider=lambda: {
            "ok": True,
            "todos": [{"id": "t1", "title": "一步", "status": "in_progress"}],
            "message_count": 3,
            "pending_tools": [{"id": "c1", "name": "run_shell"}],
        },
        jobs_provider=lambda: [
            {"job_id": "job-aaa", "state": "waiting_user", "status": "need confirm"}
        ],
    )
    assert app.set_monitor_mode("both") == "both"
    app.refresh_chrome(force=True)
    left = str(app._render_left())  # noqa: SLF001
    assert "TODO" in left or "一步" in left
    right = str(app._render_right())  # noqa: SLF001
    assert "AUTH" in right or "waiting_user" in right
    app._focus = "right"  # noqa: SLF001
    app._auth_action("allow")  # noqa: SLF001
    assert allowed == ["1"]


def test_sample_host_metrics_safe() -> None:
    from argusai.cli.metrics import (
        format_metrics_lines,
        format_token_usage_lines,
        meter_bar,
        sample_host_metrics,
        sparkline,
    )

    sample = sample_host_metrics()
    assert sample.get("ok") is True
    # Second sample so CPU delta can resolve on platforms that support it.
    sample = sample_host_metrics()
    lines = format_metrics_lines(sample)
    joined = "\n".join(lines)
    assert "CPU" in joined
    assert "MEM" in joined
    assert "DSK" in joined
    assert "APP" in joined
    assert "HOST" in joined
    assert any("█" in line or "░" in line for line in lines)
    assert len(meter_bar(0.5, width=10)) == 10
    assert len(sparkline([10, 40, 80], width=8)) == 8
    token_lines = format_token_usage_lines(
        {
            "token_usage": {
                "input_tokens": 120_000,
                "output_tokens": 4_200,
                "cache_hit_tokens": 90_000,
                "cache_miss_tokens": 30_000,
                "calls": 8,
            },
            "cache_hit_rate": 75.0,
        }
    )
    token_text = "\n".join(token_lines)
    assert "120.0k" in token_text
    assert "75.0%" in token_text
    assert token_lines[-1].startswith("CALLS")
    assert token_lines[-1].rstrip().endswith("8")
    assert len({len(line) for line in token_lines}) == 1
    ctx_lines = format_token_usage_lines(
        {
            "prompt_tokens": 123_400,
            "context_window": 256_000,
            "token_usage": {
                "input_tokens": 120_000,
                "output_tokens": 4_200,
                "cache_hit_tokens": 90_000,
                "cache_miss_tokens": 30_000,
                "calls": 8,
            },
            "cache_hit_rate": 75.0,
        }
    )
    assert ctx_lines[0].startswith("CTX")
    assert "48%" in ctx_lines[0]
    assert ctx_lines[-1].startswith("CALLS")
    assert len({len(line) for line in ctx_lines}) == 1


def test_ops_monitor_renders_live_token_usage() -> None:
    from argusai.cli.ops_app import OpsApp

    app = OpsApp(
        on_line=lambda _line: None,
        token_usage_provider=lambda: {
            "ok": True,
            "token_usage": {
                "input_tokens": 32_000,
                "output_tokens": 1_200,
                "cache_hit_tokens": 24_000,
                "cache_miss_tokens": 8_000,
                "calls": 3,
            },
            "cache_hit_rate": 75.0,
        },
    )
    app.set_monitor_mode("tokens")
    app.refresh_chrome(force=True)

    rendered = str(app._render_right())  # noqa: SLF001
    assert "Token" in rendered or "TOKENS" in rendered
    assert "75.0%" in rendered
    assert "class:monitor-cyan" in rendered
    assert "class:monitor-ok" in rendered


def test_jobs_command() -> None:
    api = _FakeApi()

    def _jobs(session_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "jobs": [
                {
                    "job_id": "abc123",
                    "state": "waiting_user",
                    "status": "confirm",
                    "updated_at": 1,
                }
            ],
        }

    api.list_assistant_run_jobs = _jobs  # type: ignore[method-assign]
    result = dispatch(api, "cli-default", "/jobs")
    assert result.handled
    joined = "\n".join(result.lines)
    assert "abc123" in joined
    assert "waiting_user" in joined


def test_greenlight_localized() -> None:
    from argusai.cli.locale import set_locale

    api = _FakeApi()
    set_locale("zh")
    result = dispatch(api, "cli-default", "/greenlight on")
    assert result.handled
    joined = "\n".join(result.lines)
    assert "绿灯" in joined or "greenlight" in joined.lower()


def test_greenlight_with_goal_enables_then_forwards_objective() -> None:
    api = _FakeApi()
    calls: list[tuple[str, dict[str, Any]]] = []

    def _set_engagement(session_id: str, declaration: dict[str, Any]) -> dict[str, Any]:
        calls.append((session_id, declaration))
        return {"ok": True, "engagement_declaration": declaration}

    api.set_assistant_engagement = _set_engagement  # type: ignore[method-assign]
    result = dispatch(api, "cli-default", "/greenlight on 检查本地测试站")

    assert result.handled
    assert result.forward_text == "检查本地测试站"
    assert calls == [
        (
            "cli-default",
            {
                "active": True,
                "mode": "greenlight",
                "goal_text": "检查本地测试站",
            },
        )
    ]


def test_extract_waitables_prefers_decision() -> None:
    from argusai.cli.commands.chat import _extract_waitables

    waits = [
        {
            "status": "awaiting_user_decision",
            "blocks": [
                {
                    "type": "user_decision",
                    "question": "下一步？",
                    "options": ["逆向", "监控", "算了"],
                }
            ],
        }
    ]
    decisions, confirms = _extract_waitables(waits)
    assert len(decisions) == 1
    assert decisions[0]["options"][0] == "逆向"
    assert confirms == []


def test_extract_waitables_ignores_bare_waiting_status() -> None:
    """Bare waiting_user must not become a blind tool AUTH confirm."""
    from argusai.cli.commands.chat import _extract_waitables

    waits = [
        {"type": "status", "status": "awaiting_user_decision"},
        {"status": "waiting_user"},
        {"status": "awaiting_confirmation"},
    ]
    decisions, confirms = _extract_waitables(waits)
    assert decisions == []
    assert confirms == []


def test_hydrate_waits_from_session_history() -> None:
    from argusai.cli.commands.chat import _hydrate_waits_from_session

    api = SimpleNamespace(
        get_assistant_session=lambda _sid: {
            "ok": True,
            "pending_blocks": [],
            "pending_tools": [],
            "history": [
                {
                    "role": "assistant",
                    "blocks": [
                        {
                            "type": "user_decision",
                            "question": "选一个",
                            "options": ["A", "B", "C"],
                        }
                    ],
                }
            ],
        }
    )
    found = _hydrate_waits_from_session(api, "cli-default")
    assert len(found) == 1
    assert found[0]["type"] == "user_decision"
    assert found[0]["options"] == ["A", "B", "C"]


def test_run_turn_keeps_polling_after_decision_resume(monkeypatch) -> None:
    """Selecting a choice on a done stream must poll the resumed run_id."""
    from argusai.cli.commands import chat as chat_mod

    stream_calls: list[dict[str, Any]] = []
    poll_ids: list[str] = []

    class _Api:
        def start_assistant_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
            stream_calls.append(payload)
            if payload.get("message") == "ask":
                return {"ok": True, "run_id": "run-1"}
            return {"ok": True, "run_id": "run-2"}

        def get_assistant_stream_events(self, run_id: str, offset: int) -> dict[str, Any]:
            poll_ids.append(run_id)
            if run_id == "run-1":
                return {
                    "ok": True,
                    "next": 1,
                    "done": True,
                    "job": {"state": "waiting_user"},
                    "events": [
                        {
                            "type": "done",
                            "result": {
                                "status": "awaiting_user_decision",
                                "text": "pick",
                                "blocks": [
                                    {
                                        "type": "user_decision",
                                        "question": "选？",
                                        "options": ["甲", "乙", "丙"],
                                    }
                                ],
                            },
                        }
                    ],
                }
            return {
                "ok": True,
                "next": 1,
                "done": True,
                "events": [
                    {
                        "type": "done",
                        "result": {"status": "done", "text": "开工了"},
                    }
                ],
            }

        def get_assistant_session(self, _session_id: str) -> dict[str, Any]:
            return {"ok": True, "history": [], "pending_blocks": [], "pending_tools": []}

    monkeypatch.setattr(chat_mod, "ask_choice_line", lambda _p: "3")
    monkeypatch.setattr(
        chat_mod,
        "print_decision_panel",
        lambda block, console=None: list(block["options"]),
    )
    monkeypatch.setattr(chat_mod, "ops_rule", lambda: None)
    # Avoid Rich Status / console side effects in CI.
    fake_console = SimpleNamespace(is_terminal=False, print=lambda *_a, **_k: None)
    monkeypatch.setattr(chat_mod, "get_console", lambda: fake_console)

    out = chat_mod._run_turn(
        _Api(),
        session_id="cli-default",
        user_text="ask",
        yes=False,
        timeout=5.0,
        json_out=False,
        attachments=[{"type": "text", "name": "proof.txt", "text": "proof"}],
    )
    assert "run-2" in poll_ids
    assert out == "开工了"
    assert stream_calls[0]["attachments"][0]["name"] == "proof.txt"
    assert stream_calls[-1]["message"] == "丙"
    assert stream_calls[-1].get("hide_user_message") is True


def _capture_ui_card(block: dict[str, Any]) -> str:
    from io import StringIO

    from rich.console import Console

    from argusai.cli.cards import print_ui_card
    from argusai.cli.ui import ARGUS_THEME

    console = Console(
        file=StringIO(),
        force_terminal=True,
        width=88,
        theme=ARGUS_THEME,
        highlight=False,
        color_system="truecolor",
    )
    drawn = print_ui_card(block, console=console)
    buf = console.file
    return ("drawn" if drawn else "") + "\n" + buf.getvalue()  # type: ignore[union-attr]


def test_print_ui_card_renders_data_table_with_headers_and_rows() -> None:
    output = _capture_ui_card(
        {
            "type": "data_table",
            "id": "blk_1",
            "title": "全量能力清单",
            "columns": [
                {"key": "domain", "label": "域"},
                {"key": "ability", "label": "能干的具体事"},
            ],
            "rows": [
                {"id": "rev", "cells": {"domain": "逆向", "ability": "APK → 源码"}},
                {"id": "net", "cells": {"domain": "网络", "ability": "TLS 探测"}},
            ],
        }
    )
    assert output.startswith("drawn")
    assert "全量能力清单" in output
    assert "域" in output and "能干的具体事" in output
    assert "逆向" in output and "TLS 探测" in output


def test_print_ui_card_renders_structured_card_sections() -> None:
    output = _capture_ui_card(
        {
            "type": "structured_card",
            "id": "blk_2",
            "title": "能力一览",
            "subtitle": "分组说明",
            "sections": [
                {
                    "type": "kv",
                    "title": "配置",
                    "items": [{"key": "runtime", "value": "cli", "note": "beta"}],
                },
                {
                    "type": "list",
                    "title": "要点",
                    "items": [{"label": "只跑授权内目标"}, "被动为主"],
                },
                {
                    "type": "steps",
                    "items": [
                        {"title": "抓取", "body": "curl 探测", "status": "done"}
                    ],
                },
            ],
        }
    )
    assert output.startswith("drawn")
    assert "能力一览" in output
    assert "配置" in output and "runtime" in output and "beta" in output
    assert "只跑授权内目标" in output and "被动为主" in output
    assert "1. 抓取" in output and "done" in output


def test_print_ui_card_renders_todo_list_with_status_markers() -> None:
    output = _capture_ui_card(
        {
            "type": "todo_list",
            "id": "blk_3",
            "title": "任务",
            "todos": [
                {"id": "a", "title": "已完成", "status": "completed"},
                {"id": "b", "title": "进行中", "status": "in_progress"},
                {"id": "c", "title": "待做", "status": "pending"},
            ],
        }
    )
    assert output.startswith("drawn")
    assert "[x] 已完成" in output
    assert "[~] 进行中" in output
    assert "[ ] 待做" in output


def test_print_ui_card_renders_command_result_with_excerpts() -> None:
    output = _capture_ui_card(
        {
            "type": "command_result",
            "id": "blk_4",
            "title": "运行结果",
            "command": "curl",
            "args": ["-s", "https://example.com"],
            "exit_code": 0,
            "stdout_excerpt": "hello",
            "stderr_excerpt": "warn: slow",
        }
    )
    assert output.startswith("drawn")
    assert "curl" in output
    assert "hello" in output
    assert "warn: slow" in output


def test_print_ui_card_skips_interactive_and_tool_blocks() -> None:
    for skipped in (
        {"type": "user_decision", "id": "u1", "options": ["a", "b"]},
        {"type": "tool_trace", "id": "t1", "name": "curl"},
        {"type": "command_plan", "id": "p1", "title": "plan"},
    ):
        output = _capture_ui_card(skipped)
        assert not output.startswith("drawn"), skipped["type"]


def test_run_turn_paints_data_table_from_stream_only_once(monkeypatch) -> None:
    """Card streamed via block_upsert should render, and dedup on final replay."""
    from argusai.cli.commands import chat as chat_mod

    table_block = {
        "type": "data_table",
        "id": "blk_stream",
        "title": "命中列表",
        "columns": [{"key": "id", "label": "ID"}],
        "rows": [{"id": "1", "cells": {"id": "row-1"}}],
    }

    class _Api:
        def start_assistant_stream(self, _payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "run_id": "run-1"}

        def get_assistant_stream_events(
            self, _run_id: str, _offset: int
        ) -> dict[str, Any]:
            return {
                "ok": True,
                "next": 2,
                "done": True,
                "events": [
                    {"type": "block_upsert", "block": table_block},
                    {
                        "type": "done",
                        "result": {
                            "status": "done",
                            "text": "看这里",
                            "blocks": [table_block],
                        },
                    },
                ],
            }

        def get_assistant_session(self, _session_id: str) -> dict[str, Any]:
            return {"ok": True, "history": [], "pending_blocks": [], "pending_tools": []}

    monkeypatch.setattr(chat_mod, "ops_rule", lambda: None)
    fake_console = SimpleNamespace(is_terminal=False, print=lambda *_a, **_k: None)
    monkeypatch.setattr(chat_mod, "get_console", lambda: fake_console)

    painted: list[dict[str, Any]] = []

    def _fake_print_card(block: dict[str, Any], console: Any | None = None) -> bool:
        painted.append(block)
        return True

    monkeypatch.setattr(chat_mod, "print_ui_card", _fake_print_card)

    out = chat_mod._run_turn(
        _Api(),
        session_id="cli-default",
        user_text="show",
        yes=False,
        timeout=5.0,
        json_out=False,
    )
    assert out == "看这里"
    assert len(painted) == 1
    assert painted[0]["id"] == "blk_stream"


def test_run_turn_does_not_replay_tool_returns_under_the_reply(monkeypatch) -> None:
    """Final ``done.blocks`` include tool payloads; CLI must not dump them after the answer."""
    from argusai.cli.commands import chat as chat_mod

    table_block = {
        "type": "data_table",
        "id": "blk_keep",
        "title": "命中列表",
        "columns": [{"key": "id", "label": "ID"}],
        "rows": [{"id": "1", "cells": {"id": "row-1"}}],
    }
    tool_returns = [
        {
            "type": "sandbox_file",
            "title": "sandbox_read_file",
            "relative_path": "out.txt",
            "preview": "tool stdout that should not land under the reply",
        },
        {
            "type": "command_result",
            "title": "http_curl",
            "command": "curl",
            "stdout_excerpt": "HTTP/1.1 200",
        },
        {
            "type": "probe_result",
            "title": "http_probe",
            "url": "https://example.com",
            "status": 200,
        },
    ]

    class _Api:
        def start_assistant_stream(self, _payload: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "run_id": "run-1"}

        def get_assistant_stream_events(
            self, _run_id: str, _offset: int
        ) -> dict[str, Any]:
            return {
                "ok": True,
                "next": 2,
                "done": True,
                "events": [
                    {"type": "delta", "text": "结论如下"},
                    {
                        "type": "done",
                        "result": {
                            "status": "done",
                            "text": "结论如下",
                            "blocks": [*tool_returns, table_block],
                        },
                    },
                ],
            }

        def get_assistant_session(self, _session_id: str) -> dict[str, Any]:
            return {"ok": True, "history": [], "pending_blocks": [], "pending_tools": []}

    monkeypatch.setattr(chat_mod, "ops_rule", lambda: None)
    fake_console = SimpleNamespace(is_terminal=False, print=lambda *_a, **_k: None)
    monkeypatch.setattr(chat_mod, "get_console", lambda: fake_console)

    painted: list[str] = []

    def _fake_print_card(block: dict[str, Any], console: Any | None = None) -> bool:
        painted.append(str(block.get("type") or ""))
        return True

    monkeypatch.setattr(chat_mod, "print_ui_card", _fake_print_card)

    out = chat_mod._run_turn(
        _Api(),
        session_id="cli-default",
        user_text="show",
        yes=False,
        timeout=5.0,
        json_out=False,
    )
    assert out == "结论如下"
    assert painted == ["data_table"]
