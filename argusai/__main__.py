"""Executable entry point for ``python -m argusai`` and frozen builds."""

from __future__ import annotations


def _bootstrap_logging() -> None:
    """Configure file-based logging as early as possible.

    We do this before importing the GUI / settings so that any record
    emitted during those imports lands in the rotating log file. Errors
    inside the bootstrap are swallowed: a broken logger must never stop
    the app from starting.
    """

    try:
        from argusai.core.infra.logging_setup import LoggingSettings, init_logging

        try:
            from argusai.core.settings import SettingsManager

            settings = SettingsManager().get()
            init_logging(settings.logging, data_root=settings.data_root)
        except Exception:
            init_logging(LoggingSettings())
    except Exception:
        pass


def main() -> None:
    import sys

    if "--mcp-tool-proxy" in sys.argv[1:]:
        from argusai.core.agent_runtimes.mcp_proxy import main as proxy_main

        proxy_main()
        return
    if "--channel-stream-sidecar" in sys.argv[1:]:
        from argusai.core.channels.stream_sidecar import main as channel_sidecar_main

        channel_sidecar_main()
        return
    if "--self-check-browser" in sys.argv[1:]:
        import json

        from argusai.core.browser.browser_selfcheck import self_check_browser

        report = self_check_browser()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0 if report.get("ok") else 1)

    # Console-subsystem Windows build: Explorer / Start Menu / Run-key launches
    # get a console of their own (a Windows Terminal tab on Windows 11). Get rid
    # of it before the slow GUI import chain. Inherited terminals
    # (``argusai --gui`` from PowerShell) are left alone.
    # Packaged builds use Nuitka ``--windows-console-mode=hide`` so terminal
    # launches retain a real console subsystem (the shell waits and the ops
    # TUI owns input), while desktop-only consoles are dropped.
    if _hide_console_for_desktop_launch():
        raise SystemExit(0)

    # Remaining argv → Typer CLI (interactive console→ops; else GUI).
    from argusai.cli.main import main as cli_main

    cli_main()


_DESKTOP_LAUNCH_FLAGS = frozenset({"--gui", "--tray"})


def _hide_console_for_desktop_launch() -> bool:
    """Get rid of a desktop launch's console. True → a windowless copy took over."""
    import sys

    try:
        from argusai.core.host import resolve_launch_mode
        from argusai.core.infra.win_stdio import (
            hide_self_allocated_console,
            relaunch_without_console,
        )

        argv = sys.argv[1:]
        force = None
        if "--gui" in argv or "--tray" in argv:
            force = "gui"
        elif "--chat" in argv:
            force = "cli"
        if resolve_launch_mode(force=force) != "gui":
            return False
        # Subcommands keep the old hide-in-place path; only bare / --gui /
        # --tray launches (Explorer, Start Menu, Run key) are re-executed.
        if set(argv) <= _DESKTOP_LAUNCH_FLAGS:
            relaunch_args = argv if "--gui" in argv else ["--gui", *argv]
            if relaunch_without_console(relaunch_args):
                return True
        hide_self_allocated_console()
    except Exception:
        pass
    return False


if __name__ == "__main__":
    main()
