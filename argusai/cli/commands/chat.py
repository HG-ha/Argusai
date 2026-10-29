"""``argusai chat`` — interactive Agent REPL as a red-team ops console.

- fullscreen ops app: pinned input dock, in-place transcript refresh
- local slash commands never open an Agent turn
- ``user_decision`` cards render as numbered choice panels
- tool confirms render as AUTH panels (localized)
- stream assistant Markdown into the live transcript pane
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from io import StringIO
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.status import Status

from argusai.cli.cards import (
    print_batch_decision_panel,
    print_confirm_panel,
    print_decision_panel,
    print_ui_card,
)
from argusai.cli.locale import get_locale, label, sync_from_api
from argusai.cli.metrics import sample_host_metrics
from argusai.cli.ops_app import OpsApp, run_ops_worker
from argusai.cli.prompt import ask_choice_line, ask_ops_line, set_toolbar_provider
from argusai.cli.render import ReplyStream, print_tool_exec, tool_detail_from_block
from argusai.cli.shell import (
    dispatch,
    looks_like_slash,
    needs_tty_confirm,
    print_shell_result,
    team_greenlight_lines,
    team_room_greenlight_override,
)
from argusai.cli.ui import (
    ARGUS_THEME,
    emit_result,
    get_console,
    ops_rule,
    print_ops_hud,
    set_json_mode,
)
from argusai.core.assistant.attachments import (
    _ATTACHMENT_AUDIO_MIME,
    _ATTACHMENT_FILE_MAX_BYTES,
    _ATTACHMENT_IMAGE_MIME,
    _ATTACHMENT_MAX_BYTES,
    _ATTACHMENT_MAX_COUNT,
    _ATTACHMENT_TEXT_MAX_BYTES,
    _ATTACHMENT_VIDEO_MAX_BYTES,
    _ATTACHMENT_VIDEO_MIME,
    _attachment_text_ext_ok,
    _attachment_text_mime_ok,
)

_TOOL_CONFIRM_TYPES = frozenset({
    "tool_confirmation",
    "confirm",
    "batch_confirmation",
    "tool_confirm",
    "command_plan",
})
_DECISION_TYPES = frozenset({"user_decision", "user_decision_batch"})
# Tool-authored payloads already had an EXEC line (and often a mid-turn card).
# Replaying them on ``done`` dumps stdout / file excerpts under the finished
# answer and shoves the transcript while the user is reading.
_TOOL_RETURN_CARD_TYPES = frozenset({
    "sandbox_file",
    "sandbox_run",
    "sandbox_env",
    "command_result",
    "probe_result",
    "runtime_trace",
    "reverse_result",
    "request_replay",
    "vulnerability_evidence",
    "file_resource_view",
    "desktop_observe",
    "tool_trace",
    "tool_call",
    "tool_activity",
    "tool_result",
})
_INLINE_FILE_RE = re.compile(
    r"""(?<!\S)@(?:"([^"\r\n]+)"|'([^'\r\n]+)'|([^\s]+))"""
)


def _resolve_cli_file(raw_path: str) -> Path:
    expanded = os.path.expandvars(str(raw_path or "").strip())
    return Path(expanded).expanduser().resolve()


def _extract_inline_files(user_text: str) -> tuple[str, list[Path]]:
    """Extract existing ``@path`` / ``@"path with spaces"`` references."""
    paths: list[Path] = []
    issues: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw = next((part for part in match.groups() if part is not None), "")
        try:
            path = _resolve_cli_file(raw)
        except (OSError, RuntimeError, ValueError):
            issues.append(f"invalid path: {raw}")
            return ""
        if path.is_file():
            paths.append(path)
            return ""
        if path.exists():
            issues.append(f"not a file: {raw}")
            return ""
        # Keep ordinary @mentions intact. Path-shaped references get a useful
        # error instead of silently becoming prompt text.
        if any(mark in raw for mark in ("/", "\\", ":")) or raw.startswith("."):
            issues.append(f"file not found: {raw}")
            return ""
        return match.group(0)

    cleaned = _INLINE_FILE_RE.sub(_replace, user_text or "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if issues:
        raise ValueError("; ".join(issues))
    return cleaned, paths


def _load_cli_attachments(paths: list[Path]) -> list[dict[str, Any]]:
    """Read explicitly selected local files into the assistant attachment contract."""
    unique: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = _resolve_cli_file(str(raw_path))
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if len(unique) > _ATTACHMENT_MAX_COUNT:
        raise ValueError(
            f"too many attachments: {len(unique)} (max {_ATTACHMENT_MAX_COUNT})"
        )

    attachments: list[dict[str, Any]] = []
    for path in unique:
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"cannot inspect file: {path} ({exc})") from exc

        mime = (mimetypes.guess_type(path.name)[0] or "application/octet-stream").lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        elif mime == "audio/x-wav":
            mime = "audio/wav"

        if mime in _ATTACHMENT_IMAGE_MIME:
            kind, limit = "image", _ATTACHMENT_MAX_BYTES
        elif mime in _ATTACHMENT_VIDEO_MIME:
            kind, limit = "video", _ATTACHMENT_VIDEO_MAX_BYTES
        elif mime in _ATTACHMENT_AUDIO_MIME or mime.startswith("audio/"):
            kind, limit = "audio", _ATTACHMENT_MAX_BYTES
        elif _attachment_text_mime_ok(mime) or _attachment_text_ext_ok(path.name):
            kind, limit = "text", _ATTACHMENT_TEXT_MAX_BYTES
        else:
            kind, limit = "file", _ATTACHMENT_FILE_MAX_BYTES

        if size > limit:
            raise ValueError(
                f"file too large: {path.name} ({size:,} bytes; max {limit:,})"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read file: {path} ({exc})") from exc
        if kind == "text":
            try:
                text = payload.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError(f"text file is not UTF-8: {path.name}") from exc
            attachments.append(
                {
                    "type": "text",
                    "mime": mime,
                    "name": path.name,
                    "text": text,
                    "size": len(payload),
                }
            )
            continue
        attachments.append(
            {
                "type": kind,
                "mime": mime,
                "name": path.name,
                "data_base64": base64.b64encode(payload).decode("ascii"),
                "size": len(payload),
            }
        )
    return attachments


def _prepare_cli_turn(
    user_text: str,
    explicit_files: list[Path] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    message, inline_files = _extract_inline_files(user_text)
    attachments = _load_cli_attachments([*(explicit_files or []), *inline_files])
    return message, attachments


_TEAM_LAUNCH_RE = re.compile(
    r"^team(?:\s+(?:list|ls|use|join|open|pm|leave|back|exit|chat|stop|greenlight)(?:\s+\S.*)?)?$",
    re.IGNORECASE,
)


def _opening_line(message: str | list[str] | None) -> str:
    """Join ``argusai chat`` words into one opening line.

    ``chat /team use <id>`` and ``chat team use <id>`` both enter that room.
    A normal sentence that merely starts with "team" stays a user message.
    """
    if isinstance(message, list):
        text = " ".join(str(part).strip() for part in message if str(part).strip()).strip()
    else:
        text = str(message or "").strip()
    if text.startswith("/"):
        return text
    if _TEAM_LAUNCH_RE.fullmatch(text):
        return f"/{text}"
    return text


def chat(
    message: list[str] | None = typer.Argument(
        None,
        help="Opening line. Words are joined, so `chat /team use <id>` enters that team.",
    ),
    session_id: str = typer.Option(
        "cli-default",
        "--session",
        "-s",
        help="Assistant session id (history is reused across turns).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Auto-approve eligible non-high-risk tool confirms; high-risk still asks on a TTY.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="One-shot mode: require a message or file, run one turn, then exit.",
    ),
    timeout: float = typer.Option(600.0, "--timeout", help="Max seconds per turn."),
    json_out: bool = typer.Option(False, "--json", help="One-shot JSON summary (implies --once)."),
    files: list[Path] | None = typer.Option(
        None,
        "--file",
        "-f",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Attach a local file to the next Agent turn. Repeat for multiple files.",
    ),
) -> None:
    """Enter an interactive Agent conversation (multi-turn ops console)."""
    if json_out:
        once = True
    set_json_mode(json_out)

    opening = _opening_line(message)
    if once and not opening and not files:
        raise typer.BadParameter("message or --file is required with --once / --json")

    from argusai import __version__
    from argusai.__main__ import _bootstrap_logging
    from argusai.core.infra.logging_setup import silence_console_logging
    from argusai.gui import Api

    # Ensure interactive chat never paints logging onto the REPL.
    os.environ.setdefault("ARGUSAI_LOG_CONSOLE", "0")
    _bootstrap_logging()
    silence_console_logging()
    api = Api.for_cli()
    sync_from_api(api)
    console = get_console()
    active_session = session_id
    pending_files = list(files or [])
    # Standing-team room inside this same REPL. Empty id means the main Agent.
    team_room: dict[str, str] = {"id": "", "name": "", "member_id": "", "member_role": ""}
    # Long-lived CLI hosts fire cron without the desktop GUI.
    if not once:
        try:
            api.start_assistant_cron_scheduler()
        except Exception:
            pass

    def _toolbar() -> str:
        return label(
            "toolbar",
            session=active_session[:18],
            lang=get_locale(),
        )

    set_toolbar_provider(_toolbar)
    use_ops_app = bool(console.is_terminal and not once and not json_out)

    try:
        first = opening

        if use_ops_app:
            def _header() -> str:
                import platform
                from datetime import datetime

                host = platform.system().upper()
                now = datetime.now().strftime("%H:%M:%S")
                in_team = bool(team_room.get("id"))
                who = (team_room.get("name") or active_session)[:18]
                mode = "TEAM" if in_team else "AGENT"
                hint = "/team leave · /quit" if in_team else "/help · /quit · Tab"
                return (
                    f"ARGUSAI ops  {__version__}   "
                    f"{who} · {host} · {mode} · {now}   "
                    f"{hint}"
                )

            def _session_snap() -> dict[str, Any]:
                try:
                    data = api.get_assistant_session(active_session)
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}

            def _jobs_snap() -> list[dict[str, Any]]:
                if team_room.get("id"):
                    return _team_monitor_jobs(api, team_room)
                if not hasattr(api, "list_assistant_run_jobs"):
                    return []
                try:
                    result = api.list_assistant_run_jobs(active_session)
                except Exception:
                    return []
                if not isinstance(result, dict) or not result.get("ok"):
                    return []
                jobs = [j for j in list(result.get("jobs") or []) if isinstance(j, dict)]
                terminal = {"completed", "failed", "cancelled"}
                jobs.sort(
                    key=lambda j: (
                        0 if str(j.get("state") or "").lower() not in terminal else 1,
                        -float(j.get("updated_at") or j.get("created_at") or 0),
                    )
                )
                return jobs[:12]

            def _metrics() -> dict[str, Any]:
                try:
                    root = api.settings_mgr.get().data_root
                except Exception:
                    root = None
                return sample_host_metrics(data_root=root)

            def _token_usage() -> dict[str, Any]:
                try:
                    if team_room.get("id"):
                        return _team_monitor_tokens(api, team_room)
                    data = api.get_assistant_token_usage(active_session)
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}

            def _allow(selector: str) -> None:
                lines = dispatch(
                    api, active_session, f"/allow {selector}".rstrip()
                ).lines
                ops.append_panel(lines)

            def _deny(selector: str) -> None:
                lines = dispatch(
                    api, active_session, f"/deny {selector}".rstrip()
                ).lines
                ops.append_panel(lines)

            ops = OpsApp(
                on_line=lambda _line: None,
                toolbar_provider=_toolbar,
                session_provider=_session_snap,
                jobs_provider=_jobs_snap,
                metrics_provider=_metrics,
                token_usage_provider=_token_usage,
                on_allow=_allow,
                on_deny=_deny,
                header_provider=_header,
            )

            def _echo(user_line: str, **kwargs: Any) -> None:
                if ops.take_pending_echo(user_line):
                    return
                ops.append_user_message(user_line, **kwargs)

            def _handle_line(user_text: str) -> None:
                nonlocal active_session, pending_files
                text = (user_text or "").strip()
                if not text:
                    return
                shell: bool | str | tuple[str, str] = False
                shell_candidate = looks_like_slash(text)
                if shell_candidate:
                    _echo(text)
                    # /allow all and /model switch ask y/n. That must not run
                    # on the prompt_toolkit UI thread — Confirm.ask steals
                    # stdin the TUI still owns, so the cursor blinks but
                    # keys never reach the nested prompt.
                    if needs_tty_confirm(text):

                        def _confirm_shell() -> None:
                            nonlocal active_session
                            session_ref = [active_session]
                            _try_shell(
                                api,
                                session_ref,
                                text,
                                version=__version__,
                                json_out=False,
                                ops=ops,
                                team_room=team_room,
                            )
                            active_session = session_ref[0]
                            ops.refresh_chrome(force=True)

                        run_ops_worker(_confirm_shell)
                        return
                    session_ref = [active_session]
                    shell = _try_shell(
                        api,
                        session_ref,
                        text,
                        version=__version__,
                        json_out=False,
                        ops=ops,
                        team_room=team_room,
                    )
                    active_session = session_ref[0]
                if shell == "quit":
                    ops.stop()
                    return
                if isinstance(shell, tuple):
                    text = shell[1]
                    shell = False
                if shell:
                    ops.refresh_chrome(force=True)
                    return

                # Main turn already running — do not stack another Agent loop.
                # Side questions use /btw (handled above as a shell command).
                if ops.busy:
                    if not shell_candidate:
                        _echo(text)
                    ops.append_markup(f"[hud.dim]{label('ops_busy_hint')}[/]")
                    return

                turn_files = list(pending_files)
                try:
                    text, attachments = _prepare_cli_turn(text, turn_files)
                except ValueError as exc:
                    if turn_files:
                        pending_files.clear()
                    if not shell_candidate:
                        _echo(text)
                    ops.append_markup(
                        f"[fail]✗ {escape(label('cli_attachment_error', error=str(exc)))}[/]"
                    )
                    return
                pending_files.clear()
                if not text and not attachments:
                    return
                if not shell_candidate:
                    _echo(text, attachments=attachments)
                elif attachments:
                    names = ", ".join(str(item.get("name") or "") for item in attachments)
                    ops.append_markup(
                        f"[meta]{escape(label('cli_attachment_summary', names=names))}[/]"
                    )

                ops.set_busy(True)

                def _work() -> None:
                    try:
                        if team_room.get("id"):
                            _run_team_turn(
                                api,
                                team_room,
                                text,
                                ops=ops,
                                json_out=False,
                                attachments=attachments,
                            )
                        else:
                            _run_turn(
                                api,
                                session_id=active_session,
                                user_text=text,
                                yes=yes,
                                timeout=timeout,
                                json_out=False,
                                ops=ops,
                                attachments=attachments,
                            )
                    except typer.Exit:
                        pass
                    except Exception as exc:
                        ops.append_markup(f"[fail]{exc}[/]")
                    finally:
                        ops.set_busy(False)
                        ops.refresh_chrome(force=True)

                run_ops_worker(_work)

            ops.set_on_line(_handle_line)
            ops.refresh_chrome(force=True)
            from argusai.cli.setup_hint import build_setup_hint_lines

            setup_lines = build_setup_hint_lines(api)
            if setup_lines:
                ops.append_panel(setup_lines, title=f"[amber]▸ {label('setup_title')}[/]")
            if first:
                _handle_line(first)
            ops.run()
            return

        # Linear / scripted path (--once, --json, or non-TTY).
        if not json_out:
            print_ops_hud(__version__, session_id=active_session, mode="AGENT")

        if first or pending_files:
            session_ref = [active_session]
            shell: bool | str | tuple[str, str] = False
            if first:
                shell = _try_shell(
                    api,
                    session_ref,
                    first,
                    version=__version__,
                    json_out=json_out,
                    team_room=team_room,
                )
            active_session = session_ref[0]
            if shell == "quit":
                return
            if isinstance(shell, tuple):
                first = shell[1]
                shell = False
            if shell is False:
                try:
                    first, attachments = _prepare_cli_turn(first, pending_files)
                except ValueError as exc:
                    error = label("cli_attachment_error", error=str(exc))
                    if json_out:
                        emit_result({"ok": False, "error": error})
                    else:
                        console.print(f"[fail]✗ {escape(error)}[/]")
                    raise typer.Exit(2) from exc
                pending_files.clear()
                if team_room.get("id"):
                    _run_team_turn(
                        api,
                        team_room,
                        first,
                        ops=None,
                        json_out=json_out,
                        attachments=attachments,
                    )
                    reply = ""
                else:
                    reply = _run_turn(
                        api,
                        session_id=active_session,
                        user_text=first,
                        yes=yes,
                        timeout=timeout,
                        json_out=json_out,
                        attachments=attachments,
                    )
                if json_out:
                    emit_result(
                        {
                            "ok": True,
                            "session_id": active_session,
                            "text": reply,
                            "done": True,
                        }
                    )
                    return
            elif json_out:
                emit_result({"ok": True, "session_id": active_session, "shell": True})
                return
            if once:
                return

        if once:
            return

        while True:
            try:
                user_text = ask_ops_line(_team_prompt(team_room)).strip()
            except (EOFError, KeyboardInterrupt):
                console.print(f"\n[hud.dim]{label('session_closed')}[/]")
                break
            if not user_text:
                continue

            session_ref = [active_session]
            shell = _try_shell(
                api,
                session_ref,
                user_text,
                version=__version__,
                json_out=False,
                team_room=team_room,
            )
            active_session = session_ref[0]
            if shell == "quit":
                break
            if isinstance(shell, tuple):
                user_text = shell[1]
                shell = False
            if shell:
                continue

            try:
                user_text, attachments = _prepare_cli_turn(
                    user_text,
                    pending_files,
                )
            except ValueError as exc:
                pending_files.clear()
                console.print(
                    f"[fail]✗ {escape(label('cli_attachment_error', error=str(exc)))}[/]"
                )
                continue
            pending_files.clear()
            if not user_text and not attachments:
                continue
            if team_room.get("id"):
                try:
                    _run_team_turn(
                        api,
                        team_room,
                        user_text,
                        ops=None,
                        json_out=False,
                        attachments=attachments,
                    )
                except KeyboardInterrupt:
                    stopped = api.stop_team_members(team_room.get("id") or "")
                    count = (
                        int((stopped or {}).get("stopped_count") or 0)
                        if isinstance(stopped, dict)
                        else 0
                    )
                    console.print(f"\n[ok]{label('team_room_stopped', count=count)}[/]")
            else:
                _run_turn(
                    api,
                    session_id=active_session,
                    user_text=user_text,
                    yes=yes,
                    timeout=timeout,
                    json_out=False,
                    attachments=attachments,
                )
    finally:
        api.shutdown_background_services()


def _team_monitor_jobs(api: Any, team_room: dict[str, str]) -> list[dict[str, Any]]:
    """Agent rail rows for the open team room, or the private member."""
    current = api.get_team(str(team_room.get("id") or ""))
    team = current.get("team") if isinstance(current, dict) else None
    if not isinstance(team, dict):
        return []
    member_id = str(team_room.get("member_id") or "")
    rows: list[dict[str, Any]] = []
    for member in list(team.get("members") or []):
        if not isinstance(member, dict):
            continue
        agent_id = str(member.get("agent_id") or "")
        if member_id and agent_id != member_id:
            continue
        rows.append({
            "job_id": agent_id,
            "label": str(member.get("role") or agent_id or "member"),
            "state": str(member.get("status") or "idle"),
        })
    return rows[:12]


def _team_monitor_tokens(api: Any, team_room: dict[str, str]) -> dict[str, Any]:
    """Token block for the team, or for the member when a private room is open."""
    team_id = str(team_room.get("id") or "")
    usage = api.get_team_token_usage(team_id)
    if not isinstance(usage, dict) or not usage.get("ok"):
        return {}
    member_id = str(team_room.get("member_id") or "")
    rows = [row for row in list(usage.get("members") or []) if isinstance(row, dict)]
    chosen = next((row for row in rows if str(row.get("agent_id") or "") == member_id), None) if member_id else None
    if chosen is not None:
        counters = chosen.get("usage") if isinstance(chosen.get("usage"), dict) else {}
        hit_rate = chosen.get("cache_hit_rate")
    else:
        counters = usage.get("total") if isinstance(usage.get("total"), dict) else {}
        hit_rate = usage.get("cache_hit_rate")
    payload: dict[str, Any] = {
        "ok": True,
        "token_usage": dict(counters),
        "cache_hit_rate": hit_rate,
    }
    if chosen is not None:
        current = api.get_team(team_id)
        team = current.get("team") if isinstance(current, dict) else None
        child_id = ""
        if isinstance(team, dict):
            for member in list(team.get("members") or []):
                if isinstance(member, dict) and str(member.get("agent_id") or "") == member_id:
                    child_id = str(member.get("child_session_id") or "")
                    break
        if child_id:
            live = api.get_assistant_token_usage(child_id)
            if isinstance(live, dict):
                for key in ("prompt_tokens", "prompt_bytes", "context_window", "last_prompt_tokens_est", "last_prompt_bytes"):
                    if live.get(key):
                        payload[key] = live.get(key)
    return payload


def _team_prompt(team_room: dict[str, str]) -> str:
    if not team_room.get("id"):
        return "ops › "
    who = f"@{team_room['member_role']} › " if team_room.get("member_role") else "team › "
    return f"{team_room.get('name') or 'team'} {who}"


def _team_rail_lines(api: Any, team_room: dict[str, str]) -> list[str]:
    """Left rail while a team room is open: members and the latest line."""
    current = api.get_team(str(team_room.get("id") or ""))
    team = current.get("team") if isinstance(current, dict) else None
    if not isinstance(team, dict):
        return [label("team_rail_leave"), label("team_rail_quit")]
    where = (
        label("team_rail_private", role=team_room.get("member_role") or "")
        if team_room.get("member_id")
        else label("team_rail_group")
    )
    status = team.get("status") if isinstance(team.get("status"), dict) else {}
    lines = [
        str(team.get("name") or "")[:28],
        f"{where}  {status.get('overall') or ''}".strip(),
        "",
    ]
    active = str(team_room.get("member_id") or "")
    members = [row for row in list(team.get("members") or []) if isinstance(row, dict)]
    if not members:
        lines.append(label("team_room_no_messages"))
    for member in members[:10]:
        role = str(member.get("role") or "member")[:16]
        mark = "»" if active and str(member.get("agent_id") or "") == active else " "
        state = str(member.get("status") or "")[:10]
        lines.append(f"{mark} {role}  {state}".rstrip())
        preview = str(member.get("summary") or member.get("status_message") or member.get("objective") or "")
        preview = preview.replace("\n", " ").strip()
        if preview:
            lines.append(f"  {preview[:26]}")
    lines.extend([
        "",
        label("team_rail_at"),
        label("team_rail_pm"),
        label("team_rail_leave"),
        label("team_rail_stop"),
        label("team_rail_quit"),
    ])
    return lines


def _team_mention_names(api: Any, team_room: dict[str, str]) -> list[str]:
    if not team_room.get("id"):
        return []
    current = api.get_team(str(team_room.get("id") or ""))
    team = current.get("team") if isinstance(current, dict) else None
    if not isinstance(team, dict):
        return []
    names: list[str] = []
    for member in list(team.get("members") or []):
        if isinstance(member, dict):
            role = str(member.get("role") or "").strip()
            if role:
                names.append(role)
    return names


def _sync_team_rail(api: Any, team_room: dict[str, str], ops: OpsApp | None) -> None:
    from argusai.cli.prompt import set_team_mentions

    set_team_mentions(_team_mention_names(api, team_room))
    if ops is None:
        return
    if not team_room.get("id"):
        ops.set_rail_lines(None)
        previous = team_room.pop("sidebar_was", "")
        if previous:
            ops.set_sidebar_mode(previous)
        return
    if not team_room.get("sidebar_was"):
        current = str(ops.sidebar_mode or "off")
        team_room["sidebar_was"] = current
        if current != "on":
            ops.set_sidebar_mode("on")
    ops.set_rail_lines(_team_rail_lines(api, team_room))


def _paint_team_messages(ops: OpsApp, rows: list[dict[str, Any]]) -> None:
    """Paint team rows as conversation bubbles, not command cards."""
    if not rows:
        ops.append_markup(f"[hud.dim]{label('team_room_no_messages')}[/]")
        return
    for item in rows:
        is_user = str(item.get("role") or "") == "user" or str(item.get("sender") or "") == "leader"
        who = label("team_room_you") if is_user else str(
            item.get("role_name") or item.get("sender_role") or item.get("role") or "member"
        )
        if item.get("type") == "activity":
            tools = ", ".join(str(name) for name in (item.get("tool_names") or []))
            if tools:
                ops.append_markup(f"[hud.dim]{escape(who)}[/]  [meta]{escape(tools)}[/]")
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        if is_user:
            ops.append_user_message(text)
        else:
            ops.append_speaker(who, text)


def _apply_team_room(result: Any, team_room: dict[str, str], ops: OpsApp | None) -> None:
    if result.leave_team:
        team_room.update(id="", name="", member_id="", member_role="")
    elif result.team_id:
        team_room.update(
            id=str(result.team_id),
            name=str(result.team_name or ""),
            member_id=str(result.team_member_id or ""),
            member_role=str(result.team_member_role or ""),
        )
    if ops is None or not (result.leave_team or result.team_id):
        return
    ops.set_dock_prompt(_team_prompt(team_room))
    ops.refresh_chrome(force=True)


def _run_team_turn(
    api: Any,
    team_room: dict[str, str],
    message: str,
    *,
    ops: OpsApp | None,
    json_out: bool,
    attachments: list[dict[str, Any]] | None = None,
    poll_seconds: float = 0.6,
    quiet_seconds: float = 1.2,
) -> None:
    """Send one line into the open team room and print member replies as they land."""
    from argusai.cli.commands.team import _visible_chat
    from argusai.cli.shell import _format_team_lines

    team_id = str(team_room.get("id") or "")
    member_id = str(team_room.get("member_id") or "")

    def _emit(lines: list[str]) -> None:
        if not lines:
            return
        if ops is not None:
            for line in lines:
                ops.append_markup(line)
        elif not json_out:
            for line in lines:
                get_console().print(line)

    before = api.get_team_chat(team_id)
    known = {
        str(item.get("id") or "")
        for item in list((before or {}).get("messages") or [])
        if isinstance(item, dict) and item.get("id")
    }
    result = api.send_team_message(
        team_id,
        message,
        [member_id] if member_id else [],
        "private" if member_id else "group",
        attachments,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        err = str((result or {}).get("error") or "send failed")
        _emit([f"[fail]{escape(err)}[/]"])
        return
    # Waiting is a status, not a transcript line. A member who passes
    # leaves no reply, so this must disappear when the room goes idle.
    if ops is not None:
        ops.set_status(label("team_room_sent", count=len(result.get("delivered") or [])))
    quiet_since = time.time()
    deadline = time.time() + 3600
    try:
        while time.time() < deadline:
            chat = api.get_team_chat(team_id)
            fresh: list[dict[str, Any]] = []
            for item in list((chat or {}).get("messages") or []):
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "")
                if item_id and item_id in known:
                    continue
                if item_id:
                    known.add(item_id)
                fresh.append(item)
            visible = _visible_chat(fresh, member_id)
            shown = [
                item for item in visible
                if not (
                    str(item.get("role") or "") == "user"
                    or str(item.get("sender") or "") == "leader"
                )
            ]
            if shown:
                quiet_since = time.time()
                if ops is not None:
                    _paint_team_messages(ops, shown)
                else:
                    _emit(_format_team_lines(shown))
            _sync_team_rail(api, team_room, ops)
            current = api.get_team(team_id)
            status: dict[str, Any] = {}
            if isinstance(current, dict) and isinstance(current.get("team"), dict):
                raw_status = current["team"].get("status")
                status = raw_status if isinstance(raw_status, dict) else {}
            if int(status.get("active") or 0) <= 0 and time.time() - quiet_since >= quiet_seconds:
                break
            time.sleep(poll_seconds)
    finally:
        if ops is not None:
            ops.set_status("")


def _try_shell(
    api: Any,
    session_ref: list[str],
    text: str,
    *,
    version: str,
    json_out: bool,
    ops: OpsApp | None = None,
    team_room: dict[str, str] | None = None,
) -> bool | str | tuple[str, str]:
    """Handle a local slash command.

    Returns:
      ``False`` — not a shell command (feed the Agent)
      ``True`` — consumed locally
      ``\"quit\"`` — leave the REPL
      ``("forward", text)`` — process text as the next Agent turn
    """
    confirm_fn = None
    if ops is not None:
        ops_app = ops

        def confirm_fn(prompt: str, default: bool = True) -> bool:
            return ops_app.ask_confirm(prompt, default=default)

    override = None
    if team_room and team_room.get("id"):
        override = team_room_greenlight_override(api, team_room["id"], text)
    result = override if override is not None else dispatch(
        api,
        session_ref[0],
        text,
        confirm=confirm_fn,
    )
    if result.team_greenlight:
        room_id = str((team_room or {}).get("id") or "")
        if room_id:
            result.lines = team_greenlight_lines(api, room_id, result.team_greenlight)
        else:
            result.lines = [f"[fail]{label('team_room_greenlight_usage')}[/]"]
    if result.stop_current_team:
        room_id = str((team_room or {}).get("id") or "")
        if room_id:
            stopped = api.stop_team_members(room_id)
            count = (
                int((stopped or {}).get("stopped_count") or 0)
                if isinstance(stopped, dict)
                else 0
            )
            result.lines = [f"[ok]{label('team_room_stopped', count=count)}[/]"]
        else:
            result.lines = [f"[fail]{label('team_room_stop_usage')}[/]"]
    if team_room and team_room.get("id") and text.strip().lower() == "/cancel":
        stopped = api.stop_team_members(team_room["id"])
        count = (
            int((stopped or {}).get("stopped_count") or 0)
            if isinstance(stopped, dict)
            else 0
        )
        result.lines.append(f"[ok]{label('team_room_stopped', count=count)}[/]")
    if team_room is not None and (result.leave_team or result.team_id):
        _apply_team_room(result, team_room, ops)
        _sync_team_rail(api, team_room, ops)
    if not result.handled:
        return False
    if result.quit:
        if not json_out and ops is None:
            get_console().print(f"[hud.dim]{label('session_closed')}[/]")
        return "quit"
    if result.session_id:
        session_ref[0] = result.session_id
    if result.clear:
        if ops is not None:
            ops.clear_screen()
            ops.refresh_chrome(force=True)
        elif not json_out:
            get_console().clear()
            print_ops_hud(version, session_id=session_ref[0], mode="AGENT")
        return True
    if result.monitor_mode and ops is not None:
        mode = ops.set_monitor_mode(result.monitor_mode)
        # Reflect actual mode in the already-built status line.
        if result.lines:
            result.lines[-1] = label("monitor_set", mode=mode)
    if result.sidebar_mode and ops is not None:
        mode = ops.set_sidebar_mode(result.sidebar_mode)
        if result.lines:
            result.lines[-1] = label("sidebar_set", mode=mode)
    forward_text = str(result.forward_text or "").strip()
    if json_out and not forward_text:
        emit_result(
            {
                "ok": True,
                "session_id": session_ref[0],
                "shell": True,
                "lines": result.lines,
            }
        )
        return True
    if ops is not None:
        if result.team_transcript is not None:
            for line in result.lines:
                ops.append_markup(line)
            _paint_team_messages(ops, result.team_transcript)
        elif result.lines:
            ops.append_panel(result.lines)
        if result.refresh:
            ops.refresh_chrome(force=True)
    else:
        if result.team_transcript is not None:
            from argusai.cli.shell import _format_team_lines

            extra = _format_team_lines(result.team_transcript) or [
                f"[hud.dim]{label('team_room_no_messages')}[/]"
            ]
            result.lines = [*result.lines, *extra, f"[hud.dim]{label('team_room_footer')}[/]"]
        print_shell_result(result)
    if forward_text:
        return ("forward", forward_text)
    return True


def _capture_console() -> Console:
    return Console(
        file=StringIO(),
        force_terminal=True,
        width=max(40, (get_console().width or 100) - 2),
        theme=ARGUS_THEME,
        highlight=False,
        color_system="truecolor",
    )


def _paint_card(ops: OpsApp | None, painter: Any) -> Any:
    """Run a card painter; when ``ops`` is set, capture into the transcript."""
    if ops is None:
        return painter(get_console())
    cons = _capture_console()
    out = painter(cons)
    buf = cons.file
    if isinstance(buf, StringIO):
        ops.append_ansi(buf.getvalue())
    return out


def _run_turn(
    api: Any,
    *,
    session_id: str,
    user_text: str,
    yes: bool,
    timeout: float,
    json_out: bool,
    ops: OpsApp | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    console = get_console()
    started = api.start_assistant_stream(
        {
            "session_id": session_id,
            "message": user_text,
            "attachments": list(attachments or []),
        }
    )
    if not started.get("ok"):
        err = started.get("error") or "failed to start"
        if json_out:
            emit_result({"ok": False, "error": err})
        elif ops is not None:
            ops.append_markup(f"[fail]  ✗ FAIL[/]  {err}")
        else:
            console.print(f"[fail]  ✗ FAIL[/]  {err}")
        raise typer.Exit(1)

    run_id = str(started.get("run_id") or "")
    if not run_id:
        raise typer.Exit(1)

    offset = 0
    text_parts: list[str] = []
    pending_waits: list[dict[str, Any]] = []
    deadline = time.time() + max(5.0, float(timeout))
    saw_delta = False
    status_label = "linking context"
    printed_tools: set[str] = set()
    printed_cards: set[str] = set()
    reply = ReplyStream(console) if ops is None else None

    status: Status | None = None
    if ops is None and not json_out and console.is_terminal:
        status = console.status(
            f"[hud.dim]▸ {label('tag_think')}[/]  {status_label}…",
            spinner="dots",
        )
        status.start()

    def _set_status(text: str) -> None:
        nonlocal status_label
        status_label = text[:72] or "working"
        if ops is not None:
            ops.set_status(status_label)
            return
        if status is not None:
            status.update(f"[hud.dim]▸ {label('tag_think')}[/]  {status_label}…")

    def _stop_status() -> None:
        nonlocal status
        if status is not None:
            status.stop()
            status = None

    def _ensure_status() -> None:
        nonlocal status
        if ops is not None or json_out or not console.is_terminal or status is not None:
            return
        if reply is not None and reply.pending:
            return
        status = console.status(
            f"[hud.dim]▸ {label('tag_think')}[/]  {status_label}…",
            spinner="dots",
        )
        status.start()

    def _flush_reply() -> None:
        if json_out:
            return
        _stop_status()
        if ops is not None:
            ops.flush_reply()
        elif reply is not None:
            reply.flush()

    def _emit_delta(piece: str) -> None:
        nonlocal saw_delta
        if not piece:
            return
        text_parts.append(piece)
        saw_delta = True
        if json_out:
            return
        _stop_status()
        if ops is not None:
            ops.feed_reply(piece)
        elif reply is not None:
            reply.feed(piece)

    def _emit_tool_line(name: str, *, detail: str = "") -> None:
        key = name.strip()
        if not key or key in printed_tools or json_out:
            return
        printed_tools.add(key)
        _flush_reply()
        _stop_status()
        if ops is not None:
            ops.tool_exec(key, detail)
        else:
            print_tool_exec(console, key, detail=detail)
        _ensure_status()
        _set_status(f"tool · {key}")

    def _card_key(block: dict[str, Any]) -> str:
        btype = str(block.get("type") or "")
        bid = str(block.get("id") or "")
        if bid:
            return f"{btype}:{bid}"
        # Fall back to a content-derived key so re-streamed blocks without a
        # stable id do not paint twice within one turn.
        digest = str(
            block.get("title")
            or block.get("name")
            or block.get("subtitle")
            or block.get("summary")
            or ""
        )
        return f"{btype}:{digest}"

    def _paint_ui_card(block: dict[str, Any]) -> None:
        if not isinstance(block, dict) or json_out:
            return
        key = _card_key(block)
        if key in printed_cards:
            return
        drawn_flag = {"drawn": False}

        def _draw(cons: Any) -> Any:
            drawn = print_ui_card(block, console=cons)
            drawn_flag["drawn"] = bool(drawn)
            return drawn

        _flush_reply()
        _stop_status()
        _paint_card(ops, _draw)
        if drawn_flag["drawn"]:
            printed_cards.add(key)
        _ensure_status()

    try:
        while time.time() < deadline:
            chunk = api.get_assistant_stream_events(run_id, offset)
            if not chunk.get("ok"):
                time.sleep(0.1)
                continue
            events = chunk.get("events") or []
            offset = int(chunk.get("next") or offset)

            for event in events:
                if not isinstance(event, dict):
                    continue
                et = str(event.get("type") or event.get("kind") or "")

                if et in {"delta", "text_delta", "assistant_delta", "text_chunk"}:
                    piece = str(
                        event.get("text")
                        or event.get("delta")
                        or (event.get("data") or {}).get("text")
                        or ""
                    )
                    _emit_delta(piece)

                elif et in {"status", "stage"}:
                    stage = str(
                        event.get("text")
                        or (event.get("data") or {}).get("label")
                        or ""
                    ).strip()
                    if stage:
                        compact = stage
                        for prefix in ("正在", "已生成", "（", "("):
                            if prefix in compact and len(compact) > 24:
                                compact = compact.split("（")[0].split("(")[0].strip()
                                break
                        _set_status(compact or stage)
                    status_code = str(event.get("status") or "")
                    if status_code in {
                        "waiting_user",
                        "awaiting_confirmation",
                        "awaiting_user_decision",
                    }:
                        pending_waits.append(event)

                elif et in {"block", "block_upsert", "ui_block"}:
                    block = event.get("block")
                    if not isinstance(block, dict):
                        data = event.get("data")
                        block = data.get("block") if isinstance(data, dict) else event
                    block = block if isinstance(block, dict) else {}
                    btype = str(block.get("type") or "")
                    if btype in _TOOL_CONFIRM_TYPES or btype in _DECISION_TYPES:
                        pending_waits.append(block)
                    name = str(block.get("name") or block.get("title") or block.get("tool") or "")
                    if btype in {"tool_trace", "tool_call", "tool_activity"}:
                        if name:
                            _emit_tool_line(name, detail=tool_detail_from_block(block))
                    elif btype == "tool_result" and name:
                        # Avoid a second EXEC line for the matching result card.
                        _set_status(f"tool · {name}")
                    else:
                        _paint_ui_card(block)

                elif et in {"segment", "agent_round_start"}:
                    round_i = event.get("round") or (event.get("data") or {}).get("round")
                    if round_i:
                        _set_status(f"round {round_i}")

                elif et in {"done", "run_done"}:
                    result = event.get("result") if isinstance(event.get("result"), dict) else {}
                    if not saw_delta:
                        fallback = str(result.get("text") or result.get("reply") or "").strip()
                        if fallback:
                            _emit_delta(fallback)
                    st = str(result.get("status") or "")
                    if st in {
                        "awaiting_confirmation",
                        "pending_confirmation",
                        "awaiting_user_decision",
                        "pending_user_decision",
                    }:
                        pending_waits.append(result)
                    # Some cards (e.g. user_decision) are final-only and never
                    # arrive as ``block_upsert``. Tool-return payloads already
                    # streamed next to EXEC — do not dump them under the reply.
                    # Everything else has already streamed; dedup by id/title.
                    for final_block in list(result.get("blocks") or []):
                        if not isinstance(final_block, dict):
                            continue
                        ftype = str(final_block.get("type") or "")
                        if ftype in _TOOL_CONFIRM_TYPES or ftype in _DECISION_TYPES:
                            pending_waits.append(final_block)
                            continue
                        if ftype in _TOOL_RETURN_CARD_TYPES:
                            continue
                        _paint_ui_card(final_block)

                elif et in {"error", "run_error"}:
                    _flush_reply()
                    _stop_status()
                    err = str(
                        event.get("error")
                        or (event.get("data") or {}).get("message")
                        or "error"
                    )
                    if ops is not None:
                        ops.append_markup(f"[fail]  ✗ FAIL[/]  {err}")
                    else:
                        console.print(f"[fail]  ✗ FAIL[/]  {err}")
                    raise typer.Exit(1)

            job = chunk.get("job") if isinstance(chunk.get("job"), dict) else {}
            job_state = str(job.get("state") or "").lower()
            stream_done = bool(chunk.get("done"))
            decisions, confirms = _extract_waitables(pending_waits)
            # user_decision cards are final-only: they arrive on the ``done``
            # event. An earlier ``waiting_user`` job state must NOT be treated
            # as a blind tool AUTH — that is what produced "allow confirm?".
            needs_wait = bool(decisions or confirms) or (
                stream_done
                and (
                    pending_waits
                    or job_state in {"waiting_user"}
                )
            )
            if needs_wait and not decisions and not confirms:
                hydrated = _hydrate_waits_from_session(api, session_id)
                if hydrated:
                    pending_waits.extend(hydrated)
                    decisions, confirms = _extract_waitables(pending_waits)
            if needs_wait and (decisions or confirms or stream_done):
                _flush_reply()
                _stop_status()
                handled = _handle_waits(
                    api,
                    session_id=session_id,
                    run_id=run_id,
                    waits=pending_waits,
                    yes=yes,
                    json_out=json_out,
                    ops=ops,
                )
                pending_waits.clear()
                if handled is False:
                    if json_out:
                        emit_result(
                            {
                                "ok": False,
                                "error": "confirmation required",
                                "partial": "".join(text_parts),
                            }
                        )
                    elif ops is not None:
                        ops.append_markup(
                            f"[warn]▸ {label('tag_auth')}[/]  {label('auth_abort')}"
                        )
                    else:
                        console.print(
                            f"[warn]▸ {label('tag_auth')}[/]  {label('auth_abort')}"
                        )
                    return "".join(text_parts).strip()
                if isinstance(handled, str) and handled:
                    # Decision/confirm always arrives on a finished stream;
                    # the resume starts a *new* run_id that we must keep
                    # polling — do not break just because the prior chunk
                    # was ``done`` (that left the REPL silent after "3").
                    run_id = handled
                    offset = 0
                    saw_delta = False
                    text_parts.clear()
                    if ops is not None:
                        ops.discard_reply()
                    elif reply is not None:
                        reply.discard()
                    printed_tools.clear()
                    deadline = time.time() + max(5.0, float(timeout))
                    _ensure_status()
                    _set_status(label("resumed"))
                    time.sleep(0.05)
                    continue

            if stream_done:
                _flush_reply()
                break
            time.sleep(0.05)
        else:
            _flush_reply()
            _stop_status()
            if json_out:
                emit_result({"ok": False, "error": "timeout", "partial": "".join(text_parts)})
            elif ops is not None:
                ops.append_markup(f"[warn]▸ {label('timeout')}[/]")
            else:
                console.print(f"\n[warn]▸ {label('timeout')}[/]")
            raise typer.Exit(4)
    finally:
        _stop_status()
        if not json_out:
            if ops is not None and ops.pending_reply:
                try:
                    ops.flush_reply()
                except Exception:
                    ops.discard_reply()
            elif reply is not None and reply.pending:
                try:
                    reply.flush()
                except Exception:
                    reply.discard()

    final = "".join(text_parts).strip()
    if json_out:
        return final

    if ops is not None:
        return final

    if not final:
        console.print(f"[hud.dim]  {label('empty_reply')}[/]")
    else:
        ops_rule()
    console.print()
    return final


def _extract_waitables(
    waits: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decisions: list[dict[str, Any]] = []
    confirms: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(block: dict[str, Any]) -> None:
        btype = str(block.get("type") or "")
        key = (
            f"{btype}:"
            f"{block.get('question') or block.get('title') or block.get('name') or id(block)}"
        )
        if key in seen:
            return
        seen.add(key)
        if btype in _DECISION_TYPES:
            decisions.append(block)
        elif btype in _TOOL_CONFIRM_TYPES:
            confirms.append(block)

    for item in waits:
        if not isinstance(item, dict):
            continue
        btype = str(item.get("type") or "")
        if btype in _DECISION_TYPES or btype in _TOOL_CONFIRM_TYPES:
            _add(item)
            continue
        for key in ("blocks", "pending_blocks", "ui_blocks"):
            for block in list(item.get(key) or []):
                if isinstance(block, dict):
                    _add(block)
        # Bare waiting statuses are NOT tool confirms — decisions often
        # arrive a moment later on the done payload / session history.
    return decisions, confirms


def _hydrate_waits_from_session(api: Any, session_id: str) -> list[dict[str, Any]]:
    """Pull unresolved decision/confirm cards from the live session."""
    try:
        status = api.get_assistant_session(session_id)
    except Exception:
        return []
    if not isinstance(status, dict) or not status.get("ok"):
        return []
    found: list[dict[str, Any]] = []
    for block in list(status.get("pending_blocks") or []):
        if isinstance(block, dict):
            found.append(block)
    for tool in list(status.get("pending_tools") or []):
        if not isinstance(tool, dict):
            continue
        # Normalize session pending_tools into a confirm block shape.
        block = dict(tool)
        if str(block.get("type") or "") not in _TOOL_CONFIRM_TYPES:
            block["type"] = "tool_confirmation"
        found.append(block)
    # user_decision lives on the latest assistant message, not pending_tools.
    for msg in reversed(list(status.get("history") or [])):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for block in list(msg.get("blocks") or []):
            if isinstance(block, dict) and str(block.get("type") or "") in _DECISION_TYPES:
                # Skip already-answered cards.
                if block.get("selected") or block.get("decision") or block.get("submitted"):
                    continue
                found.append(block)
        break
    return found


def _pick_option(
    options: list[str],
    *,
    prompt_key: str = "decision_ask",
    ops: OpsApp | None = None,
) -> str | None:
    prompt = f"{label(prompt_key)} › "
    raw = (
        ops.ask_choice(options, prompt)
        if ops is not None
        else ask_choice_line(prompt)
    )
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]
    folded = raw.casefold()
    for opt in options:
        if opt.casefold() == folded:
            return opt
    return raw


def _handle_user_decision(
    api: Any,
    *,
    session_id: str,
    block: dict[str, Any],
    json_out: bool,
    ops: OpsApp | None = None,
) -> bool | str:
    if json_out:
        return False
    btype = str(block.get("type") or "")
    if btype == "user_decision_batch":
        questions = _paint_card(
            ops,
            lambda cons: print_batch_decision_panel(block, console=cons),
        )
        if not questions:
            return True
        answers: dict[str, str] = {}
        lines: list[str] = []
        for i, q in enumerate(questions, start=1):
            ask_line = f"[amber]{label('decision_batch_ask', n=i)}[/]  {q['question']}"
            if ops is not None:
                ops.append_markup(ask_line)
            else:
                get_console().print(ask_line)
            picked = _pick_option(q["options"], ops=ops)
            if not picked:
                return False
            answers[q["id"]] = picked
            lines.append(f"{q['question']}: {picked}")
        message = "\n".join(lines) if lines else json.dumps(
            {"type": "user_decision_batch_response", "answers": answers},
            ensure_ascii=False,
        )
    else:
        options = _paint_card(
            ops,
            lambda cons: print_decision_panel(block, console=cons),
        )
        picked = _pick_option(options, ops=ops)
        if not picked:
            return False
        message = picked

    started = api.start_assistant_stream(
        {
            "session_id": session_id,
            "message": message,
            "hide_user_message": True,
            "message_source": "cli_decision",
        }
    )
    if not started.get("ok"):
        err = f"[fail]{started.get('error') or 'failed'}[/]"
        if ops is not None:
            ops.append_markup(err)
        else:
            get_console().print(err)
        return False
    return str(started.get("run_id") or "") or True


def _handle_tool_confirms(
    api: Any,
    *,
    session_id: str,
    run_id: str,
    confirms: list[dict[str, Any]],
    yes: bool,
    json_out: bool,
    ops: OpsApp | None = None,
) -> bool | str:
    if not confirms:
        return True
    high_risk = False
    titles: list[str] = []
    for item in confirms:
        titles.append(
            str(item.get("title") or item.get("name") or item.get("tool") or "confirm")
        )
        risk = str(item.get("risk") or item.get("risk_level") or "").lower()
        if risk in {"high", "critical", "danger"} or item.get("no_auto_approve"):
            high_risk = True
    name = ", ".join(titles[:3])
    console = get_console()
    if not json_out:
        _paint_card(
            ops,
            lambda cons: print_confirm_panel(
                confirms[0], high_risk=high_risk, console=cons
            ),
        )
    if json_out and (high_risk or not yes):
        return False
    ask = label("auth_high_risk_ask" if high_risk else "auth_allow_ask", name=name)
    if high_risk:
        if ops is None and not console.is_terminal:
            return False
        allowed = (
            ops.ask_confirm(f"▸ {label('tag_auth')} {ask}", default=False)
            if ops is not None
            else Confirm.ask(f"[warn]▸ {label('tag_auth')}[/] {ask}", default=False)
        )
        if not allowed:
            api.start_assistant_action_stream(
                {"session_id": session_id, "job_id": run_id, "action": "deny"}
            )
            return False
        started = api.start_assistant_action_stream(
            {"session_id": session_id, "job_id": run_id, "action": "allow"}
        )
        return str(started.get("run_id") or "") or True
    if yes:
        allowed = True
    elif ops is not None:
        allowed = ops.ask_confirm(f"▸ {label('tag_auth')} {ask}", default=True)
    elif console.is_terminal:
        allowed = Confirm.ask(f"[amber]▸ {label('tag_auth')}[/] {ask}", default=True)
    else:
        allowed = False
    if allowed:
        started = api.start_assistant_action_stream(
            {"session_id": session_id, "job_id": run_id, "action": "allow"}
        )
        return str(started.get("run_id") or "") or True
    api.start_assistant_action_stream(
        {"session_id": session_id, "job_id": run_id, "action": "deny"}
    )
    return False


def _handle_waits(
    api: Any,
    *,
    session_id: str,
    run_id: str,
    waits: list[dict[str, Any]],
    yes: bool,
    json_out: bool,
    ops: OpsApp | None = None,
) -> bool | str:
    decisions, confirms = _extract_waitables(waits)
    if decisions:
        return _handle_user_decision(
            api,
            session_id=session_id,
            block=decisions[0],
            json_out=json_out,
            ops=ops,
        )
    if confirms:
        return _handle_tool_confirms(
            api,
            session_id=session_id,
            run_id=run_id,
            confirms=confirms,
            yes=yes,
            json_out=json_out,
            ops=ops,
        )
    if json_out:
        return False
    hint = f"[amber]▸ {label('tag_choice')}[/]  {label('decision_pick_hint')}"
    if ops is not None:
        ops.append_markup(hint)
        typed = ops.ask_line(f"{label('decision_ask')} › ")
    else:
        get_console().print(hint)
        typed = ask_choice_line(f"{label('decision_ask')} › ")
    if not typed:
        return False
    started = api.start_assistant_stream(
        {"session_id": session_id, "message": typed}
    )
    if not started.get("ok"):
        return False
    return str(started.get("run_id") or "") or True
