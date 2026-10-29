"""AI provider abstraction — OpenAI-compatible and Anthropic-native providers.

Supports OpenAI-compatible /v1/chat/completions and Anthropic /v1/messages,
with multi-endpoint configuration, same-endpoint retries for transient
failures, and automatic failover by priority.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from argusai.core.ai_model_capabilities import (
    SOURCE_API,
    SOURCE_LEARNED,
    SOURCE_USER,
    _guess_audio_in,
    _guess_image_in,
    _guess_video_in,
    normalize_model_capabilities,
    resolve_endpoint_model_capabilities,
)
from argusai.core.ai_thinking import (
    DEFAULT_EFFORT,
    apply_anthropic_thinking,
    apply_openai_thinking,
    normalize_thinking_effort,
    thinking_effort_from_config,
)
from argusai.core.ai_model_limits import (
    normalize_model_limits,
    resolve_endpoint_model_limits,
)
from argusai.core.ai_provider_catalog import (
    get_provider_preset,
    resolve_provider_id_for,
)
from argusai.core.infra.httpx_client import direct_async_client

log = logging.getLogger("argusai.ai")

# Per-endpoint attempts before falling over to the next configured endpoint.
# Covers transport timeouts and transient 429/5xx; permanent 4xx skip retries.
_ENDPOINT_ATTEMPTS = 3

# Stable product identity for upstream LLM / gateway attribution.
# OpenRouter uses HTTP-Referer + X-Title; official APIs ignore the extras.
_AI_CLIENT_NAME = "ArgusAI"
_AI_CLIENT_REFERER = "https://argusai.local"
_OS_NAMES = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}


def _package_version() -> str:
    try:
        from argusai import __version__ as version
    except Exception:
        return "0.0.0"
    text = str(version or "").strip()
    return text or "0.0.0"


def _header_token(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n").strip()


def default_ai_client_headers() -> dict[str, str]:
    """Identify this Agent to upstream model APIs and aggregators."""
    version = _header_token(_package_version())
    system = _OS_NAMES.get(platform.system(), platform.system() or "unknown")
    machine = platform.machine() or "unknown"
    pyver = platform.python_version()
    user_agent = (
        f"{_AI_CLIENT_NAME}/{version} "
        f"({_header_token(system)}; {_header_token(machine)}) "
        f"Python/{_header_token(pyver)}"
    )
    return {
        "User-Agent": user_agent,
        "HTTP-Referer": _AI_CLIENT_REFERER,
        "X-Title": _AI_CLIENT_NAME,
        "X-OpenRouter-Title": _AI_CLIENT_NAME,
    }


def merge_ai_headers(*parts: dict[str, str] | None) -> dict[str, str]:
    """Merge header maps; later entries win. Names are case-insensitive."""
    merged: dict[str, str] = {}
    seen: dict[str, str] = {}
    for part in parts:
        if not part:
            continue
        for key, value in part.items():
            name = str(key).strip()
            if not name:
                continue
            low = name.lower()
            previous = seen.get(low)
            if previous is not None and previous != name:
                merged.pop(previous, None)
            merged[name] = str(value)
            seen[low] = name
    return merged


def _describe_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        text = _safe_response_text(e.response)[:800] if e.response is not None else ""
        return f"HTTP {e.response.status_code}: {text}"
    if isinstance(e, httpx.RequestError):
        return f"{type(e).__name__}: {e}"
    if e.__cause__:
        return f"{type(e).__name__}: {e}; caused by {_describe_error(e.__cause__)}"
    return f"{type(e).__name__}: {e}"


def _root_exception(exc: BaseException) -> BaseException:
    root: BaseException = exc
    while isinstance(root, RuntimeError) and root.__cause__ is not None:
        root = root.__cause__
    return root


def _is_retryable_ai_error(exc: BaseException) -> bool:
    """True for timeouts / connection errors / 429 / 5xx — safe to retry same endpoint."""
    root = _root_exception(exc)
    if isinstance(root, httpx.RequestError):
        return True
    if isinstance(root, httpx.HTTPStatusError) and root.response is not None:
        status = int(root.response.status_code)
        return status == 429 or status >= 500
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        return _is_retryable_ai_error(cause)
    return False


def _retry_backoff_seconds(attempt_index: int) -> float:
    """Backoff before attempt ``attempt_index + 1`` after a failure (0-based)."""
    return min(2.0, 0.5 * (2 ** attempt_index))


def _safe_response_text(response: httpx.Response) -> str:
    try:
        return response.text
    except RuntimeError:
        return "<response body was not read>"


_HTTP_STATUS_GUIDANCE: dict[int, dict[str, str]] = {
    401: {
        "title": "AI 端点鉴权失败 (HTTP 401)",
        "reason": "API key 无效、过期或被吊销。",
        "next": "打开「设置 → AI 端点」检查并重新填写 API key；保存后点「重新生成」。",
        "retry": "key 未更新前直接「重新生成」会再次 401。",
    },
    402: {
        "title": "AI 端点余额不足 (HTTP 402)",
        "reason": "该端点账户余额不足，供应商已拒绝继续计费。",
        "next": "前往「设置 → AI 端点」添加另一个可用端点，或给当前账户充值后再「重新生成」。",
        "retry": "不充值/不换端点的情况下重试只会得到同样的 402。",
    },
    403: {
        "title": "AI 端点拒绝访问 (HTTP 403)",
        "reason": "权限不足或区域受限。常见原因：模型未启用、未通过实名、IP 受限。",
        "next": "在供应商后台确认账户与模型权限，或在「设置 → AI 端点」换一个端点。",
        "retry": "权限未变化前重试也是 403。",
    },
    404: {
        "title": "AI 端点找不到资源 (HTTP 404)",
        "reason": "通常是 base_url 拼写错误，或选用的模型名在该端点不存在。",
        "next": "打开「设置 → AI 端点」核对 base_url 和模型名。",
        "retry": "路径/模型未修正前重试无效。",
    },
    429: {
        "title": "AI 端点限流 (HTTP 429)",
        "reason": "请求频率或配额已超出。",
        "next": "稍候 30–60 秒再点「重新生成」，或在「设置 → AI 端点」追加另一条优先级更低的备用端点。",
        "retry": "立即重试很可能还会 429，建议等一会儿。",
    },
}


def _parse_endpoint_error_body(text: str) -> str:
    """Extract a human message from a provider error body without dumping JSON."""

    if not text:
        return ""
    snippet = text.strip()
    if not snippet:
        return ""
    try:
        payload = json.loads(snippet)
    except (ValueError, TypeError):
        return snippet[:200]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            for key in ("message", "msg", "detail", "description"):
                value = err.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        for key in ("message", "error", "detail", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return snippet[:200]


def format_ai_failure(exc: BaseException | None) -> str:
    """Render an AI provider failure as a user-facing multi-line message.

    Recognises common HTTP status codes (401/402/403/404/429) raised by
    OpenAI-compatible and Anthropic endpoints, plus httpx connectivity
    errors. The output is what session_chat_loop puts in the assistant
    bubble when the call ultimately fails — it must be actionable, point
    at Settings, and explain why pure "Regenerate" will not help.
    """

    if exc is None:
        return "AI 调用失败：未知错误。请前往「设置 → AI 端点」检查端点配置。"

    root: BaseException = exc
    last_endpoint: str | None = None
    if isinstance(exc, RuntimeError):
        match = re.search(r"All (\d+) AI endpoints failed", str(exc))
        if match and exc.__cause__ is not None:
            root = exc.__cause__
            try:
                last_endpoint = str(int(match.group(1)))
            except (TypeError, ValueError):
                last_endpoint = None
    while isinstance(root, RuntimeError) and root.__cause__ is not None:
        root = root.__cause__

    if isinstance(root, httpx.HTTPStatusError) and root.response is not None:
        status = int(root.response.status_code)
        body = _safe_response_text(root.response)[:800]
        provider_msg = _parse_endpoint_error_body(body)
        guidance = _HTTP_STATUS_GUIDANCE.get(status)
        lines: list[str] = []
        if guidance:
            lines.append(f"⚠️ {guidance['title']}")
            lines.append("")
            lines.append(f"**原因**：{guidance['reason']}")
            if provider_msg:
                lines.append(f"**端点返回**：`{provider_msg}`")
            lines.append("")
            lines.append(f"**下一步**：{guidance['next']}")
            lines.append("")
            lines.append(f"_提示：{guidance['retry']}_")
        else:
            severity = "服务端错误" if status >= 500 else "请求错误"
            lines.append(f"⚠️ AI 端点{severity} (HTTP {status})")
            if provider_msg:
                lines.append("")
                lines.append(f"**端点返回**：`{provider_msg}`")
            lines.append("")
            if status >= 500:
                lines.append("**下一步**：稍后再点「重新生成」，或在「设置 → AI 端点」配置备用端点。")
            else:
                lines.append("**下一步**：前往「设置 → AI 端点」检查端点参数（base_url、模型名、API key）。")
        if last_endpoint:
            lines.append("")
            lines.append(f"_所有 {last_endpoint} 个已配置端点都失败了。多端点 fallback 已尝试。_")
        return "\n".join(lines)

    if isinstance(root, httpx.RequestError):
        return (
            "⚠️ 无法连接到 AI 端点\n\n"
            f"**原因**：`{type(root).__name__}: {root}`\n\n"
            "**下一步**：\n"
            "1. 检查网络（代理/VPN/防火墙是否拦截了 api 域名）\n"
            "2. 打开「设置 → AI 端点」核对 base_url\n"
            "3. 网络恢复后再点「重新生成」\n\n"
            "_当前无法到达端点，立即重新生成多半得到同一错误。_"
        )

    # Generic fallback — keep the original detail but in a friendlier shell.
    detail = _describe_error(root) if isinstance(root, Exception) else str(root)
    return (
        "⚠️ AI 调用失败\n\n"
        f"**详细信息**：`{detail}`\n\n"
        "**下一步**：前往「设置 → AI 端点」核查端点配置，或追加备用端点后再试。"
    )


async def _raise_for_status_with_body(response: httpx.Response) -> None:
    if response.is_error:
        await response.aread()
    response.raise_for_status()


# Matches Anthropic's 400 body, e.g.
#   "max_tokens: 131072 > 128000, which is the maximum allowed number of
#    output tokens for claude-opus-4-7"
_OUTPUT_TOKEN_CAP_RE = re.compile(r"max_tokens\D{0,40}?(\d{3,})\s*>\s*(\d{3,})")

# OpenAI-compatible gateways phrase the same problem without the ">" form, e.g.
#   "max_tokens is too large: 131072. This model supports at most 128000 ..."
#   "maximum output tokens is 8192"
# Context-window errors are *not* output-cap signals — see
# ``_CONTEXT_OVERFLOW_RE``. The older "maximum context length is 128000"
# prose used to be treated as max_tokens and produced a useless retry
# with the same oversized prompt.
_OUTPUT_TOKEN_OPENAI_RES = (
    # "... supports at most 128000 completion tokens" — number before "tokens".
    re.compile(
        r"(?:at most|maximum[\w\s'/]*?(?:is|of)?|supports?(?:\s+at\s+most)?|up to)\s*(\d{3,})\s*"
        r"(?:completion|output|context|response)?\s*tokens",
        re.IGNORECASE,
    ),
    # "... maximum output tokens is 8192" — number after "tokens is/of/:".
    re.compile(r"tokens\s*(?:is|of|:|=)\s*(\d{3,})", re.IGNORECASE),
)

_CONTEXT_OVERFLOW_RE = re.compile(
    r"\b(?:context(?:\s+window|\s+length)?|prompt\s+(?:is\s+)?too\s+long)\b",
    re.IGNORECASE,
)
_OUTPUT_CAP_HINT_RE = re.compile(
    r"\b(?:max_tokens|completion|output)\b",
    re.IGNORECASE,
)


def _extract_output_token_cap(text: str, requested: int | None = None) -> int | None:
    """Parse the allowed output-token cap out of a 400 error body.

    Returns the model's maximum ``max_tokens`` when the body reports that the
    requested value exceeded it, otherwise ``None``. Lets us self-heal a
    misconfigured "最大输出" without forcing the user to guess the cap. Handles
    both Anthropic's ``X > Y`` form and the prose OpenAI-compatible gateways use.
    """
    if not text:
        return None

    match = _OUTPUT_TOKEN_CAP_RE.search(text)
    if match:
        try:
            req = int(match.group(1))
            allowed = int(match.group(2))
        except (TypeError, ValueError):
            allowed = req = 0
        if 0 < allowed < req:
            return allowed

    # A context-overflow 400 is not an output-token cap. Clamping max_tokens
    # and resending the same prompt still 400s, and can hide the real budget
    # problem (the request should have been compacted for this model).
    if _CONTEXT_OVERFLOW_RE.search(text) and not _OUTPUT_CAP_HINT_RE.search(text):
        return None

    # OpenAI-style prose: collect every plausible cap and keep the largest one
    # that is still below what we asked for. Only consulted when we know the
    # requested value, so we never clamp a request that was already in range.
    if requested and requested > 0:
        candidates: list[int] = []
        for pattern in _OUTPUT_TOKEN_OPENAI_RES:
            candidates.extend(
                int(n)
                for n in pattern.findall(text)
                if n.isdigit() and 0 < int(n) < requested
            )
        if candidates:
            return max(candidates)
    return None


def _apply_openai_thinking_preference(
    body: dict[str, Any],
    thinking_enabled: bool | None,
    *,
    config: AIConfig | None = None,
) -> None:
    apply_openai_thinking(
        body,
        thinking_enabled,
        model=str(getattr(config, "model", "") or ""),
        effort=thinking_effort_from_config(config),
    )


def _openai_should_replay_reasoning_content(
    extra_body: dict[str, Any] | None,
    thinking_enabled: bool | None,
) -> bool:
    """Whether prior assistant ``reasoning_content`` should be sent upstream.

    DashScope / Qwen thinking (and similar OpenAI-compatible gateways) reject
    follow-up turns with HTTP 400 unless previous thinking is echoed:
    "The reasoning_content in the thinking mode must be passed back to the API."
    History still stores the field for the UI; this only controls re-feed.
    """
    extra = extra_body or {}
    if thinking_enabled is False:
        return False
    if thinking_enabled is True:
        return True
    if extra.get("replay_reasoning_content") is False:
        return False
    return True


# Keys consumed only by ArgusAI client logic — never forwarded upstream.
_OPENAI_LOCAL_EXTRA_BODY_KEYS = frozenset({
    "explicit_cache",
    "replay_reasoning_content",
})


def _merge_extra_body(body: dict[str, Any], extra_body: dict[str, Any]) -> None:
    """Merge provider-specific overrides, using JSON null to omit defaults."""
    for key, value in extra_body.items():
        if key in _OPENAI_LOCAL_EXTRA_BODY_KEYS:
            continue
        if value is None:
            body.pop(key, None)
        else:
            body[key] = value


def _apply_temperature(
    body: dict[str, Any],
    config: AIConfig,
    temperature: float | None,
) -> None:
    """Add temperature when the endpoint allows it (some Claude models reject it)."""
    if not getattr(config, "send_temperature", True):
        return
    if temperature is not None:
        body["temperature"] = temperature
    else:
        body["temperature"] = config.temperature


def _apply_anthropic_thinking_preference(
    body: dict[str, Any],
    thinking_enabled: bool | None,
    *,
    budget_tokens: int = 1024,
    config: AIConfig | None = None,
) -> None:
    apply_anthropic_thinking(
        body,
        thinking_enabled,
        model=str(getattr(config, "model", "") or ""),
        effort=thinking_effort_from_config(config),
        budget_tokens=int(
            getattr(config, "thinking_budget_tokens", None) or budget_tokens or 1024
        ),
    )


def _anthropic_cache_marker(cache_ttl: str = "5m") -> dict[str, str]:
    marker: dict[str, str] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _anthropic_system_with_cache(system: str, *, cache_ttl: str = "5m") -> Any:
    """Stable system prefix with a cache breakpoint (Loom22 §12.5)."""
    text = str(system or "")
    if not text.strip():
        return text
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": _anthropic_cache_marker(cache_ttl),
        }
    ]


def _can_carry_anthropic_cache_marker(message: dict[str, Any]) -> bool:
    """True if a ``cache_control`` marker on this message's last content part
    is actually meaningful. ``AnthropicProvider._convert_messages`` never
    appends a message with empty content, so this is mostly a defensive
    check against future changes to that conversion.
    """
    content = message.get("content")
    if content is None or content == "":
        return False
    if isinstance(content, list):
        return bool(content) and isinstance(content[-1], dict)
    return isinstance(content, str)


def _apply_anthropic_content_marker(message: dict[str, Any], marker: dict[str, str]) -> None:
    """Attach ``cache_control`` to the last content part of one message."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content, "cache_control": dict(marker)}]
        return
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = dict(marker)


def _apply_anthropic_cache_control(
    messages: list[dict[str, Any]],
    *,
    cache_ttl: str = "5m",
    max_breakpoints: int = 3,
) -> list[dict[str, Any]]:
    """Mark the most recent non-system messages so the growing conversation
    caches too, not just the system prompt (Hermes Agent's ``system_and_3``
    layout: https://github.com/NousResearch/hermes-agent).

    Anthropic allows 4 ``cache_control`` breakpoints per request; the system
    prompt already claims one (``_anthropic_system_with_cache``), so this
    spends the remaining 3 on the tail of the conversation — the region that
    is identical to the previous call except for whatever the latest turn
    appended. Messages are always converted to Anthropic's native
    content-block-list shape here (never OpenRouter's top-level-marker
    envelope), so a marker on the last block of *any* role, including a
    tool_use-only assistant turn, is honored — no format-specific carve-outs
    needed.

    Returns a list with copies of only the newly-marked message dicts, so the
    caller's (deep-copied, request-local) list is left otherwise untouched.
    """
    if not messages or max_breakpoints <= 0:
        return messages
    marker = _anthropic_cache_marker(cache_ttl)
    result = list(messages)
    candidates = [i for i, m in enumerate(result) if _can_carry_anthropic_cache_marker(m)]
    for idx in candidates[-max_breakpoints:]:
        copy = dict(result[idx])
        _apply_anthropic_content_marker(copy, marker)
        result[idx] = copy
    return result


def _normalize_openai_cache_usage(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Canonical cache-usage shape from an OpenAI/DeepSeek-compatible ``usage`` object.

    DeepSeek reports ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``.
    DashScope / OpenAI-style gateways often nest hits under
    ``prompt_tokens_details.cached_tokens`` and writes under
    ``cache_creation_input_tokens`` (top-level or inside details). Returns
    ``None`` when the endpoint sent no usage at all so callers can tell
    "no data" from "all zero".
    """
    if not isinstance(usage, dict):
        return None
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if not prompt_tokens and not completion_tokens:
        return None
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    cache_hit = int(
        usage.get("prompt_cache_hit_tokens")
        or prompt_details.get("cached_tokens")
        or 0
    )
    cache_write = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_tokens")
        or prompt_details.get("cache_creation_input_tokens")
        or prompt_details.get("cache_creation_tokens")
        or prompt_details.get("cache_write_tokens")
        or 0
    )
    raw_miss = usage.get("prompt_cache_miss_tokens")
    if raw_miss is not None:
        cache_miss = int(raw_miss)
    else:
        # Creation tokens are billed separately from "other" miss tokens;
        # keep miss as the non-hit remainder so HIT+MISS ≈ prompt.
        cache_miss = max(0, prompt_tokens - cache_hit)
    details = usage.get("completion_tokens_details")
    reasoning = int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
    return {
        "input_tokens": prompt_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_write_tokens": cache_write,
        "output_tokens": completion_tokens,
        "reasoning_tokens": reasoning,
        "reply_tokens": max(0, completion_tokens - reasoning),
    }


# OpenAI-compatible explicit-cache flavors injected into chat/completions.
_CACHE_FLAVOR_CONTROL = "cache_control"  # Qwen / OpenRouter-Gemini (Anthropic-shaped)
_CACHE_FLAVOR_OPENAI = "prompt_cache_breakpoint"  # OpenAI GPT-5.6+


def _openai_model_id(model: str) -> str:
    """Strip OpenRouter-style ``vendor/`` prefix and ``:suffix`` tags."""
    mid = str(model or "").strip().lower()
    if "/" in mid:
        mid = mid.rsplit("/", 1)[-1]
    if ":" in mid:
        mid = mid.split(":", 1)[0]
    return mid


def _openai_model_supports_prompt_cache_breakpoint(model: str) -> bool:
    """True for GPT-5.6+ / GPT-6+ families that accept ``prompt_cache_breakpoint``.

    Older OpenAI models reject the field with 400 — keep them on implicit cache.
    """
    mid = _openai_model_id(model)
    if re.match(r"^gpt-([6-9]|[1-9]\d)", mid):
        return True
    m = re.match(r"^gpt-5\.(\d+)", mid)
    return bool(m and int(m.group(1)) >= 6)


def _openai_explicit_cache_flavor(config: "AIConfig") -> str | None:
    """Which explicit-cache marker shape to inject, or ``None`` to leave alone.

    ``extra_body.explicit_cache``:
      - ``false`` / ``0`` / ``""`` → disabled
      - ``true`` → auto-pick flavor for this endpoint/model
      - ``"cache_control"`` / ``"qwen"`` / ``"gemini"`` → Anthropic-shaped markers
      - ``"prompt_cache_breakpoint"`` / ``"openai"`` → OpenAI GPT-5.6+ markers

    Auto (when unset):
      - DashScope / Qwen / QwQ → ``cache_control``
      - OpenRouter + Gemini → ``cache_control`` (OpenRouter's Gemini path)
      - GPT-5.6+ (any host) → ``prompt_cache_breakpoint``
      - Google AI OpenAI-compat Gemini → no markers (implicit cache; use
        ``extra_body.google.cached_content`` for CachedContents IDs)
    """
    extra = config.extra_body if isinstance(getattr(config, "extra_body", None), dict) else {}
    raw = extra.get("explicit_cache", None)
    base = str(getattr(config, "base_url", "") or "").lower()
    model = str(getattr(config, "model", "") or "").lower()
    mid = _openai_model_id(model)

    def _auto() -> str | None:
        if "dashscope" in base or "bailian" in base:
            return _CACHE_FLAVOR_CONTROL
        if mid.startswith("qwen") or mid.startswith("qwq"):
            return _CACHE_FLAVOR_CONTROL
        if "gemini" in mid and "openrouter" in base:
            return _CACHE_FLAVOR_CONTROL
        if _openai_model_supports_prompt_cache_breakpoint(model):
            return _CACHE_FLAVOR_OPENAI
        return None

    if raw is None:
        return _auto()
    if isinstance(raw, str):
        key = raw.strip().lower()
        if not key or key in {"0", "false", "no", "off", "none"}:
            return None
        if key in {"1", "true", "yes", "on", "auto"}:
            return _auto() or _CACHE_FLAVOR_CONTROL
        if key in {_CACHE_FLAVOR_CONTROL, "qwen", "gemini", "anthropic"}:
            return _CACHE_FLAVOR_CONTROL
        if key in {_CACHE_FLAVOR_OPENAI, "openai", "gpt"}:
            return _CACHE_FLAVOR_OPENAI
        return _auto()
    if raw:
        return _auto() or _CACHE_FLAVOR_CONTROL
    return None


def _openai_explicit_cache_enabled(config: "AIConfig") -> bool:
    """Whether any OpenAI-compatible explicit-cache markers will be injected."""
    return _openai_explicit_cache_flavor(config) is not None


def _select_openai_cache_breakpoint_indexes(
    messages: list[dict[str, Any]],
    *,
    max_breakpoints: int = 4,
) -> list[int]:
    """Pick system + conversation-tail indexes for cache markers (≤ ``max_breakpoints``)."""
    if not messages or max_breakpoints <= 0:
        return []
    indexes: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            continue
        if _can_carry_anthropic_cache_marker(msg):
            indexes.append(i)
        break
    remaining = max_breakpoints - len(indexes)
    if remaining <= 0:
        return indexes
    candidates = [
        i for i, m in enumerate(messages)
        if m.get("role") != "system" and _can_carry_anthropic_cache_marker(m)
    ]
    indexes.extend(candidates[-remaining:])
    return indexes


def _apply_content_part_marker(
    message: dict[str, Any],
    *,
    key: str,
    value: dict[str, str],
) -> None:
    """Attach a marker field to the last content part (wrapping bare strings)."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = [{"type": "text", "text": content, key: dict(value)}]
        return
    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last[key] = dict(value)


def _apply_openai_explicit_cache_markers(
    messages: list[dict[str, Any]],
    *,
    flavor: str,
    max_breakpoints: int = 4,
) -> list[dict[str, Any]]:
    """Mark system + recent turns with the provider-specific cache breakpoint field."""
    if not messages or max_breakpoints <= 0:
        return messages
    if flavor == _CACHE_FLAVOR_OPENAI:
        key, value = "prompt_cache_breakpoint", {"mode": "explicit"}
    else:
        key, value = "cache_control", {"type": "ephemeral"}
    indexes = _select_openai_cache_breakpoint_indexes(messages, max_breakpoints=max_breakpoints)
    if not indexes:
        return messages
    result = list(messages)
    for idx in indexes:
        copy = dict(result[idx])
        _apply_content_part_marker(copy, key=key, value=value)
        result[idx] = copy
    return result


def _apply_openai_explicit_cache_control(
    messages: list[dict[str, Any]],
    *,
    max_breakpoints: int = 4,
) -> list[dict[str, Any]]:
    """Back-compat wrapper: Qwen/DashScope ``cache_control`` markers."""
    return _apply_openai_explicit_cache_markers(
        messages,
        flavor=_CACHE_FLAVOR_CONTROL,
        max_breakpoints=max_breakpoints,
    )


def _finalize_openai_cache_body(body: dict[str, Any], config: "AIConfig", flavor: str | None) -> None:
    """Request-level cache knobs + Google OpenAI-compat ``extra_body.google`` nesting."""
    if flavor == _CACHE_FLAVOR_OPENAI:
        opts = body.get("prompt_cache_options")
        if not isinstance(opts, dict):
            body["prompt_cache_options"] = {"mode": "explicit"}
        else:
            opts.setdefault("mode", "explicit")

    base = str(getattr(config, "base_url", "") or "").lower()
    if "googleapis.com" not in base and "generativelanguage" not in base:
        return
    # Users commonly put ``{"google": {...}}`` in endpoint extra_body; Google's
    # OpenAI-compat REST expects that object nested under top-level ``extra_body``.
    google = body.pop("google", None)
    if google is None:
        return
    nested = body.get("extra_body")
    if not isinstance(nested, dict):
        nested = {}
        body["extra_body"] = nested
    existing = nested.get("google")
    if isinstance(existing, dict) and isinstance(google, dict):
        nested["google"] = {**existing, **google}
    else:
        nested["google"] = google


def _normalize_anthropic_cache_usage(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Canonical cache-usage shape from an Anthropic ``usage`` object.

    Anthropic reports the *miss* portion as ``input_tokens`` (cached tokens
    are explicitly excluded from it), plus separate
    ``cache_creation_input_tokens`` (write, billed at a TTL-dependent
    premium) and ``cache_read_input_tokens`` (hit, billed at a steep
    discount) counters.
    """
    if not isinstance(usage, dict):
        return None
    cache_miss = int(usage.get("input_tokens") or 0)
    cache_hit = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if not cache_miss and not cache_hit and not cache_write and not output_tokens:
        return None
    return {
        "input_tokens": cache_miss + cache_hit + cache_write,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": 0,
        "reply_tokens": output_tokens,
    }


def _format_anthropic_refusal(
    stop_reason: str | None,
    stop_details: dict[str, Any] | None = None,
) -> str:
    """Build a user-visible message for Anthropic API-level refusals.

    Recent Claude endpoints may finish a stream with ``stop_reason=refusal``
    and empty ``content`` (no text/tool blocks). Without surfacing that
    detail the Agent UI looks like a silent no-op.
    """
    if str(stop_reason or "") != "refusal":
        return ""
    details = stop_details if isinstance(stop_details, dict) else {}
    category = str(details.get("category") or "").strip()
    explanation = str(details.get("explanation") or "").strip()
    lines = [
        "[模型拒绝回复]",
        "Anthropic 端点以 Usage Policy 拦截了本次请求（stop_reason=refusal），没有生成任何正文。",
    ]
    if category:
        lines.append(f"类别：{category}")
    if explanation:
        lines.append(f"说明：{explanation}")
    lines.append(
        "常见原因：Agent 系统提示词 / 安全工具描述触发了「cyber / 入侵相关」策略。"
        "绿灯模式无法绕过这类 API 层拒绝；请换用更宽松的安全审计模型，或降低攻击性工具暴露。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Single-endpoint config (backward-compatible, also used for CLI overrides)
# ---------------------------------------------------------------------------

@dataclass
class AIConfig:
    """Single-endpoint AI provider configuration."""

    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 16384
    context_window: int = 131072
    temperature: float = 0.7
    send_temperature: bool = True
    timeout: float = 300.0
    protocol: str = "openai"  # "openai" or "anthropic"
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Provider-side capability gates. Decide whether image/video/audio
    # attachments carried on user history messages get serialised into the
    # multimodal rails (image_url / video_url / input_audio). When the active
    # model doesn't support them, we keep the attachment data in history (so
    # the UI still renders thumbnails) but DO NOT forward it to the upstream
    # API — auxiliary enrichment is expected to have already injected a text
    # description.
    #
    # Video is a strict superset of image on every gateway we've seen: a model
    # that takes video also takes stills, but plenty of vision models (gpt-4o,
    # claude) take stills only. Hence the separate gate — see
    # media_enrichment.enrich_user_attachments for the keyframe fallback used
    # when the model is image-only.
    image_in: bool = False
    video_in: bool = False
    audio_in: bool = False
    # Per-key origin of the flags above when this config was built from an
    # endpoint (``user`` / ``api`` / ``guess``). Ad-hoc AIConfig leaves this
    # empty so the name heuristic still guards text-only models.
    capability_source: dict[str, str] = field(default_factory=dict)
    # Dedicated auxiliary endpoints (vision / ASR) are, by definition,
    # multimodal. They must always serialise the image/audio block regardless
    # of the model-name heuristic, otherwise the bridge sends a text-only
    # request and the model replies "no image was provided". Set ONLY for
    # auxiliary providers (see create_auxiliary_provider) — never for the main
    # chat config, where the name guard protects text-only models.
    force_multimodal: bool = False
    # Loom22 §3.1 — Anthropic extended thinking budget (tokens).
    thinking_budget_tokens: int = 1024
    # Unified thinking intensity. Mapped per vendor in ``ai_thinking``.
    thinking_effort: str = DEFAULT_EFFORT

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "temperature": self.temperature,
            "send_temperature": self.send_temperature,
        }


# ---------------------------------------------------------------------------
# Multi-endpoint config (from user-level settings.json)
# ---------------------------------------------------------------------------


def _trusted_capability(config: AIConfig, key: str) -> bool | None:
    """Return a resolved bool when Settings / ``/models`` already decided.

    Ad-hoc ``AIConfig`` (tests, CLI overrides) leaves ``capability_source``
    empty, so the name heuristic still has to approve a ``True`` flag — that
    is the DashScope ``qwen3.7-max`` 400 guard. Endpoint-built configs pass
    ``user`` / ``api`` and are trusted as-is, including custom-named VL
    models the heuristic would miss.
    """
    source = str((getattr(config, "capability_source", None) or {}).get(key) or "")
    if source in (SOURCE_USER, SOURCE_LEARNED, SOURCE_API):
        return bool(getattr(config, key, False))
    return None


def _effective_native_image_in(config: AIConfig) -> bool:
    """Whether this request should serialise ``image_url`` blocks."""
    if getattr(config, "force_multimodal", False):
        return True
    trusted = _trusted_capability(config, "image_in")
    if trusted is not None:
        return trusted
    return bool(getattr(config, "image_in", False)) and _guess_image_in(config.model)


def _effective_native_video_in(config: AIConfig) -> bool:
    """Same as images, minus the auxiliary escape.

    ``force_multimodal`` is only set for the auxiliary vision/ASR providers,
    which are fed stills and audio clips — never video. Honouring it here
    would push a ``video_url`` block at an endpoint that never asked for one.
    """
    trusted = _trusted_capability(config, "video_in")
    if trusted is not None:
        return trusted
    return bool(getattr(config, "video_in", False)) and _guess_video_in(config.model)


def _effective_native_audio_in(config: AIConfig) -> bool:
    if getattr(config, "force_multimodal", False):
        return True
    trusted = _trusted_capability(config, "audio_in")
    if trusted is not None:
        return trusted
    return bool(getattr(config, "audio_in", False)) and _guess_audio_in(config.model)


def _collapse_openai_text_parts(parts: list[Any]) -> str:
    texts: list[str] = []
    for block in parts:
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "")
            if text:
                texts.append(text)
    return "\n".join(texts)


def _sanitize_openai_message_content(
    content: Any,
    *,
    allow_image: bool,
    allow_audio: bool,
    allow_video: bool = False,
) -> Any:
    """Drop unsupported multimodal blocks; flatten text-only arrays to strings."""
    if not isinstance(content, list):
        return content if isinstance(content, str) else str(content or "")

    cleaned: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            cleaned.append(block)
        elif btype == "image_url" and allow_image:
            cleaned.append(block)
        elif btype == "video_url" and allow_video:
            cleaned.append(block)
        elif btype == "input_audio" and allow_audio:
            cleaned.append(block)

    if not cleaned:
        return _collapse_openai_text_parts(content)
    if not allow_image and not allow_audio and not allow_video:
        collapsed = _collapse_openai_text_parts(cleaned)
        return collapsed if collapsed else _collapse_openai_text_parts(content)
    if len(cleaned) == 1 and cleaned[0].get("type") == "text":
        return str(cleaned[0].get("text") or "")
    return cleaned


@dataclass
class AIEndpoint:
    """One entry in the multi-endpoint settings list."""

    enabled: bool = True
    name: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o"
    # Every model this endpoint is allowed to serve. ``model`` stays the active
    # one so failover and budgeting keep working untouched; this catalog is only
    # what the model picker may switch between. Empty means "just ``model``",
    # which is how every config written before the catalog existed reads.
    models: list[str] = field(default_factory=list)
    max_tokens: int = 16384
    context_window: int = 131072
    # Per-model window / output caps. Endpoint-level numbers stay the
    # fallback for ids that have no row here (legacy configs, unlisted
    # models). See ``argusai.core.ai_model_limits``.
    model_limits: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per-model image/video/audio input. Endpoint-level ``image_in`` below
    # is a legacy mirror only — runtime follows the active model. See
    # ``argusai.core.ai_model_capabilities``.
    model_capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    temperature: float = 0.7
    send_temperature: bool = True
    timeout: float = 300.0
    priority: int = 0  # higher = tried first
    protocol: str = "openai"  # "openai" or "anthropic"
    # Stable preset id from ai_provider_catalog; "" means unknown/legacy and
    # is resolved on load (see from_dict). "custom" means a hand-rolled endpoint.
    provider_id: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # Legacy mirrors of the *active* model's resolved modalities. Kept so
    # older settings.json / UI still round-trip; ``to_ai_config`` ignores
    # them and calls ``resolve_endpoint_model_capabilities``.
    image_in: bool = False
    video_in: bool = False
    audio_in: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "models": list(self.models),
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "model_limits": normalize_model_limits(self.model_limits),
            "model_capabilities": normalize_model_capabilities(self.model_capabilities),
            "temperature": self.temperature,
            "send_temperature": self.send_temperature,
            "priority": self.priority,
            "timeout": self.timeout,
            "protocol": self.protocol,
            "provider_id": self.provider_id,
            "extra_headers": self.extra_headers,
            "extra_body": self.extra_body,
            "modalities": {
                "text": True,
                "image_in": bool(self.image_in),
                "video_in": bool(self.video_in),
                "audio_in": bool(self.audio_in),
            },
            "image_in": bool(self.image_in),
            "video_in": bool(self.video_in),
            "audio_in": bool(self.audio_in),
        }

    def model_catalog(self) -> list[str]:
        """Switchable models for this endpoint, the active one first.

        Endpoints written before the catalog existed carry no ``models``, so
        they report their single model and stay selectable like any other.
        """
        catalog = [self.model] if self.model else []
        for name in self.models:
            candidate = str(name or "").strip()
            if candidate and candidate not in catalog:
                catalog.append(candidate)
        return catalog

    def to_ai_config(self) -> AIConfig:
        context_window, max_tokens = resolve_endpoint_model_limits(self)
        caps = resolve_endpoint_model_capabilities(self)
        return AIConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            max_tokens=max_tokens,
            context_window=context_window,
            temperature=self.temperature,
            send_temperature=self.send_temperature,
            timeout=self.timeout,
            protocol=self.protocol,
            extra_headers=self.extra_headers,
            extra_body=self.extra_body,
            image_in=bool(caps.get("image_in")),
            video_in=bool(caps.get("video_in")),
            audio_in=bool(caps.get("audio_in")),
            capability_source=dict(caps.get("sources") or {}),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AIEndpoint":
        raw = dict(d or {})
        modalities = raw.get("modalities") if isinstance(raw.get("modalities"), dict) else {}
        data = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        data["enabled"] = bool(data.get("enabled", True))
        if "send_temperature" in data:
            data["send_temperature"] = bool(data["send_temperature"])
        catalog: list[str] = []
        for name in data.get("models") or []:
            candidate = str(name or "").strip()
            if candidate and candidate not in catalog:
                catalog.append(candidate)
        model = str(data.get("model") or raw.get("model") or "")
        # A catalog without the active model would let the picker "switch" to a
        # model the endpoint is not currently on and show nothing as selected.
        if model and model not in catalog:
            catalog.insert(0, model)
        elif not model and catalog:
            model = catalog[0]
            data["model"] = model
        data["models"] = catalog
        # provider_id: keep an explicit known value; resolve an explicit unknown
        # to custom; otherwise migrate legacy configs by unique base_url match.
        raw_provider = str(raw.get("provider_id") or "").strip() or ""
        if raw_provider and raw_provider != "custom":
            data["provider_id"] = (
                raw_provider
                if get_provider_preset(raw_provider) is not None
                else "custom"
            )
        elif "provider_id" not in data:
            data["provider_id"] = resolve_provider_id_for(raw)
        elif data.get("provider_id") in (None, ""):
            data["provider_id"] = resolve_provider_id_for(raw)
        if "image_in" not in data:
            data["image_in"] = bool(
                raw.get("image_in", modalities.get("image_in", _guess_image_in(model)))
            )
        if "video_in" not in data:
            data["video_in"] = bool(
                raw.get("video_in", modalities.get("video_in", _guess_video_in(model)))
            )
        if "audio_in" not in data:
            data["audio_in"] = bool(
                raw.get("audio_in", modalities.get("audio_in", _guess_audio_in(model)))
            )
        # A video-capable model is always still-capable; the reverse is not
        # true. Repair configs written before video_in existed (or hand-edited)
        # so the enrichment fallback never sees "video yes, images no".
        if data.get("video_in"):
            data["image_in"] = True
        data["model_limits"] = normalize_model_limits(
            data.get("model_limits") or raw.get("model_limits")
        )
        data["model_capabilities"] = normalize_model_capabilities(
            data.get("model_capabilities") or raw.get("model_capabilities")
        )
        return cls(**data)


@dataclass
class AuxiliaryAIEndpoint:
    """Dedicated vision / ASR endpoint — never mixed into chat failover."""

    enabled: bool = False
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    max_tokens: int = 4096
    context_window: int = 131072
    temperature: float = 0.2
    send_temperature: bool = True
    timeout: float = 120.0
    protocol: str = "openai"
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "temperature": self.temperature,
            "send_temperature": self.send_temperature,
            "timeout": self.timeout,
            "protocol": self.protocol,
            "extra_headers": self.extra_headers,
            "extra_body": self.extra_body,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AuxiliaryAIEndpoint":
        raw = dict(d or {})
        data = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        # The ``enabled`` flag is the source of truth; we no longer auto-flip
        # it on when credentials are filled. Users explicitly toggle the
        # switch in Settings → AI 配置 → 视觉辅助 / ASR 辅助.
        data["enabled"] = bool(data.get("enabled", False))
        if "send_temperature" in data:
            data["send_temperature"] = bool(data["send_temperature"])
        return cls(**data)


# ---------------------------------------------------------------------------
# Abstract provider interface
# ---------------------------------------------------------------------------

class AIProvider(ABC):
    """Abstract base for LLM interaction."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request. Returns the full API response dict."""
        ...

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_reasoning: Callable[[str], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Stream text deltas when supported, returning the final response dict.

        ``on_reasoning`` (optional) receives reasoning/thinking deltas for models
        that emit a separate thinking channel (DeepSeek/Anthropic). It lets the UI
        show live progress while the model reasons — without it the bubble looks
        frozen during a long thinking phase that produces no visible text yet.
        """
        result = await self.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
        )
        if on_delta:
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
                maybe = on_delta(text)
                if hasattr(maybe, "__await__"):
                    await maybe
        return result

    @abstractmethod
    async def analyze(self, system_prompt: str, user_content: str) -> str:
        """Single-shot analysis: system prompt + user message -> assistant text."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""
        ...


# ---------------------------------------------------------------------------
# Single-endpoint provider
# ---------------------------------------------------------------------------

class OpenAICompatibleProvider(AIProvider):
    """Works with any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self, config: AIConfig) -> None:
        self.config = config
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        headers = merge_ai_headers(
            headers,
            default_ai_client_headers(),
            config.extra_headers,
        )
        self._client = direct_async_client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        converted = self._convert_messages(messages, thinking_enabled=thinking_enabled)
        cache_flavor = _openai_explicit_cache_flavor(self.config)
        if cache_flavor:
            converted = _apply_openai_explicit_cache_markers(converted, flavor=cache_flavor)
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": converted,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        _apply_temperature(body, self.config, temperature)
        if tools:
            body["tools"] = tools
        _merge_extra_body(body, self.config.extra_body)
        _apply_openai_thinking_preference(body, thinking_enabled, config=self.config)
        _finalize_openai_cache_body(body, self.config, cache_flavor)

        import time as _time

        start = _time.monotonic()
        try:
            resp = await self._client.post("/chat/completions", json=body)
            if resp.status_code == 400:
                await resp.aread()
                cap = _extract_output_token_cap(resp.text, int(body.get("max_tokens") or 0))
                if cap is not None and cap < int(body.get("max_tokens") or 0):
                    log.warning(
                        "OpenAI max_tokens=%s exceeds model cap %s; retrying clamped.",
                        body.get("max_tokens"),
                        cap,
                    )
                    body["max_tokens"] = cap
                    self.config.max_tokens = cap
                    resp = await self._client.post("/chat/completions", json=body)
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            log.info(
                "ai.http endpoint=%s model=%s mode=chat status=%d elapsed_ms=%d msgs=%d tools=%d",
                self.config.base_url,
                self.config.model,
                resp.status_code,
                elapsed_ms,
                len(messages),
                len(tools or []),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            log.warning(
                "ai.http_error endpoint=%s model=%s mode=chat status=%d elapsed_ms=%d body=%r",
                self.config.base_url,
                self.config.model,
                e.response.status_code if e.response is not None else -1,
                elapsed_ms,
                _safe_response_text(e.response)[:200] if e.response is not None else "",
            )
            raise
        except Exception as e:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            log.warning(
                "ai.http_error endpoint=%s model=%s mode=chat status=transport elapsed_ms=%d error=%s",
                self.config.base_url,
                self.config.model,
                elapsed_ms,
                e,
            )
            raise
        data = resp.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            log.info(
                "ai.usage endpoint=%s model=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s cache_hit=%s cache_miss=%s",
                self.config.base_url,
                self.config.model,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                usage.get("prompt_cache_hit_tokens"),
                usage.get("prompt_cache_miss_tokens"),
            )
        cache_usage = _normalize_openai_cache_usage(usage)
        if cache_usage is not None:
            data["_cache_usage"] = cache_usage
        return data

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_reasoning: Callable[[str], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        converted = self._convert_messages(messages, thinking_enabled=thinking_enabled)
        cache_flavor = _openai_explicit_cache_flavor(self.config)
        if cache_flavor:
            converted = _apply_openai_explicit_cache_markers(converted, flavor=cache_flavor)
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": converted,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": True,
            # Ask the endpoint to append one usage-only chunk (empty
            # ``choices``, ``prompt_cache_hit_tokens`` / ``_miss_tokens``
            # for DeepSeek) after ``[DONE]``'s predecessor. Off by default on
            # OpenAI-compatible streaming, otherwise cache-hit telemetry is
            # invisible on the hot path even though the API reports it.
            "stream_options": {"include_usage": True},
        }
        _apply_temperature(body, self.config, temperature)
        if tools:
            body["tools"] = tools
        _merge_extra_body(body, self.config.extra_body)
        _apply_openai_thinking_preference(body, thinking_enabled, config=self.config)
        _finalize_openai_cache_body(body, self.config, cache_flavor)
        body["stream"] = True

        import time as _time

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        usage_data: dict[str, Any] | None = None
        clamp_attempted = False

        start = _time.monotonic()
        while True:
            content_parts = []
            reasoning_parts = []
            tool_parts = {}
            usage_data = None
            async with self._client.stream("POST", "/chat/completions", json=body) as resp:
                if resp.status_code == 400 and not clamp_attempted:
                    await resp.aread()
                    cap = _extract_output_token_cap(resp.text, int(body.get("max_tokens") or 0))
                    if cap is not None and cap < int(body.get("max_tokens") or 0):
                        log.warning(
                            "OpenAI stream max_tokens=%s exceeds model cap %s; retrying clamped.",
                            body.get("max_tokens"),
                            cap,
                        )
                        body["max_tokens"] = cap
                        self.config.max_tokens = cap
                        clamp_attempted = True
                        continue
                try:
                    await _raise_for_status_with_body(resp)
                except httpx.HTTPStatusError as e:
                    log.warning(
                        "ai.http_error endpoint=%s model=%s mode=stream status=%d elapsed_ms=%d body=%r",
                        self.config.base_url,
                        self.config.model,
                        e.response.status_code if e.response is not None else -1,
                        int((_time.monotonic() - start) * 1000),
                        _safe_response_text(e.response)[:200] if e.response is not None else "",
                    )
                    raise
                log.info(
                    "ai.http endpoint=%s model=%s mode=stream status=%d msgs=%d tools=%d",
                    self.config.base_url,
                    self.config.model,
                    resp.status_code,
                    len(messages),
                    len(tools or []),
                )
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    chunk_usage = chunk.get("usage")
                    if isinstance(chunk_usage, dict):
                        usage_data = chunk_usage
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        if on_reasoning:
                            maybe = on_reasoning(reasoning)
                            if hasattr(maybe, "__await__"):
                                await maybe
                    text = delta.get("content") or ""
                    if text:
                        content_parts.append(text)
                        if on_delta:
                            maybe = on_delta(text)
                            if hasattr(maybe, "__await__"):
                                await maybe
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index", 0))
                        item = tool_parts.setdefault(idx, {
                            "id": tc.get("id") or "",
                            "type": tc.get("type") or "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tc.get("id"):
                            item["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            item["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            item["function"]["arguments"] += fn["arguments"]
            break

        elapsed_ms = int((_time.monotonic() - start) * 1000)
        log.info(
            "ai.stream_done endpoint=%s model=%s elapsed_ms=%d text_chunks=%d tool_call_count=%d",
            self.config.base_url,
            self.config.model,
            elapsed_ms,
            len(content_parts),
            len(tool_parts),
        )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_parts:
            message["tool_calls"] = [tool_parts[i] for i in sorted(tool_parts)]
        result: dict[str, Any] = {"choices": [{"index": 0, "message": message}]}
        if isinstance(usage_data, dict):
            log.info(
                "ai.usage endpoint=%s model=%s mode=stream prompt_tokens=%s completion_tokens=%s cache_hit=%s cache_miss=%s",
                self.config.base_url,
                self.config.model,
                usage_data.get("prompt_tokens"),
                usage_data.get("completion_tokens"),
                usage_data.get("prompt_cache_hit_tokens"),
                usage_data.get("prompt_cache_miss_tokens"),
            )
            result["usage"] = usage_data
            cache_usage = _normalize_openai_cache_usage(usage_data)
            if cache_usage is not None:
                result["_cache_usage"] = cache_usage
        return result

    async def analyze(self, system_prompt: str, user_content: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            result = await self.chat(messages)
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""
        except httpx.HTTPStatusError as e:
            log.error("AI API error %s: %s", e.response.status_code, _safe_response_text(e.response)[:500])
            raise
        except Exception as e:
            log.error("AI API call failed: %s", e)
            raise

    async def close(self) -> None:
        await self._client.aclose()

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking_enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        allow_image = _effective_native_image_in(self.config)
        allow_video = _effective_native_video_in(self.config)
        allow_audio = _effective_native_audio_in(self.config)
        replay_reasoning = _openai_should_replay_reasoning_content(
            self.config.extra_body,
            thinking_enabled,
        )
        for m in messages:
            role = m.get("role")
            content: Any = m.get("content", "")
            attachments = m.get("attachments") or []
            if role == "user" and attachments:
                # Only image attachments need to ride the multimodal
                # `image_url` rail. Text-style attachments are already
                # spilled to the session sandbox upstream and their
                # presence is described inside ``content`` itself, so
                # they intentionally don't appear here.
                #
                # The capability gates follow the *active model* (user /
                # /models metadata / name guess). When that model doesn't
                # advertise the modality we MUST NOT serialise the binary —
                # the upstream would either 4xx or hallucinate. The image
                # data is still preserved in history for UI rendering and
                # for re-enrichment if the user later switches models.
                has_image = allow_image and any(
                    (att.get("type") or "image") == "image" and (att.get("data_base64") or "")
                    for att in attachments
                )
                has_video = allow_video and any(
                    (att.get("type") or "").lower() == "video" and (att.get("data_base64") or "")
                    for att in attachments
                )
                has_audio = allow_audio and any(
                    (att.get("type") or "").lower() == "audio" and (att.get("data_base64") or "")
                    for att in attachments
                )
                if has_image or has_video or has_audio:
                    parts: list[dict[str, Any]] = []
                    text = content if isinstance(content, str) else ""
                    if text:
                        parts.append({"type": "text", "text": text})
                    for att in attachments:
                        kind = (att.get("type") or "image").lower()
                        data = att.get("data_base64") or ""
                        if not data:
                            continue
                        if kind == "image":
                            if not allow_image:
                                continue
                            mime = att.get("mime") or "image/png"
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"},
                            })
                        elif kind == "video":
                            if not allow_video:
                                continue
                            mime = att.get("mime") or "video/mp4"
                            # `video_url` is what the OpenAI-compatible
                            # gateways that actually take video (DashScope
                            # qwen-vl, Zhipu glm-4v, Gemini compat) expect.
                            parts.append({
                                "type": "video_url",
                                "video_url": {"url": f"data:{mime};base64,{data}"},
                            })
                        elif kind == "audio":
                            if not allow_audio:
                                continue
                            mime = att.get("mime") or "audio/wav"
                            fmt = mime.split("/")[-1].split(";")[0].strip() or "wav"
                            if fmt == "mpeg":
                                fmt = "mp3"
                            parts.append({
                                "type": "input_audio",
                                "input_audio": {"data": data, "format": fmt},
                            })
                    if parts:
                        content = parts
            content = _sanitize_openai_message_content(
                content,
                allow_image=allow_image,
                allow_audio=allow_audio,
                allow_video=allow_video,
            )
            item: dict[str, Any] = {"role": role, "content": content}
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            # Thinking-mode gateways (DashScope/Qwen) require prior
            # reasoning_content on follow-up turns. Skip only when thinking
            # is explicitly off, or extra_body opts out while thinking is unset.
            if replay_reasoning and m.get("reasoning_content"):
                item["reasoning_content"] = m["reasoning_content"]
            converted.append(item)
        return self._patch_openai_tool_adjacency(converted)

    @staticmethod
    def _patch_openai_tool_adjacency(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep OpenAI-style tool calls immediately followed by tool results.

        Some "OpenAI-compatible" gateways translate chat/completions payloads
        to Anthropic internally and enforce the stricter Anthropic invariant:
        every assistant tool_use must have matching tool_result blocks in the
        immediately following message. A cancelled or partially checkpointed
        session can leave an assistant tool_calls message without all of its
        role=tool results. Insert explicit tombstones so the provider accepts
        the history and the model can recover.
        """
        patched: list[dict[str, Any]] = []
        expected_ids: set[str] = set()
        i = 0
        inserted_tombstones = 0
        dropped_orphans = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role")
            if role == "tool" and expected_ids:
                tid = str(msg.get("tool_call_id") or "")
                if tid in expected_ids:
                    patched.append(msg)
                    expected_ids.discard(tid)
                i += 1
                continue
            if expected_ids:
                for tid in sorted(expected_ids):
                    patched.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": json.dumps(
                            {
                                "error": "missing_tool_result",
                                "reason": "previous tool call was interrupted before a result was recorded",
                            },
                            ensure_ascii=False,
                        ),
                    })
                    inserted_tombstones += 1
                expected_ids.clear()
            if role == "tool":
                # Drop orphaned tool results. Keeping them can make strict
                # providers reject the request because there is no adjacent
                # assistant tool_call to answer.
                dropped_orphans += 1
                i += 1
                continue
            patched.append(msg)
            tool_calls = msg.get("tool_calls") if role == "assistant" else None
            if isinstance(tool_calls, list):
                expected_ids = {str(tc.get("id") or "") for tc in tool_calls if tc.get("id")}
            i += 1
        if expected_ids:
            for tid in sorted(expected_ids):
                patched.append({
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": json.dumps(
                        {
                            "error": "missing_tool_result",
                            "reason": "previous tool call was interrupted before a result was recorded",
                        },
                        ensure_ascii=False,
                    ),
                })
                inserted_tombstones += 1
        if inserted_tombstones or dropped_orphans:
            log.warning(
                "ai.adjacency_patch inserted_tombstones=%d dropped_orphan_tool_results=%d input_msgs=%d output_msgs=%d",
                inserted_tombstones,
                dropped_orphans,
                len(messages),
                len(patched),
            )
        return patched


# ---------------------------------------------------------------------------
# Anthropic provider — native /v1/messages API
# ---------------------------------------------------------------------------

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(AIProvider):
    """Uses Anthropic's native /v1/messages endpoint."""

    def __init__(self, config: AIConfig) -> None:
        self.config = config
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["x-api-key"] = config.api_key
            # Some Anthropic-compatible gateways, including DeepSeek, accept
            # Bearer auth while preserving the Anthropic request schema.
            headers["Authorization"] = f"Bearer {config.api_key}"
        headers["anthropic-version"] = ANTHROPIC_VERSION
        headers = merge_ai_headers(
            headers,
            default_ai_client_headers(),
            config.extra_headers,
        )
        self._client = direct_async_client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=config.timeout,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        # Extract system message, convert roles
        system, converted = self._convert_messages(messages, include_thinking=thinking_enabled is not False)

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _apply_anthropic_cache_control(converted),
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        if system:
            body["system"] = _anthropic_system_with_cache(system)
        _apply_temperature(body, self.config, temperature)
        if tools:
            body["tools"] = self._convert_tools(tools)
        _merge_extra_body(body, self.config.extra_body)
        _apply_anthropic_thinking_preference(
            body,
            thinking_enabled,
            budget_tokens=int(getattr(self.config, "thinking_budget_tokens", 1024) or 1024),
            config=self.config,
        )
        self._disable_thinking_for_tool_context(body, converted)

        resp = await self._client.post("/messages", json=body)
        if resp.status_code == 400:
            await resp.aread()
            cap = _extract_output_token_cap(resp.text, int(body.get("max_tokens") or 0))
            if cap is not None and cap < int(body.get("max_tokens") or 0):
                log.warning(
                    "Anthropic max_tokens=%s exceeds model cap %s; retrying clamped.",
                    body.get("max_tokens"),
                    cap,
                )
                body["max_tokens"] = cap
                self.config.max_tokens = cap
                resp = await self._client.post("/messages", json=body)
        resp.raise_for_status()
        raw = resp.json()
        return self._normalize(raw, usage=raw.get("usage") if isinstance(raw, dict) else None)

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
        on_reasoning: Callable[[str], Awaitable[None] | None] | None = None,
        thinking_enabled: bool | None = None,
    ) -> dict[str, Any]:
        system, converted = self._convert_messages(messages, include_thinking=thinking_enabled is not False)
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": _apply_anthropic_cache_control(converted),
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "stream": True,
        }
        if system:
            body["system"] = _anthropic_system_with_cache(system)
        _apply_temperature(body, self.config, temperature)
        if tools:
            body["tools"] = self._convert_tools(tools)
        _merge_extra_body(body, self.config.extra_body)
        _apply_anthropic_thinking_preference(
            body,
            thinking_enabled,
            budget_tokens=int(getattr(self.config, "thinking_budget_tokens", 1024) or 1024),
            config=self.config,
        )
        self._disable_thinking_for_tool_context(body, converted)
        body["stream"] = True

        blocks: dict[int, dict[str, Any]] = {}
        stop_reason: str | None = None
        stop_details: dict[str, Any] | None = None
        usage_acc: dict[str, Any] = {}
        clamp_attempted = False
        while True:
            blocks = {}
            stop_reason = None
            stop_details = None
            usage_acc = {}
            async with self._client.stream("POST", "/messages", json=body) as resp:
                if resp.status_code == 400 and not clamp_attempted:
                    await resp.aread()
                    cap = _extract_output_token_cap(resp.text, int(body.get("max_tokens") or 0))
                    if cap is not None and cap < int(body.get("max_tokens") or 0):
                        log.warning(
                            "Anthropic stream max_tokens=%s exceeds model cap %s; retrying clamped.",
                            body.get("max_tokens"),
                            cap,
                        )
                        body["max_tokens"] = cap
                        self.config.max_tokens = cap
                        clamp_attempted = True
                        continue
                await _raise_for_status_with_body(resp)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")
                    idx = int(event.get("index", 0) or 0)
                    if etype == "message_start":
                        # Carries input_tokens / cache_creation_input_tokens /
                        # cache_read_input_tokens up front; output_tokens here
                        # is a running placeholder, overwritten by message_delta.
                        start_usage = (event.get("message") or {}).get("usage")
                        if isinstance(start_usage, dict):
                            usage_acc.update(start_usage)
                        continue
                    if etype == "message_delta":
                        delta = event.get("delta") or {}
                        if delta.get("stop_reason"):
                            stop_reason = str(delta.get("stop_reason") or "")
                        details = delta.get("stop_details")
                        if isinstance(details, dict):
                            stop_details = details
                        delta_usage = event.get("usage")
                        if isinstance(delta_usage, dict):
                            usage_acc.update(delta_usage)
                        continue
                    if etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            block.setdefault("input", {})
                            block["_partial_json"] = ""
                        blocks[idx] = block
                        continue

                    if etype != "content_block_delta":
                        continue

                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    block = blocks.setdefault(idx, {"type": "text", "text": ""})
                    if dtype == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            block["type"] = "text"
                            block["text"] = block.get("text", "") + text
                            if on_delta:
                                maybe = on_delta(text)
                                if hasattr(maybe, "__await__"):
                                    await maybe
                    elif dtype == "input_json_delta":
                        block["type"] = "tool_use"
                        block["_partial_json"] = block.get("_partial_json", "") + (delta.get("partial_json") or "")
                    elif dtype == "thinking_delta":
                        block["type"] = "thinking"
                        thinking_text = delta.get("thinking") or ""
                        block["thinking"] = block.get("thinking", "") + thinking_text
                        if thinking_text and on_reasoning:
                            maybe = on_reasoning(thinking_text)
                            if hasattr(maybe, "__await__"):
                                await maybe
                    elif dtype == "signature_delta":
                        block["type"] = "thinking"
                        block["signature"] = delta.get("signature") or block.get("signature", "")
            break

        content: list[dict[str, Any]] = []
        for idx in sorted(blocks):
            block = dict(blocks[idx])
            partial = block.pop("_partial_json", "")
            if block.get("type") == "tool_use" and partial:
                try:
                    block["input"] = json.loads(partial)
                except json.JSONDecodeError:
                    block["input"] = {}
            content.append(block)
        normalized = self._normalize(
            {
                "content": content,
                "stop_reason": stop_reason,
                "stop_details": stop_details,
            },
            usage=usage_acc or None,
        )
        # Refusal streams often emit no text deltas. Push the synthetic notice
        # through on_delta so the live bubble is not left blank.
        refusal_text = ""
        try:
            refusal_text = str(
                ((normalized.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
        except Exception:
            refusal_text = ""
        if (
            stop_reason == "refusal"
            and refusal_text
            and on_delta
            and not any(
                (blk.get("type") == "text" and str(blk.get("text") or "").strip())
                for blk in content
            )
        ):
            maybe = on_delta(refusal_text)
            if hasattr(maybe, "__await__"):
                await maybe
        return normalized

    async def analyze(self, system_prompt: str, user_content: str) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        try:
            result = await self.chat(messages)
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
            return ""
        except httpx.HTTPStatusError as e:
            log.error("Anthropic API error %s: %s", e.response.status_code, _safe_response_text(e.response)[:500])
            raise
        except Exception as e:
            log.error("Anthropic API call failed: %s", e)
            raise

    async def close(self) -> None:
        await self._client.aclose()

    def _convert_messages(self, messages: list[dict[str, Any]], include_thinking: bool = True) -> tuple[str, list[dict[str, Any]]]:
        """Convert OpenAI chat messages to Anthropic messages."""
        system = ""
        converted: list[dict[str, Any]] = []

        def append_message(role: str, content: Any) -> None:
            if not content:
                return
            if converted and converted[-1]["role"] == role:
                prev = converted[-1]["content"]
                if not isinstance(prev, list):
                    prev = [{"type": "text", "text": str(prev)}]
                if not isinstance(content, list):
                    content = [{"type": "text", "text": str(content)}]
                converted[-1]["content"] = prev + content
                return
            converted.append({"role": role, "content": content})

        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                system = (system + "\n\n" if system else "") + content
            elif role == "user":
                attachments = m.get("attachments") or []
                allow_image = bool(getattr(self.config, "image_in", True))
                if attachments and allow_image:
                    blocks: list[dict[str, Any]] = []
                    if isinstance(content, str) and content:
                        blocks.append({"type": "text", "text": content})
                    elif isinstance(content, list):
                        blocks.extend(content)
                    for att in attachments:
                        # Stills only. No Claude model accepts video or audio
                        # blocks, so video_in/audio_in never reach this rail.
                        if (att.get("type") or "image") != "image":
                            continue
                        data = att.get("data_base64") or ""
                        mime = att.get("mime") or "image/png"
                        if not data:
                            continue
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": data,
                            },
                        })
                    if not blocks:
                        blocks = [{"type": "text", "text": content if isinstance(content, str) else ""}]
                    append_message("user", blocks)
                else:
                    # Either no attachments, or the active Anthropic model
                    # doesn't advertise vision — keep text only. The image
                    # data still lives in history for UI/re-enrichment.
                    append_message("user", content)
            elif role == "assistant":
                provider_content = m.get("provider_content") or []
                if provider_content:
                    if not include_thinking:
                        provider_content = [b for b in provider_content if b.get("type") != "thinking"]
                    append_message("assistant", provider_content)
                    continue
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": args,
                    })
                append_message("assistant", blocks)
            elif role == "tool":
                append_message("user", [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": content,
                }])
        self._patch_orphan_tool_uses(converted)
        return system, converted

    @staticmethod
    def _patch_orphan_tool_uses(converted: list[dict[str, Any]]) -> None:
        """Ensure every ``tool_use`` is followed by a matching ``tool_result``.

        Anthropic strictly requires that any ``tool_use`` block in an assistant
        message has a corresponding ``tool_result`` block in the very next
        user message. If a previous session was cancelled, crashed, or had
        a tool result dropped for any other reason, the history can contain
        an orphan ``tool_use`` and the API will return 400. We synthesise
        a tombstone result for any such orphan so the model can still be
        replayed.

        Mutates ``converted`` in place. Idempotent.
        """
        i = 0
        while i < len(converted):
            msg = converted[i]
            if msg.get("role") != "assistant":
                i += 1
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                i += 1
                continue

            orphan_ids: list[tuple[str, str]] = []
            next_msg = converted[i + 1] if i + 1 < len(converted) else None
            next_results: set[str] = set()
            if next_msg and next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                tool_blocks: list[dict[str, Any]] = []
                other_blocks: list[Any] = []
                for blk in next_msg["content"]:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tool_blocks.append(blk)
                        rid = blk.get("tool_use_id") or ""
                        if rid:
                            next_results.add(rid)
                    else:
                        other_blocks.append(blk)
                if tool_blocks and other_blocks:
                    # Anthropic wants tool_result blocks at the start of the
                    # immediately-following user message. Prior UI decision
                    # text may have been merged into that same user message.
                    next_msg["content"] = tool_blocks + other_blocks

            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") != "tool_use":
                    continue
                tid = blk.get("id") or ""
                if not tid or tid in next_results:
                    continue
                orphan_ids.append((tid, blk.get("name") or "tool"))

            if not orphan_ids:
                i += 1
                continue

            synthetic = [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": f"[tombstone] No result was recorded for {name} (previous run cancelled or interrupted).",
                    "is_error": True,
                }
                for tid, name in orphan_ids
            ]
            if next_msg and next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
                # Prepend so tool_results come before any other content
                # in the user message that already follows.
                next_msg["content"] = synthetic + list(next_msg["content"])
                i += 2
            elif next_msg and next_msg.get("role") == "user":
                # Existing user message but content is a plain string. Lift
                # it into a list so we can prepend tombstones.
                lifted = [{"type": "text", "text": str(next_msg["content"])}] if next_msg["content"] else []
                next_msg["content"] = synthetic + lifted
                i += 2
            else:
                # No suitable next user message — insert a fresh one. If the
                # following message is *also* a user message (e.g. the next
                # turn starts with user text), the conversation will now
                # have two consecutive user roles. Merge them so Anthropic
                # sees a single message: tombstones first, original text
                # appended.
                converted.insert(i + 1, {"role": "user", "content": synthetic})
                if i + 2 < len(converted) and converted[i + 2].get("role") == "user":
                    later = converted.pop(i + 2)
                    later_content = later.get("content")
                    if isinstance(later_content, list):
                        converted[i + 1]["content"] = synthetic + list(later_content)
                    elif later_content:
                        converted[i + 1]["content"] = synthetic + [{"type": "text", "text": str(later_content)}]
                i += 2

    def _disable_thinking_for_tool_context(
        self,
        body: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        if (body.get("thinking") or {}).get("type") == "disabled":
            return
        has_tool_result = any(
            block.get("type") == "tool_result"
            for msg in messages
            for block in (msg.get("content") if isinstance(msg.get("content"), list) else [])
        )
        if not has_tool_result:
            return
        tool_use_messages = [
            msg
            for msg in messages
            if msg.get("role") == "assistant"
            and isinstance(msg.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in msg["content"]
            )
        ]
        can_replay_thinking = bool(tool_use_messages) and all(
            any(
                isinstance(block, dict) and block.get("type") == "thinking"
                for block in msg["content"]
            )
            for msg in tool_use_messages
        )
        if not can_replay_thinking:
            # Some Anthropic-compatible DeepSeek endpoints enable thinking by
            # default even when the request omits ``thinking``. A continued
            # tool chain is then rejected unless every original thinking block
            # is replayed. Explicitly disable it when history cannot satisfy
            # that contract.
            body["thinking"] = {"type": "disabled"}

    def _disable_thinking_for_tools(self, body: dict[str, Any]) -> None:
        if body.get("tools"):
            body["thinking"] = {"type": "disabled"}

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI tool format to Anthropic tool format."""
        converted = []
        for t in tools:
            fn = t.get("function", {})
            converted.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}, "required": []}),
            })
        return converted

    def _normalize(self, raw: dict[str, Any], *, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        """Convert Anthropic response to OpenAI-compatible format."""
        # Extract text content
        text = ""
        tool_calls: list[dict[str, Any]] = []
        for block in raw.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

        if not text.strip() and not tool_calls:
            refusal = _format_anthropic_refusal(
                raw.get("stop_reason"),
                raw.get("stop_details") if isinstance(raw.get("stop_details"), dict) else None,
            )
            if refusal:
                log.warning(
                    "Anthropic refusal stop_reason=%s details=%s",
                    raw.get("stop_reason"),
                    raw.get("stop_details"),
                )
                text = refusal

        choices = [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
                "tool_calls": tool_calls if tool_calls else None,
                "provider_content": raw.get("content", []),
            },
        }]
        result: dict[str, Any] = {"choices": choices}
        cache_usage = _normalize_anthropic_cache_usage(usage)
        if cache_usage is not None:
            result["_cache_usage"] = cache_usage
        return result


# ---------------------------------------------------------------------------
# Multi-endpoint provider with automatic failover
# ---------------------------------------------------------------------------

class FallbackProvider(AIProvider):
    """Tries endpoints in priority order; falls back on failure.

    Each endpoint is attempted up to ``_ENDPOINT_ATTEMPTS`` times for
    retryable failures (timeouts, connection errors, HTTP 429/5xx) with
    short exponential backoff, then the next endpoint is tried. Permanent
    client errors (401/402/403/404/…) skip remaining retries on that
    endpoint and fail over immediately.

    Sub-providers (and their underlying ``httpx.AsyncClient``) are created
    lazily on first use and cached for the lifetime of this instance, then
    released by :meth:`close`. A single agent turn issues many LLM calls (one
    per round); rebuilding the client — and thus paying a fresh TCP+TLS
    handshake — on every call was pure overhead.

    The cache is intentionally per-instance and never shared across event
    loops: an ``AsyncClient`` is bound to the loop it was created on, and each
    chat turn runs in its own loop. Owners must therefore call :meth:`close`
    when the turn (or one-shot operation) ends.
    """

    def __init__(self, endpoints: list[AIEndpoint]) -> None:
        endpoints = [ep for ep in endpoints if getattr(ep, "enabled", True)]
        if not endpoints:
            raise ValueError("at least one AIEndpoint is required")
        self._endpoints = sorted(endpoints, key=lambda e: e.priority, reverse=True)
        self._provider_cache: dict[int, AIProvider] = {}
        # Stamped by the agent loop so failover providers created later
        # inherit the current turn's thinking intensity.
        self.thinking_effort: str = DEFAULT_EFFORT
        self.thinking_budget_tokens: int = 1024

    @property
    def endpoints(self) -> list[AIEndpoint]:
        return list(self._endpoints)

    def _make_provider(self, ep: AIEndpoint) -> AIProvider:
        config = ep.to_ai_config()
        config.thinking_effort = normalize_thinking_effort(
            getattr(self, "thinking_effort", DEFAULT_EFFORT)
        )
        config.thinking_budget_tokens = int(
            getattr(self, "thinking_budget_tokens", 1024) or 1024
        )
        if config.protocol == "anthropic":
            return AnthropicProvider(config)
        return OpenAICompatibleProvider(config)

    @property
    def config(self):
        """Active (highest-priority) endpoint config, already resolved per model."""
        return self._provider_for(self._endpoints[0]).config

    def _provider_for(self, ep: AIEndpoint) -> AIProvider:
        """Return a cached sub-provider for ``ep``, building it on first use."""
        key = id(ep)
        provider = self._provider_cache.get(key)
        if provider is None:
            provider = self._make_provider(ep)
            self._provider_cache[key] = provider
        return provider

    async def _should_retry_endpoint(self, ep: AIEndpoint, attempt: int, exc: Exception, *, mode: str) -> bool:
        retryable = _is_retryable_ai_error(exc)
        will_retry = retryable and attempt + 1 < _ENDPOINT_ATTEMPTS
        log.warning(
            "AI endpoint %r (%s) %s failed (attempt %s/%s)%s: %s",
            ep.name,
            ep.base_url,
            mode,
            attempt + 1,
            _ENDPOINT_ATTEMPTS,
            ", retrying same endpoint" if will_retry else ", moving on",
            _describe_error(exc),
        )
        if will_retry:
            await asyncio.sleep(_retry_backoff_seconds(attempt))
        return will_retry

    async def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, thinking_enabled=None):
        last_error = None
        for ep in self._endpoints:
            provider = self._provider_for(ep)
            for attempt in range(_ENDPOINT_ATTEMPTS):
                try:
                    return await provider.chat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        thinking_enabled=thinking_enabled,
                    )
                except Exception as e:
                    last_error = e
                    if await self._should_retry_endpoint(ep, attempt, e, mode="chat"):
                        continue
                    break
        detail = _describe_error(last_error) if last_error else "unknown error"
        raise RuntimeError(f"All {len(self._endpoints)} AI endpoints failed: {detail}") from last_error

    async def stream_chat(self, messages, *, tools=None, temperature=None, max_tokens=None, on_delta=None, on_reasoning=None, thinking_enabled=None):
        last_error = None
        for ep in self._endpoints:
            provider = self._provider_for(ep)
            for attempt in range(_ENDPOINT_ATTEMPTS):
                try:
                    return await provider.stream_chat(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        on_delta=on_delta,
                        on_reasoning=on_reasoning,
                        thinking_enabled=thinking_enabled,
                    )
                except Exception as e:
                    log.warning(
                        "AI endpoint %r (%s) stream failed (attempt %s/%s): %s",
                        ep.name,
                        ep.base_url,
                        attempt + 1,
                        _ENDPOINT_ATTEMPTS,
                        _describe_error(e),
                    )
                    last_error = e
                    # Prefer retrying stream on transient errors; only fall back
                    # to non-stream when this endpoint's retries are exhausted
                    # or the error is permanent.
                    if _is_retryable_ai_error(e) and attempt + 1 < _ENDPOINT_ATTEMPTS:
                        await asyncio.sleep(_retry_backoff_seconds(attempt))
                        continue
                    try:
                        result = await provider.chat(
                            messages,
                            tools=tools,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            thinking_enabled=thinking_enabled,
                        )
                        if on_delta:
                            choices = result.get("choices", [])
                            text = choices[0].get("message", {}).get("content", "") if choices else ""
                            if text:
                                maybe = on_delta(text)
                                if hasattr(maybe, "__await__"):
                                    await maybe
                        return result
                    except Exception as chat_error:
                        last_error = chat_error
                        if await self._should_retry_endpoint(ep, attempt, chat_error, mode="non-stream fallback"):
                            continue
                        break
        detail = _describe_error(last_error) if last_error else "unknown error"
        raise RuntimeError(f"All {len(self._endpoints)} AI endpoints failed: {detail}") from last_error

    async def analyze(self, system_prompt: str, user_content: str) -> str:
        last_error = None
        for ep in self._endpoints:
            provider = self._provider_for(ep)
            for attempt in range(_ENDPOINT_ATTEMPTS):
                try:
                    return await provider.analyze(system_prompt, user_content)
                except Exception as e:
                    last_error = e
                    if await self._should_retry_endpoint(ep, attempt, e, mode="analyze"):
                        continue
                    break
        detail = _describe_error(last_error) if last_error else "unknown error"
        raise RuntimeError(f"All {len(self._endpoints)} AI endpoints failed: {detail}") from last_error

    async def close(self) -> None:
        providers = list(self._provider_cache.values())
        self._provider_cache.clear()
        for provider in providers:
            try:
                await provider.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_provider(
    endpoints: list[AIEndpoint] | None = None,
    override: AIConfig | None = None,
) -> AIProvider | None:
    """Create an AI provider.

    1. If ``override`` is given, returns a single provider (OA or Anthropic).
    2. If ``endpoints`` is non-empty, returns a FallbackProvider.
    3. Otherwise returns None.
    """
    if override is not None and override.base_url:
        if override.protocol == "anthropic":
            return AnthropicProvider(override)
        return OpenAICompatibleProvider(override)
    enabled_endpoints = [ep for ep in (endpoints or []) if getattr(ep, "enabled", True)]
    if enabled_endpoints:
        return FallbackProvider(enabled_endpoints)
    return None
