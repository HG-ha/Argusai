"""Shared imports and constants for assistant GUI mixins."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from argusai.core.assistant import AssistantSession, build_tool_registry
from argusai.core.infra.logging_setup import session_log_context

from ._helpers import debug_log

_SESSION_ID_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_DEFAULT_ASSISTANT_SESSION_NAMES = {"", "AI 助手", "新会话", "加载中..."}


def assistant_chat_has_input(params: dict[str, Any]) -> bool:
    """True when the user sent text, voice, attachments, or a typed confirmation."""
    if str(params.get("message") or "").strip():
        return True
    confirmation = params.get("confirmation")
    if isinstance(confirmation, dict) and confirmation:
        return True
    voice = params.get("voice")
    if isinstance(voice, dict) and (voice.get("data_base64") or voice.get("data")):
        return True
    attachments = params.get("attachments")
    return isinstance(attachments, list) and len(attachments) > 0


__all__ = [
    "Any", "AssistantSession", "Path", "build_tool_registry", "debug_log",
    "json", "session_log_context", "time", "_DEFAULT_ASSISTANT_SESSION_NAMES",
    "_SESSION_ID_ALLOWED", "assistant_chat_has_input",
]
