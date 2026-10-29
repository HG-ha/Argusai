"""Fullscreen ops console — btop-style framed panes with pinned input.

Layout: Header | (Left · Center · Right) | Input dock | Status
Tab cycles the *logical* pane (styles + nav bindings). Real keyboard focus
stays on the input dock so Windows never echoes typed chars into MAIN.
MAIN: ↑↓ line scroll, PgUp/PgDn page scroll, mouse wheel stays in-pane.
Right AUTH: Enter allow.
"""

from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import Future
from io import StringIO
from typing import Any, Callable, Literal

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.filters import Condition
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.formatted_text import (
    ANSI,
    FormattedText,
    StyleAndTextTuples,
    split_lines,
    to_formatted_text,
)
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from argusai.cli.locale import label
from argusai.cli.metrics import (
    format_metrics_lines,
    format_token_usage_lines,
    sample_host_metrics,
)
from argusai.core.token_usage_display import context_usage_parts, fmt_tokens
from argusai.cli.prompt import (
    _history_file,
    build_cli_input_completer,
    cancel_input_completion,
    input_completion_at_cursor,
)
from argusai.cli.render import make_markdown
from argusai.cli.ui import (
    ARGUS_THEME,
    OPS_BORDER,
    OPS_HUD,
    OPS_MUTED,
    OPS_SECONDARY,
    OPS_TEXT,
)

FocusPane = Literal["input", "left", "main", "right"]
MonitorMode = Literal["off", "sys", "agents", "tokens", "both"]
SidebarMode = Literal["auto", "on", "off"]

# Outer frame widths (borders included). Content = width - 2.
_LEFT_FRAME = 32
_RIGHT_FRAME = 36
# Floor for live MAIN redraws. Slow frames raise this (Aider-style backoff).
_PAINT_INTERVAL_S = 0.06
_WHEEL_STEP = 3
_IDLE_REFRESH_S = 0.5
_ACTIVITY_REFRESH_S = 0.08
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_ACTIVITY_ALIASES: tuple[tuple[str, str], ...] = (
    ("正在请求模型", "ops_activity_model"),
    ("requesting model", "ops_activity_model"),
    ("waiting for model", "ops_activity_model"),
    ("waiting on model", "ops_activity_model"),
    ("正在思考", "ops_activity_think"),
    ("thinking", "ops_activity_think"),
    ("正在生成回复", "ops_activity_reply"),
    ("composing reply", "ops_activity_reply"),
    ("linking context", "ops_activity_context"),
    ("整理上下文", "ops_activity_context"),
    ("已恢复", "ops_activity_resume"),
    ("resumed", "ops_activity_resume"),
)


class _OpsOutput:
    """VT mouse without the 1003 motion flood; keep wheel inside the TUI.

    prompt_toolkit's default mouse mode also sends 1003h (any movement). That
    floods stdin on Windows Terminal. We only want button events + SGR +
    alternate-scroll so the emulator cannot pan past the alt screen.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def enable_mouse_support(self) -> None:
        write = getattr(self._inner, "write_raw", None)
        if callable(write):
            write("\x1b[?1000h\x1b[?1006h\x1b[?1007h")
            self._inner.flush()
        else:
            self._inner.enable_mouse_support()
        win32 = getattr(self._inner, "win32_output", None)
        if win32 is not None:
            try:
                win32.enable_mouse_support()
            except Exception:
                pass

    def disable_mouse_support(self) -> None:
        write = getattr(self._inner, "write_raw", None)
        if callable(write):
            write("\x1b[?1000l\x1b[?1006l\x1b[?1007l")
            self._inner.flush()
        else:
            self._inner.disable_mouse_support()
        win32 = getattr(self._inner, "win32_output", None)
        if win32 is not None:
            try:
                win32.disable_mouse_support()
            except Exception:
                pass

    def enter_alternate_screen(self) -> None:
        self._inner.enter_alternate_screen()
        write = getattr(self._inner, "write_raw", None)
        if callable(write):
            # Wipe alt-buffer history so wheel/scrollbar cannot leave the TUI.
            write("\x1b[3J\x1b[H")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _format_elapsed(seconds: float) -> str:
    if seconds < 9.95:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _activity_verb(status: str, *, busy: bool) -> str:
    raw = (status or "").strip().rstrip(".…").strip()
    lowered = raw.lower()
    if lowered.startswith("tool"):
        name = raw.split("·", 1)[-1].strip() if "·" in raw else ""
        if not name and " " in raw:
            name = raw.split(None, 1)[-1].strip()
        if name and name.lower() not in {"tool"}:
            return label("ops_activity_tool", name=name[:28])
        return label("ops_activity_tool_generic")
    if lowered.startswith("round "):
        return label("ops_activity_round", n=raw.split(None, 1)[-1].strip() or "?")
    for needle, key in _ACTIVITY_ALIASES:
        if needle in raw or needle in lowered:
            return label(key)
    if raw:
        return raw[:36]
    if busy:
        return label("ops_activity_working")
    return ""


def ops_activity_parts(
    status: str,
    *,
    busy: bool,
    started_at: float,
    now: float,
) -> tuple[str, str, str] | None:
    verb = _activity_verb(status, busy=busy)
    if not verb:
        return None
    frame = _SPINNER_FRAMES[int(max(0.0, now) * 10) % len(_SPINNER_FRAMES)]
    elapsed = _format_elapsed(max(0.0, now - started_at) if started_at else 0.0)
    return frame, verb, elapsed


def _mouse_scroll_delta(data: str) -> int:
    """Parse a PT mouse payload into MAIN scroll delta (positive = older)."""
    raw = data or ""
    if "SCROLL_UP" in raw:
        return _WHEEL_STEP
    if "SCROLL_DOWN" in raw:
        return -_WHEEL_STEP
    if "<" in raw:
        try:
            code = int(raw.split("<", 1)[1].split(";", 1)[0])
        except (IndexError, ValueError):
            return 0
        base = code & ~0b11100
        if base == 64:
            return _WHEEL_STEP
        if base == 65:
            return -_WHEEL_STEP
        return 0
    if len(raw) >= 4 and raw[0] == "\x1b" and raw[1] == "[" and raw[2] == "M":
        kind = ord(raw[3]) - 32
        if kind == 64:
            return _WHEEL_STEP
        if kind == 65:
            return -_WHEEL_STEP
    return 0


def _term_width() -> int:
    try:
        return max(40, shutil.get_terminal_size(fallback=(100, 30)).columns)
    except Exception:
        return 100


def _term_cols() -> int:
    return _term_width()


def _term_rows() -> int:
    try:
        return max(12, shutil.get_terminal_size(fallback=(100, 30)).lines)
    except Exception:
        return 30


def rich_to_ansi(renderable: Any, *, width: int | None = None) -> str:
    """Render a Rich object to an ANSI string."""
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        width=width or max(40, _term_width() - 4),
        theme=ARGUS_THEME,
        highlight=False,
        color_system="truecolor",
    )
    try:
        console.print(renderable)
    except Exception:
        console.print(str(renderable))
    return buf.getvalue().rstrip("\n")


def _with_ansi_gutter(text: str, color: str) -> str:
    """Prefix every visual line with a true-color role rail."""
    raw = (text or "").rstrip("\n")
    if not raw:
        return ""
    rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
    rail = f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m│\x1b[0m "
    return "\n".join(f"{rail}{line}" for line in raw.split("\n"))


def _todo_mark(status: str) -> str:
    st = (status or "pending").lower()
    if st == "completed":
        return "[x]"
    if st == "in_progress":
        return "[~]"
    if st in {"blocked", "cancelled"}:
        return "[!]"
    return "[ ]"


def _flatten_todos(raw: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        kids = item.get("todos") or item.get("children")
        if isinstance(kids, list) and kids:
            out.extend(_flatten_todos(kids))
        else:
            out.append(item)
    return out


def _section(title: str, *, focus: bool = False) -> str:
    mark = "›" if focus else "·"
    return f"{mark} {title}"


def _push_formatted_line(
    target: StyleAndTextTuples,
    fragments: StyleAndTextTuples,
) -> None:
    if target:
        target.append(("", "\n"))
    target.extend(fragments)


def _monitor_row(
    row: str,
    *,
    accent: str,
    subtle: bool = False,
) -> StyleAndTextTuples:
    """Color one compact MONITOR row without losing meter alignment."""
    if not row:
        return [("", "")]
    if subtle:
        return [("class:monitor-subtle", row)]

    leading_len = len(row) - len(row.lstrip())
    leading = row[:leading_len]
    content = row[leading_len:]
    label, separator, rest = content.partition(" ")
    fragments: StyleAndTextTuples = []
    if leading:
        fragments.append(("class:monitor-value", leading))
    fragments.append((f"{accent} bold", label))
    if not separator:
        return fragments

    tail = separator + rest
    meter_chars = frozenset("█░▁▂▃▄▅▆▇")
    start = 0
    in_meter = tail[0] in meter_chars
    for idx, char in enumerate(tail[1:], start=1):
        char_is_meter = char in meter_chars
        if char_is_meter == in_meter:
            continue
        style = accent if in_meter else "class:monitor-value"
        fragments.append((style, tail[start:idx]))
        start = idx
        in_meter = char_is_meter
    style = accent if in_meter else "class:monitor-value"
    fragments.append((style, tail[start:]))
    return fragments


class _TranscriptControl(UIControl):
    """Center pane control that sizes the sticky viewport from real layout dims."""

    def __init__(self, owner: "OpsApp") -> None:
        self._owner = owner

    def create_content(self, width: int, height: int) -> UIContent:
        owner = self._owner
        width = max(1, int(width))
        height = max(1, int(height))
        started = time.monotonic()
        with owner._lock:
            owner._viewport_width = width
            owner._viewport_height = height
            live = bool(owner._live_md)
        lines = owner._visible_transcript_lines(width, height)
        if live:
            owner._note_paint_cost(time.monotonic() - started)

        # Pad a copy so short transcripts don't jump, without mutating caches.
        if len(lines) < height:
            lines = [[("", "")]] * (height - len(lines)) + list(lines)

        def get_line(i: int) -> StyleAndTextTuples:
            if 0 <= i < len(lines):
                return list(lines[i])
            return [("", "")]

        return UIContent(
            get_line=get_line,
            line_count=height,
            show_cursor=False,
        )

    def is_focusable(self) -> bool:
        # Never take real prompt_toolkit focus. Tab only flips the logical
        # pane; keybindings read ``OpsApp._focus``. Focusing this window on
        # Windows lets console echo land in the transcript (cursor ghost).
        return False

    def mouse_handler(self, mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self._owner._scroll_transcript(_WHEEL_STEP)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self._owner._scroll_transcript(-_WHEEL_STEP)
            return None
        return NotImplemented


class OpsApp:
    """Pinned-input ops console with btop-like framed regions."""

    def __init__(
        self,
        *,
        on_line: Callable[[str], None],
        toolbar_provider: Callable[[], str] | None = None,
        session_provider: Callable[[], dict[str, Any]] | None = None,
        jobs_provider: Callable[[], list[dict[str, Any]]] | None = None,
        metrics_provider: Callable[[], dict[str, Any]] | None = None,
        token_usage_provider: Callable[[], dict[str, Any]] | None = None,
        on_allow: Callable[[str], None] | None = None,
        on_deny: Callable[[str], None] | None = None,
        header_provider: Callable[[], str] | None = None,
        title: str = "ARGUSAI OPS",
    ) -> None:
        self._on_line = on_line
        self._toolbar_provider = toolbar_provider
        self._session_provider = session_provider
        self._jobs_provider = jobs_provider
        self._metrics_provider = metrics_provider or sample_host_metrics
        self._token_usage_provider = token_usage_provider
        self._on_allow = on_allow
        self._on_deny = on_deny
        self._header_provider = header_provider
        self._title = title

        self._log: list[str] = []
        self._live_md = ""
        self._status = ""
        self._status_started_at = 0.0
        self._pending_echo: str | None = None
        self._prompt = "ops › "
        self._busy = False
        self._ask_mode = False
        self._ask_event = threading.Event()
        self._ask_value = ""
        self._choice_options: list[str] = []
        self._choice_index = 0
        self._scroll = 0  # visual lines above the sticky bottom
        self._app: Application[None] | None = None
        self._input: TextArea | None = None
        self._input_completer = build_cli_input_completer()
        self._lock = threading.RLock()
        self._center_line_count = 0
        self._viewport_width = 0
        self._viewport_height = 0

        self._focus: FocusPane = "input"
        self._monitor: MonitorMode = "off"
        self._sidebar: SidebarMode = "off"
        self._job_index = 0
        self._jobs_cache: list[dict[str, Any]] = []
        self._session_cache: dict[str, Any] = {}
        self._metrics_cache: dict[str, Any] = {}
        self._token_usage_cache: dict[str, Any] = {}
        self._header_text = ""
        self._rail_lines: list[str] | None = None
        self._last_chrome_ts = 0.0
        self._last_token_usage_ts = 0.0
        self._auth_announced: set[str] = set()
        self._last_paint_ts = 0.0
        self._paint_timer: threading.Timer | None = None
        self._log_lines_cache: list[StyleAndTextTuples] | None = None
        self._log_lines_width = 0
        self._log_gen = 0
        self._live_ansi_cache: tuple[str, int, str] | None = None
        self._live_header_cache: tuple[int, str] | None = None
        self._live_stable_src = ""
        self._live_stable_width = 0
        self._live_stable_lines: list[StyleAndTextTuples] | None = None
        self._paint_interval = _PAINT_INTERVAL_S

        self._frame_left: Frame | None = None
        self._frame_center: Frame | None = None
        self._frame_right: Frame | None = None
        self._frame_input: Frame | None = None
        self._body_left: Window | None = None
        self._body_center: Window | None = None
        self._body_right: Window | None = None

    # ── surface API ────────────────────────────────────────────────

    @property
    def pending_reply(self) -> bool:
        with self._lock:
            return bool(self._live_md)

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._busy)

    @property
    def monitor_mode(self) -> MonitorMode:
        with self._lock:
            return self._monitor

    @property
    def sidebar_mode(self) -> SidebarMode:
        with self._lock:
            return self._sidebar

    def set_on_line(self, fn: Callable[[str], None]) -> None:
        self._on_line = fn

    def take_pending_echo(self, text: str) -> bool:
        """True when `_accept` already painted this user line."""
        wanted = (text or "").strip()
        with self._lock:
            pending = self._pending_echo
            if pending is None or pending != wanted:
                return False
            self._pending_echo = None
            return True

    def _schedule_on_line(self, line: str) -> None:
        """Run the submit handler after this key event so MAIN can paint the echo."""

        def _dispatch() -> None:
            try:
                self._on_line(line)
            except Exception as exc:
                self.append_markup(f"[fail]{exc}[/]")
            finally:
                with self._lock:
                    if self._pending_echo == line:
                        self._pending_echo = None

        app = self._app
        loop = getattr(app, "loop", None) if app is not None else None
        if loop is not None:
            try:
                loop.call_soon(_dispatch)
                return
            except Exception:
                pass
        _dispatch()

    def set_monitor_mode(self, mode: str) -> str:
        wanted = str(mode or "off").strip().lower()
        if wanted in {"on", "both"}:
            wanted = "both"
        if wanted not in {"off", "sys", "agents", "tokens", "both"}:
            wanted = "off"
        with self._lock:
            self._monitor = wanted  # type: ignore[assignment]
            if wanted == "off" and self._focus == "right":
                self._focus = "input"
        self._sync_frame_styles()
        self.refresh_chrome(force=True)
        self.invalidate(immediate=True)
        return wanted

    def set_sidebar_mode(self, mode: str) -> str:
        wanted = str(mode or "auto").strip().lower()
        if wanted not in {"auto", "on", "off"}:
            wanted = "auto"
        with self._lock:
            self._sidebar = wanted  # type: ignore[assignment]
            if (
                (self._focus == "left" and not self._layout_show_left())
                or (self._focus == "right" and not self._layout_show_right())
            ):
                self._focus = "input"
        self._apply_layout_focus()
        self._sync_frame_styles()
        self.refresh_chrome(force=True)
        self.invalidate(immediate=True)
        return wanted

    def set_busy(self, busy: bool) -> None:
        with self._lock:
            self._busy = bool(busy)
            if not busy:
                self._status = ""
                self._status_started_at = 0.0
            elif not self._status_started_at:
                self._status_started_at = time.monotonic()
        self.refresh_chrome(force=True)
        self._sync_activity_refresh()
        self.invalidate(immediate=True)

    def set_status(self, text: str) -> None:
        with self._lock:
            clipped = (text or "")[:80]
            prev_verb = _activity_verb(self._status, busy=True)
            new_verb = _activity_verb(clipped, busy=True)
            if clipped == self._status:
                return
            self._status = clipped
            if new_verb != prev_verb or not self._status_started_at:
                self._status_started_at = time.monotonic()
            streaming = bool(self._live_md) or self._busy
        self._sync_activity_refresh()
        self.invalidate(immediate=not streaming)

    def set_header(self, text: str) -> None:
        with self._lock:
            wanted = text or ""
            if wanted == self._header_text:
                return
            self._header_text = wanted
        self.invalidate(immediate=True)

    def feed_reply(self, piece: str) -> None:
        if not piece:
            return
        with self._lock:
            self._live_md += piece
            prev_verb = _activity_verb(self._status, busy=True)
            self._status = label("reply_streaming")
            if _activity_verb(self._status, busy=True) != prev_verb or not self._status_started_at:
                self._status_started_at = time.monotonic()
            self._live_ansi_cache = None
        self._sync_activity_refresh()
        self.invalidate()

    def append_user_message(
        self,
        text: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        body = (text or "").rstrip()
        names = [
            str(item.get("name") or "").strip()
            for item in list(attachments or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if names:
            summary = label("cli_attachment_summary", names=", ".join(names))
            body = f"{body}\n{summary}".strip()
        if not body:
            return
        width = self._center_width()
        header = rich_to_ansi(
            Text.from_markup(f"[prompt]╭─ {label('tag_user')}[/]"),
            width=width,
        )
        rendered = rich_to_ansi(
            Text(body, style=OPS_TEXT),
            width=max(20, width - 2),
        )
        self.append_ansi(header + "\n" + _with_ansi_gutter(rendered, "#e6a23c"))

    def flush_reply(self) -> None:
        with self._lock:
            body = self._live_md.rstrip()
            self._live_md = ""
            self._status = ""
            self._status_started_at = 0.0
            self._live_ansi_cache = None
            self._live_stable_lines = None
            self._live_stable_src = ""
        if body:
            width = self._center_width()
            ansi = rich_to_ansi(
                Text.from_markup("[reply]╭─ ARGUSAI[/]"),
                width=width,
            )
            md = rich_to_ansi(
                make_markdown(body, fancy=True),
                width=max(20, width - 2),
            )
            self.append_ansi(ansi + "\n" + _with_ansi_gutter(md, "#7eb8c9"))
        else:
            self.invalidate(immediate=True)
        self._sync_activity_refresh()

    def discard_reply(self) -> None:
        with self._lock:
            self._live_md = ""
            self._live_ansi_cache = None
            self._live_stable_lines = None
            self._live_stable_src = ""
        self.invalidate(immediate=True)

    def tool_exec(self, name: str, detail: str = "") -> None:
        self.flush_reply()
        width = self._center_width()
        key = (name or "tool").strip() or "tool"
        line = f"[tool]├─ {label('tag_exec')}[/]  [accent]{key}[/]"
        ansi = rich_to_ansi(Text.from_markup(line), width=width)
        note = (detail or "").strip()
        if note:
            detail_ansi = rich_to_ansi(
                Text(note[:160], style=OPS_MUTED),
                width=max(20, width - 2),
            )
            ansi += "\n" + _with_ansi_gutter(detail_ansi, "#d4a574")
        self.append_ansi(ansi)
        self.set_status(f"tool · {key}")

    def append_markup(self, markup: str) -> None:
        self.append_ansi(
            rich_to_ansi(Text.from_markup(markup), width=self._center_width())
        )

    def append_ansi(self, ansi: str) -> None:
        text = (ansi or "").rstrip("\n")
        if not text:
            return
        with self._lock:
            self._log.append(text)
            self._log_gen += 1
            if len(self._log) > 400:
                self._log = self._log[-300:]
                self._log_lines_cache = None
            else:
                self._extend_log_line_cache(text)
            self._scroll = 0
        self.invalidate(immediate=True)

    def append_panel(self, lines: list[str], *, title: str | None = None) -> None:
        """Command card sized to the center content width (avoids torn borders)."""
        width = self._center_width()
        joined = "\n".join(str(line) for line in lines)
        try:
            body: Any = Text.from_markup(joined)
        except Exception:
            body = Text(joined)
        tag = title or f"[amber]{label('tag_cmd')}[/]"
        panel = Panel(
            body,
            title=tag,
            title_align="left",
            border_style=OPS_BORDER,
            box=box.ROUNDED,
            padding=(0, 1),
            width=max(20, width),
        )
        self.append_ansi(rich_to_ansi(panel, width=width))

    def clear_screen(self) -> None:
        with self._lock:
            self._log.clear()
            self._live_md = ""
            self._status = ""
            self._status_started_at = 0.0
            self._auth_announced.clear()
            self._log_lines_cache = None
            self._live_ansi_cache = None
            self._live_stable_lines = None
            self._live_stable_src = ""
            self._log_gen += 1
        self.refresh_chrome(force=True)
        self.invalidate(immediate=True)

    def set_rail_lines(self, lines: list[str] | None) -> None:
        """Replace the left rail. ``None`` restores the session todo list."""
        with self._lock:
            self._rail_lines = [str(line) for line in lines] if lines is not None else None
        self.invalidate(immediate=True)

    def append_speaker(self, name: str, body: str) -> None:
        """One member reply, in the same shape as a main-agent answer."""
        text = (body or "").rstrip()
        who = (name or "member").strip() or "member"
        if not text:
            return
        width = self._center_width()
        header = rich_to_ansi(
            Text.from_markup(f"[reply]╭─ {escape(who)}[/]"),
            width=width,
        )
        try:
            body_renderable: Any = make_markdown(text, fancy=True)
        except Exception:
            body_renderable = Text(text, style=OPS_TEXT)
        rendered = rich_to_ansi(
            body_renderable,
            width=max(20, width - 2),
        )
        self.append_ansi(header + "\n" + _with_ansi_gutter(rendered, "#7eb8c9"))

    def set_dock_prompt(self, prompt: str) -> None:
        """Prompt shown in the input dock while not answering a card."""
        with self._lock:
            if not self._ask_mode:
                self._prompt = str(prompt or "ops › ")
        self.invalidate(immediate=True)

    def ask_line(self, prompt: str) -> str:
        return self._wait_for_answer(prompt, choices=[])

    def ask_choice(self, options: list[str], prompt: str) -> str:
        choices = [str(option).strip() for option in options if str(option).strip()]
        if not choices:
            return self.ask_line(prompt)
        return self._wait_for_answer(prompt, choices=choices)

    def _wait_for_answer(self, prompt: str, *, choices: list[str]) -> str:
        with self._lock:
            self._ask_mode = True
            self._focus = "input"
            self._prompt = prompt
            self._ask_value = ""
            self._choice_options = list(choices)
            self._choice_index = 0
            self._ask_event.clear()
        self._sync_frame_styles()
        self.invalidate(immediate=True)
        self._ask_event.wait()
        with self._lock:
            self._ask_mode = False
            self._prompt = "ops › "
            value = self._ask_value
            self._choice_options = []
            self._choice_index = 0
            self._focus = "input"
        self._sync_frame_styles()
        self.invalidate(immediate=True)
        return value

    def ask_confirm(self, prompt: str, *, default: bool = True) -> bool:
        """Read y/n from the input dock. Must run off the UI thread."""
        hint = " [Y/n] " if default else " [y/N] "
        raw = self.ask_line(f"{prompt}{hint}").strip().lower()
        if not raw:
            return default
        return raw in {"y", "yes", "是", "好", "ok"}

    def refresh_chrome(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_chrome_ts < 1.0:
                return
            self._last_chrome_ts = now
            monitor = self._monitor
            refresh_tokens = force or now - self._last_token_usage_ts >= 3.0
            if refresh_tokens:
                self._last_token_usage_ts = now
        if self._header_provider is not None:
            try:
                text = str(self._header_provider() or "")
                with self._lock:
                    self._header_text = text
            except Exception:
                pass
        if self._session_provider is not None:
            try:
                data = self._session_provider() or {}
                with self._lock:
                    self._session_cache = dict(data) if isinstance(data, dict) else {}
            except Exception:
                pass
        if monitor in {"agents", "both"} and self._jobs_provider is not None:
            try:
                jobs = self._jobs_provider() or []
                with self._lock:
                    self._jobs_cache = [j for j in jobs if isinstance(j, dict)]
                    if self._jobs_cache:
                        self._job_index = min(self._job_index, len(self._jobs_cache) - 1)
                    else:
                        self._job_index = 0
            except Exception:
                pass
        if monitor in {"sys", "both"}:
            try:
                sample = self._metrics_provider() or {}
                with self._lock:
                    self._metrics_cache = dict(sample) if isinstance(sample, dict) else {}
            except Exception:
                pass
        if (
            refresh_tokens
            and monitor in {"tokens", "both"}
            and self._token_usage_provider is not None
        ):
            try:
                usage = self._token_usage_provider() or {}
                with self._lock:
                    self._token_usage_cache = (
                        dict(usage) if isinstance(usage, dict) else {}
                    )
            except Exception:
                pass
        self._maybe_announce_auth()
        self._sync_frame_styles()

    def _maybe_announce_auth(self) -> None:
        """Push AUTH notice into transcript when new pending tools appear."""
        with self._lock:
            session = dict(self._session_cache)
        pending = [
            p for p in list(session.get("pending_tools") or []) if isinstance(p, dict)
        ]
        if not pending:
            with self._lock:
                self._auth_announced.clear()
            return
        fresh: list[dict[str, Any]] = []
        with self._lock:
            for item in pending:
                key = str(item.get("id") or item.get("name") or id(item))
                if key not in self._auth_announced:
                    self._auth_announced.add(key)
                    fresh.append(item)
        for item in fresh[:3]:
            name = str(item.get("name") or item.get("title") or "tool")
            self.append_markup(
                f"[amber]▸ {label('tag_auth')}[/]  {name}  "
                f"[hud.dim]{label('ops_auth_hint')}[/]"
            )

    # ── lifecycle ──────────────────────────────────────────────────

    def invalidate(self, *, immediate: bool = False) -> None:
        """Redraw the TUI. Streaming omits ``immediate`` and coalesces paints."""
        app = self._app
        if app is None:
            return
        if not immediate:
            now = time.monotonic()
            wait = self._paint_interval - (now - self._last_paint_ts)
            if wait > 0:
                with self._lock:
                    if self._paint_timer is not None:
                        return
                    timer = threading.Timer(wait, self._deferred_invalidate)
                    timer.daemon = True
                    self._paint_timer = timer
                timer.start()
                return
        self._cancel_paint_timer()
        self._last_paint_ts = time.monotonic()
        app.invalidate()

    def _deferred_invalidate(self) -> None:
        with self._lock:
            self._paint_timer = None
        self._last_paint_ts = time.monotonic()
        app = self._app
        if app is not None:
            app.invalidate()

    def _cancel_paint_timer(self) -> None:
        with self._lock:
            timer = self._paint_timer
            self._paint_timer = None
        if timer is not None:
            timer.cancel()

    def _note_paint_cost(self, elapsed: float) -> None:
        """Aider-style adaptive fps: slow frames back off instead of stacking."""
        self._paint_interval = min(max(float(elapsed) * 8.0, _PAINT_INTERVAL_S), 0.4)

    def _sync_activity_refresh(self) -> None:
        """Faster chrome ticks while waiting on the model so the spinner moves."""
        app = self._app
        if app is None:
            return
        with self._lock:
            waiting = (
                bool(self._busy or self._status)
                and not self._live_md
                and not self._ask_mode
            )
        app.refresh_interval = _ACTIVITY_REFRESH_S if waiting else _IDLE_REFRESH_S

    def run(self) -> None:
        self.refresh_chrome(force=True)
        app = self._build()
        self._app = app
        self._sync_activity_refresh()
        try:
            app.run()
        finally:
            self._cancel_paint_timer()
            self._app = None
            self._ask_value = ""
            self._choice_options = []
            self._choice_index = 0
            self._ask_event.set()

    def stop(self) -> None:
        self._cancel_paint_timer()
        app = self._app
        if app is not None:
            app.exit()

    def _pane_style(self, pane: FocusPane) -> str:
        with self._lock:
            focus = self._focus
        if focus == pane:
            return "class:pane-focus"
        return "class:pane"

    def _sync_frame_styles(self) -> None:
        if self._frame_left is not None:
            self._frame_left.style = self._pane_style("left")
        if self._frame_center is not None:
            self._frame_center.style = self._pane_style("main")
        if self._frame_right is not None:
            self._frame_right.style = self._pane_style("right")
        if self._frame_input is not None:
            self._frame_input.style = self._pane_style("input")

    def _build(self) -> Application[None]:
        header = Frame(
            Window(
                height=1,
                content=FormattedTextControl(self._render_header),
            ),
            title=self._header_title,
            style="class:pane",
        )
        tip = Window(
            height=Dimension(min=0, max=2),
            content=FormattedTextControl(self._render_narrow_tip, focusable=False),
        )

        left_body = Window(
            content=FormattedTextControl(self._render_left, focusable=False),
            wrap_lines=False,
            style="class:rail",
        )
        center_body = Window(
            content=_TranscriptControl(self),
            wrap_lines=False,
            always_hide_cursor=True,
            style="class:main",
        )
        right_body = Window(
            content=FormattedTextControl(self._render_right, focusable=False),
            wrap_lines=False,
            style="class:rail",
        )
        self._body_left = left_body
        self._body_center = center_body
        self._body_right = right_body

        self._frame_left = Frame(
            left_body,
            title=self._left_title,
            style="class:pane",
            width=Dimension(min=_LEFT_FRAME, max=_LEFT_FRAME, preferred=_LEFT_FRAME),
        )
        self._frame_center = Frame(
            center_body,
            title=self._center_title,
            style="class:pane",
        )
        self._frame_right = Frame(
            right_body,
            title=self._right_title,
            style="class:pane",
            width=Dimension(min=_RIGHT_FRAME, max=_RIGHT_FRAME, preferred=_RIGHT_FRAME),
        )

        @Condition
        def _show_left() -> bool:
            return self._layout_show_left()

        @Condition
        def _show_right() -> bool:
            return self._layout_show_right()

        mid = VSplit(
            [
                ConditionalContainer(self._frame_left, filter=_show_left),
                self._frame_center,
                ConditionalContainer(self._frame_right, filter=_show_right),
            ],
            padding=0,
        )

        history = FileHistory(str(_history_file()))
        self._input = TextArea(
            height=1,
            prompt=self._prompt_text,
            multiline=False,
            wrap_lines=False,
            history=history,
            completer=self._input_completer,
            complete_while_typing=True,
        )
        self._input.accept_handler = self._accept
        self._frame_input = Frame(
            self._input,
            title=self._input_title,
            style="class:pane-focus",
        )

        # Toolbar must never participate in focus traversal. On Windows a lost
        # BufferControl focus often parks the hardware cursor on the last row;
        # if that row is this Window, typed keys echo into the status line.
        toolbar = Window(
            height=1,
            content=FormattedTextControl(self._toolbar_text, focusable=False),
            style="class:bottom-toolbar",
            dont_extend_height=True,
        )

        root = FloatContainer(
            content=HSplit(
                [
                    header,
                    tip,
                    mid,
                    self._frame_input,
                    toolbar,
                ]
            ),
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=CompletionsMenu(max_height=12, scroll_offset=1),
                )
            ],
        )

        kb = self._key_bindings()
        style = Style.from_dict(
            {
                "pane": OPS_BORDER,
                "pane-focus": "#e6a23c",
                "rail": OPS_SECONDARY,
                "rail-focus": "#e6a23c bold",
                "header": OPS_HUD,
                "bottom-toolbar": f"bg:#171a20 {OPS_SECONDARY}",
                "main": OPS_TEXT,
                "text-area": OPS_TEXT,
                "text-area.prompt": "#e6a23c bold",
                "completion-menu.completion": f"bg:#20242c {OPS_TEXT}",
                "completion-menu.completion.current": "bg:#3b4657 #ffffff bold",
                "completion-menu.meta.completion": f"bg:#171a20 {OPS_MUTED}",
                "completion-menu.meta.completion.current": "bg:#3b4657 #cdd3de",
                "meter": "#7eb8c9",
                "auth": "#e6a23c bold",
                "monitor-title": "#e6a23c bold",
                "monitor-value": OPS_TEXT,
                "monitor-subtle": OPS_SECONDARY,
                "monitor-cyan": "#7eb8c9",
                "monitor-ok": "#78ad87",
                "monitor-warn": "#e6a23c",
                "monitor-tool": "#d4a574",
                "monitor-fail": "#e06c75",
                "activity": "#e6a23c bold",
                "activity-time": "#7eb8c9",
            }
        )

        self._sync_frame_styles()
        return Application(
            layout=Layout(root, focused_element=self._input),
            key_bindings=kb,
            style=style,
            full_screen=True,
            mouse_support=True,
            output=_OpsOutput(get_app_session().output),
            refresh_interval=_IDLE_REFRESH_S,
            before_render=lambda _app: self._before_render(),
            after_render=lambda _app: self._pin_input_layout_focus(),
        )

    def _header_title(self) -> Any:
        return FormattedText([("class:pane-focus", " ARGUSAI "), ("class:rail", " ops ")])

    def _left_title(self) -> Any:
        with self._lock:
            focus = self._focus == "left"
            session = dict(self._session_cache)
            rail = self._rail_lines
        if rail is not None:
            mark = "›" if focus else " "
            style = "class:pane-focus" if focus else "class:rail"
            return FormattedText([(style, f" {mark} {label('team_rail_title')} ")])
        todos = _flatten_todos(list(session.get("todos") or []))
        done = sum(1 for t in todos if (t.get("status") or "") == "completed")
        total = len(todos)
        mark = "›" if focus else " "
        style = "class:pane-focus" if focus else "class:rail"
        return FormattedText([(style, f" {mark} TODO {done}/{total} ")])

    def _center_title(self) -> Any:
        height = self._center_height()
        with self._lock:
            scroll = self._scroll
            total = self._center_line_count
            focus = self._focus == "main"
            choice_count = len(self._choice_options)
            choice_index = self._choice_index
            pending_n = len(
                [p for p in list(self._session_cache.get("pending_tools") or []) if isinstance(p, dict)]
            )
            busy = self._busy
        max_scroll = max(0, total - height) if total else 0
        mark = "›" if focus else " "
        style = "class:pane-focus" if focus else "class:rail"
        bits = [f" {mark} MAIN "]
        if busy:
            bits.append("· run ")
        if pending_n:
            bits.append(f"· AUTH {pending_n} ")
        if choice_count:
            bits.append(f"· {label('tag_choice')} {choice_index + 1}/{choice_count} ")
            if focus:
                bits.append("· ↑↓ Enter ")
        if max_scroll > 0:
            # How far above the bottom we are / how far we can go.
            bits.append(f"· ▲{scroll}/{max_scroll} ")
        elif focus:
            bits.append("· ↑↓ ")
        return FormattedText([(style, "".join(bits))])

    def _right_title(self) -> Any:
        with self._lock:
            focus = self._focus == "right"
            monitor = self._monitor
        mark = "›" if focus else " "
        style = "class:pane-focus" if focus else "class:rail"
        return FormattedText([(style, f" {mark} MONITOR:{monitor} ")])

    def _input_title(self) -> Any:
        with self._lock:
            focus = self._focus == "input"
            ask = self._ask_mode
        mark = "›" if focus else " "
        style = "class:pane-focus" if focus else "class:rail"
        name = " ASK " if ask else " INPUT "
        return FormattedText([(style, f" {mark}{name}")])

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        def _move_input_completion(event, *, backwards: bool = False) -> bool:  # noqa: ANN001
            if self._focus != "input" or self._input is None:
                return False
            buffer = self._input.buffer
            state = buffer.complete_state
            reference_document = (
                state.original_document
                if state is not None
                else buffer.document
            )
            if not input_completion_at_cursor(reference_document):
                return False
            if state is not None:
                if backwards:
                    buffer.complete_previous()
                else:
                    buffer.complete_next()
                event.app.invalidate()
                return True
            if not input_completion_at_cursor(buffer.document):
                return False
            buffer.start_completion(
                select_first=not backwards,
                select_last=backwards,
            )
            event.app.invalidate()
            return True

        @kb.add("c-c", eager=True)
        def _interrupt(event) -> None:  # noqa: ANN001
            # Always leave — a stuck Windows console focus can make the usual
            # "busy" soft-cancel unreachable; users need a hard escape hatch.
            if self._ask_mode:
                self._ask_value = ""
                self._ask_event.set()
            event.app.exit()

        @kb.add("c-q", eager=True)
        def _force_quit(event) -> None:  # noqa: ANN001
            event.app.exit()

        @kb.add("c-d", eager=True)
        def _eof(event) -> None:  # noqa: ANN001
            if not self._input or not self._input.text:
                event.app.exit()

        @Condition
        def _blank_input_enter() -> bool:
            """Empty Enter must not run accept_handler (Windows cursor trap)."""
            if self._ask_mode:
                return False
            if self._focus == "right":
                return False
            if self._input is None:
                return False
            if self._input.buffer.complete_state is not None:
                return False
            return not str(self._input.buffer.text or "").strip()

        @kb.add("enter", filter=_blank_input_enter, eager=True)
        def _ignore_blank_enter(event) -> None:  # noqa: ANN001
            # Swallow before TextArea.validate_and_handle / accept_handler.
            with self._lock:
                self._focus = "input"
            self._sync_frame_styles()
            event.app.invalidate()

        @kb.add("pageup")
        def _page_up(event) -> None:  # noqa: ANN001
            self._scroll_transcript(self._page_step())
            event.app.invalidate()

        @kb.add("pagedown")
        def _page_down(event) -> None:  # noqa: ANN001
            self._scroll_transcript(-self._page_step())
            event.app.invalidate()

        @kb.add(Keys.Vt100MouseEvent, eager=True)
        def _vt_mouse(event) -> Any:  # noqa: ANN001
            delta = _mouse_scroll_delta(event.data or "")
            if not delta:
                return NotImplemented
            self._scroll_transcript(delta)
            event.app.invalidate()
            return None

        @kb.add(Keys.WindowsMouseEvent, eager=True)
        def _win_mouse(event) -> Any:  # noqa: ANN001
            delta = _mouse_scroll_delta(event.data or "")
            if not delta:
                return NotImplemented
            self._scroll_transcript(delta)
            event.app.invalidate()
            return None

        @kb.add(Keys.ScrollUp, eager=True)
        def _alt_scroll_up(event) -> None:  # noqa: ANN001
            self._scroll_transcript(_WHEEL_STEP)
            event.app.invalidate()

        @kb.add(Keys.ScrollDown, eager=True)
        def _alt_scroll_down(event) -> None:  # noqa: ANN001
            self._scroll_transcript(-_WHEEL_STEP)
            event.app.invalidate()

        @kb.add("c-home")
        @kb.add("home", filter=Condition(lambda: self._focus == "main" and not self._ask_mode))
        def _scroll_top(event) -> None:  # noqa: ANN001
            self._scroll_transcript(10_000)
            event.app.invalidate()

        @kb.add("c-end")
        @kb.add("end", filter=Condition(lambda: self._focus == "main" and not self._ask_mode))
        def _scroll_bottom(event) -> None:  # noqa: ANN001
            self._scroll_transcript(-10_000)
            event.app.invalidate()

        @kb.add("tab")
        def _tab(event) -> None:  # noqa: ANN001
            if self._ask_mode:
                if self._choice_options:
                    self._toggle_choice_focus()
                    event.app.invalidate()
                return
            if _move_input_completion(event):
                return
            self._cycle_focus(1)
            event.app.invalidate()

        @kb.add("s-tab")
        def _s_tab(event) -> None:  # noqa: ANN001
            if self._ask_mode:
                if self._choice_options:
                    self._toggle_choice_focus()
                    event.app.invalidate()
                return
            if _move_input_completion(event, backwards=True):
                return
            self._cycle_focus(-1)
            event.app.invalidate()

        @Condition
        def _input_completion_selected() -> bool:
            if self._focus != "input" or self._input is None:
                return False
            state = self._input.buffer.complete_state
            return state is not None and state.current_completion is not None

        @Condition
        def _input_completion_visible() -> bool:
            return (
                self._focus == "input"
                and self._input is not None
                and self._input.buffer.complete_state is not None
            )

        @kb.add("escape", filter=_input_completion_visible, eager=True)
        def _cancel_input_completion(event) -> None:  # noqa: ANN001
            if self._input is not None:
                cancel_input_completion(self._input.buffer)
                event.app.invalidate()

        @kb.add("enter", filter=_input_completion_selected, eager=True)
        def _apply_input_completion(event) -> None:  # noqa: ANN001
            if self._input is None:
                return
            state = self._input.buffer.complete_state
            completion = state.current_completion if state is not None else None
            if completion is not None:
                self._input.buffer.apply_completion(completion)
                event.app.invalidate()

        @Condition
        def _right_nav() -> bool:
            return self._focus == "right" and not self._ask_mode

        @Condition
        def _main_nav() -> bool:
            return self._focus == "main" and not self._ask_mode

        @Condition
        def _choice_nav() -> bool:
            return (
                self._focus == "main"
                and self._ask_mode
                and bool(self._choice_options)
            )

        @kb.add("up", filter=_choice_nav)
        def _choice_up(event) -> None:  # noqa: ANN001
            self._move_choice(-1)
            event.app.invalidate()

        @kb.add("down", filter=_choice_nav)
        def _choice_down(event) -> None:  # noqa: ANN001
            self._move_choice(1)
            event.app.invalidate()

        @kb.add("enter", filter=_choice_nav, eager=True)
        def _choice_enter(event) -> None:  # noqa: ANN001
            self._submit_choice()
            event.app.invalidate()

        @kb.add("up", filter=_main_nav)
        def _main_up(event) -> None:  # noqa: ANN001
            self._scroll_transcript(1)
            event.app.invalidate()

        @kb.add("down", filter=_main_nav)
        def _main_down(event) -> None:  # noqa: ANN001
            self._scroll_transcript(-1)
            event.app.invalidate()

        @kb.add("up", filter=_right_nav)
        def _up(event) -> None:  # noqa: ANN001
            with self._lock:
                if self._jobs_cache:
                    self._job_index = max(0, self._job_index - 1)
            event.app.invalidate()

        @kb.add("down", filter=_right_nav)
        def _down(event) -> None:  # noqa: ANN001
            with self._lock:
                if self._jobs_cache:
                    self._job_index = min(len(self._jobs_cache) - 1, self._job_index + 1)
            event.app.invalidate()

        @kb.add("n", filter=_right_nav)
        @kb.add("d", filter=_right_nav)
        def _deny_right(event) -> None:  # noqa: ANN001
            self._auth_action("deny")
            event.app.invalidate()

        @Condition
        def _needs_input_refocus() -> bool:
            # Logical pane is browsing (MAIN/LEFT). Real PT focus is always
            # pinned to the input dock; this only resets the logical pane.
            if self._ask_mode:
                return False
            return self._focus not in {"input", "right"}

        @kb.add(Keys.Any, filter=_needs_input_refocus)
        def _type_returns_to_input(event) -> None:  # noqa: ANN001
            """Typing while browsing panes jumps logical focus back to input."""
            key = event.key_sequence[-1].key if event.key_sequence else None
            if key in {
                Keys.Vt100MouseEvent,
                Keys.WindowsMouseEvent,
                Keys.ScrollUp,
                Keys.ScrollDown,
            }:
                return
            data = getattr(event, "data", None)
            if not data or not str(data).isprintable() or str(data) in {"\x00", "\r", "\n"}:
                return
            self._focus_input()
            buf = getattr(self._input, "buffer", None) if self._input is not None else None
            if buf is not None:
                buf.insert_text(str(data))
            event.app.invalidate()

        return kb

    def _page_step(self) -> int:
        return max(3, self._center_height() - 2)

    def _scroll_transcript(self, delta: int) -> None:
        """Move the sticky-bottom viewport by ``delta`` visual lines."""
        width = self._center_width()
        lines = self._transcript_lines(width)
        height = self._center_height()
        max_scroll = max(0, len(lines) - height)
        with self._lock:
            self._scroll = max(0, min(max_scroll, self._scroll + delta))
            self._center_line_count = len(lines)

    def _focus_ring(self) -> list[FocusPane]:
        # INPUT → LEFT → MAIN → RIGHT (skip hidden rails)
        ring: list[FocusPane] = ["input"]
        if self._layout_show_left():
            ring.append("left")
        ring.append("main")
        if self._layout_show_right():
            ring.append("right")
        return ring

    def _layout_focus_is_input(self) -> bool | None:
        """True when the real PT focus is the input buffer; None if unknown."""
        if self._app is None or self._input is None:
            return None
        try:
            return self._app.current_buffer is self._input.buffer
        except Exception:
            return None

    def _pin_input_layout_focus(self) -> None:
        """Force real keyboard focus onto the input dock (never left/main/right)."""
        if self._app is None or self._input is None:
            return
        if self._layout_focus_is_input() is True:
            return
        try:
            self._app.layout.focus(self._input)
        except Exception:
            pass

    def _apply_layout_focus(self) -> None:
        """Pin real PT focus to the input dock.

        Tab only changes the logical ``_focus`` pane (styles + which nav
        bindings are armed). Moving real focus to MAIN/LEFT/RIGHT on Windows
        makes typed characters echo into the transcript instead of the dock.
        """
        self._pin_input_layout_focus()

    def _before_render(self) -> None:
        self.refresh_chrome(force=False)
        self._pin_input_layout_focus()

    def _focus_input(self) -> None:
        with self._lock:
            self._focus = "input"
        self._apply_layout_focus()
        self._sync_frame_styles()

    def _toggle_choice_focus(self) -> None:
        with self._lock:
            self._focus = "main" if self._focus == "input" else "input"
        self._apply_layout_focus()
        self._sync_frame_styles()

    def _move_choice(self, delta: int) -> None:
        with self._lock:
            if not self._choice_options:
                return
            self._choice_index = max(
                0,
                min(len(self._choice_options) - 1, self._choice_index + delta),
            )
            self._scroll = 0

    def _submit_choice(self) -> None:
        with self._lock:
            if not self._ask_mode or not self._choice_options:
                return
            self._ask_value = self._choice_options[self._choice_index]
            self._focus = "input"
            self._ask_event.set()
        self._apply_layout_focus()
        self._sync_frame_styles()

    def _cycle_focus(self, step: int) -> None:
        ring = self._focus_ring()
        with self._lock:
            cur = self._focus
            if cur not in ring:
                cur = "input"
            idx = ring.index(cur)
            self._focus = ring[(idx + step) % len(ring)]
        self._apply_layout_focus()
        self._sync_frame_styles()

    def _layout_show_left(self) -> bool:
        cols = _term_cols()
        with self._lock:
            monitor_on = self._monitor != "off"
            sidebar = self._sidebar
        if sidebar == "off":
            return False
        if sidebar == "on":
            return cols >= 64
        if cols < 88:
            return False
        if 88 <= cols < 120 and monitor_on:
            return False
        return True

    def _layout_show_right(self) -> bool:
        cols = _term_cols()
        with self._lock:
            monitor_on = self._monitor != "off"
            sidebar = self._sidebar
        if not monitor_on:
            return False
        if sidebar == "on" and cols < 120:
            return False
        return cols >= 88

    def _auth_action(self, verdict: str) -> None:
        selector = ""
        with self._lock:
            jobs = list(self._jobs_cache)
            idx = self._job_index
            session = dict(self._session_cache)
        pending = [
            p for p in list(session.get("pending_tools") or []) if isinstance(p, dict)
        ]
        if jobs and 0 <= idx < len(jobs):
            job = jobs[idx]
            state = str(job.get("state") or "").lower()
            if state == "waiting_user" and pending:
                selector = str(idx + 1)
        if verdict == "allow" and self._on_allow is not None:
            self._on_allow(selector)
        elif verdict == "deny" and self._on_deny is not None:
            self._on_deny(selector)

    # ── renderers ──────────────────────────────────────────────────

    def _prompt_text(self) -> Any:
        with self._lock:
            prompt = self._prompt
        return FormattedText([("class:text-area.prompt", prompt)])

    def _toolbar_text(self) -> Any:
        text = ""
        if self._toolbar_provider is not None:
            try:
                text = str(self._toolbar_provider() or "")
            except Exception:
                text = ""
        if not text:
            text = " ArgusAI ops "
        with self._lock:
            status = self._status
            busy = self._busy and not self._ask_mode
            monitor = self._monitor
            sidebar = self._sidebar
            choice_count = len(self._choice_options)
            choice_index = self._choice_index
            pending_n = len(
                [p for p in list(self._session_cache.get("pending_tools") or []) if isinstance(p, dict)]
            )
            focus = self._focus
            started_at = self._status_started_at
        activity = ops_activity_parts(
            status,
            busy=busy,
            started_at=started_at,
            now=time.monotonic(),
        )
        bits: list[str] = []
        if activity is None:
            bits.append(text.strip())
            if busy:
                bits.insert(0, label("ops_busy_toolbar"))
        else:
            bits.append(text.strip())
            if busy:
                bits.insert(0, label("ops_busy_side"))
        if pending_n:
            bits.insert(0, label("ops_auth_pending", count=pending_n))
        if choice_count:
            bits.insert(
                0,
                label(
                    "ops_choice_active",
                    current=choice_index + 1,
                    total=choice_count,
                ),
            )
        if monitor != "off":
            bits.append(f"monitor:{monitor}")
        if sidebar != "off":
            bits.append(f"sidebar:{sidebar}")
        bits.append(
            label("ops_choice_tab_hint" if choice_count else "ops_tab_hint")
        )
        bits.append(f"focus:{focus}")
        rest = "  ·  ".join(bit for bit in bits if bit)
        if activity is None:
            return FormattedText([("class:bottom-toolbar", f"  {rest} ")])
        spinner, verb, elapsed = activity
        return FormattedText(
            [
                ("class:activity", f"  {spinner} {verb}"),
                ("class:activity-time", f"  {elapsed}"),
                ("class:bottom-toolbar", f"  ·  {rest} "),
            ]
        )

    def _render_header(self) -> Any:
        with self._lock:
            text = self._header_text or self._title
        # Drop redundant brand — frame title already says ARGUSAI.
        cleaned = text
        for prefix in ("ARGUSAI ops  ", "ARGUSAI ops ", "ARGUSAI "):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :]
                break
        return FormattedText([("class:header", f" {cleaned} ")])

    def _render_narrow_tip(self) -> Any:
        with self._lock:
            session = dict(self._session_cache)
            monitor = self._monitor
        left_visible = self._layout_show_left()
        right_visible = self._layout_show_right()
        lines: list[str] = []
        if not left_visible:
            todos = _flatten_todos(list(session.get("todos") or []))
            if todos:
                done = sum(1 for t in todos if (t.get("status") or "") == "completed")
                cur = next(
                    (t for t in todos if (t.get("status") or "") == "in_progress"),
                    None,
                )
                title = str((cur or todos[0]).get("title") or (cur or todos[0]).get("content") or "")[:40]
                lines.append(
                    f" TODO {done}/{len(todos)}  "
                    f"{_todo_mark((cur or {}).get('status') or 'pending')} {title}"
                )
        if monitor != "off" and not right_visible:
            lines.append(f" {label('ops_monitor_collapsed')}")
        if not lines:
            return FormattedText([("", "")])
        return FormattedText([(OPS_MUTED, "\n".join(lines))])

    def _render_left(self) -> Any:
        width = _LEFT_FRAME - 4
        with self._lock:
            session = dict(self._session_cache)
            focus = self._focus == "left"
            rail = list(self._rail_lines) if self._rail_lines is not None else None
        if rail is not None:
            shown = [line.replace("\n", " ")[: max(8, width)] for line in rail[:24]]
            style = "class:rail-focus" if focus else "class:rail"
            return FormattedText([(style, "\n".join(shown))])
        todos = _flatten_todos(list(session.get("todos") or []))
        lines = [_section(label("ops_rail_todo"), focus=focus)]
        if todos:
            for item in todos[:12]:
                st = str(item.get("status") or "pending")
                title = str(item.get("title") or item.get("content") or item.get("id") or "")
                title = title.replace("\n", " ")[: max(8, width - 4)]
                prefix = "»" if st == "in_progress" else " "
                lines.append(f"{prefix}{_todo_mark(st)} {title}")
        else:
            lines.append(f"  {label('ops_todo_empty')}")
        lines.append("")
        lines.append(_section(label("ops_rail_session"), focus=focus))
        try:
            msgs = int(session.get("message_count") or 0)
        except (TypeError, ValueError):
            msgs = 0
        running = bool(session.get("active_stream"))
        state = label("ops_session_running" if running else "ops_session_idle")
        lines.append(f"  msgs  {msgs}")
        lines.append(f"  state {state}")
        think_on = bool(session.get("thinking_enabled"))
        think_effort = str(session.get("thinking_effort") or "high")
        lines.append(f"  think {'on/' + think_effort if think_on else 'off'}")
        ctx = context_usage_parts(session)
        if ctx is not None:
            lines.append(
                f"  ctx   {ctx['percent']:.0f}% {fmt_tokens(int(ctx['used']))}/{fmt_tokens(int(ctx['window']))}"
            )
        style = "class:rail-focus" if focus else "class:rail"
        return FormattedText([(style, "\n".join(lines))])

    def _render_right(self) -> Any:
        with self._lock:
            monitor = self._monitor
            focus = self._focus == "right"
            metrics = dict(self._metrics_cache)
            token_usage = dict(self._token_usage_cache)
            session = dict(self._session_cache)
            jobs = list(self._jobs_cache)
            job_index = self._job_index
            pending = [
                p
                for p in list(self._session_cache.get("pending_tools") or [])
                if isinstance(p, dict)
            ]
        lines: StyleAndTextTuples = []
        if monitor in {"tokens", "both"}:
            _push_formatted_line(
                lines,
                [("class:monitor-title", _section(label("ops_rail_tokens"), focus=focus))],
            )
            if not token_usage:
                token_usage = {
                    "ok": True,
                    "token_usage": dict(session.get("token_usage") or {}),
                    "cache_hit_rate": session.get("cache_hit_rate"),
                }
            else:
                token_usage = dict(token_usage)
            for key in (
                "prompt_tokens",
                "prompt_bytes",
                "context_window",
                "last_prompt_tokens_est",
                "last_prompt_bytes",
            ):
                if not token_usage.get(key) and session.get(key):
                    token_usage[key] = session[key]
            token_lines = format_token_usage_lines(token_usage, bar_width=8)
            if token_lines:
                token_accents = {
                    "CTX": "class:monitor-cyan",
                    "IN": "class:monitor-cyan",
                    "OUT": "class:monitor-ok",
                    "HIT": "class:monitor-ok",
                    "H/M": "class:monitor-warn",
                    "WRITE": "class:monitor-warn",
                    "CALLS": "class:monitor-cyan",
                }
                for row in token_lines:
                    key = row.split(maxsplit=1)[0]
                    _push_formatted_line(
                        lines,
                        _monitor_row(
                            f" {row}",
                            accent=token_accents.get(key, "class:monitor-cyan"),
                        ),
                    )
            else:
                _push_formatted_line(
                    lines,
                    [("class:monitor-subtle", f"  {label('ops_tokens_empty')}")],
                )
            _push_formatted_line(lines, [("", "")])
        if monitor in {"sys", "both"}:
            _push_formatted_line(
                lines,
                [("class:monitor-title", _section(label("ops_rail_sys"), focus=focus))],
            )
            metric_accents = {
                "CPU": "class:monitor-cyan",
                "MEM": "class:monitor-ok",
                "SWP": "class:monitor-warn",
                "DSK": "class:monitor-tool",
                "APP": "class:monitor-cyan",
                "HOST": "class:monitor-subtle",
            }
            current_accent = "class:monitor-cyan"
            for row in format_metrics_lines(metrics, bar_width=8):
                key = row.split(maxsplit=1)[0] if row.strip() else ""
                if key in metric_accents:
                    current_accent = metric_accents[key]
                _push_formatted_line(
                    lines,
                    _monitor_row(f" {row}", accent=current_accent),
                )
            _push_formatted_line(lines, [("", "")])
        if monitor in {"agents", "both"}:
            _push_formatted_line(
                lines,
                [("class:monitor-title", _section(label("ops_rail_agents"), focus=focus))],
            )
            if not jobs:
                _push_formatted_line(
                    lines,
                    [("class:monitor-subtle", f"  {label('ops_agents_empty')}")],
                )
            else:
                for i, job in enumerate(jobs[:6]):
                    jid = str(job.get("label") or job.get("job_id") or job.get("id") or "")[:16]
                    state = str(job.get("state") or "?")
                    auth = ""
                    if state == "waiting_user":
                        auth = " !AUTH"
                    elif pending and i == 0 and state == "running":
                        auth = " ?"
                    cursor = "▶" if focus and i == job_index else " "
                    state_lower = state.lower()
                    if state_lower == "completed":
                        state_style = "class:monitor-ok"
                    elif state_lower in {"failed", "cancelled"}:
                        state_style = "class:monitor-fail"
                    elif state_lower in {"waiting_user", "awaiting_confirmation"}:
                        state_style = "class:monitor-warn"
                    else:
                        state_style = "class:monitor-cyan"
                    _push_formatted_line(
                        lines,
                        [(state_style, f"{cursor}{i + 1}.{jid} {state}{auth}")],
                    )
        return FormattedText(lines or [("", " ")])

    def _center_width(self) -> int:
        """Prefer the last laid-out MAIN width; fall back to terminal math."""
        with self._lock:
            if self._viewport_width > 0:
                return self._viewport_width
        cols = _term_cols()
        width = cols
        if self._layout_show_left():
            width -= _LEFT_FRAME
        if self._layout_show_right():
            width -= _RIGHT_FRAME
        width -= 2  # MAIN frame borders
        # Stay a bit tight vs estimate — oversizing clips the newest lines.
        return max(24, width - 1)

    def _center_height(self) -> int:
        """Prefer the last laid-out MAIN height; fall back conservatively."""
        with self._lock:
            if self._viewport_height > 0:
                return self._viewport_height
        rows = _term_rows()
        with self._lock:
            monitor = self._monitor
        tip = (
            2
            if not self._layout_show_left()
            or (monitor != "off" and not self._layout_show_right())
            else 0
        )
        # header(3) + tip + input(3) + toolbar(1) + main borders(2) + safety(2)
        return max(4, rows - (3 + tip + 3 + 1 + 2 + 2))

    def _extend_log_line_cache(self, text: str) -> None:
        """Append one committed blob onto the cached line list when width matches."""
        cache = self._log_lines_cache
        width = self._log_lines_width
        if cache is None or width <= 0:
            return
        if self._viewport_width and width != self._viewport_width:
            self._log_lines_cache = None
            return
        new_lines = list(split_lines(to_formatted_text(ANSI(text))))
        if cache:
            cache.append([("", "")])
        cache.extend(new_lines)

    def _committed_transcript_lines(self, width: int) -> list[StyleAndTextTuples]:
        with self._lock:
            if self._log_lines_cache is not None and self._log_lines_width == width:
                return self._log_lines_cache
            log = list(self._log)
            gen = self._log_gen
        if not log:
            lines: list[StyleAndTextTuples] = []
        else:
            lines = list(split_lines(to_formatted_text(ANSI("\n\n".join(log)))))
        with self._lock:
            if self._log_gen == gen:
                self._log_lines_cache = lines
                self._log_lines_width = width
        return lines

    def _live_header_ansi(self, width: int) -> str:
        with self._lock:
            header_cached = self._live_header_cache
        if header_cached is not None and header_cached[0] == width:
            return header_cached[1]
        header = rich_to_ansi(
            Text.from_markup("[reply]╭─ ARGUSAI[/]"),
            width=width,
        )
        with self._lock:
            self._live_header_cache = (width, header)
        return header

    def _wrap_plain_gutter_lines(self, text: str, width: int) -> list[StyleAndTextTuples]:
        if not text:
            return []
        body = rich_to_ansi(
            Text(text, style=OPS_TEXT),
            width=max(20, width - 2),
        )
        return list(
            split_lines(to_formatted_text(ANSI(_with_ansi_gutter(body, "#7eb8c9"))))
        )

    def _live_transcript_lines(self, width: int, live: str) -> list[StyleAndTextTuples]:
        """Codex-style live tail: freeze wrapped complete lines, rewrap only the last line."""
        complete, newline, tail = live.rpartition("\n")
        with self._lock:
            stable_src = self._live_stable_src
            stable_width = self._live_stable_width
            stable_lines = self._live_stable_lines
        if complete != stable_src or width != stable_width or stable_lines is None:
            rest = ""
            if (
                stable_lines is not None
                and width == stable_width
                and stable_src
                and complete.startswith(stable_src)
                and complete[len(stable_src) :].startswith("\n")
            ):
                rest = complete[len(stable_src) + 1 :]
                extra = self._wrap_plain_gutter_lines(rest, width) if rest else []
                stable_lines = [*stable_lines, *extra]
            else:
                stable_lines = (
                    self._wrap_plain_gutter_lines(complete, width) if complete else []
                )
            with self._lock:
                self._live_stable_src = complete
                self._live_stable_width = width
                self._live_stable_lines = stable_lines
        header_lines = list(split_lines(to_formatted_text(ANSI(self._live_header_ansi(width)))))
        tail_lines = self._wrap_plain_gutter_lines(tail, width) if tail else []
        parts = header_lines
        if stable_lines:
            parts = [*parts, *stable_lines]
        if tail_lines:
            parts = [*parts, *tail_lines]
        elif newline and not tail:
            # Trailing newline: keep a blank gutter row so the cursor line exists.
            parts = [*parts, *self._wrap_plain_gutter_lines(" ", width)]
        return parts

    def _live_reply_ansi(self, width: int, live: str) -> str:
        # Tests and blob helper: cheap wrapped text, not Markdown.
        with self._lock:
            cached = self._live_ansi_cache
        if cached is not None and cached[0] == live and cached[1] == width:
            return cached[2]
        header = self._live_header_ansi(width)
        body = rich_to_ansi(
            Text(live, style=OPS_TEXT),
            width=max(20, width - 2),
        )
        blob = header + "\n" + _with_ansi_gutter(body, "#7eb8c9")
        with self._lock:
            self._live_ansi_cache = (live, width, blob)
        return blob

    def _transcript_blob(self, width: int) -> str:
        with self._lock:
            log = list(self._log)
            live = self._live_md
            status = self._status
            choices = list(self._choice_options)
            choice_index = self._choice_index

        parts: list[str] = []
        if log:
            parts.append("\n\n".join(log))
        if live.strip():
            parts.append(self._live_reply_ansi(width, live))
        elif status and not log:
            parts.append(
                rich_to_ansi(Text(f"▸ {status}…", style=OPS_MUTED), width=width)
            )
        if choices:
            selected = choices[max(0, min(len(choices) - 1, choice_index))]
            selected = selected.replace("\n", " ")
            max_chars = max(12, width - 12)
            if len(selected) > max_chars:
                selected = selected[: max_chars - 1].rstrip() + "…"
            choice = Text()
            choice.append(
                f"▶ [{choice_index + 1}/{len(choices)}] ",
                style="bold #e6a23c",
            )
            choice.append(selected, style=OPS_TEXT)
            choice.append("\n")
            choice.append(label("decision_keyboard_hint"), style=OPS_SECONDARY)
            parts.append(rich_to_ansi(choice, width=width))

        if not parts:
            idle = Text()
            idle.append(label("ops_idle_title"), style="#e6a23c")
            idle.append("\n")
            idle.append(label("ops_idle_hint"), style=OPS_MUTED)
            return rich_to_ansi(idle, width=width)
        return "\n\n".join(parts)

    def _transcript_lines(self, width: int) -> list[StyleAndTextTuples]:
        with self._lock:
            live = self._live_md
            status = self._status
            busy = self._busy
            has_log = bool(self._log)
            started_at = self._status_started_at
            choices = list(self._choice_options)
            choice_index = self._choice_index

        lines = self._committed_transcript_lines(width)
        extra_lines: list[StyleAndTextTuples] = []
        if live.strip():
            extra_lines.extend(self._live_transcript_lines(width, live))
        elif not choices:
            activity = ops_activity_parts(
                status,
                busy=busy,
                started_at=started_at,
                now=time.monotonic(),
            )
            if activity:
                spinner, verb, elapsed = activity
                row = Text()
                row.append(f"{spinner} ", style="#e6a23c")
                row.append(verb, style=OPS_SECONDARY)
                row.append(f"  {elapsed}", style="#7eb8c9")
                extra_lines.extend(
                    split_lines(
                        to_formatted_text(ANSI(rich_to_ansi(row, width=width)))
                    )
                )
            elif status and not has_log:
                extra_lines.extend(
                    split_lines(
                        to_formatted_text(
                            ANSI(rich_to_ansi(Text(f"▸ {status}…", style=OPS_MUTED), width=width))
                        )
                    )
                )
        if choices:
            selected = choices[max(0, min(len(choices) - 1, choice_index))]
            selected = selected.replace("\n", " ")
            max_chars = max(12, width - 12)
            if len(selected) > max_chars:
                selected = selected[: max_chars - 1].rstrip() + "…"
            choice = Text()
            choice.append(
                f"▶ [{choice_index + 1}/{len(choices)}] ",
                style="bold #e6a23c",
            )
            choice.append(selected, style=OPS_TEXT)
            choice.append("\n")
            choice.append(label("decision_keyboard_hint"), style=OPS_SECONDARY)
            extra_lines.extend(
                split_lines(to_formatted_text(ANSI(rich_to_ansi(choice, width=width))))
            )

        if extra_lines:
            extra_lines = list(extra_lines)
            if lines:
                return [*lines, [("", "")], *extra_lines]
            return extra_lines
        if lines:
            return lines
        idle = Text()
        idle.append(label("ops_idle_title"), style="#e6a23c")
        idle.append("\n")
        idle.append(label("ops_idle_hint"), style=OPS_MUTED)
        return list(split_lines(to_formatted_text(ANSI(rich_to_ansi(idle, width=width)))))

    def _visible_transcript_lines(
        self, width: int, height: int
    ) -> list[StyleAndTextTuples]:
        """Return exactly the lines that should fill the MAIN viewport."""
        height = max(1, height)
        lines = self._transcript_lines(width)
        total = len(lines)
        with self._lock:
            scroll = self._scroll
            max_scroll = max(0, total - height)
            if scroll > max_scroll:
                scroll = max_scroll
                self._scroll = scroll
            self._center_line_count = total

        if total <= height:
            visible = lines
        else:
            end = total - scroll
            start = max(0, end - height)
            visible = lines[start:end]
            # Keep a one-line scroll cue without hiding the newest line:
            # replace the topmost visible line when scrolled up.
            if scroll > 0 and visible:
                hint = f" ▲ +{scroll}  PgUp/PgDn "
                visible = list(visible)
                visible[0] = [(OPS_MUTED, hint[: max(8, width)])]
        return visible

    def _render_center(self) -> Any:
        """Test/helper: sticky-bottom view using estimated or cached dims."""
        width = self._center_width()
        height = self._center_height()
        visible = self._visible_transcript_lines(width, height)
        out: StyleAndTextTuples = []
        for i, line in enumerate(visible):
            if i:
                out.append(("", "\n"))
            out.extend(line)
        return FormattedText(out)

    def _accept(self, buffer) -> bool:  # noqa: ANN001
        """Handle Enter in the input dock.

        prompt_toolkit order is: ``accept_handler`` → ``append_to_history`` →
        ``reset`` (when handler returns False). We must NOT ``buffer.reset()``
        before returning False, or submitted lines never reach FileHistory.

        Do not call ``layout.focus`` from here — re-entrant focus changes during
        accept corrupt the Win32 cursor (status-line echo, Ctrl+C dead). Blank
        Enter is swallowed earlier by an eager key binding.
        """
        text = buffer.text
        if self._focus == "right" and not self._ask_mode and not text.strip():
            self._auth_action("allow")
            return True
        if self._focus != "input" and text.strip():
            with self._lock:
                self._focus = "input"
            self._sync_frame_styles()

        if self._ask_mode:
            self._ask_value = text
            self._ask_event.set()
            # Drop ask answers from the shared command history.
            buffer.reset()
            return True

        line = text.strip()
        if not line:
            # Belt-and-suspenders if the eager binding did not run.
            with self._lock:
                self._focus = "input"
            self._sync_frame_styles()
            return True

        # Paint the user bubble now, then dispatch after this key event so the
        # empty prompt and MAIN echo share the next frame (Claude/Codex style).
        with self._lock:
            self._scroll = 0
            self._focus = "input"
            self._pending_echo = line
        self._sync_frame_styles()
        self.append_user_message(line)
        self._schedule_on_line(line)
        # False → toolkit appends ``text`` to history, then resets the buffer.
        return False


def run_ops_worker(fn: Callable[[], Any]) -> Future[Any]:
    """Run a turn on a daemon thread; returns a Future."""
    fut: Future[Any] = Future()

    def _target() -> None:
        try:
            fut.set_result(fn())
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)

    threading.Thread(target=_target, name="argusai-ops-turn", daemon=True).start()
    return fut


__all__ = [
    "OpsApp",
    "MonitorMode",
    "rich_to_ansi",
    "run_ops_worker",
]
