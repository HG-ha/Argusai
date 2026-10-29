"""Tests for proxy shutdown and system-proxy restoration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from argusai.core.security import proxy as proxy_mod
from argusai.core.security.proxy import ProxyManager
from argusai.core.security.system_proxy import SystemProxySnapshot


def test_needs_shutdown_when_system_proxy_active() -> None:
    mgr = ProxyManager()
    mgr._system_proxy_snapshot = SystemProxySnapshot(enabled=False, server="", platform="windows")
    assert mgr.needs_shutdown() is True


def _contains(snapshot: SystemProxySnapshot) -> bool:
    return any(existing is snapshot for existing in proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS)


def test_restore_system_proxy_clears_persisted_snapshot() -> None:
    proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()
    mgr = ProxyManager()
    snapshot = SystemProxySnapshot(enabled=True, server="127.0.0.1:8866", platform="windows")
    mgr._system_proxy_snapshot = snapshot
    proxy_mod._remember_system_proxy_snapshot(snapshot)
    assert _contains(snapshot)

    with patch("argusai.core.security.system_proxy.restore_system_proxy") as restore:
        mgr._restore_system_proxy()
        restore.assert_called_once_with(snapshot)

    assert mgr._system_proxy_snapshot is None
    assert not _contains(snapshot)


def test_stop_proxy_force_restores_even_without_running_server() -> None:
    proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()
    mgr = ProxyManager()
    snapshot = SystemProxySnapshot(enabled=True, server="127.0.0.1:8866", platform="windows")
    mgr._system_proxy_snapshot = snapshot
    proxy_mod._remember_system_proxy_snapshot(snapshot)

    with patch("argusai.core.security.system_proxy.restore_system_proxy") as restore:
        result = mgr.stop_proxy(force=True)
        assert result["ok"] is True
        restore.assert_called_once_with(snapshot)

    assert mgr._system_proxy_snapshot is None


def test_atexit_restore_system_proxy() -> None:
    proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()
    snapshot = SystemProxySnapshot(enabled=True, server="127.0.0.1:8866", platform="windows")
    proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS.append(snapshot)
    with patch("argusai.core.security.system_proxy.restore_system_proxy") as restore:
        proxy_mod._atexit_restore_system_proxy()
        restore.assert_called_once_with(snapshot)
    assert not proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS


def test_multiple_snapshots_all_restored_on_exit() -> None:
    proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS.clear()
    s1 = SystemProxySnapshot(enabled=True, server="127.0.0.1:8866", platform="windows")
    s2 = SystemProxySnapshot(enabled=False, server="", platform="windows")
    proxy_mod._remember_system_proxy_snapshot(s1)
    proxy_mod._remember_system_proxy_snapshot(s2)
    with patch("argusai.core.security.system_proxy.restore_system_proxy") as restore:
        proxy_mod._atexit_restore_system_proxy()
    assert restore.call_count == 2
    assert not proxy_mod._PERSISTED_SYSTEM_PROXY_SNAPSHOTS
