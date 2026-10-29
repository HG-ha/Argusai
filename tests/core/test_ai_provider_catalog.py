"""Provider catalog integrity tests.

Verifies the built-in preset directory is structurally sound: unique stable
ids, supported protocols, valid URL / auth / default-model shapes, no API keys
or promotional parameters, and only directly supported protocols.
"""

from __future__ import annotations

import json
import re

import pytest

from argusai.core.ai_provider_catalog import (
    CATEGORY_ORDER,
    CUSTOM_PROVIDER_ID,
    PROVIDER_PRESETS,
    find_provider_by_url,
    get_provider_preset,
    is_custom_provider,
    list_provider_presets,
    resolve_provider_id_for,
)

ALLOWED_PROTOCOLS = {"openai", "anthropic"}
SECRET_VALUE_PATTERN = re.compile(r"sk-[A-Za-z0-9]{12,}|Bearer\s+ey|password\s*[:=]|secret\s*[:=]", re.I)


def _all_presets():
    return list(PROVIDER_PRESETS.values())


def test_ids_are_unique_and_stable():
    ids = [p.id for p in _all_presets()]
    assert len(ids) == len(set(ids)), "provider ids must be unique"
    # ids remain stable identifiers (lowercase, dash-separated)
    for pid in ids:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pid), f"bad provider id: {pid!r}"


def test_every_preset_in_category_order():
    for p in _all_presets():
        assert p.category in CATEGORY_ORDER, f"{p.id}: unknown category {p.category}"
        # order is defined for all categories
        assert CATEGORY_ORDER.index(p.category) >= 0


def test_protocols_supported_only():
    for p in _all_presets():
        assert p.protocol in ALLOWED_PROTOCOLS, f"{p.id}: unsupported protocol {p.protocol}"
        assert p.auth.scheme in {"bearer", "x-api-key"}, f"{p.id}: bad auth {p.auth.scheme}"
        assert p.auth.header, f"{p.id}: missing auth header"


def test_url_and_console_well_formed():
    https = re.compile(r"^https://")
    for p in _all_presets():
        assert https.match(p.base_url), f"{p.id}: base_url must be https: {p.base_url}"
        assert https.match(p.website_url), f"{p.id}: website_url must be https"
        assert https.match(p.api_key_url), f"{p.id}: api_key_url must be https"
        if p.console_url:
            assert https.match(p.console_url), f"{p.id}: console_url must be https"


def test_default_model_required_except_templates():
    for p in _all_presets():
        assert p.default_model, f"{p.id}: direct preset needs a default_model"
        assert p.default_model in p.suggested_models
        assert p.model_list_strategy in {"api", "preset", "manual"}


def test_defaults_do_not_regress_to_retired_model_ids():
    retired = {
        "gpt-4o",
        "gpt-4.1",
        "deepseek-chat",
        "deepseek-reasoner",
        "glm-5.1",
        "ernie-4.0-8k",
        "ernie-4.5-8k-preview",
        "deepseek-ai/deepseek-v3.2",
        "moonshotai/kimi-k2.5",
    }
    configured = {
        model.casefold()
        for preset in _all_presets()
        for model in preset.suggested_models
    }
    assert configured.isdisjoint(retired)


def test_no_secrets_or_promotional_params():
    serialized = json.dumps([p.__dict__ for p in _all_presets()], default=str)
    assert not SECRET_VALUE_PATTERN.search(serialized), "catalog leaked a secret/credential-like token"
    for p in _all_presets():
        assert not getattr(p, "api_key", None), f"{p.id}: catalog must not store api_key"
    # Everything listed in base_url/console is a host; nothing is a call-to-action.
    assert "invite" not in serialized.lower()
    assert "referral" not in serialized.lower()


def test_no_oauth_bedrock_responses_or_native_gemini():
    for p in _all_presets():
        # No preset configures the unsupported surfaces as its transport.
        assert p.protocol in ALLOWED_PROTOCOLS
        assert "bedrock" not in (p.base_url or "").lower()
        assert "responses" not in (p.base_url or "").lower()
        assert "scope" not in p.auth.scheme  # OAuth not modeled
        assert p.auth.scheme in {"bearer", "x-api-key"}


def test_all_required_providers_present():
    required = {
        "anthropic", "openai", "google-openai", "deepseek", "zhipu-glm",
        "zhipu-glm-global", "ali-bailian", "kimi", "kimi-coding", "stepfun",
        "stepfun-global", "modelscope", "minimax", "minimax-global",
        "volcengine-agentplan", "byteplus-agentplan", "doubao-seed",
        "baidu-qianfan", "siliconflow-cn", "siliconflow-global", "openrouter",
        "novita", "nvidia-nim", "kat-coder", "longcat", "aihubmix", "dmxapi",
        "packycode", "apikey-fun", "apinebula", "atlascloud", "cubence",
        "aigocode", "aicodemirror", "claudecn", "runapi", "crazyrouter",
        "sssaicode", "micu", "etok", "shengsuanyun", "qiniu", "fennoai",
        "zetaapi", "teamorouter", "amux", "code0", "nekocode", "ccsub",
        "subrouter", "unity2", "opencode-go", "cherryin", "therouter", "pipellm",
    }
    present = {p.id for p in _all_presets()}
    missing = required - present
    assert not missing, f"missing required providers: {sorted(missing)}"
    assert len(present) >= 55


def test_custom_id_is_never_a_real_preset():
    assert CUSTOM_PROVIDER_ID == "custom"
    assert get_provider_preset(CUSTOM_PROVIDER_ID) is None
    assert is_custom_provider("") is True
    assert is_custom_provider(CUSTOM_PROVIDER_ID) is True
    assert is_custom_provider("openai") is False


def test_find_by_url_unique_match_only():
    assert find_provider_by_url("https://api.deepseek.com/v1").id == "deepseek"
    # trailing slash normalised
    assert find_provider_by_url("https://api.deepseek.com/v1/").id == "deepseek"
    # unknown endpoint -> None, not a guess
    assert find_provider_by_url("https://internal.corp/v1") is None
    assert find_provider_by_url("") is None


def test_resolve_provider_id_prefers_explicit():
    assert resolve_provider_id_for({"base_url": "https://x", "provider_id": "openai"}) == "openai"
    # explicit unknown id -> custom (never fabricates a provider)
    assert resolve_provider_id_for({"base_url": "https://x", "provider_id": "bogus"}) == "custom"
    # missing id -> unique URL migration
    assert resolve_provider_id_for({"base_url": "https://api.deepseek.com/v1"}) == "deepseek"
    # missing id + unknown URL -> custom
    assert resolve_provider_id_for({"base_url": "https://internal.corp/v1"}) == "custom"


def test_list_presets_is_stable_and_grouped_by_category():
    presets = list_provider_presets()
    assert len(presets) == len(PROVIDER_PRESETS)
    # sorted by category, featured-first, then name — must be deterministic
    keys = [
        (CATEGORY_ORDER.index(p.category), not p.featured, p.name.lower())
        for p in presets
    ]
    assert keys == sorted(keys)
