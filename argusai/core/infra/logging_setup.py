"""Centralised logging configuration.

The application is a packaged desktop GUI: most users launch it from a
shortcut and never see a console. We therefore need to make sure every
diagnostic line lands on disk under the configured storage root so that
when a self-loop / tool-dispatch storm happens the user can zip the
``logs/`` folder and send it back to us.

The log directory is ``<data_root>/logs`` (``paths.SUBDIR_LOGS``), i.e. it
follows the storage root the user picked in Settings → Storage exactly like
``tasks/`` and ``knowledge/`` do. An explicit ``logging.log_dir`` setting or
``ARGUSAI_LOG_DIR`` still wins. Installs that predate this layout kept their
logs at ``<app_root>/logs``; :func:`init_logging` adopts those files once so
the history is not orphaned.

Public entry points:

* :func:`init_logging` — call from every process entry (GUI ``launch``,
  ``python -m argusai`` etc.). Idempotent. Reads optional environment
  variable overrides and the persisted ``LoggingSettings`` from
  ``settings.json``. Routes records to:

  * a rotating ``logs/argusai.log`` (5 MiB × N, root-level INFO+).
  * a dedicated ``logs/assistant_loop.log`` (5 MiB × N, DEBUG, focused on
    ``argusai.assistant`` + ``argusai.ai`` + ``argusai.session_sandbox``)
    — this is what we tail when chasing self-loop / repetition bugs.
  * ``stderr`` (only when running inside a real TTY / dev mode).

* :func:`apply_logging_settings` — re-applies live changes from the
  webui settings panel (level, retention, payload redaction) without
  restarting the process.

* :func:`session_log_context` — context manager that pins a
  ``session_id`` onto the current task / thread so the assistant loop
  log lines can be filtered per conversation.

* :func:`redact_text` — defensive secret scrubber used both by the
  formatter (default mode) and on demand by callers that want to log
  message bodies safely.
"""

from __future__ import annotations

import contextvars
import io
import logging
import logging.handlers
import os
import re
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _coerce_level(value: Any, default: str = "INFO") -> str:
    text = str(value or "").strip().upper()
    return text if text in _VALID_LEVELS else default


def _coerce_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        if value is None or value == "":
            return int(default)
        n = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(int(lo), min(int(hi), n))


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class LoggingSettings:
    """User-tunable knobs that drive :func:`init_logging`.

    ``level``               root level (default INFO).
    ``assistant_level``     level for assistant + ai + sandbox loggers
                            (defaults to DEBUG so self-loop forensics are
                            always captured).
    ``backup_count``        rotation depth for ``argusai.log`` and
                            ``assistant_loop.log`` (5 MiB each).
    ``max_bytes``           rotate-after size, in bytes. Same value applies
                            to both files.
    ``redact_payloads``     when True (default), formatter scrubs API
                            keys / cookies / authorization tokens from
                            every record before it is written. When False
                            the scrubbing is skipped — only use this on
                            isolated dev machines.
    ``log_dir``             optional explicit override of the log
                            directory (defaults to ``<data_root>/logs``).
    """

    level: str = "INFO"
    assistant_level: str = "DEBUG"
    backup_count: int = 10
    max_bytes: int = 5 * 1024 * 1024
    redact_payloads: bool = True
    log_dir: str = ""

    def normalised(self) -> "LoggingSettings":
        return LoggingSettings(
            level=_coerce_level(self.level, "INFO"),
            assistant_level=_coerce_level(self.assistant_level, "DEBUG"),
            backup_count=_coerce_int(self.backup_count, default=10, lo=1, hi=200),
            max_bytes=_coerce_int(self.max_bytes, default=5 * 1024 * 1024, lo=64 * 1024, hi=256 * 1024 * 1024),
            redact_payloads=bool(self.redact_payloads),
            log_dir=str(self.log_dir or "").strip(),
        )

    @classmethod
    def from_dict(cls, raw: Any) -> "LoggingSettings":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            level=_coerce_level(raw.get("level"), "INFO"),
            assistant_level=_coerce_level(raw.get("assistant_level"), "DEBUG"),
            backup_count=_coerce_int(raw.get("backup_count"), default=10, lo=1, hi=200),
            max_bytes=_coerce_int(raw.get("max_bytes"), default=5 * 1024 * 1024, lo=64 * 1024, hi=256 * 1024 * 1024),
            redact_payloads=_coerce_bool(raw.get("redact_payloads"), True),
            log_dir=str(raw.get("log_dir") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "assistant_level": self.assistant_level,
            "backup_count": int(self.backup_count),
            "max_bytes": int(self.max_bytes),
            "redact_payloads": bool(self.redact_payloads),
            "log_dir": self.log_dir,
        }


# ---------------------------------------------------------------------------
# Session contextvar (lets log lines carry the active conversation id)
# ---------------------------------------------------------------------------


_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "argusai_session_id",
    default="-",
)


@contextmanager
def session_log_context(session_id: str) -> Iterator[None]:
    """Pin ``session_id`` onto the current async task / thread.

    The :class:`_ContextFilter` reads this value and stitches it onto
    every record produced inside the block, so the assistant_loop log
    can be filtered per conversation later.
    """

    token = _session_id_var.set(str(session_id or "-").strip() or "-")
    try:
        yield
    finally:
        _session_id_var.reset(token)


def set_session_log_id(session_id: str) -> None:
    """Permanently override the contextvar (sync entry points)."""

    _session_id_var.set(str(session_id or "-").strip() or "-")


class _ContextFilter(logging.Filter):
    """Inject ``session_id`` into every record so the formatter can show it."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        try:
            record.session_id = _session_id_var.get()
        except LookupError:
            record.session_id = "-"
        return True


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


# We deliberately keep this short: it must run on every formatted line, so the
# patterns need to be cheap. We focus on the highest-impact leaks (API keys,
# session cookies, Authorization headers). For deeper scrubbing callers should
# pre-process payloads via :func:`redact_text` themselves before logging.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,\"']+)"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(api[-_]?key\s*[:=]\s*)([^\s,\"']+)"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)([^\s,]+)"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(set-cookie\s*[:=]\s*)([^\s,]+)"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(\"(?:authorization|api_key|access_token|secret|password|cookie)\"\s*:\s*\")([^\"]+)(\")"), r"\1***REDACTED***\3"),
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"), r"\1***REDACTED***"),
    (re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"), r"***REDACTED-OPENAI-KEY***"),
    (re.compile(r"(sk-or-[A-Za-z0-9_\-]{8,})"), r"***REDACTED-OR-KEY***"),
)


def redact_text(text: str, *, max_len: int | None = 200) -> str:
    """Best-effort scrubber for ad-hoc payload strings.

    The formatter calls this whenever ``redact_payloads`` is on, but it
    is also safe to call directly when a caller wants to drop a value
    into a log message without going through the formatter (e.g. when
    interpolating before the logger sees it).
    """

    if not text:
        return ""
    out = text
    for pattern, repl in _REDACTION_PATTERNS:
        out = pattern.sub(repl, out)
    if max_len is not None and max_len > 0 and len(out) > max_len:
        out = f"{out[:max_len]}…<truncated {len(out) - max_len} chars>"
    return out


class _RedactingFormatter(logging.Formatter):
    """Formatter that optionally runs every line through :func:`redact_text`.

    We only truncate when the line is unreasonably long (>4 KiB). The
    point of file logs is to be able to read them later; aggressive
    truncation in the formatter would defeat the purpose. Callers that
    want hard truncation should call :func:`redact_text` themselves.
    """

    def __init__(self, fmt: str, datefmt: str | None = None, *, redact: bool = True) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "session_id"):
            record.session_id = "-"
        text = super().format(record)
        if not self._redact:
            return text
        # Skip per-pattern scrubbing on small lines that obviously cannot
        # contain a secret to keep the hot path cheap.
        if len(text) <= 16:
            return text
        try:
            return redact_text(text, max_len=4096)
        except Exception:
            return text


# ---------------------------------------------------------------------------
# Buffer that captures records emitted before init_logging() ran
# ---------------------------------------------------------------------------


class _BootBufferHandler(logging.Handler):
    """Stash records in-memory until file handlers are wired up.

    Without this, anything logged during ``import`` (e.g. while building
    the settings manager) would be lost because the root logger has no
    handler yet. Once :func:`init_logging` runs we flush the buffer to
    the real handlers and remove ourselves.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            # Cap the buffer so a runaway debug call before init can't
            # exhaust RAM.
            if len(self._records) >= 2000:
                self._records.pop(0)
            self._records.append(record)

    def drain(self) -> list[logging.LogRecord]:
        with self._lock:
            records, self._records = self._records, []
        return records


_boot_buffer: _BootBufferHandler | None = None
_init_lock = threading.Lock()
_initialised = False
_current_settings: LoggingSettings | None = None
_log_dir_in_use: Path | None = None
_managed_logger_names: tuple[str, ...] = (
    "argusai",
    "argusai.assistant",
    "argusai.ai",
    "argusai.session_sandbox",
    "argusai.runtime_manager",
    "argusai.command",
    "argusai.gui",
    "argusai.knowledge",
    "argusai.tasks",
    "argusai.settings",
    "argusai.weak_credentials",
)


def _ensure_boot_buffer() -> None:
    """Install the boot buffer the first time any module imports us."""

    global _boot_buffer
    if _boot_buffer is not None:
        return
    root = logging.getLogger()
    # If someone already configured logging (e.g. a vendored library) we
    # still want our buffer attached so we don't miss subsequent records.
    _boot_buffer = _BootBufferHandler()
    root.addHandler(_boot_buffer)
    if root.level == logging.WARNING or root.level == 0:
        # Defaults the root level wide enough that the buffer can see
        # everything; init_logging() will tighten this later.
        root.setLevel(logging.DEBUG)


_ensure_boot_buffer()


# ---------------------------------------------------------------------------
# init_logging
# ---------------------------------------------------------------------------


def _persisted_data_root() -> str:
    """Read ``data_root`` straight out of settings.json.

    Deliberately a raw JSON read instead of ``SettingsManager``: the settings
    module imports this one, and logging must stay usable during bootstrap
    even when settings parsing would fail.
    """

    import json

    from argusai.core.paths import PathManager

    try:
        raw = json.loads(PathManager().settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("data_root") or "").strip()


def resolve_log_dir(
    settings: LoggingSettings | None = None,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Where log files belong, given the settings and the storage root."""

    effective = settings or LoggingSettings()
    explicit = (effective.log_dir or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    env_dir = os.environ.get("ARGUSAI_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    # Lazy import — paths depends on nothing else we touch here.
    from argusai.core.paths import PathManager

    root = str(data_root or "").strip() or _persisted_data_root()
    return PathManager(data_root=root or None).logs_dir


def _is_derived_log_dir(settings: LoggingSettings) -> bool:
    return not (settings.log_dir or "").strip() and not os.environ.get("ARGUSAI_LOG_DIR", "").strip()


def adopt_legacy_log_files(target: Path) -> list[str]:
    """Move pre-``data_root`` logs from ``<app_root>/logs`` into ``target``.

    Only runs while ``target`` holds no log files of its own, so it is a
    one-shot upgrade step rather than a merge. Files another process still
    holds open are left where they are — the caller keeps reporting the
    active directory either way.
    """

    import shutil

    from argusai.core.paths import PathManager

    legacy = PathManager().legacy_logs_dir
    if not legacy.is_dir() or os.path.normcase(str(legacy)) == os.path.normcase(str(target)):
        return []
    try:
        if any(target.glob("*.log*")):
            return []
        sources = sorted(legacy.glob("*.log*"))
    except OSError:
        return []
    moved: list[str] = []
    for source in sources:
        if not source.is_file():
            continue
        try:
            shutil.move(str(source), str(target / source.name))
        except (OSError, shutil.Error):
            continue
        moved.append(source.name)
    return moved


def pending_legacy_log_files(active: Path | None = None) -> list[str]:
    """Names of log files still sitting at the pre-``data_root`` location."""

    from argusai.core.paths import PathManager

    legacy = PathManager().legacy_logs_dir
    target = active if active is not None else current_log_dir()
    if not legacy.is_dir() or os.path.normcase(str(legacy)) == os.path.normcase(str(target)):
        return []
    try:
        return sorted(entry.name for entry in legacy.glob("*.log*") if entry.is_file())
    except OSError:
        return []


def _env_overrides(settings: LoggingSettings) -> LoggingSettings:
    """Honor ARGUSAI_LOG_LEVEL / ARGUSAI_DEBUG_LOG without losing settings."""

    overridden = LoggingSettings(
        level=settings.level,
        assistant_level=settings.assistant_level,
        backup_count=settings.backup_count,
        max_bytes=settings.max_bytes,
        redact_payloads=settings.redact_payloads,
        log_dir=settings.log_dir,
    )
    env_level = os.environ.get("ARGUSAI_LOG_LEVEL", "").strip().upper()
    if env_level in _VALID_LEVELS:
        overridden.level = env_level
    env_assistant_level = os.environ.get("ARGUSAI_ASSISTANT_LOG_LEVEL", "").strip().upper()
    if env_assistant_level in _VALID_LEVELS:
        overridden.assistant_level = env_assistant_level
    return overridden.normalised()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _console_logging_enabled() -> bool:
    """Whether log records should also mirror to stderr.

    CLI interactive hosts set ``ARGUSAI_LOG_CONSOLE=0`` so the Agent REPL
    stays clean; file logs under ``logs/`` are unaffected. Explicit
    ``1``/``true`` re-enables the mirror (``argusai --verbose …``).
    """
    raw = os.environ.get("ARGUSAI_LOG_CONSOLE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stream_handler_for_stderr() -> logging.StreamHandler | None:
    """Return a StreamHandler bound to a real stderr, or None if missing.

    Frozen Nuitka onefile builds may run with ``sys.stderr is None`` (no
    console). We must not blow up trying to call ``.write`` in that case.
    """
    if not _console_logging_enabled():
        return None

    stream: Any = sys.stderr
    if stream is None:
        return None
    if not hasattr(stream, "write"):
        return None
    try:
        if isinstance(stream, io.IOBase) and stream.closed:
            return None
    except Exception:
        pass
    return logging.StreamHandler(stream)


def _build_handlers(
    settings: LoggingSettings,
    log_dir: Path,
) -> tuple[logging.Handler, logging.Handler, logging.Handler | None]:
    fmt = (
        "%(asctime)s | %(process)d | %(threadName)s | sid=%(session_id)s | "
        "%(name)s | %(levelname)s | %(message)s"
    )
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = _RedactingFormatter(fmt=fmt, datefmt=datefmt, redact=settings.redact_payloads)

    main_file = log_dir / "argusai.log"
    main_handler = logging.handlers.RotatingFileHandler(
        str(main_file),
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
        delay=True,
    )
    main_handler.setLevel(getattr(logging, settings.level))
    main_handler.setFormatter(formatter)
    main_handler.addFilter(_ContextFilter())

    loop_file = log_dir / "assistant_loop.log"
    loop_handler = logging.handlers.RotatingFileHandler(
        str(loop_file),
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
        delay=True,
    )
    loop_handler.setLevel(getattr(logging, settings.assistant_level))
    loop_handler.setFormatter(formatter)
    loop_handler.addFilter(_ContextFilter())
    loop_handler.addFilter(_AssistantLoopFilter())

    stderr_handler = _stream_handler_for_stderr()
    if stderr_handler is not None:
        stderr_handler.setLevel(getattr(logging, settings.level))
        stderr_handler.setFormatter(formatter)
        stderr_handler.addFilter(_ContextFilter())

    return main_handler, loop_handler, stderr_handler


class _AssistantLoopFilter(logging.Filter):
    """Restrict ``assistant_loop.log`` to the relevant subsystem loggers."""

    _PREFIXES = (
        "argusai.assistant",
        "argusai.ai",
        "argusai.session_sandbox",
        "argusai.command",
        "argusai.weak_credentials",
        "argusai.runtime_manager",
    )

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        name = record.name or ""
        return any(name == p or name.startswith(p + ".") for p in self._PREFIXES)


def _detach_managed_handlers(logger: logging.Logger) -> None:
    """Strip handlers our previous init call attached, leave others alone."""

    for handler in list(logger.handlers):
        if getattr(handler, "_argusai_managed", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _mark_managed(handler: logging.Handler) -> logging.Handler:
    handler._argusai_managed = True  # type: ignore[attr-defined]
    return handler


def init_logging(
    settings: LoggingSettings | None = None,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Configure handlers, replay buffered records, return the log dir.

    Safe to call multiple times. Subsequent calls reconfigure handlers
    in place — this is how the settings panel propagates live changes and
    how a ``data_root`` relocation redirects subsequent writes.
    """

    global _initialised, _current_settings, _log_dir_in_use, _boot_buffer

    effective = (settings or LoggingSettings()).normalised()
    effective = _env_overrides(effective)

    with _init_lock:
        log_dir = _ensure_dir(resolve_log_dir(effective, data_root=data_root))
        adopted = adopt_legacy_log_files(log_dir) if _is_derived_log_dir(effective) else []
        main_handler, loop_handler, stderr_handler = _build_handlers(effective, log_dir)
        _mark_managed(main_handler)
        _mark_managed(loop_handler)
        if stderr_handler is not None:
            _mark_managed(stderr_handler)

        root = logging.getLogger()
        # Detach our previously installed handlers before adding the new ones.
        _detach_managed_handlers(root)
        for name in _managed_logger_names:
            _detach_managed_handlers(logging.getLogger(name))

        # Drain the boot buffer into the freshly built handlers so the
        # records that arrived during import are not lost.
        if _boot_buffer is not None:
            buffered = _boot_buffer.drain()
            for record in buffered:
                if record.levelno >= main_handler.level:
                    main_handler.handle(record)
                if record.levelno >= loop_handler.level:
                    loop_handler.handle(record)
                if stderr_handler is not None and record.levelno >= stderr_handler.level:
                    stderr_handler.handle(record)
            try:
                root.removeHandler(_boot_buffer)
            except Exception:
                pass
            _boot_buffer = None

        root.setLevel(getattr(logging, effective.level))
        root.addHandler(main_handler)
        root.addHandler(loop_handler)
        if stderr_handler is not None:
            root.addHandler(stderr_handler)

        # Make sure focused loggers honour the deeper assistant level
        # regardless of how the root is configured.
        deep_level = getattr(logging, effective.assistant_level)
        for name in (
            "argusai.assistant",
            "argusai.ai",
            "argusai.session_sandbox",
            "argusai.command",
            "argusai.weak_credentials",
            "argusai.runtime_manager",
        ):
            sub = logging.getLogger(name)
            sub.setLevel(min(sub.level or deep_level, deep_level) if sub.level else deep_level)
            sub.propagate = True

        # Quiet down noisy third-party loggers we don't usually care about.
        for name in ("urllib3", "httpx", "httpcore", "asyncio", "PIL"):
            logging.getLogger(name).setLevel(max(getattr(logging, effective.level), logging.WARNING))

        _initialised = True
        _current_settings = effective
        _log_dir_in_use = log_dir

        rotate_text = (
            f"{effective.max_bytes // (1024 * 1024)}MiB"
            if effective.max_bytes >= 1024 * 1024
            else f"{max(1, effective.max_bytes // 1024)}KiB"
        )
        logging.getLogger("argusai").info(
            "logging.init log_dir=%s level=%s assistant_level=%s redact=%s rotate=%sx%d",
            log_dir,
            effective.level,
            effective.assistant_level,
            effective.redact_payloads,
            rotate_text,
            effective.backup_count,
        )
        if adopted:
            logging.getLogger("argusai").info(
                "logging.init adopted %d legacy log file(s) into %s",
                len(adopted),
                log_dir,
            )

        return log_dir


def apply_logging_settings(
    settings: LoggingSettings,
    *,
    data_root: str | Path | None = None,
) -> Path:
    """Re-run :func:`init_logging` with the latest persisted settings."""

    return init_logging(settings, data_root=data_root)


def silence_console_logging() -> None:
    """Detach stderr/stdout StreamHandlers so CLI chat is not polluted.

    File handlers keep writing. Safe to call repeatedly. Prefer setting
    ``ARGUSAI_LOG_CONSOLE=0`` *before* :func:`init_logging` when possible.
    """
    os.environ["ARGUSAI_LOG_CONSOLE"] = "0"
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not isinstance(handler, logging.StreamHandler):
            continue
        if isinstance(handler, logging.FileHandler):
            continue
        stream = getattr(handler, "stream", None)
        if stream is not None and stream not in {sys.stderr, sys.stdout}:
            continue
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def current_log_dir() -> Path:
    """Best-effort location of the active log directory."""

    if _log_dir_in_use is not None:
        return _log_dir_in_use
    return resolve_log_dir()


def current_logging_settings() -> LoggingSettings:
    return (_current_settings or LoggingSettings()).normalised()


def is_initialised() -> bool:
    return _initialised


__all__ = [
    "LoggingSettings",
    "adopt_legacy_log_files",
    "apply_logging_settings",
    "current_log_dir",
    "current_logging_settings",
    "init_logging",
    "is_initialised",
    "pending_legacy_log_files",
    "redact_text",
    "resolve_log_dir",
    "session_log_context",
    "set_session_log_id",
]
