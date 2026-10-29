"""Vision capability awareness + the on-demand `vision_inspect_image` tool.

Covers the gap where a text-only main model would otherwise hallucinate
"I cannot see the image" even though an auxiliary vision endpoint is wired up,
and the tool that lets the agent actively read any conversation image.
"""

from __future__ import annotations

import asyncio
import types

from argusai.core.ai import (
    AIConfig,
    AIEndpoint,
    AuxiliaryAIEndpoint,
    _effective_native_image_in,
)
from argusai.core.assistant import session_tool_methods
from argusai.core.assistant.media_auxiliary import create_auxiliary_provider
from argusai.core.assistant.media_enrichment import vision_capability_note
from argusai.core.assistant.session_tool_methods import SessionToolMethodsMixin


def _text_endpoint() -> AIEndpoint:
    return AIEndpoint(enabled=True, priority=0, model="qwen3.7-max", image_in=False)


def _text_endpoint_image_toggle_on() -> AIEndpoint:
    # image_in toggled on by mistake, but the model name is not a vision model.
    return AIEndpoint(enabled=True, priority=0, model="qwen3.7-max", image_in=True)


def _vision_endpoint() -> AIEndpoint:
    return AIEndpoint(enabled=True, priority=0, model="qwen-vl-max", image_in=True)


def _aux(enabled: bool) -> AuxiliaryAIEndpoint:
    return AuxiliaryAIEndpoint(
        enabled=enabled,
        base_url="https://dashscope.example/v1" if enabled else "",
        model="qwen3-vl-flash" if enabled else "",
        api_key="k" if enabled else "",
    )


def _settings(*, chat: AIEndpoint, aux_enabled: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(ai_endpoints=[chat], ai_auxiliary_vision=_aux(aux_enabled))


# ---------------------------------------------------------------------------
# vision_capability_note
# ---------------------------------------------------------------------------


def test_capability_note_native_when_main_model_is_vision() -> None:
    note = vision_capability_note(_settings(chat=_vision_endpoint(), aux_enabled=False))
    assert "NATIVE" in note
    assert "not limited to the session sandbox" in note
    assert "vision_inspect_image(path=" in note


def test_capability_note_available_via_auxiliary_for_text_model() -> None:
    note = vision_capability_note(_settings(chat=_text_endpoint(), aux_enabled=True))
    assert "auxiliary vision endpoint" in note
    assert "vision_inspect_image" in note
    assert "qwen3-vl-flash" in note
    # Must explicitly forbid the "I can't see images" hallucination.
    assert "Never tell the user you cannot see" in note
    assert "vision_inspect_image(path=" in note
    assert "do not copy them into the sandbox" in note


def test_capability_note_unconfirmed_when_unknown_model_and_no_aux() -> None:
    note = vision_capability_note(_settings(chat=_text_endpoint(), aux_enabled=False))
    assert "UNCONFIRMED" in note
    assert "UNAVAILABLE" not in note
    assert "Do not invent pixel details" in note


def test_capability_note_unavailable_when_user_disables_vision() -> None:
    chat = AIEndpoint(
        enabled=True,
        priority=0,
        model="gpt-4o",
        image_in=True,
        model_capabilities={"gpt-4o": {"image_in": False, "source": "user"}},
    )
    note = vision_capability_note(_settings(chat=chat, aux_enabled=False))
    assert "UNAVAILABLE" in note
    assert "cannot see" in note


def test_capability_note_native_ignores_endpoint_toggle_for_vl_model() -> None:
    chat = AIEndpoint(enabled=True, priority=0, model="qwen-vl-max", image_in=False)
    note = vision_capability_note(_settings(chat=chat, aux_enabled=False))
    assert "NATIVE" in note


def test_capability_note_image_toggle_on_text_model_still_uses_aux() -> None:
    # The footgun case: image_in left on for a text-only model. We must NOT
    # claim NATIVE (the image would be stripped at serialization), and should
    # route through the auxiliary endpoint instead.
    note = vision_capability_note(_settings(chat=_text_endpoint_image_toggle_on(), aux_enabled=True))
    assert "NATIVE" not in note
    assert "auxiliary vision endpoint" in note


# ---------------------------------------------------------------------------
# Auxiliary provider must actually deliver the image to the vision model.
# Regression: a text-only AIConfig (image_in unset) routed through
# _convert_messages would strip the image_url, so the vision model answered
# "no image provided". The auxiliary provider must force multimodal on.
# ---------------------------------------------------------------------------


def test_force_multimodal_overrides_model_name_guess() -> None:
    cfg = AIConfig(base_url="http://x", model="house-model", api_key="k", force_multimodal=True)
    assert _effective_native_image_in(cfg) is True
    cfg_off = AIConfig(base_url="http://x", model="house-model", api_key="k")
    assert _effective_native_image_in(cfg_off) is False


def test_auxiliary_provider_preserves_image_block_for_unguessable_model() -> None:
    # Model name has no vision hint, so only force_multimodal keeps the image.
    ep = AuxiliaryAIEndpoint(enabled=True, base_url="http://x", model="house-model", api_key="k")
    provider = create_auxiliary_provider(ep)
    assert provider is not None
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        }
    ]
    converted = provider._convert_messages(messages)
    user_msg = next(m for m in converted if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(block.get("type") == "image_url" for block in user_msg["content"])


# ---------------------------------------------------------------------------
# vision_inspect_image_tool
# ---------------------------------------------------------------------------


class _Harness(SessionToolMethodsMixin):
    def __init__(self, history: list, settings) -> None:
        self.history = history
        self.registry = types.SimpleNamespace(
            api=types.SimpleNamespace(
                settings_mgr=types.SimpleNamespace(get=lambda: settings)
            )
        )


def _msg(*, hidden: bool, attachments: list) -> types.SimpleNamespace:
    return types.SimpleNamespace(role="user", hidden=hidden, attachments=attachments)


def _img(name: str) -> dict:
    return {"type": "image", "mime": "image/png", "data_base64": "QUJD", "name": name}


def test_inspect_returns_error_when_no_image_present() -> None:
    h = _Harness([_msg(hidden=False, attachments=[])], _settings(chat=_text_endpoint(), aux_enabled=True))
    out = asyncio.run(h.vision_inspect_image_tool())
    assert out["ok"] is False
    assert "没有找到" in out["error"]


def test_inspect_returns_disable_reason_when_aux_off() -> None:
    h = _Harness(
        [_msg(hidden=False, attachments=[_img("shot.png")])],
        _settings(chat=_text_endpoint(), aux_enabled=False),
    )
    out = asyncio.run(h.vision_inspect_image_tool())
    assert out["ok"] is False
    assert "视觉辅助端点" in out["error"]
    assert "设置" in out.get("next_step", "")


def test_inspect_describes_latest_image_via_auxiliary(monkeypatch) -> None:
    calls: dict = {}

    async def fake_describe(settings, *, data_base64, mime, user_hint=""):
        calls["data_base64"] = data_base64
        calls["user_hint"] = user_hint
        return {"ok": True, "description": "一个 Bandizip 报错对话框", "model": "qwen3-vl-flash"}

    monkeypatch.setattr(session_tool_methods, "vision_describe_image", fake_describe)
    h = _Harness(
        [_msg(hidden=False, attachments=[_img("error.png")])],
        _settings(chat=_text_endpoint(), aux_enabled=True),
    )
    out = asyncio.run(h.vision_inspect_image_tool(query="读出报错文字"))
    assert out["ok"] is True
    assert out["description"] == "一个 Bandizip 报错对话框"
    assert out["model"] == "qwen3-vl-flash"
    assert out["image_name"] == "error.png"
    assert out["source"] == "user"
    assert calls["user_hint"] == "读出报错文字"
    assert calls["data_base64"] == "QUJD"


def test_inspect_source_filter_picks_tool_screenshot(monkeypatch) -> None:
    seen: dict = {}

    async def fake_describe(settings, *, data_base64, mime, user_hint=""):
        seen["data_base64"] = data_base64
        return {"ok": True, "description": "desktop", "model": "m"}

    monkeypatch.setattr(session_tool_methods, "vision_describe_image", fake_describe)
    # A user image (visible) followed by a more-recent tool screenshot (hidden).
    user_img = _img("user.png")
    tool_img = dict(_img("desktop.png"), data_base64="WFla")
    history = [
        _msg(hidden=False, attachments=[user_img]),
        _msg(hidden=True, attachments=[tool_img]),
    ]
    settings = _settings(chat=_text_endpoint(), aux_enabled=True)

    # source=user must skip the hidden tool screenshot and pick the user image.
    out_user = asyncio.run(_Harness(history, settings).vision_inspect_image_tool(source="user"))
    assert out_user["ok"] is True
    assert out_user["source"] == "user"
    assert seen["data_base64"] == "QUJD"

    # source=tool must pick the hidden tool screenshot.
    out_tool = asyncio.run(_Harness(history, settings).vision_inspect_image_tool(source="tool"))
    assert out_tool["ok"] is True
    assert out_tool["source"] == "tool"
    assert seen["data_base64"] == "WFla"


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00"
    b"\x00\x04\x00\x01\xa3\n\x15\xe3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_inspect_reads_host_image_path_outside_sandbox(tmp_path, monkeypatch) -> None:
    host = tmp_path / "outside-sandbox.png"
    host.write_bytes(TINY_PNG)
    seen: dict = {}

    async def fake_describe(settings, *, data_base64, mime, user_hint=""):
        seen["data_base64"] = data_base64
        seen["mime"] = mime
        return {"ok": True, "description": "one pixel", "model": "m"}

    monkeypatch.setattr(session_tool_methods, "vision_describe_image", fake_describe)
    out = asyncio.run(
        _Harness([], _settings(chat=_text_endpoint(), aux_enabled=True)).vision_inspect_image_tool(
            path=str(host),
            query="what color",
        )
    )
    assert out["ok"] is True
    assert out["source"] == "file"
    assert out["image_name"] == "outside-sandbox.png"
    assert out["description"] == "one pixel"
    assert out["attached_to_model"] is True
    assert out["_assistant_attachments"][0]["type"] == "image"
    assert seen["mime"] == "image/png"
    assert seen["data_base64"]


def test_inspect_host_path_works_for_native_vision_without_aux(tmp_path) -> None:
    host = tmp_path / "desk.png"
    host.write_bytes(TINY_PNG)
    out = asyncio.run(
        _Harness([], _settings(chat=_vision_endpoint(), aux_enabled=False)).vision_inspect_image_tool(
            path=str(host),
        )
    )
    assert out["ok"] is True
    assert out["attached_to_model"] is True
    assert "description" not in out
    assert out["_assistant_attachments"][0]["name"] == "desk.png"


def test_inspect_reloads_spilled_attachment_path(tmp_path, monkeypatch) -> None:
    spilled = tmp_path / "spilled.png"
    spilled.write_bytes(TINY_PNG)
    att = {
        "type": "image",
        "mime": "image/png",
        "name": "spilled.png",
        "sandbox_path": str(spilled),
    }

    async def fake_describe(settings, *, data_base64, mime, user_hint=""):
        return {"ok": True, "description": "from disk", "model": "m"}

    monkeypatch.setattr(session_tool_methods, "vision_describe_image", fake_describe)
    out = asyncio.run(
        _Harness(
            [_msg(hidden=False, attachments=[att])],
            _settings(chat=_text_endpoint(), aux_enabled=True),
        ).vision_inspect_image_tool()
    )
    assert out["ok"] is True
    assert out["description"] == "from disk"
    assert out["image_name"] == "spilled.png"


def test_inspect_missing_path_does_not_fall_back_to_conversation_image() -> None:
    out = asyncio.run(
        _Harness(
            [_msg(hidden=False, attachments=[_img("user.png")])],
            _settings(chat=_text_endpoint(), aux_enabled=True),
        ).vision_inspect_image_tool(path="C:/definitely-missing/nope.png")
    )
    assert out["ok"] is False
    assert "找不到" in out["error"]
