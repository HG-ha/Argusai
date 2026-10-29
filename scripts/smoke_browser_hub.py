"""Smoke-test the managed browser hub: first call pays Chromium startup;
subsequent calls should be much faster because the same browser is reused."""

import asyncio
import time

from argusai.core.browser.browser_pool import get_browser_hub
from argusai.core.security.probes import browser_evaluate, browser_probe


async def main() -> None:
    hub = get_browser_hub()
    print("initial status:", hub.status())

    t0 = time.time()
    r1 = await browser_probe("https://example.com/", scope="example.com")
    e1 = int((time.time() - t0) * 1000)
    print(f"call1 probe: ok={r1.get('ok')} status={r1.get('status_code')} total={e1}ms html_len={r1.get('html_length')}")

    t1 = time.time()
    r2 = await browser_probe("https://example.com/?x=2", scope="example.com")
    e2 = int((time.time() - t1) * 1000)
    print(f"call2 probe: ok={r2.get('ok')} status={r2.get('status_code')} total={e2}ms")

    t2 = time.time()
    r3 = await browser_evaluate(
        "https://example.com/",
        "document.title",
        scope="example.com",
    )
    e3 = int((time.time() - t2) * 1000)
    print(f"call3 evaluate: ok={r3.get('ok')} value={r3.get('value')!r} total={e3}ms")

    print("status:", hub.status())
    hub.shutdown(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
