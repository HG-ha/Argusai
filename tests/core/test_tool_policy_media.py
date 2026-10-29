"""Tool policy defaults and media enrichment helpers."""

from __future__ import annotations

from argusai.core.ai import AIEndpoint, AuxiliaryAIEndpoint
from argusai.core.assistant.tool_policy import (
    DEFAULT_TOOL_ALWAYS_ALLOW,
    apply_settings_tool_always_allow,
    normalize_tool_always_allow,
    tool_policy_tier,
    TOOL_POLICY_EXTENSION,
)
from argusai.core.assistant.media_auxiliary import (
    auxiliary_endpoint_configured,
    auxiliary_endpoint_enabled,
)
def test_default_tool_always_allow_includes_extension_tools() -> None:
    assert "desktop_observe" in DEFAULT_TOOL_ALWAYS_ALLOW
    assert "grep_text" in DEFAULT_TOOL_ALWAYS_ALLOW
    assert tool_policy_tier("desktop_observe") == TOOL_POLICY_EXTENSION
    assert tool_policy_tier("desktop_click") != TOOL_POLICY_EXTENSION


def test_normalize_tool_always_allow_merges_defaults() -> None:
    out = normalize_tool_always_allow(["grep_text"])
    assert "grep_text" in out
    assert "desktop_observe" in out


def test_ai_endpoint_modalities_guess() -> None:
    ep = AIEndpoint.from_dict({"model": "gpt-4o-mini", "base_url": "http://x"})
    assert ep.image_in is True
    ep2 = AIEndpoint.from_dict({"model": "deepseek-chat", "image_in": False})
    assert ep2.image_in is False


def test_auxiliary_endpoint_enabled() -> None:
    # Missing api_key — never enabled, even with the switch on.
    incomplete = AuxiliaryAIEndpoint(enabled=True, base_url="http://x", model="m")
    # Fully configured BUT the user left the switch off — must stay disabled.
    # The previous "auto-enable when credentials are complete" behavior was
    # confusing and led users to think the endpoint was off when in fact it
    # was silently active. WYSIWYG now: the switch is the source of truth.
    configured_off_switch = AuxiliaryAIEndpoint(
        enabled=False,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3-asr-flash",
        api_key="sk-test",
    )
    on = AuxiliaryAIEndpoint(enabled=True, base_url="http://x", model="m", api_key="k")
    assert not auxiliary_endpoint_enabled(incomplete)
    assert auxiliary_endpoint_configured(configured_off_switch)
    assert not auxiliary_endpoint_enabled(configured_off_switch)
    assert auxiliary_endpoint_enabled(on)


def test_app_settings_parses_auxiliary_and_tool_always_allow() -> None:
    raw = {
        "ai": {
            "auxiliary": {
                "vision": {"enabled": True, "base_url": "http://v", "model": "vision-1"},
                "asr": {"enabled": True, "base_url": "http://a", "model": "asr-1"},
            },
        },
        "assistant": {"tool_always_allow": ["grep_text"], "auto_approve": True},
    }
    from argusai.core.settings import _parse

    s = _parse(raw)
    assert s.ai_auxiliary_vision.enabled
    assert s.ai_auxiliary_vision.model == "vision-1"
    assert "grep_text" in s.assistant_tool_always_allow
    assert "desktop_observe" in s.assistant_tool_always_allow
    assert s.assistant_auto_approve is True


def test_desktop_os_settings_can_force_desktop_always_allow(monkeypatch) -> None:
    class Registry:
        _tools = {
            "desktop_observe": object(),
            "desktop_mouse_move": object(),
            "desktop_click": object(),
        }

    class Session:
        registry = Registry()

        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def set_auto_approve_tool(self, name: str, enabled: bool, force: bool = False) -> None:
            if enabled:
                self.calls.append((name, force))

    class Settings:
        assistant_tool_always_allow = [
            "desktop_mouse_move",
            "desktop_click",
        ]

    for platform in ("win32", "darwin"):
        monkeypatch.setattr("argusai.core.assistant.tool_policy.sys.platform", platform)
        session = Session()
        apply_settings_tool_always_allow(session, Settings())
        assert ("desktop_mouse_move", True) in session.calls
        assert ("desktop_click", True) in session.calls


def test_linux_settings_do_not_force_desktop_always_allow(monkeypatch) -> None:
    class Registry:
        _tools = {"desktop_click": object()}

    class Session:
        registry = Registry()

        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def set_auto_approve_tool(self, name: str, enabled: bool, force: bool = False) -> None:
            if enabled:
                self.calls.append((name, force))

    class Settings:
        assistant_tool_always_allow = ["desktop_click"]

    monkeypatch.setattr("argusai.core.assistant.tool_policy.sys.platform", "linux")
    session = Session()
    apply_settings_tool_always_allow(session, Settings())
    assert session.calls == []
