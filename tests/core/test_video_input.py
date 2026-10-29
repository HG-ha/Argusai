"""Native video input: capability gating, attachment intake, keyframe fallback."""

from __future__ import annotations

import asyncio
import base64

import pytest

from argusai.core.ai import (
    AIConfig,
    AIEndpoint,
    OpenAICompatibleProvider,
    _effective_native_video_in,
    _guess_video_in,
)
from argusai.core.assistant.attachments import (
    _ATTACHMENT_VIDEO_MAX_BYTES,
    _materialize_video,
    _sanitize_attachments,
)


def _video_attachment(data: str = "AAAA", mime: str = "video/mp4") -> dict:
    return {"type": "video", "mime": mime, "data_base64": data, "name": "repro.mp4"}


def _user_turn(**att) -> list[dict]:
    return [{"role": "user", "content": "看这个", "attachments": [_video_attachment(**att)]}]


# --- capability guessing -----------------------------------------------------

@pytest.mark.parametrize("model", ["gemini-2.5-pro", "qwen-vl-max", "glm-4v-plus", "qwen3-omni"])
def test_video_capable_models_are_recognised(model: str) -> None:
    assert _guess_video_in(model) is True


@pytest.mark.parametrize("model", ["gpt-4o", "claude-opus-4-8", "qwen3.7-max", "gpt-5-sol"])
def test_still_only_and_text_only_models_are_not_video_capable(model: str) -> None:
    """gpt-4o and Claude take images but reject video blocks outright."""
    assert _guess_video_in(model) is False


def test_auxiliary_force_multimodal_does_not_unlock_video() -> None:
    """force_multimodal exists for the vision/ASR aux endpoints, which never see clips."""
    config = AIConfig(model="some-private-vision-build", video_in=True, force_multimodal=True)
    assert _effective_native_video_in(config) is False


# --- OpenAI serialization ----------------------------------------------------

def test_video_capable_model_forwards_video_url_block() -> None:
    provider = OpenAICompatibleProvider(
        AIConfig(base_url="http://x", model="qwen-vl-max", api_key="k", video_in=True)
    )
    user_msg = next(m for m in provider._convert_messages(_user_turn()) if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    block = next(b for b in user_msg["content"] if b.get("type") == "video_url")
    assert block["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_image_only_model_never_receives_video_even_with_toggle_on() -> None:
    """gpt-4o sees stills; a video_url block would 4xx the whole request."""
    provider = OpenAICompatibleProvider(
        AIConfig(base_url="http://x", model="gpt-4o", api_key="k", image_in=True, video_in=True)
    )
    user_msg = next(m for m in provider._convert_messages(_user_turn()) if m["role"] == "user")
    assert "video_url" not in str(user_msg["content"])


def test_video_in_off_drops_the_block() -> None:
    provider = OpenAICompatibleProvider(
        AIConfig(base_url="http://x", model="qwen-vl-max", api_key="k", video_in=False)
    )
    user_msg = next(m for m in provider._convert_messages(_user_turn()) if m["role"] == "user")
    assert "video_url" not in str(user_msg["content"])


def test_sanitizer_strips_video_blocks_replayed_from_history() -> None:
    provider = OpenAICompatibleProvider(
        AIConfig(base_url="http://x", model="gpt-4o", api_key="k", image_in=True)
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA"}},
            ],
        },
    ]
    user_msg = next(m for m in provider._convert_messages(messages) if m["role"] == "user")
    assert user_msg["content"] == "hello"


def test_anthropic_never_serialises_video() -> None:
    from argusai.core.ai import AnthropicProvider

    provider = AnthropicProvider(
        AIConfig(base_url="http://x", model="claude-opus-4-8", api_key="k", image_in=True, video_in=True)
    )
    converted, _ = provider._convert_messages(_user_turn())
    assert "video" not in str(converted).replace("video/mp4", "")


# --- endpoint persistence ----------------------------------------------------

def test_endpoint_infers_video_in_from_model_name() -> None:
    assert AIEndpoint.from_dict({"model": "gemini-2.5-pro"}).video_in is True
    assert AIEndpoint.from_dict({"model": "gpt-4o"}).video_in is False


def test_explicit_video_in_survives_roundtrip() -> None:
    endpoint = AIEndpoint.from_dict({"model": "qwen-vl-max", "video_in": False})
    assert endpoint.video_in is False
    assert AIEndpoint.from_dict(endpoint.to_dict()).video_in is False


def test_video_capability_implies_image_capability() -> None:
    """No gateway takes clips but refuses stills; the keyframe fallback relies on this."""
    endpoint = AIEndpoint.from_dict({"model": "qwen-vl-max", "video_in": True, "image_in": False})
    assert endpoint.image_in is True
    assert endpoint.to_ai_config().image_in is True


def test_legacy_config_without_video_field_still_loads() -> None:
    legacy = {"model": "gpt-4o", "image_in": True, "modalities": {"text": True, "image_in": True}}
    assert AIEndpoint.from_dict(legacy).video_in is False


# --- attachment intake -------------------------------------------------------

def test_sanitize_accepts_video_and_keeps_duration() -> None:
    out = _sanitize_attachments([{**_video_attachment(), "duration_ms": "4200"}])
    assert out == [{
        "type": "video",
        "mime": "video/mp4",
        "data_base64": "AAAA",
        "name": "repro.mp4",
        "duration_ms": 4200,
    }]


def test_sanitize_rejects_unknown_video_mime() -> None:
    assert _sanitize_attachments([_video_attachment(mime="video/avi-ish")]) == []


def test_sanitize_rejects_oversized_video() -> None:
    oversized = "A" * (int(_ATTACHMENT_VIDEO_MAX_BYTES * 1.4) + 1)
    assert _sanitize_attachments([_video_attachment(data=oversized)]) == []


def test_video_larger_than_the_image_cap_is_still_accepted() -> None:
    """Video has its own budget — the 8 MiB image cap would reject any real clip."""
    payload = "A" * (12 * 1024 * 1024)
    assert len(_sanitize_attachments([_video_attachment(data=payload)])) == 1


def test_materialize_video_spills_to_sandbox_and_drops_base64(tmp_path) -> None:
    payload = base64.b64encode(b"\x00\x01binary").decode("ascii")
    out = _materialize_video([_video_attachment(data=payload)], tmp_path)
    assert "data_base64" not in out[0]
    assert out[0]["sandbox_rel"].startswith("_video/")
    written = tmp_path / "_video"
    assert [p.read_bytes() for p in written.iterdir()] == [b"\x00\x01binary"]


def test_materialize_video_leaves_other_kinds_untouched(tmp_path) -> None:
    image = {"type": "image", "mime": "image/png", "data_base64": "AAAA", "name": "a.png"}
    assert _materialize_video([image], tmp_path) == [image]


def test_materialize_video_without_sandbox_keeps_payload() -> None:
    out = _materialize_video([_video_attachment()], None)
    assert out[0]["data_base64"] == "AAAA"


# --- keyframe fallback -------------------------------------------------------

def test_keyframe_extraction_reports_missing_ffmpeg(monkeypatch) -> None:
    from argusai.core.assistant import video_frames

    monkeypatch.setattr(video_frames, "resolve_ffmpeg", lambda: None)
    result = asyncio.run(video_frames.extract_keyframes(data_base64="AAAA", mime="video/mp4"))
    assert result == {"ok": False, "reason": "no_ffmpeg", "error": "ffmpeg not found"}


def test_video_degrades_to_a_text_note_when_ffmpeg_is_absent(monkeypatch) -> None:
    from argusai.core.assistant import media_enrichment

    monkeypatch.setattr(media_enrichment, "ffmpeg_available", lambda: False)

    class _Settings:
        ai_endpoints = [AIEndpoint.from_dict({"model": "gpt-4o", "image_in": True, "enabled": True})]
        ai_auxiliary_vision = None
        ai_auxiliary_asr = None

    message, attachments = asyncio.run(
        media_enrichment.enrich_user_attachments(_Settings(), "看这个", [_video_attachment()])
    )
    assert attachments == []
    assert "ffmpeg" in message
    assert "repro.mp4" in message


def test_keyframes_ride_the_image_rail_for_still_only_models(monkeypatch) -> None:
    from argusai.core.assistant import media_enrichment

    monkeypatch.setattr(media_enrichment, "ffmpeg_available", lambda: True)

    async def _fake_extract(**_kwargs):
        return {
            "ok": True,
            "duration_s": 12.0,
            "frames": [
                {"data_base64": "F1", "mime": "image/jpeg", "label": "00:00"},
                {"data_base64": "F2", "mime": "image/jpeg", "label": "00:06"},
            ],
        }

    monkeypatch.setattr(media_enrichment, "extract_keyframes", _fake_extract)

    class _Settings:
        ai_endpoints = [AIEndpoint.from_dict({"model": "gpt-4o", "image_in": True, "enabled": True})]
        ai_auxiliary_vision = None
        ai_auxiliary_asr = None

    message, attachments = asyncio.run(
        media_enrichment.enrich_user_attachments(_Settings(), "看这个", [_video_attachment()])
    )
    assert [att["type"] for att in attachments] == ["image", "image"]
    assert all(att["derived"] == "video_frame" for att in attachments)
    assert "00:06" in message


def test_native_video_follows_model_name_even_when_endpoint_toggle_is_off(monkeypatch) -> None:
    from argusai.core.assistant import media_enrichment

    def _explode(**_kwargs):
        raise AssertionError("qwen-vl-max must keep the video rail after a stale endpoint toggle")

    monkeypatch.setattr(media_enrichment, "extract_keyframes", _explode)

    class _Settings:
        ai_endpoints = [AIEndpoint.from_dict({"model": "qwen-vl-max", "video_in": False, "enabled": True})]
        ai_auxiliary_vision = None
        ai_auxiliary_asr = None

    message, attachments = asyncio.run(
        media_enrichment.enrich_user_attachments(_Settings(), "看这个", [_video_attachment()])
    )
    assert [att["type"] for att in attachments] == ["video"]
    assert message == "看这个"


def test_native_video_endpoint_skips_keyframe_extraction(monkeypatch) -> None:
    from argusai.core.assistant import media_enrichment

    def _explode(**_kwargs):
        raise AssertionError("native video endpoints must not be degraded to stills")

    monkeypatch.setattr(media_enrichment, "extract_keyframes", _explode)

    class _Settings:
        ai_endpoints = [AIEndpoint.from_dict({"model": "qwen-vl-max", "video_in": True, "enabled": True})]
        ai_auxiliary_vision = None
        ai_auxiliary_asr = None

    message, attachments = asyncio.run(
        media_enrichment.enrich_user_attachments(_Settings(), "看这个", [_video_attachment()])
    )
    assert [att["type"] for att in attachments] == ["video"]
    assert message == "看这个"
