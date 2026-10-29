"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .common import *
from .common import _extract_port

def browser_system_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Inventory Chromium-family browsers installed on this machine and report whether Playwright already has bundled engines downloaded. Use this to pick a real browser to drive (Chrome / Edge / Brave / Vivaldi) instead of asking the user to download Playwright's bundled Chromium. Returns ``system_browsers`` list (brand/label/channel/executable_path/version), ``playwright_bundle`` summary, and ``recommended`` hint for browser_probe / browser_evaluate channel/executable_path parameters."""
    from argusai.core.browser.browser_locator import (
        playwright_bundle_status,
        resolve_browser_binary,
        scan_system_browsers,
    )

    system = scan_system_browsers()
    bundle = playwright_bundle_status()
    recommended = resolve_browser_binary()
    return {
        "ok": True,
        "system_browsers": system,
        "playwright_bundle": bundle,
        "recommended": recommended,
        "note": (
            "Pass channel=<chrome|msedge|...> for system browsers (preferred), "
            "or executable_path=<absolute path> for any Chromium-family binary. "
            "Firefox / WebKit can only run via the Playwright bundle "
            "(call browser_install_engine to download)."
        ),
    }


async def browser_install_engine(api: Any, engine: str = "chromium", **kwargs: Any) -> dict[str, Any]:
    """Trigger ``playwright install <engine>`` to download Playwright's bundled browser binaries. ``engine`` accepts chromium | firefox | webkit. Use this when browser_system_inventory shows no system Chrome / Edge AND the user wants the offline bundled engine. The download is ~120-300 MB and runs in the background; this tool returns once Playwright finishes (or fails)."""
    import asyncio as _asyncio
    import logging as _logging
    from argusai.core.infra.win_stdio import devnull_stdin_fd, ensure_valid_stdio, quiet_subprocess_kwargs

    _log = _logging.getLogger("argusai.browser_install")

    eng = (engine or "chromium").strip().lower()
    if eng not in ("chromium", "firefox", "webkit"):
        return {"ok": False, "error": f"unknown engine {engine!r}"}
    try:
        from argusai.core.browser.browser_locator import playwright_install_argv

        cmd = playwright_install_argv(eng)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "engine": eng}
    _log.info("browser_install_engine start engine=%s", eng)
    ensure_valid_stdio()
    stdin_fd = devnull_stdin_fd()
    try:
        try:
            proc = await _asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_fd if stdin_fd is not None else _asyncio.subprocess.DEVNULL,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.STDOUT,
                **quiet_subprocess_kwargs(),
            )
        finally:
            if stdin_fd is not None:
                try:
                    import os as _os

                    _os.close(stdin_fd)
                except OSError:
                    pass
    except FileNotFoundError as e:
        _log.warning("browser_install_engine spawn failed: %s", e)
        return {"ok": False, "error": f"无法启动 playwright 安装命令: {e}"}
    try:
        stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=600)
    except _asyncio.TimeoutError:
        try:
            proc.terminate()
        except Exception:
            pass
        _log.warning("browser_install_engine timeout engine=%s", eng)
        return {"ok": False, "error": "playwright install 超过 10 分钟未完成,已取消"}
    if proc.returncode != 0:
        _log.warning("browser_install_engine nonzero engine=%s exit_code=%s", eng, proc.returncode)
    else:
        _log.info("browser_install_engine ok engine=%s", eng)
    output = (stdout or b"").decode("utf-8", errors="replace")
    if len(output) > 8000:
        output = output[:4000] + "\n...\n" + output[-3500:]
    from argusai.core.browser.browser_locator import playwright_bundle_status

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "engine": eng,
        "log": output.strip(),
        "playwright_bundle": playwright_bundle_status(),
    }


# ---------- CDP attach: drive the user's real browser ---------------
#
# These tools let the AI see the tabs the user already has open and act
# inside their real, logged-in profile. Workflow:
#
#   1. browser_launch_for_attach(channel="msedge")
#        -> kills the user's running Edge (after explicit confirmation
#           upstream), relaunches it with --remote-debugging-port=9222
#           pointing at their default user-data-dir.
#   2. browser_attach_existing(endpoint="http://127.0.0.1:9222")
#        -> Playwright connect_over_cdp; the global hub override is set
#           so every later browser_* tool reuses this real browser
#           instead of starting a fresh headless one.
#   3. browser_list_tabs() / browser_use_tab(...)
#        -> enumerate / pick a specific tab.
#   4. browser_detach()
#        -> drop the override (the user's browser keeps running).


def browser_attach_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report whether the AI is currently attached to a real running browser via CDP. Returns ``attached`` bool, ``endpoint``, ``alive`` (does the DevTools endpoint still answer), and ``hub_status`` snapshot. Cheap to call; safe to use before deciding whether to launch / attach."""
    from argusai.core.browser.browser_attach import cdp_endpoint_alive
    from argusai.core.browser.browser_pool import get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": True, "attached": False}
    st = hub.status()
    endpoint = st.get("attached_endpoint") or ""
    port = _extract_port(endpoint) if endpoint.startswith("http") else 0
    alive = cdp_endpoint_alive(port) if port else {"ok": False}
    return {
        "ok": True,
        "attached": True,
        "endpoint": endpoint,
        "alive": bool(alive.get("ok")),
        "browser": alive.get("browser", ""),
        "hub_status": st,
    }


async def browser_launch_for_attach(
    api: Any,
    channel: str = "msedge",
    port: int = 9222,
    use_my_profile: bool = True,
    user_data_dir: str = "",
    executable_path: str = "",
    kill_existing: bool = True,
    show_window: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Spawn the user's real browser (Edge / Chrome / Brave / Vivaldi) with ``--remote-debugging-port=<port>`` so it can later be controlled via CDP. Cookies, logged-in sessions, open tabs and extensions are preserved because we point ``--user-data-dir`` at the user's existing profile (auto-detected from ``channel`` unless ``user_data_dir`` is given). When ``kill_existing=True`` (default) any running instance of that channel is killed first — Chromium would otherwise silently dedupe into the existing process and ignore the debug port. Launched HIDDEN by default (``--headless=new``): the agent drives it off-screen and the user watches via the in-app Browser live view. Pass ``show_window=True`` only if the user explicitly wants the real browser window visible. After this returns ok, call ``browser_attach_existing`` with the same endpoint to start driving it. Requires explicit user confirmation; this WILL close the user's current browser window. Returns ``ok``, ``endpoint``, ``pid``, ``headless``, ``user_data_dir`` actually used, and ``browser`` (DevTools-reported browser/version)."""
    from argusai.core.browser.browser_attach import (
        argusai_cdp_profile_path,
        channel_friendly_label,
        find_default_profile_path,
        kill_browser_processes,
        launch_with_debug_port,
    )
    from argusai.core.browser.browser_locator import find_channel_executable, resolve_browser_binary

    ch = (channel or "msedge").strip().lower()
    exe = (executable_path or "").strip()
    if not exe:
        # resolve_browser_binary returns a channel hint without an exe
        # path when it picks an installed Playwright channel; we need
        # the real path for subprocess.Popen, so probe scan_system_browsers.
        hint = resolve_browser_binary(explicit_channel=ch)
        exe = hint.get("executable_path") or find_channel_executable(ch)
    if not exe:
        return {
            "ok": False,
            "error": f"could not locate a binary for channel {ch!r}; call browser_system_inventory or pass executable_path",
        }
    udd = (user_data_dir or "").strip()
    if not udd and use_my_profile:
        udd = find_default_profile_path(ch)
    kill_result: dict[str, Any] = {}
    if kill_existing:
        kill_result = kill_browser_processes(ch)
    headless = not bool(show_window)
    launch = launch_with_debug_port(
        executable_path=exe,
        user_data_dir=udd,
        port=int(port),
        headless=headless,
    )
    fallback_profile = ""
    if not launch.get("ok") and use_my_profile:
        fallback_profile = argusai_cdp_profile_path(ch)
        retry = launch_with_debug_port(
            executable_path=exe,
            user_data_dir=fallback_profile,
            port=int(port),
            headless=headless,
        )
        if retry.get("ok"):
            launch = retry
            udd = fallback_profile
    return {
        "ok": bool(launch.get("ok")),
        "channel": ch,
        "channel_label": channel_friendly_label(ch),
        "executable_path": exe,
        "user_data_dir": udd,
        "headless": headless,
        "fallback_profile": fallback_profile,
        "kill_result": kill_result,
        "pid": launch.get("pid"),
        "endpoint": launch.get("endpoint"),
        "browser": launch.get("browser"),
        "error": launch.get("error", ""),
        "argv": launch.get("argv", []),
        "hint": (
            "On success, immediately call browser_attach_existing with the returned endpoint. "
            "If the user previously had unsaved work in their browser, advise them."
        ),
    }


async def browser_attach_existing(
    api: Any,
    endpoint: str = "http://127.0.0.1:9222",
    **kwargs: Any,
) -> dict[str, Any]:
    """Connect to an already-running Chromium browser via Chrome DevTools Protocol. After this succeeds every subsequent browser_probe / browser_evaluate / browser_trace / browser_list_tabs / browser_use_tab call routes through the attached browser (the user's real profile, with all their cookies and open tabs). The browser MUST have been started with ``--remote-debugging-port=<port>`` (use ``browser_launch_for_attach`` if not). Idempotent: if we are already attached to the same endpoint and the CDP server is alive, returns ``reused=True`` without re-attaching. Does not kill or modify the browser. Returns ``ok``, ``endpoint``, ``browser`` (DevTools banner), ``tabs`` (current page list), ``reused``, and ``hub_status``."""
    from argusai.core.browser.browser_attach import cdp_endpoint_alive
    from argusai.core.browser.browser_pool import (
        BrowserHub,
        clear_attached_hub,
        get_attached_hub,
        set_attached_hub,
    )

    ep = (endpoint or "").strip()
    if not ep:
        return {"ok": False, "error": "endpoint is required (e.g. http://127.0.0.1:9222)"}
    info = cdp_endpoint_alive(_extract_port(ep))
    if not info.get("ok"):
        return {
            "ok": False,
            "error": f"CDP endpoint {ep} is not responding. "
            "Make sure the browser is running with --remote-debugging-port=<port>.",
        }
    # Idempotency: if we already have a hub pointing at the same endpoint
    # and it's still alive, reuse it. Tearing it down and rebuilding for
    # the same endpoint would risk closing the user's tabs on some
    # Playwright/Edge combos.
    existing = get_attached_hub()
    if existing is not None:
        existing_endpoint = (existing.status().get("attached_endpoint") or "").strip()
        if existing_endpoint == ep:
            try:
                tabs = await existing.alist_pages()
                windows = existing._group_windows(tabs)  # type: ignore[attr-defined]
                return {
                    "ok": True,
                    "reused": True,
                    "endpoint": ep,
                    "browser": info.get("browser", ""),
                    "tabs": tabs,
                    "windows": windows,
                    "window_count": len(windows),
                    "hub_status": existing.status(),
                    "note": "Already attached to this endpoint — reused the existing connection.",
                }
            except Exception:
                # Stale connection — fall through and rebuild.
                pass
    clear_attached_hub(shutdown=True)
    hub = BrowserHub(attached_endpoint=ep)
    try:
        tabs = await hub.alist_pages()
        windows = hub._group_windows(tabs)  # type: ignore[attr-defined]
    except Exception as e:
        try:
            hub.shutdown()
        except Exception:
            pass
        return {"ok": False, "error": f"connect_over_cdp failed: {e}"}
    set_attached_hub(hub)
    return {
        "ok": True,
        "reused": False,
        "endpoint": ep,
        "browser": info.get("browser", ""),
        "tabs": tabs,
        "windows": windows,
        "window_count": len(windows),
        "hub_status": hub.status(),
        "note": "Subsequent browser_* tools now operate on the user's real browser session. Call browser_detach to release.",
    }


def browser_detach(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Release the CDP attachment so future browser_* tools go back to using the managed headless Playwright browser. Does NOT close the user's actual browser window. Safe to call when not attached (returns ``ok=True, attached=False``)."""
    from argusai.core.browser.browser_pool import clear_attached_hub, get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": True, "attached": False, "note": "nothing was attached"}
    endpoint = hub.status().get("attached_endpoint", "")
    clear_attached_hub(shutdown=True)
    return {
        "ok": True,
        "attached": False,
        "previous_endpoint": endpoint,
        "note": "Detached from CDP. The browser process keeps running.",
    }


def browser_resume_control(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Reclaim control of the live browser after the human took it over.

    When the user clicks "take control" (or "force takeover") in the Browser
    live view, your browser_* page operations are blocked with a
    ``BrowserControlHeld`` error until control returns. Call this to take
    control back so you can keep driving. Use it ONLY once the user has
    finished or asked you to continue (e.g. they logged in / solved a captcha
    and told you to proceed) — do not yank control away while they are still
    interacting. Returns ``ok`` and the resulting ``control`` mode ("agent")."""
    del api, kwargs
    from argusai.core.browser.browser_live import CONTROL
    from argusai.core.browser.browser_pool import notify_browser_ui

    before = CONTROL.snapshot().get("mode")
    out = CONTROL.release()
    notify_browser_ui({"reason": "control"})
    return {"ok": True, "previous_mode": before, "control": out.get("mode", "agent")}


async def browser_list_tabs(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List every open tab in the currently attached browser.

    Returns both a backwards-compatible flat ``tabs`` list and a window-aware
    ``windows`` grouping. Prefer ``window_id + tab_index`` or ``target_id`` for
    follow-up ``browser_use_tab`` calls when multiple browser windows are open.
    """
    from argusai.core.browser.browser_pool import get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}
    tabs = await hub.alist_pages()
    windows = hub._group_windows(tabs)  # type: ignore[attr-defined]
    owned_count = sum(1 for t in tabs if t.get("ai_owned"))
    return {
        "ok": True,
        "count": len(tabs),
        "owned_count": owned_count,
        "window_count": len(windows),
        "windows": windows,
        "tabs": tabs,
        "selection_hint": (
            "For new navigation, prefer browser_open_tab(reuse_existing=true) so user tabs are not overwritten. "
            "When multiple windows exist, prefer browser_use_tab(window_id=<id>, tab_index=<n>) "
            "or browser_use_tab(target_id=<target_id>) over the legacy global index. "
            "Lab links opened via target=_blank from an ai_owned tab inherit ownership automatically."
        ),
    }


async def browser_open_tab(
    api: Any,
    url: str = "",
    reuse_existing: bool = True,
    expression: str = "",
    wait_until: str = "load",
    wait_for_load: bool = True,
    navigation_timeout_ms: int = 30000,
    extra_wait_ms: int = 0,
    capture_html: bool | None = None,
    max_html_chars: int = 8000,
    max_eval_chars: int = 8000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Open or reuse a ArgusAI-owned tab in the attached browser.

    Use this instead of ``browser_use_tab(navigate_to=...)`` when the AI needs
    to visit a new URL but the user did not explicitly ask to overwrite an
    existing tab. The tab is marked ``ai_owned`` in browser_list_tabs and is
    left open for later reuse.
    """
    from argusai.core.browser.browser_pool import get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}

    nav_url = (url or "").strip()
    expr = (expression or "").strip()
    wu = (wait_until or "load").strip().lower()
    if wu not in ("load", "domcontentloaded", "networkidle", "commit"):
        wu = "load"
    load_wait = bool(wait_for_load)
    if not load_wait:
        wu = "commit"
    nav_timeout = _safe_int(
        navigation_timeout_ms,
        30000 if load_wait else 8000,
        minimum=1000,
        maximum=120000,
    )
    settle_ms = _safe_int(extra_wait_ms, 0, minimum=0, maximum=30000)
    cap_html = (not expr) if capture_html is None else bool(capture_html)
    max_chars = _safe_int(max_html_chars, 8000, minimum=200, maximum=200000)
    eval_cap = _safe_int(max_eval_chars, 8000, minimum=256, maximum=50_000)

    async def _run(page: Any) -> dict[str, Any]:
        navigated = False
        nav_err = ""
        if nav_url:
            try:
                await page.goto(nav_url, wait_until=wu, timeout=nav_timeout)
                navigated = True
            except Exception as e:
                nav_err = str(e)
            if settle_ms:
                try:
                    await page.wait_for_timeout(settle_ms)
                except Exception:
                    pass
        title = ""
        page_url = ""
        try:
            title = await page.title()
        except Exception:
            pass
        try:
            page_url = page.url or ""
        except Exception:
            pass
        tab_snapshot: dict[str, Any] = {"title": title, "url": page_url, "ai_owned": True}
        try:
            tab_snapshot = await hub._page_snapshot_inside_loop(page, -1)  # type: ignore[attr-defined]
            tab_snapshot = hub._assign_window_indices([tab_snapshot])[0]  # type: ignore[attr-defined]
            tab_snapshot["title"] = title
            tab_snapshot["url"] = page_url
            tab_snapshot["ai_owned"] = True
        except Exception:
            pass
        html = ""
        if cap_html:
            try:
                html = await page.evaluate("document.documentElement.outerHTML")
            except Exception as e:
                html = f"[outerHTML capture failed: {e}]"
            if isinstance(html, str) and len(html) > max_chars:
                html = html[:max_chars] + "\n... [truncated]"
        evaluation: Any = None
        eval_err = ""
        eval_size: int | None = None
        eval_truncated = False
        if expr:
            try:
                evaluation = await page.evaluate(expr)
            except Exception as e:
                eval_err = str(e)
            import json as _json
            if not isinstance(evaluation, (str, int, float, bool)) and evaluation is not None:
                try:
                    serialised = _json.dumps(evaluation, ensure_ascii=False, default=str)
                except Exception:
                    serialised = repr(evaluation)
                eval_size = len(serialised)
                if eval_size > eval_cap:
                    evaluation = serialised[:eval_cap] + f"...[truncated {eval_size - eval_cap} chars; pass max_eval_chars= or narrow your expression]"
                    eval_truncated = True
                else:
                    evaluation = serialised
            elif isinstance(evaluation, str) and len(evaluation) > eval_cap:
                eval_size = len(evaluation)
                evaluation = evaluation[:eval_cap] + f"...[truncated {eval_size - eval_cap} chars]"
                eval_truncated = True
        return {
            "tab": tab_snapshot,
            "navigated": navigated,
            "navigation_error": nav_err,
            "wait_until": wu,
            "wait_for_load": load_wait,
            "navigation_timeout_ms": nav_timeout,
            "html": html if cap_html else "",
            "evaluation": evaluation,
            "evaluation_error": eval_err,
            "evaluation_size_chars": eval_size,
            "evaluation_truncated": eval_truncated,
        }

    try:
        result = await hub.aopen_owned_tab(_run, reuse_existing=bool(reuse_existing))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "ai_owned": True, **result}


async def browser_use_tab(
    api: Any,
    index: int | None = None,
    window_id: int | None = None,
    window_index: int | None = None,
    tab_index: int | None = None,
    target_id: str = "",
    url_contains: str = "",
    title_contains: str = "",
    navigate_to: str = "",
    expression: str = "",
    wait_until: str = "load",
    wait_for_load: bool = True,
    navigation_timeout_ms: int = 30000,
    extra_wait_ms: int = 0,
    capture_html: bool | None = None,
    max_html_chars: int = 8000,
    max_eval_chars: int = 8000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Operate on a single tab in the attached browser. The tab stays open after the call — we never close it. This is the workhorse for driving the user's real browser across many steps: pick a tab once, then keep reusing it via ``navigate_to`` instead of opening a new tab per URL.

    Tab selection: prefer ``target_id`` or ``window_id + tab_index`` from
    browser_list_tabs when multiple browser windows are open. Legacy ``index``
    (global position), ``url_contains``, and ``title_contains`` are still
    supported. If none match, falls back to tab 0.

    ``navigate_to``: if non-empty, navigate the matched tab to this URL before running ``expression``. Use this to walk a SEQUENCE of URLs on the same tab — much less noisy than opening one new tab per URL. ``wait_for_load=False`` returns after navigation commits (useful when the visible page is open but assets keep loading); otherwise ``wait_until`` ∈ {load, domcontentloaded, networkidle, commit} controls when navigation is considered complete. ``extra_wait_ms`` adds a final settle delay (0-30000).

    ``expression``: JS evaluated against the page after any navigation. Prefer a targeted expression that returns ONLY the structured data you need.

    ``capture_html`` is auto-smart: TRUE when no expression is provided (recon), FALSE when expression is set (the JS result is what you want). Override only if you genuinely need both.

    Returns ``ok``, ``tab`` (matched tab info), ``navigated`` (bool), ``html`` (when captured), ``evaluation``, ``evaluation_error``, and ``error``."""
    from argusai.core.browser.browser_pool import get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}

    match: dict[str, Any] = {}
    if target_id:
        match["target_id"] = str(target_id)
    if window_id is not None:
        try:
            match["window_id"] = int(window_id)
        except (TypeError, ValueError):
            pass
    if window_index is not None:
        try:
            match["window_index"] = int(window_index)
        except (TypeError, ValueError):
            pass
    if tab_index is not None:
        try:
            match["tab_index"] = int(tab_index)
        except (TypeError, ValueError):
            pass
    if index is not None:
        try:
            match["index"] = int(index)
        except (TypeError, ValueError):
            pass
    if url_contains:
        match["url_contains"] = str(url_contains)
    if title_contains:
        match["title_contains"] = str(title_contains)

    expr = (expression or "").strip()
    nav_url = (navigate_to or "").strip()
    wu = (wait_until or "load").strip().lower()
    if wu not in ("load", "domcontentloaded", "networkidle", "commit"):
        wu = "load"
    load_wait = bool(wait_for_load)
    if not load_wait:
        wu = "commit"
    nav_timeout = _safe_int(
        navigation_timeout_ms,
        30000 if load_wait else 8000,
        minimum=1000,
        maximum=120000,
    )
    settle_ms = _safe_int(extra_wait_ms, 0, minimum=0, maximum=30000)
    # Smart default: when the AI provides a JS expression it already
    # knows what data it wants — dumping HTML on top is just expensive
    # bytes and longer prompts on the next round. Only auto-capture HTML
    # when there's no expression (pure recon).
    if capture_html is None:
        cap_html = not expr
    else:
        cap_html = bool(capture_html)
    max_chars = _safe_int(max_html_chars, 8000, minimum=200, maximum=200000)
    eval_cap = _safe_int(max_eval_chars, 8000, minimum=256, maximum=50_000)

    async def _run(page: Any) -> dict[str, Any]:
        navigated = False
        nav_err: str = ""
        if nav_url:
            try:
                await page.goto(nav_url, wait_until=wu, timeout=nav_timeout)
                navigated = True
            except Exception as e:
                nav_err = str(e)
            if settle_ms:
                try:
                    await page.wait_for_timeout(settle_ms)
                except Exception:
                    pass
        title = ""
        url = ""
        try:
            title = await page.title()
        except Exception:
            pass
        try:
            url = page.url or ""
        except Exception:
            pass
        tab_snapshot: dict[str, Any] = {"title": title, "url": url}
        try:
            tab_snapshot = await hub._page_snapshot_inside_loop(page, -1)  # type: ignore[attr-defined]
            tab_snapshot = hub._assign_window_indices([tab_snapshot])[0]  # type: ignore[attr-defined]
            tab_snapshot["title"] = title
            tab_snapshot["url"] = url
        except Exception:
            pass
        html: str = ""
        if cap_html:
            try:
                html = await page.evaluate("document.documentElement.outerHTML")
            except Exception as e:
                html = f"[outerHTML capture failed: {e}]"
            if isinstance(html, str) and len(html) > max_chars:
                html = html[:max_chars] + "\n... [truncated]"
        evaluation: Any = None
        eval_err: str = ""
        eval_size: int | None = None
        eval_truncated = False
        if expr:
            try:
                evaluation = await page.evaluate(expr)
            except Exception as e:
                eval_err = str(e)
            # Always size + truncate the evaluation result before
            # returning. A naïve ``return document.body`` can return
            # several MB of serialised DOM, which would dominate the
            # LLM's next prompt.
            import json as _json
            if not isinstance(evaluation, (str, int, float, bool)) and evaluation is not None:
                try:
                    serialised = _json.dumps(evaluation, ensure_ascii=False, default=str)
                except Exception:
                    serialised = repr(evaluation)
                eval_size = len(serialised)
                if eval_size > eval_cap:
                    evaluation = serialised[:eval_cap] + f"...[truncated {eval_size - eval_cap} chars; pass max_eval_chars= or narrow your expression]"
                    eval_truncated = True
                else:
                    evaluation = serialised
            elif isinstance(evaluation, str) and len(evaluation) > eval_cap:
                eval_size = len(evaluation)
                evaluation = evaluation[:eval_cap] + f"...[truncated {eval_size - eval_cap} chars]"
                eval_truncated = True
        return {
            "tab": tab_snapshot,
            "navigated": navigated,
            "navigation_error": nav_err,
            "wait_until": wu,
            "wait_for_load": load_wait,
            "navigation_timeout_ms": nav_timeout,
            "html": html if cap_html else "",
            "evaluation": evaluation,
            "evaluation_error": eval_err,
            "evaluation_size_chars": eval_size,
            "evaluation_truncated": eval_truncated,
        }

    try:
        result = await hub.acall_with_existing_page(_run, match=match)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **result}


async def browser_screenshot(
    api: Any,
    url: str = "",
    scope: str = "",
    out_path: str = "",
    selector: str = "",
    full_page: bool = False,
    attach_to_model: bool = True,
    headless: bool = True,
    wait_until: str = "load",
    wait_for_load: bool = True,
    wait_for_selector: str = "",
    extra_wait_ms: int = 0,
    navigation_timeout_ms: int = 30000,
    index: int | None = None,
    window_id: int | None = None,
    window_index: int | None = None,
    tab_index: int | None = None,
    target_id: str = "",
    url_contains: str = "",
    title_contains: str = "",
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    fresh_context: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Capture a browser screenshot to the session sandbox and optionally attach it to the next model call."""
    import base64
    import hashlib
    import time
    from pathlib import Path

    from argusai.core.infra.command import in_scope, normalize_scope
    from argusai.core.browser.browser_pool import get_attached_hub, get_browser_hub

    nav_url = str(url or "").strip()
    if nav_url and not in_scope(nav_url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {nav_url}", "url": nav_url}
    if not nav_url and not get_attached_hub():
        return {"ok": False, "error": "url is required when no CDP browser is attached"}

    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)

    raw_out = str(out_path or "").strip()
    if not raw_out:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        raw_out = f"screenshots/browser-{stamp}.png"
    if not raw_out.lower().endswith(".png"):
        raw_out += ".png"
    try:
        target, root_kind, root_path = _resolve_session_output_path(api, kwargs, raw_out)
    except Exception as e:
        return {"ok": False, "error": f"bad out_path: {e}"}
    target = Path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": f"cannot create output directory: {e}", "path": str(target)}

    wu = (wait_until or "load").strip().lower()
    if wu not in ("load", "domcontentloaded", "networkidle", "commit"):
        wu = "load"
    load_wait = bool(wait_for_load)
    if not load_wait:
        wu = "commit"
    nav_timeout = _safe_int(navigation_timeout_ms, 30000 if load_wait else 8000, 1000, 120000)
    settle_ms = _safe_int(extra_wait_ms, 0, 0, 30000)
    wait_sel = str(wait_for_selector or "").strip()
    shot_sel = str(selector or "").strip()

    async def _capture(page: Any, hub: Any | None = None) -> dict[str, Any]:
        navigated = False
        nav_err = ""
        if nav_url:
            try:
                await page.goto(nav_url, wait_until=wu, timeout=nav_timeout)
                navigated = True
            except Exception as e:
                nav_err = str(e)
        if wait_sel:
            try:
                await page.wait_for_selector(wait_sel, timeout=nav_timeout)
            except Exception as e:
                return {"ok": False, "error": f"wait_for_selector failed: {e}"}
        if settle_ms:
            try:
                await page.wait_for_timeout(settle_ms)
            except Exception:
                pass
        title = ""
        page_url = ""
        try:
            title = await page.title()
        except Exception:
            pass
        try:
            page_url = page.url or ""
        except Exception:
            pass
        tab_snapshot: dict[str, Any] = {"title": title, "url": page_url}
        if hub is not None:
            try:
                tab_snapshot = await hub._page_snapshot_inside_loop(page, -1)  # type: ignore[attr-defined]
                tab_snapshot = hub._assign_window_indices([tab_snapshot])[0]  # type: ignore[attr-defined]
                tab_snapshot["title"] = title
                tab_snapshot["url"] = page_url
            except Exception:
                pass
        try:
            if shot_sel:
                el = await page.query_selector(shot_sel)
                if el is None:
                    return {"ok": False, "error": f"selector not found: {shot_sel}", "url": page_url, "tab": tab_snapshot}
                data = await el.screenshot(type="png")
                mode = "selector"
            else:
                data = await page.screenshot(type="png", full_page=bool(full_page))
                mode = "full_page" if full_page else "viewport"
        except Exception as e:
            return {"ok": False, "error": f"screenshot failed: {e}", "url": page_url, "tab": tab_snapshot}
        return {
            "ok": True,
            "bytes": data,
            "mode": mode,
            "title": title,
            "url": page_url,
            "tab": tab_snapshot,
            "navigated": navigated,
            "navigation_error": nav_err,
        }

    match: dict[str, Any] = {}
    if target_id:
        match["target_id"] = str(target_id)
    for key, value in (("window_id", window_id), ("window_index", window_index), ("tab_index", tab_index), ("index", index)):
        if value is not None:
            try:
                match[key] = int(value)
            except (TypeError, ValueError):
                pass
    if url_contains:
        match["url_contains"] = str(url_contains)
    if title_contains:
        match["title_contains"] = str(title_contains)

    attached_hub = get_attached_hub()
    try:
        if attached_hub:
            result = await attached_hub.acall_with_existing_page(lambda page: _capture(page, attached_hub), match=match)
            source = "attached_cdp"
        else:
            hub = get_browser_hub(
                headless=bool(headless),
                channel=channel or "",
                engine=engine or "chromium",
                profile=profile or None,
                executable_path=executable_path or "",
            )
            result = await hub.acall_with_page(lambda page: _capture(page, hub), fresh_context=bool(fresh_context))
            source = "managed_browser"
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(target)}
    if not isinstance(result, dict) or not result.get("ok"):
        result = result if isinstance(result, dict) else {"ok": False, "error": "screenshot failed"}
        result.setdefault("path", str(target))
        return result

    data = result.pop("bytes", b"")
    if not isinstance(data, (bytes, bytearray)):
        return {"ok": False, "error": "screenshot did not return bytes", "path": str(target)}
    try:
        target.write_bytes(bytes(data))
    except Exception as e:
        return {"ok": False, "error": f"failed to write screenshot: {e}", "path": str(target)}

    size = int(target.stat().st_size)
    sha256 = hashlib.sha256(bytes(data)).hexdigest()
    rel_path = ""
    if root_kind == "session_sandbox" and root_path:
        try:
            rel_path = target.resolve().relative_to(Path(root_path).resolve()).as_posix()
        except Exception:
            rel_path = target.name

    response: dict[str, Any] = {
        "ok": True,
        "path": str(target),
        "relative_path": rel_path,
        "mime_type": "image/png",
        "size_bytes": size,
        "sha256": sha256,
        "source": source,
        "root_kind": root_kind,
        **result,
        "note": "Screenshot saved as PNG. Use file_resource_view or sandbox_open_file to preview it.",
    }
    # Keep the prompt lean: only route reasonably-sized screenshots through
    # the model's multimodal attachment rail. The full PNG remains on disk.
    max_attach_bytes = 4_000_000
    if attach_to_model and size <= max_attach_bytes:
        response["_assistant_attachments"] = [{
            "type": "image",
            "mime": "image/png",
            "name": target.name,
            "data_base64": base64.b64encode(bytes(data)).decode("ascii"),
        }]
        response["attached_to_model"] = True
    else:
        response["attached_to_model"] = False
        if attach_to_model:
            response["attachment_skipped_reason"] = f"screenshot too large for model attachment ({size} bytes > {max_attach_bytes})"
    return response


async def browser_close_owned_tabs(
    api: Any,
    target_id: str = "",
    target_ids: list[str] | None = None,
    url_contains: str = "",
    force: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Close ArgusAI-owned attached-browser tabs. Use force=true with target_ids only when the user explicitly asked to close specific tabs."""
    from argusai.core.browser.browser_pool import get_attached_hub

    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}
    return await hub.aclose_owned_pages(
        target_id=target_id,
        target_ids=list(target_ids or []),
        url_contains=url_contains,
        force=bool(force),
    )


async def browser_mark_tabs_owned(
    api: Any,
    target_ids: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Mark existing attached-browser tabs as ai_owned so browser_close_owned_tabs can close them later (e.g. lab popups the user asked to discard)."""
    from argusai.core.browser.browser_pool import get_attached_hub

    ids = [str(t).strip() for t in (target_ids or []) if str(t).strip()]
    if not ids:
        return {"ok": False, "error": "target_ids is required (from browser_list_tabs)"}
    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}
    return await hub.amark_pages_owned(ids)


async def browser_save_wasm(
    api: Any,
    out_path: str = "",
    expression: str = "",
    index: int | None = None,
    url_contains: str = "",
    title_contains: str = "",
    chunk_chars: int = 524288,
    max_total_bytes: int = 268435456,
    overwrite: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Save a binary WASM blob (or any ArrayBuffer / Uint8Array) from
    the attached browser to disk WITHOUT routing the bytes through the
    chat context.

    Why this exists: ``browser_evaluate`` / ``browser_use_tab`` cap their
    return ``value`` at 8 KB by default and 50 KB hard maximum to keep
    the LLM prompt small. A 6 KB WASM module → ~8 KB base64 → already
    over the default cap, and a 200 KB module is hopeless. Hex doubles
    the cost again. The right fix is to write the bytes directly from
    the browser process into the sandbox over many small chunks.

    Flow:

    1. ``expression`` is evaluated inside the matched page and MUST
       return one of: a ``Uint8Array``, an ``ArrayBuffer``, an array
       of those, or a string of base64. The page is responsible for
       hooking ``WebAssembly.instantiate`` (use the
       ``webassembly_compile`` hook recipe) and stashing the bytes
       somewhere it can be retrieved — e.g.
       ``globalThis.__argusai_wasm_samples[0]``.
    2. The browser side wraps the result in a ``chunk_chars``-sized
       base64 stream and we pull one chunk per ``page.evaluate`` round,
       writing decoded bytes straight into ``out_path``.
    3. SHA-256 is computed incrementally; the response is just
       ``{path, size_bytes, sha256, chunks}`` — never the body itself.

    ``out_path`` is required. Relative paths are written under the current
    assistant session sandbox; absolute paths are treated as explicit user
    choices. ``overwrite=False`` means existing files are refused (saves you
    from clobbering by typo).
    """
    import base64
    import hashlib
    import json as _json
    import os as _os
    from argusai.core.browser.browser_pool import get_attached_hub

    if not out_path or not str(out_path).strip():
        return {"ok": False, "error": "out_path is required"}
    if not expression or not expression.strip():
        return {
            "ok": False,
            "error": (
                "expression is required — provide a JS expression that returns "
                "a Uint8Array / ArrayBuffer / array thereof / base64 string. "
                "Typical use: hook WebAssembly.instantiate via the "
                "webassembly_compile recipe, then call this with "
                "expression='globalThis.__argusai_wasm_samples[0]'."
            ),
        }
    try:
        target, root_kind, root_path = _resolve_session_output_path(api, kwargs, out_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        return {"ok": False, "error": f"refusing to overwrite {target}; pass overwrite=true"}

    hub = get_attached_hub()
    if not hub:
        return {"ok": False, "error": "not attached; call browser_attach_existing first"}

    match: dict[str, Any] = {}
    if index is not None:
        try:
            match["index"] = int(index)
        except (TypeError, ValueError):
            pass
    if url_contains:
        match["url_contains"] = str(url_contains)
    if title_contains:
        match["title_contains"] = str(title_contains)

    chunk = _safe_int(chunk_chars, 524288, minimum=4096, maximum=2_000_000)
    max_bytes = _safe_int(max_total_bytes, 268435456, minimum=1024, maximum=1_073_741_824)

    # JS resolver — runs inside the page on every chunk. ``__argusai_save_state``
    # caches the resolved Uint8Array between rounds so we don't re-eval the
    # source expression each time (which might have side effects).
    resolver_js = """
    async (params) => {
      const ww = (globalThis.__argusai_save = globalThis.__argusai_save || {});
      const key = params.key || '__default';
      let entry = ww[key];
      if (!entry) {
        let value;
        try {
          value = await (async () => (%(expr)s))();
        } catch (e) {
          return {ok: false, error: 'expression threw: ' + (e && e.message || String(e))};
        }
        let u8 = null;
        if (value == null) {
          return {ok: false, error: 'expression returned null/undefined — is the WASM hook installed?'};
        }
        if (typeof value === 'string') {
          // Treat as base64.
          try {
            const bin = atob(value);
            u8 = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
          } catch (e) {
            return {ok: false, error: 'string value did not decode as base64: ' + (e && e.message || String(e))};
          }
        } else if (value instanceof Uint8Array) {
          u8 = value;
        } else if (value instanceof ArrayBuffer) {
          u8 = new Uint8Array(value);
        } else if (Array.isArray(value)) {
          // Concatenate Uint8Arrays / ArrayBuffers / numeric arrays.
          let total = 0;
          const parts = [];
          for (const part of value) {
            if (part instanceof Uint8Array) { parts.push(part); total += part.byteLength; }
            else if (part instanceof ArrayBuffer) { const p = new Uint8Array(part); parts.push(p); total += p.byteLength; }
            else if (typeof part === 'number') { parts.push(new Uint8Array([part & 0xff])); total += 1; }
            else { return {ok: false, error: 'array contained unsupported element ' + (typeof part)}; }
          }
          u8 = new Uint8Array(total);
          let off = 0;
          for (const p of parts) { u8.set(p, off); off += p.byteLength; }
        } else if (typeof value === 'object' && value.buffer instanceof ArrayBuffer) {
          // Typed array view.
          u8 = new Uint8Array(value.buffer, value.byteOffset || 0, value.byteLength || value.buffer.byteLength);
        } else {
          return {ok: false, error: 'expression returned unsupported type ' + (typeof value)};
        }
        entry = ww[key] = {u8, total: u8.byteLength};
      }
      const total = entry.total;
      const start = params.offset|0;
      // ``chunk_bytes`` is how many raw bytes we'll base64 in this round.
      // base64 expands 3 -> 4, so to land near chunk_chars chars we use
      // chunk_chars * 3 / 4 bytes.
      const chunkBytes = Math.max(3, Math.floor(params.chunkChars * 3 / 4));
      const end = Math.min(total, start + chunkBytes);
      if (start >= total) {
        return {ok: true, total, eof: true, data_base64: ''};
      }
      const slice = entry.u8.subarray(start, end);
      // btoa on a long Uint8Array can OOM; build in 32 KB pieces.
      let bin = '';
      const STEP = 32768;
      for (let i = 0; i < slice.length; i += STEP) {
        const sub = slice.subarray(i, Math.min(slice.length, i + STEP));
        bin += String.fromCharCode.apply(null, sub);
      }
      const b64 = btoa(bin);
      return {
        ok: true,
        total,
        start,
        end,
        eof: end >= total,
        data_base64: b64,
      };
    }
    """ % {"expr": expression}

    # Each chunk is its own evaluate call so it lands well under
    # CDP and our prompt-side caps.
    async def _run_chunk(page: Any, offset: int) -> dict[str, Any]:
        return await page.evaluate(resolver_js, {
            "offset": offset,
            "chunkChars": chunk,
            "key": "__default",
        })

    h = hashlib.sha256()
    written = 0
    chunks = 0
    total_reported: int | None = None
    fh = None
    try:
        fh = target.open("wb")
        offset = 0
        while True:
            try:
                step = await hub.acall_with_existing_page(_run_chunk, offset, match=match)
            except Exception as e:
                return {"ok": False, "error": f"page.evaluate failed: {e}", "written": written}
            if not isinstance(step, dict) or not step.get("ok"):
                err = (step or {}).get("error") if isinstance(step, dict) else "no response"
                return {"ok": False, "error": err, "written": written, "chunks": chunks}
            if total_reported is None:
                total_reported = int(step.get("total") or 0)
                if total_reported <= 0:
                    return {"ok": False, "error": "expression returned 0-byte buffer", "written": 0}
                if total_reported > max_bytes:
                    return {
                        "ok": False,
                        "error": f"refusing to write {total_reported} bytes > max_total_bytes ({max_bytes})",
                    }
            b64 = step.get("data_base64") or ""
            if b64:
                try:
                    raw = base64.b64decode(b64, validate=False)
                except Exception as e:
                    return {"ok": False, "error": f"chunk decode failed: {e}", "chunk_index": chunks}
                fh.write(raw)
                h.update(raw)
                written += len(raw)
                chunks += 1
                offset = int(step.get("end") or offset + len(raw))
            else:
                offset = int(step.get("end") or offset)
            if step.get("eof"):
                break
            # Belt-and-braces: don't loop forever if the page lies about EOF.
            if chunks > 8192:
                return {
                    "ok": False,
                    "error": "too many chunks; expression may be unstable across calls",
                    "written": written,
                }
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass

    # Sanity check: total reported should match what we wrote.
    if total_reported is not None and written != total_reported:
        return {
            "ok": False,
            "error": (
                f"size mismatch: page reported {total_reported} bytes but "
                f"we wrote {written}. File may be corrupt — re-run."
            ),
            "path": str(target), "written": written,
            "sha256": h.hexdigest(), "chunks": chunks,
        }

    # Cheap magic-number hint so the caller knows if they got a real WASM
    # without paying another tool call.
    magic = ""
    try:
        with target.open("rb") as fhcheck:
            head = fhcheck.read(8)
        if head[:4] == b"\x00asm":
            ver = int.from_bytes(head[4:8], "little") if len(head) >= 8 else 0
            magic = f"valid WASM magic, version {ver}"
        else:
            magic = f"non-WASM magic: {head[:4].hex()}"
    except Exception:
        magic = ""

    return {
        "ok": True,
        "path": str(target),
        "root_kind": root_kind,
        "root": root_path,
        "size_bytes": written,
        "sha256": h.hexdigest(),
        "chunks": chunks,
        "magic": magic,
        "hint": (
            f"Wrote {written} bytes to {target} in {chunks} chunk(s). "
            + (
                "Next: wasm_inspect → wasm_disassemble / wasm_decompile / wasm_call_graph / wasm_strings."
                if "valid WASM" in magic else
                "Magic doesn't match WASM (\\0asm). Check the source expression."
            )
        ),
    }


