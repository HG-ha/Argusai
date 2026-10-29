"""Tests for user habits profile."""

from __future__ import annotations

from argusai.core.settings import AppSettings
from argusai.core.workspace.user_profile import UserHabitsProfile, profile_from_settings, profile_prompt_summary


def test_profile_roundtrip() -> None:
    p = UserHabitsProfile(
        speaking_style="简洁",
        audit_domains=["web", "api"],
        risk_preference="保守",
    )
    raw = p.to_dict()
    p2 = UserHabitsProfile.from_dict(raw)
    assert p2.speaking_style == "简洁"
    assert p2.audit_domains == ["web", "api"]


def test_profile_prompt_summary() -> None:
    settings = AppSettings(user_habits=UserHabitsProfile(speaking_style="技术向", task_focus="渗透"))
    text = profile_prompt_summary(settings)
    assert "User habits profile" in text
    assert "技术向" in text


def test_profile_from_settings_dict() -> None:
    settings = {"user_habits": {"notes": "no emoji bullets"}}
    p = profile_from_settings(settings)
    assert p.notes == "no emoji bullets"
