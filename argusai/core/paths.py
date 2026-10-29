"""Centralised filesystem layout for the GUI application.

ArgusAI is a desktop GUI app. Runtime paths must not depend on the
process working directory because users launch it from IDEs, terminals,
shortcuts, and packaged binaries. This module owns the stable per-user app
root, settings file location, derived data directories, and per-session
assistant sandboxes.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


SUBDIR_TASKS = "tasks"
SUBDIR_SESSIONS = "assistant_sessions"
SUBDIR_POC_PAGES = "poc_pages"
SUBDIR_KNOWLEDGE = "knowledge"
SUBDIR_REVERSE_JOBS = "reverse_jobs"
SUBDIR_TOOLS = "tools"
SUBDIR_PROXY = "proxy"
SUBDIR_CASES = "cases"
SUBDIR_SESSION_VAULT = "session_vault"
SUBDIR_USER_TODOS = "user_todos"
SUBDIR_ASSISTANT_CRON = "assistant_cron"
SUBDIR_CHANNELS = "channels"
SUBDIR_LOGS = "logs"

# ``logs`` is deliberately NOT part of this tuple: the data_root migration
# transfers these subtrees with a verified copy, and a rotating log file the
# running process still holds open cannot be relocated on Windows.
PERSISTENT_DATA_SUBDIRS: tuple[str, ...] = (
    SUBDIR_TASKS,
    SUBDIR_SESSIONS,
    SUBDIR_POC_PAGES,
    SUBDIR_KNOWLEDGE,
    SUBDIR_REVERSE_JOBS,
    SUBDIR_TOOLS,
    SUBDIR_PROXY,
    SUBDIR_CASES,
    SUBDIR_SESSION_VAULT,
    SUBDIR_USER_TODOS,
    SUBDIR_ASSISTANT_CRON,
    SUBDIR_CHANNELS,
)


def _app_root_for_name(name: str) -> Path:
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / name
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / name.lower()


def legacy_app_root() -> Path:
    """Return the previous default root used before the ArgusAI rename."""
    return _app_root_for_name("Web" + "Audit")


def _migrate_legacy_app_root(new_root: Path) -> None:
    old_root = legacy_app_root()
    if new_root.exists() or not old_root.exists() or old_root == new_root:
        return
    new_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        old_root.rename(new_root)
    except OSError:
        try:
            shutil.copytree(old_root, new_root)
        except OSError:
            return
        shutil.rmtree(old_root, ignore_errors=True)


def default_app_root() -> Path:
    root = _app_root_for_name("ArgusAI")
    _migrate_legacy_app_root(root)
    return root


def is_explicit_absolute(path: str | Path) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    p = Path(text).expanduser()
    return (
        p.is_absolute()
        or bool(re.match(r"^[a-zA-Z]:[\\/]", text))
        or text.startswith(("\\\\", "//"))
    )


def validate_session_id(session_id: str) -> str:
    sid = (session_id or "default").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not sid or len(sid) > 128 or sid in {".", ".."} or any(ch not in allowed for ch in sid):
        raise ValueError("invalid session_id")
    return sid


@dataclass(frozen=True)
class PathManager:
    """Resolved path layout for one ArgusAI process."""

    app_root: Path | None = None
    data_root: Path | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_root", Path(self.app_root or default_app_root()).expanduser())
        data = Path(self.data_root).expanduser() if self.data_root else Path(self.app_root) / "data"
        object.__setattr__(self, "data_root", data)

    @property
    def settings_path(self) -> Path:
        return Path(self.app_root) / "settings.json"

    @property
    def tasks_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_TASKS

    @property
    def sessions_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_SESSIONS

    @property
    def poc_pages_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_POC_PAGES

    @property
    def knowledge_base_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_KNOWLEDGE

    @property
    def reverse_jobs_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_REVERSE_JOBS

    @property
    def tools_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_TOOLS

    @property
    def proxy_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_PROXY

    @property
    def cases_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_CASES

    @property
    def session_vault_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_SESSION_VAULT

    @property
    def user_todos_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_USER_TODOS

    @property
    def assistant_cron_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_ASSISTANT_CRON

    @property
    def channels_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_CHANNELS

    @property
    def logs_dir(self) -> Path:
        return Path(self.data_root) / SUBDIR_LOGS

    @property
    def legacy_logs_dir(self) -> Path:
        """Where logs lived before they were rooted at ``data_root``."""
        return Path(self.app_root) / SUBDIR_LOGS

    def assistant_sandbox(self, session_id: str) -> Path:
        safe = validate_session_id(session_id)
        base = self.sessions_dir.expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        root = (base / f"{safe}.sandbox").resolve()
        if root.parent != base:
            raise ValueError("invalid sandbox path")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def ensure_base_dirs(self) -> None:
        for path in (
            Path(self.app_root),
            Path(self.data_root),
            self.tasks_dir,
            self.sessions_dir,
            self.poc_pages_dir,
            self.knowledge_base_dir,
            self.reverse_jobs_dir,
            self.tools_dir,
            self.proxy_dir,
            self.cases_dir,
            self.session_vault_dir,
            self.user_todos_dir,
            self.assistant_cron_dir,
            self.channels_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

