"""Per-model context / output limits: parse, merge, resolve, budget."""

from __future__ import annotations

from types import SimpleNamespace

from argusai.core.ai import AIEndpoint
from argusai.core.ai_model_limits import (
    SOURCE_API,
    SOURCE_USER,
    extract_model_limits_from_models_payload,
    extract_model_limits_from_record,
    merge_model_limits,
    resolve_endpoint_model_limits,
)
from argusai.core.assistant.session import AssistantSession


def test_extract_openrouter_and_vllm_fields():
    openrouter = extract_model_limits_from_record(
        {
            "id": "anthropic/claude-sonnet-4",
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 64000, "context_length": 200000},
        }
    )
    assert openrouter["context_window"] == 200000
    assert openrouter["max_tokens"] == 64000
    assert openrouter["source"] == SOURCE_API

    vllm = extract_model_limits_from_record({"id": "local-qwen", "max_model_len": 32768})
    assert vllm == {"context_window": 32768, "source": SOURCE_API}


def test_extract_models_payload_skips_id_only_rows():
    limits = extract_model_limits_from_models_payload(
        {
            "data": [
                {"id": "gpt-4o"},
                {"id": "kimi-k2", "context_length": 262144, "max_output_tokens": 8192},
            ]
        }
    )
    assert "gpt-4o" not in limits
    assert limits["kimi-k2"]["context_window"] == 262144
    assert limits["kimi-k2"]["max_tokens"] == 8192


def test_merge_does_not_overwrite_user():
    existing = {
        "kimi-k2": {"context_window": 128000, "max_tokens": 4096, "source": SOURCE_USER},
    }
    incoming = {
        "kimi-k2": {"context_window": 262144, "max_tokens": 8192, "source": SOURCE_API},
        "glm-5": {"context_window": 202752, "source": SOURCE_API},
    }
    merged = merge_model_limits(existing, incoming, incoming_source=SOURCE_API)
    assert merged["kimi-k2"]["context_window"] == 128000
    assert merged["kimi-k2"]["source"] == SOURCE_USER
    assert merged["glm-5"]["context_window"] == 202752


def test_resolve_uses_active_model_then_endpoint_fallback():
    ep = AIEndpoint.from_dict(
        {
            "model": "small",
            "models": ["small", "huge"],
            "context_window": 131072,
            "max_tokens": 16384,
            "model_limits": {
                "small": {"context_window": 8192, "max_tokens": 2048, "source": "api"},
                "huge": {"context_window": 1048576, "max_tokens": 65536, "source": "api"},
            },
        }
    )
    assert resolve_endpoint_model_limits(ep) == (8192, 2048)
    ep.model = "huge"
    assert resolve_endpoint_model_limits(ep) == (1048576, 65536)
    assert ep.to_ai_config().context_window == 1048576
    assert ep.to_ai_config().max_tokens == 65536
    ep.model = "unknown"
    assert resolve_endpoint_model_limits(ep) == (131072, 16384)


def test_roundtrip_preserves_model_limits():
    ep = AIEndpoint.from_dict(
        {
            "base_url": "https://internal.corp/v1",
            "model": "mine",
            "model_limits": {"mine": {"context_window": 32000, "source": "user"}},
        }
    )
    restored = AIEndpoint.from_dict(ep.to_dict())
    assert restored.model_limits["mine"]["context_window"] == 32000
    assert restored.model_limits["mine"]["source"] == SOURCE_USER


def test_session_budget_follows_active_model_window():
    session = AssistantSession()
    small = AIEndpoint.from_dict(
        {
            "enabled": True,
            "priority": 20,
            "model": "8k",
            "context_window": 131072,
            "model_limits": {"8k": {"context_window": 8192, "source": "api"}},
        }
    )
    session.registry = SimpleNamespace(
        api=SimpleNamespace(settings_mgr=SimpleNamespace(get=lambda: SimpleNamespace(ai_endpoints=[small])))
    )
    assert session._context_window_tokens() == 8192
    small.model = "missing"
    assert session._context_window_tokens() == 131072
