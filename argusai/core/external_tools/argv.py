"""Safe argv construction for domain-owned external CLI wrappers.

This module deliberately has no process runner. Domain wrappers own their
argument policy, working directory, timeout, scope checks, and subprocess
lifecycle; this layer only binds arguments to a registry-resolved executable.
"""

from __future__ import annotations

from collections.abc import Sequence

from .registry import ExternalToolRegistry, ProbeResult

MAX_ARG_COUNT = 512
MAX_ARG_LENGTH = 32_768
MAX_ARGV_CHARS = 256_000


class UnsafeArgvError(ValueError):
    """The wrapper supplied malformed or unreasonably large argv data."""


class ExternalToolUnavailable(RuntimeError):
    """The requested registered executable could not be resolved."""

    def __init__(self, blocked_result: dict[str, object]) -> None:
        super().__init__(str(blocked_result.get("error") or "external tool unavailable"))
        self.blocked_result = blocked_result


def argv_from_probe(
    result: ProbeResult,
    arguments: Sequence[str],
) -> tuple[str, ...]:
    """Bind validated argument strings to an already resolved executable."""
    if not result.available or not result.path:
        raise ExternalToolUnavailable(
            {
                "ok": False,
                "blocked": True,
                "do_not_retry": True,
                "error_code": result.error_code or "external_tool_unavailable",
                "external_tool_required": result.tool_id,
                "error": result.error or f"{result.tool_id} is unavailable",
                "external_tool": result.as_dict(),
            }
        )
    if isinstance(arguments, (str, bytes)):
        raise TypeError("arguments must be a sequence of strings, not one string")
    if len(arguments) > MAX_ARG_COUNT:
        raise UnsafeArgvError(f"argument count exceeds {MAX_ARG_COUNT}")
    normalized: list[str] = []
    total = len(result.path)
    for argument in arguments:
        if not isinstance(argument, str):
            raise TypeError("every argv item must be a string")
        if "\x00" in argument or "\r" in argument or "\n" in argument:
            raise UnsafeArgvError("argv contains a NUL or line-break control character")
        if len(argument) > MAX_ARG_LENGTH:
            raise UnsafeArgvError(f"one argument exceeds {MAX_ARG_LENGTH} characters")
        normalized.append(argument)
        total += len(argument)
    if total > MAX_ARGV_CHARS:
        raise UnsafeArgvError(f"combined argv exceeds {MAX_ARGV_CHARS} characters")
    return (result.path, *normalized)


def build_safe_argv(
    registry: ExternalToolRegistry,
    tool_id: str,
    arguments: Sequence[str],
) -> tuple[str, ...]:
    """Resolve a registered executable and return argv without executing it."""
    result = registry.detect(tool_id)
    if not result.available:
        raise ExternalToolUnavailable(registry.blocked_result(tool_id, probe_result=result))
    return argv_from_probe(result, arguments)
