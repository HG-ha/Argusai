"""Per-model vision/video/audio: parse, merge, resolve across one endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from argusai.core.ai import AIEndpoint, OpenAICompatibleProvider
from argusai.core.ai_model_capabilities import (
    SOURCE_API,
    SOURCE_GUESS,
    SOURCE_USER,
    extract_model_capabilities_from_models_payload,
    extract_model_capabilities_from_record,
    merge_model_capabilities,
    resolve_endpoint_model_capabilities,
)


def test_extract_openrouter_input_modalities():
    row = extract_model_capabilities_from_record(
        {
            "id": "anthropic/claude-sonnet-4",
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
        }
    )
    assert row["image_in"] is True
    assert row["video_in"] is False
    assert row["source"] == SOURCE_API


def test_extract_text_only_modalities_are_explicit_no():
    row = extract_model_capabilities_from_record(
        {"id": "deepseek-chat", "architecture": {"input_modalities": ["text"]}}
    )
    assert row["image_in"] is False
    assert row["source"] == SOURCE_API


def test_extract_ollama_and_litellm_flags():
    ollama = extract_model_capabilities_from_record(
        {"id": "gemma4", "capabilities": ["completion", "vision"]}
    )
    assert ollama["image_in"] is True
    litellm = extract_model_capabilities_from_record(
        {"id": "gpt-4o", "supports_vision": True}
    )
    assert litellm["image_in"] is True


def test_extract_supported_parameters_image_url():
    row = extract_model_capabilities_from_record(
        {"id": "house-vl", "supported_parameters": ["temperature", "image_url"]}
    )
    assert row["image_in"] is True
    assert row["source"] == SOURCE_API
    video = extract_model_capabilities_from_record(
        {"id": "house-vl", "supported_parameters": ["video_url"]}
    )
    assert video["video_in"] is True
    assert video["image_in"] is True


def test_image_probe_response_is_decisive():
    from argusai.core.ai_model_capabilities import interpret_image_probe_response

    assert interpret_image_probe_response(200, "") is True
    assert interpret_image_probe_response(400, "This model does not support image") is False
    assert interpret_image_probe_response(400, "Unexpected item type in content") is False
    assert interpret_image_probe_response(400, "max_tokens is too large") is None
    assert interpret_image_probe_response(401, "invalid api key") is None


def test_video_probe_response_is_decisive():
    from argusai.core.ai_model_capabilities import (
        build_video_probe_payload,
        interpret_video_probe_response,
    )

    assert build_video_probe_payload("claude-opus-4-8", protocol="anthropic") is None
    assert interpret_video_probe_response(200, "") is True
    assert interpret_video_probe_response(400, "This model does not support video") is False
    assert interpret_video_probe_response(400, "Unexpected item type in content") is False
    assert interpret_video_probe_response(400, "Failed to decode video") is True
    assert interpret_video_probe_response(401, "invalid api key") is None


def test_learned_probe_outranks_name_guess():
    ep = AIEndpoint.from_dict(
        {
            "model": "house-model",
            "model_capabilities": {"house-model": {"image_in": True, "source": "learned"}},
        }
    )
    caps = resolve_endpoint_model_capabilities(ep)
    assert caps["image_in"] is True
    assert caps["sources"]["image_in"] == "learned"


def test_extract_payload_skips_id_only_rows():
    caps = extract_model_capabilities_from_models_payload(
        {
            "data": [
                {"id": "plain"},
                {
                    "id": "claude-sonnet-4",
                    "architecture": {"input_modalities": ["text", "image"]},
                },
            ]
        }
    )
    assert "plain" not in caps
    assert caps["claude-sonnet-4"]["image_in"] is True


def test_merge_does_not_overwrite_user():
    existing = {"house-vl": {"image_in": True, "source": SOURCE_USER}}
    incoming = {
        "house-vl": {"image_in": False, "source": SOURCE_API},
        "gpt-4o": {"image_in": True, "source": SOURCE_API},
    }
    merged = merge_model_capabilities(existing, incoming, incoming_source=SOURCE_API)
    assert merged["house-vl"]["image_in"] is True
    assert merged["house-vl"]["source"] == SOURCE_USER
    assert merged["gpt-4o"]["image_in"] is True


def test_resolve_follows_active_model_not_endpoint_toggle():
    ep = AIEndpoint.from_dict(
        {
            "model": "qwen3.7-max",
            "models": ["qwen3.7-max", "qwen-vl-max"],
            "image_in": False,
        }
    )
    text = resolve_endpoint_model_capabilities(ep)
    assert text["image_in"] is False
    assert text["source"] == SOURCE_GUESS
    ep.model = "qwen-vl-max"
    vision = resolve_endpoint_model_capabilities(ep)
    assert vision["image_in"] is True
    assert vision["video_in"] is True
    assert ep.to_ai_config().image_in is True
    assert ep.to_ai_config().video_in is True


def test_user_override_unlocks_custom_named_vision_model():
    ep = AIEndpoint.from_dict(
        {
            "model": "house-vl",
            "image_in": False,
            "model_capabilities": {"house-vl": {"image_in": True, "source": SOURCE_USER}},
        }
    )
    caps = resolve_endpoint_model_capabilities(ep)
    assert caps["image_in"] is True
    assert caps["sources"]["image_in"] == SOURCE_USER
    cfg = ep.to_ai_config()
    provider = OpenAICompatibleProvider(cfg)
    converted = provider._convert_messages(
        [
            {
                "role": "user",
                "content": "看图",
                "attachments": [{"type": "image", "mime": "image/png", "data_base64": "AAAA"}],
            }
        ]
    )
    user_msg = next(m for m in converted if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(block.get("type") == "image_url" for block in user_msg["content"])


def test_api_can_mark_a_guessed_vision_model_text_only():
    ep = AIEndpoint.from_dict(
        {
            "model": "gpt-4o",
            "image_in": True,
            "model_capabilities": {"gpt-4o": {"image_in": False, "source": SOURCE_API}},
        }
    )
    caps = resolve_endpoint_model_capabilities(ep)
    assert caps["image_in"] is False
    assert caps["sources"]["image_in"] == SOURCE_API
    assert ep.to_ai_config().image_in is False


def test_official_deepseek_models_are_treated_as_vision():
    from argusai.core.ai import _guess_image_in

    assert _guess_image_in("deepseek-flash") is True
    assert _guess_image_in("deepseek-v4-flash") is True
    assert _guess_image_in("deepseek-v4-flash-vision-exp") is True
    assert _guess_image_in("deepseek-chat") is False

    official = AIEndpoint.from_dict(
        {
            "provider_id": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        }
    )
    assert resolve_endpoint_model_capabilities(official)["image_in"] is True
    official.model = "deepseek-flash"
    assert official.to_ai_config().image_in is True


def test_official_deepseek_pro_follows_flash_reroute_date(monkeypatch):
    from argusai.core import ai_model_capabilities as caps

    ep = AIEndpoint.from_dict(
        {
            "provider_id": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
        }
    )
    monkeypatch.setattr(caps, "_DEEPSEEK_PRO_VISION_ON", datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert resolve_endpoint_model_capabilities(ep)["image_in"] is False
    monkeypatch.setattr(caps, "_DEEPSEEK_PRO_VISION_ON", datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert resolve_endpoint_model_capabilities(ep)["image_in"] is True


def test_third_party_deepseek_weights_are_not_auto_vision():
    replica = AIEndpoint.from_dict(
        {
            "provider_id": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "deepseek-ai/DeepSeek-V3",
        }
    )
    assert resolve_endpoint_model_capabilities(replica)["image_in"] is False


def test_roundtrip_preserves_model_capabilities():
    ep = AIEndpoint.from_dict(
        {
            "model": "mine",
            "model_capabilities": {"mine": {"image_in": True, "source": SOURCE_USER}},
        }
    )
    restored = AIEndpoint.from_dict(ep.to_dict())
    assert restored.model_capabilities["mine"]["image_in"] is True
    assert restored.model_capabilities["mine"]["source"] == SOURCE_USER
