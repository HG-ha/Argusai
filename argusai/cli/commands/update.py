"""Explicit update checks; importing or running other commands stays offline."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn
from rich.text import Text

from argusai.cli.locale import label
from argusai.cli.ui import emit_result, get_console, json_mode, set_json_mode


def _manager():
    from argusai import __version__
    from argusai.core.paths import default_app_root
    from argusai.core.updater import UpdateManager

    return UpdateManager(default_app_root() / "cache" / "updates", __version__)


def _payload(manager, state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    result["ok"] = state.get("status") not in {"error", "unsupported"} and not state.get("error")
    if state.get("install_mode") == "source":
        result["guidance"] = label("update_source")
    elif state.get("status") == "downloaded":
        from argusai.core.updater import SUFFIXES

        suffix = SUFFIXES.get(state.get("platform", ""))
        if suffix and state.get("version"):
            result["package_path"] = str(Path(manager.cache_dir) / f"ArgusAI-{state['version']}-{suffix}")
        result["guidance"] = label("update_install_hint")
    return result


def _print_result(result: dict[str, Any]) -> None:
    if json_mode():
        emit_result(result)
        return
    console = get_console()
    console.print(Text(label("update_versions", current=result.get("current_version") or "—",
                             latest=result.get("version") or "—")))
    status_key = {
        "available": "update_available", "up_to_date": "update_current",
        "downloaded": "update_downloaded", "unsupported": "update_unsupported",
        "error": "update_failed", "cancelled": "update_cancelled",
    }.get(result.get("status"), "update_failed")
    console.print(Text(label(status_key)))
    for key in ("notes", "release_url", "package_path", "guidance", "error"):
        if result.get(key):
            console.print(Text(str(result[key])))


def update(
    check: bool = typer.Option(False, "--check", help="Check for updates without downloading (default)."),
    download: bool = typer.Option(False, "--download", help="Download and verify the matching release package."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Check for updates only when requested; optionally download a verified package."""
    set_json_mode(bool(json_out or json_mode()))
    if check and download:
        raise typer.BadParameter(label("update_conflict"))
    manager = None
    try:
        manager = _manager()
        state = manager.check(force=True)
        if download and state.get("status") in {"available", "downloaded"}:
            if state.get("install_mode") == "source":
                state = {**state, "error": label("update_source"), "error_code": "source_installation"}
            elif state.get("status") != "downloaded":
                state = manager.start_download()
                console = get_console()
                with Progress(TextColumn(label("update_downloading")), BarColumn(), DownloadColumn(),
                              console=console, disable=json_mode() or not console.is_terminal) as progress:
                    task = progress.add_task("download", total=state.get("total_bytes") or 0)
                    while state.get("status") == "downloading":
                        progress.update(task, completed=state.get("downloaded_bytes") or 0)
                        time.sleep(0.1)
                        state = manager.status()
                    progress.update(task, completed=state.get("downloaded_bytes") or 0)
        result = _payload(manager, state)
    except KeyboardInterrupt:
        if manager is not None:
            manager.cancel_download()
        # Read timeout is 30 seconds; allow bounded partial-file cleanup.
        worker = getattr(manager, "_worker", None)
        if worker is not None:
            worker.join(timeout=35)
        state = manager.status() if manager is not None else {}
        result = _payload(manager, {**state, "status": "cancelled", "error": label("update_cancelled")})
        result["ok"] = False
        _print_result(result)
        raise typer.Exit(130)
    except Exception as exc:
        result = {"ok": False, "status": "error", "error": str(exc)}
    _print_result(result)
    if not result.get("ok"):
        raise typer.Exit(1)
