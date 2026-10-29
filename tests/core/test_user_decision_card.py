"""Loom22 §9.6: decision cards carry a short question, not the essay."""

from __future__ import annotations

from argusai.core.assistant.session import AssistantSession


def test_extract_decision_card_question_keeps_trailing_ask() -> None:
    session = AssistantSession()
    essay = (
        "ArgusAI 没有内置邮件 MCP，需要你自己接入。常见类型有 mcp-email / Office365 / Gmail。\n"
        "安装流程：mcp_provision_plan → mcp_server_add → 验证连通。\n\n"
        "1. 你想用哪个邮箱服务（QQ / 163 / Gmail / Outlook）？\n"
        "2. 是否已有授权码或 OAuth 凭据？"
    )
    q = session._extract_decision_card_question(essay, max_chars=320)
    assert "mcp_provision_plan" not in q
    assert "哪个邮箱" in q or "授权码" in q
    assert len(q) < len(essay)


def test_implicit_user_question_stop_does_not_dump_essay_into_card() -> None:
    session = AssistantSession()
    essay = (
        "详细说明安装步骤和 MCP 类型选择，很长很长的背景文字。" * 8
        + "\n\n要现在开始吗？"
    )
    resp = session._commit_implicit_user_question_stop(essay)
    assert resp.status == "awaiting_user_decision"
    card = next(b for b in (resp.blocks or []) if b.get("type") == "user_decision")
    assert card["question"] == "要现在开始吗？"
    assert card["options"] == ["开始", "取消"]
    # Full essay remains in message content for the user to read once.
    assert "详细说明安装步骤" in (resp.text or "")
    assert "详细说明安装步骤" not in card["question"]


def test_idle_open_question_does_not_mint_start_cancel_card() -> None:
    """「你在干嘛」这类空闲回复不该配一张开始/取消卡。

    句尾问号会让同条消息里的工具调用停下来，但「需要接着做什么？」
    不是确认执行，按钮没有对应动作。
    """
    session = AssistantSession()
    text = "现在我没有在执行任何任务。你正在设置页，我这边空闲。需要接着做什么？"
    response = {
        "content": text,
        "tool_calls": [{"id": "1", "function": {"name": "get_ui_context"}}],
    }
    assert session._should_stop_before_tool_calls(response) is True
    assert session._warrants_proceed_decision_card(text) is False
    resp = session._finish_if_implicit_user_question(response, [])
    assert resp is not None
    assert resp.status == "done"
    assert "需要接着做什么" in (resp.text or "")
    assert not any(b.get("type") == "user_decision" for b in (resp.blocks or []))
    assert session._user_decision_requested is False


def test_proceed_question_still_mints_decision_card() -> None:
    session = AssistantSession()
    text = "扫描范围已经确认。要现在开始吗？"
    response = {
        "content": text,
        "tool_calls": [{"id": "1", "function": {"name": "run_shell_snippet"}}],
    }
    resp = session._finish_if_implicit_user_question(response, [])
    assert resp is not None
    assert resp.status == "awaiting_user_decision"
    card = next(b for b in (resp.blocks or []) if b.get("type") == "user_decision")
    assert "要现在开始吗" in card["question"]
    assert card["options"] == ["开始", "取消"]
    assert session._user_decision_requested is True


def test_stop_for_user_decision_clamps_long_question() -> None:
    session = AssistantSession()
    long_q = ("背景说明。" * 40) + "\n\n请选择邮箱服务商？"
    out = session.stop_for_user_decision(long_q, options=["QQ", "Gmail"], context="x" * 900)
    assert out["ok"] is True
    assert len(out["question"]) <= 320
    assert len(out["context"]) <= 480
    assert "请选择" in out["question"] or "邮箱" in out["question"]


def test_child_agent_auto_continues_user_decision_tools() -> None:
    session = AssistantSession()
    session._subagent_depth = 1
    out = session.stop_for_user_decision("现在开始吗？", options=["开始", "取消"])
    assert out["stopped"] is False
    assert out["auto_continued"] is True
    assert out["chosen"] == "开始"
    assert session._user_decision_requested is False

    batch = session.stop_for_user_decision_batch(
        "确认",
        questions=[{"id": "q1", "question": "继续？", "options": ["是", "否"]}],
    )
    assert batch["stopped"] is False
    assert batch["answers"]["q1"] == "是"
    assert session._should_stop_before_tool_calls({
        "content": "要现在开始吗？",
        "tool_calls": [{"id": "1", "function": {"name": "run_shell_snippet"}}],
    }) is False


def test_parent_user_decision_still_stops() -> None:
    session = AssistantSession()
    out = session.stop_for_user_decision("现在开始吗？", options=["开始"])
    assert out["stopped"] is True
    assert session._user_decision_requested is True


def test_commit_user_decision_stop_keeps_essay_after_marker() -> None:
    """The card stays short; the 10-title list (etc.) lives in message text.

    The UI must strip ``[等待用户决定]`` and still render that prose. Hiding
    the whole body is how a decision card referred to content the user
    never saw.
    """
    session = AssistantSession()
    tool_result = session.stop_for_user_decision(
        "这 10 个炸裂版标题的力度，你觉得 OK 吗？",
        options=["力度 OK，全改", "收一收再改", "先改 5 篇"],
        context="改标题会触发三处同步。",
    )
    essay = (
        "1. 标题A：踩进xx\n"
        "2. 标题B：拆穿yy\n"
        "3. 标题C：对死zz"
    )
    resp = session._commit_user_decision_stop(
        essay,
        [session._trace("stop_for_user_decision", {}, tool_result)],
        [],
        [],
    )
    assert resp.status == "awaiting_user_decision"
    assert (resp.text or "").startswith("[等待用户决定]")
    assert "标题A：踩进xx" in (resp.text or "")
    card = next(b for b in (resp.blocks or []) if b.get("type") == "user_decision")
    assert "踩进xx" not in (card.get("question") or "")
    assert "力度" in (card.get("question") or "")


def test_team_room_pass_ends_silently() -> None:
    session = AssistantSession()
    session._register_team_room_pass_tool()
    assert session.registry.get("team_room_pass") is not None
    out = session.team_room_pass("not my objective")
    assert out["silent"] is True
    assert session._team_room_passed is True
    resp = session._commit_team_room_silence([])
    assert resp.status == "done"
    assert resp.text == ""
    assert session.history[-1].ui_hidden is True
