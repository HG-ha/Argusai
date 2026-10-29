"""Interactive request breakpoint engine: matching, resolve, edits, async hold."""

from __future__ import annotations

import asyncio

import pytest

import argusai.core.security.proxy_breakpoint as bp
from argusai.core.security.proxy_breakpoint import BreakpointEngine, apply_request_edits


def test_matches_respects_enabled_and_filters() -> None:
    eng = BreakpointEngine()
    assert eng.matches("GET", "http://a/api/x", "a") is False  # disabled
    eng.set_config({"enabled": True, "url_contains": "/api/", "methods": ["POST"]})
    assert eng.matches("POST", "http://a/api/x", "a") is True
    assert eng.matches("GET", "http://a/api/x", "a") is False  # wrong method
    assert eng.matches("POST", "http://a/other", "a") is False  # url filter miss
    eng.set_config({"enabled": True, "host_contains": "evil"})
    assert eng.matches("GET", "http://evil.test/", "evil.test") is True
    assert eng.matches("GET", "http://safe.test/", "safe.test") is False


def test_resolve_unknown_id() -> None:
    eng = BreakpointEngine()
    out = eng.resolve("nope", "forward")
    assert out["ok"] is False


def test_apply_request_edits_overlays_and_keeps() -> None:
    m, u, h, b = apply_request_edits(
        {"method": "PUT", "headers": [["X", "1"]], "body_text": "hi"},
        method="GET", url="http://x/", header_items=[("A", "b")], body=b"orig",
    )
    assert m == "PUT" and u == "http://x/" and h == [("X", "1")] and b == b"hi"

    m2, u2, h2, b2 = apply_request_edits(
        {}, method="GET", url="http://x/", header_items=[("A", "b")], body=b"orig",
    )
    assert m2 == "GET" and u2 == "http://x/" and h2 == [("A", "b")] and b2 == b"orig"


def test_apply_request_edits_body_b64() -> None:
    import base64
    edits = {"body_b64": base64.b64encode(b"\x00\x01binary").decode()}
    _, _, _, b = apply_request_edits(edits, method="GET", url="u", header_items=[], body=b"orig")
    assert b == b"\x00\x01binary"


def test_pause_forwards_immediately_when_disabled() -> None:
    async def scenario():
        eng = BreakpointEngine()
        return await eng.pause_request(method="GET", url="http://x/", host="x", header_items=[], body=b"")

    out = asyncio.run(scenario())
    assert out == {"action": "forward"}


def test_pause_parks_and_resolve_forwards_with_edits() -> None:
    async def scenario():
        eng = BreakpointEngine()
        eng.set_config({"enabled": True})

        async def driver():
            for _ in range(200):
                if eng.list_paused():
                    break
                await asyncio.sleep(0.005)
            pid = eng.list_paused()[0]["id"]
            eng.resolve(pid, "forward", {"url": "http://x/edited"})

        task = asyncio.create_task(driver())
        res = await eng.pause_request(
            method="GET", url="http://x/", host="x", header_items=[("A", "b")], body=b"",
        )
        await task
        return eng, res

    eng, res = asyncio.run(scenario())
    assert res["action"] == "forward"
    assert res["edits"]["url"] == "http://x/edited"
    assert eng.list_paused() == []  # unparked after resolve


def test_pause_drop() -> None:
    async def scenario():
        eng = BreakpointEngine()
        eng.set_config({"enabled": True})

        async def driver():
            for _ in range(200):
                if eng.list_paused():
                    break
                await asyncio.sleep(0.005)
            eng.resolve(eng.list_paused()[0]["id"], "drop")

        task = asyncio.create_task(driver())
        res = await eng.pause_request(method="POST", url="http://x/", host="x", header_items=[], body=b"x")
        await task
        return res

    assert asyncio.run(scenario())["action"] == "drop"


def test_pause_auto_forwards_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp, "PAUSE_TIMEOUT_SECONDS", 0.05)

    async def scenario():
        eng = BreakpointEngine()
        eng.set_config({"enabled": True})
        return await eng.pause_request(method="GET", url="http://x/", host="x", header_items=[], body=b"")

    out = asyncio.run(scenario())
    assert out["action"] == "forward"
    assert out.get("timed_out") is True
