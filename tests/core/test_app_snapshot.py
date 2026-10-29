"""Tests for application / screen snapshot builder."""

from __future__ import annotations

from argusai.core.workspace.app_snapshot import ROUTE_LABELS, build_screen_snapshot


class _FakeApi:
    def __init__(self) -> None:
        self._ui: dict = {}

    def get_ui_context(self):
        return dict(self._ui)

    def list_tasks(self):
        return []

    def get_proxy_status(self):
        return {"running": False}

    def get_settings(self):
        return {"ai": {"enabled": True, "endpoints": []}}

    def list_checks(self):
        return []

    def list_mcp_servers(self):
        return {
            "ok": True,
            "servers": [
                {"id": "1", "name": "可视化图表", "enabled": True, "last_probe": {"status": "error", "error": "HTTP 410"}},
            ],
        }


def test_screen_snapshot_mcp_route() -> None:
    api = _FakeApi()
    api._ui = {"route": "mcp", "page_label": ROUTE_LABELS["mcp"]}
    snap = build_screen_snapshot(api)
    assert snap["route"] == "mcp"
    assert "MCP" in snap["screen_summary"]
    assert snap["current_view"]["mcp"]["count"] == 1


def test_screen_snapshot_includes_ui_theme() -> None:
    api = _FakeApi()
    api._ui = {
        "route": "settings",
        "page_label": ROUTE_LABELS["settings"],
        "client": {"theme": {"preferred": "auto", "effective": "dark"}},
    }
    snap = build_screen_snapshot(api)
    assert snap["ui_theme"]["effective"] == "dark"
    assert snap["current_view"]["ui_theme"]["label"] == "深色"
    assert "界面主题：深色模式" in snap["screen_summary"]
