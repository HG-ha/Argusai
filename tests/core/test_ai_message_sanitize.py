"""OpenAI-compatible message sanitization for multimodal gateways."""

from __future__ import annotations

from argusai.core.ai import AIConfig, OpenAICompatibleProvider


def test_qwen_text_model_drops_image_even_when_image_in_enabled() -> None:
    """DashScope qwen3.x rejects image_url — user toggle must not override."""
    config = AIConfig(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.7-max",
        api_key="k",
        image_in=True,
    )
    provider = OpenAICompatibleProvider(config)
    messages = [
        {
            "role": "user",
            "content": "报错了",
            "attachments": [
                {"type": "image", "mime": "image/png", "data_base64": "AAAA"},
            ],
        },
    ]
    converted = provider._convert_messages(messages)
    user_msg = next(m for m in converted if m["role"] == "user")
    assert user_msg["content"] == "报错了"
    assert "image_url" not in str(user_msg["content"])


def test_glm45v_and_omni_names_are_treated_as_vision() -> None:
    from argusai.core.ai import _guess_image_in

    assert _guess_image_in("glm-4.5v") is True
    assert _guess_image_in("qwen3-omni") is True
    assert _guess_image_in("claude-haiku-4-5") is True
    assert _guess_image_in("qwen3.7-max") is False


def test_vision_model_forwards_image_when_image_in_enabled() -> None:
    config = AIConfig(
        base_url="http://x",
        model="qwen-vl-max",
        api_key="k",
        image_in=True,
    )
    provider = OpenAICompatibleProvider(config)
    messages = [
        {
            "role": "user",
            "content": "这是什么？",
            "attachments": [
                {"type": "image", "mime": "image/png", "data_base64": "AAAA"},
            ],
        },
    ]
    converted = provider._convert_messages(messages)
    user_msg = next(m for m in converted if m["role"] == "user")
    assert isinstance(user_msg["content"], list)
    assert any(block.get("type") == "image_url" for block in user_msg["content"])


def test_sanitize_strips_unknown_content_blocks_from_history() -> None:
    config = AIConfig(base_url="http://x", model="qwen3.7-max", api_key="k")
    provider = OpenAICompatibleProvider(config)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ],
        },
    ]
    converted = provider._convert_messages(messages)
    user_msg = next(m for m in converted if m["role"] == "user")
    assert user_msg["content"] == "hello"
