"""Terminal rendering for assistant replies and tool traces.

Aider / Codex / Claude Code all split streaming from commit:
live paint is a cheap wrapped tail; fancy Markdown is rendered once
when the segment lands in scrollback.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from argusai.cli.locale import label


def make_markdown(text: str, *, fancy: bool = True) -> Markdown:
    # Commit path only. Live streaming paints plain Text, not Markdown.
    return Markdown(
        text or "…",
        code_theme="monokai" if fancy else "ansi_dark",
        hyperlinks=fancy,
        inline_code_lexer=None,
    )


def print_reply_markdown(console: Console, text: str) -> None:
    """Print one assistant segment as terminal-rendered Markdown."""
    body = (text or "").strip()
    if not body:
        return
    if not console.is_terminal:
        console.print(body)
        return
    try:
        console.print(make_markdown(body))
    except Exception:
        console.print(body)


def print_tool_exec(
    console: Console,
    name: str,
    *,
    detail: str = "",
) -> None:
    """One EXEC line for a tool call (keeps the transcript linear)."""
    key = (name or "tool").strip() or "tool"
    console.print(f"[tool]├─ {label('tag_exec')}[/]  [accent]{key}[/]")
    note = (detail or "").strip()
    if note:
        console.print(f"[hud.dim]  {note[:160]}[/]")


def tool_detail_from_block(block: dict[str, Any]) -> str:
    """Best-effort one-line summary from a tool_* UI block."""
    args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
    if not args:
        args = block.get("args") if isinstance(block.get("args"), dict) else {}
    for key in ("command", "cmd", "path", "url", "query", "code", "message", "prompt"):
        val = args.get(key) if args else block.get(key)
        if val is None:
            continue
        text = str(val).strip().replace("\n", " ")
        if text:
            return f"{key}={text[:120]}"
    summary = str(block.get("summary") or block.get("detail") or "").strip()
    return summary[:160]


class ReplyStream:
    """Stream plain text into a transient Live region, then commit Markdown.

    Mirrors Aider's live window + Codex's "don't paint partial Markdown":
    incomplete ``**`` / fences stay literal until flush.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self._parts: list[str] = []
        self._live: Live | None = None
        self._header_printed = False
        self._last_paint = 0.0
        self._min_interval = 0.08

    @property
    def pending(self) -> bool:
        return bool(self._parts) or self._live is not None

    def feed(self, piece: str) -> None:
        if not piece:
            return
        self._parts.append(piece)
        if not self.console.is_terminal:
            return
        now = time.monotonic()
        renderable = self._renderable()
        if self._live is None:
            self._print_header()
            try:
                self._live = Live(
                    renderable,
                    console=self.console,
                    refresh_per_second=12,
                    transient=True,
                    vertical_overflow="visible",
                )
                self._live.start()
                self._last_paint = now
            except Exception:
                self._live = None
            return
        if now - self._last_paint < self._min_interval:
            return
        started = time.monotonic()
        try:
            self._live.update(renderable)
        except Exception:
            pass
        elapsed = time.monotonic() - started
        self._min_interval = min(max(elapsed * 8.0, 0.08), 0.4)
        self._last_paint = now

    def flush(self) -> str:
        """Commit the current segment; return the plain text body."""
        body = "".join(self._parts).rstrip()
        self._parts.clear()
        live = self._live
        self._live = None
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass
        if body:
            if not self._header_printed:
                self._print_header()
            print_reply_markdown(self.console, body)
            self.console.print()
        self._header_printed = False
        return body

    def discard(self) -> None:
        """Drop buffer without printing (e.g. run reset)."""
        self._parts.clear()
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        self._header_printed = False

    def _print_header(self) -> None:
        if self._header_printed:
            return
        self.console.print("[reply]╭─ ARGUSAI[/]")
        self._header_printed = True

    def _renderable(self) -> Any:
        text = "".join(self._parts)
        if not text.strip():
            return Text("…", style="bright_black")
        return Text(text)
