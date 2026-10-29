"""First-run / empty-config hints for the ops console."""

from __future__ import annotations

from typing import Any

from argusai.cli.locale import label


def has_usable_ai_endpoint(settings: Any) -> bool:
    if not getattr(settings, "ai_enabled", True):
        return False
    for ep in list(getattr(settings, "ai_endpoints", None) or []):
        if not getattr(ep, "enabled", True):
            continue
        key = str(getattr(ep, "api_key", "") or "").strip()
        base = str(getattr(ep, "base_url", "") or "").strip()
        if key and base:
            return True
    return False


def available_local_agents(api: Any) -> list[dict[str, Any]]:
    try:
        result = api.list_agent_runtimes()
    except Exception:
        return []
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    out: list[dict[str, Any]] = []
    for row in list(result.get("runtimes") or []):
        if not isinstance(row, dict):
            continue
        if not row.get("enabled", True):
            continue
        backend = str(row.get("backend") or "")
        if backend == "native":
            continue
        probe = row.get("probe") if isinstance(row.get("probe"), dict) else {}
        if not probe.get("available"):
            continue
        out.append(row)
    return out


def build_setup_hint_lines(api: Any) -> list[str] | None:
    """Return markup lines when built-in AI is unusable; else ``None``."""
    try:
        settings = api.settings_mgr.get()
    except Exception:
        return None
    if has_usable_ai_endpoint(settings):
        return None

    lines = [f"[warn]{label('setup_no_endpoint')}[/]"]
    agents = available_local_agents(api)
    if agents:
        names = ", ".join(
            str(a.get("name") or a.get("id") or "?")[:24] for a in agents[:5]
        )
        lines.append(label("setup_use_local_agent", names=names))
        lines.append(f"[hud.dim]{label('setup_hint_new')}[/]")
    else:
        lines.append(label("setup_configure_endpoint"))
        lines.append(f"[hud.dim]{label('setup_hint_agent_list')}[/]")
    lines.append(f"[hud.dim]{label('setup_hint_doctor')}[/]")
    return lines
