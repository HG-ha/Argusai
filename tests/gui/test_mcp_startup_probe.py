from __future__ import annotations

from types import SimpleNamespace

from argusai.gui.settings import SettingsMixin


class _SettingsManager:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(mcp_servers=[])

    def get(self):
        return self.settings

    def save(self, settings) -> None:
        self.settings = settings

    def update(self, updater):
        self.settings = updater(self.settings)
        return self.settings


class _Api(SettingsMixin):
    def __init__(self) -> None:
        self.settings_mgr = _SettingsManager()


def test_ensure_mcp_startup_probe_runs_once_per_process(monkeypatch) -> None:
    api = _Api()
    calls = 0

    async def fake_probe(_api):
        nonlocal calls
        calls += 1
        return {"ok": True, "probed": 2, "alive": 1, "dead": 1, "servers": []}

    monkeypatch.setattr("argusai.core.tools.case_mcp.probe_all_mcp_servers", fake_probe)
    monkeypatch.setattr(
        "argusai.core.workspace.data_root_migrate.repair_mcp_venv_launchers",
        lambda settings: ([], settings),
    )

    started = api.ensure_mcp_startup_probe()
    assert started["ok"] is True
    api._mcp_probe_thread.join(timeout=2)
    assert not api._mcp_probe_thread.is_alive()

    listed = api.list_mcp_servers()
    assert listed["startup_probe"]["status"] == "done"
    assert listed["startup_probe"]["alive"] == 1
    assert listed["startup_probe"]["dead"] == 1

    api.ensure_mcp_startup_probe()
    assert calls == 1


def test_save_repairs_modelscope_npx_config_before_persisting(monkeypatch) -> None:
    api = _Api()

    def prepare(payload):
        return {
            **payload,
            "args": ["bazi-mcp@0.1.0"],
            "provenance": {
                **payload["provenance"],
                "version": "0.1.0",
            },
        }

    monkeypatch.setattr(
        "argusai.core.infra.mcp_registry.prepare_marketplace_config",
        prepare,
    )
    result = api.save_mcp_server(
        {
            "name": "八字 MCP",
            "enabled": False,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "bazi-mcp"],
            "provenance": {"source": "modelscope"},
        }
    )

    assert result["ok"] is True
    assert result["marketplace_prepared"] is True
    assert result["server"]["args"] == ["bazi-mcp@0.1.0"]
    assert api.settings_mgr.get().mcp_servers[0]["args"] == ["bazi-mcp@0.1.0"]


def test_settings_api_lists_mcp_server_tools_with_bounded_schema(monkeypatch) -> None:
    api = _Api()
    seen: dict[str, object] = {}

    async def fake_list_tools(
        passed_api,
        server_id="",
        limit=0,
        include_schema=False,
        **_kwargs,
    ):
        seen.update({
            "api": passed_api,
            "server_id": server_id,
            "limit": limit,
            "include_schema": include_schema,
        })
        return {
            "ok": True,
            "server_id": server_id,
            "total_count": 1,
            "tools": [{"mcp_name": "read_file", "parameters": {}}],
        }

    monkeypatch.setattr(
        "argusai.core.tools.case_mcp.mcp_server_list_tools",
        fake_list_tools,
    )

    result = api.list_mcp_server_tools("filesystem")

    assert result["ok"] is True
    assert result["tools"][0]["mcp_name"] == "read_file"
    assert seen == {
        "api": api,
        "server_id": "filesystem",
        "limit": 100,
        "include_schema": True,
    }
