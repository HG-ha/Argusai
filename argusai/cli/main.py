"""Typer entry point for ``argusai`` / ``python -m argusai``."""

from __future__ import annotations

import os
import sys
from typing import Optional

import typer

from argusai.cli.ui import apply_typer_theme, print_banner, set_json_mode

apply_typer_theme()

app = typer.Typer(
    name="argusai",
    help=(
        "ArgusAI Security Workbench — local · scriptable · safe-by-default.\n\n"
        "Bare invoke: interactive console → ops console; otherwise desktop GUI "
        "(override with --gui / --chat, or ARGUSAI_HOST=gui|cli)."
    ),
    no_args_is_help=False,
    rich_markup_mode="rich",
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

_commands_registered = False


def _register_commands() -> None:
    global _commands_registered
    if _commands_registered:
        return
    from argusai.cli.commands import (
        agent,
        ai,
        channels,
        chat,
        cron,
        doctor,
        kb,
        mcp,
        proxy,
        report,
        team,
        reverse_cmd,
        scan,
        serve,
        todos,
        update,
    )

    app.add_typer(channels.app, name="channels")
    app.add_typer(ai.app, name="ai")
    app.add_typer(agent.app, name="agent")
    app.add_typer(kb.app, name="kb")
    app.add_typer(mcp.app, name="mcp")
    app.add_typer(reverse_cmd.app, name="reverse")
    app.add_typer(todos.app, name="todos")
    app.add_typer(cron.app, name="cron")
    app.add_typer(serve.app, name="serve")
    app.add_typer(proxy.app, name="proxy")
    app.add_typer(team.app, name="team")
    app.command("chat")(chat.chat)
    app.command("doctor")(doctor.doctor)
    app.command("report")(report.report)
    app.command("scan")(scan.scan)
    app.command("version")(version_cmd)
    app.command("update")(update.update)
    _commands_registered = True


def version_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Print the ArgusAI version."""
    from argusai import __version__
    from argusai.cli.ui import emit_result, get_console

    set_json_mode(json_out)
    if json_out:
        emit_result({"ok": True, "version": __version__})
        return
    get_console().print(f"[brand]ArgusAI[/] [muted]v{__version__}[/]")


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Prefer JSON output where supported.",
        is_eager=True,
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Mirror logs to stderr (default: quiet for CLI subcommands).",
        is_eager=True,
    ),
    gui: bool = typer.Option(
        False,
        "--gui",
        help="Force desktop GUI (overrides TTY auto-detect).",
        is_eager=True,
    ),
    tray: bool = typer.Option(
        False,
        "--tray",
        help="Start the desktop GUI hidden in the system tray.",
        is_eager=True,
    ),
    chat_mode: bool = typer.Option(
        False,
        "--chat",
        help="Force ops console / Agent REPL (same as interactive default).",
        is_eager=True,
    ),
) -> None:
    """ArgusAI CLI. Bare invoke picks GUI vs ops console from the environment."""
    set_json_mode(json_out)

    if ctx.invoked_subcommand is not None:
        if verbose:
            os.environ["ARGUSAI_LOG_CONSOLE"] = "1"
        else:
            os.environ.setdefault("ARGUSAI_LOG_CONSOLE", "0")
        return

    if tray:
        gui = True
    if gui and chat_mode:
        raise typer.BadParameter("use either --gui or --chat, not both")

    from argusai.core.host import resolve_launch_mode

    force = "gui" if gui else ("cli" if chat_mode else None)
    mode = resolve_launch_mode(force=force)

    if mode == "cli":
        if verbose:
            os.environ["ARGUSAI_LOG_CONSOLE"] = "1"
        else:
            os.environ.setdefault("ARGUSAI_LOG_CONSOLE", "0")
        # Bare ``argusai`` opens the same REPL as ``argusai chat`` (no args).
        # Pass concrete defaults — Typer Option objects are not callable defaults.
        from argusai.cli.commands.chat import chat as chat_cmd

        chat_cmd(
            message=None,
            session_id="cli-default",
            yes=False,
            once=False,
            timeout=600.0,
            json_out=False,
            files=None,
        )
        return

    from argusai.__main__ import _bootstrap_logging
    from argusai.gui import main as gui_main

    # Desktop GUI writes to the rotating log file. Mirroring the same records
    # to a console-subsystem window is what makes the black CMD flash stay
    # visible after Explorer / NSIS shortcut launches.
    os.environ.setdefault("ARGUSAI_LOG_CONSOLE", "0")
    _bootstrap_logging()
    gui_main()


def main(argv: Optional[list[str]] = None) -> None:
    """CLI main used by console_scripts and ``python -m argusai``."""
    from argusai.cli.locale import bootstrap_locale, configure_cli_stdio
    from argusai.cli.typer_i18n import build_localized_click_app

    configure_cli_stdio()
    bootstrap_locale()
    _register_commands()
    from argusai import __version__

    # Materialize + localize once. Invoking ``app()`` would rebuild Click from
    # Typer models and discard the rewritten help strings.
    click_app = build_localized_click_app(app)

    args = list(sys.argv[1:] if argv is None else argv)
    if sys.stdout.isatty() and any(a in {"-h", "--help"} for a in args):
        print_banner(__version__)
    click_app.main(args=args, prog_name="argusai", standalone_mode=True)


if __name__ == "__main__":
    main()
