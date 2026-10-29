"""History-aware ops console prompt with a sticky bottom status bar."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.completion import (
    Completer,
    Completion,
    ThreadedCompleter,
)
from prompt_toolkit.document import Document

from argusai.cli.shell import CLI_COMMAND_COMPLETIONS

_session: Any = None
_toolbar_fn: Callable[[], str] | None = None
_FILE_REFERENCE_RE = re.compile(
    r"""(?<!\S)@(?:"([^"\r\n]*)|'([^'\r\n]*)|([^\s"']*))$"""
)
_COMPLETION_SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
})
_COMPLETION_INDEX_LIMIT = 50_000
_COMPLETION_RESULT_LIMIT = 24
_team_mentions: tuple[str, ...] = ()


def set_team_mentions(names: list[str] | None) -> None:
    """Roles offered by ``@`` while a team room is open. Empty outside a room."""
    global _team_mentions
    cleaned: list[str] = []
    seen: set[str] = set()
    for name in names or []:
        text = str(name or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    _team_mentions = tuple(cleaned)


def _file_reference_query(document: Document) -> tuple[str, int] | None:
    """Return the active @file query and replacement width at the cursor."""
    match = _FILE_REFERENCE_RE.search(document.text_before_cursor)
    if match is None:
        return None
    query = next((part for part in match.groups() if part is not None), "")
    return query, len(match.group(0))


def file_reference_at_cursor(document: Document) -> bool:
    return _file_reference_query(document) is not None


def _slash_completion_query(
    document: Document,
) -> tuple[str, int, tuple[str, ...], bool] | None:
    """Return query, replacement width, candidates, and command-stage flag."""
    before = document.text_before_cursor
    if "\n" in before or "\r" in before:
        return None
    stripped = before.lstrip()
    if not stripped.startswith("/"):
        return None
    if not any(char.isspace() for char in stripped):
        return (
            stripped,
            len(stripped),
            tuple(CLI_COMMAND_COMPLETIONS),
            True,
        )

    # Require a gap after the command, then take the first option token.
    # Trailing spaces after the option must not kill completion (``/think on ``).
    match = re.fullmatch(r"\s*(/\S+)\s+(\S*)(\s*)", before)
    if match is None:
        return None
    command = match.group(1).casefold().split("@", 1)[0]
    choices = CLI_COMMAND_COMPLETIONS.get(command)
    if not choices:
        return None
    query = match.group(2) or ""
    trailing = match.group(3) or ""
    return query, len(query) + len(trailing), choices, False


def input_completion_at_cursor(document: Document) -> bool:
    return (
        _file_reference_query(document) is not None
        or _slash_completion_query(document) is not None
    )


def cancel_input_completion(buffer: Any) -> bool:
    """Close an active completion menu and restore the text before selection."""
    if getattr(buffer, "complete_state", None) is None:
        return False
    buffer.cancel_completion()
    return True


def cli_completion_key_bindings():
    """Completion-only bindings shared by the linear prompt."""
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @Condition
    def _completion_visible() -> bool:
        from prompt_toolkit.application.current import get_app

        return get_app().current_buffer.complete_state is not None

    @bindings.add("escape", filter=_completion_visible, eager=True)
    def _cancel(event) -> None:  # noqa: ANN001
        cancel_input_completion(event.current_buffer)
        event.app.invalidate()

    return bindings


def _compact_fuzzy_text(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _subsequence_gap(needle: str, haystack: str) -> int | None:
    if not needle:
        return 0
    position = -1
    gap = 0
    for char in needle:
        next_position = haystack.find(char, position + 1)
        if next_position < 0:
            return None
        if position >= 0:
            gap += next_position - position - 1
        position = next_position
    return gap + max(0, len(haystack) - len(needle)) // 8


def _file_match_score(query: str, relative_path: str) -> tuple[int, int, int] | None:
    normalized_query = query.casefold().replace("\\", "/").lstrip("./")
    normalized_path = relative_path.casefold().replace("\\", "/")
    basename = normalized_path.rsplit("/", 1)[-1]
    depth = normalized_path.count("/")
    if not normalized_query:
        return 6, depth, len(normalized_path)
    if basename.startswith(normalized_query):
        return 0, depth, len(normalized_path)
    if normalized_query in basename:
        return 1, basename.index(normalized_query), len(normalized_path)
    if normalized_path.startswith(normalized_query):
        return 2, depth, len(normalized_path)
    if normalized_query in normalized_path:
        return 3, normalized_path.index(normalized_query), len(normalized_path)

    compact_query = _compact_fuzzy_text(normalized_query)
    compact_basename = _compact_fuzzy_text(basename)
    compact_path = _compact_fuzzy_text(normalized_path)
    basename_gap = _subsequence_gap(compact_query, compact_basename)
    if basename_gap is not None:
        return 4, basename_gap, len(normalized_path)
    path_gap = _subsequence_gap(compact_query, compact_path)
    if path_gap is not None:
        return 5, path_gap, len(normalized_path)
    return None


class CliFileCompleter(Completer):
    """Fuzzy @file completion rooted at the CLI launch directory."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        cache_seconds: float = 3.0,
    ) -> None:
        self.root = Path(root or Path.cwd()).resolve()
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._indexed_at = 0.0
        self._files: list[Path] = []

    def _index_files(self) -> list[Path]:
        now = time.monotonic()
        if self._indexed_at and now - self._indexed_at < self.cache_seconds:
            return self._files
        files: list[Path] = []
        if self.root.is_dir():
            for current, directories, names in os.walk(
                self.root,
                topdown=True,
                followlinks=False,
            ):
                directories[:] = sorted(
                    directory
                    for directory in directories
                    if directory.casefold() not in _COMPLETION_SKIP_DIRS
                )
                base = Path(current)
                for name in sorted(names):
                    path = base / name
                    try:
                        if not path.is_file():
                            continue
                        files.append(path.relative_to(self.root))
                    except (OSError, ValueError):
                        continue
                    if len(files) >= _COMPLETION_INDEX_LIMIT:
                        break
                if len(files) >= _COMPLETION_INDEX_LIMIT:
                    break
        self._files = files
        self._indexed_at = now
        return files

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        reference = _file_reference_query(document)
        if reference is None:
            return
        query, replace_width = reference
        ranked: list[tuple[tuple[int, int, int], str, Path]] = []
        for relative in self._index_files():
            relative_text = str(relative)
            score = _file_match_score(query, relative_text)
            if score is not None:
                ranked.append((score, relative_text.casefold(), relative))
        ranked.sort(key=lambda item: (item[0], item[1]))

        active_token = document.text_before_cursor[-replace_width:]
        for _, _, relative in ranked[:_COMPLETION_RESULT_LIMIT]:
            relative_text = str(relative)
            replacement = (
                f'@"{relative_text}"'
                if any(char.isspace() for char in relative_text)
                else f"@{relative_text}"
            )
            if replacement == active_token:
                continue
            parent = str(relative.parent)
            yield Completion(
                replacement,
                start_position=-replace_width,
                display=relative.name,
                display_meta=parent if parent != "." else "./",
            )


class CliMentionCompleter(Completer):
    """``@role`` completion for the open team room. Files stay available after these."""

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        if not _team_mentions:
            return
        reference = _file_reference_query(document)
        if reference is None:
            return
        from argusai.cli.locale import label

        query, replace_width = reference
        ranked: list[tuple[tuple[int, int, int], str]] = []
        for name in _team_mentions:
            score = _file_match_score(query, name)
            if score is not None:
                ranked.append((score, name))
        ranked.sort(key=lambda item: (item[0], item[1].casefold()))
        active_token = document.text_before_cursor[-replace_width:]
        meta = label("team_mention_meta")
        for _, name in ranked[:_COMPLETION_RESULT_LIMIT]:
            replacement = f'@"{name}"' if any(char.isspace() for char in name) else f"@{name}"
            if replacement == active_token:
                continue
            yield Completion(
                replacement,
                start_position=-replace_width,
                display=name,
                display_meta=meta,
            )


class CliCommandCompleter(Completer):
    """Fuzzy slash-command and static first-argument completion."""

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        context = _slash_completion_query(document)
        if context is None:
            return
        query, replace_width, candidates, command_stage = context
        ranked: list[tuple[tuple[int, int, int], str]] = []
        for candidate in candidates:
            score = _file_match_score(query, candidate)
            if score is not None:
                ranked.append((score, candidate))
        ranked.sort(key=lambda item: (item[0], item[1]))

        active_token = document.text_before_cursor[-replace_width:] if replace_width else ""
        for _, candidate in ranked:
            replacement = candidate
            if command_stage and CLI_COMMAND_COMPLETIONS.get(candidate) != ():
                replacement += " "
            if not command_stage and candidate == query:
                continue
            if replacement == active_token:
                continue
            yield Completion(
                replacement,
                start_position=-replace_width,
                display=candidate,
                display_meta="command" if command_stage else "option",
            )


class CliInputCompleter(Completer):
    """Compose slash-command and @file completion for all CLI prompts."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.command_completer = CliCommandCompleter()
        self.mention_completer = CliMentionCompleter()
        self.file_completer = CliFileCompleter(root)

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        yield from self.command_completer.get_completions(document, complete_event)
        yield from self.mention_completer.get_completions(document, complete_event)
        yield from self.file_completer.get_completions(document, complete_event)


def build_cli_input_completer(
    root: Path | str | None = None,
) -> ThreadedCompleter:
    return ThreadedCompleter(CliInputCompleter(root))


def _history_file() -> Path:
    try:
        from argusai.core.settings import SettingsManager

        root = Path(SettingsManager().get().data_root)
    except Exception:
        root = Path.home() / ".argusai"
    root.mkdir(parents=True, exist_ok=True)
    return root / "cli_history"


def set_toolbar_provider(fn: Callable[[], str] | None) -> None:
    """Register a zero-arg callback that returns the bottom toolbar text."""
    global _toolbar_fn
    _toolbar_fn = fn


def _ensure_session() -> Any:
    global _session
    if _session is not None:
        return _session
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style

    path = _history_file()
    style = Style.from_dict(
        {
            "ops": "#e6a23c bold",
            "bottom-toolbar": "noreverse bg:#1c1f26 #8b93a7",
            "frame": "#3a3f4b",
        }
    )
    _session = PromptSession(
        history=FileHistory(str(path)),
        style=style,
        enable_history_search=True,
        completer=build_cli_input_completer(Path.cwd()),
        complete_while_typing=True,
        key_bindings=cli_completion_key_bindings(),
    )
    return _session


def _bottom_toolbar() -> Any:
    from prompt_toolkit.formatted_text import FormattedText

    text = ""
    if _toolbar_fn is not None:
        try:
            text = str(_toolbar_fn() or "")
        except Exception:
            text = ""
    if not text:
        text = " ArgusAI ops console "
    return FormattedText([("class:bottom-toolbar", text)])


def ask_ops_line(prompt: str = "ops › ") -> str:
    """Read one line with ↑↓ history and a sticky bottom toolbar."""
    try:
        session = _ensure_session()
        # Visual “input dock”: top rule + prompt; toolbar stays pinned below.
        from prompt_toolkit.formatted_text import FormattedText

        message = FormattedText(
            [
                ("class:frame", "─" * 48 + "\n"),
                ("class:ops", prompt),
            ]
        )
        return str(
            session.prompt(
                message,
                bottom_toolbar=_bottom_toolbar,
            )
        )
    except EOFError:
        raise
    except KeyboardInterrupt:
        raise
    except Exception:
        from rich.prompt import Prompt

        class _OpsPrompt(Prompt):
            prompt_suffix = " "

        return str(_OpsPrompt.ask(f"[prompt]{prompt.rstrip()}[/]"))


def ask_choice_line(prompt: str) -> str:
    """Ask for a choice without polluting the main chat history file heavily."""
    try:
        session = _ensure_session()
        from prompt_toolkit.formatted_text import FormattedText

        return str(
            session.prompt(
                FormattedText([("class:ops", prompt)]),
                bottom_toolbar=_bottom_toolbar,
            )
        ).strip()
    except Exception:
        from rich.prompt import Prompt

        class _OpsPrompt(Prompt):
            prompt_suffix = " "

        return str(_OpsPrompt.ask(f"[amber]{prompt.rstrip()}[/]")).strip()
