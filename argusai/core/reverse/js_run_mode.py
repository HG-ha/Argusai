"""Heuristics for ``run_node_script`` / ``run_js`` mode selection.

The managed Node runner's ``eval`` mode wraps source as ``(source)`` and only
accepts a single JavaScript *expression*. Models often pass statement
snippets (``var``, ``require``, ``console.log``) with ``mode=eval``,
which fails at parse time with ``Unexpected token`` in ``expr.js``.
"""

from __future__ import annotations

import re
from typing import Any

_STATEMENT_MARKERS = re.compile(
    r"(?:^|[;\n])\s*(?:var|let|const|function|async\s+function|class|import|export|"
    r"for|while|if|try|switch|do|throw|return)\b"
    r"|require\s*\("
    r"|console\.(?:log|error|warn|info|debug)\s*\(",
    re.MULTILINE,
)

_MODE_COERCE_NOTICE = (
    "mode 已从 eval 自动改为 script：检测到语句或多行代码。"
    " eval 只接受单个表达式；含 var/require/function/console.log 等请用 script，"
    "需要返回值时设置 globalThis.__result = <value>。"
)

_RETRY_NOTICE = (
    "mode 已从 eval 自动改为 script 并重试：eval 解析失败（Unexpected token / expr.js）。"
    " 请默认使用 mode=script，eval 仅用于单行表达式。"
)


def coerce_js_run_mode(source: str, mode: str) -> tuple[str, str | None]:
    """Return ``(effective_mode, notice)``; notice is set when eval → script."""
    requested = (str(mode or "script").strip().lower() or "script")
    if requested != "eval":
        return requested, None
    text = str(source or "")
    if not text.strip():
        return requested, None
    normalized = text.replace("\r\n", "\n")
    if "\n" in normalized.strip():
        return "script", _MODE_COERCE_NOTICE
    if _STATEMENT_MARKERS.search(normalized):
        return "script", _MODE_COERCE_NOTICE
    return requested, None


def should_retry_eval_as_script(result: dict[str, Any]) -> bool:
    """True when an eval run failed with the classic expression-parse error."""
    if result.get("ok"):
        return False
    err = f"{result.get('error') or ''}\n{result.get('stack') or ''}"
    return "expr.js" in err or "Unexpected token" in err


def retry_eval_notice() -> str:
    return _RETRY_NOTICE
