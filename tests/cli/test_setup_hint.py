"""First-run setup hint when AI endpoints are missing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from argusai.cli.setup_hint import (
    available_local_agents,
    build_setup_hint_lines,
    has_usable_ai_endpoint,
)


def test_has_usable_ai_endpoint() -> None:
    assert not has_usable_ai_endpoint(
        SimpleNamespace(ai_enabled=True, ai_endpoints=[])
    )
    assert not has_usable_ai_endpoint(
        SimpleNamespace(
            ai_enabled=True,
            ai_endpoints=[SimpleNamespace(enabled=True, api_key="", base_url="https://x")],
        )
    )
    assert has_usable_ai_endpoint(
        SimpleNamespace(
            ai_enabled=True,
            ai_endpoints=[
                SimpleNamespace(
                    enabled=True, api_key="sk-test", base_url="https://api.example.com"
                )
            ],
        )
    )


class _Api:
    def __init__(self, settings: Any, runtimes: list[dict[str, Any]]) -> None:
        self.settings_mgr = SimpleNamespace(get=lambda: settings)
        self._runtimes = runtimes

    def list_agent_runtimes(self) -> dict[str, Any]:
        return {"ok": True, "runtimes": self._runtimes}


def test_setup_hint_with_local_agent() -> None:
    settings = SimpleNamespace(ai_enabled=True, ai_endpoints=[])
    api = _Api(
        settings,
        [
            {
                "id": "cursor-acp",
                "name": "Cursor",
                "backend": "acp",
                "enabled": True,
                "probe": {"available": True},
            }
        ],
    )
    lines = build_setup_hint_lines(api)
    assert lines
    joined = "\n".join(lines)
    assert "Cursor" in joined or "本地" in joined or "Local" in joined
    assert available_local_agents(api)


def test_setup_hint_silent_when_endpoint_ok() -> None:
    settings = SimpleNamespace(
        ai_enabled=True,
        ai_endpoints=[
            SimpleNamespace(enabled=True, api_key="sk", base_url="https://x")
        ],
    )
    api = _Api(settings, [])
    assert build_setup_hint_lines(api) is None
