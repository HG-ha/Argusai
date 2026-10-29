"""Application settings persistence — user-level ``settings.json``.

``SettingsManager`` loads/saves a JSON settings file under the stable
per-user app root managed by :mod:`argusai.core.paths`. Legacy
``config/settings.yaml`` is imported once when no JSON file exists.
"""

from __future__ import annotations

import logging
import json
import os
import re
import hashlib
import tempfile
import threading
from dataclasses import dataclass, field

from argusai.core.workspace.user_profile import UserHabitsProfile
from pathlib import Path
from typing import Any, Callable

from argusai.core.ai import AIEndpoint, AuxiliaryAIEndpoint
from argusai.core.ai_thinking import DEFAULT_EFFORT, normalize_thinking_effort
from argusai.core.agent_runtimes import normalize_agent_runtimes
from argusai.core.assistant.tool_policy import DEFAULT_TOOL_ALWAYS_ALLOW, normalize_tool_always_allow
from argusai.core.infra.logging_setup import LoggingSettings
from argusai.core.security.mcp_provision import normalize_provenance
from argusai.core.paths import (
    PathManager,
    SUBDIR_KNOWLEDGE,
    SUBDIR_POC_PAGES,
    SUBDIR_REVERSE_JOBS,
    SUBDIR_SESSIONS,
    SUBDIR_TASKS,
    default_app_root,
)

log = logging.getLogger("argusai.settings")

LEGACY_SETTINGS_PATH = Path("config") / "settings.yaml"
LEGACY_SESSIONS_DIR = "config/assistant_sessions"
SETTINGS_BACKUP_COUNT = 10

# Every SettingsManager in this process participates in the same reentrant
# critical section. Several GUI/background subsystems share settings.json, and
# some compatibility methods call load/save from inside a larger update.
_SETTINGS_LOCK = threading.RLock()

DEFAULT_ASSISTANT_AUTO_ARCHIVE_TOOLS = [
    "http_curl",
    "http_request_impersonate",
    "raw_http_send",
    "browser_use_tab",
    "browser_trace",
    "browser_probe",
    "sqli_probe_param",
]


MCP_TRANSPORTS = ("stdio", "streamable_http", "sse")
MCP_RISK_LEVELS = ("low", "medium-low", "medium", "high")
MCP_DEFAULT_RISK = "high"
MCP_DEFAULT_MAX_TIMEOUT = 60
MCP_DEFAULT_MAX_RESULT_BYTES = 200_000
MCP_RESULT_BYTES_HARD_CAP = 2_000_000

# Bare command names that get vendor-Node resolution at launch time.
MCP_NODE_LAUNCH_NAMES = ("node", "npm", "npx", "yarn", "pnpm", "pnpx")


def _mcp_server_id(name: str, command: str, args: list[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "mcp").strip().lower()).strip("-")
    base = base or "mcp"
    digest = hashlib.sha1(json.dumps([command, args], ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _normalize_mcp_url_id(name: str, url: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "mcp").strip().lower()).strip("-")
    base = base or "mcp"
    digest = hashlib.sha1(json.dumps([url], ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _normalize_mcp_risk(value: Any, *, default: str = MCP_DEFAULT_RISK) -> str:
    text = str(value or "").strip().lower()
    return text if text in MCP_RISK_LEVELS else default


def _normalize_mcp_tool_names(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[\s,;]+", value)
    elif isinstance(value, list):
        items = value
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _normalize_mcp_tool_risks(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        name = str(k or "").strip()
        if not name:
            continue
        risk = _normalize_mcp_risk(v, default="")
        if risk:
            out[name] = risk
    return out


def normalize_mcp_policy(value: Any) -> dict[str, Any]:
    """Normalize the per-server trust policy with safe defaults.

    The policy is what Loom22 §5.5 calls the ``ExternalToolPolicy``:
    a per-server allow/deny list, default risk, auto-approve switch,
    timeout and result-size limits. Defaults bias toward "treat third
    party MCP servers as untrusted" — the user can relax this per
    server in the UI.
    """
    if not isinstance(value, dict):
        value = {}
    return {
        "default_risk": _normalize_mcp_risk(value.get("default_risk"), default=MCP_DEFAULT_RISK),
        "allow_tools": _normalize_mcp_tool_names(value.get("allow_tools")),
        "deny_tools": _normalize_mcp_tool_names(value.get("deny_tools")),
        "tool_risks": _normalize_mcp_tool_risks(value.get("tool_risks")),
        "allow_auto_approve": bool(value.get("allow_auto_approve", False)),
        "require_confirmation_for_all": bool(value.get("require_confirmation_for_all", True)),
        "max_timeout_seconds": _clamp_int(
            value.get("max_timeout_seconds"),
            default=MCP_DEFAULT_MAX_TIMEOUT,
            lo=1,
            hi=600,
        ),
        "max_result_bytes": _clamp_int(
            value.get("max_result_bytes"),
            default=MCP_DEFAULT_MAX_RESULT_BYTES,
            lo=1024,
            hi=MCP_RESULT_BYTES_HARD_CAP,
        ),
        "allow_insecure_http": bool(value.get("allow_insecure_http", False)),
    }


def _normalize_mcp_transport(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in MCP_TRANSPORTS:
        return text
    return "stdio"


def _normalize_mcp_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        key = str(k or "").strip()
        if not key:
            continue
        out[key] = "" if v is None else str(v)
    return out


def normalize_mcp_servers(value: Any) -> list[dict[str, Any]]:
    """Return JSON-clean MCP server configs safe for settings storage.

    Backward compatible: legacy entries without ``transport`` default to
    ``stdio``. Each entry always carries the union of stdio + HTTP/SSE
    fields plus a ``policy`` dict, so consumers don't need to defensively
    sprinkle ``.get()`` everywhere.
    """
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        transport = _normalize_mcp_transport(raw.get("transport"))
        name = str(raw.get("name") or raw.get("id") or "MCP Server").strip() or "MCP Server"

        # Transport-specific required field check.
        command = str(raw.get("command") or "").strip()
        url = str(raw.get("url") or "").strip()
        if transport == "stdio":
            if not command:
                # Legacy entries with no command get dropped to keep
                # ``settings.json`` clean — same as the old behaviour.
                continue
        else:
            if not url:
                continue

        raw_args = raw.get("args") or []
        if isinstance(raw_args, str):
            args = [part for part in re.split(r"\s+", raw_args.strip()) if part]
        elif isinstance(raw_args, list):
            args = [str(item) for item in raw_args if str(item)]
        else:
            args = []

        if transport == "stdio":
            sid_seed = _mcp_server_id(name, command, args)
        else:
            sid_seed = _normalize_mcp_url_id(name, url)
        sid = str(raw.get("id") or "").strip() or sid_seed
        sid = re.sub(r"[^a-zA-Z0-9_.-]+", "-", sid).strip("-._") or sid_seed
        if sid in seen:
            continue
        seen.add(sid)

        raw_env = raw.get("env") or {}
        env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
        headers = _normalize_mcp_headers(raw.get("headers"))
        provenance = normalize_provenance(raw.get("provenance"))
        item = {
            "id": sid,
            "name": name,
            "enabled": bool(raw.get("enabled", True)),
            "transport": transport,
            "command": command,
            "args": args,
            "env": env,
            "cwd": str(raw.get("cwd") or "").strip(),
            "url": url,
            "headers": headers,
            "timeout_seconds": _clamp_int(raw.get("timeout_seconds"), default=20, lo=1, hi=120),
            "notes": str(raw.get("notes") or "").strip(),
            "policy": normalize_mcp_policy(raw.get("policy")),
        }
        if provenance:
            item["provenance"] = provenance
        out.append(item)
    return out


def default_user_data_dir() -> Path:
    """Backward-compatible alias for the stable per-user app root."""
    return default_app_root()


def default_data_root() -> str:
    """The single OS-canonical root all persistent data lives under.

    Everything else (tasks/sessions/poc/knowledge) is derived as a
    subdirectory of this root.
    """
    return str(PathManager().data_root)


def default_settings_path() -> Path:
    """Stable per-user settings path, independent of the process cwd."""
    return PathManager().settings_path


def normalize_data_root(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return default_data_root()
    return text


_ALLOWED_DEFAULT_SHELLS = frozenset({"auto", "powershell", "pwsh", "cmd", "bash", "sh"})


def normalize_default_shell(value: Any) -> str:
    """Return a safe assistant.default_shell value."""
    text = str(value or "auto").strip().lower()
    if text in {"", "auto", "default"}:
        return "auto"
    if text in {"windows-powershell", "powershell.exe"}:
        return "powershell"
    if text in {"powershell7", "pwsh.exe"}:
        return "pwsh"
    if text in _ALLOWED_DEFAULT_SHELLS:
        return text
    return "auto"


def _subdir(root: str, name: str) -> str:
    """Return ``<root>/<name>`` as a normalised string path."""
    return str(Path(root) / name)


# Convenience wrappers — kept so existing callers / tests that imported
# ``default_tasks_dir()`` etc. keep working. All of them now resolve as
# "<data_root>/<subdir>" against the OS-default data root.

def default_sessions_dir() -> str:
    return _subdir(default_data_root(), SUBDIR_SESSIONS)


def default_poc_pages_dir() -> str:
    return _subdir(default_data_root(), SUBDIR_POC_PAGES)


def default_knowledge_base_dir() -> str:
    return _subdir(default_data_root(), SUBDIR_KNOWLEDGE)


def default_tasks_dir() -> str:
    return _subdir(default_data_root(), SUBDIR_TASKS)


def default_reverse_jobs_dir() -> str:
    return _subdir(default_data_root(), SUBDIR_REVERSE_JOBS)


# --- legacy normalisers ------------------------------------------------
# Pre-refactor callers (gui.py, third-party scripts) imported these.
# They now collapse to a no-op: anything that resolves to an empty string
# falls back to the canonical default. New code should NOT use them —
# configure ``data_root`` and read ``settings.tasks_dir`` etc. instead.

def normalize_sessions_dir(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.replace("\\", "/") == LEGACY_SESSIONS_DIR:
        return default_sessions_dir()
    return text


def normalize_poc_pages_dir(value: Any) -> str:
    text = str(value or "").strip()
    return text or default_poc_pages_dir()


def normalize_tasks_dir(value: Any) -> str:
    text = str(value or "").strip()
    return text or default_tasks_dir()


def normalize_knowledge_base_dir(value: Any) -> str:
    text = str(value or "").strip()
    return text or default_knowledge_base_dir()


def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    """Coerce ``value`` to int and clamp into ``[lo, hi]``.

    Unlike ``int(x or default)`` this DOES NOT replace a legitimate ``0``
    with the default — important for "0 = disabled" semantics
    (e.g. ``kb_trash_retention_days=0`` = never auto-purge).
    """
    try:
        if value is None or value == "":
            return int(default)
        n = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), n))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class AppSettings:
    """Parsed application settings.

    Storage layout: there is exactly ONE user-configurable path
    (``data_root``). The four legacy storage directories — tasks,
    assistant sessions, PoC pages, knowledge base — are computed as
    fixed-name subdirectories under it. This keeps the settings UI
    simple and matches how 99% of users actually want to organise
    persistent state ("just put all my ArgusAI data in this one folder").
    """

    ai_enabled: bool = True
    ai_endpoints: list[AIEndpoint] = field(default_factory=list)
    ai_auxiliary_vision: AuxiliaryAIEndpoint = field(default_factory=AuxiliaryAIEndpoint)
    ai_auxiliary_asr: AuxiliaryAIEndpoint = field(default_factory=AuxiliaryAIEndpoint)
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8866
    proxy_scope: list[str] = field(default_factory=list)
    proxy_deny_hosts: list[str] = field(default_factory=list)
    proxy_intercept_mode: str = "all"
    proxy_mime_allow: list[str] = field(default_factory=list)
    proxy_use_system_proxy: bool = False
    # Decryption rules (keys included) — user-level secrets, see proxy_decrypt.
    proxy_decrypt_rules: list[dict[str, Any]] = field(default_factory=list)
    # Active rewrite rules — turn the passive proxy into an active one, see
    # proxy_rewrite. Off unless the user (or a confirmed Agent action) adds one.
    proxy_rewrite_rules: list[dict[str, Any]] = field(default_factory=list)
    data_root: str = field(default_factory=default_data_root)
    # How long entries linger in ``knowledge/_trash`` before the next app
    # start hard-deletes them. ``0`` = never auto-purge (keep forever).
    kb_trash_retention_days: int = 30
    oob_callback_base: str = ""
    max_active_requests: int = 80
    max_time_delay_seconds: int = 5
    enable_protocol_tests: bool = False
    browser_channel: str = ""
    browser_executable_path: str = ""
    # Default viewport/window size for the managed automation browser (incl.
    # headless). Applied when the chosen fingerprint profile does not pin its
    # own viewport; fingerprint presets keep theirs for identity integrity.
    browser_window_width: int = 1920
    browser_window_height: int = 1080
    # Optional absolute paths for reverse-engineering CLIs (jadx, apktool, frida, …).
    toolchain_paths: dict[str, str] = field(default_factory=dict)
    # Optional custom mirrors are tried before the built-in verified sources.
    # Values are newline/comma separated URLs or URL templates.
    runtime_mirrors: dict[str, str] = field(default_factory=dict)
    # Out-of-band interaction (Interactsh) settings. ``mode`` is one of
    # ``auto`` (use ProjectDiscovery public servers, register lazily on first
    # use) and ``disabled`` (legacy stub behaviour — AI must paste events
    # manually). ``server`` overrides the default server choice when set.
    oob_mode: str = "auto"
    oob_server: str = ""
    oob_poll_interval: float = 5.0
    # Legacy runtime-tuning keys. They remain in the settings schema and
    # serialization so existing settings.json files round-trip safely; the
    # Loom22 runtime now owns these policies and does not consume the values.
    assistant_max_agent_rounds: int = 200
    assistant_max_auto_continue: int = 10
    assistant_max_toolless_continue_decisions: int = 0
    assistant_max_budget_restarts: int = 10
    # Legacy persisted value; the deadlock threshold is framework-owned.
    assistant_round_signature_hard_stop: int = 4
    # Loop detector (result-aware, Loom22 §11). ``poll_no_progress``: same
    # tool returning byte-identical results this many times in a row earns a
    # one-shot guidance hint; 2x escalates to a user_decision card. 0 disables.
    assistant_poll_no_progress_stop: int = 4
    # ``ping_pong``: alternating between exactly two (tool,args) states
    # (A-B-A-B), counted in calls; hint at this many, stop at 2x. 0 disables.
    assistant_ping_pong_stop: int = 6
    # Taxonomy-driven guards (Loom22 §11.2). ``same_tool_failure``: same tool
    # failing this many times in a row (args/error may vary) earns a one-shot
    # hint; 2x escalates to a user_decision card. 0 disables.
    assistant_same_tool_failure_stop: int = 3
    # ``mutating_repetition``: a mutating tool succeeding with the identical
    # call signature this many times; hint at this many, stop at 2x. 0 disables.
    assistant_mutating_repetition_stop: int = 3
    # Loom22 §3.1 — Anthropic extended-thinking budget (tokens).
    assistant_thinking_budget_tokens: int = 1024
    # Unified thinking intensity (auto / none / minimal / low / medium / high / xhigh / max).
    assistant_thinking_effort: str = DEFAULT_EFFORT
    # Emergency wall-clock boundary for one interactive agent epoch. Progress
    # guards remain active independently; zero explicitly disables the limit.
    assistant_turn_deadline_seconds: int = 604800
    assistant_desktop_notifications: bool = True
    # Signed event-trigger listener. Loopback by default; exposing it requires
    # an authenticated reverse proxy and an explicit settings change.
    assistant_cron_webhook_host: str = "127.0.0.1"
    assistant_cron_webhook_port: int = 8765
    # Loom22 §12.6 — proactive digest-before-drop when prompt estimate exceeds
    # this fraction of the context window. 0 = only hard budget triggers.
    assistant_auto_compact_ratio: float = 0.8
    # Legacy persisted values; retention is derived from the active context.
    assistant_tool_result_keep_last: int = 20
    assistant_tool_result_summary_cap: int = 500
    assistant_enable_web_search: bool = True
    # How many standing-team members may be queued or running in one team.
    assistant_team_max_parallel: int = 12
    # Preferred sandbox shell when run_shell_snippet/script omit ``shell``.
    # ``auto`` = platform default (Windows PowerShell / POSIX bash).
    assistant_default_shell: str = "auto"
    assistant_auto_archive_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ASSISTANT_AUTO_ARCHIVE_TOOLS))
    assistant_tool_always_allow: list[str] = field(default_factory=lambda: list(DEFAULT_TOOL_ALWAYS_ALLOW))
    # Composer-level Cursor-style auto-approve: tools, decision cards, todo plans.
    assistant_auto_approve: bool = False
    user_habits: UserHabitsProfile = field(default_factory=UserHabitsProfile)
    agent_runtimes: list[dict[str, Any]] = field(default_factory=lambda: normalize_agent_runtimes(None))
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    # Document parsing: local pypdf/python-docx always available; MinerU is optional cloud.
    document_parse_mineru_enabled: bool = False
    document_parse_mineru_api_key: str = ""
    document_parse_mineru_base_url: str = "https://mineru.net"
    document_parse_mineru_model_version: str = "vlm"
    document_parse_mineru_timeout_seconds: float = 300.0
    document_parse_max_output_chars: int = 200_000
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    # Last main-window placement (pywebview logical pixels). ``None`` x/y → center on next boot.
    gui_window_x: int | None = None
    gui_window_y: int | None = None
    gui_window_width: int = 0
    gui_window_height: int = 0
    gui_window_maximized: bool = False
    gui_close_to_tray: bool = True

    # ------------------------------------------------------------------
    # Derived subdirectories. These are exposed as attributes so the rest
    # of the codebase can keep reading ``settings.tasks_dir`` etc. without
    # caring whether they come from a single root or were independently
    # configured under the old schema.

    @property
    def tasks_dir(self) -> str:
        return _subdir(self.data_root, SUBDIR_TASKS)

    @property
    def sessions_dir(self) -> str:
        return _subdir(self.data_root, SUBDIR_SESSIONS)

    @property
    def poc_pages_dir(self) -> str:
        return _subdir(self.data_root, SUBDIR_POC_PAGES)

    @property
    def knowledge_base_dir(self) -> str:
        return _subdir(self.data_root, SUBDIR_KNOWLEDGE)

    @property
    def reverse_jobs_dir(self) -> str:
        return _subdir(self.data_root, SUBDIR_REVERSE_JOBS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ai": {
                "enabled": self.ai_enabled,
                "endpoints": [ep.to_dict() for ep in self.ai_endpoints],
                "auxiliary": {
                    "vision": self.ai_auxiliary_vision.to_dict(),
                    "asr": self.ai_auxiliary_asr.to_dict(),
                },
            },
            "proxy": {
                "host": self.proxy_host,
                "port": self.proxy_port,
                "scope": self.proxy_scope,
                "deny_hosts": self.proxy_deny_hosts,
                "intercept_mode": self.proxy_intercept_mode,
                "mime_allow": self.proxy_mime_allow,
                "use_system_proxy": self.proxy_use_system_proxy,
                "decrypt_rules": [dict(rule) for rule in self.proxy_decrypt_rules],
                "rewrite_rules": [dict(rule) for rule in self.proxy_rewrite_rules],
            },
            "data_root": self.data_root,
            # Derived paths are included for inspection (e.g. the
            # frontend renders them read-only) but they are NOT a source
            # of truth — saving will only read ``data_root`` back.
            "tasks_dir": self.tasks_dir,
            "sessions_dir": self.sessions_dir,
            "poc_pages_dir": self.poc_pages_dir,
            "knowledge_base_dir": self.knowledge_base_dir,
            "reverse_jobs_dir": self.reverse_jobs_dir,
            "kb_trash_retention_days": int(self.kb_trash_retention_days),
            "security": {
                "oob_callback_base": self.oob_callback_base,
                "max_active_requests": self.max_active_requests,
                "max_time_delay_seconds": self.max_time_delay_seconds,
                "enable_protocol_tests": self.enable_protocol_tests,
            },
            "browser": {
                "channel": self.browser_channel,
                "executable_path": self.browser_executable_path,
                "window_width": self.browser_window_width,
                "window_height": self.browser_window_height,
            },
            "toolchain": {
                "paths": {k: v for k, v in sorted(self.toolchain_paths.items()) if str(v).strip()},
            },
            "runtime": {
                "mirrors": {
                    key: str(self.runtime_mirrors.get(key) or "").strip()
                    for key in ("python", "node", "npm")
                    if str(self.runtime_mirrors.get(key) or "").strip()
                },
            },
            "oob": {
                "mode": self.oob_mode,
                "server": self.oob_server,
                "poll_interval": self.oob_poll_interval,
            },
            "assistant": {
                "max_agent_rounds": int(self.assistant_max_agent_rounds),
                "max_auto_continue": int(self.assistant_max_auto_continue),
                "max_toolless_continue_decisions": int(self.assistant_max_toolless_continue_decisions),
                "max_budget_restarts": int(self.assistant_max_budget_restarts),
                "round_signature_hard_stop": int(self.assistant_round_signature_hard_stop),
                "poll_no_progress_stop": int(self.assistant_poll_no_progress_stop),
                "ping_pong_stop": int(self.assistant_ping_pong_stop),
                "same_tool_failure_stop": int(self.assistant_same_tool_failure_stop),
                "mutating_repetition_stop": int(self.assistant_mutating_repetition_stop),
                "thinking_budget_tokens": int(self.assistant_thinking_budget_tokens),
                "thinking_effort": normalize_thinking_effort(self.assistant_thinking_effort),
                "turn_deadline_seconds": int(self.assistant_turn_deadline_seconds),
                "desktop_notifications": bool(self.assistant_desktop_notifications),
                "cron_webhook_host": str(
                    self.assistant_cron_webhook_host or "127.0.0.1"
                ),
                "cron_webhook_port": int(self.assistant_cron_webhook_port),
                "auto_compact_ratio": float(self.assistant_auto_compact_ratio),
                "tool_result_keep_last": int(self.assistant_tool_result_keep_last),
                "tool_result_summary_cap": int(self.assistant_tool_result_summary_cap),
                "enable_web_search": bool(self.assistant_enable_web_search),
                "team_max_parallel": int(self.assistant_team_max_parallel),
                "default_shell": normalize_default_shell(self.assistant_default_shell),
                "auto_archive_tools": list(self.assistant_auto_archive_tools or []),
                "tool_always_allow": list(self.assistant_tool_always_allow or []),
                "auto_approve": bool(self.assistant_auto_approve),
            },
            "mcp": {
                "servers": normalize_mcp_servers(self.mcp_servers),
            },
            "document_parse": {
                "max_output_chars": int(self.document_parse_max_output_chars),
                "mineru": {
                    "enabled": bool(self.document_parse_mineru_enabled),
                    "api_key": self.document_parse_mineru_api_key,
                    "base_url": self.document_parse_mineru_base_url,
                    "model_version": self.document_parse_mineru_model_version,
                    "timeout_seconds": float(self.document_parse_mineru_timeout_seconds),
                },
            },
            "agent_runtimes": normalize_agent_runtimes(self.agent_runtimes),
            "user_habits": self.user_habits.to_dict(),
            "logging": self.logging.normalised().to_dict(),
            "gui": {
                "close_to_tray": bool(self.gui_close_to_tray),
                "window": {
                    "x": self.gui_window_x,
                    "y": self.gui_window_y,
                    "width": int(self.gui_window_width or 0),
                    "height": int(self.gui_window_height or 0),
                    "maximized": bool(self.gui_window_maximized),
                },
            },
        }


def _default_content() -> dict[str, Any]:
    return {
        "ai": {
            "enabled": True,
            "endpoints": [],
            "auxiliary": {"vision": {}, "asr": {}},
        },
        "proxy": {"host": "127.0.0.1", "port": 8866, "scope": []},
        "data_root": default_data_root(),
        "kb_trash_retention_days": 30,
        "security": {
            "oob_callback_base": "",
            "max_active_requests": 80,
            "max_time_delay_seconds": 5,
            "enable_protocol_tests": False,
        },
        "browser": {
            "channel": "",
            "executable_path": "",
        },
        "toolchain": {
            "paths": {},
        },
        "runtime": {"mirrors": {}},
        "oob": {
            "mode": "auto",
            "server": "",
            "poll_interval": 5.0,
        },
        "assistant": {
            # Legacy keys retained for settings-file compatibility. Runtime
            # policy is framework-owned and these values are no longer exposed
            # as user-facing controls.
            "max_agent_rounds": 200,
            "max_auto_continue": 10,
            "max_toolless_continue_decisions": 0,
            "max_budget_restarts": 10,
            "tool_result_keep_last": 20,
            "tool_result_summary_cap": 500,
            "enable_web_search": True,
            "auto_archive_tools": list(DEFAULT_ASSISTANT_AUTO_ARCHIVE_TOOLS),
            "tool_always_allow": list(DEFAULT_TOOL_ALWAYS_ALLOW),
            "auto_approve": False,
        },
        "mcp": {"servers": []},
        "document_parse": {
            "max_output_chars": 200_000,
            "mineru": {
                "enabled": False,
                "api_key": "",
                "base_url": "https://mineru.net",
                "model_version": "vlm",
                "timeout_seconds": 300.0,
            },
        },
        "agent_runtimes": normalize_agent_runtimes(None),
        "user_habits": UserHabitsProfile().to_dict(),
        "logging": LoggingSettings().normalised().to_dict(),
    }


def _parse(raw: dict[str, Any]) -> AppSettings:
    ai = raw.get("ai") or {}
    proxy = raw.get("proxy") or {}
    security = raw.get("security") or {}
    browser = raw.get("browser") or {}
    toolchain = raw.get("toolchain") or {}
    toolchain_paths_raw = toolchain.get("paths") if isinstance(toolchain.get("paths"), dict) else {}
    toolchain_paths = {
        str(k).strip().lower(): str(v).strip()
        for k, v in toolchain_paths_raw.items()
        if str(k).strip() and str(v).strip()
    }
    runtime = raw.get("runtime") or {}
    runtime_mirrors_raw = runtime.get("mirrors") if isinstance(runtime.get("mirrors"), dict) else {}
    runtime_mirrors = {
        key: str(runtime_mirrors_raw.get(key) or "").strip()
        for key in ("python", "node", "npm")
        if str(runtime_mirrors_raw.get(key) or "").strip()
    }
    oob = raw.get("oob") or {}
    assistant = raw.get("assistant") or {}
    mcp = raw.get("mcp") or {}
    document_parse = raw.get("document_parse") if isinstance(raw.get("document_parse"), dict) else {}
    document_parse_mineru = (
        document_parse.get("mineru") if isinstance(document_parse.get("mineru"), dict) else {}
    )
    gui = raw.get("gui") or {}
    gui_window = gui.get("window") if isinstance(gui.get("window"), dict) else {}

    endpoints: list[AIEndpoint] = []
    for ep_raw in ai.get("endpoints") or []:
        if not isinstance(ep_raw, dict):
            continue
        endpoints.append(AIEndpoint.from_dict(ep_raw))

    auxiliary = ai.get("auxiliary") if isinstance(ai.get("auxiliary"), dict) else {}
    ai_auxiliary_vision = AuxiliaryAIEndpoint.from_dict(auxiliary.get("vision"))
    ai_auxiliary_asr = AuxiliaryAIEndpoint.from_dict(auxiliary.get("asr"))

    # Legacy compatibility: settings files written before the single-root
    # refactor stored four independent ``*_dir`` keys. We don't auto-migrate
    # their VALUE (the user opted not to migrate data), but if a legacy file
    # is missing ``data_root`` entirely we infer it from the common parent
    # of the four legacy paths if they all share one. Otherwise we just
    # fall back to the OS default.
    data_root_raw = raw.get("data_root")
    if data_root_raw:
        data_root = normalize_data_root(data_root_raw)
    else:
        legacy_paths = [
            str(raw.get("sessions_dir") or "").strip(),
            str(raw.get("poc_pages_dir") or "").strip(),
            str(raw.get("knowledge_base_dir") or "").strip(),
        ]
        legacy_paths = [p for p in legacy_paths if p]
        inferred = _infer_data_root(legacy_paths) if legacy_paths else ""
        data_root = inferred or default_data_root()

    return AppSettings(
        ai_enabled=bool(ai.get("enabled", True)),
        ai_endpoints=endpoints,
        ai_auxiliary_vision=ai_auxiliary_vision,
        ai_auxiliary_asr=ai_auxiliary_asr,
        proxy_host=str(proxy.get("host", "127.0.0.1")),
        proxy_port=int(proxy.get("port", 8866)),
        proxy_scope=list(proxy.get("scope") or []),
        proxy_deny_hosts=list(proxy.get("deny_hosts") or []),
        proxy_intercept_mode=str(proxy.get("intercept_mode") or "all"),
        proxy_mime_allow=list(proxy.get("mime_allow") or []),
        proxy_use_system_proxy=bool(proxy.get("use_system_proxy", False)),
        proxy_decrypt_rules=[dict(r) for r in (proxy.get("decrypt_rules") or []) if isinstance(r, dict)],
        proxy_rewrite_rules=[dict(r) for r in (proxy.get("rewrite_rules") or []) if isinstance(r, dict)],
        data_root=data_root,
        kb_trash_retention_days=_clamp_int(raw.get("kb_trash_retention_days"), default=30, lo=0, hi=3650),
        oob_callback_base=str(security.get("oob_callback_base") or ""),
        max_active_requests=max(1, min(int(security.get("max_active_requests") or 80), 1000)),
        max_time_delay_seconds=max(1, min(int(security.get("max_time_delay_seconds") or 5), 30)),
        enable_protocol_tests=bool(security.get("enable_protocol_tests", False)),
        browser_channel=str(browser.get("channel") or "").strip(),
        browser_executable_path=str(browser.get("executable_path") or "").strip(),
        browser_window_width=_clamp_int(browser.get("window_width"), default=1920, lo=320, hi=7680),
        browser_window_height=_clamp_int(browser.get("window_height"), default=1080, lo=240, hi=4320),
        toolchain_paths=toolchain_paths,
        runtime_mirrors=runtime_mirrors,
        oob_mode=_normalize_oob_mode(oob.get("mode")),
        oob_server=str(oob.get("server") or "").strip(),
        oob_poll_interval=max(1.0, min(float(oob.get("poll_interval") or 5.0), 60.0)),
        assistant_max_agent_rounds=_clamp_int(assistant.get("max_agent_rounds"), default=200, lo=0, hi=500),
        assistant_max_auto_continue=_clamp_int(assistant.get("max_auto_continue"), default=10, lo=0, hi=50),
        assistant_max_toolless_continue_decisions=_clamp_int(
            assistant.get("max_toolless_continue_decisions"),
            default=0,
            lo=0,
            hi=50,
        ),
        assistant_max_budget_restarts=_clamp_int(assistant.get("max_budget_restarts"), default=10, lo=0, hi=50),
        assistant_round_signature_hard_stop=_clamp_int(
            assistant.get("round_signature_hard_stop"),
            default=4,
            lo=0,
            hi=10,
        ),
        assistant_poll_no_progress_stop=_clamp_int(
            assistant.get("poll_no_progress_stop"),
            default=4,
            lo=0,
            hi=50,
        ),
        assistant_ping_pong_stop=_clamp_int(
            assistant.get("ping_pong_stop"),
            default=6,
            lo=0,
            hi=50,
        ),
        assistant_same_tool_failure_stop=_clamp_int(
            assistant.get("same_tool_failure_stop"),
            default=3,
            lo=0,
            hi=50,
        ),
        assistant_mutating_repetition_stop=_clamp_int(
            assistant.get("mutating_repetition_stop"),
            default=3,
            lo=0,
            hi=50,
        ),
        assistant_thinking_budget_tokens=_clamp_int(
            assistant.get("thinking_budget_tokens"),
            default=1024,
            lo=256,
            hi=64000,
        ),
        assistant_thinking_effort=normalize_thinking_effort(
            assistant.get("thinking_effort"),
            default=DEFAULT_EFFORT,
        ),
        assistant_turn_deadline_seconds=_clamp_int(
            assistant.get("turn_deadline_seconds"),
            default=604800,
            lo=0,
            hi=604800,
        ),
        assistant_desktop_notifications=bool(assistant.get("desktop_notifications", True)),
        assistant_cron_webhook_host=str(
            assistant.get("cron_webhook_host") or "127.0.0.1"
        ).strip() or "127.0.0.1",
        assistant_cron_webhook_port=_clamp_int(
            assistant.get("cron_webhook_port"),
            default=8765,
            lo=1,
            hi=65535,
        ),
        assistant_auto_compact_ratio=max(
            0.0,
            min(1.0, float(assistant.get("auto_compact_ratio", 0.8) or 0.0)),
        ),
        assistant_tool_result_keep_last=_clamp_int(assistant.get("tool_result_keep_last"), default=20, lo=0, hi=200),
        assistant_tool_result_summary_cap=_clamp_int(assistant.get("tool_result_summary_cap"), default=500, lo=0, hi=20000),
        assistant_enable_web_search=bool(assistant.get("enable_web_search", True)),
        assistant_team_max_parallel=_clamp_int(
            assistant.get("team_max_parallel"),
            default=12,
            lo=1,
            hi=64,
        ),
        assistant_default_shell=normalize_default_shell(assistant.get("default_shell")),
        assistant_auto_archive_tools=_normalize_tool_list(
            assistant.get("auto_archive_tools"),
            default=DEFAULT_ASSISTANT_AUTO_ARCHIVE_TOOLS,
        ),
        assistant_tool_always_allow=normalize_tool_always_allow(
            assistant.get("tool_always_allow"),
            default=DEFAULT_TOOL_ALWAYS_ALLOW,
        ),
        assistant_auto_approve=bool(assistant.get("auto_approve", False)),
        user_habits=UserHabitsProfile.from_dict(
            raw.get("user_habits") or raw.get("user_profile") or assistant.get("user_habits"),
        ),
        agent_runtimes=normalize_agent_runtimes(raw.get("agent_runtimes")),
        mcp_servers=normalize_mcp_servers(mcp.get("servers")),
        document_parse_mineru_enabled=bool(document_parse_mineru.get("enabled", False)),
        document_parse_mineru_api_key=str(document_parse_mineru.get("api_key") or "").strip(),
        document_parse_mineru_base_url=(
            str(document_parse_mineru.get("base_url") or "").strip() or "https://mineru.net"
        ),
        document_parse_mineru_model_version=(
            str(document_parse_mineru.get("model_version") or "").strip() or "vlm"
        ),
        document_parse_mineru_timeout_seconds=max(
            30.0,
            min(1800.0, float(document_parse_mineru.get("timeout_seconds", 300) or 300)),
        ),
        document_parse_max_output_chars=_clamp_int(
            document_parse.get("max_output_chars"),
            default=200_000,
            lo=1000,
            hi=2_000_000,
        ),
        logging=LoggingSettings.from_dict(raw.get("logging")),
        gui_window_x=_optional_int(gui_window.get("x")),
        gui_window_y=_optional_int(gui_window.get("y")),
        gui_window_width=max(0, _clamp_int(gui_window.get("width"), default=0, lo=0, hi=10000)),
        gui_window_height=max(0, _clamp_int(gui_window.get("height"), default=0, lo=0, hi=10000)),
        gui_window_maximized=bool(gui_window.get("maximized", False)),
        gui_close_to_tray=True if "close_to_tray" not in gui else bool(gui.get("close_to_tray")),
    )


def _infer_data_root(paths: list[str]) -> str:
    """Pick a sensible ``data_root`` from legacy independent storage paths.

    If every legacy path is of the form ``<root>/<subdir>`` for the SAME
    root and a known subdirectory name (sessions / poc_pages / knowledge),
    return that ``<root>``. Otherwise return ``""`` so the caller falls
    back to the OS default.
    """
    known_names = {SUBDIR_SESSIONS, SUBDIR_POC_PAGES, SUBDIR_KNOWLEDGE}
    parents: list[Path] = []
    for raw in paths:
        try:
            p = Path(raw)
        except (TypeError, ValueError):
            return ""
        if p.name in known_names:
            parents.append(p.parent)
        else:
            return ""
    if not parents:
        return ""
    first = parents[0]
    if all(p == first for p in parents[1:]):
        return str(first)
    return ""


def _normalize_oob_mode(value: Any) -> str:
    v = str(value or "auto").strip().lower()
    if v in {"auto", "disabled"}:
        return v
    return "auto"


def _normalize_tool_list(value: Any, *, default: list[str]) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = default
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("settings JSON root must be an object")
    return raw


def _json_text(raw: dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=False)


def _logical_settings_revision(raw: dict[str, Any]) -> str:
    """Optimistic-concurrency hash that ignores window placement.

    The main window writes ``gui.window`` on every resize (a few hundred ms).
    Hashing the whole file made Settings autosave fail whenever the user
    moved or snapped the window — the panel thought another editor had
    overwritten their work. Placement still persists; it just no longer
    participates in the version token.
    """
    payload = dict(raw)
    gui = payload.get("gui")
    if isinstance(gui, dict):
        payload["gui"] = {key: value for key, value in gui.items() if key != "window"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after publishing a temp file."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text durably, then atomically publish it in-place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verified = json.loads(temp_path.read_text(encoding="utf-8"))
        if not isinstance(verified, dict):
            raise ValueError("settings payload must be a JSON object")
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_legacy_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class SettingsCorruptionError(RuntimeError):
    """The primary settings file and all recoverable backups are invalid."""


class SettingsVersionConflictError(RuntimeError):
    """The caller attempted to save over a newer settings revision."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"settings changed since load: expected {expected}, found {actual}")


class SettingsManager:
    """Load / save / cache app settings from ``settings.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_settings_path()
        self._legacy_path: Path | None = None if path is not None else LEGACY_SETTINGS_PATH
        self._cache: AppSettings | None = None
        self._mtime: int = 0  # nanoseconds; track file replacement/modification
        self._recovery_notice: dict[str, str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _set_mtime_locked(self) -> None:
        try:
            self._mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._mtime = 0

    def _backup_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.bak.{index}")

    def _recover_from_backup_locked(self, primary_error: Exception) -> AppSettings | None:
        for index in range(1, SETTINGS_BACKUP_COUNT + 1):
            backup = self._backup_path(index)
            if not backup.exists():
                continue
            try:
                raw = _read_json(backup)
                settings = _parse(raw)
            except Exception as backup_error:
                log.warning("Ignoring invalid settings backup %s: %s", backup, backup_error)
                continue
            _atomic_write_text(self._path, _json_text(raw))
            self._set_mtime_locked()
            log.warning(
                "Recovered corrupt settings %s from %s after: %s",
                self._path,
                backup,
                primary_error,
            )
            self._recovery_notice = {
                "backup": str(backup),
                "reason": str(primary_error),
            }
            return settings
        return None

    def _load_locked(self) -> AppSettings:
        p = self._path
        if p.exists():
            try:
                raw = _read_json(p)
                settings = _parse(raw)
                self._set_mtime_locked()
            except Exception as e:
                recovered = self._recover_from_backup_locked(e)
                if recovered is None:
                    raise SettingsCorruptionError(
                        f"settings file is corrupt and no valid backup is available: {p}"
                    ) from e
                settings = recovered
        else:
            legacy = self._legacy_path
            if legacy is not None and legacy.exists():
                try:
                    raw = _read_legacy_yaml(legacy)
                    settings = _parse(raw)
                    log.info("Migrating legacy settings from %s to %s", legacy, p)
                except Exception as e:
                    raise SettingsCorruptionError(f"legacy settings file is corrupt: {legacy}") from e
            else:
                settings = _parse(_default_content())
            try:
                _atomic_write_text(p, _json_text(settings.to_dict()))
                self._set_mtime_locked()
                log.info("Created settings at %s", p)
            except Exception as e:
                log.warning("Could not create default settings file: %s", e)
        self._cache = settings
        return settings

    @staticmethod
    def _sync_runtime_settings(settings: AppSettings) -> None:
        try:
            from argusai.core.reverse.toolchain import sync_path_overrides_from_settings

            sync_path_overrides_from_settings(settings)
        except Exception:
            pass

    def load(self) -> AppSettings:
        """Read settings.json. Creates, migrates, or recovers it if needed."""
        with _SETTINGS_LOCK:
            settings = self._load_locked()
        self._sync_runtime_settings(settings)
        return settings

    def get(self) -> AppSettings:
        """Re-read from disk if file was modified since last read."""
        loaded = False
        with _SETTINGS_LOCK:
            p = self._path
            if self._cache is not None and p.exists():
                try:
                    loaded = p.stat().st_mtime_ns != self._mtime
                except OSError:
                    loaded = False
            elif self._cache is None:
                loaded = True
            if loaded:
                settings = self._load_locked()
            else:
                settings = self._cache
        if settings is None:
            # Defensive fallback for an impossible cache state.
            return self.load()
        if loaded:
            self._sync_runtime_settings(settings)
        return settings

    def _prepare_primary_for_save_locked(self) -> None:
        """Refuse to overwrite unrecoverable corruption; recover if possible."""
        if not self._path.exists():
            return
        try:
            _parse(_read_json(self._path))
        except Exception as error:
            if self._recover_from_backup_locked(error) is None:
                raise SettingsCorruptionError(
                    f"refusing to overwrite corrupt settings without a valid backup: {self._path}"
                ) from error

    def _rotate_backup_locked(self) -> None:
        if not self._path.exists():
            return
        raw = _read_json(self._path)
        _parse(raw)

        for index in range(SETTINGS_BACKUP_COUNT, 1, -1):
            source = self._backup_path(index - 1)
            if source.exists():
                os.replace(source, self._backup_path(index))
        _atomic_write_text(self._backup_path(1), _json_text(raw))

        # Clean up backups created by an older policy with a larger limit.
        prefix = f"{self._path.name}.bak."
        for candidate in self._path.parent.glob(f"{prefix}*"):
            try:
                index = int(candidate.name[len(prefix):])
            except ValueError:
                continue
            if index > SETTINGS_BACKUP_COUNT:
                try:
                    candidate.unlink()
                except OSError:
                    pass
        _fsync_directory(self._path.parent)

    def _save_locked(self, settings: AppSettings) -> None:
        content = _json_text(settings.to_dict())
        self._prepare_primary_for_save_locked()
        if self._path.exists():
            current = _read_json(self._path)
            _parse(current)
            if _json_text(current) == content:
                self._set_mtime_locked()
                self._cache = settings
                return
        self._rotate_backup_locked()
        _atomic_write_text(self._path, content)
        self._set_mtime_locked()
        self._cache = settings

    def _revision_locked(self) -> str:
        if not self._path.exists():
            return ""
        try:
            return _logical_settings_revision(_read_json(self._path))
        except Exception:
            return hashlib.sha256(self._path.read_bytes()).hexdigest()

    def revision(self) -> str:
        """Return the content revision of the latest valid settings file."""
        with _SETTINGS_LOCK:
            self._load_locked()
            return self._revision_locked()

    def snapshot(self) -> tuple[AppSettings, str]:
        """Return one settings object and its matching on-disk revision."""
        with _SETTINGS_LOCK:
            settings = self._load_locked()
            return _parse(settings.to_dict()), self._revision_locked()

    def metadata(self, *, consume_recovery: bool = False) -> dict[str, Any]:
        """Return frontend-safe persistence state for conflict/recovery notices."""
        with _SETTINGS_LOCK:
            self._load_locked()
            notice = dict(self._recovery_notice or {})
            if consume_recovery:
                self._recovery_notice = None
            return {
                "version": self._revision_locked(),
                "recovered": bool(notice),
                "recovery": notice,
            }

    def save(self, settings: AppSettings) -> None:
        with _SETTINGS_LOCK:
            self._save_locked(settings)
        self._sync_runtime_settings(settings)
        log.debug("Settings saved to %s", self._path)

    def update(
        self,
        updater: Callable[[AppSettings], AppSettings | None],
        *,
        expected_revision: str = "",
    ) -> AppSettings:
        """Atomically read the latest settings, mutate/replace them, and save.

        ``updater`` runs under the process-wide reentrant lock. It may mutate
        the supplied object and return ``None``, or return a replacement
        ``AppSettings``. No write occurs if it raises.
        """
        with _SETTINGS_LOCK:
            current = self._load_locked()
            actual_revision = self._revision_locked()
            if expected_revision and expected_revision != actual_revision:
                raise SettingsVersionConflictError(expected_revision, actual_revision)
            working = _parse(current.to_dict())
            replacement = updater(working)
            settings = working if replacement is None else replacement
            if not isinstance(settings, AppSettings):
                raise TypeError("settings updater must return AppSettings or None")
            self._save_locked(settings)
        self._sync_runtime_settings(settings)
        return settings

    def update_ai(
        self,
        endpoints: list[dict[str, Any]],
        enabled: bool | None = None,
    ) -> AppSettings:
        """Convenience: update AI settings and persist."""
        def _update(settings: AppSettings) -> None:
            settings.ai_endpoints = [AIEndpoint.from_dict(ep) for ep in endpoints]
            if enabled is not None:
                settings.ai_enabled = enabled

        return self.update(_update)
