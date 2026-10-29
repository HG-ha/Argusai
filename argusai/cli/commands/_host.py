"""Shared headless/CLI Api bootstrap for management commands."""

from __future__ import annotations

from typing import Any


def headless_api() -> Any:
    from argusai.__main__ import _bootstrap_logging
    from argusai.gui import Api

    _bootstrap_logging()
    return Api.for_headless()


def parse_kv_pairs(items: list[str] | None, *, flag: str = "--secret") -> dict[str, str]:
    """Parse repeated ``KEY=VALUE`` CLI options."""
    out: dict[str, str] = {}
    for raw in items or []:
        text = str(raw or "").strip()
        if not text or "=" not in text:
            raise ValueError(f"{flag} expects KEY=VALUE, got: {raw!r}")
        key, _, value = text.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"{flag} key is empty: {raw!r}")
        out[key] = value
    return out


def fail(msg: str, *, json_out: bool = False, code: int = 1) -> None:
    import typer

    from argusai.cli.ui import emit_result, get_console

    if json_out:
        emit_result({"ok": False, "error": msg})
    else:
        get_console().print(f"[fail]{msg}[/]")
    raise typer.Exit(code)


def ok_payload(payload: dict[str, Any], *, json_out: bool, human: str | None = None) -> None:
    from argusai.cli.ui import emit_result, get_console

    if json_out:
        emit_result({"ok": True, **payload})
        return
    if human:
        get_console().print(human)
    else:
        get_console().print(payload)
