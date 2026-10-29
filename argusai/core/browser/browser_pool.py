"""App-wide managed headless browser used by AI assistant tools.

``browser_probe`` / ``browser_evaluate`` would otherwise spin up a fresh
Playwright runtime + Chromium process on every call, paying a 1-3 second
startup tax each time the agent loop chains another browser step. The
``BrowserHub`` owns a dedicated background thread that runs its own
asyncio event loop and keeps Playwright + Chromium + a BrowserContext
alive across calls.

Public surface:

* :func:`get_browser_hub` — global singleton accessor (lazy).
* :meth:`BrowserHub.acall_with_page` — async helper for tools running in
  another event loop; acquires a fresh page from the shared context,
  runs the supplied coroutine, then closes the page (not the browser).
* :meth:`BrowserHub.reset` — drop the current context (clears cookies /
  storage / cache) but keep the browser process warm.
* :meth:`BrowserHub.status` — diagnostic snapshot.
* :meth:`BrowserHub.shutdown` — best-effort full teardown, registered
  via :mod:`atexit` so app exit doesn't leak Chrome processes.

Cross-loop safety:

The :mod:`playwright.async_api` objects are bound to the loop that
created them. To use them from a different loop (every
``chat_assistant`` round creates its own loop), we wrap a
``run_coroutine_threadsafe`` future via :func:`asyncio.wrap_future`, so
the caller's loop yields cooperatively while the hub loop performs the
work.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from argusai.core.browser.browser_live import CONTROL as _LIVE_CONTROL
from argusai.core.infra.win_stdio import ensure_valid_stdio

log = logging.getLogger("argusai.browser_hub")


_VALID_ENGINES = ("chromium", "firefox", "webkit")
_OWNED_TARGETS_BY_ENDPOINT: dict[str, set[str]] = {}
_OWNED_TARGETS_LOCK = threading.Lock()


def _endpoint_owned_targets(endpoint: str) -> set[str]:
    key = str(endpoint or "").strip()
    if not key:
        return set()
    with _OWNED_TARGETS_LOCK:
        return _OWNED_TARGETS_BY_ENDPOINT.setdefault(key, set())


def register_owned_target(endpoint: str, target_id: str) -> None:
    tid = str(target_id or "").strip()
    key = str(endpoint or "").strip()
    if not tid or not key:
        return
    with _OWNED_TARGETS_LOCK:
        _OWNED_TARGETS_BY_ENDPOINT.setdefault(key, set()).add(tid)


def discard_owned_target(endpoint: str, target_id: str) -> None:
    tid = str(target_id or "").strip()
    key = str(endpoint or "").strip()
    if not tid or not key:
        return
    with _OWNED_TARGETS_LOCK:
        bucket = _OWNED_TARGETS_BY_ENDPOINT.get(key)
        if bucket is not None:
            bucket.discard(tid)


class BrowserHub:
    """Dedicated thread + asyncio loop owning long-lived Playwright resources."""

    def __init__(
        self,
        *,
        headless: bool = True,
        channel: str = "chrome",
        engine: str = "chromium",
        profile_name: str = "default",
        profile: dict[str, Any] | None = None,
        executable_path: str = "",
        attached_endpoint: str = "",
    ) -> None:
        engine = (engine or "chromium").lower()
        if engine not in _VALID_ENGINES:
            engine = "chromium"
        self._headless = headless
        self._channel = channel if engine == "chromium" else ""
        self._engine = engine
        self._executable_path = executable_path or ""
        self._profile_name = profile_name or "default"
        self._profile: dict[str, Any] = dict(profile or {})
        # When set, the hub connects to an existing browser via CDP
        # instead of launching its own process. In attach mode we reuse
        # the user's real default BrowserContext (so cookies / logged-in
        # sessions / open tabs are visible) and never reset it.
        self._attached_endpoint = attached_endpoint or ""
        # Filled in by ``_ensure_browser_inside_loop`` after we figure out
        # which binary we actually used (system Chrome / Edge / bundled).
        self._resolved_source: str = ""
        self._resolved_label: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._init_lock: asyncio.Lock | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._closed = False
        self._launched_at: float = 0.0
        self._pages_served: int = 0
        self._context_resets: int = 0
        if self._attached_endpoint:
            self._owned_target_ids = _endpoint_owned_targets(self._attached_endpoint)
        else:
            self._owned_target_ids = set()
        self._page_listener_installed = False
        self._thread_lock = threading.Lock()
        self._shutdown_requested = False
        # Live-view (CDP screencast) state. ``_live_session`` / ``_live_page``
        # are bound to the hub loop; ``_live_frame`` is read from the GUI
        # bridge thread so it is guarded by its own lock.
        self._live_session: Any = None
        self._live_page: Any = None
        self._live_target_id: str = ""
        self._live_frame_lock = threading.Lock()
        self._live_frame: dict[str, Any] = {}
        self._live_seq = 0

    def _register_owned_target(self, target_id: str) -> None:
        tid = str(target_id or "").strip()
        if not tid:
            return
        self._owned_target_ids.add(tid)
        if self._attached_endpoint:
            register_owned_target(self._attached_endpoint, tid)

    def _discard_owned_target(self, target_id: str) -> None:
        tid = str(target_id or "").strip()
        if not tid:
            return
        self._owned_target_ids.discard(tid)
        if self._attached_endpoint:
            discard_owned_target(self._attached_endpoint, tid)

    async def _mark_page_owned(self, page: Any) -> dict[str, Any]:
        snap = await self._page_snapshot_inside_loop(page, -1)
        tid = str(snap.get("target_id") or "")
        if tid:
            self._register_owned_target(tid)
            snap["ai_owned"] = True
        return snap

    async def _handle_new_page_inside_loop(self, page: Any) -> None:
        if not self._attached_endpoint:
            return
        try:
            opener = page.opener
        except Exception:
            opener = None
        if opener is None:
            return
        try:
            opener_snap = await self._page_snapshot_inside_loop(opener, -1)
        except Exception:
            return
        opener_tid = str(opener_snap.get("target_id") or "")
        if opener_tid and opener_tid in self._owned_target_ids:
            await self._mark_page_owned(page)

    def _install_page_listener_inside_loop(self) -> None:
        if self._page_listener_installed or not self._attached_endpoint or self._context is None:
            return
        loop = asyncio.get_event_loop()

        def _on_page(page: Any) -> None:
            loop.create_task(self._handle_new_page_inside_loop(page))

        try:
            self._context.on("page", _on_page)
            self._page_listener_installed = True
        except Exception as exc:
            log.debug("failed to install page listener: %s", exc)

    # ---- thread / loop bootstrap ------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            t = threading.Thread(
                target=self._thread_main,
                name="argusai-browser-hub",
                daemon=True,
            )
            t.start()
            self._thread = t
            if not self._ready.wait(timeout=10):
                raise RuntimeError("BrowserHub thread failed to start in time")

    def _thread_main(self) -> None:
        ensure_valid_stdio()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._init_lock = asyncio.Lock()
        self._ready.set()
        try:
            loop.run_until_complete(self._stop_event.wait())
        except Exception as e:
            log.warning("BrowserHub loop exited: %s", e)
        finally:
            try:
                loop.run_until_complete(self._teardown_inside_loop())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    async def _teardown_inside_loop(self) -> None:
        try:
            # In attach mode the browser + its default context belong to
            # the *user*; closing either would shut down their Edge/Chrome
            # window. Calling ``Browser.close()`` on a CDP-attached client
            # has inconsistent behaviour across Playwright versions /
            # browser builds (sometimes a clean disconnect, sometimes a
            # full ``Browser.close`` CDP command that quits the browser).
            # The only safe move is to NEVER call .close() on the attached
            # Browser — just drop our refs and stop the Playwright client.
            # The browser process keeps running because we spawned it
            # with DETACHED_PROCESS / its own session.
            if self._attached_endpoint:
                self._context = None
                self._browser = None
                if self._pw is not None:
                    try:
                        await self._pw.stop()
                    except Exception:
                        pass
                    self._pw = None
                return
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
                self._pw = None
        finally:
            self._closed = True

    # ---- Playwright lifecycle (runs INSIDE hub loop) ----------------

    async def _ensure_browser_inside_loop(self) -> None:
        assert self._init_lock is not None
        async with self._init_lock:
            if self._closed:
                raise RuntimeError("BrowserHub is closed")
            if self._pw is None:
                ensure_valid_stdio()
                from argusai.core.browser.browser_locator import ensure_playwright_driver_node
                from playwright.async_api import async_playwright

                driver = ensure_playwright_driver_node()
                if not driver.get("ok"):
                    raise RuntimeError(
                        driver.get("error") or "Playwright driver runtime is unavailable"
                    )

                self._pw = await async_playwright().start()
                self._launched_at = asyncio.get_event_loop().time()
            try:
                connected = bool(self._browser and self._browser.is_connected())
            except Exception:
                connected = False
            if not connected:
                if self._attached_endpoint:
                    # Reuse the user's running browser instead of starting
                    # our own. Cookies / login state / open tabs are all
                    # part of ``browser.contexts[0]`` because the user
                    # launched the browser with their default profile.
                    # Auto-reconnect: this path also runs when our
                    # previous CDP websocket died (network blip, page
                    # crash, navigation away from devtools). The AI
                    # should NOT have to manually call browser_attach_*
                    # to recover — just transparently reconnect.
                    chromium = getattr(self._pw, "chromium")
                    try:
                        self._browser = await chromium.connect_over_cdp(self._attached_endpoint)
                    except Exception as e:
                        # If reconnect failed the user's browser is
                        # almost certainly gone (or moved port). Make the
                        # error message actionable so the AI surfaces it
                        # cleanly instead of spamming retries.
                        raise RuntimeError(
                            f"CDP attach lost: cannot reconnect to {self._attached_endpoint}. "
                            f"Either the browser was closed or the debug port changed. "
                            f"Ask the user to confirm, then either call browser_attach_existing "
                            f"with a fresh endpoint or browser_launch_for_attach to relaunch. "
                            f"(underlying error: {e})"
                        ) from e
                    self._resolved_source = "attached_cdp"
                    self._resolved_label = self._attached_endpoint
                    self._context = None
                    self._launched_at = asyncio.get_event_loop().time()
                else:
                    engine_obj = getattr(self._pw, self._engine, None) or self._pw.chromium
                    launch_kwargs: dict[str, Any] = {"headless": self._headless}
                    # The automation browser must never emit audio: it may run
                    # off-screen and the user only ever sees the muted mirror, so
                    # a page that autoplays video/audio would otherwise leak
                    # sound with no visible source. Chromium-only flag.
                    if self._engine == "chromium":
                        from argusai.core.browser.browser_profiles import automation_window_size

                        _w, _h = automation_window_size()
                        launch_kwargs["args"] = ["--mute-audio", f"--window-size={_w},{_h}"]
                    # Only Chromium can pick between system binaries (Chrome /
                    # Edge / Brave / Vivaldi) or the bundled Playwright build.
                    # Firefox / WebKit have to use Playwright's patched fork.
                    if self._engine == "chromium":
                        from argusai.core.browser.browser_locator import (
                            KNOWN_CHANNELS,
                            resolve_browser_binary,
                        )

                        ch = (self._channel or "").strip().lower()
                        explicit_channel = ch if ch in KNOWN_CHANNELS else ""
                        hint = resolve_browser_binary(
                            explicit_executable=self._executable_path,
                            explicit_channel=explicit_channel,
                        )
                        if hint.get("channel"):
                            launch_kwargs["channel"] = hint["channel"]
                        elif hint.get("executable_path"):
                            launch_kwargs["executable_path"] = hint["executable_path"]
                        # else: let Playwright fall back to its bundled
                        # Chromium (will only succeed if ms-playwright has it).
                        self._resolved_source = hint.get("source", "")
                        self._resolved_label = hint.get("label", "")
                    try:
                        self._browser = await engine_obj.launch(**launch_kwargs)
                    except Exception as e:
                        msg = str(e)
                        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                            raise RuntimeError(
                                "未发现可用浏览器: 系统里没有 Chrome / Edge / Brave / Vivaldi, "
                                "Playwright bundle 也没下载。请在设置里指定本机浏览器路径, "
                                "或调用 browser_install_engine 工具下载 Playwright Chromium。"
                                f" (原始错误: {e})"
                            ) from e
                        raise
                    self._context = None
                    self._launched_at = asyncio.get_event_loop().time()
            if self._context is None:
                if self._attached_endpoint:
                    # Use the user's persistent context that already has
                    # their cookies + open tabs. Fall back to creating a
                    # new one only if the browser somehow has none.
                    contexts = list(getattr(self._browser, "contexts", []) or [])
                    self._context = contexts[0] if contexts else await self._new_context_inside_loop()
                else:
                    self._context = await self._new_context_inside_loop()
            self._install_page_listener_inside_loop()

    async def _new_context_inside_loop(self) -> Any:
        """Create a context using the current fingerprint profile."""
        from argusai.core.browser.browser_profiles import context_kwargs, stealth_init_script

        ctx_kwargs = context_kwargs(self._profile, engine=self._engine) if self._profile else {}
        # Pin the configured default viewport (1920x1080 unless changed) when the
        # profile does not spoof one — otherwise Playwright falls back to its
        # 1280x720 default. Fingerprint presets keep their own viewport.
        if not ctx_kwargs.get("viewport"):
            from argusai.core.browser.browser_profiles import automation_window_size

            w, h = automation_window_size()
            ctx_kwargs["viewport"] = {"width": w, "height": h}
        try:
            context = await self._browser.new_context(**ctx_kwargs)
        except Exception as e:
            # Be forgiving: if a profile arg is rejected (e.g. is_mobile on
            # an old Playwright build), fall back to a vanilla context so
            # the agent can still work.
            log.warning("new_context with profile=%s failed (%s); retrying without overrides", self._profile_name, e)
            vp = ctx_kwargs.get("viewport")
            context = await self._browser.new_context(viewport=vp) if vp else await self._browser.new_context()
        init_script = stealth_init_script(self._profile) if self._profile else ""
        if init_script:
            try:
                await context.add_init_script(init_script)
            except Exception as e:
                log.warning("add_init_script failed: %s", e)
        return context

    async def _with_page_inside_loop(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fresh_context: bool = False,
    ) -> Any:
        await self._ensure_browser_inside_loop()
        # ``fresh_context`` would destroy the user's real cookies / open
        # tabs in attach mode, so we silently ignore it there.
        if fresh_context and not self._attached_endpoint:
            assert self._init_lock is not None
            async with self._init_lock:
                if self._context is not None:
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                self._context = await self._new_context_inside_loop()
                self._context_resets += 1
        mark_hub_active(self)
        _LIVE_CONTROL.agent_enter(self)
        page = await self._context.new_page()
        self._pages_served += 1
        try:
            return await async_fn(page, *args, **kwargs)
        finally:
            _LIVE_CONTROL.agent_exit(self)
            try:
                await page.close()
            except Exception:
                pass

    async def _reset_inside_loop(self) -> None:
        assert self._init_lock is not None
        async with self._init_lock:
            if self._attached_endpoint:
                # Reset in attach mode would dump cookies / blow away
                # logged-in sessions belonging to the user. Pretend we
                # reset (bump the counter) but don't touch their state.
                self._context_resets += 1
                return
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    pass
            if self._browser is not None and self._browser.is_connected():
                self._context = await self._new_context_inside_loop()
            else:
                self._context = None
            self._context_resets += 1

    async def _list_pages_inside_loop(self) -> list[Any]:
        await self._ensure_browser_inside_loop()
        if self._attached_endpoint and self._browser is not None:
            pages: list[Any] = []
            for ctx in list(getattr(self._browser, "contexts", []) or []):
                pages.extend(list(getattr(ctx, "pages", []) or []))
            visible = [page for page in pages if _page_worth_showing(page)]
            if visible:
                return visible
            if pages:
                return pages
        if self._context is None:
            return []
        return list(self._context.pages)

    async def _page_snapshot_inside_loop(self, page: Any, index: int) -> dict[str, Any]:
        title = ""
        url = ""
        target_id = ""
        window_id: int | None = None
        window_bounds: dict[str, Any] = {}
        window_state = ""
        try:
            title = await page.title()
        except Exception:
            pass
        try:
            url = page.url or ""
        except Exception:
            pass
        if self._engine == "chromium" or self._attached_endpoint:
            session = None
            try:
                session = await page.context.new_cdp_session(page)
                try:
                    info = await session.send("Target.getTargetInfo")
                    target_info = info.get("targetInfo") or {}
                    target_id = str(target_info.get("targetId") or "")
                except Exception:
                    pass
                try:
                    win = await session.send("Browser.getWindowForTarget")
                    if win.get("windowId") is not None:
                        window_id = int(win["windowId"])
                    bounds = win.get("bounds") or {}
                    if isinstance(bounds, dict):
                        window_bounds = bounds
                        window_state = str(bounds.get("windowState") or "")
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                if session is not None:
                    try:
                        await session.detach()
                    except Exception:
                        pass
        return {
            "index": index,
            "target_id": target_id,
            "ai_owned": bool(target_id and target_id in self._owned_target_ids),
            "window_id": window_id,
            "window_bounds": window_bounds,
            "window_state": window_state,
            "title": title,
            "url": url,
        }

    def _assign_window_indices(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        window_order: list[Any] = []
        tab_counts: dict[Any, int] = {}
        for item in snapshots:
            wid: Any = item.get("window_id")
            if wid is None:
                wid = f"unknown-{item.get('index')}"
            if wid not in window_order:
                window_order.append(wid)
            window_index = window_order.index(wid)
            tab_index = tab_counts.get(wid, 0)
            tab_counts[wid] = tab_index + 1
            item["window_index"] = window_index
            item["tab_index"] = tab_index
        return snapshots

    def _group_windows(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: list[dict[str, Any]] = []
        by_key: dict[Any, dict[str, Any]] = {}
        for item in snapshots:
            key: Any = item.get("window_id")
            if key is None:
                key = f"unknown-{item.get('index')}"
            group = by_key.get(key)
            if group is None:
                group = {
                    "window_index": item.get("window_index"),
                    "window_id": item.get("window_id"),
                    "window_state": item.get("window_state") or "",
                    "bounds": item.get("window_bounds") or {},
                    "tabs": [],
                }
                by_key[key] = group
                grouped.append(group)
            tab = {k: v for k, v in item.items() if k != "window_bounds"}
            group["tabs"].append(tab)
        return grouped

    async def _page_snapshots_inside_loop(self) -> list[dict[str, Any]]:
        pages = await self._list_pages_inside_loop()
        snapshots: list[dict[str, Any]] = []
        for idx, page in enumerate(pages):
            snapshots.append(await self._page_snapshot_inside_loop(page, idx))
        return self._assign_window_indices(snapshots)

    async def _first_owned_page_inside_loop(self) -> Any:
        pages = await self._list_pages_inside_loop()
        for idx, page in enumerate(pages):
            snap = await self._page_snapshot_inside_loop(page, idx)
            if snap.get("ai_owned"):
                return page
        return None

    # ---- public async API for callers in their own loop -------------

    async def acall_with_page(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        fresh_context: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run ``async_fn(page, *args, **kwargs)`` on the hub loop using a
        freshly acquired page from the shared context. The caller awaits
        cooperatively without blocking its own event loop."""
        self._ensure_thread()
        coro = self._with_page_inside_loop(async_fn, args, kwargs, fresh_context=fresh_context)
        cf = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def aopen_owned_tab(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        reuse_existing: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Create or reuse a ArgusAI-owned tab and leave it open.

        This is for attached-browser workflows where the AI needs a scratch tab
        without navigating over one of the user's existing tabs.
        """
        self._ensure_thread()

        async def _inside() -> Any:
            await self._ensure_browser_inside_loop()
            if self._context is None:
                raise RuntimeError("no browser context")
            mark_hub_active(self)
            _LIVE_CONTROL.agent_enter(self)
            try:
                page = await self._first_owned_page_inside_loop() if reuse_existing else None
                created = False
                if page is None:
                    page = await self._context.new_page()
                    created = True
                snapshot = await self._page_snapshot_inside_loop(page, -1)
                target_id = snapshot.get("target_id") or ""
                if target_id:
                    self._register_owned_target(str(target_id))
                    snapshot["ai_owned"] = True
                self._pages_served += 1
                result = await async_fn(page, *args, **kwargs)
                if isinstance(result, dict):
                    result.setdefault("created", created)
                    result.setdefault("tab", snapshot)
                return result
            finally:
                _LIVE_CONTROL.agent_exit(self)

        cf = asyncio.run_coroutine_threadsafe(_inside(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def acall_with_each_page(
        self,
        async_fn: Any,
    ) -> list[Any]:
        """Run ``async_fn(page)`` on every currently open page in the live
        context. Returns the list of per-page results.

        Used by the hook manager to broadcast install/uninstall/drain
        across every tab the user has open.
        """
        self._ensure_thread()

        async def _inside() -> list[Any]:
            await self._ensure_browser_inside_loop()
            if self._context is None:
                return []
            pages = list(self._context.pages)
            out: list[Any] = []
            for page in pages:
                try:
                    out.append(await async_fn(page))
                except Exception as e:
                    out.append({"ok": False, "error": str(e)})
            return out

        cf = asyncio.run_coroutine_threadsafe(_inside(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def alist_pages(self) -> list[dict[str, Any]]:
        """Return a serialisable snapshot of every page in the live context.

        Used by ``browser_list_tabs`` / ``browser_use_tab`` to enumerate
        the tabs the user already has open in an attached browser. We
        cannot return the live ``Page`` objects across loops, so we
        capture title + url synchronously inside the hub loop.
        """
        self._ensure_thread()

        async def _collect() -> list[dict[str, Any]]:
            pages = await self._list_pages_inside_loop()
            out: list[dict[str, Any]] = []
            for idx, p in enumerate(pages):
                out.append(await self._page_snapshot_inside_loop(p, idx))
            return self._assign_window_indices(out)

        cf = asyncio.run_coroutine_threadsafe(_collect(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def amark_pages_owned(self, target_ids: list[str]) -> dict[str, Any]:
        """Mark existing tabs as ArgusAI-owned so they can be closed later."""
        self._ensure_thread()
        wanted = {str(t).strip() for t in target_ids if str(t).strip()}

        async def _mark() -> dict[str, Any]:
            pages = await self._list_pages_inside_loop()
            marked: list[dict[str, Any]] = []
            missing: list[str] = []
            for idx, page in enumerate(pages):
                snap = await self._page_snapshot_inside_loop(page, idx)
                tid = str(snap.get("target_id") or "")
                if tid not in wanted:
                    continue
                self._register_owned_target(tid)
                snap["ai_owned"] = True
                marked.append(snap)
            for tid in sorted(wanted):
                if tid not in {m.get("target_id") for m in marked}:
                    missing.append(tid)
            return {
                "ok": True,
                "marked": marked,
                "marked_count": len(marked),
                "missing_target_ids": missing,
            }

        cf = asyncio.run_coroutine_threadsafe(_mark(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def aclose_owned_pages(
        self,
        target_id: str = "",
        *,
        target_ids: list[str] | None = None,
        url_contains: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Close ArgusAI-owned tabs.

        In attached-browser mode this is deliberately limited to pages whose
        CDP target id was created through :meth:`aopen_owned_tab`, inherited
        from an owned opener, or explicitly marked via :meth:`amark_pages_owned`.
        With ``force=True`` (user-confirmed), ``target_ids`` may close tabs
        even when they were not previously tracked as owned.
        """
        self._ensure_thread()
        target_filter = str(target_id or "").strip()
        wanted = {str(t).strip() for t in (target_ids or []) if str(t).strip()}
        if target_filter:
            wanted.add(target_filter)
        url_needle = str(url_contains or "").strip().lower()

        async def _close() -> dict[str, Any]:
            pages = await self._list_pages_inside_loop()
            closed: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            for idx, page in enumerate(pages):
                snap = await self._page_snapshot_inside_loop(page, idx)
                tid = str(snap.get("target_id") or "")
                page_url = str(snap.get("url") or "")
                owned = bool(tid and tid in self._owned_target_ids)
                selected = (not wanted and not url_needle) or (tid in wanted) or (
                    url_needle and url_needle in page_url.lower()
                )
                if wanted or url_needle:
                    if not selected:
                        continue
                elif target_filter and tid != target_filter:
                    continue
                if not owned and not force:
                    skipped.append({
                        "target_id": tid,
                        "title": snap.get("title") or "",
                        "url": page_url,
                        "reason": "not_argusai_owned",
                    })
                    continue
                try:
                    await page.close()
                    if tid:
                        self._discard_owned_target(tid)
                    closed.append({
                        "target_id": tid,
                        "title": snap.get("title") or "",
                        "url": page_url,
                        "forced": bool(force and not owned),
                    })
                except Exception as e:
                    skipped.append({
                        "target_id": tid,
                        "title": snap.get("title") or "",
                        "url": page_url,
                        "reason": str(e),
                    })
            hint = ""
            if skipped and not closed:
                hint = (
                    "Tabs were skipped because they are not tracked as ai_owned. "
                    "Prefer browser_open_tab(reuse_existing=true) for new lab navigation. "
                    "If the user asked to close specific tabs, pass target_ids from browser_list_tabs "
                    "with force=true (requires confirmation), or call browser_mark_tabs_owned first."
                )
            elif skipped:
                hint = (
                    "Some tabs were skipped (not ai_owned). "
                    "Use browser_mark_tabs_owned(target_ids=[...]) for lab popups, "
                    "or force=true when the user explicitly asked to close listed tabs."
                )
            return {
                "ok": True,
                "closed": closed,
                "closed_count": len(closed),
                "skipped": skipped,
                "skipped_count": len(skipped),
                "target_id": target_filter,
                "target_ids": sorted(wanted),
                "url_contains": url_contains,
                "force": bool(force),
                "next_step": "browser_mark_tabs_owned" if skipped else "",
                "suggested_fix": hint,
            }

        cf = asyncio.run_coroutine_threadsafe(_close(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def acall_with_existing_page(
        self,
        async_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        match: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Like :meth:`acall_with_page` but bind to a *pre-existing* tab.

        ``match`` selects which tab to operate on:

        * ``{"index": int}`` — positional match in the current page list
        * ``{"target_id": str}`` — stable CDP target id from alist_pages
        * ``{"window_id": int, "tab_index": int}`` — tab within a browser window
        * ``{"window_index": int, "tab_index": int}`` — tab within listed window order
        * ``{"url_contains": str}`` — first page whose URL contains the substring
        * ``{"title_contains": str}`` — first page whose title contains the substring

        Closing the tab afterwards is the user's responsibility — we
        absolutely must not call ``page.close()`` on a tab the user
        opened themselves.
        """
        self._ensure_thread()
        match = match or {}

        async def _run() -> Any:
            pages = await self._list_pages_inside_loop()
            if not pages:
                raise RuntimeError("no pages in the attached browser context")
            snapshots = self._assign_window_indices([
                await self._page_snapshot_inside_loop(page, idx)
                for idx, page in enumerate(pages)
            ])
            target = None
            if match.get("target_id"):
                needle = str(match["target_id"])
                for idx, snap in enumerate(snapshots):
                    if snap.get("target_id") == needle:
                        target = pages[idx]
                        break
            if target is None and match.get("window_id") is not None and match.get("tab_index") is not None:
                try:
                    win_id = int(match["window_id"])
                    tab_index = int(match["tab_index"])
                    for idx, snap in enumerate(snapshots):
                        if snap.get("window_id") == win_id and snap.get("tab_index") == tab_index:
                            target = pages[idx]
                            break
                except (TypeError, ValueError):
                    target = None
            if target is None and match.get("window_index") is not None and match.get("tab_index") is not None:
                try:
                    win_index = int(match["window_index"])
                    tab_index = int(match["tab_index"])
                    for idx, snap in enumerate(snapshots):
                        if snap.get("window_index") == win_index and snap.get("tab_index") == tab_index:
                            target = pages[idx]
                            break
                except (TypeError, ValueError):
                    target = None
            if "index" in match and match["index"] is not None:
                try:
                    if target is None:
                        target = pages[int(match["index"])]
                except (IndexError, TypeError, ValueError):
                    target = None
            if target is None and match.get("url_contains"):
                needle = str(match["url_contains"]).lower()
                for p in pages:
                    try:
                        if needle in (p.url or "").lower():
                            target = p
                            break
                    except Exception:
                        continue
            if target is None and match.get("title_contains"):
                needle = str(match["title_contains"]).lower()
                for p in pages:
                    try:
                        if needle in ((await p.title()) or "").lower():
                            target = p
                            break
                    except Exception:
                        continue
            if target is None:
                target = pages[0]
            mark_hub_active(self)
            _LIVE_CONTROL.agent_enter(self)
            self._pages_served += 1
            try:
                return await async_fn(target, *args, **kwargs)
            finally:
                _LIVE_CONTROL.agent_exit(self)

        cf = asyncio.run_coroutine_threadsafe(_run(), self._loop)  # type: ignore[arg-type]
        return await asyncio.wrap_future(cf)

    async def areset(self) -> dict[str, Any]:
        self._ensure_thread()
        cf = asyncio.run_coroutine_threadsafe(self._reset_inside_loop(), self._loop)  # type: ignore[arg-type]
        await asyncio.wrap_future(cf)
        return self.status()

    def reset(self) -> dict[str, Any]:
        self._ensure_thread()
        cf = asyncio.run_coroutine_threadsafe(self._reset_inside_loop(), self._loop)  # type: ignore[arg-type]
        cf.result(timeout=15)
        return self.status()

    # ---- live view (CDP screencast + input forwarding) --------------

    async def _resolve_live_page_inside(self, target_id: str = "", index: int = -1) -> Any:
        pages = await self._list_pages_inside_loop()
        if not pages:
            return None
        if target_id:
            for idx, page in enumerate(pages):
                snap = await self._page_snapshot_inside_loop(page, idx)
                if str(snap.get("target_id") or "") == str(target_id):
                    return page
        if 0 <= index < len(pages):
            return pages[index]
        owned = await self._first_owned_page_inside_loop()
        if owned is not None:
            return owned
        return pages[0]

    def _store_live_frame(self, data: str, metadata: dict[str, Any]) -> None:
        with self._live_frame_lock:
            self._live_seq += 1
            self._live_frame = {
                "data": data,
                "metadata": dict(metadata or {}),
                "seq": self._live_seq,
                "ts": time.time(),
            }

    async def _start_live_inside(
        self, target_id: str, index: int, max_width: int, max_height: int, quality: int, every_nth: int
    ) -> dict[str, Any]:
        await self._ensure_browser_inside_loop()
        await self._stop_live_inside()
        page = await self._resolve_live_page_inside(target_id=target_id, index=index)
        if page is None:
            return {"ok": False, "error": "no page to stream"}
        session = await page.context.new_cdp_session(page)
        snap = await self._page_snapshot_inside_loop(page, -1)

        def _on_frame(payload: dict[str, Any]) -> None:
            data = str(payload.get("data") or "")
            metadata = payload.get("metadata") or {}
            session_id = payload.get("sessionId")
            self._store_live_frame(data, metadata)
            try:
                asyncio.ensure_future(session.send("Page.screencastFrameAck", {"sessionId": session_id}))
            except Exception:
                pass

        session.on("Page.screencastFrame", _on_frame)
        await session.send("Page.enable")
        await session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": int(max(10, min(90, quality))),
                "maxWidth": int(max(320, max_width)),
                "maxHeight": int(max(240, max_height)),
                "everyNthFrame": int(max(1, every_nth)),
            },
        )
        self._live_session = session
        self._live_page = page
        self._live_target_id = str(snap.get("target_id") or "")
        with self._live_frame_lock:
            self._live_frame = {}
            self._live_seq = 0
        _LIVE_CONTROL.bind(self)
        _LIVE_CONTROL.set_screencast(True)
        return {"ok": True, "target_id": self._live_target_id, "url": snap.get("url", ""), "title": snap.get("title", "")}

    async def _stop_live_inside(self) -> dict[str, Any]:
        session = self._live_session
        self._live_session = None
        self._live_page = None
        self._live_target_id = ""
        if session is not None:
            try:
                await session.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await session.detach()
            except Exception:
                pass
        _LIVE_CONTROL.set_screencast(False)
        return {"ok": True}

    async def _dispatch_live_inside(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        from argusai.core.browser.browser_live import build_key_events, build_mouse_event

        session = self._live_session
        if session is None:
            return {"ok": False, "error": "live view not running"}
        sent = 0
        for ev in events or []:
            domain = str(ev.get("domain") or "mouse")
            try:
                if domain == "mouse":
                    await session.send("Input.dispatchMouseEvent", build_mouse_event(ev))
                    sent += 1
                elif domain == "key":
                    for call in build_key_events(ev):
                        method = call.pop("_method")
                        await session.send(method, call)
                        sent += 1
            except Exception as exc:
                log.debug("live input dispatch failed: %s", exc)
        return {"ok": True, "sent": sent}

    async def _thumbnails_inside(self, max_tabs: int, quality: int, scale: float) -> list[dict[str, Any]]:
        pages = await self._list_pages_inside_loop()
        out: list[dict[str, Any]] = []
        for idx, page in enumerate(pages[: max(1, max_tabs)]):
            snap = await self._page_snapshot_inside_loop(page, idx)
            data = ""
            session = None
            try:
                session = await page.context.new_cdp_session(page)
                shot = await session.send(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": int(max(20, min(70, quality))), "captureBeyondViewport": False},
                )
                data = str(shot.get("data") or "")
            except Exception:
                data = ""
            finally:
                if session is not None:
                    try:
                        await session.detach()
                    except Exception:
                        pass
            out.append({
                "target_id": snap.get("target_id", ""),
                "title": snap.get("title", ""),
                "url": snap.get("url", ""),
                "ai_owned": bool(snap.get("ai_owned")),
                "index": idx,
                "thumb": data,
            })
        return out

    def live_thumbnails(self, *, max_tabs: int = 12, quality: int = 45, scale: float = 0.4) -> dict[str, Any]:
        """Low-fps still screenshot of every open tab for the grid overview."""
        self._ensure_thread()
        cf = asyncio.run_coroutine_threadsafe(self._thumbnails_inside(max_tabs, quality, scale), self._loop)  # type: ignore[arg-type]
        try:
            tabs = cf.result(timeout=20)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "tabs": []}
        return {"ok": True, "tabs": tabs}

    def start_live_view(
        self,
        *,
        target_id: str = "",
        index: int = -1,
        max_width: int = 1280,
        max_height: int = 900,
        quality: int = 55,
        every_nth: int = 1,
    ) -> dict[str, Any]:
        self._ensure_thread()
        coro = self._start_live_inside(target_id, index, max_width, max_height, quality, every_nth)
        cf = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return cf.result(timeout=30)

    def stop_live_view(self) -> dict[str, Any]:
        if not self._thread or not self._thread.is_alive() or not self._loop:
            _LIVE_CONTROL.unbind(self)
            return {"ok": True}
        cf = asyncio.run_coroutine_threadsafe(self._stop_live_inside(), self._loop)  # type: ignore[arg-type]
        try:
            out = cf.result(timeout=10)
        except Exception as exc:
            out = {"ok": False, "error": str(exc)}
        _LIVE_CONTROL.unbind(self)
        return out

    def live_frame(self) -> dict[str, Any]:
        """Return the latest screencast frame without hitting the hub loop."""
        with self._live_frame_lock:
            frame = dict(self._live_frame)
        running = self._live_session is not None
        ctrl = _LIVE_CONTROL.snapshot()
        return {
            "ok": True,
            "running": running,
            "frame": frame.get("data", ""),
            "seq": frame.get("seq", 0),
            "metadata": frame.get("metadata", {}),
            "target_id": self._live_target_id,
            "control": ctrl.get("mode"),
            "agent_busy": ctrl.get("agent_busy"),
            "can_take": ctrl.get("can_take"),
        }

    def dispatch_live_input(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not _LIVE_CONTROL.user_holds(self):
            return {"ok": False, "error": "control_not_held", "control": _LIVE_CONTROL.snapshot().get("mode")}
        if self._live_session is None:
            return {"ok": False, "error": "live view not running"}
        cf = asyncio.run_coroutine_threadsafe(self._dispatch_live_inside(events), self._loop)  # type: ignore[arg-type]
        return cf.result(timeout=10)

    def status(self) -> dict[str, Any]:
        try:
            connected = bool(self._browser and self._browser.is_connected())
        except Exception:
            connected = False
        from argusai.core.browser.browser_profiles import profile_summary

        return {
            "alive": connected,
            "headless": self._headless,
            "engine": self._engine,
            "channel": self._channel,
            "executable_path": self._executable_path,
            "resolved_source": self._resolved_source,
            "resolved_label": self._resolved_label,
            "profile_name": self._profile_name,
            "profile": profile_summary(self._profile) if self._profile else {},
            "has_context": self._context is not None,
            "pages_served": self._pages_served,
            "context_resets": self._context_resets,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "attached": bool(self._attached_endpoint),
            "attached_endpoint": self._attached_endpoint,
        }

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        if not self._thread or not self._thread.is_alive():
            return
        if not self._loop or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_event.set)  # type: ignore[union-attr]
        except Exception:
            return
        self._thread.join(timeout=timeout)


_HUBS: dict[str, BrowserHub] = {}
_HUB_LOCK = threading.Lock()
# Set by ``browser_attach_existing``. When live, every Chromium-family
# request from the tools is redirected to the attached hub so the AI
# operates on the user's real browser session instead of starting a new
# headless one.
_ATTACHED_HUB: BrowserHub | None = None

# The hub whose page a browser tool most recently served. The in-app
# "live browser" viewer defaults to streaming this one so the human sees
# whatever the agent is currently driving.
_ACTIVE_HUB: BrowserHub | None = None
_ACTIVE_LOCK = threading.Lock()

# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op.
_UI_NOTIFY: Callable[[str, dict[str, Any]], None] | None = None


def set_browser_ui_notify(callback: "Callable[[str, dict[str, Any]], None] | None") -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


def notify_browser_ui(payload: dict[str, Any] | None = None) -> None:
    """Best-effort push so the Browser live-view panel refreshes on change."""
    cb = _UI_NOTIFY
    if cb is None:
        return
    try:
        cb("browser_live", payload or {})
    except Exception:
        log.debug("browser ui notify failed", exc_info=True)


def mark_hub_active(hub: "BrowserHub") -> None:
    """Record ``hub`` as the browser the agent is currently driving."""
    global _ACTIVE_HUB
    with _ACTIVE_LOCK:
        changed = _ACTIVE_HUB is not hub
        _ACTIVE_HUB = hub
    if changed:
        notify_browser_ui({"reason": "active_hub"})


def active_browser_hub() -> "BrowserHub | None":
    """Best-effort "hub the agent is driving" for the live viewer.

    Prefers the CDP-attached hub (the user's real browser) when present,
    otherwise the most recently served managed hub.
    """
    if _ATTACHED_HUB is not None:
        return _ATTACHED_HUB
    with _ACTIVE_LOCK:
        return _ACTIVE_HUB


def set_attached_hub(hub: BrowserHub | None) -> None:
    """Install ``hub`` as the global CDP-attached override (or clear it)."""
    global _ATTACHED_HUB
    with _HUB_LOCK:
        _ATTACHED_HUB = hub
    if hub is not None:
        mark_hub_active(hub)
    else:
        notify_browser_ui({"reason": "detached"})


def get_attached_hub() -> BrowserHub | None:
    return _ATTACHED_HUB


def clear_attached_hub(shutdown: bool = True) -> None:
    """Detach: drop the override and disconnect (but don't kill) the browser."""
    global _ATTACHED_HUB
    with _HUB_LOCK:
        hub = _ATTACHED_HUB
        _ATTACHED_HUB = None
    if hub and shutdown:
        try:
            hub.shutdown(timeout=3.0)
        except Exception:
            pass
    notify_browser_ui({"reason": "detached"})


def shutdown_all_browser_hubs(timeout: float = 1.0) -> None:
    """Best-effort fast shutdown of every managed browser hub."""
    global _ATTACHED_HUB
    with _HUB_LOCK:
        hubs = list(_HUBS.values())
        if _ATTACHED_HUB is not None:
            hubs.append(_ATTACHED_HUB)
        _HUBS.clear()
        _ATTACHED_HUB = None
    seen: set[int] = set()
    for hub in hubs:
        if id(hub) in seen:
            continue
        seen.add(id(hub))
        try:
            hub.shutdown(timeout=timeout)
        except Exception:
            pass


def get_browser_hub(
    *,
    headless: bool = True,
    channel: str = "chrome",
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    executable_path: str = "",
) -> BrowserHub:
    """Return the shared hub for ``(engine, channel, executable, headless, profile)``.

    A different fingerprint profile / browser binary yields a different
    hub so each combination runs in its own browser process and context.

    When an attached hub is installed via :func:`set_attached_hub` and
    the caller asks for ``engine == "chromium"``, we transparently return
    the attached hub. Asking for firefox / webkit still goes through the
    normal launch path because CDP attach only exists for Chromium.
    """
    from argusai.core.browser.browser_profiles import profile_key as _profile_key
    from argusai.core.browser.browser_profiles import resolve_profile

    engine = (engine or "chromium").lower()
    if engine not in _VALID_ENGINES:
        engine = "chromium"
    if engine == "chromium" and _ATTACHED_HUB is not None:
        return _ATTACHED_HUB
    profile_name, resolved = resolve_profile(profile, fallback_engine=engine)
    # If the preset declares a preferred engine, follow it so e.g. asking
    # for "desktop_safari_mac" auto-routes through webkit.
    declared_engine = (resolved.get("engine") or "").lower()
    if declared_engine in _VALID_ENGINES:
        engine = declared_engine
    pkey = _profile_key(resolved) if resolved else "none"
    exec_tag = ""
    if executable_path:
        import hashlib

        exec_tag = hashlib.sha1(executable_path.encode("utf-8", errors="replace")).hexdigest()[:8]
    key = (
        f"{engine}|{channel if engine == 'chromium' else ''}"
        f"|{exec_tag}|{int(bool(headless))}|{profile_name}|{pkey}"
    )
    hub = _HUBS.get(key)
    if hub is not None:
        return hub
    with _HUB_LOCK:
        hub = _HUBS.get(key)
        if hub is None:
            hub = BrowserHub(
                headless=headless,
                channel=channel,
                engine=engine,
                profile_name=profile_name,
                profile=resolved,
                executable_path=executable_path,
            )
            _HUBS[key] = hub
            atexit.register(hub.shutdown)
        return hub


def watchable_browser_hubs() -> list["BrowserHub"]:
    """Hubs the live browser panel should show.

    The CDP-attached real browser lives outside ``_HUBS``. Team members each
    get their own managed hub. A hub that has never launched is omitted so
    opening the panel does not start a browser by itself.
    """
    ordered: list[BrowserHub] = []
    seen: set[int] = set()
    candidates: list[BrowserHub] = []
    if _ATTACHED_HUB is not None:
        candidates.append(_ATTACHED_HUB)
    candidates.extend(list(_HUBS.values()))
    for hub in candidates:
        if id(hub) in seen:
            continue
        seen.add(id(hub))
        try:
            st = hub.status()
        except Exception:
            continue
        if st.get("attached") or st.get("alive") or st.get("thread_alive") or int(st.get("pages_served") or 0) > 0:
            ordered.append(hub)
    return ordered


def list_browser_hubs() -> list[dict[str, Any]]:
    return [hub.status() for hub in watchable_browser_hubs()]


def _page_worth_showing(page: Any) -> bool:
    """Skip extension and DevTools targets that crowd an attached real browser."""
    try:
        url = str(page.url or "")
    except Exception:
        return True
    lowered = url.lower()
    return not lowered.startswith(("chrome-extension://", "devtools://"))
