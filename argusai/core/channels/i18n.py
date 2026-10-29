"""Locale-aware text for channel replies, cards, and surfaced store errors.

Channel transports are headless, so the host process injects the active UI
locale. Library defaults stay English; only a host that knows the user's
language switches the catalog.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_LOCALE = "en"
BILINGUAL = "both"
BILINGUAL_PRIMARY = "zh"
SUPPORTED_LOCALES = ("en", "zh", BILINGUAL)


def normalize_locale(value: Any) -> str:
    """Map any locale-ish input onto a supported catalog key."""
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {BILINGUAL, "bilingual", "zh-en", "en-zh", "zh+en"}:
        return BILINGUAL
    if text.startswith("zh") or text in {"cn", "chs", "hans"}:
        return "zh"
    return DEFAULT_LOCALE


def _as_ui_locale(mode: str) -> str:
    return BILINGUAL_PRIMARY if mode == BILINGUAL else mode


def _tag_is_chinese(tag: str) -> bool:
    text = str(tag or "").strip().lower().replace("_", "-")
    return text.startswith("zh") or text.startswith("yue") or text in {
        "cn",
        "chs",
        "cht",
        "hans",
        "hant",
    }


def _win_is_chinese() -> bool:
    """True when Windows UI / user locale / console codepage looks Chinese."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for getter in (
            kernel32.GetUserDefaultUILanguage,
            kernel32.GetSystemDefaultUILanguage,
            kernel32.GetUserDefaultLangID,
        ):
            try:
                lang_id = int(getter())
            except Exception:
                continue
            if (lang_id & 0xFF) == 0x04:  # LANG_CHINESE
                return True

        # Locale name API (e.g. "zh-CN").
        try:
            buf = ctypes.create_unicode_buffer(85)
            if kernel32.GetUserDefaultLocaleName(buf, 85):
                if str(buf.value or "").lower().startswith("zh"):
                    return True
        except Exception:
            pass
    except Exception:
        pass
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetConsoleCP())
        if cp in {936, 54936}:  # GBK / GB18030
            return True
    except Exception:
        pass
    return False


def _macos_cf_preferred_languages() -> list[str]:
    """Read ``AppleLanguages`` via CoreFoundation (live System Settings)."""
    if sys.platform != "darwin":
        return []
    try:
        import ctypes
        from ctypes import c_bool, c_char_p, c_long, c_uint32, c_void_p
    except Exception:
        return []

    try:
        cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError:
        return []

    k_utf8 = 0x08000100
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    cf.CFPreferencesCopyAppValue.restype = c_void_p
    cf.CFPreferencesCopyAppValue.argtypes = [c_void_p, c_void_p]
    cf.CFGetTypeID.restype = c_long
    cf.CFGetTypeID.argtypes = [c_void_p]
    cf.CFArrayGetTypeID.restype = c_long
    cf.CFArrayGetCount.restype = c_long
    cf.CFArrayGetCount.argtypes = [c_void_p]
    cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
    cf.CFStringGetTypeID.restype = c_long
    cf.CFStringGetLength.restype = c_long
    cf.CFStringGetLength.argtypes = [c_void_p]
    cf.CFStringGetMaximumSizeForEncoding.restype = c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [c_long, c_uint32]
    cf.CFStringGetCString.restype = c_bool
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
    cf.CFRelease.argtypes = [c_void_p]

    def cf_str(text: bytes) -> int:
        return int(cf.CFStringCreateWithCString(None, text, k_utf8) or 0)

    def py_str(ref: int) -> str:
        if not ref or cf.CFGetTypeID(ref) != cf.CFStringGetTypeID():
            return ""
        length = int(cf.CFStringGetLength(ref))
        size = int(cf.CFStringGetMaximumSizeForEncoding(length, k_utf8)) + 1
        buf = ctypes.create_string_buffer(max(size, 16))
        if not cf.CFStringGetCString(ref, buf, len(buf), k_utf8):
            return ""
        return buf.value.decode("utf-8", errors="ignore")

    key = app = value = 0
    tags: list[str] = []
    try:
        key = cf_str(b"AppleLanguages")
        app = cf_str(b"kCFPreferencesAnyApplication")
        if not key or not app:
            return []
        value = int(cf.CFPreferencesCopyAppValue(key, app) or 0)
        if not value or cf.CFGetTypeID(value) != cf.CFArrayGetTypeID():
            return []
        for idx in range(int(cf.CFArrayGetCount(value))):
            item = int(cf.CFArrayGetValueAtIndex(value, idx) or 0)
            text = py_str(item)
            if text:
                tags.append(text)
    except Exception:
        return []
    finally:
        for ref in (value, key, app):
            if ref:
                try:
                    cf.CFRelease(ref)
                except Exception:
                    pass
    return tags


def _macos_plist_preferred_languages() -> list[str]:
    """``AppleLanguages`` from the user defaults plist (Finder / .app launches)."""
    if sys.platform != "darwin":
        return []
    try:
        import plistlib
        from pathlib import Path

        path = Path.home() / "Library" / "Preferences" / ".GlobalPreferences.plist"
        if not path.is_file():
            return []
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return []
    raw = data.get("AppleLanguages") if isinstance(data, dict) else None
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def macos_preferred_language_tags() -> list[str]:
    """Primary UI languages from System Settings, not POSIX ``LANG``."""
    return _macos_cf_preferred_languages() or _macos_plist_preferred_languages()


def _macos_ui_locale() -> str | None:
    tags = macos_preferred_language_tags()
    if not tags:
        return None
    return "zh" if _tag_is_chinese(tags[0]) else "en"


def detect_system_locale() -> str:
    """Best-effort OS UI language → ``en`` | ``zh`` (not bilingual).

    Explicit ``ARGUSAI_LANG`` / ``ARGUSAI_LOCALE`` win. Otherwise the desktop
    preferred language is used: Windows UI lang-id, then macOS
    ``AppleLanguages``. POSIX ``LANG`` is last because Finder-launched .app
    bundles and developer shells often leave it at ``en_US`` even when the
    user runs Chinese.
    """
    for key in ("ARGUSAI_LANG", "ARGUSAI_LOCALE"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return _as_ui_locale(normalize_locale(raw))

    if _win_is_chinese():
        return "zh"
    mac = _macos_ui_locale()
    if mac is not None:
        return mac

    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        raw = (os.environ.get(key) or "").strip()
        if raw and raw.lower() not in {"c", "posix"}:
            # LANGUAGE may be "zh_CN:en" — take the first tag.
            first = raw.split(":")[0].strip()
            return _as_ui_locale(normalize_locale(first))
    try:
        import locale as _locale

        for getter in (_locale.getlocale, getattr(_locale, "getdefaultlocale", None)):
            if getter is None:
                continue
            try:
                candidate = getter()[0]
            except Exception:
                candidate = None
            if candidate:
                return _as_ui_locale(normalize_locale(str(candidate)))
    except Exception:
        pass
    return DEFAULT_LOCALE


@dataclass(frozen=True)
class Frag:
    """A catalog reference resolved in whatever locale its parent renders in.

    Composed messages (``"{count} messages.{suffix}"``) must not mix languages,
    so fragments are resolved per output line rather than up front.
    """

    key: str
    params: dict[str, Any] = field(default_factory=dict)

    def __init__(self, key: str, **params: Any) -> None:
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "params", params)


#: Keys that stay in a single language even in bilingual mode: button captions
#: that platforms truncate, and row templates that would double a data listing.
SINGLE_LOCALE_KEYS = frozenset({
    "card_allow_once",
    "card_deny",
    "card_submit",
    "card_choose_options",
    "card_plan",
    "card_plan_progress",
    "todo_status_pending",
    "todo_status_in_progress",
    "todo_status_completed",
    "todo_status_cancelled",
    "todo_status_blocked",
    "card_run_tool",
    "card_selected",
    "cancel_label",
    "auto_allow_label",
    "ready_mode_enable",
    "ready_mode_skip",
    "list_pick_label",
    "sessions_previous",
    "sessions_next",
    "sessions_row",
    "sessions_current_marker",
    "files_previous",
    "files_next",
    "file_list_row",
    "endpoint_row",
    "endpoint_current_marker",
    "endpoint_switch_label",
    "agent_row",
    "agent_unavailable_marker",
    "status_row_name",
    "status_row_id",
    "status_row_messages",
    "status_row_agent",
    "status_row_running",
    "status_row_idle",
    "status_row_greenlight_on",
    "status_row_greenlight_off",
    "status_row_search_on",
    "status_row_search_off",
    "status_row_think_on",
    "status_row_think_effort",
    "status_row_think_off",
    "status_row_think_scale",
    "status_row_context",
    "status_row_live_on",
    "status_row_live_off",
    "status_row_auto_allow",
    "status_row_pending",
    "status_row_pending_none",
    "status_row_todos",
    "status_row_memory",
    "status_row_tokens",
    "status_row_cache",
    "status_row_cache_write",
    "status_row_tokens_empty",
    "status_row_sandbox",
    "status_row_sandbox_empty",
    "status_row_note",
    "waiting_confirmation_title",
})


MESSAGES: dict[str, dict[str, str]] = {
    # Pairing and identity.
    "not_paired": {
        "en": "This identity is not paired. In ArgusAI, create a pairing code, then send /pair 123456 in a direct message.",
        "zh": "该身份尚未配对。请在 ArgusAI 中生成配对码，然后在与机器人的私聊中发送 /pair 123456。",
    },
    "pairing_complete_bound": {
        "en": "Pairing complete. Bound session: {session_id}.",
        "zh": "配对完成。已绑定会话：{session_id}。",
    },
    "pairing_complete_unbound": {
        "en": "Pairing complete. Send /new to create a conversation, or bind one in ArgusAI.",
        "zh": "配对完成。发送 /new 新建会话，或在 ArgusAI 中绑定一个已有会话。",
    },
    "pairing_invalid_input": {
        "en": "invalid pairing code or external user id",
        "zh": "配对码或外部用户 ID 无效",
    },
    "pairing_invalid": {
        "en": "pairing code is invalid or expired",
        "zh": "配对码无效或已过期",
    },
    # Conversation listing and switching.
    "no_sessions_match": {
        "en": 'No conversations match "{query}".',
        "zh": "没有匹配“{query}”的会话。",
    },
    "no_sessions": {
        "en": "No conversations are available. Send /new to create one.",
        "zh": "当前没有可用会话。发送 /new 新建一个。",
    },
    "sessions_title": {
        "en": "Conversations · page {page}/{page_count}",
        "zh": "会话列表 · 第 {page}/{page_count} 页",
    },
    "sessions_title_query": {
        "en": 'Conversations · page {page}/{page_count} · search "{query}"',
        "zh": "会话列表 · 第 {page}/{page_count} 页 · 搜索“{query}”",
    },
    "sessions_row": {
        "en": "{ordinal}. {name} · {count} messages{marker}",
        "zh": "{ordinal}. {name} · {count} 条消息{marker}",
    },
    "sessions_current_marker": {
        "en": " (current)",
        "zh": "（当前）",
    },
    # Compact pickers for list cards: body rows carry the full title.
    "list_pick_label": {
        "en": "{ordinal}",
        "zh": "{ordinal}",
    },
    "sessions_previous": {
        "en": "‹ Previous",
        "zh": "‹ 上一页",
    },
    "sessions_next": {
        "en": "Next ›",
        "zh": "下一页 ›",
    },
    "sessions_hint": {
        "en": "Only conversations granted to this pairing are listed. Use /use <number or conversation ID> to switch, or /sessions search <keyword> to filter.",
        "zh": "这里只列出当前配对可以使用的会话。发送 /use <编号或会话 ID> 切换，或 /sessions search <关键词> 筛选。",
    },
    "sessions_card_title": {
        "en": "Switch conversation",
        "zh": "切换会话",
    },
    "sessions_card_body": {
        "en": "Choose a conversation to bind to this chat.",
        "zh": "选择要绑定到当前聊天的会话。",
    },
    "session_not_found": {
        "en": "Conversation not found. Send /sessions to refresh the list.",
        "zh": "未找到该会话。发送 /sessions 刷新列表。",
    },
    "session_switched": {
        "en": "Switched to {name} ({session_id}).",
        "zh": "已切换到 {name}（{session_id}）。",
    },
    "session_created": {
        "en": "Created and bound conversation {session_id}.",
        "zh": "已新建并绑定会话 {session_id}。",
    },
    "no_conversation_bound": {
        "en": "No conversation is bound. Send /new or bind one from ArgusAI.",
        "zh": "尚未绑定会话。发送 /new 新建，或在 ArgusAI 中绑定。",
    },
    "usage_sessions": {
        "en": "Usage: /sessions [page] or /sessions search <keyword>",
        "zh": "用法：/sessions [页码] 或 /sessions search <关键词>",
    },
    "usage_sessions_search": {
        "en": "Usage: /sessions search <keyword>",
        "zh": "用法：/sessions search <关键词>",
    },
    "usage_file": {
        "en": "Usage: /file [list [page]|search <keyword>|number|path]",
        "zh": "用法：/file [list [页码]|search <关键词>|编号|路径]",
    },
    "usage_file_search": {
        "en": "Usage: /file search <keyword>",
        "zh": "用法：/file search <关键词>",
    },
    # Help.
    # /help is assembled line by line so the command column stays language
    # neutral; only the descriptions are translated.
    "help_title": {
        "en": "Commands",
        "zh": "可用命令",
    },
    "help_group_conversation": {
        "en": "Conversation",
        "zh": "会话",
    },
    "help_group_run": {
        "en": "Execution",
        "zh": "执行",
    },
    "help_group_config": {
        "en": "Configuration",
        "zh": "配置",
    },
    "help_btw": {
        "en": "side question: no history, no interruption",
        "zh": "侧问：不写入历史，也不打断运行",
    },
    "help_new": {
        "en": "start a conversation, optionally on a local agent",
        "zh": "新建会话，可指定本地 Agent",
    },
    "help_agent": {
        "en": "list the local agents on this machine",
        "zh": "列出本机可用的本地 Agent",
    },
    "help_sessions": {
        "en": "list conversations",
        "zh": "会话列表",
    },
    "help_use": {
        "en": "switch conversation",
        "zh": "切换会话",
    },
    "help_status": {
        "en": "conversation overview (name, run, sandbox, pending)",
        "zh": "会话概览（名称、运行、沙盒、待确认）",
    },
    "help_tools": {
        "en": "tools and pending confirmations",
        "zh": "可用工具与待确认项",
    },
    "help_allow": {
        "en": "approve a tool call",
        "zh": "允许工具调用",
    },
    "help_deny": {
        "en": "reject a tool call",
        "zh": "拒绝工具调用",
    },
    "help_act": {
        "en": "trigger a card button by token",
        "zh": "用 token 触发卡片按钮",
    },
    "help_cancel": {
        "en": "stop the current run",
        "zh": "取消当前运行",
    },
    "help_greenlight": {
        "en": "toggle Greenlight Mode",
        "zh": "开关绿灯模式",
    },
    "help_live": {
        "en": "push mid-turn replies and cards live (on by default)",
        "zh": "实时推送阶段性回复与卡片（默认开启）",
    },
    "help_model": {
        "en": "view or switch AI endpoint / ACP model",
        "zh": "查看或切换 AI 端点 / ACP 模型",
    },
    "help_search": {
        "en": "toggle web search",
        "zh": "联网搜索开关",
    },
    "help_think": {
        "en": "thinking on/off or intensity (auto/none/low/medium/high/xhigh/max)",
        "zh": "思考开关或强度（auto/none/low/medium/high/xhigh/max）",
    },
    "help_lang": {
        "en": "reply language",
        "zh": "回复语言",
    },
    "help_file": {
        "en": "list, search, or fetch sandbox files",
        "zh": "列出、搜索或取回沙盒文件",
    },
    "help_plain": {
        "en": "Plain messages go straight to the bound conversation.",
        "zh": "普通消息会直接发送给已绑定的会话。",
    },
    # Local agent runtimes.
    "agent_list_title": {
        "en": "Local agents",
        "zh": "本地 Agent",
    },
    "agent_row": {
        "en": "{ordinal}. {name} · {kind}{marker}",
        "zh": "{ordinal}. {name} · {kind}{marker}",
    },
    "agent_unavailable_marker": {
        "en": " (not detected)",
        "zh": "（未检测到）",
    },
    "agent_list_hint": {
        "en": (
            "Send /new <number or name> to start a conversation on that agent. "
            "It always runs in a conversation-owned sandbox, never in a local project "
            "directory. Plain /new keeps the built-in agent."
        ),
        "zh": (
            "发送 /new <编号或名称> 用该 Agent 新建会话，工作区固定为会话专属沙盒，"
            "远程不会进入本地项目目录。直接发 /new 则使用内置 Agent。"
        ),
    },
    "agent_card_title": {
        "en": "Start a conversation",
        "zh": "新建会话",
    },
    "agent_card_body": {
        "en": "Pick the agent that should run it.",
        "zh": "选择运行该会话的 Agent。",
    },
    "agent_none": {
        "en": "No local agent is available. Add one in the ArgusAI local agent panel.",
        "zh": "没有可用的本地 Agent。可在 ArgusAI 的本地 Agent 面板里添加。",
    },
    "agent_not_found": {
        "en": "No such agent. Send /agent to see the list.",
        "zh": "没有匹配的 Agent。发送 /agent 查看列表。",
    },
    "agent_not_detected": {
        "en": "That agent's command was not detected on this machine.",
        "zh": "该 Agent 的启动命令在本机未检测到。",
    },
    "agent_not_allowed": {
        "en": "This identity may not read or start local agents.",
        "zh": "该身份无权查看或启动本地 Agent。",
    },
    "agent_unavailable": {
        "en": "Local agents are unavailable on this host.",
        "zh": "当前主机不支持本地 Agent。",
    },
    "agent_session_created": {
        "en": "Conversation {session_id} created on {name}, in its own sandbox. Send a message to start.",
        "zh": "已用 {name} 新建会话 {session_id}（专属沙盒工作区）。直接发消息即可开始。",
    },
    "agent_create_failed": {
        "en": "Could not start a conversation on that agent.",
        "zh": "无法用该 Agent 新建会话。",
    },
    # AI endpoints.
    "endpoint_list_title": {
        "en": "AI endpoints (in failover order)",
        "zh": "AI 端点（按 failover 顺序）",
    },
    "endpoint_row": {
        "en": "{ordinal}. {name} · {model}{marker}",
        "zh": "{ordinal}. {name} · {model}{marker}",
    },
    "endpoint_current_marker": {
        "en": " (in use)",
        "zh": "（使用中）",
    },
    "endpoint_list_hint": {
        "en": (
            "Send /model <number or name> to switch. Only enabled endpoints are listed, "
            "and the change applies everywhere, including the desktop app. "
            "Vision and speech endpoints are configured separately and stay untouched."
        ),
        "zh": (
            "发送 /model <编号或名称> 切换。这里只列出已启用的端点，"
            "切换后对所有会话和桌面端同时生效。"
            "视觉与语音识别端点是单独配置的，不受影响。"
        ),
    },
    "endpoint_card_title": {
        "en": "Switch AI endpoint",
        "zh": "切换 AI 端点",
    },
    "endpoint_card_body": {
        "en": "Pick the endpoint that should answer first.",
        "zh": "选择优先使用的端点。",
    },
    "endpoint_none": {
        "en": "No AI endpoint is enabled. Add one in ArgusAI settings.",
        "zh": "没有已启用的 AI 端点。请在 ArgusAI 设置里添加。",
    },
    "endpoint_not_found": {
        "en": "No enabled endpoint matches that. Send /model to see the list.",
        "zh": "没有匹配的已启用端点。发送 /model 查看列表。",
    },
    "endpoint_not_allowed": {
        "en": "This identity may not read or change AI endpoints.",
        "zh": "该身份无权查看或切换 AI 端点。",
    },
    "endpoint_unavailable": {
        "en": "Endpoint control is unavailable on this host.",
        "zh": "当前主机不支持切换端点。",
    },
    "endpoint_already_active": {
        "en": "{name} already answers first.",
        "zh": "「{name}」已经是当前优先使用的端点。",
    },
    "endpoint_switch_prompt": {
        "en": "Confirm the switch below.",
        "zh": "请在下方确认切换。",
    },
    "endpoint_switch_title": {
        "en": "Switch AI endpoint",
        "zh": "切换 AI 端点",
    },
    "endpoint_switch_body": {
        "en": (
            "{name} ({model}, {host}) will answer first, for every conversation "
            "and for the desktop app. Vision and speech endpoints stay as they are."
        ),
        "zh": (
            "将改用「{name}」（{model}，{host}）优先应答，所有会话与桌面端同时生效。"
            "视觉与语音识别端点保持不变。"
        ),
    },
    "endpoint_switch_label": {
        "en": "Switch",
        "zh": "确认切换",
    },
    "endpoint_switched": {
        "en": "{name} ({model}) now answers first.",
        "zh": "已切换为「{name}」（{model}）优先应答。",
    },
    "endpoint_failed": {
        "en": "Could not read the AI endpoints.",
        "zh": "无法读取 AI 端点配置。",
    },
    "endpoint_changed": {
        "en": "Endpoints changed while you were choosing. Send /model again.",
        "zh": "端点配置在此期间发生了变化，请重新发送 /model。",
    },
    "acp_model_list_title": {
        "en": "ACP models for this conversation",
        "zh": "本会话的 ACP 模型",
    },
    "acp_model_list_hint": {
        "en": (
            "Send /model <number or name> to switch for this ACP conversation only. "
            "Send /model refresh to re-read models from the agent. "
            "Native AI endpoints stay unchanged."
        ),
        "zh": (
            "发送 /model <编号或名称> 仅切换本 ACP 会话的模型。"
            "发送 /model refresh 重新向 Agent 拉取模型列表。"
            "内置 AI 端点不受影响。"
        ),
    },
    "acp_model_none": {
        "en": "This local agent does not expose a model selector over ACP.",
        "zh": "该本地 Agent 未通过 ACP 暴露模型选择。",
    },
    "acp_model_not_found": {
        "en": "No ACP model matches that. Send /model to see the list.",
        "zh": "没有匹配的 ACP 模型。发送 /model 查看列表。",
    },
    "acp_model_unavailable": {
        "en": "ACP model control is unavailable for this conversation.",
        "zh": "当前会话不支持 ACP 模型切换。",
    },
    "acp_model_probe_failed": {
        "en": "Could not read models from the local agent.",
        "zh": "无法从本地 Agent 读取模型列表。",
    },
    "acp_model_already_active": {
        "en": "{name} is already selected for this conversation.",
        "zh": "本会话已在使用「{name}」。",
    },
    "acp_model_switch_title": {
        "en": "Switch ACP model",
        "zh": "切换 ACP 模型",
    },
    "acp_model_switch_body": {
        "en": (
            "{name} ({model}, {host}) will be used for the next turns of this "
            "ACP conversation only."
        ),
        "zh": (
            "将改用「{name}」（{model}，{host}），仅影响本 ACP 会话的后续回合。"
        ),
    },
    "acp_model_switched": {
        "en": "{name} ({model}) selected for this ACP conversation.",
        "zh": "本 ACP 会话已切换为「{name}」（{model}）。",
    },
    "acp_model_changed": {
        "en": "That model list changed while you were choosing. Send /model and try again.",
        "zh": "模型列表在确认前已变化。请发送 /model 后重试。",
    },
    "acp_model_failed": {
        "en": "Could not switch the ACP model.",
        "zh": "无法切换 ACP 模型。",
    },
    "lang_usage": {
        "en": "Usage: /lang zh, /lang en, or /lang both. Current: {current}.",
        "zh": "用法：/lang zh、/lang en 或 /lang both。当前：{current}。",
    },
    "lang_set": {
        "en": "Reply language set to {current}.",
        "zh": "回复语言已设置为 {current}。",
    },
    "unknown_command": {
        "en": "Unknown command: {command}. Send /help.",
        "zh": "未知命令：{command}。发送 /help 查看用法。",
    },
    # Permissions.
    "chat_not_allowed": {
        "en": "Chat access is not allowed.",
        "zh": "该身份没有对话权限。",
    },
    "create_not_allowed": {
        "en": "This identity cannot create conversations.",
        "zh": "该身份无法新建会话。",
    },
    "switch_not_allowed": {
        "en": "Conversation switching is not allowed for this identity.",
        "zh": "该身份不允许切换会话。",
    },
    "status_not_allowed": {
        "en": "Status access is not allowed.",
        "zh": "该身份没有查看状态的权限。",
    },
    "tools_not_allowed": {
        "en": "Tool status access is not allowed.",
        "zh": "该身份没有查看工具状态的权限。",
    },
    "greenlight_not_allowed": {
        "en": "Greenlight access is not allowed for this identity.",
        "zh": "该身份不允许查看或修改 Greenlight。",
    },
    "greenlight_unavailable": {
        "en": "Greenlight configuration is unavailable.",
        "zh": "Greenlight 设置当前不可用。",
    },
    "greenlight_failed": {
        "en": "Failed to update Greenlight.",
        "zh": "更新 Greenlight 失败。",
    },
    "live_not_allowed": {
        "en": "Live update configuration is not allowed for this identity.",
        "zh": "该身份不允许修改实时推送设置。",
    },
    "live_unavailable": {
        "en": "Live update configuration is unavailable.",
        "zh": "实时推送设置当前不可用。",
    },
    "live_failed": {
        "en": "Failed to update live updates.",
        "zh": "更新实时推送失败。",
    },
    "search_not_allowed": {
        "en": "Search configuration is not allowed for this identity.",
        "zh": "该身份不允许修改联网搜索设置。",
    },
    "think_not_allowed": {
        "en": "Thinking-mode configuration is not allowed for this identity.",
        "zh": "该身份不允许修改思考模式。",
    },
    "cancel_not_allowed": {
        "en": "Cancellation is not allowed.",
        "zh": "该身份没有取消运行的权限。",
    },
    "confirm_not_allowed": {
        "en": "Remote confirmation is not allowed for this identity.",
        "zh": "该身份不允许远程确认工具。",
    },
    "confirm_not_allowed_hint": {
        "en": (
            "Waiting on tool `{name}`, but this identity cannot confirm remotely. "
            "Grant `confirm` (GUI Paired identities, or `argusai channels grant "
            "<id> --all`) and retry, or allow the tool in the ArgusAI host."
        ),
        "zh": (
            "正在等待工具 `{name}`，但该身份无远程确认权限。"
            "请在 GUI「已配对身份」勾选 confirm，或执行 "
            "`argusai channels grant <id> --all` 后重试；也可在 ArgusAI 主机侧放行。"
        ),
    },
    "waiting_confirmation_title": {
        "en": "Waiting for confirmation",
        "zh": "等待确认",
    },
    "file_not_allowed": {
        "en": "File delivery is not allowed for this identity.",
        "zh": "该身份不允许接收文件。",
    },
    # Session status.
    "conversation_unavailable": {
        "en": "Conversation unavailable.",
        "zh": "会话不可用。",
    },
    "status_overview_title": {
        "en": "Conversation overview",
        "zh": "会话概览",
    },
    "status_row_name": {
        "en": "Name: {name}",
        "zh": "名称：{name}",
    },
    "status_row_id": {
        "en": "ID: {session_id}",
        "zh": "ID：{session_id}",
    },
    "status_row_messages": {
        "en": "Messages: {count}",
        "zh": "消息：{count}",
    },
    "status_row_agent": {
        "en": "Agent: {name}",
        "zh": "Agent：{name}",
    },
    "status_row_running": {
        "en": "Run: in progress",
        "zh": "运行：进行中",
    },
    "status_row_idle": {
        "en": "Run: idle",
        "zh": "运行：空闲",
    },
    "status_row_greenlight_on": {
        "en": "Authorization: on ({scope})",
        "zh": "授权：开启（{scope}）",
    },
    "status_row_greenlight_off": {
        "en": "Authorization: off",
        "zh": "授权：关闭",
    },
    "status_row_search_on": {
        "en": "Web search: on",
        "zh": "联网搜索：开",
    },
    "status_row_search_off": {
        "en": "Web search: off",
        "zh": "联网搜索：关",
    },
    "status_row_think_on": {
        "en": "Thinking: on",
        "zh": "思考：开",
    },
    "status_row_think_effort": {
        "en": "Thinking: on ({effort})",
        "zh": "思考：开（{effort}）",
    },
    "status_row_think_off": {
        "en": "Thinking: off",
        "zh": "思考：关",
    },
    "status_row_think_scale": {
        "en": "  {scale}",
        "zh": "  {scale}",
    },
    "status_row_live_on": {
        "en": "Live updates: on",
        "zh": "实时推送：开",
    },
    "status_row_live_off": {
        "en": "Live updates: off",
        "zh": "实时推送：关",
    },
    "status_row_auto_allow": {
        "en": "Auto-allowed tools: {count}",
        "zh": "自动允许工具：{count}",
    },
    "status_row_pending": {
        "en": "Pending confirmation: {names}",
        "zh": "待确认：{names}",
    },
    "status_row_pending_none": {
        "en": "Pending confirmation: none",
        "zh": "待确认：无",
    },
    "status_row_todos": {
        "en": "Todos: {count}",
        "zh": "待办：{count}",
    },
    "status_row_memory": {
        "en": "Memory entries: {count}",
        "zh": "记忆条目：{count}",
    },
    "status_row_context": {
        "en": "Context: {percent} · {used} / {window}",
        "zh": "上下文：{percent} · {used} / {window}",
    },
    "status_row_tokens": {
        "en": "Tokens: IN {input} · OUT {output} · CALLS {calls}",
        "zh": "Token：IN {input} · OUT {output} · CALLS {calls}",
    },
    "status_row_cache": {
        "en": "Cache: HIT {rate} · H/M {hit}/{miss}",
        "zh": "缓存：HIT {rate} · H/M {hit}/{miss}",
    },
    "status_row_cache_write": {
        "en": "Cache write: {write}",
        "zh": "缓存写入：{write}",
    },
    "status_row_tokens_empty": {
        "en": "Tokens / cache: none yet",
        "zh": "Token / 缓存：暂无",
    },
    "status_row_sandbox": {
        "en": "Sandbox files: {count}",
        "zh": "沙盒文件：{count}",
    },
    "status_row_sandbox_empty": {
        "en": "Sandbox files: none",
        "zh": "沙盒文件：无",
    },
    "status_row_note": {
        "en": "Note: {note}",
        "zh": "备注：{note}",
    },
    "status_overview_hint": {
        "en": "Send /tools for pending tools, /file list for sandbox files, /sessions to switch.",
        "zh": "发送 /tools 查看待确认工具，/file list 浏览沙盒，/sessions 切换会话。",
    },
    "status_line": {
        "en": "Conversation {session_id}: {count} messages.{suffix}",
        "zh": "会话 {session_id}：共 {count} 条消息。{suffix}",
    },
    "status_waiting": {
        "en": " Waiting for confirmation: {name}.",
        "zh": "正在等待确认：{name}。",
    },
    "tools_line": {
        "en": "{count} tools are available.{suffix}",
        "zh": "当前有 {count} 个可用工具。{suffix}",
    },
    "tools_pending": {
        "en": " Pending: {names}.",
        "zh": "待确认：{names}。",
    },
    "tools_no_pending": {
        "en": " No pending confirmations.",
        "zh": "没有待确认项。",
    },
    "greenlight_active": {
        "en": "Greenlight is on for this conversation.",
        "zh": "本会话已开启绿灯模式。",
    },
    "greenlight_off": {
        "en": "Greenlight is off for this conversation.",
        "zh": "本会话绿灯模式已关闭。",
    },
    "live_on": {
        "en": "Live updates are on. Mid-turn replies and cards will be pushed to this chat.",
        "zh": "实时推送已开启。阶段性回复与卡片会推送到本对话。",
    },
    "live_off": {
        "en": "Live updates are off. Only the final reply is sent.",
        "zh": "实时推送已关闭。仅发送最终回复。",
    },
    # Search, cancel, confirmations.
    "search_unavailable": {
        "en": "Search configuration is unavailable.",
        "zh": "联网搜索设置当前不可用。",
    },
    "search_updated": {
        "en": "Search setting updated.",
        "zh": "联网搜索设置已更新。",
    },
    "think_unavailable": {
        "en": "Thinking-mode configuration is unavailable.",
        "zh": "思考模式设置当前不可用。",
    },
    "think_on": {
        "en": "Thinking mode is on. Follow-up turns will request model reasoning.",
        "zh": "思考模式已开启。后续对话会请求模型推理。",
    },
    "think_on_effort": {
        "en": "Thinking mode is on ({effort}). Follow-up turns will request model reasoning.",
        "zh": "思考模式已开启（{effort}）。后续对话会请求模型推理。",
    },
    "think_off": {
        "en": "Thinking mode is off.",
        "zh": "思考模式已关闭。",
    },
    "usage_think": {
        "en": "Usage: /think [on|off|status|auto|none|minimal|low|medium|high|xhigh|max]",
        "zh": "用法：/think [on|off|status|auto|none|minimal|low|medium|high|xhigh|max]",
    },
    "usage_search": {
        "en": "Usage: /search [on|off|status]",
        "zh": "用法：/search [on|off|status]",
    },
    "usage_live": {
        "en": "Usage: /live [on|off|status]",
        "zh": "用法：/live [on|off|status]",
    },
    "usage_greenlight": {
        "en": "Usage: /greenlight [on|off|status]",
        "zh": "用法：/greenlight [on|off|status]",
    },
    "cancel_requested": {
        "en": "Cancellation requested.",
        "zh": "已请求取消。",
    },
    "cancel_failed": {
        "en": "Cancellation failed.",
        "zh": "取消失败。",
    },
    "no_pending_confirmation": {
        "en": "There is no pending confirmation.",
        "zh": "当前没有待确认项。",
    },
    "decision_forwarded": {
        "en": "Decision forwarded.",
        "zh": "决定已提交。",
    },
    "confirmation_failed": {
        "en": "Confirmation failed.",
        "zh": "确认失败。",
    },
    "assistant_turn_failed": {
        "en": "Assistant turn failed.",
        "zh": "助手执行失败。",
    },
    "waiting_confirmation": {
        "en": "ArgusAI is waiting for confirmation. Send /status for details.",
        "zh": "ArgusAI 正在等待确认。发送 /status 查看详情。",
    },
    # Session-wide tool authorization.
    "auto_allow_requires_configure": {
        "en": "Session-wide tool authorization requires the configure permission.",
        "zh": "整会话工具放行需要 configure 权限。",
    },
    "auto_allow_not_allowed": {
        "en": "Session-wide tool authorization is not allowed for this identity.",
        "zh": "该身份不允许开启整会话工具放行。",
    },
    "session_tools_not_allowed": {
        "en": "Session tool configuration is not allowed for this identity.",
        "zh": "该身份不允许修改会话工具授权。",
    },
    "session_tools_unavailable": {
        "en": "Session tool configuration is unavailable.",
        "zh": "会话工具授权设置当前不可用。",
    },
    "session_tools_disabled": {
        "en": "Session tool authorization disabled.",
        "zh": "已关闭整会话工具放行。",
    },
    "auto_allow_failed": {
        "en": "Session tool authorization failed.",
        "zh": "开启整会话工具放行失败。",
    },
    "auto_allow_enabled": {
        "en": "All tools are allowed for this conversation. Send /allow off to stop.",
        "zh": "本会话已放行全部工具。发送 /allow off 关闭。",
    },
    "auto_allow_batch_partial": {
        "en": "Session-wide authorization is enabled, but the current pending batch was not resumed: {error}.",
        "zh": "整会话放行已开启，但当前待确认批次未能继续：{error}。",
    },
    "auto_allow_prompt": {
        "en": "Confirm session-wide tool authorization.",
        "zh": "请确认是否开启整会话工具放行。",
    },
    "auto_allow_title": {
        "en": "Allow all tools for this conversation?",
        "zh": "允许本会话的所有工具？",
    },
    "auto_allow_body": {
        "en": (
            "All current and future tool confirmations in this conversation, "
            "including sensitive tools, will be approved automatically. "
            "Send /allow off to stop."
        ),
        "zh": (
            "本会话中现有和后续的所有工具确认（包括高风险工具）都会被自动放行。"
            "发送 /allow off 可随时关闭。"
        ),
    },
    "auto_allow_label": {
        "en": "Allow for this session",
        "zh": "本会话全部放行",
    },
    "cancel_label": {
        "en": "Cancel",
        "zh": "取消",
    },
    "ready_mode_title": {
        "en": "Quick enable",
        "zh": "一键开启",
    },
    "ready_mode_body": {
        "en": (
            "Enable for this conversation:\n"
            "• auto-allow ordinary tools — shells, raw HTTP, sqlmap, Frida and "
            "desktop control still ask every time\n"
            "• /live on — mid-turn replies and cards\n"
            "• /search on — web search\n"
            "• /greenlight on — Greenlight Mode\n"
            "Not included: /allow all (high-risk tier). Send it yourself if needed."
        ),
        "zh": (
            "为本会话开启：\n"
            "• 普通工具自动放行 —— shell、原始 HTTP、sqlmap、Frida、"
            "桌面控制仍然每次询问\n"
            "• /live on —— 实时推送阶段性回复与卡片\n"
            "• /search on —— 联网搜索\n"
            "• /greenlight on —— 绿灯模式\n"
            "不包含：/allow all（高危工具），需要时请自行发送。"
        ),
    },
    "ready_mode_footer": {
        "en": "You can change any of these later with the matching /command.",
        "zh": "之后仍可用对应 /命令 单独开关。",
    },
    "ready_mode_enable": {
        "en": "Enable all",
        "zh": "一键全部开启",
    },
    "ready_mode_skip": {
        "en": "Not now",
        "zh": "暂时不用",
    },
    "ready_mode_enabled": {
        "en": (
            "Ready mode on: ordinary tools auto-allowed, live updates on, web search on, "
            "Greenlight Mode on. High-risk tools still ask."
        ),
        "zh": "已开启：普通工具自动放行、实时推送、联网搜索、绿灯模式。高危工具仍会询问。",
    },
    "ready_mode_partial": {
        "en": "Some ready-mode settings failed: {detail}",
        "zh": "部分一键设置失败：{detail}",
    },
    "ready_mode_not_allowed": {
        "en": "Ready mode is not allowed for this identity.",
        "zh": "该身份不允许使用一键开启。",
    },
    "ready_mode_unavailable": {
        "en": "Ready mode is unavailable.",
        "zh": "一键开启当前不可用。",
    },
    # Files.
    "file_unavailable": {
        "en": "Conversation file delivery is unavailable.",
        "zh": "会话文件下发当前不可用。",
    },
    "file_failed": {
        "en": "Conversation file delivery failed.",
        "zh": "会话文件下发失败。",
    },
    "file_sending": {
        "en": "Sending {name}.",
        "zh": "正在发送 {name}。",
    },
    "file_none": {
        "en": "This conversation has no deliverable files yet.",
        "zh": "当前会话还没有可下发的文件。",
    },
    "file_none_match": {
        "en": 'No sandbox files match "{query}".',
        "zh": "没有匹配“{query}”的沙盒文件。",
    },
    "file_list_title": {
        "en": "Conversation files:",
        "zh": "会话文件：",
    },
    "file_list_title_paged": {
        "en": "Conversation files · page {page}/{page_count}",
        "zh": "会话文件 · 第 {page}/{page_count} 页",
    },
    "file_list_title_query": {
        "en": 'Conversation files · page {page}/{page_count} · search "{query}"',
        "zh": "会话文件 · 第 {page}/{page_count} 页 · 搜索“{query}”",
    },
    "file_list_row": {
        "en": "{index}. {path} ({size} bytes)",
        "zh": "{index}. {path}（{size} 字节）",
    },
    "file_list_hint": {
        "en": "Tap a number to download, or send /file <number|path>. Use /file search <keyword> to filter.",
        "zh": "点编号下载，或发送 /file <编号|路径>。用 /file search <关键词> 筛选。",
    },
    "files_previous": {
        "en": "‹ Previous",
        "zh": "‹ 上一页",
    },
    "files_next": {
        "en": "Next ›",
        "zh": "下一页 ›",
    },
    # Actions.
    "action_other_conversation": {
        "en": "This action belongs to a different conversation.",
        "zh": "该操作属于另一个会话。",
    },
    "action_cancelled": {
        "en": "Cancelled.",
        "zh": "已取消。",
    },
    "action_unsupported": {
        "en": "Unsupported or expired channel action.",
        "zh": "不支持或已过期的渠道操作。",
    },
    "action_invalid": {
        "en": "channel action is invalid or expired",
        "zh": "该渠道操作无效或已过期",
    },
    "action_other_identity": {
        "en": "channel action belongs to another identity",
        "zh": "该渠道操作属于另一个身份",
    },
    "group_invalid": {
        "en": "channel action group is invalid or expired",
        "zh": "该批量选择已失效或过期",
    },
    "group_other_identity": {
        "en": "channel action group belongs to another identity",
        "zh": "该批量选择属于另一个身份",
    },
    "batch_invalid_option": {
        "en": "invalid batch decision option",
        "zh": "批量选择的选项无效",
    },
    "batch_incomplete": {
        "en": "batch decision is incomplete: {missing}",
        "zh": "批量选择尚未完成：{missing}",
    },
    "selection_updated": {
        "en": "Selection updated. Review the choices, then submit.",
        "zh": "选择已更新。确认无误后点击提交。",
    },
    # Cards.
    "card_allow_once": {
        "en": "Allow once",
        "zh": "允许一次",
    },
    "card_deny": {
        "en": "Deny",
        "zh": "拒绝",
    },
    "card_submit": {
        "en": "Submit",
        "zh": "提交",
    },
    "card_choose_options": {
        "en": "Choose options",
        "zh": "请选择",
    },
    "card_plan": {
        "en": "Plan",
        "zh": "计划",
    },
    "card_plan_progress": {
        "en": "{done}/{total} done · {pending} pending",
        "zh": "已完成 {done}/{total} · 待处理 {pending}",
    },
    "todo_status_pending": {
        "en": "pending",
        "zh": "待办",
    },
    "todo_status_in_progress": {
        "en": "in progress",
        "zh": "进行中",
    },
    "todo_status_completed": {
        "en": "done",
        "zh": "已完成",
    },
    "todo_status_cancelled": {
        "en": "cancelled",
        "zh": "已取消",
    },
    "todo_status_blocked": {
        "en": "blocked",
        "zh": "已阻塞",
    },
    "card_run_tool": {
        "en": "Run {name}?",
        "zh": "运行 {name}？",
    },
    "card_local_confirmation": {
        "en": "This sensitive action can only be allowed in the local ArgusAI GUI.",
        "zh": "该高风险操作只能在本地 ArgusAI 界面中确认。",
    },
    "card_selected": {
        "en": " — selected: {value}",
        "zh": " — 已选：{value}",
    },
    "done": {
        "en": "Done.",
        "zh": "完成。",
    },
    # Runtime.
    "rate_limited": {
        "en": "Too many messages. Please wait before trying again.",
        "zh": "消息过于频繁，请稍后再试。",
    },
    "delivery_failed": {
        "en": "Delivery failed: {reason}",
        "zh": "投递失败：{reason}",
    },
    # Interjections and side questions.
    "interjected": {
        "en": (
            "This conversation is mid-task. I asked it to wrap up the current "
            "step and will run your message next, then send the answer here."
        ),
        "zh": (
            "这个会话正在跑任务。已让它收尾当前这一步，随后就处理你这条，"
            "答复会发到这里。"
        ),
    },
    "interrupted": {
        "en": (
            "Stopped the task that was running and picked up your new message. "
            "The answer will be sent here."
        ),
        "zh": "已打断正在跑的任务，开始处理你这条新消息，答复会发到这里。",
    },
    "side_title": {
        "en": "Side question",
        "zh": "侧问",
    },
    "side_footer": {
        "en": "Read-only answer. Not saved to this conversation; the run kept going.",
        "zh": "只读回答。不写入会话历史，正在跑的任务也没有被打断。",
    },
    "usage_btw": {
        "en": (
            "Usage: /btw <question>. It answers from the current conversation "
            "without saving anything or interrupting a running task."
        ),
        "zh": (
            "用法：/btw <问题>。它基于当前对话回答一次，不写入历史，"
            "也不打断正在跑的任务。"
        ),
    },
    "side_unavailable": {
        "en": "Side questions are not available here.",
        "zh": "这里暂时用不了侧问。",
    },
    "side_failed": {
        "en": "The side question failed.",
        "zh": "侧问失败了。",
    },
    "side_empty": {
        "en": "The side agent had nothing to say. Try asking differently.",
        "zh": "侧问没给出内容，换个问法试试。",
    },
}


def _render_one(key: str, locale: str, params: dict[str, Any]) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        raise KeyError(f"unknown channel message key: {key}")
    template = entry.get(locale) or entry[DEFAULT_LOCALE]
    if not params:
        return template
    resolved = {
        name: (
            _render_one(value.key, locale, value.params)
            if isinstance(value, Frag)
            else value
        )
        for name, value in params.items()
    }
    return template.format(**resolved)


def channel_text(
    key: str,
    locale: str = DEFAULT_LOCALE,
    *,
    inline: bool = False,
    **params: Any,
) -> str:
    """Return one catalog entry for ``locale``, falling back to English.

    In bilingual mode both languages are emitted, Chinese first, separated by a
    blank line when either half spans multiple lines so the block stays legible.
    ``inline`` keeps the pair on one line regardless of length, for table-like
    layouts (``/help``) where every row must stay a row.
    """
    mode = normalize_locale(locale)
    if mode != BILINGUAL:
        return _render_one(key, mode, params)
    if key in SINGLE_LOCALE_KEYS:
        return _render_one(key, BILINGUAL_PRIMARY, params)
    primary = _render_one(key, BILINGUAL_PRIMARY, params)
    secondary = _render_one(key, "en" if BILINGUAL_PRIMARY == "zh" else "zh", params)
    if primary.strip() == secondary.strip():
        return primary
    # Short labels fit on one line; long or multi-line blocks need a blank line
    # so the two languages stay separable in a chat bubble.
    if "\n" in primary or "\n" in secondary:
        joiner = "\n\n"
    elif inline:
        joiner = " / "
    elif max(len(primary), len(secondary)) <= 24 and "/" not in primary + secondary:
        # A slash separator would blend into commands and page counters.
        joiner = " / "
    elif max(len(primary), len(secondary)) > 80:
        joiner = "\n\n"
    else:
        joiner = "\n"
    return f"{primary}{joiner}{secondary}"
