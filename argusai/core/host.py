"""Minimal host protocol shared by GUI / headless / CLI entry points.

Hosts are thin adapters around the same core runtime. This module only
defines the notification surface that must not assume a pywebview window.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Literal, Protocol, runtime_checkable

HostKind = Literal["gui", "headless", "cli"]
LaunchMode = Literal["gui", "cli"]

log = logging.getLogger("argusai.host")

_VALID_KINDS: frozenset[str] = frozenset({"gui", "headless", "cli"})


def normalize_host_kind(value: str | None, *, default: HostKind = "gui") -> HostKind:
    raw = str(value or "").strip().lower()
    if raw in _VALID_KINDS:
        return raw  # type: ignore[return-value]
    return default


def is_interactive_cli() -> bool:
    """True when stdin+stdout look like an interactive terminal session."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            if not ctypes.windll.kernel32.GetConsoleWindow():
                return False
        except Exception:
            pass
        # The packaged build is console-subsystem, so an Explorer double-click
        # gets a console of its own and would otherwise look like a terminal
        # session. A console nobody else shares means desktop launch → GUI.
        if is_packaged_binary() and _console_is_self_allocated() is True:
            return False
    return True


def _console_is_self_allocated() -> bool | None:
    try:
        from argusai.core.infra.win_stdio import console_is_self_allocated

        return console_is_self_allocated()
    except Exception:
        return None


def is_packaged_binary() -> bool:
    """True for Nuitka/pyinstaller-style packaged launches (not ``uv``/pip scripts).

    Do **not** key off the executable basename: Windows console scripts are also
    named ``argusai.exe`` and must keep the TTY → ops-console path.
    """
    try:
        from argusai.core.infra.runtime_exec import is_frozen_runtime

        return bool(is_frozen_runtime())
    except Exception:
        return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def desktop_gui_available() -> bool:
    """Best-effort: can a desktop window realistically be shown?"""
    # SSH without X11 / Wayland — classic headless shell.
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        if not display:
            return False
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
    return True


def resolve_launch_mode(
    *,
    force: str | None = None,
    environ: dict[str, str] | None = None,
    interactive: bool | None = None,
    gui_available: bool | None = None,
    packaged: bool | None = None,
) -> LaunchMode:
    """Decide bare ``argusai`` launch: ops console vs desktop GUI.

    Precedence:
    1. Explicit ``force`` (``--gui`` / ``--chat``)
    2. ``ARGUSAI_HOST`` / ``ARGUSAI_LAUNCH`` env (``gui`` | ``cli`` | ``chat``)
    3. Interactive TTY / shared console → CLI ops console
       (covers ``uv run argusai``, pip ``argusai.exe``, and packaged exe from a shell)
    4. Packaged binary on a console of its own → GUI (Explorer double-click / Start Menu)
    5. Non-interactive / no TTY → GUI when a desktop is available
    6. No desktop surface → CLI
    """
    explicit = str(force or "").strip().lower()
    if explicit in {"gui", "desktop", "webview"}:
        return "gui"
    if explicit in {"cli", "chat", "repl", "ops"}:
        return "cli"

    env = environ if environ is not None else os.environ
    raw = str(env.get("ARGUSAI_HOST") or env.get("ARGUSAI_LAUNCH") or "").strip().lower()
    if raw in {"gui", "desktop", "webview"}:
        return "gui"
    if raw in {"cli", "chat", "repl", "ops", "headless"}:
        return "cli"

    # ``packaged`` is accepted for API compatibility but must not override TTY:
    # a packaged exe launched from PowerShell should open the ops console.
    _ = packaged
    has_gui = desktop_gui_available() if gui_available is None else bool(gui_available)
    tty = is_interactive_cli() if interactive is None else bool(interactive)
    # Interactive console → CLI. No console (Explorer / Start Menu) → GUI.
    if tty or not has_gui:
        return "cli"
    return "gui"


@runtime_checkable
class HostCallbacks(Protocol):
    """Session/attention callbacks every ArgusAI host should expose."""

    @property
    def host_kind(self) -> HostKind: ...

    def notify_session_updated(
        self,
        session_id: str,
        *,
        reason: str,
        source_ref: str = "",
    ) -> None: ...

    def notify_attention(
        self,
        event_key: str,
        *,
        title: str,
        message: str,
        level: str = "info",
        cooldown_seconds: float = 30.0,
        require_background: bool = True,
        max_notifications: int = 0,
    ) -> bool: ...


def host_supports_webview(kind: HostKind | str | None) -> bool:
    return normalize_host_kind(kind) == "gui"


def log_session_updated(
    kind: HostKind | str | None,
    session_id: str,
    *,
    reason: str,
    source_ref: str = "",
) -> None:
    log.debug(
        "session_updated host=%s session_id=%s reason=%s source_ref=%s",
        normalize_host_kind(kind),
        session_id,
        reason,
        source_ref or "-",
    )


def noop_attention(**_kwargs: Any) -> bool:
    """Headless/CLI default when desktop notifications are not wanted."""
    return False
