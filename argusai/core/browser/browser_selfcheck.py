"""Headless diagnosis of the browser stack, runnable from a packaged build.

Packaging regressions in this area are invisible until someone opens a browser
tool by hand: the GUI starts fine, and only the first navigation fails. This
module drives the same resolution path the hub uses (managed driver Node,
system browser discovery, a real page round-trip) without needing a window, so
``ArgusAI.exe --self-check-browser`` answers "is browser automation actually
wired up on this machine?" in one command.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def _run() -> dict[str, Any]:
    from argusai.core.browser.browser_locator import (
        ensure_playwright_driver_node,
        playwright_bundle_status,
        resolve_browser_binary,
    )

    report: dict[str, Any] = {"ok": False, "steps": {}}

    driver = ensure_playwright_driver_node()
    report["steps"]["driver_node"] = driver
    if not driver.get("ok"):
        report["error"] = driver.get("error") or "driver Node unavailable"
        return report

    resolved = resolve_browser_binary()
    report["steps"]["browser"] = resolved
    report["steps"]["engines"] = playwright_bundle_status().get("engines", {})
    channel = str(resolved.get("channel") or "")
    executable = str(resolved.get("executable_path") or "")

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        launch: dict[str, Any] = {"headless": True}
        if channel:
            launch["channel"] = channel
        elif executable:
            launch["executable_path"] = executable

        browser = await pw.chromium.launch(**launch)
        try:
            page = await browser.new_page()
            await page.set_content("<h1 id='probe'>argusai self check</h1>")
            text = await page.inner_text("#probe")
            shot = await page.screenshot()
            report["steps"]["page"] = {
                "text": text,
                "screenshot_bytes": len(shot),
                "user_agent": await page.evaluate("() => navigator.userAgent"),
            }
        finally:
            await browser.close()
    finally:
        await pw.stop()

    report["ok"] = True
    return report


def self_check_browser() -> dict[str, Any]:
    """Return a JSON-serialisable report; never raises."""
    try:
        return asyncio.run(_run())
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
