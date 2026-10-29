"""Per-session web-search toggle precedence.

The per-session toolbar toggle (``联网 开/关``) is the highest-priority source of
truth for whether ``web_search`` is available: an explicit per-session value must
win over the global ``assistant_enable_web_search`` default, which only seeds new
sessions. These tests pin that precedence at the registry layer (where the tool is
actually included/excluded) so a regression can't silently re-introduce the
"toolbar says 开 but backend is off" desync.
"""

from __future__ import annotations

from types import SimpleNamespace

from argusai.core.assistant.tools import build_tool_registry


def _api(global_enabled: bool) -> SimpleNamespace:
    settings = SimpleNamespace(assistant_enable_web_search=global_enabled)
    return SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: settings))


def _has_web_search(registry) -> bool:
    return any(t["name"] == "web_search" for t in registry.list_tools())


def test_session_override_on_beats_global_off() -> None:
    api = _api(global_enabled=False)
    registry = build_tool_registry(api, "sess", allow_web_search=True)
    assert _has_web_search(registry), "explicit per-session ON must win over global OFF"


def test_session_override_off_beats_global_on() -> None:
    api = _api(global_enabled=True)
    registry = build_tool_registry(api, "sess", allow_web_search=False)
    assert not _has_web_search(registry), "explicit per-session OFF must win over global ON"


def test_no_override_inherits_global_off() -> None:
    api = _api(global_enabled=False)
    registry = build_tool_registry(api, "sess", allow_web_search=None)
    assert not _has_web_search(registry), "no per-session opinion inherits the global default"


def test_no_override_inherits_global_on() -> None:
    api = _api(global_enabled=True)
    registry = build_tool_registry(api, "sess", allow_web_search=None)
    assert _has_web_search(registry), "no per-session opinion inherits the global default"
