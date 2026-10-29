"""Assistant-facing, read-only wireless tool executors."""

from __future__ import annotations

from typing import Any

from .common import *


def wireless_preflight(api: Any, platform_name: str = "", **kwargs: Any) -> dict[str, Any]:
    """Probe netsh/nmcli and tshark availability without touching an interface."""
    from argusai.core.security.wireless_tools import wireless_toolchain_preflight

    return wireless_toolchain_preflight(platform_name=platform_name)


wireless_environment_preflight = wireless_preflight


def wireless_wlan_inventory(
    api: Any,
    platform_name: str = "",
    timeout_seconds: int = 15,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read the OS WLAN inventory; never rescan, associate, disconnect, or inject."""
    from argusai.core.security.wireless_tools import wlan_inventory

    return wlan_inventory(
        platform_name=platform_name,
        timeout_seconds=_safe_int(timeout_seconds, 15, 1, 60),
    )


wlan_inventory = wireless_wlan_inventory


def wireless_parse_netsh_interfaces(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse saved ``netsh wlan show interfaces`` text offline."""
    from argusai.core.security.wireless_tools import parse_netsh_wlan_interfaces

    return parse_netsh_wlan_interfaces(text)


def wireless_parse_netsh_networks(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse saved ``netsh wlan show networks mode=bssid`` text offline."""
    from argusai.core.security.wireless_tools import parse_netsh_wlan_networks

    return parse_netsh_wlan_networks(text)


def wireless_parse_nmcli(api: Any, text: str = "", **kwargs: Any) -> dict[str, Any]:
    """Parse saved escaped ``nmcli --terse`` Wi-Fi output offline."""
    from argusai.core.security.wireless_tools import parse_nmcli_wifi

    return parse_nmcli_wifi(text)


def wireless_pcap_plan(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Build an offline-only 802.11 tshark plan for a session/local capture."""
    from argusai.core.security.wireless_tools import build_80211_pcap_plan

    try:
        capture = _resolve_session_input_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return build_80211_pcap_plan(capture)


plan_80211_pcap_analysis = wireless_pcap_plan


def wireless_action_check(api: Any, action: str = "", **kwargs: Any) -> dict[str, Any]:
    """Check one requested action against the strict wireless read-only policy."""
    from argusai.core.security.wireless_tools import wireless_action_policy

    return wireless_action_policy(action)


__all__ = [
    "plan_80211_pcap_analysis",
    "wireless_action_check",
    "wireless_environment_preflight",
    "wireless_parse_netsh_interfaces",
    "wireless_parse_netsh_networks",
    "wireless_parse_nmcli",
    "wireless_pcap_plan",
    "wireless_preflight",
    "wireless_wlan_inventory",
    "wlan_inventory",
]
