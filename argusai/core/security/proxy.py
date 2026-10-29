"""Passive proxy scanner — facade re-exporting split submodules.

Runs a local HTTP/HTTPS proxy that passively analyzes traffic for
vulnerabilities. Findings feed into the same Finding model and EventBus
as active scan checks.

Not a Check module — it is a standalone service managed by ProxyManager,
following the same thread+asyncio pattern as TaskManager.
"""

from __future__ import annotations

from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_constants import (
    _FLOW_BODY_PREVIEW,
    _MAX_FLOW_BODY_BYTES,
    _decode_response_body,
    _utcnow,
)
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow
from argusai.core.security.proxy_manager import ProxyManager
from argusai.core.security.proxy_server import ProxyServer
from argusai.core.security.proxy_system import (
    _PERSISTED_SYSTEM_PROXY_SNAPSHOTS,
    _atexit_restore_system_proxy,
    _clear_system_proxy_snapshot,
    _remember_system_proxy_snapshot,
)

__all__ = [
    "PassiveAnalyzer",
    "ProxyConfig",
    "ProxyManager",
    "ProxyServer",
    "TrafficFlow",
    "_PERSISTED_SYSTEM_PROXY_SNAPSHOTS",
    "_atexit_restore_system_proxy",
    "_clear_system_proxy_snapshot",
    "_decode_response_body",
    "_remember_system_proxy_snapshot",
    "_utcnow",
]

