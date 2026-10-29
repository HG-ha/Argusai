"""Defensive operations are an intent-triggered Skill, not session state."""

from __future__ import annotations

import importlib

import argusai.core.assistant.skills as skills_mod
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


def _reload() -> None:
    importlib.reload(skills_mod)


def test_proactive_defense_is_strong_intent_match() -> None:
    _reload()
    for message in (
        "帮我做业务安全检查和主动防御",
        "分析这些日志并做攻击溯源",
        "set up a security patrol and alert triage workflow",
    ):
        matched = skills_mod._select_matched_skills(message)
        defense = next(item for item in matched if item.name == "proactive-defense")
        assert skills_mod._is_strong_skill_match(defense, message)


def test_proactive_defense_attaches_bootstrap_tools() -> None:
    _reload()
    names = set(skills_mod._select_tool_names("assistant", "帮我做主动防御和安全巡检"))
    assert {
        "local_attack_surface_snapshot",
        "network_assess_exposure",
        "cron_upsert",
        "kb_save",
        "case_create",
        "artifact_add",
    } <= names


def test_proactive_defense_distinguishes_short_waits_from_cron() -> None:
    _reload()
    defense = next(
        item for item in skills_mod._SKILL_DOCS if item.name == "proactive-defense"
    )
    assert "wait_for_duration" in defense.body
    assert "do not default to Cron" in defense.body
    assert "Multiple scan-and-verify steps do not by themselves justify" in defense.body


def test_defense_tools_are_not_always_on() -> None:
    _reload()
    names = set(skills_mod._select_tool_names("assistant", "你好"))
    assert "local_attack_surface_snapshot" not in names
    assert "cron_upsert" not in names


def test_legacy_defense_declaration_is_ignored() -> None:
    raw = {
        "history": [],
        "defense_declaration": {"active": True, "assets": ["host-a"]},
    }
    session = AssistantSession.from_dict(raw, ToolRegistry(), None)
    assert "defense_declaration" not in session.to_dict()
    assert not hasattr(session, "defense_declaration")
