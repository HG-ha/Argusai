"""Whitespace-tolerant slash-command tokenization.

CLI and channel gateways share this so ``/think  on``, ``/think [on]``, and
``/think@Bot on`` all resolve the same way. Keep this module free of the
assistant/runtime import graph — ``argusai.cli.shell`` loads it at import time.
"""

from __future__ import annotations

# IME / rich-text leftovers that look like gaps but are not ``str.split()`` whitespace.
INVISIBLE_SPACE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
TOGGLE_VALUES = {
    "on": "on",
    "off": "off",
    "status": "status",
    "开": "on",
    "开启": "on",
    "关": "off",
    "关闭": "off",
    "状态": "status",
}

# ``/think high`` and IME-glued ``/think高``. Keep this table here so slash
# parsing stays free of the assistant/runtime import graph.
THINK_ACTIONS = {
    **TOGGLE_VALUES,
    "auto": "auto",
    "none": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "自动": "auto",
    "默认": "auto",
    "无": "none",
    "无推理": "none",
    "最低": "minimal",
    "低": "low",
    "中": "medium",
    "高": "high",
    "更高": "xhigh",
    "极高": "xhigh",
    "最大": "max",
}
THINK_EFFORT_ACTIONS = frozenset({
    "auto", "none", "minimal", "low", "medium", "high", "xhigh", "max",
})


def split_slash_command(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Split ``/cmd[@bot]   arg`` on any Unicode whitespace.

    Telegram ``/think@Bot on``, copied help ``/think [on]``, and doubled
    spaces all collapse to the same ``('/think', ('on',))``.
    """
    raw = str(text or "").translate(INVISIBLE_SPACE).strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    if not parts:
        return None
    cmd = parts[0].split("@", 1)[0].rstrip(":").lower()
    args = tuple(
        token
        for token in (part.strip("[]()<>") for part in parts[1:])
        if token
    )
    return cmd, args


def match_slash_names(
    text: str,
    names: tuple[str, ...],
    *,
    glue_toggle: bool = False,
    glue_values: dict[str, str] | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(command, args)`` when ``text`` is one of ``names``.

    ``glue_toggle`` also accepts IME-eaten spaces: ``/thinkon`` → ``/think`` + ``on``.
    """
    parsed = split_slash_command(text)
    if parsed is None:
        return None
    cmd, args = parsed
    if cmd in names:
        return cmd, args
    if not glue_toggle:
        return None
    values = glue_values if glue_values is not None else TOGGLE_VALUES
    for name in sorted(names, key=len, reverse=True):
        if not cmd.startswith(name) or cmd == name:
            continue
        suffix = cmd[len(name):]
        action = values.get(suffix.casefold()) or values.get(suffix)
        if action:
            return name, (action, *args)
    return None


def toggle_action(
    args: tuple[str, ...],
    *,
    allow_extra: bool = False,
) -> tuple[str, str] | None:
    """Map toggle args to ``(on|off|status, rest)``. ``None`` means invalid."""
    if not args:
        return "status", ""
    token = args[0]
    action = TOGGLE_VALUES.get(token.casefold()) or TOGGLE_VALUES.get(token)
    if action is None:
        return None
    rest = " ".join(args[1:]).strip()
    if rest and not allow_extra:
        return None
    return action, rest


def think_action(args: tuple[str, ...]) -> str | None:
    """Map ``/think`` args to on/off/status or a canonical effort level."""
    if not args:
        return "status"
    if len(args) > 1:
        return None
    token = args[0]
    return THINK_ACTIONS.get(token.casefold()) or THINK_ACTIONS.get(token)
