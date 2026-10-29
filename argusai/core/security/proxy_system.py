"""Process-exit restoration of OS proxy settings."""

from __future__ import annotations

import atexit
import logging
from typing import Any

log = logging.getLogger("argusai.proxy")

_ATEXIT_REGISTERED = False
_PERSISTED_SYSTEM_PROXY_SNAPSHOTS: list[Any] = []

def _register_system_proxy_atexit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(_atexit_restore_system_proxy)
    _ATEXIT_REGISTERED = True


def _atexit_restore_system_proxy() -> None:
    """Best-effort restore of every snapshot still registered when we exit."""
    snapshots = list(_PERSISTED_SYSTEM_PROXY_SNAPSHOTS)
    if not snapshots:
        return
    try:
        from argusai.core.security.system_proxy import restore_system_proxy
    except Exception as exc:
        log.warning("Cannot import system_proxy on exit: %s", exc)
        _PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()
        return
    for snapshot in snapshots:
        try:
            restore_system_proxy(snapshot)
            log.info("Restored system proxy settings on process exit")
        except Exception as exc:
            log.warning("Failed to restore system proxy on exit: %s", exc)
    _PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()


def _remember_system_proxy_snapshot(snapshot: Any) -> None:
    if snapshot is None:
        return
    # Identity-based membership: SystemProxySnapshot is a mutable dataclass and
    # therefore not hashable, so we can't use a set here.
    for existing in _PERSISTED_SYSTEM_PROXY_SNAPSHOTS:
        if existing is snapshot:
            break
    else:
        _PERSISTED_SYSTEM_PROXY_SNAPSHOTS.append(snapshot)
    _register_system_proxy_atexit()


def _clear_system_proxy_snapshot(snapshot: Any | None) -> None:
    if snapshot is None:
        return
    for i, existing in enumerate(_PERSISTED_SYSTEM_PROXY_SNAPSHOTS):
        if existing is snapshot:
            _PERSISTED_SYSTEM_PROXY_SNAPSHOTS.pop(i)
            return
