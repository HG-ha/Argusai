"""Tests for ArgusAI-owned CDP tab tracking and cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from argusai.core.browser import browser_pool


def test_owned_targets_persist_per_endpoint() -> None:
    browser_pool._OWNED_TARGETS_BY_ENDPOINT.clear()
    browser_pool.register_owned_target("http://127.0.0.1:9222", "T1")
    hub_a = browser_pool.BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    hub_b = browser_pool.BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    assert "T1" in hub_a._owned_target_ids
    assert hub_a._owned_target_ids is hub_b._owned_target_ids
    hub_a._register_owned_target("T2")
    assert "T2" in hub_b._owned_target_ids


def test_close_skips_unowned_with_actionable_hint() -> None:
    hub = browser_pool.BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    page = MagicMock()
    page.close = AsyncMock()

    async def _run() -> None:
        with patch.object(hub, "_list_pages_inside_loop", AsyncMock(return_value=[page])), patch.object(
            hub,
            "_page_snapshot_inside_loop",
            AsyncMock(return_value={"target_id": "USER1", "title": "Lab", "url": "https://lab.example/"}),
        ):
            out = await hub.aclose_owned_pages()
        assert out["closed_count"] == 0
        assert out["skipped_count"] == 1
        assert out["skipped"][0]["reason"] == "not_argusai_owned"
        assert "browser_mark_tabs_owned" in out.get("next_step", "")

    asyncio.run(_run())


def test_force_close_unowned_tab() -> None:
    hub = browser_pool.BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    page = MagicMock()
    page.close = AsyncMock()

    async def _run() -> None:
        with patch.object(hub, "_list_pages_inside_loop", AsyncMock(return_value=[page])), patch.object(
            hub,
            "_page_snapshot_inside_loop",
            AsyncMock(return_value={"target_id": "USER1", "title": "Lab", "url": "https://lab.example/"}),
        ):
            out = await hub.aclose_owned_pages(target_ids=["USER1"], force=True)
        assert out["closed_count"] == 1
        assert out["closed"][0]["forced"] is True
        page.close.assert_awaited_once()

    asyncio.run(_run())


def test_mark_tabs_owned_registers_targets() -> None:
    browser_pool._OWNED_TARGETS_BY_ENDPOINT.clear()
    hub = browser_pool.BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    page = MagicMock()

    async def _run() -> None:
        with patch.object(hub, "_list_pages_inside_loop", AsyncMock(return_value=[page])), patch.object(
            hub,
            "_page_snapshot_inside_loop",
            AsyncMock(return_value={"target_id": "LAB1", "title": "Lab", "url": "https://lab.example/"}),
        ):
            out = await hub.amark_pages_owned(["LAB1"])
        assert out["marked_count"] == 1
        assert "LAB1" in hub._owned_target_ids

    asyncio.run(_run())
