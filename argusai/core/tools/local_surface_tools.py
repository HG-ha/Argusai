"""Tool wrapper for local attack-surface snapshots."""

from __future__ import annotations

from typing import Any


def local_attack_surface_snapshot(
    api: Any,
    include_firewall: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Collect a read-only local listen-port / firewall snapshot (this machine only)."""
    from argusai.core.security.local_surface import collect_local_attack_surface

    try:
        return collect_local_attack_surface(include_firewall=bool(include_firewall))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
