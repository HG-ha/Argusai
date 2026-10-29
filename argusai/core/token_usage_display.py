"""Shared token/cache formatting for CLI monitor rail and ``/status`` rows."""

from __future__ import annotations

from typing import Any, Callable


def safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def context_usage_parts(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Current context fill vs the active model's context window."""
    window = safe_nonnegative_int(payload.get("context_window"))
    if window <= 0:
        return None
    used = safe_nonnegative_int(
        payload.get("prompt_tokens")
        or payload.get("last_prompt_tokens")
        or payload.get("last_prompt_tokens_est")
    )
    return {
        "used": used,
        "window": window,
        "bytes": safe_nonnegative_int(
            payload.get("prompt_bytes") or payload.get("last_prompt_bytes")
        ),
        "percent": min(100.0, 100.0 * used / window),
    }


def context_usage_status_params(payload: dict[str, Any]) -> dict[str, str] | None:
    parts = context_usage_parts(payload)
    if parts is None:
        return None
    return {
        "percent": f"{parts['percent']:.0f}%",
        "used": fmt_tokens(int(parts["used"])),
        "window": fmt_tokens(int(parts["window"])),
    }


def token_usage_parts(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize token/cache counters; return ``None`` when everything is zero."""
    raw = payload.get("token_usage")
    usage = raw if isinstance(raw, dict) else payload
    calls = safe_nonnegative_int(usage.get("calls"))
    input_tokens = safe_nonnegative_int(usage.get("input_tokens"))
    output_tokens = safe_nonnegative_int(usage.get("output_tokens"))
    hit_tokens = safe_nonnegative_int(usage.get("cache_hit_tokens"))
    miss_tokens = safe_nonnegative_int(usage.get("cache_miss_tokens"))
    write_tokens = safe_nonnegative_int(usage.get("cache_write_tokens"))
    if not any((calls, input_tokens, output_tokens, hit_tokens, miss_tokens, write_tokens)):
        return None

    rate = payload.get("cache_hit_rate")
    try:
        hit_rate = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        hit_rate = None
    if hit_rate is None and hit_tokens + miss_tokens > 0:
        hit_rate = 100.0 * hit_tokens / (hit_tokens + miss_tokens)
    return {
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "hit_tokens": hit_tokens,
        "miss_tokens": miss_tokens,
        "write_tokens": write_tokens,
        "hit_rate": hit_rate,
    }


def token_usage_status_params(payload: dict[str, Any]) -> dict[str, str] | None:
    """Format kwargs for ``/status`` token/cache rows (CLI + channel)."""
    parts = token_usage_parts(payload)
    if parts is None:
        return None
    hit_rate = parts["hit_rate"]
    return {
        "input": fmt_tokens(int(parts["input_tokens"])),
        "output": fmt_tokens(int(parts["output_tokens"])),
        "calls": str(int(parts["calls"])),
        "rate": f"{hit_rate:.1f}%" if hit_rate is not None else "n/a",
        "hit": fmt_tokens(int(parts["hit_tokens"])),
        "miss": fmt_tokens(int(parts["miss_tokens"])),
        "write": fmt_tokens(int(parts["write_tokens"])),
        "has_write": "1" if int(parts["write_tokens"]) > 0 else "",
    }


def token_usage_status_rows(
    payload: dict[str, Any],
    translate: Callable[..., str],
) -> list[str]:
    """Localized ``/status`` lines for context fill, tokens, and cache."""
    rows: list[str] = []
    context = context_usage_status_params(payload)
    if context is not None:
        rows.append(translate("status_row_context", **context))
    params = token_usage_status_params(payload)
    if params is None:
        if not rows:
            return [translate("status_row_tokens_empty")]
        rows.append(translate("status_row_tokens_empty"))
        return rows
    rows.extend(
        [
            translate(
                "status_row_tokens",
                input=params["input"],
                output=params["output"],
                calls=params["calls"],
            ),
            translate(
                "status_row_cache",
                rate=params["rate"],
                hit=params["hit"],
                miss=params["miss"],
            ),
        ]
    )
    if params["has_write"]:
        rows.append(translate("status_row_cache_write", write=params["write"]))
    return rows
