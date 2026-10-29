"""CRUD helpers for chat AI endpoints (CLI / settings.json)."""

from __future__ import annotations

from pathlib import Path

from argusai.gui.ai_endpoints import AIEndpointsMixin


class _Harness(AIEndpointsMixin):
    def __init__(self, path: Path) -> None:
        from argusai.core.settings import SettingsManager

        self.settings_mgr = SettingsManager(path)


def test_save_list_activate_delete_ai_endpoint(tmp_path: Path) -> None:
    api = _Harness(tmp_path / "settings.json")

    saved = api.save_ai_endpoint(
        {
            "provider_id": "ali-bailian",
            "api_key": "sk-test-key-123456",
            "name": "百炼",
        }
    )
    assert saved["ok"] is True
    assert saved["endpoint"]["provider_id"] == "ali-bailian"
    assert saved["endpoint"]["api_key_set"] is True
    assert saved["endpoint"]["api_key"].endswith("3456")
    assert "sk-test" not in saved["endpoint"]["api_key"]
    assert saved["endpoint"]["base_url"].startswith("https://dashscope")
    assert saved["endpoint"]["model"]

    listed = api.list_ai_endpoints()
    assert listed["ok"] is True
    assert len(listed["endpoints"]) == 1
    assert listed["endpoints"][0]["active"] is True

    second = api.save_ai_endpoint(
        {
            "name": "OpenAI",
            "provider_id": "openai",
            "api_key": "sk-openai",
            "model": "gpt-5.5",
            "priority": 1,
        }
    )
    assert second["ok"] is True
    activated = api.activate_ai_endpoint("百炼")
    assert activated["ok"] is True
    assert activated["endpoint"]["name"] == "百炼"
    listed2 = api.list_ai_endpoints()
    active = next(r for r in listed2["endpoints"] if r["active"])
    assert active["name"] == "百炼"

    disabled = api.save_ai_endpoint({"index": active["index"], "enabled": False})
    assert disabled["ok"] is True
    assert disabled["endpoint"]["enabled"] is False

    removed = api.delete_ai_endpoint("OpenAI")
    assert removed["ok"] is True
    listed3 = api.list_ai_endpoints()
    assert len(listed3["endpoints"]) == 1
    assert listed3["endpoints"][0]["name"] == "百炼"


def test_partial_update_keeps_api_key(tmp_path: Path) -> None:
    api = _Harness(tmp_path / "settings.json")
    api.save_ai_endpoint(
        {
            "name": "keep-key",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-secret-abcdef",
            "model": "demo",
        }
    )
    updated = api.save_ai_endpoint({"name": "keep-key", "model": "demo-2"})
    assert updated["ok"] is True
    assert updated["endpoint"]["model"] == "demo-2"
    settings = api.settings_mgr.get()
    assert settings.ai_endpoints[0].api_key == "sk-secret-abcdef"
