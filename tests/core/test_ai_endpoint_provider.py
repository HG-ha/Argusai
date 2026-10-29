"""AIEndpoint provider_id compatibility migration + settings round-trip tests.

Covers: legacy configs without provider_id, custom endpoints, region variants,
and the guarantee that a preset prefill never overwrites an existing API key.
"""

from __future__ import annotations

from argusai.core.ai import AIEndpoint


def _endpoint(**over):
    base = {
        "name": "test",
        "base_url": "",
        "api_key": "k-secret",
        "model": "m",
        "protocol": "openai",
    }
    base.update(over)
    return base


def test_legacy_config_without_provider_id_migrates_by_url():
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://api.deepseek.com/v1"))
    assert ep.provider_id == "deepseek"
    assert ep.api_key == "k-secret"  # never lost during migration


def test_legacy_unknown_url_falls_back_to_custom():
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://internal.corp/v1"))
    assert ep.provider_id == "custom"


def test_explicit_known_provider_id_survives():
    ep = AIEndpoint.from_dict(_endpoint(base_url="", provider_id="openai"))
    assert ep.provider_id == "openai"


def test_explicit_unknown_provider_id_becomes_custom():
    ep = AIEndpoint.from_dict(_endpoint(base_url="", provider_id="not-a-real-preset"))
    assert ep.provider_id == "custom"


def test_region_variant_migrates_correctly():
    # Distinct regional URLs map to the same id but are unique matches.
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://api.siliconflow.cn/v1"))
    assert ep.provider_id == "siliconflow-cn"
    ep2 = AIEndpoint.from_dict(_endpoint(base_url="https://api.siliconflow.com/v1"))
    assert ep2.provider_id == "siliconflow-global"


def test_roundtrip_preserves_provider_id_and_api_key():
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://api.openai.com/v1", provider_id="openai", api_key="sk-abc"))
    out = ep.to_dict()
    assert out["provider_id"] == "openai"
    assert out["api_key"] == "sk-abc"
    # from our own serialization, nothing is re-resolved by accident
    ep2 = AIEndpoint.from_dict(out)
    assert ep2.provider_id == "openai"
    assert ep2.api_key == "sk-abc"


def test_empty_provider_id_resolved_on_load():
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://api.deepseek.com/v1", provider_id=""))
    assert ep.provider_id == "deepseek"


def test_custom_provider_id_preserves_all_fields():
    raw = _endpoint(base_url="https://internal.corp/v1", provider_id="custom",
                    model="my-model", max_tokens=4096, context_window=8192)
    ep = AIEndpoint.from_dict(raw)
    assert ep.provider_id == "custom"
    assert ep.model == "my-model"
    assert ep.max_tokens == 4096
    assert ep.context_window == 8192


def test_api_key_field_is_never_touched_by_provider_defaults():
    # Simulate a preset prefill applied only to non-credential fields.
    ep = AIEndpoint.from_dict(_endpoint(base_url="https://api.deepseek.com/v1", api_key="sk-keep-me"))
    # preset would prefill name/protocol/url/model etc., but never the key:
    ep.name = "DeepSeek"
    ep.model = "deepseek-chat"
    assert ep.api_key == "sk-keep-me"
    assert ep.provider_id == "deepseek"
