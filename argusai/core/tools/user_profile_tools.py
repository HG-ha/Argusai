"""User habits profile tools (settings-backed, distinct from knowledge base)."""

from __future__ import annotations

import time
from typing import Any

from .common import *


def user_profile_read(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Read the user's habits profile summary (read-only)."""
    from argusai.core.workspace.user_profile import profile_from_settings, profile_prompt_summary

    settings = api.settings_mgr.get()
    profile = profile_from_settings(settings)
    return {
        "ok": True,
        "profile": profile.to_dict(),
        "summary": profile_prompt_summary(settings),
    }


def user_profile_suggest_update(
    api: Any,
    patch: dict | None = None,
    reason: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Suggest updates to user habits without applying them. User must confirm via user_profile_apply_update."""
    from argusai.core.workspace.user_profile import UserHabitsProfile, profile_from_settings

    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "patch object is required"}
    current = profile_from_settings(api.settings_mgr.get())
    merged = UserHabitsProfile.from_dict({**current.to_dict(), **patch})
    return {
        "ok": True,
        "suggested": merged.to_dict(),
        "reason": str(reason or "")[:500],
        "requires_confirmation": True,
        "apply_tool": "user_profile_apply_update",
    }


def user_profile_apply_update(
    api: Any,
    patch: dict | None = None,
    confirm_label: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply user habits profile changes after explicit user confirmation."""
    from argusai.core.workspace.user_profile import UserHabitsProfile, profile_from_settings

    if str(confirm_label or "").strip() != "APPLY_USER_PROFILE":
        return {
            "ok": False,
            "error": "requires confirm_label='APPLY_USER_PROFILE' after user approval",
        }
    if not isinstance(patch, dict) or not patch:
        return {"ok": False, "error": "patch object is required"}

    result: dict[str, UserHabitsProfile] = {}

    def _update(settings):
        current = profile_from_settings(settings)
        merged = UserHabitsProfile.from_dict({
            **current.to_dict(),
            **patch,
            "updated_at": time.time(),
        })
        settings.user_habits = merged
        result["profile"] = merged
        return settings

    api.settings_mgr.update(_update)
    merged = result["profile"]
    return {"ok": True, "profile": merged.to_dict()}
