"""Host and MIME filters for passive proxy capture."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from argusai.core.security.proxy import ProxyConfig

INTERCEPT_ALL = "all"
INTERCEPT_ALLOWLIST = "allowlist"
INTERCEPT_DENYLIST = "denylist"

LOCAL_BYPASS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def host_pattern_matches(host: str, pattern: str) -> bool:
    host = normalize_host(host)
    pattern = (pattern or "").strip().lower()
    if not pattern or pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:].lstrip(".")
        if not suffix:
            return False
        return host == suffix or host.endswith("." + suffix)
    if pattern.startswith("."):
        pattern = pattern[1:]
    return host == pattern or host.endswith("." + pattern)


def host_in_list(host: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    return any(host_pattern_matches(host, pat) for pat in patterns if pat)


def is_loopback_host(host: str) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    if h in LOCAL_BYPASS_HOSTS:
        return True
    if h.startswith("127."):
        return True
    if h == "localhost":
        return True
    return False


def should_bypass_capture(config: ProxyConfig, host: str) -> bool:
    if is_loopback_host(host) and not getattr(config, "allow_loopback", False):
        return True
    bypass = list(getattr(config, "bypass_hosts", None) or [])
    return host_in_list(host, bypass)


def should_capture_host(config: ProxyConfig, host: str) -> bool:
    """Decide whether a host should be captured.

    Precedence (highest first):
      1. ``bypass_hosts`` / loopback  → never capture
      2. ``deny_hosts``               → never capture (applies to every mode)
      3. ``intercept_mode`` + ``scope`` → mode-specific gate
    """
    if should_bypass_capture(config, host):
        return False
    mode = (getattr(config, "intercept_mode", INTERCEPT_ALL) or INTERCEPT_ALL).lower()
    scope = list(getattr(config, "scope", None) or [])
    deny_hosts = list(getattr(config, "deny_hosts", None) or [])

    if mode == INTERCEPT_DENYLIST:
        # In denylist mode ``scope`` is the deny list when ``deny_hosts`` is empty,
        # preserving the historical UX where one input drove everything.
        blocked = deny_hosts or scope
        return not host_in_list(host, blocked) if blocked else True

    # deny_hosts is honoured in ``all`` and ``allowlist`` modes.
    if deny_hosts and host_in_list(host, deny_hosts):
        return False

    if mode == INTERCEPT_ALLOWLIST:
        return host_in_list(host, scope) if scope else True

    # ``all`` — optional scope narrows capture.
    if scope and not host_in_list(host, scope):
        return False
    return True


def mime_pattern_matches(content_type: str, resource_kind: str, pattern: str) -> bool:
    pattern = (pattern or "").strip().lower()
    if not pattern:
        return False
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    rk = (resource_kind or "").strip().lower()
    if pattern.endswith("/*"):
        prefix = pattern[:-1]
        return ct.startswith(prefix)
    if "/" in pattern:
        return ct == pattern
    return rk == pattern or pattern in ct


def should_capture_mime(config: ProxyConfig, content_type: str, resource_kind: str) -> bool:
    mime_allow = list(getattr(config, "mime_allow", None) or [])
    if not mime_allow:
        return True
    return any(
        mime_pattern_matches(content_type, resource_kind, pat)
        for pat in mime_allow
        if pat
    )


def filter_config_summary(config: ProxyConfig) -> dict[str, Any]:
    return {
        "intercept_mode": getattr(config, "intercept_mode", INTERCEPT_ALL),
        "scope": list(getattr(config, "scope", None) or []),
        "deny_hosts": list(getattr(config, "deny_hosts", None) or []),
        "bypass_hosts": list(getattr(config, "bypass_hosts", None) or []),
        "mime_allow": list(getattr(config, "mime_allow", None) or []),
    }
