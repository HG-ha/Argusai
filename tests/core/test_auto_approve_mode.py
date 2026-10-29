"""Conversation-scoped assistant auto-approve: tools, decisions, and todo plans."""

from __future__ import annotations

import json
import pytest

from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import build_tool_registry
from argusai.core.settings import _parse


def test_settings_parses_assistant_auto_approve() -> None:
    off = _parse({"assistant": {}})
    assert off.assistant_auto_approve is False
    on = _parse({"assistant": {"auto_approve": True}})
    assert on.assistant_auto_approve is True
    assert on.to_dict()["assistant"]["auto_approve"] is True


def test_session_roundtrip_preserves_auto_approve_mode() -> None:
    session = AssistantSession()
    session.apply_user_auto_approve_mode(True)
    dumped = session.to_dict()
    assert dumped["auto_approve_mode"] is True
    restored = AssistantSession.from_dict(dumped, session.registry)
    assert restored.user_auto_approve_mode is True


def test_user_auto_approve_mode_lets_agent_decide() -> None:
    session = AssistantSession()
    blocked = session.stop_for_user_decision(
        "下一步做哪个？",
        options=["先验证", "先打包"],
        recommended="先打包",
    )
    assert blocked["stopped"] is True
    assert session._user_decision_requested is True

    session.apply_user_auto_approve_mode(True)
    session._user_decision_requested = False
    picked = session.stop_for_user_decision(
        "下一步做哪个？",
        options=["先验证", "先打包"],
        recommended="先打包",
    )
    assert picked["stopped"] is False
    assert picked["auto_continued"] is True
    assert picked["agent_must_choose"] is True
    assert not picked.get("chosen")
    assert session._user_decision_requested is False

    first = session.stop_for_user_decision("继续？", options=["是", "否"])
    assert first["agent_must_choose"] is True
    assert not first.get("chosen")

    batch = session.stop_for_user_decision_batch(
        "确认",
        questions=[
            {"id": "q1", "question": "平台？", "options": ["Android", "Windows"], "recommended": "Windows"},
        ],
    )
    assert batch["stopped"] is False
    assert batch["agent_must_choose"] is True
    assert batch["answers"] == {}


def test_user_auto_approve_mode_skips_tool_confirmation() -> None:
    session = AssistantSession(registry=build_tool_registry(None))
    tool_call = {
        "id": "1",
        "type": "function",
        "function": {
            "name": "run_shell_snippet",
            "arguments": json.dumps({"command": "echo hi"}),
        },
    }
    assert session._confirmation_requirements([tool_call])
    session.apply_user_auto_approve_mode(True)
    assert session._tool_auto_approved("run_shell_snippet") is True
    assert session._confirmation_requirements([tool_call]) == []
    assert session._should_stop_before_tool_calls({
        "content": "要现在开始吗？",
        "tool_calls": [tool_call],
    }) is False


def test_turning_off_auto_approve_mode_restores_decision_stop() -> None:
    session = AssistantSession()
    session.apply_user_auto_approve_mode(True)
    session.apply_user_auto_approve_mode(False)
    out = session.stop_for_user_decision("现在开始吗？", options=["开始"])
    assert out["stopped"] is True
    assert session.user_auto_approve_mode is False
    assert session._user_decision_requested is True


def test_auto_continued_decision_does_not_render_pending_card() -> None:
    session = AssistantSession()
    session.apply_user_auto_approve_mode(True)
    picked = session.stop_for_user_decision(
        "签名口令要换吗？",
        options=["不换", "生成新密钥"],
        recommended="不换",
    )
    assert picked["auto_continued"] is True
    blocks = session._blocks_for_tool_trace([
        session._trace("stop_for_user_decision", {}, picked),
    ])
    assert not any(block.get("type") == "user_decision" for block in blocks)

    batch = session.stop_for_user_decision_batch(
        "确认",
        questions=[{"id": "q1", "question": "继续？", "options": ["是", "否"]}],
    )
    batch_blocks = session._blocks_for_tool_trace([
        session._trace("stop_for_user_decision_batch", {}, batch),
    ])
    assert not any(block.get("type") == "user_decision_batch" for block in batch_blocks)


def test_run_start_inherits_settings_auto_approve() -> None:
    from types import SimpleNamespace

    from argusai.gui.assistant_sessions import AssistantSessionsMixin

    class _Api(AssistantSessionsMixin):
        def __init__(self) -> None:
            self.settings_mgr = SimpleNamespace(
                get=lambda: SimpleNamespace(
                    assistant_auto_approve=True,
                    assistant_tool_always_allow=[],
                    assistant_force_always_allow_desktop_os=False,
                ),
            )

    session = AssistantSession()
    assert session.user_auto_approve_mode is False
    _Api()._apply_session_auto_approve_policy(session)
    assert session.user_auto_approve_mode is True
    picked = session.stop_for_user_decision("继续？", options=["是", "否"])
    assert picked["auto_continued"] is True
    assert picked["agent_must_choose"] is True
    assert not picked.get("chosen")


def test_cron_and_child_still_host_pick_decisions() -> None:
    cron = AssistantSession()
    cron._current_turn_source = "cron"
    cron.apply_user_auto_approve_mode(True)
    picked = cron.stop_for_user_decision("范围？", options=["当前", "全部"], recommended="全部")
    assert picked["chosen"] == "全部"
    assert picked.get("agent_must_choose") is not True

    child = AssistantSession()
    child._subagent_depth = 1
    child.apply_user_auto_approve_mode(True)
    child_pick = child.stop_for_user_decision("开始吗？", options=["开始", "取消"])
    assert child_pick["chosen"] == "开始"
    assert child_pick.get("agent_must_choose") is not True


def test_user_auto_approve_mode_auto_approves_todo_plan() -> None:
    session = AssistantSession()
    session.apply_user_auto_approve_mode(True)
    result = session.set_todos([{"id": "step.one", "title": "执行已选方案"}])
    assert result.get("plan_confirmation") is None
    assert result["plan_approved"] is True
    assert session._todos_plan_approved is True
    assert session._user_decision_requested is False


def test_global_default_only_seeds_a_conversation_once() -> None:
    from types import SimpleNamespace
    from argusai.gui.assistant_sessions import AssistantSessionsMixin

    settings = SimpleNamespace(assistant_auto_approve=True, assistant_tool_always_allow=[])
    host = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))
    session = AssistantSession()
    AssistantSessionsMixin._apply_session_auto_approve_policy(host, session)
    assert session.user_auto_approve_mode
    session.apply_user_auto_approve_mode(False)
    AssistantSessionsMixin._apply_session_auto_approve_policy(host, session)
    assert not session.user_auto_approve_mode
    restored = AssistantSession.from_dict(session.to_dict(), session.registry)
    AssistantSessionsMixin._apply_session_auto_approve_policy(host, restored)
    assert not restored.user_auto_approve_mode
    fresh = AssistantSession()
    AssistantSessionsMixin._apply_session_auto_approve_policy(host, fresh)
    assert fresh.user_auto_approve_mode


def test_composer_toggle_never_changes_other_sessions_or_global_default(tmp_path) -> None:
    from types import SimpleNamespace
    from argusai.gui.assistant_tools import AssistantToolsMixin

    selected = AssistantSession()
    other = AssistantSession()
    settings = SimpleNamespace(assistant_auto_approve=False, assistant_tool_always_allow=[])
    saved = []
    class Host(AssistantToolsMixin):
        settings_mgr = SimpleNamespace(get=lambda: settings, update=lambda fn: pytest.fail("global setting changed"))
        _assistant_sessions = {"selected": selected, "other": other}
        def _validate_session_id(self, value):
            return value
        def _ensure_session(self, value):
            return self._assistant_sessions[value]
        def _session_path(self, value):
            return tmp_path / (value + ".json")
        def _sync_child_auto_allow(self, session):
            return {}
        def _save_session(self, path, session, name):
            saved.append(session.to_dict())
    result = Host().set_assistant_auto_approve_mode("selected", True)
    assert result["ok"], result
    assert selected.user_auto_approve_mode
    assert not other.user_auto_approve_mode
    assert not settings.assistant_auto_approve
    assert saved[-1]["auto_approve_mode"] is True


def test_seeding_false_default_preserves_legacy_per_tool_grants() -> None:
    from types import SimpleNamespace
    from argusai.gui.assistant_sessions import AssistantSessionsMixin

    session = AssistantSession()
    session._auto_approve_tools = {"legacy-per-tool-grant"}
    host = SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: SimpleNamespace(
        assistant_auto_approve=False, assistant_tool_always_allow=[],
    )))
    AssistantSessionsMixin._apply_session_auto_approve_policy(host, session)
    assert not session.user_auto_approve_mode
    assert "legacy-per-tool-grant" in session.auto_approve_tools()
