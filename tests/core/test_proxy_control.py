"""Localhost breakpoint control used by ``argusai proxy break``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from argusai.core.security.proxy_control import ProxyControlServer


class _FakeProxy:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {"enabled": False}
        self.paused: list[dict[str, Any]] = [
            {"id": "bp-1", "method": "GET", "url": "https://example.test/a"},
        ]
        self.resolved: list[dict[str, Any]] = []

    def breakpoint_list_paused(self) -> dict[str, Any]:
        return {"ok": True, "paused": list(self.paused), "running": True}

    def get_breakpoint_config(self) -> dict[str, Any]:
        return {"ok": True, "config": dict(self.config), "running": True}

    def set_breakpoint_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        self.config = dict(config or {})
        return {"ok": True, "config": dict(self.config), "running": True}

    def breakpoint_resolve(self, pause_id: str, action: str = "forward", edits: dict[str, Any] | None = None) -> dict[str, Any]:
        self.resolved.append({"id": pause_id, "action": action, "edits": edits or {}})
        self.paused = [row for row in self.paused if row.get("id") != pause_id]
        return {"ok": True, "id": pause_id, "action": action}


def _call(info: dict[str, Any], method: str, path: str, payload: dict[str, Any] | None = None, *, token: str | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{info['port']}{path}",
        data=body,
        method=method,
        headers={
            "X-Argus-Token": token if token is not None else str(info["token"]),
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_breakpoint_control_lists_arms_and_forwards(tmp_path: Path) -> None:
    proxy = _FakeProxy()
    server = ProxyControlServer(proxy, tmp_path)
    info = server.start()
    try:
        listed = _call(info, "GET", "/v1/breakpoints")
        assert listed["paused"][0]["id"] == "bp-1"
        armed = _call(info, "POST", "/v1/breakpoints/config", {
            "enabled": True,
            "host_contains": "example.test",
            "methods": ["GET"],
        })
        assert armed["config"]["enabled"] is True
        forwarded = _call(info, "POST", "/v1/breakpoints/resolve", {
            "id": "bp-1",
            "action": "forward",
            "edits": {"url": "https://example.test/b"},
        })
        assert forwarded["ok"] is True
        assert proxy.resolved[0]["edits"]["url"].endswith("/b")
        assert _call(info, "GET", "/v1/breakpoints")["paused"] == []
    finally:
        server.stop()
    assert not (tmp_path / "control.json").exists()


def test_breakpoint_control_rejects_a_bad_token(tmp_path: Path) -> None:
    server = ProxyControlServer(_FakeProxy(), tmp_path)
    info = server.start()
    try:
        try:
            _call(info, "GET", "/v1/breakpoints", token="nope")
        except Exception as exc:
            assert getattr(exc, "code", None) == 401
        else:
            raise AssertionError("expected 401")
    finally:
        server.stop()
