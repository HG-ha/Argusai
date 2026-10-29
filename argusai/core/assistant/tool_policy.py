"""Per-tool policy tiers and settings-driven always-allow lists."""

from __future__ import annotations

import sys
from typing import Any

# Tools that may be pre-enabled in settings.json (non-invasive extension).
DEFAULT_TOOL_ALWAYS_ALLOW: list[str] = [
    "desktop_observe",
    "grep_text",
    "read_text_slice",
    "stat_text_file",
    "read_text_file",
    "list_assistant_sandbox_directory",
    "path_exists",
    "search_all_assistant_memory",
    "read_assistant_session",
    "list_session_messages",
    "read_session_message",
    "list_assistant_skills",
    "channel_list_targets",
    "channel_delivery_status",
]

TOOL_POLICY_EXTENSION = "extension"
TOOL_POLICY_READ_SENSITIVE = "read_sensitive"
TOOL_POLICY_WRITE = "write"
TOOL_POLICY_DESTRUCTIVE = "destructive"

# Desktop input tools are intentionally high-risk at runtime, but desktop app
# users can opt into persistent auto-allow from Settings for local automation
# workflows. The setting is explicit, still scoped to tools present in the
# current registry, and the OS may still enforce its own permission layer
# (e.g. macOS Accessibility / Screen Recording).
SETTINGS_FORCE_ALWAYS_ALLOW_DESKTOP_INPUT: set[str] = {
    "desktop_launch_app",
    "desktop_mouse_move",
    "desktop_click",
    "desktop_type",
    "desktop_hotkey",
    "desktop_focus_window",
    "desktop_uia_action",
}

_TIER_BY_TOOL: dict[str, str] = {
    "desktop_observe": TOOL_POLICY_EXTENSION,
    "desktop_uia_find": TOOL_POLICY_READ_SENSITIVE,
    "desktop_notify": TOOL_POLICY_EXTENSION,
    "grep_text": TOOL_POLICY_EXTENSION,
    "patch_text_file": TOOL_POLICY_WRITE,
    "write_text_file": TOOL_POLICY_WRITE,
    "read_text_slice": TOOL_POLICY_EXTENSION,
    "stat_text_file": TOOL_POLICY_EXTENSION,
    "read_text_file": TOOL_POLICY_EXTENSION,
    "list_assistant_sandbox_directory": TOOL_POLICY_EXTENSION,
    "path_exists": TOOL_POLICY_EXTENSION,
    "search_all_assistant_memory": TOOL_POLICY_EXTENSION,
    "read_assistant_session": TOOL_POLICY_EXTENSION,
    "list_session_messages": TOOL_POLICY_EXTENSION,
    "read_session_message": TOOL_POLICY_EXTENSION,
    "list_assistant_skills": TOOL_POLICY_EXTENSION,
    "browser_screenshot": TOOL_POLICY_READ_SENSITIVE,
    "browser_use_tab": TOOL_POLICY_READ_SENSITIVE,
    "desktop_launch_app": TOOL_POLICY_DESTRUCTIVE,
    "desktop_mouse_move": TOOL_POLICY_DESTRUCTIVE,
    "desktop_click": TOOL_POLICY_DESTRUCTIVE,
    "desktop_type": TOOL_POLICY_DESTRUCTIVE,
    "desktop_hotkey": TOOL_POLICY_DESTRUCTIVE,
    "desktop_focus_window": TOOL_POLICY_DESTRUCTIVE,
    "desktop_uia_action": TOOL_POLICY_DESTRUCTIVE,
    "run_python_snippet": TOOL_POLICY_DESTRUCTIVE,
    "run_shell_snippet": TOOL_POLICY_DESTRUCTIVE,
    "run_host_python_snippet": TOOL_POLICY_DESTRUCTIVE,
    "run_host_python_script": TOOL_POLICY_DESTRUCTIVE,
    "shell_session_start": TOOL_POLICY_DESTRUCTIVE,
    "shell_session_exec": TOOL_POLICY_DESTRUCTIVE,
    "shell_session_write": TOOL_POLICY_DESTRUCTIVE,
    "shell_session_stop": TOOL_POLICY_DESTRUCTIVE,
    "raw_http_send": TOOL_POLICY_DESTRUCTIVE,
    "channel_list_targets": TOOL_POLICY_EXTENSION,
    "channel_delivery_status": TOOL_POLICY_EXTENSION,
    "channel_send_message": TOOL_POLICY_WRITE,
    "channel_send_file": TOOL_POLICY_WRITE,
}


def tool_policy_tier(name: str) -> str:
    return _TIER_BY_TOOL.get(str(name or "").strip(), TOOL_POLICY_WRITE)


def normalize_tool_always_allow(value: Any, *, default: list[str] | None = None) -> list[str]:
    base = list(default if default is not None else DEFAULT_TOOL_ALWAYS_ALLOW)
    if isinstance(value, str):
        raw_items = [p.strip() for p in value.replace(";", ",").split(",")]
    elif isinstance(value, list):
        raw_items = [str(x).strip() for x in value]
    else:
        return list(dict.fromkeys(base))
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    for item in base:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out


def apply_settings_tool_always_allow(session: Any, settings: Any) -> None:
    """Seed per-session auto-approve from user settings.

    Extension-tier tools are safe defaults. On desktop OSes, desktop input
    tools may also be explicitly listed by the user in Settings; apply those
    with ``force=True`` so they survive the high-risk enable guard.
    """
    names = normalize_tool_always_allow(getattr(settings, "assistant_tool_always_allow", None))
    registry = getattr(session, "registry", None)
    if registry is None:
        return
    tools = getattr(registry, "_tools", {})
    for name in names:
        if name not in tools:
            continue
        force = False
        if tool_policy_tier(name) != TOOL_POLICY_EXTENSION:
            if (
                (sys.platform.startswith("win") or sys.platform == "darwin")
                and name in SETTINGS_FORCE_ALWAYS_ALLOW_DESKTOP_INPUT
            ):
                force = True
            else:
                continue
        try:
            session.set_auto_approve_tool(name, True, force=force)
        except Exception:
            continue
