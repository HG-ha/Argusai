"""Shared scan context: HTTP client, Playwright handle, event bus, work dir."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from argusai.core.events import EventBus

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

    from argusai.core.ai import AIProvider


DEFAULT_UA = (
    "Mozilla/5.0 (argusai/0.1; +https://github.com/) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass
class ContextSettings:
    timeout: float = 15.0
    http_concurrency: int = 8
    browser_channel: str = "chrome"  # use system Chrome, fall back to chromium
    browser_persist: bool = True
    headless: bool = True
    browser_slow_mo: int = 0
    xss_observe_delay_ms: int = 0
    user_agent: str = DEFAULT_UA
    verify_tls: bool = True
    follow_redirects: bool = True
    cdp_url: str | None = None  # if set, attach to an already-running Chrome
    extra_http2: bool = False
    ai_config: Any | None = None  # AIConfig from core/ai.py, optional (CLI override)
    ai_endpoints: list[Any] | None = None  # list[AIEndpoint] from settings, optional


@dataclass
class Context:
    """One shared context per scan run."""

    bus: EventBus
    work_dir: Path
    settings: ContextSettings = field(default_factory=ContextSettings)

    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _http_sem: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _playwright: "Playwright | None" = field(default=None, init=False, repr=False)
    _browser: "Browser | None" = field(default=None, init=False, repr=False)
    _browser_ctx: "BrowserContext | None" = field(default=None, init=False, repr=False)
    _browser_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    ai: "AIProvider | None" = field(default=None, init=False, repr=False)
    shared: dict[str, Any] = field(default_factory=dict)

    async def __aenter__(self) -> "Context":
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._http_sem = asyncio.Semaphore(self.settings.http_concurrency)
        self._http = httpx.AsyncClient(
            timeout=self.settings.timeout,
            verify=self.settings.verify_tls,
            follow_redirects=self.settings.follow_redirects,
            http2=self.settings.extra_http2,
            headers={"User-Agent": self.settings.user_agent},
            trust_env=False,
        )
        # Lazy-init AI provider if configured (override first, then endpoints)
        if self.settings.ai_config or self.settings.ai_endpoints:
            try:
                from argusai.core.ai import create_provider
                self.ai = create_provider(
                    endpoints=self.settings.ai_endpoints,
                    override=self.settings.ai_config,
                )
            except Exception as e:
                self.bus.emit("log", level="warn", msg=f"AI provider init failed: {e}")
                self.ai = None
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self.ai is not None:
            try:
                await self.ai.close()
            except Exception:
                pass
        async with self._browser_lock:
            if self._browser_ctx is not None:
                try:
                    await self._browser_ctx.close()
                except Exception:
                    pass
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass

    @property
    def http(self) -> httpx.AsyncClient:
        assert self._http is not None, "Context not entered"
        return self._http

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        """HTTP GET with global concurrency limit."""
        assert self._http_sem is not None and self._http is not None
        async with self._http_sem:
            return await self._http.get(url, **kw)

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        assert self._http_sem is not None and self._http is not None
        async with self._http_sem:
            return await self._http.request(method, url, **kw)

    async def browser_context(self) -> "BrowserContext":
        """Lazily start Playwright and return a shared browser context.

        Uses system Chrome via ``channel='chrome'`` by default. If
        ``settings.cdp_url`` is set, attaches to an already-running browser
        instead. When ``browser_persist`` is true the user-data-dir is stored
        under this run's ``work_dir/.profile`` so concurrent runs do not share
        the same Chrome profile.
        """
        async with self._browser_lock:
            if self._browser_ctx is not None:
                return self._browser_ctx
            from argusai.core.browser.browser_locator import ensure_playwright_driver_node
            from playwright.async_api import async_playwright

            driver = ensure_playwright_driver_node()
            if not driver.get("ok"):
                raise RuntimeError(driver.get("error") or "Playwright driver runtime is unavailable")

            self._playwright = await async_playwright().start()
            s = self.settings
            if s.cdp_url:
                self._browser = await self._playwright.chromium.connect_over_cdp(s.cdp_url)
                self._browser_ctx = (
                    self._browser.contexts[0]
                    if self._browser.contexts
                    else await self._browser.new_context(user_agent=s.user_agent)
                )
            elif s.browser_persist:
                profile = self.work_dir / ".profile"
                profile.mkdir(parents=True, exist_ok=True)
                self._browser_ctx = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel=s.browser_channel,
                    headless=s.headless,
                    slow_mo=s.browser_slow_mo,
                    user_agent=s.user_agent,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                self._browser = self._browser_ctx.browser
            else:
                self._browser = await self._playwright.chromium.launch(
                    channel=s.browser_channel,
                    headless=s.headless,
                    slow_mo=s.browser_slow_mo,
                )
                self._browser_ctx = await self._browser.new_context(user_agent=s.user_agent)
            return self._browser_ctx

    def emit(self, type: str, **payload: Any) -> None:
        self.bus.emit(type, **payload)

    def log(self, msg: str, level: str = "info", **extra: Any) -> None:
        self.bus.emit("log", level=level, msg=msg, **extra)
