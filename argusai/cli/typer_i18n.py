"""Localize Typer/Click help chrome and command blurbs to the active CLI locale."""

from __future__ import annotations

from typing import Any

from argusai.cli.locale import get_locale, label

# click command path (tuple of names) → locale label key
_COMMAND_HELP: dict[tuple[str, ...], str] = {
    (): "help_root",
    ("channels",): "help_channels",
    ("channels", "providers"): "help_channels_providers",
    ("channels", "list"): "help_channels_list",
    ("channels", "add"): "help_channels_add",
    ("channels", "enable"): "help_channels_enable",
    ("channels", "disable"): "help_channels_disable",
    ("channels", "remove"): "help_channels_remove",
    ("channels", "test"): "help_channels_test",
    ("channels", "pair"): "help_channels_pair",
    ("channels", "identities"): "help_channels_identities",
    ("channels", "bind"): "help_channels_bind",
    ("channels", "grant"): "help_channels_grant",
    ("channels", "login-qq"): "help_channels_login_qq",
    ("channels", "login-wechat"): "help_channels_login_wechat",
    ("channels", "serve"): "help_channels_serve",
    ("ai",): "help_ai",
    ("ai", "list"): "help_ai_list",
    ("ai", "providers"): "help_ai_providers",
    ("ai", "add"): "help_ai_add",
    ("ai", "enable"): "help_ai_enable",
    ("ai", "disable"): "help_ai_disable",
    ("ai", "remove"): "help_ai_remove",
    ("ai", "activate"): "help_ai_activate",
    ("ai", "test"): "help_ai_test",
    ("agent",): "help_agent",
    ("agent", "list"): "help_agent_list",
    ("agent", "add"): "help_agent_add",
    ("agent", "enable"): "help_agent_enable",
    ("agent", "disable"): "help_agent_disable",
    ("agent", "remove"): "help_agent_remove",
    ("agent", "test"): "help_agent_test",
    ("chat",): "help_chat",
    ("doctor",): "help_doctor",
    ("proxy",): "help_proxy",
    ("proxy", "break"): "help_proxy_break",
    ("proxy", "break", "on"): "help_proxy_break_on",
    ("proxy", "break", "off"): "help_proxy_break_off",
    ("proxy", "break", "list"): "help_proxy_break_list",
    ("proxy", "break", "forward"): "help_proxy_break_forward",
    ("proxy", "break", "drop"): "help_proxy_break_drop",
    ("team",): "help_team",
    ("team", "list"): "help_team_list",
    ("team", "create"): "help_team_create",
    ("team", "show"): "help_team_show",
    ("team", "delete"): "help_team_delete",
    ("team", "add-member"): "help_team_add_member",
    ("team", "remove-member"): "help_team_remove_member",
    ("team", "chat"): "help_team_chat",
    ("team", "send"): "help_team_send",
    ("team", "stop"): "help_team_stop",
    ("team", "greenlight"): "help_team_greenlight",
    ("report",): "help_report",
    ("scan",): "help_scan",
    ("version",): "help_version",
    ("update",): "help_update",
    ("kb",): "help_kb",
    ("kb", "list"): "help_kb_list",
    ("kb", "search"): "help_kb_search",
    ("kb", "show"): "help_kb_show",
    ("kb", "add"): "help_kb_add",
    ("kb", "delete"): "help_kb_delete",
    ("kb", "trash"): "help_kb_trash",
    ("kb", "restore"): "help_kb_restore",
    ("kb", "purge"): "help_kb_purge",
    ("mcp",): "help_mcp",
    ("mcp", "list"): "help_mcp_list",
    ("mcp", "add"): "help_mcp_add",
    ("mcp", "remove"): "help_mcp_remove",
    ("mcp", "enable"): "help_mcp_enable",
    ("mcp", "disable"): "help_mcp_disable",
    ("mcp", "test"): "help_mcp_test",
    ("mcp", "probe"): "help_mcp_probe",
    ("reverse",): "help_reverse",
    ("reverse", "list"): "help_reverse_list",
    ("reverse", "create"): "help_reverse_create",
    ("reverse", "show"): "help_reverse_show",
    ("reverse", "trash"): "help_reverse_trash",
    ("reverse", "restore"): "help_reverse_restore",
    ("reverse", "purge"): "help_reverse_purge",
    ("reverse", "export"): "help_reverse_export",
    ("reverse", "toolchain"): "help_reverse_toolchain",
    ("todos",): "help_todos",
    ("todos", "list"): "help_todos_list",
    ("todos", "add"): "help_todos_add",
    ("todos", "show"): "help_todos_show",
    ("todos", "done"): "help_todos_done",
    ("todos", "status"): "help_todos_status",
    ("todos", "delete"): "help_todos_delete",
    ("cron",): "help_cron",
    ("cron", "list"): "help_cron_list",
    ("cron", "add"): "help_cron_add",
    ("cron", "enable"): "help_cron_enable",
    ("cron", "disable"): "help_cron_disable",
    ("cron", "remove"): "help_cron_remove",
    ("cron", "runs"): "help_cron_runs",
    ("cron", "serve"): "help_cron_serve",
    ("serve",): "help_serve",
    ("serve", "run"): "help_serve_run",
    ("serve", "install"): "help_serve_install",
    ("serve", "boot"): "help_serve_boot",
    ("serve", "uninstall"): "help_serve_uninstall",
    ("serve", "start"): "help_serve_start",
    ("serve", "stop"): "help_serve_stop",
    ("serve", "restart"): "help_serve_restart",
    ("serve", "status"): "help_serve_status",
}

# option long-opt → label key (applied wherever that opt appears)
_OPTION_HELP: dict[str, str] = {
    "--help": "help_opt_help",
    "--json": "help_opt_json",
    "--verbose": "help_opt_verbose",
    "--gui": "help_opt_gui",
    "--tray": "help_opt_tray",
    "--chat": "help_opt_chat",
    "--yes": "help_opt_yes",
    "--enable": "help_opt_enable",
    "--disable": "help_opt_disable",
    "--id": "help_opt_id",
    "--name": "help_opt_name",
    "--secret": "help_opt_secret",
    "--setting": "help_opt_setting",
    "--host": "help_opt_host",
    "--port": "help_opt_port",
    "--path": "help_opt_path",
    "--session": "help_opt_session",
    "--timeout": "help_opt_timeout",
    "--command": "help_opt_command",
    "--arg": "help_opt_arg",
    "--env": "help_opt_env",
    "--cwd": "help_opt_cwd",
    "--notes": "help_opt_notes",
    "--cron-only": "help_opt_cron_only",
    "--boot": "help_opt_boot",
    "--no-start": "help_opt_no_start",
    "--dry-run": "help_opt_dry_run",
    "--start": "help_opt_start",
    "--once": "help_opt_once",
    "--system": "help_opt_system",
    "--url": "help_opt_url",
    "--base-url": "help_opt_base_url",
    "--api-key": "help_opt_api_key",
    "--provider": "help_opt_provider",
    "--model": "help_opt_model",
    "--protocol": "help_opt_protocol",
    "--priority": "help_opt_priority",
    "--max-tokens": "help_opt_max_tokens",
    "--context-window": "help_opt_context_window",
    "--extra-body": "help_opt_extra_body",
    "--activate": "help_opt_activate",
    "--index": "help_opt_index",
    "--transport": "help_opt_transport",
    "--header": "help_opt_header",
    "--tag": "help_opt_tag",
    "--limit": "help_opt_limit",
    "--title": "help_opt_title",
    "--body": "help_opt_body",
    "--file": "help_opt_file",
    "--reason": "help_opt_reason",
    "--trash": "help_opt_trash",
    "--superseded": "help_opt_superseded",
    "--max-chars": "help_opt_max_chars",
    "--scope": "help_opt_scope",
    "--status": "help_opt_status",
    "--intent": "help_opt_intent",
    "--platform": "help_opt_platform",
    "--task": "help_opt_task",
    "--dest": "help_opt_dest",
    "--decompiled": "help_opt_decompiled",
    "--folder": "help_opt_folder",
    "--job": "help_opt_job",
    "--tool": "help_opt_tool",
    "--due": "help_opt_due",
    "--prompt": "help_opt_prompt",
    "--every": "help_opt_every",
    "--cron": "help_opt_cron_expr",
    "--destination": "help_opt_destination",
    "--checks": "help_opt_checks",
    "--out": "help_opt_out",
    "--format": "help_opt_format",
    "--task-dir": "help_opt_task_dir",
    "--task-id": "help_opt_task_id",
    "--install-completion": "help_opt_install_completion",
    "--show-completion": "help_opt_show_completion",
    "--older-days": "help_opt_older_days",
    "--all": "help_opt_all",
}

# Command-scoped option help (wins over ``_OPTION_HELP`` when present).
_OPTION_HELP_CTX: dict[tuple[str, ...], str] = {
    ("update", "--check"): "help_opt_update_check",
    ("update", "--download"): "help_opt_update_download",
    ("chat", "--file"): "help_opt_chat_file",
    ("kb", "purge", "--id"): "help_opt_kb_purge_id",
    ("channels", "grant", "--all"): "help_opt_channels_grant_all",
}

# (command path…, argument name) → label key
_ARGUMENT_HELP: dict[tuple[str, ...], str] = {
    ("channels", "add", "provider"): "help_arg_provider",
    ("channels", "enable", "channel_id"): "help_arg_channel_id",
    ("channels", "disable", "channel_id"): "help_arg_channel_id",
    ("channels", "remove", "channel_id"): "help_arg_channel_id",
    ("channels", "test", "channel_id"): "help_arg_channel_id",
    ("channels", "pair", "channel_id"): "help_arg_channel_id",
    ("channels", "login-qq", "channel_id"): "help_arg_channel_id",
    ("channels", "login-wechat", "channel_id"): "help_arg_channel_id",
    ("channels", "bind", "identity_id"): "help_arg_identity_id",
    ("channels", "bind", "session_id"): "help_arg_session_id",
    ("agent", "enable", "runtime_id"): "help_arg_runtime_id",
    ("agent", "disable", "runtime_id"): "help_arg_runtime_id",
    ("agent", "remove", "runtime_id"): "help_arg_runtime_id",
    ("agent", "test", "runtime_id"): "help_arg_runtime_id",
    ("mcp", "remove", "server_id"): "help_arg_mcp_server_id",
    ("mcp", "enable", "server_id"): "help_arg_mcp_server_id",
    ("mcp", "disable", "server_id"): "help_arg_mcp_server_id",
    ("mcp", "test", "server_id"): "help_arg_mcp_server_id",
    ("kb", "search", "query"): "help_arg_kb_query",
    ("kb", "show", "entry_id"): "help_arg_kb_entry_id",
    ("kb", "delete", "entry_id"): "help_arg_kb_entry_id",
    ("kb", "restore", "entry_id"): "help_arg_kb_entry_id",
    ("reverse", "show", "job_id"): "help_arg_reverse_job_id",
    ("reverse", "trash", "job_id"): "help_arg_reverse_job_id",
    ("reverse", "restore", "job_id"): "help_arg_reverse_job_id",
    ("reverse", "purge", "job_id"): "help_arg_reverse_job_id",
    ("reverse", "export", "job_id"): "help_arg_reverse_job_id",
    ("todos", "add", "title"): "help_arg_todo_title",
    ("todos", "show", "todo_id"): "help_arg_todo_id",
    ("todos", "done", "todo_id"): "help_arg_todo_id",
    ("todos", "status", "todo_id"): "help_arg_todo_id",
    ("todos", "status", "status"): "help_arg_todo_status",
    ("todos", "delete", "todo_id"): "help_arg_todo_id",
    ("cron", "enable", "job_id"): "help_arg_cron_job_id",
    ("cron", "disable", "job_id"): "help_arg_cron_job_id",
    ("cron", "remove", "job_id"): "help_arg_cron_job_id",
    ("cron", "runs", "job_id"): "help_arg_cron_job_id",
    ("serve", "boot", "policy"): "help_arg_boot_policy",
    ("scan", "url"): "help_arg_scan_url",
    ("chat", "message"): "help_arg_chat_message",
}


def apply_typer_locale_chrome() -> None:
    """Translate Rich-help panel titles when locale is Chinese."""
    if get_locale() != "zh":
        return
    try:
        import typer.rich_utils as ru

        ru.OPTIONS_PANEL_TITLE = "选项"
        ru.COMMANDS_PANEL_TITLE = "命令"
        if hasattr(ru, "ARGUMENTS_PANEL_TITLE"):
            ru.ARGUMENTS_PANEL_TITLE = "参数"
    except Exception:
        pass


def _patch_help_option(cmd: Any) -> None:
    """Click builds ``--help`` lazily; rewrite its help string when created."""
    if getattr(cmd, "_argusai_help_localized", False):
        return
    original = getattr(cmd, "get_help_option", None)
    if not callable(original):
        return

    def get_help_option(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        opt = original(ctx, *args, **kwargs)
        if opt is None:
            return None
        text = label("help_opt_help")
        if text and text != "help_opt_help":
            opt.help = text
        return opt

    cmd.get_help_option = get_help_option  # type: ignore[method-assign]
    cmd._argusai_help_localized = True


def localize_click_command(root: Any) -> Any:
    """Rewrite help text on a Click command tree; returns ``root`` for chaining."""
    apply_typer_locale_chrome()

    def _walk(cmd: Any, path: tuple[str, ...]) -> None:
        key = _COMMAND_HELP.get(path)
        if key:
            text = label(key)
            if text and text != key:
                cmd.help = text
                if getattr(cmd, "short_help", None) is not None:
                    cmd.short_help = text.split("\n", 1)[0][:120]
        _patch_help_option(cmd)
        for param in list(getattr(cmd, "params", None) or []):
            opts = list(getattr(param, "opts", None) or [])
            applied = False
            for opt in opts:
                okey = _OPTION_HELP_CTX.get(path + (opt,)) or _OPTION_HELP.get(opt)
                if not okey:
                    continue
                text = label(okey)
                if text and text != okey:
                    param.help = text
                    applied = True
            if not applied:
                # Positional arguments use ``name`` instead of ``--flag``.
                name = str(getattr(param, "name", "") or "")
                akey = _ARGUMENT_HELP.get(path + (name,))
                if akey:
                    text = label(akey)
                    if text and text != akey:
                        param.help = text
        group = getattr(cmd, "commands", None)
        if isinstance(group, dict):
            for name, child in group.items():
                _walk(child, path + (str(name),))

    _walk(root, ())
    return root


def build_localized_click_app(typer_app: Any) -> Any:
    """Materialize the Click app and apply locale rewrites (call once per process)."""
    from typer.main import get_command

    return localize_click_command(get_command(typer_app))
