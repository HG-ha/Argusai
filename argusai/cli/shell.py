"""Local slash-command shell for ``argusai chat`` (ops console).

These commands mirror the channel gateway vocabulary (``/status``, ``/model``,
…) but run against the in-process ``Api`` — they never open an Agent turn.
Unknown ``/…`` tokens are rejected locally (not forwarded as chat).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.prompt import Confirm
from rich.text import Text

from argusai.cli.cards import print_cmd_panel
from argusai.cli.locale import get_locale, label, set_locale, sync_from_api, t
from argusai.cli.ui import get_console
from argusai.core.channels.slash import (
    THINK_ACTIONS,
    match_slash_names,
    think_action,
    toggle_action,
)
from argusai.core.ai_thinking import thinking_scale_line
from argusai.core.token_usage_display import token_usage_status_rows

_QUIT = frozenset({"/quit", "/exit", "/q", "quit", "exit", ":q"})
_MODEL_RE = re.compile(r"^/(?:model|endpoint)(?:\s+(.+))?$", re.IGNORECASE)
_USE_RE = re.compile(r"^/use(?:\s+)([A-Za-z0-9._:-]+)$", re.IGNORECASE)
_NEW_RE = re.compile(r"^/new(?:\s+(.+))?$", re.IGNORECASE)
_AGENT_RE = re.compile(r"^/agents?$", re.IGNORECASE)
_BTW_RE = re.compile(r"^/(?:btw|side)(?:\s+([\s\S]+))?$", re.IGNORECASE)
_DECISION_RE = re.compile(
    r"^/(allow|deny)(?:\s+([A-Za-z0-9._:-]+))?$",
    re.IGNORECASE,
)
_LANG_RE = re.compile(r"^/lang(?:\s+([A-Za-z+-]{2,8}))?$", re.IGNORECASE)
_FILE_RE = re.compile(r"^/file(?:\s+(.+))?$", re.IGNORECASE)
_MONITOR_RE = re.compile(
    r"^/monitor(?:\s+(on|off|sys|tokens|agents|both|status))?$",
    re.IGNORECASE,
)
_SIDEBAR_RE = re.compile(
    r"^/sidebar(?:\s+(on|off|auto|status))?$",
    re.IGNORECASE,
)
_JOBS_RE = re.compile(r"^/jobs(?:\s+(\d+))?$", re.IGNORECASE)
_RESUME_RE = re.compile(r"^/resume(?:\s+(\S+)\s+([\s\S]+))?$", re.IGNORECASE)

_PAGE_SIZE = 8
_FILE_PAGE_SIZE = 8

# Canonical slash commands for prompt completion. ``None`` means free-form
# arguments, an empty tuple means no arguments, and a non-empty tuple contains
# the first argument's static choices.
CLI_COMMAND_COMPLETIONS: dict[str, tuple[str, ...] | None] = {
    "/help": (),
    "/status": (),
    "/tools": (),
    "/cancel": (),
    "/tasks": None,
    "/monitor": ("on", "off", "sys", "tokens", "agents", "both", "status"),
    "/sidebar": ("on", "off", "auto", "status"),
    "/jobs": None,
    "/resume": None,
    "/sessions": None,
    "/use": None,
    "/model": None,
    "/search": ("on", "off", "status"),
    "/think": ("on", "off", "status", "auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "/thinking": ("on", "off", "status", "auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "/greenlight": ("on", "off", "status"),
    "/allow": ("pending", "all", "off"),
    "/deny": ("pending", "all"),
    "/lang": ("zh", "en"),
    "/file": ("list", "search"),
    "/agents": (),
    "/team": ("list", "use", "leave", "chat", "stop", "pm", "greenlight"),
    "/new": None,
    "/btw": None,
    "/session": (),
    "/clear": (),
    "/quit": (),
}

# CLI-only extras appended under the shared channel help layout.
_CLI_HELP_EXTRA: tuple[tuple[str, str], ...] = (
    ("@<path>", "cli_extra_attachment"),
    ("@<role>", "cli_extra_mention"),
    ("/greenlight on <目标>", "cli_extra_greenlight_target"),
    ("/monitor [on|off|sys|tokens|agents|both]", "cli_extra_monitor"),
    ("/sidebar [on|off|auto]", "cli_extra_sidebar"),
    ("/jobs", "cli_extra_jobs"),
    ("/resume <job-id> <msg>", "cli_extra_resume"),
    ("/tasks [page]", "cli_extra_tasks"),
    ("/session", "cli_extra_session"),
    ("/clear", "cli_extra_clear"),
    ("/quit", "cli_extra_quit"),
    ("/team", "cli_extra_team"),
    ("/team use <name>", "cli_extra_team_use"),
    ("/team leave", "cli_extra_team_leave"),
    ("/team greenlight on", "cli_extra_team_greenlight"),
)

# Channel-only commands — kept out of the ops-console help map.
_CLI_HELP_SKIP_COMMANDS = frozenset({
    "/act <token>",
    "/live [on|off|status]",
})
_HELP_CMD_WIDTH = 34


@dataclass
class ShellResult:
    """Outcome of one local shell attempt."""

    handled: bool
    lines: list[str] = field(default_factory=list)
    session_id: str | None = None
    quit: bool = False
    clear: bool = False
    monitor_mode: str | None = None
    sidebar_mode: str | None = None
    refresh: bool = False
    # A Greenlight target is first stored as the session's accepted objective,
    # then replayed as an ordinary Agent turn — the CLI equivalent of
    # right-clicking a user message and selecting "Review bypass" in the GUI.
    forward_text: str | None = None
    # Enter or leave the standing-team room inside this same REPL.
    team_id: str | None = None
    team_name: str = ""
    team_member_id: str = ""
    team_member_role: str = ""
    leave_team: bool = False
    # ``/team stop`` with no name: the REPL stops whichever room is open.
    stop_current_team: bool = False
    # ``/team greenlight on|off|status`` with no name: apply to the open room.
    team_greenlight: str | None = None
    # History rows for the room just opened. The REPL paints these as chat
    # bubbles; ``lines`` stays a one-line banner.
    team_transcript: list[dict[str, Any]] | None = None


def looks_like_slash(text: str) -> bool:
    raw = (text or "").strip()
    return bool(raw.startswith("/") and len(raw) > 1)


def needs_tty_confirm(text: str) -> bool:
    """True when this slash command would block on an interactive y/n prompt.

    The fullscreen ops TUI owns stdin. Running ``Confirm.ask`` (or
    ``OpsApp.ask_confirm``) on the prompt_toolkit UI thread freezes the
    console — keys never reach the nested prompt. Callers must dispatch
    these commands on a worker and pass ``confirm=ops.ask_confirm``.
    """
    raw = (text or "").strip()
    decision = _DECISION_RE.fullmatch(raw)
    if decision is not None:
        verdict = str(decision.group(1) or "").lower()
        selector = str(decision.group(2) or "").strip().lower()
        return verdict == "allow" and selector == "all"
    model_match = _MODEL_RE.fullmatch(raw)
    if model_match is None:
        return False
    selector = str(model_match.group(1) or "").strip()
    return bool(selector) and selector.lower() not in {"list", "refresh", "reload"}


def _plain_prompt(message: str) -> str:
    try:
        return str(Text.from_markup(message).plain)
    except Exception:
        return message


def _prompt_confirm(
    message: str,
    *,
    default: bool,
    confirm: Callable[[str, bool], bool] | None = None,
    allow_non_tty: bool = False,
) -> bool:
    """Ask y/n without stealing stdin from the ops TUI.

    ``rich.prompt.Confirm`` paints ``[y/n]`` into the input dock while the
    application still holds the keyboard, so the user cannot answer. The ops
    console must pass ``confirm`` bound to ``OpsApp.ask_confirm``.
    """
    if confirm is not None:
        return bool(confirm(_plain_prompt(message), default))
    console = get_console()
    if not console.is_terminal:
        return allow_non_tty
    return bool(Confirm.ask(message, default=default))


def dispatch(
    api: Any,
    session_id: str,
    text: str,
    *,
    confirm: Callable[[str, bool], bool] | None = None,
) -> ShellResult:
    """Handle one user line if it is a local slash command."""
    raw = (text or "").strip()
    if not raw:
        return ShellResult(handled=False)

    # Keep shell locale aligned with Api (GUI / previous /lang).
    sync_from_api(api)

    lowered = raw.lower()
    if lowered in _QUIT:
        return ShellResult(handled=True, quit=True, lines=[label("session_closed")])

    if not looks_like_slash(raw):
        return ShellResult(handled=False)

    if lowered in {"/help", "/?", "help", "?"}:
        return ShellResult(handled=True, lines=_help_lines())
    if lowered == "/clear":
        return ShellResult(handled=True, clear=True)
    if lowered == "/session":
        return ShellResult(
            handled=True,
            lines=[t("status_row_id", session_id=session_id)],
        )
    if lowered == "/status":
        return ShellResult(handled=True, lines=_cmd_status(api, session_id))
    if lowered == "/tools":
        return ShellResult(handled=True, lines=_cmd_tools(api, session_id))
    if lowered == "/cancel":
        return ShellResult(handled=True, lines=_cmd_cancel(api, session_id))

    if lowered == "/tasks" or lowered.startswith("/tasks "):
        arg = raw[len("/tasks") :].strip()
        return ShellResult(handled=True, lines=_cmd_tasks(api, arg))

    monitor_match = _MONITOR_RE.fullmatch(raw)
    if monitor_match is not None:
        action = str(monitor_match.group(1) or "status").lower()
        return _cmd_monitor(action)

    sidebar_match = _SIDEBAR_RE.fullmatch(raw)
    if sidebar_match is not None:
        action = str(sidebar_match.group(1) or "status").lower()
        return _cmd_sidebar(action)

    jobs_match = _JOBS_RE.fullmatch(raw)
    if jobs_match is not None:
        page = int(jobs_match.group(1) or "1")
        return ShellResult(
            handled=True,
            lines=_cmd_jobs(api, session_id, page=page),
            refresh=True,
        )

    resume_match = _RESUME_RE.fullmatch(raw)
    if resume_match is not None:
        job_id = str(resume_match.group(1) or "").strip()
        message = str(resume_match.group(2) or "").strip()
        if not job_id or not message:
            return ShellResult(handled=True, lines=[label("resume_usage")])
        return ShellResult(
            handled=True,
            lines=_cmd_resume(api, session_id, job_id, message),
            refresh=True,
        )

    if lowered == "/sessions" or lowered.startswith("/sessions "):
        arg = raw[len("/sessions") :].strip()
        return ShellResult(handled=True, lines=_cmd_sessions(api, session_id, arg))

    use_match = _USE_RE.fullmatch(raw)
    if use_match is not None:
        return _cmd_use(api, session_id, use_match.group(1))

    model_match = _MODEL_RE.fullmatch(raw)
    if model_match is not None:
        return ShellResult(
            handled=True,
            lines=_cmd_model(
                api,
                session_id,
                str(model_match.group(1) or "").strip(),
                confirm=confirm,
            ),
        )

    search_match = match_slash_names(raw, ("/search",), glue_toggle=True)
    if search_match is not None:
        resolved = toggle_action(search_match[1])
        if resolved is None:
            return ShellResult(handled=True, lines=[t("usage_search")])
        action, _ = resolved
        return ShellResult(
            handled=True,
            lines=_cmd_option(api, session_id, "search", action),
        )

    think_match = match_slash_names(
        raw,
        ("/think", "/thinking"),
        glue_toggle=True,
        glue_values=THINK_ACTIONS,
    )
    if think_match is not None:
        action = think_action(think_match[1])
        if action is None:
            return ShellResult(handled=True, lines=[t("usage_think")])
        return ShellResult(
            handled=True,
            lines=_cmd_option(api, session_id, "think", action),
        )

    green_match = match_slash_names(raw, ("/greenlight",), glue_toggle=True)
    if green_match is not None:
        args = green_match[1]
        if args and args[0].casefold() == "of":
            args = ("off", *args[1:])
        resolved = toggle_action(args, allow_extra=True)
        if resolved is None:
            return ShellResult(handled=True, lines=[t("usage_greenlight")])
        action, goal_text = resolved
        if action == "on" and goal_text:
            return _cmd_greenlight_with_goal(api, session_id, goal_text)
        return ShellResult(
            handled=True,
            lines=_cmd_option(api, session_id, "greenlight", action),
        )

    decision = _DECISION_RE.fullmatch(raw)
    if decision is not None:
        verdict = decision.group(1).lower()
        selector = str(decision.group(2) or "").strip()
        return ShellResult(
            handled=True,
            lines=_cmd_decision(
                api, session_id, verdict, selector, confirm=confirm
            ),
        )

    lang_match = _LANG_RE.fullmatch(raw)
    if lang_match is not None:
        return ShellResult(
            handled=True,
            lines=_cmd_lang(api, str(lang_match.group(1) or "").strip()),
        )

    file_match = _FILE_RE.fullmatch(raw)
    if file_match is not None:
        return ShellResult(
            handled=True,
            lines=_cmd_file(api, session_id, str(file_match.group(1) or "").strip()),
        )

    if _AGENT_RE.fullmatch(raw) is not None:
        return ShellResult(handled=True, lines=_cmd_agents(api))

    if lowered == "/team" or lowered.startswith("/team ") or lowered.startswith("/team\n"):
        return _cmd_team(api, raw[len("/team"):].strip())

    new_match = _NEW_RE.fullmatch(raw)
    if new_match is not None:
        return _cmd_new(api, str(new_match.group(1) or "").strip())

    btw_match = _BTW_RE.fullmatch(raw)
    if btw_match is not None:
        question = str(btw_match.group(1) or "").strip()
        if not question:
            return ShellResult(handled=True, lines=[t("usage_btw")])
        return ShellResult(handled=True, lines=_cmd_btw(api, session_id, question))

    # Recognisable slash shape but unknown token — do not feed the Agent.
    return ShellResult(
        handled=True,
        lines=[
            label("unknown_cmd", cmd=raw.split()[0]),
            label("send_help"),
        ],
    )


def print_shell_result(result: ShellResult, *, tag: str = "CMD") -> None:
    print_cmd_panel(result.lines, title=f"[amber]▸ {label('tag_cmd')}[/]")


def _help_row(command: str, description: str) -> str:
    """One help line with aligned columns.

    Command usage strings contain ``[on|off]`` brackets. If those are left
    raw inside Rich markup, Rich eats them as tags and the description column
    drifts — escape *after* padding so visible width stays stable.
    """
    from rich.markup import escape

    cmd = f"{command:<{_HELP_CMD_WIDTH}}"
    return f"  [amber]{escape(cmd)}[/] [hud.dim]{escape(description)}[/]"


def _help_lines() -> list[str]:
    from argusai.core.channels.gateway import _HELP_LAYOUT

    lines: list[str] = [f"[hud]{t('help_title')}[/]"]
    for group, rows in _HELP_LAYOUT:
        lines.append(f"[hud]{t(group)}[/]")
        for command, key in rows:
            if command in _CLI_HELP_SKIP_COMMANDS:
                continue
            lines.append(_help_row(command, t(key)))
    lines.append(f"[hud]{label('cli_group')}[/]")
    for command, key in _CLI_HELP_EXTRA:
        lines.append(_help_row(command, label(key)))
    lines.append(f"[hud.dim]{t('help_plain')}[/]")
    lines.append(f"[hud.dim]{label('history_hint')}[/]")
    return lines


def _cmd_status(api: Any, session_id: str) -> list[str]:
    status = api.get_assistant_session(session_id)
    lines: list[str] = []
    if not status.get("ok"):
        err = status.get("error") or t("conversation_unavailable")
        lines.append(f"[fail]{err}[/]")
    else:
        name = str(status.get("name") or session_id)[:60]
        pending = [
            item
            for item in list(status.get("pending_tools") or [])
            if isinstance(item, dict)
        ]
        pending_names = ", ".join(
            str(item.get("name") or "tool")[:32] for item in pending[:4]
        )
        if len(pending) > 4:
            pending_names = f"{pending_names}, …"
        decl = status.get("engagement_declaration")
        green = isinstance(decl, dict) and bool(decl.get("active"))
        scope = "*"
        if green:
            raw_scope = decl.get("scope") if isinstance(decl, dict) else ""
            if isinstance(raw_scope, (list, tuple)):
                scope = ", ".join(str(item) for item in raw_scope[:4] if str(item).strip()) or "*"
            else:
                scope = str(raw_scope or "*").strip() or "*"
            scope = scope if len(scope) <= 48 else scope[:47].rstrip() + "…"
        runtime = status.get("runtime") if isinstance(status.get("runtime"), dict) else {}
        runtime_id = str(runtime.get("runtime_id") or "argusai-native").strip()
        agent_name = (
            "ArgusAI"
            if runtime_id in {"", "argusai-native", "native"}
            else runtime_id
        )
        try:
            message_count = int(status.get("message_count") or 0)
        except (TypeError, ValueError):
            message_count = 0
        try:
            memory_count = int(status.get("memory_count") or 0)
        except (TypeError, ValueError):
            memory_count = 0
        auto_allow = list(status.get("auto_allow_tools") or [])
        lines.extend(
            [
                t("status_row_name", name=name),
                t("status_row_id", session_id=session_id),
                t("status_row_messages", count=message_count),
                t("status_row_agent", name=agent_name[:60]),
                t(
                    "status_row_running"
                    if status.get("active_stream")
                    else "status_row_idle"
                ),
                t("status_row_greenlight_on", scope=scope)
                if green
                else t("status_row_greenlight_off"),
                t(
                    "status_row_search_on"
                    if bool(status.get("allow_web_search", True))
                    else "status_row_search_off"
                ),
                *(
                    [
                        t(
                            "status_row_think_effort",
                            effort=str(status.get("thinking_effort") or "high"),
                        ),
                        t(
                            "status_row_think_scale",
                            scale=thinking_scale_line(
                                True,
                                status.get("thinking_effort"),
                            ),
                        ),
                    ]
                    if bool(status.get("thinking_enabled"))
                    else [
                        t("status_row_think_off"),
                        t(
                            "status_row_think_scale",
                            scale=thinking_scale_line(
                                False,
                                status.get("thinking_effort"),
                            ),
                        ),
                    ]
                ),
                t("status_row_auto_allow", count=len(auto_allow)),
                t("status_row_pending", names=pending_names)
                if pending
                else t("status_row_pending_none"),
                t("status_row_todos", count=len(list(status.get("todos") or []))),
                t("status_row_memory", count=memory_count),
            ]
        )
        lines.extend(token_usage_status_rows(status, t))
        note = str(status.get("note") or "").strip()
        if note:
            lines.append(t("status_row_note", note=note[:80]))

    try:
        snap = api.get_app_snapshot()
    except Exception as exc:
        lines.append(f"[hud.dim]{exc}[/]")
        lines.append(f"[hud.dim]{t('status_overview_hint')}[/]")
        return lines

    if not isinstance(snap, dict):
        lines.append(f"[hud.dim]{t('status_overview_hint')}[/]")
        return lines

    lines.append(f"[hud]{label('system_section')}[/]")
    try:
        ep_list = api._channel_ai_endpoint("list", {"session_id": session_id})
    except Exception:
        ep_list = {}
    endpoints = [
        row for row in list((ep_list or {}).get("endpoints") or []) if isinstance(row, dict)
    ]
    active = next((row for row in endpoints if row.get("active")), None)
    if active:
        lines.append(
            f"[meta]{label('label_ai')}[/]  {active.get('model') or '—'}  "
            f"({active.get('name') or 'endpoint'})"
        )
    else:
        ai = snap.get("ai") if isinstance(snap.get("ai"), dict) else {}
        names = list(ai.get("endpoint_names") or [])
        lines.append(
            f"[meta]{label('label_ai')}[/]  endpoints={ai.get('endpoint_count') or len(names)}"
            + (f"  [{', '.join(str(n) for n in names[:3])}]" if names else "")
        )

    proxy = snap.get("proxy") if isinstance(snap.get("proxy"), dict) else {}
    if proxy:
        running = bool(proxy.get("running") or proxy.get("enabled") or proxy.get("active"))
        port = proxy.get("port") or proxy.get("listen_port")
        state = label("proxy_on" if running else "proxy_off")
        lines.append(
            f"[meta]{label('label_proxy')}[/]  {state}"
            + (f"  :{port}" if port else "")
        )

    lines.append(
        f"[meta]{label('label_tasks')}[/]  "
        + label(
            "tasks_line",
            running=snap.get("running_count") or 0,
            total=snap.get("task_count") or len(list(snap.get("tasks") or [])),
        )
    )
    if snap.get("checks_available") is not None:
        lines.append(
            f"[meta]{label('label_checks')}[/]  {snap.get('checks_available')}"
        )

    kb = snap.get("knowledge") if isinstance(snap.get("knowledge"), dict) else {}
    entries = list(kb.get("entries") or []) if kb else []
    if not entries:
        view = snap.get("current_view") if isinstance(snap.get("current_view"), dict) else {}
        kb2 = view.get("knowledge") if isinstance(view.get("knowledge"), dict) else {}
        entries = list(kb2.get("entries") or [])
    if entries:
        lines.append(f"[hud]{label('knowledge_section')}[/]")
        for row in entries[:5]:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("id") or "")[:20]
            title = str(row.get("title") or "")[:56]
            lines.append(f"  [hud.dim]{kid}[/]  {title}")
    lines.append(f"[hud.dim]{t('status_overview_hint')}[/]")
    return lines


def _cmd_tools(api: Any, session_id: str) -> list[str]:
    status = api.get_assistant_session(session_id)
    if not status.get("ok"):
        return [f"[fail]{status.get('error') or t('conversation_unavailable')}[/]"]
    pending = [
        item for item in list(status.get("pending_tools") or []) if isinstance(item, dict)
    ]
    tools = list(status.get("tools") or [])
    detail = (
        t(
            "tools_pending",
            names=", ".join(str(item.get("name") or "tool") for item in pending[:6]),
        )
        if pending
        else t("tools_no_pending")
    )
    lines = [t("tools_line", count=len(tools), suffix=detail)]
    if not pending:
        return lines
    lines.append(label("tools_pending_count", count=len(pending)))
    for item in pending[:12]:
        name = str(item.get("name") or item.get("tool") or "tool")
        call_id = str(item.get("call_id") or item.get("id") or "")[:24]
        risk = str(item.get("risk") or item.get("risk_level") or "")
        extra = f"  {label('tools_risk', risk=risk)}" if risk else ""
        lines.append(f"  • {name}  [hud.dim]{call_id}[/]{extra}")
    lines.append(f"[hud.dim]{label('tools_pending_hint')}[/]")
    return lines


def _cmd_cancel(api: Any, session_id: str) -> list[str]:
    result = api.cancel_assistant_run(session_id)
    if result.get("ok"):
        return [t("cancel_requested")]
    return [f"[fail]{result.get('error') or t('cancel_failed')}[/]"]


def _cmd_tasks(api: Any, argument: str) -> list[str]:
    try:
        rows = [row for row in list(api.list_tasks() or []) if isinstance(row, dict)]
    except Exception as exc:
        return [f"[fail]{exc}[/]"]
    page = 1
    if argument:
        if not argument.isdigit():
            return [label("tasks_usage")]
        page = max(1, int(argument))
    total = len(rows)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE) if total else 1
    page = min(page, pages)
    start = (page - 1) * _PAGE_SIZE
    chunk = rows[start : start + _PAGE_SIZE]
    lines = [label("tasks_title", page=page, page_count=pages, total=total)]
    if not chunk:
        lines.append(label("tasks_empty"))
        return lines
    for i, row in enumerate(chunk, start=start + 1):
        tid = str(row.get("id") or row.get("task_id") or "")[:16]
        st = str(row.get("status") or "—")
        url = str(
            row.get("target_url") or row.get("url") or row.get("target") or ""
        )[:48]
        findings = row.get("finding_count")
        if findings is None:
            findings = row.get("findings_count")
        if findings is None:
            findings = row.get("total")
        if findings is None and isinstance(row.get("findings"), list):
            findings = len(row["findings"])
        try:
            n = int(findings or 0)
        except (TypeError, ValueError):
            n = 0
        lines.append(
            label("tasks_row", ordinal=i, status=st, findings=n, url=url or tid)
        )
    if pages > 1:
        lines.append(f"[hud.dim]{label('tasks_hint')}[/]")
    return lines


def _cmd_sessions(api: Any, session_id: str, argument: str) -> list[str]:
    rows = [
        row
        for row in list(api.list_assistant_sessions() or [])
        if isinstance(row, dict)
    ]
    page = 1
    query = ""
    if argument:
        if argument.isdigit():
            page = max(1, int(argument))
        elif argument.lower().startswith("search "):
            query = argument[7:].strip()[:120]
            if not query:
                return [t("usage_sessions_search")]
        else:
            return [t("usage_sessions")]

    if query:
        folded = query.casefold()
        rows = [
            row
            for row in rows
            if folded in str(row.get("id") or "").casefold()
            or folded in str(row.get("name") or "").casefold()
            or folded in str(row.get("note") or "").casefold()
        ]

    total = len(rows)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE) if total else 1
    page = min(page, pages)
    start = (page - 1) * _PAGE_SIZE
    chunk = rows[start : start + _PAGE_SIZE]
    if query:
        lines = [t("sessions_title_query", page=page, page_count=pages, query=query)]
    else:
        lines = [t("sessions_title", page=page, page_count=pages)]
    if not chunk:
        lines.append(t("no_sessions_match", query=query) if query else t("no_sessions"))
        return lines
    for i, row in enumerate(chunk, start=start + 1):
        sid = str(row.get("id") or "")
        name = str(row.get("name") or sid)[:40]
        msgs = row.get("message_count") or row.get("msg_count") or 0
        marker = t("sessions_current_marker") if sid == session_id else ""
        lines.append(
            t("sessions_row", ordinal=i, name=name, count=msgs, marker=marker)
        )
    lines.append(f"[hud.dim]{t('sessions_hint')}[/]")
    return lines


def _cmd_use(api: Any, session_id: str, selector: str) -> ShellResult:
    rows = [
        row
        for row in list(api.list_assistant_sessions() or [])
        if isinstance(row, dict)
    ]
    match: dict[str, Any] | None = None
    if selector.isdigit():
        ordinal = int(selector)
        if 1 <= ordinal <= len(rows):
            match = rows[ordinal - 1]
    if match is None:
        folded = selector.casefold()
        match = next(
            (row for row in rows if str(row.get("id") or "").casefold() == folded),
            None,
        ) or next(
            (
                row
                for row in rows
                if folded and folded in str(row.get("name") or "").casefold()
            ),
            None,
        )
    if match is None:
        return ShellResult(handled=True, lines=[t("session_not_found")])
    new_id = str(match.get("id") or "")
    if not new_id:
        return ShellResult(handled=True, lines=[label("switch_failed")])
    if new_id == session_id:
        return ShellResult(
            handled=True,
            lines=[label("already_on_session", session_id=new_id)],
        )
    name = str(match.get("name") or new_id)[:40]
    return ShellResult(
        handled=True,
        session_id=new_id,
        lines=[t("session_switched", name=name, session_id=new_id)],
    )


def _cmd_model(
    api: Any,
    session_id: str,
    selector: str,
    *,
    confirm: Callable[[str, bool], bool] | None = None,
) -> list[str]:
    target: dict[str, Any] = {"session_id": session_id}
    if not selector or selector.lower() in {"list"}:
        result = api._channel_ai_endpoint("list", target)
    elif selector.lower() in {"refresh", "reload"}:
        target["refresh"] = "1"
        result = api._channel_ai_endpoint("list", target)
    else:
        target["selector"] = selector
        resolved = api._channel_ai_endpoint("resolve", target)
        if not resolved.get("ok"):
            return [_endpoint_error(resolved)]
        endpoint = dict(resolved.get("endpoint") or {})
        kind = str(resolved.get("kind") or "endpoint")
        if endpoint.get("active"):
            key = (
                "acp_model_already_active"
                if kind == "acp_model"
                else "endpoint_already_active"
            )
            return [t(key, name=endpoint.get("name") or "")]
        display = f"{endpoint.get('name')} · {endpoint.get('model')}"
        if not _prompt_confirm(
            f"[amber]{label('model_switch_ask', name=display)}[/]",
            default=True,
            confirm=confirm,
        ):
            return [label("model_switch_aborted")]
        activated = api._channel_ai_endpoint(
            "activate",
            {
                "session_id": session_id,
                "index": endpoint.get("index"),
                "name": endpoint.get("name"),
                "value": endpoint.get("value") or endpoint.get("model"),
                "config_id": endpoint.get("config_id"),
            },
        )
        if not activated.get("ok"):
            return [_endpoint_error(activated)]
        ep = dict(activated.get("endpoint") or endpoint)
        key = "acp_model_switched" if kind == "acp_model" else "endpoint_switched"
        return [t(key, name=ep.get("name") or "", model=ep.get("model") or "")]

    if not result.get("ok"):
        return [_endpoint_error(result)]
    kind = str(result.get("kind") or "endpoint")
    rows = [row for row in list(result.get("endpoints") or []) if isinstance(row, dict)]
    if not rows:
        return [t("acp_model_none" if kind == "acp_model" else "endpoint_none")]
    title_key = "acp_model_list_title" if kind == "acp_model" else "endpoint_list_title"
    hint_key = "acp_model_list_hint" if kind == "acp_model" else "endpoint_list_hint"
    lines = [t(title_key)]
    for row in rows:
        marker = t("endpoint_current_marker") if row.get("active") else ""
        lines.append(
            t(
                "endpoint_row",
                ordinal=row.get("ordinal") or "?",
                name=str(row.get("name") or "")[:28],
                model=str(row.get("model") or "")[:36],
                marker=marker,
            )
        )
    lines.append(f"[hud.dim]{t(hint_key)}[/]")
    return lines


def _endpoint_error(result: dict[str, Any]) -> str:
    code = str(result.get("code") or "").strip()
    if code in {
        "endpoint_not_found",
        "endpoint_failed",
        "endpoint_changed",
        "endpoint_unavailable",
        "acp_model_not_found",
        "acp_model_failed",
        "acp_model_changed",
        "acp_model_unavailable",
        "acp_model_probe_failed",
    }:
        return f"[fail]{t(code)}[/]"
    err = str(result.get("error") or code or t("endpoint_failed"))
    return f"[fail]{err}[/]"


def _cmd_option(api: Any, session_id: str, option: str, action: str) -> list[str]:
    result = api._channel_session_option(session_id, option, action)
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or option}[/]"]
    enabled = bool(result.get("enabled"))
    # Never surface the English ``message`` blobs from the channel handler —
    # always render through the active locale catalog.
    if option == "search":
        return [t("status_row_search_on" if enabled else "status_row_search_off")]
    if option == "think":
        effort = str(result.get("thinking_effort") or "high")
        if enabled:
            lines = [t("status_row_think_effort", effort=effort)]
        else:
            lines = [t("status_row_think_off")]
        lines.append(t("status_row_think_scale", scale=thinking_scale_line(enabled, effort)))
        return lines
    if option == "greenlight":
        return [t("greenlight_active" if enabled else "greenlight_off")]
    state = label("option_on" if enabled else "option_off")
    return [f"{option}  {state}"]


def _cmd_greenlight_with_goal(api: Any, session_id: str, goal_text: str) -> ShellResult:
    """Enable Greenlight and replay the target as the next Agent turn."""
    try:
        result = api.set_assistant_engagement(
            session_id,
            {
                "active": True,
                "mode": "greenlight",
                "goal_text": goal_text,
            },
        )
    except Exception as exc:
        return ShellResult(handled=True, lines=[f"[fail]{exc}[/]"])
    if not result.get("ok"):
        return ShellResult(
            handled=True,
            lines=[f"[fail]{result.get('error') or t('greenlight_failed')}[/]"],
        )
    return ShellResult(
        handled=True,
        lines=[t("greenlight_active")],
        forward_text=goal_text,
    )


def _cmd_lang(api: Any, requested: str) -> list[str]:
    wanted = requested.strip().lower()
    if not wanted:
        return [t("lang_usage", current=get_locale())]
    if wanted not in {"zh", "en", "both"}:
        return [t("lang_usage", current=get_locale())]
    try:
        result = api.set_locale(wanted)
    except Exception as exc:
        return [f"[fail]{exc}[/]"]
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or 'lang failed'}[/]"]
    current = set_locale(wanted)
    return [t("lang_set", current=current)]


def _cmd_file(api: Any, session_id: str, argument: str) -> list[str]:
    selector = argument or "list"
    low = selector.lower()
    page = 1
    query = ""
    list_mode = (
        not argument
        or low == "list"
        or low.startswith("list ")
        or low.startswith("search ")
    )
    if list_mode:
        if low.startswith("search "):
            query = selector[7:].strip()[:120]
            if not query:
                return [t("usage_file_search")]
            handler_sel = f"search {query}"
        else:
            handler_sel = "list"
            if low.startswith("list "):
                rest = selector[5:].strip()
                if rest.isdigit():
                    page = max(1, int(rest))
                elif rest.lower().startswith("search "):
                    query = rest[7:].strip()[:120]
                    if not query:
                        return [t("usage_file_search")]
                    handler_sel = f"search {query}"
                elif rest:
                    return [t("usage_file")]
        try:
            result = api._channel_session_file(session_id, handler_sel)
        except Exception as exc:
            return [f"[fail]{exc}[/]"]
        if not result.get("ok"):
            return [f"[fail]{result.get('error') or t('file_failed')}[/]"]
        rows = [row for row in list(result.get("files") or []) if isinstance(row, dict)]
        if not rows:
            return [t("file_none_match", query=query) if query else t("file_none")]
        pages = max(1, (len(rows) + _FILE_PAGE_SIZE - 1) // _FILE_PAGE_SIZE)
        page = min(page, pages)
        start = (page - 1) * _FILE_PAGE_SIZE
        chunk = rows[start : start + _FILE_PAGE_SIZE]
        if query:
            lines = [
                t(
                    "file_list_title_query",
                    page=page,
                    page_count=pages,
                    query=query,
                )
            ]
        else:
            lines = [t("file_list_title_paged", page=page, page_count=pages)]
        for row in chunk:
            idx = int(row.get("index") or 0) or "?"
            path = str(row.get("relative_path") or row.get("name") or "file")
            size = int(row.get("size") or 0)
            lines.append(t("file_list_row", index=idx, path=path, size=size))
        lines.append(f"[hud.dim]{t('file_list_hint')}[/]")
        return lines

    try:
        result = api._channel_session_file(session_id, selector)
    except Exception as exc:
        return [f"[fail]{exc}[/]"]
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or t('file_failed')}[/]"]
    attachment = result.get("attachment") if isinstance(result.get("attachment"), dict) else {}
    if not attachment:
        return [str(result.get("message") or t("file_sending", name="file"))]
    size = attachment.get("size") or 0
    return [
        f"{label('file_meta_name')}  {attachment.get('name') or 'file'}",
        f"{label('file_meta_path')}  {attachment.get('relative_path') or ''}",
        f"{label('file_meta_abs')}   {attachment.get('path') or ''}",
        f"{label('file_meta_size')}  {label('file_bytes', size=size)}",
        f"{label('file_meta_sha')}   {str(attachment.get('sha256') or '')[:16]}…",
        f"{label('file_meta_mime')}  {attachment.get('mime') or ''}",
    ]


def _cmd_decision(
    api: Any,
    session_id: str,
    verdict: str,
    selector: str,
    *,
    confirm: Callable[[str, bool], bool] | None = None,
) -> list[str]:
    """Mirror channel ``/allow`` / ``/deny`` semantics against pending tools."""
    requested = selector.lower()
    if verdict == "allow" and requested == "all":
        if not _prompt_confirm(
            f"[amber]{label('auth_auto_allow')}[/]",
            default=False,
            confirm=confirm,
            allow_non_tty=True,
        ):
            return [label("auto_allow_aborted")]
        result = api._channel_session_option(session_id, "tools", "allow_all")
        if not result.get("ok"):
            return [f"[fail]{result.get('error') or t('session_tools_unavailable')}[/]"]
        return [t("auto_allow_enabled")]
    if verdict == "allow" and requested == "off":
        result = api._channel_session_option(session_id, "tools", "disable_all")
        if not result.get("ok"):
            return [f"[fail]{result.get('error') or t('session_tools_unavailable')}[/]"]
        return [t("session_tools_disabled")]
    if verdict == "allow" and requested == "standard":
        result = api._channel_session_option(session_id, "tools", "allow_standard")
        if not result.get("ok"):
            return [f"[fail]{result.get('error') or t('session_tools_unavailable')}[/]"]
        return [t("auto_allow_enabled")]

    status = api.get_assistant_session(session_id)
    if not status.get("ok"):
        return [f"[fail]{status.get('error') or t('conversation_unavailable')}[/]"]
    pending = [
        item for item in list(status.get("pending_tools") or []) if isinstance(item, dict)
    ]
    if not pending:
        return [t("no_pending_confirmation")]

    selected = pending[0]
    if requested in {"all", "pending"} or (verdict == "allow" and requested == "pending"):
        action = {
            "type": "batch_confirmation_decision",
            "pending_id": str(selected.get("pending_id") or ""),
            "version": int(selected.get("version") or 0),
            "decisions": {
                str(item.get("id") or ""): verdict
                for item in pending
                if str(item.get("id") or "")
            },
        }
    else:
        if requested:
            if requested.isdigit():
                ordinal = int(requested)
                if 1 <= ordinal <= len(pending):
                    selected = pending[ordinal - 1]
                else:
                    return [
                        label("no_pending_match", selector=selector),
                        f"[hud.dim]{label('try_tools')}[/]",
                    ]
            else:
                selected = next(
                    (
                        item
                        for item in pending
                        if str(item.get("id") or "").lower() == requested
                        or str(item.get("name") or "").lower() == requested
                    ),
                    None,
                )
            if selected is None:
                return [
                    label("no_pending_match", selector=selector),
                    f"[hud.dim]{label('try_tools')}[/]",
                ]
        action = {
            "type": "confirmation_decision",
            "pending_id": str(selected.get("pending_id") or ""),
            "version": int(selected.get("version") or 0),
            "call_id": str(selected.get("id") or ""),
            "verdict": verdict,
        }

    if hasattr(api, "_confirm_channel_action"):
        result = api._confirm_channel_action(session_id, action, "cli")
    else:
        result = api.start_assistant_action_stream(
            {"session_id": session_id, "action": action}
        )
    if result.get("ok"):
        return [t("decision_forwarded")]
    return [f"[fail]{result.get('error') or t('confirmation_failed')}[/]"]


def _cmd_monitor(action: str) -> ShellResult:
    act = (action or "status").lower()
    if act == "status":
        return ShellResult(
            handled=True,
            lines=[label("monitor_status_ask")],
            refresh=True,
        )
    if act == "on":
        act = "both"
    if act not in {"off", "sys", "tokens", "agents", "both"}:
        return ShellResult(handled=True, lines=[label("monitor_usage")])
    return ShellResult(
        handled=True,
        lines=[label("monitor_set", mode=act)],
        monitor_mode=act,
        refresh=True,
    )


def _cmd_sidebar(action: str) -> ShellResult:
    act = (action or "status").lower()
    if act == "status":
        return ShellResult(
            handled=True,
            lines=[label("sidebar_status_ask")],
            refresh=True,
        )
    if act not in {"on", "off", "auto"}:
        return ShellResult(handled=True, lines=[label("sidebar_usage")])
    return ShellResult(
        handled=True,
        lines=[label("sidebar_set", mode=act)],
        sidebar_mode=act,
        refresh=True,
    )


def _cmd_jobs(api: Any, session_id: str, *, page: int = 1) -> list[str]:
    if not hasattr(api, "list_assistant_run_jobs"):
        return [label("jobs_unavailable")]
    try:
        result = api.list_assistant_run_jobs(session_id)
    except Exception as exc:
        return [f"[fail]{exc}[/]"]
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or label('jobs_unavailable')}[/]"]
    jobs = [j for j in list(result.get("jobs") or []) if isinstance(j, dict)]
    # Prefer non-terminal first.
    terminal = {"completed", "failed", "cancelled"}
    jobs.sort(
        key=lambda j: (
            0 if str(j.get("state") or "").lower() not in terminal else 1,
            -float(j.get("updated_at") or j.get("created_at") or 0),
        )
    )
    page = max(1, int(page or 1))
    size = 8
    total = len(jobs)
    pages = max(1, (total + size - 1) // size) if total else 1
    page = min(page, pages)
    chunk = jobs[(page - 1) * size : page * size]
    lines = [label("jobs_title", page=page, page_count=pages, total=total)]
    if not chunk:
        lines.append(label("ops_agents_empty"))
        return lines
    for i, job in enumerate(chunk, start=(page - 1) * size + 1):
        jid = str(job.get("job_id") or job.get("id") or "")[:16]
        state = str(job.get("state") or "?")
        status = str(job.get("status") or "")[:40]
        flag = " !AUTH" if state == "waiting_user" else ""
        lines.append(
            label("jobs_row", ordinal=i, job_id=jid, state=state, status=status, flag=flag)
        )
    return lines


def _cmd_resume(api: Any, session_id: str, job_id: str, message: str) -> list[str]:
    status = api.get_assistant_session(session_id)
    pending = [
        item
        for item in list((status or {}).get("pending_tools") or [])
        if isinstance(item, dict)
    ]
    if pending:
        return [label("resume_need_allow")]
    if not hasattr(api, "resume_assistant_run_job"):
        return [label("resume_unavailable")]
    try:
        result = api.resume_assistant_run_job(
            {
                "session_id": session_id,
                "job_id": job_id,
                "message": message,
            }
        )
    except Exception as exc:
        return [f"[fail]{exc}[/]"]
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or label('resume_unavailable')}[/]"]
    return [label("resume_ok", job_id=job_id[:16])]


def team_greenlight_lines(api: Any, team_id: str, action: str) -> list[str]:
    """Turn the team shield on or off. Tool confirms stay; the 'may I do this' ask does not."""
    action = str(action or "status").lower()
    if action not in {"on", "off", "status"}:
        return [f"[fail]{label('team_room_greenlight_usage')}[/]"]
    if action == "status":
        current = api.get_team(team_id)
        team = current.get("team") if isinstance(current, dict) else None
        if not isinstance(current, dict) or not current.get("ok") or not isinstance(team, dict):
            err = current.get("error") if isinstance(current, dict) else "team not found"
            return [f"[fail]{err or 'team not found'}[/]"]
        enabled = bool(team.get("greenlight"))
    else:
        result = api.set_team_greenlight(team_id, action == "on")
        if not isinstance(result, dict) or not result.get("ok"):
            err = result.get("error") if isinstance(result, dict) else "greenlight failed"
            return [f"[fail]{err or 'greenlight failed'}[/]"]
        enabled = bool(result.get("greenlight"))
    key = "team_room_greenlight_on" if enabled else "team_room_greenlight_off"
    return [f"[ok]{label(key)}[/]"]


def team_room_greenlight_override(api: Any, team_id: str, text: str) -> ShellResult | None:
    """Inside a team room, ``/greenlight`` authorizes that team, not the main Agent."""
    raw = (text or "").strip()
    green = match_slash_names(raw, ("/greenlight",), glue_toggle=True)
    if green is None:
        return None
    args = green[1]
    if args and args[0].casefold() == "of":
        args = ("off", *args[1:])
    resolved = toggle_action(args, allow_extra=True)
    if resolved is None:
        return ShellResult(handled=True, lines=[t("usage_greenlight")])
    action, goal = resolved
    forward = goal if action == "on" and goal else None
    return ShellResult(
        handled=True,
        lines=team_greenlight_lines(api, team_id, action),
        forward_text=forward,
    )


def _cmd_team(api: Any, argument: str) -> ShellResult:
    """Standing-team room inside the ops console. Plain lines after ``/team use`` go to the room."""
    from argusai.cli.commands.team import _load_team, _resolve_member, _teams, _visible_chat

    parts = argument.split(maxsplit=1) if argument else []
    verb = (parts[0] if parts else "list").lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    try:
        if verb in {"list", "ls"}:
            rows = _teams(api)
            if not rows:
                return ShellResult(handled=True, lines=[f"[hud.dim]{label('team_room_empty')}[/]"])
            lines = [f"[accent]{label('team_room_list_title')}[/]"]
            for index, row in enumerate(rows, start=1):
                status = row.get("status") if isinstance(row.get("status"), dict) else {}
                mark = f"  {label('team_room_greenlight_mark')}" if row.get("greenlight") else ""
                lines.append(
                    f"  [accent]{index}[/]  {row.get('name')}  [meta]{row.get('team_id')}[/]  "
                    f"{status.get('overall')}  members={status.get('member_count') or 0}{mark}"
                )
            lines.append(f"[hud.dim]{label('team_room_list_hint')}[/]")
            return ShellResult(handled=True, lines=lines)
        if verb in {"leave", "back", "exit"}:
            return ShellResult(handled=True, leave_team=True, lines=[f"[ok]{label('team_room_left')}[/]"])
        if verb in {"use", "join", "open", "pm"}:
            if not rest:
                return ShellResult(handled=True, lines=[f"[fail]{label('team_room_usage')}[/]"])
            member_token = ""
            team_token = rest
            if verb == "pm":
                bits = rest.split(maxsplit=1)
                if len(bits) < 2:
                    return ShellResult(handled=True, lines=[f"[fail]{label('team_room_pm_usage')}[/]"])
                team_token, member_token = bits
            loaded = _load_team(api, team_token)
            member_id = ""
            member_role = ""
            if member_token:
                who = _resolve_member(loaded, member_token)
                member_id = str(who.get("agent_id") or "")
                member_role = str(who.get("role") or "")
            chat = api.get_team_chat(str(loaded.get("team_id") or ""))
            messages = [row for row in list((chat or {}).get("messages") or []) if isinstance(row, dict)]
            visible = _visible_chat(messages, member_id)[-20:]
            where = (
                label("team_room_where_private", role=member_role)
                if member_role
                else label("team_room_where_group")
            )
            banner = (
                f"[ok]{label('team_room_entered', where=where)}[/]  {loaded.get('name')}  "
                f"[meta]{loaded.get('team_id')}[/]"
            )
            return ShellResult(
                handled=True,
                team_id=str(loaded.get("team_id") or ""),
                team_name=str(loaded.get("name") or ""),
                team_member_id=member_id,
                team_member_role=member_role,
                lines=[banner],
                team_transcript=visible,
            )
        if verb == "chat":
            return ShellResult(handled=True, lines=[f"[hud.dim]{label('team_room_need_use')}[/]"])
        if verb == "stop":
            if not rest:
                return ShellResult(handled=True, stop_current_team=True)
            loaded = _load_team(api, rest)
            result = api.stop_team_members(str(loaded.get("team_id") or ""))
            count = int((result or {}).get("stopped_count") or 0)
            return ShellResult(handled=True, lines=[f"[ok]{label('team_room_stopped', count=count)}[/]"])
        if verb == "greenlight":
            bits = rest.split(maxsplit=1) if rest else []
            action = (bits[0] if bits else "status").lower()
            name = bits[1].strip() if len(bits) > 1 else ""
            if action not in {"on", "off", "status"}:
                return ShellResult(handled=True, lines=[f"[fail]{label('team_room_greenlight_usage')}[/]"])
            if not name:
                return ShellResult(handled=True, team_greenlight=action)
            loaded = _load_team(api, name)
            return ShellResult(
                handled=True,
                lines=team_greenlight_lines(api, str(loaded.get("team_id") or ""), action),
            )
    except Exception as exc:
        return ShellResult(handled=True, lines=[f"[fail]{exc}[/]"])
    return ShellResult(handled=True, lines=[f"[fail]{label('team_room_help')}[/]"])


def _format_team_lines(rows: list[dict[str, Any]], *, skip_user: bool = False) -> list[str]:
    from rich.markup import escape

    lines: list[str] = []
    for item in rows:
        is_user = str(item.get("role") or "") == "user" or str(item.get("sender") or "") == "leader"
        if is_user and skip_user:
            continue
        who = label("team_room_you") if is_user else str(
            item.get("role_name") or item.get("sender_role") or item.get("sender") or item.get("role") or ""
        )
        if item.get("type") == "activity":
            tools = ", ".join(str(name) for name in (item.get("tool_names") or []))
            lines.append(f"[hud.dim]{escape(who)}[/]  [meta]{escape(tools)}[/]")
            continue
        text = str(item.get("content") or "").strip().replace("\n", " ")
        if len(text) > 240:
            text = text[:239] + "…"
        if text:
            lines.append(f"[accent]{escape(who)}[/]  {escape(text)}")
    return lines


def _cmd_agents(api: Any) -> list[str]:
    result = api._channel_agent_runtime("list", {})
    if not result.get("ok"):
        err = result.get("error") or result.get("code") or t("agent_unavailable")
        return [f"[fail]{err}[/]"]
    rows = [row for row in list(result.get("agents") or []) if isinstance(row, dict)]
    if not rows:
        return [t("agent_none")]
    lines = [t("agent_list_title")]
    for row in rows:
        marker = "" if row.get("available") else t("agent_unavailable_marker")
        lines.append(
            t(
                "agent_row",
                ordinal=row.get("ordinal") or "?",
                name=str(row.get("name") or "")[:32],
                kind=str(row.get("kind") or "")[:20],
                marker=marker,
            )
        )
    lines.append(f"[hud.dim]{t('agent_list_hint')}[/]")
    return lines


def _cmd_new(api: Any, selector: str) -> ShellResult:
    if selector:
        created = api._channel_agent_runtime("create", {"selector": selector})
        if not created.get("ok"):
            code = str(created.get("code") or "")
            if code in {
                "agent_not_found",
                "agent_not_detected",
                "agent_create_failed",
                "agent_unavailable",
            }:
                err = t(code)
            else:
                err = created.get("error") or t("agent_create_failed")
            return ShellResult(handled=True, lines=[f"[fail]{err}[/]"])
        new_id = str(created.get("session_id") or "")
        agent = dict(created.get("agent") or {})
        return ShellResult(
            handled=True,
            session_id=new_id or None,
            lines=[
                t(
                    "agent_session_created",
                    name=str(agent.get("name") or agent.get("id") or selector)[:60],
                    session_id=new_id,
                )
            ],
        )
    new_id = uuid.uuid4().hex
    try:
        api.get_assistant_session(new_id)
    except Exception:
        pass
    return ShellResult(
        handled=True,
        session_id=new_id,
        lines=[t("session_created", session_id=new_id)],
    )


def _cmd_btw(api: Any, session_id: str, question: str) -> list[str]:
    result = api.assistant_side_question(session_id, question)
    if not result.get("ok"):
        return [f"[fail]{result.get('error') or t('usage_btw')}[/]"]
    answer = str(result.get("text") or result.get("reply") or result.get("answer") or "").strip()
    if not answer:
        return [label("empty_side")]
    clipped = answer if len(answer) <= 4000 else answer[:3997] + "…"
    return [clipped]
