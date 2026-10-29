"""Shared helpers for the tool executor package.

These are the small utilities every sibling tool module imports via
``from .common import *``. The whole point of this module is to give
``sandbox_code`` / ``web_static`` / ``browser_*`` / ``kb_tasks`` / ...
one shared definition each of:

- value coercion (``_safe_int``),
- read-only setting peeks (``_browser_defaults``),
- pure transforms (``_sort_findings``),
- the sandbox path resolvers — every tool that touches files needs to
  enforce the session sandbox boundary the same way, so the resolvers
  live here, not inside any one tool module.

Anything tool-specific (e.g. ``run_python_script``) belongs in its own
sibling module, NOT here.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any


def _extract_port(endpoint: str) -> int:
    """Best-effort parse for the port out of an ``http://host:port`` URL."""
    s = (endpoint or "").rstrip("/")
    try:
        return int(s.rsplit(":", 1)[-1])
    except (ValueError, IndexError):
        return 9222


def _browser_defaults(api: Any) -> tuple[str, str]:
    """Read the user-configured default browser channel / executable_path.

    Tool callers may override these by passing explicit values; otherwise
    we fall through to the resolver so a Chrome / Edge install on the
    user's box wins over the Playwright bundle.
    """
    try:
        cfg = (api.get_settings() or {}).get("browser") or {}
    except Exception:
        cfg = {}
    return str(cfg.get("channel") or "").strip(), str(cfg.get("executable_path") or "").strip()


def _sort_findings(findings: list[dict], key: str = "severity") -> list[dict]:
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    return sorted(findings, key=lambda f: order.get(f.get(key, "info"), 0), reverse=True)


def _safe_int(value: Any, default: int, minimum: int = 1, maximum: int = 500) -> int:
    try:
        if isinstance(value, str):
            value = value.strip()
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(minimum, min(maximum, n))


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "y"}:
            return True
        if text in {"0", "false", "no", "off", "n", ""}:
            return False
    return default


# ---------------------------------------------------------------------------
# Session-sandbox path resolvers (shared by every tool that takes a path).
# ---------------------------------------------------------------------------


def _normalize_path_text(raw: str) -> str:
    """Expand ``~`` and Windows ``%ENV%`` tokens before path classification."""
    text = str(raw or "").strip()
    if not text:
        return text
    return os.path.expandvars(os.path.expanduser(text))


def _session_sandbox_root(api: Any, kwargs: dict[str, Any]):
    from argusai.core.paths import PathManager

    session_id = str(kwargs.get("_session_id") or "default")
    settings = api.settings_mgr.get()
    return PathManager(data_root=settings.data_root).assistant_sandbox(session_id)


def _resolve_session_output_path(api: Any, kwargs: dict[str, Any], raw_path: str, default_name: str = ""):
    """Resolve AI-generated output paths without falling back to ``Path.cwd()``.

    Absolute paths are treated as an explicit user choice and are allowed.
    Relative paths are always scoped to the current assistant session sandbox
    so tool outputs do not appear in whichever directory launched the GUI.
    """
    root = Path(_session_sandbox_root(api, kwargs)).resolve()
    text = _normalize_path_text(raw_path)
    if not text:
        if not default_name:
            raise ValueError("path is required")
        return (root / default_name).resolve(), "session_sandbox", str(root)

    candidate = Path(text)
    is_absolute = (
        candidate.is_absolute()
        or bool(re.match(r"^[a-zA-Z]:[\\/]", text))
        or text.startswith(("\\\\", "//"))
    )
    if is_absolute:
        return candidate.resolve(), "absolute_user_path", ""

    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("relative output path escapes session sandbox")
    return resolved, "session_sandbox", str(root)


def _resolve_session_input_path(api: Any, kwargs: dict[str, Any], raw_path: str):
    """Resolve AI tool input paths; relative paths mean session sandbox paths."""
    text = _normalize_path_text(raw_path)
    if not text:
        raise ValueError("path is required")
    candidate = Path(text)
    is_absolute = (
        candidate.is_absolute()
        or bool(re.match(r"^[a-zA-Z]:[\\/]", text))
        or text.startswith(("\\\\", "//"))
    )
    if is_absolute:
        return candidate.resolve()
    root = Path(_session_sandbox_root(api, kwargs)).resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("relative input path escapes session sandbox")
    return resolved


def _session_project_dir(api: Any, kwargs: dict[str, Any] | None = None):
    """Absolute project directory bound to this assistant session, if any."""
    payload = kwargs if isinstance(kwargs, dict) else {}
    session = payload.get("_session")
    if session is None and api is not None:
        sid = str(payload.get("_session_id") or "default")
        try:
            session = (getattr(api, "_assistant_sessions", None) or {}).get(sid)
        except Exception:
            session = None
    binding = getattr(session, "runtime_binding", None) if session is not None else None
    if binding is None:
        return None
    if str(getattr(binding, "workspace_mode", "") or "") != "project":
        return None
    text = str(getattr(binding, "project_dir", "") or "").strip()
    if not text:
        return None
    try:
        root = Path(text).expanduser().resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


__all__ = [
    "_browser_defaults",
    "_sort_findings",
    "_safe_int",
    "_safe_bool",
    "_normalize_path_text",
    "_session_sandbox_root",
    "_session_project_dir",
    "_resolve_session_output_path",
    "_resolve_session_input_path",
]
