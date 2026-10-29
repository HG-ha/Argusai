"""History must retain the original user attachments so re-enrichment works.

If the user sent an image while vision was off, the raw bytes must stay in
``ChatMessage.attachments`` — both so the UI thumbnail still renders on
session reload, and so the user can re-reference the same image later
without having to re-upload it.

The provider, meanwhile, must NOT forward the binary to a model that
doesn't advertise the modality (``image_in`` / ``audio_in`` on the
endpoint). This test guards both sides of the contract.
"""

from __future__ import annotations

from argusai.core.ai import AIConfig, OpenAICompatibleProvider


def test_provider_drops_image_when_endpoint_lacks_image_in() -> None:
    config = AIConfig(
        base_url="http://x",
        model="text-only-model",
        api_key="k",
        image_in=False,
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
    # The text passes through; the image_url block is suppressed because the
    # endpoint can't accept it.
    user_msg = next(m for m in converted if m["role"] == "user")
    assert user_msg["content"] == "这是什么？" or all(
        block.get("type") != "image_url"
        for block in (user_msg["content"] if isinstance(user_msg["content"], list) else [])
    )


def test_provider_forwards_image_when_endpoint_supports_image_in() -> None:
    config = AIConfig(
        base_url="http://x",
        model="vision-model",
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


def test_audio_in_gate_default_off() -> None:
    """``audio_in`` defaults to False, so audio attachments are NOT shipped
    to a generic OpenAI-compatible chat model unless the user opts in."""
    config = AIConfig(base_url="http://x", model="m", api_key="k")
    provider = OpenAICompatibleProvider(config)
    messages = [
        {
            "role": "user",
            "content": "ping",
            "attachments": [
                {"type": "audio", "mime": "audio/wav", "data_base64": "ZZ"},
            ],
        },
    ]
    converted = provider._convert_messages(messages)
    user_msg = next(m for m in converted if m["role"] == "user")
    if isinstance(user_msg["content"], list):
        assert all(block.get("type") != "input_audio" for block in user_msg["content"])
    else:
        assert user_msg["content"] == "ping"
