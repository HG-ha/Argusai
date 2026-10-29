"""GUI bridge tests for the provider preset API and model-list auth adaptation.

- ``list_ai_provider_presets`` is read-only, grouped, and exposes no secrets.
- ``list_ai_models`` picks the right auth header from the provider preset,
  probes every endpoint that has a base URL rather than trusting the
  catalogue's guess, falls back to suggested models when no catalogue comes
  back, and does not treat a fetch failure as an unusable endpoint.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from argusai.gui.ai_endpoints import AIEndpointsMixin


def test_list_presets_grouped_and_read_only():
    r = AIEndpointsMixin().list_ai_provider_presets()
    assert r["ok"] is True
    assert len(r["presets"]) >= 55
    assert r["custom_id"] == "custom"
    cat_ids = [c["id"] for c in r["categories"]]
    assert cat_ids == ["official", "cn_official", "aggregator", "third_party", "custom"]
    # no credential *field* is ever serialized (console "api-keys" URLs are fine)
    assert all("api_key" not in p for p in r["presets"])
    assert all("api_key_url" in p and "website_url" in p for p in r["presets"])
    serialized = json.dumps(r)
    for bad in ('"api_key"', "sk-", "Bearer ey", "password", '"secret"'):
        assert bad not in serialized


def _fake_client(handler, captured=None):
    """Patch the shared async client so one ``GET /models`` reaches ``handler``."""
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            if captured is not None:
                captured["url"] = url
                captured["headers"] = headers
            return handler(url, headers)

    return patch(
        "argusai.core.infra.httpx_client.direct_async_client",
        side_effect=lambda **k: FakeClient(),
    )


def test_a_preset_strategy_provider_is_still_probed():
    """Most 'preset' entries are OpenAI-compatible relays that do serve /models.

    Skipping the probe on the catalogue's word alone told users their provider
    could not list models when it could.
    """
    import httpx

    captured: dict = {}

    def handler(url, _headers):
        return httpx.Response(
            200,
            json={"data": [{"id": "glm-5"}, {"id": "glm-5-air"}]},
            request=httpx.Request("GET", url),
        )

    with _fake_client(handler, captured):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "zhipu-glm",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "k",
        })

    assert r["source"] == "api"
    assert r["models"] == ["glm-5", "glm-5-air"]
    assert r.get("model_limits") == {}
    assert not r.get("manual")
    assert captured["url"].endswith("/models")


def test_anthropic_sends_the_version_header_it_demands():
    import httpx

    captured: dict = {}

    def handler(url, _headers):
        return httpx.Response(
            200, json={"data": [{"id": "claude-sonnet-5"}]}, request=httpx.Request("GET", url),
        )

    with _fake_client(handler, captured):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "anthropic",
            "protocol": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "sk-ant",
        })

    assert captured["headers"]["anthropic-version"]
    assert captured["headers"]["x-api-key"] == "sk-ant"
    assert r["models"] == ["claude-sonnet-5"]


def test_a_probe_that_finds_nothing_still_offers_the_curated_list():
    import httpx

    def handler(url, _headers):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    with _fake_client(handler):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "",
        })

    assert r["ok"] is True
    assert r["manual"] is True
    assert r["models"]


def test_a_local_template_is_not_probed():
    with patch("argusai.core.infra.httpx_client.direct_async_client") as mock_http:
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "anthropic",
            "base_url": "",
            "api_key": "",
        })
    assert r["ok"] is True
    assert r["manual"] is True
    assert r["source"] == "preset"
    assert r["models"]
    mock_http.assert_not_called()


def test_openai_preset_uses_bearer_auth_and_fetches_models():
    import httpx

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            assert headers["Authorization"] == "Bearer sk-test"
            return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "b-model"}]},
                                  request=httpx.Request("GET", url))

    with patch("argusai.core.infra.httpx_client.direct_async_client", side_effect=lambda **k: FakeClient()):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        })
    assert r["ok"] is True
    assert r["source"] == "api"
    assert r["models"] == ["b-model", "gpt-4o"]
    assert captured["url"].endswith("/models")
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert str(captured["headers"].get("User-Agent") or "").startswith("ArgusAI/")


def test_models_fetch_keeps_context_and_output_fields():
    import httpx

    def handler(url, _headers):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "plain"},
                    {
                        "id": "kimi-k2",
                        "context_length": 262144,
                        "top_provider": {"max_completion_tokens": 8192},
                        "architecture": {"input_modalities": ["text", "image", "video"]},
                    },
                ]
            },
            request=httpx.Request("GET", url),
        )

    with _fake_client(handler):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "custom",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "k",
        })

    assert r["source"] == "api"
    assert r["models"] == ["kimi-k2", "plain"]
    assert r["model_limits"]["kimi-k2"]["context_window"] == 262144
    assert r["model_limits"]["kimi-k2"]["max_tokens"] == 8192
    assert "plain" not in r["model_limits"]
    assert r["model_capabilities"]["kimi-k2"]["image_in"] is True
    assert r["model_capabilities"]["kimi-k2"]["video_in"] is True
    assert "plain" not in r["model_capabilities"]


def test_models_fetch_404_falls_back_not_hard_error():
    import httpx

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            resp = httpx.Response(404, request=httpx.Request("GET", url))
            resp.request = httpx.Request("GET", url)
            return resp

    with patch("argusai.core.infra.httpx_client.direct_async_client", side_effect=lambda **k: FakeClient()):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "custom",
            "base_url": "https://internal.corp/v1",
            "api_key": "",
        })
    assert r["ok"] is True
    assert r["manual"] is True
    assert r["source"] == "error"
    assert r.get("status") == 404
    # "custom" has no preset behind it, so there is nothing curated to offer.
    assert r["models"] == []


def test_models_fetch_probes_current_model_image_support():
    import httpx

    posts: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(
                200,
                json={"data": [{"id": "house-model"}]},
                request=httpx.Request("GET", url),
            )

        async def post(self, url, headers=None, json=None):
            posts.append(url)
            return httpx.Response(
                400,
                json={"error": {"message": "This model does not support image"}},
                request=httpx.Request("POST", url),
            )

    with patch("argusai.core.infra.httpx_client.direct_async_client", side_effect=lambda **k: FakeClient()):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "custom",
            "base_url": "https://internal.corp/v1",
            "api_key": "k",
            "model": "house-model",
        })

    assert r["ok"] is True
    assert posts == ["https://internal.corp/v1/chat/completions"]
    assert r["model_capabilities"]["house-model"]["image_in"] is False
    assert r["model_capabilities"]["house-model"]["video_in"] is False
    assert r["model_capabilities"]["house-model"]["source"] == "learned"


def test_models_fetch_probes_video_after_image_succeeds():
    import httpx

    kinds: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(
                200,
                json={"data": [{"id": "gemini-2.5-pro"}]},
                request=httpx.Request("GET", url),
            )

        async def post(self, url, headers=None, json=None):
            content = ((json or {}).get("messages") or [{}])[0].get("content") or []
            part = next((block.get("type") for block in content if block.get("type") != "text"), "")
            kinds.append(part)
            if part == "video_url":
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "ok"}}]},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
                request=httpx.Request("POST", url),
            )

    with patch("argusai.core.infra.httpx_client.direct_async_client", side_effect=lambda **k: FakeClient()):
        r = AIEndpointsMixin().list_ai_models({
            "provider_id": "custom",
            "base_url": "https://internal.corp/v1",
            "api_key": "k",
            "model": "gemini-2.5-pro",
        })

    assert kinds == ["image_url", "video_url"]
    assert r["model_capabilities"]["gemini-2.5-pro"]["image_in"] is True
    assert r["model_capabilities"]["gemini-2.5-pro"]["video_in"] is True
