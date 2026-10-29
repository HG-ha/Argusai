"""Temporary scoped probes used by the AI assistant."""

from __future__ import annotations

import time
from typing import Any

import httpx

from argusai.core.infra.command import in_scope, normalize_scope


BODY_LIMIT = 8000
HTML_LIMIT = 30000
SCRIPT_LIMIT = 8000


def _headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (headers or {}).items()}


async def http_probe(
    url: str,
    *,
    scope: str | list[str] | None = None,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=False) as client:
            resp = await client.request(method.upper(), url, headers=_headers(headers), content=body.encode() if body else None)
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}

    text = resp.text
    return {
        "ok": True,
        "method": method.upper(),
        "url": str(resp.url),
        "status_code": resp.status_code,
        "elapsed_ms": int((time.time() - started) * 1000),
        "headers": dict(resp.headers),
        "snippet": text[:BODY_LIMIT],
        "truncated": len(text) > BODY_LIMIT,
    }


async def payload_probe(
    url: str,
    payload: str,
    *,
    scope: str | list[str] | None = None,
    parameter: str = "",
) -> dict[str, Any]:
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if parameter:
        params = [(k, payload if k == parameter else v) for k, v in params]
    elif params:
        params[0] = (params[0][0], payload)
    else:
        params = [("q", payload)]
    test_url = urlunparse(parsed._replace(query=urlencode(params)))
    result = await http_probe(test_url, scope=scope)
    result["payload"] = payload
    result["parameter"] = parameter or (params[0][0] if params else "")
    return result


_VALID_WAITS = {"load", "domcontentloaded", "networkidle"}


def _normalize_wait_until(wait_until: str) -> str:
    return wait_until if wait_until in _VALID_WAITS else "networkidle"


async def _attach_console(page) -> list[str]:
    console_msgs: list[str] = []
    page.on("console", lambda m: console_msgs.append(f"{m.type}:{m.text}"[:300]))
    page.on("pageerror", lambda e: console_msgs.append(f"error:{e}"[:300]))
    return console_msgs


async def _goto_with_fallback(page, url: str, wait_until: str) -> Any:
    """Try ``wait_until`` first, fall back to ``domcontentloaded`` so SPAs
    or CTF levels that never reach network-idle still proceed."""
    try:
        return await page.goto(url, wait_until=wait_until, timeout=25000)
    except Exception:
        return await page.goto(url, wait_until="domcontentloaded", timeout=15000)


async def _probe_page(
    page,
    url: str,
    wait_until: str,
    wait_for_selector: str,
    extra_wait_ms: int,
) -> dict[str, Any]:
    started = time.time()
    console_msgs = await _attach_console(page)
    try:
        resp = await _goto_with_fallback(page, url, wait_until)
    except Exception as nav_err:
        return {"ok": False, "url": url, "error": str(nav_err)}
    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, timeout=8000)
        except Exception as e:
            console_msgs.append(f"wait_for_selector_timeout:{e}"[:300])
    if extra_wait_ms and extra_wait_ms > 0:
        await page.wait_for_timeout(min(int(extra_wait_ms), 10000))

    title = await page.title()
    try:
        text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        text = ""
    try:
        html = await page.content()
    except Exception:
        html = ""

    data = await page.evaluate(
        """
        () => {
          const inline_scripts = Array.from(document.scripts)
            .map(s => ({ src: s.src || "", type: s.type || "", text: (s.src ? "" : (s.text || "")).slice(0, 8000) }))
            .filter(s => s.src || s.text);
          const links = Array.from(document.querySelectorAll('a[href]'))
            .slice(0, 80)
            .map(a => ({ text: (a.textContent || "").trim().slice(0, 120), href: a.href }));
          const forms = Array.from(document.querySelectorAll('form'))
            .slice(0, 20)
            .map(f => ({
              action: f.action || "",
              method: (f.method || "get").toLowerCase(),
              inputs: Array.from(f.querySelectorAll('input,select,textarea')).map(i => ({
                name: i.name || "",
                type: i.type || i.tagName.toLowerCase(),
                value: (i.value || "").slice(0, 200),
              })),
            }));
          const meta = Array.from(document.querySelectorAll('meta[name],meta[property]'))
            .slice(0, 30)
            .map(m => ({ name: m.name || m.getAttribute('property') || "", content: (m.content || "").slice(0, 200) }));
          return { inline_scripts, links, forms, meta };
        }
        """
    )

    return {
        "ok": True,
        "url": url,
        "final_url": page.url,
        "status_code": resp.status if resp else None,
        "title": title,
        "elapsed_ms": int((time.time() - started) * 1000),
        "snippet": text[:BODY_LIMIT],
        "truncated": len(text) > BODY_LIMIT,
        "html": html[:HTML_LIMIT],
        "html_truncated": len(html) > HTML_LIMIT,
        "html_length": len(html),
        "inline_scripts": data.get("inline_scripts") or [],
        "links": data.get("links") or [],
        "forms": data.get("forms") or [],
        "meta": data.get("meta") or [],
        "console": console_msgs[:50],
    }


async def _evaluate_page(
    page,
    url: str,
    expression: str,
    wait_until: str,
    wait_for_selector: str,
    extra_wait_ms: int,
    wait_for_navigation: bool = False,
    navigation_timeout_ms: int = 15000,
    post_eval_selector: str = "",
    post_eval_wait_ms: int = 0,
) -> dict[str, Any]:
    import json as _json

    started = time.time()
    console_msgs = await _attach_console(page)
    try:
        await _goto_with_fallback(page, url, wait_until)
    except Exception as nav_err:
        return {"ok": False, "url": url, "error": str(nav_err)}
    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, timeout=15000)
        except Exception as e:
            console_msgs.append(f"wait_for_selector_timeout:{e}"[:300])
    if extra_wait_ms and extra_wait_ms > 0:
        await page.wait_for_timeout(min(int(extra_wait_ms), 60000))

    nav_promise = None
    nav_timeout = max(1000, min(int(navigation_timeout_ms or 15000), 60000))
    if wait_for_navigation:
        try:
            nav_promise = page.wait_for_event(
                "framenavigated",
                timeout=nav_timeout,
            )
        except Exception:
            nav_promise = None

    try:
        value = await page.evaluate(expression)
    except Exception as e:
        return {
            "ok": False,
            "url": url,
            "final_url": page.url,
            "error": f"evaluate failed: {e}",
            "console": console_msgs[:50],
        }

    if nav_promise is not None:
        try:
            await nav_promise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=nav_timeout)
            except Exception:
                pass
        except Exception as e:
            console_msgs.append(f"wait_for_navigation_timeout:{e}"[:300])
    if post_eval_selector:
        try:
            await page.wait_for_selector(post_eval_selector, timeout=nav_timeout)
        except Exception as e:
            console_msgs.append(f"post_eval_selector_timeout:{e}"[:300])
    if post_eval_wait_ms and post_eval_wait_ms > 0:
        await page.wait_for_timeout(min(int(post_eval_wait_ms), 60000))

    try:
        serialised = _json.loads(_json.dumps(value, default=str))
        preview = _json.dumps(serialised, ensure_ascii=False, default=str)[:BODY_LIMIT]
    except Exception:
        serialised = None
        preview = repr(value)[:BODY_LIMIT]

    return {
        "ok": True,
        "url": url,
        "final_url": page.url,
        "elapsed_ms": int((time.time() - started) * 1000),
        "expression": expression[:500],
        "value_type": type(value).__name__,
        "value": serialised if serialised is not None else preview,
        "value_preview": preview,
        "console": console_msgs[:50],
    }


TRACE_INIT_SCRIPT = r"""
(() => {
  const limit = 250;
  const clip = (v, n = 1200) => {
    try {
      if (v === undefined) return "undefined";
      if (typeof v === "string") return v.slice(0, n);
      return JSON.stringify(v).slice(0, n);
    } catch (e) {
      return String(v).slice(0, n);
    }
  };
  const push = (kind, data) => {
    try {
      const bag = window.__argusai_trace = window.__argusai_trace || [];
      if (bag.length < limit) {
        bag.push({ kind, ts: Date.now(), ...data });
      }
    } catch (_) {}
  };

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = async function(input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      push("fetch", {
        url,
        method: (init && init.method) || (input && input.method) || "GET",
        body: clip(init && init.body),
      });
      return origFetch.apply(this, arguments);
    };
  }

  const OrigXHR = window.XMLHttpRequest;
  if (OrigXHR) {
    window.XMLHttpRequest = function() {
      const xhr = new OrigXHR();
      const state = {};
      const open = xhr.open;
      xhr.open = function(method, url) {
        state.method = method;
        state.url = url;
        return open.apply(xhr, arguments);
      };
      const send = xhr.send;
      xhr.send = function(body) {
        push("xhr", { url: state.url || "", method: state.method || "", body: clip(body) });
        return send.apply(xhr, arguments);
      };
      return xhr;
    };
  }

  const OrigWS = window.WebSocket;
  if (OrigWS) {
    window.WebSocket = function(url, protocols) {
      push("websocket", { url: String(url || "") });
      const ws = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      const send = ws.send;
      ws.send = function(data) {
        push("websocket_send", { url: String(url || ""), data: clip(data) });
        return send.apply(ws, arguments);
      };
      return ws;
    };
  }

  const wrapStorage = (obj, name) => {
    if (!obj) return;
    for (const fn of ["getItem", "setItem", "removeItem"]) {
      const orig = obj[fn];
      if (!orig) continue;
      obj[fn] = function(k, v) {
        push("storage", { storage: name, op: fn, key: String(k || ""), value: clip(v, 400) });
        return orig.apply(this, arguments);
      };
    }
  };
  wrapStorage(window.localStorage, "localStorage");
  wrapStorage(window.sessionStorage, "sessionStorage");

  const subtle = window.crypto && window.crypto.subtle;
  if (subtle) {
    for (const fn of ["digest", "sign", "encrypt", "decrypt", "importKey", "deriveKey"]) {
      const orig = subtle[fn];
      if (!orig) continue;
      subtle[fn] = function() {
        push("webcrypto", { op: fn, args: Array.from(arguments).map(a => clip(a, 300)) });
        return orig.apply(this, arguments);
      };
    }
  }

  for (const fn of ["btoa", "atob"]) {
    const orig = window[fn];
    if (!orig) continue;
    window[fn] = function(v) {
      push("codec", { op: fn, input: clip(v, 500) });
      return orig.apply(this, arguments);
    };
  }

  const canvasProto = window.HTMLCanvasElement && window.HTMLCanvasElement.prototype;
  if (canvasProto) {
    for (const fn of ["toDataURL", "toBlob"]) {
      const orig = canvasProto[fn];
      if (!orig) continue;
      canvasProto[fn] = function() {
        push("canvas", { op: fn, width: this.width, height: this.height });
        return orig.apply(this, arguments);
      };
    }
  }
  const ctxProto = window.CanvasRenderingContext2D && window.CanvasRenderingContext2D.prototype;
  if (ctxProto && ctxProto.getImageData) {
    const orig = ctxProto.getImageData;
    ctxProto.getImageData = function(x, y, w, h) {
      push("canvas", { op: "getImageData", width: w, height: h });
      return orig.apply(this, arguments);
    };
  }

  const origAdd = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (["click", "submit", "input", "change", "dragstart", "dragend"].includes(type)) {
      const tag = this && this.tagName ? this.tagName.toLowerCase() : "";
      push("event_listener", { event: type, tag, id: this && this.id || "", className: clip(this && this.className || "", 120) });
    }
    return origAdd.apply(this, arguments);
  };

  const mo = new MutationObserver((items) => {
    push("dom_mutation", { count: items.length });
  });
  try { mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true }); } catch (_) {}
})();
"""


async def _trace_page(
    page,
    url: str,
    wait_until: str,
    wait_for_selector: str,
    extra_wait_ms: int,
    click_selector: str,
    max_events: int = 250,
) -> dict[str, Any]:
    from urllib.parse import urlparse

    started = time.time()
    console_msgs = await _attach_console(page)
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    cap = max(1, int(max_events or 250))
    page.on("request", lambda r: requests.append({
        "method": r.method,
        "url": r.url,
        "resource_type": r.resource_type,
        "post_data": (r.post_data or "")[:1200],
    }) if len(requests) < cap else None)
    page.on("response", lambda r: responses.append({
        "url": r.url,
        "status": r.status,
        "resource_type": r.request.resource_type,
    }) if len(responses) < cap else None)
    await page.add_init_script(TRACE_INIT_SCRIPT)
    try:
        resp = await _goto_with_fallback(page, url, wait_until)
    except Exception as nav_err:
        return {"ok": False, "url": url, "error": str(nav_err)}
    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, timeout=8000)
        except Exception as e:
            console_msgs.append(f"wait_for_selector_timeout:{e}"[:300])
    if click_selector:
        try:
            await page.click(click_selector, timeout=8000)
        except Exception as e:
            console_msgs.append(f"click_failed:{e}"[:300])
    if extra_wait_ms and extra_wait_ms > 0:
        await page.wait_for_timeout(min(int(extra_wait_ms), 10000))

    trace = await page.evaluate("() => window.__argusai_trace || []")
    state = await page.evaluate(
        """
        () => ({
          title: document.title,
          url: location.href,
          globals: Object.keys(window).filter(k => /^(__|webpack|vue|react|nuxt|next|captcha|gt|grecaptcha|hcaptcha|turnstile)/i.test(k)).slice(0, 80),
          localStorage: Object.fromEntries(Object.entries(localStorage || {}).slice(0, 50).map(([k,v]) => [k, String(v).slice(0, 500)])),
          sessionStorage: Object.fromEntries(Object.entries(sessionStorage || {}).slice(0, 50).map(([k,v]) => [k, String(v).slice(0, 500)])),
          cookies: document.cookie.slice(0, 2000),
          captchaNodes: Array.from(document.querySelectorAll('[class*=captcha],[id*=captcha],iframe[src*=captcha],iframe[src*=recaptcha],iframe[src*=turnstile],iframe[src*=hcaptcha]')).slice(0, 30).map(el => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || "",
            className: String(el.className || "").slice(0, 200),
            src: el.src || "",
            sitekey: el.getAttribute('data-sitekey') || "",
          })),
        })
        """
    )
    host = urlparse(url).netloc
    third_party = sorted({urlparse(r["url"]).netloc for r in requests if urlparse(r["url"]).netloc and urlparse(r["url"]).netloc != host})[:50]
    return {
        "ok": True,
        "url": url,
        "final_url": page.url,
        "status_code": resp.status if resp else None,
        "elapsed_ms": int((time.time() - started) * 1000),
        "requests": requests,
        "responses": responses,
        "trace": trace[:cap],
        "trace_total": len(trace),
        "trace_truncated": len(trace) > cap,
        "request_truncated": len(requests) >= cap,
        "response_truncated": len(responses) >= cap,
        "state": state,
        "third_party_hosts": third_party,
        "console": console_msgs[:80],
    }


async def browser_probe(
    url: str,
    *,
    scope: str | list[str] | None = None,
    headless: bool = True,
    wait_until: str = "networkidle",
    wait_for_selector: str = "",
    extra_wait_ms: int = 0,
    fresh_context: bool = False,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    """Load a URL in the managed browser and return the rendered DOM.

    Unlike ``fetch_page_source`` (raw HTML over HTTP), this executes the
    page's JavaScript so SPAs, dynamic injections and CTF challenges that
    only materialise their target into the DOM at runtime can be
    inspected. Returns rendered HTML, inline ``<script>`` contents,
    links, forms and any console errors observed during load.

    Reuses the shared :class:`~argusai.core.browser.browser_pool.BrowserHub`
    Chromium process so chained agent calls avoid Playwright's startup
    cost. Pass ``fresh_context=True`` to drop cookies/storage from
    earlier calls before this one. Pass ``profile`` to spoof the
    fingerprint (UA, locale, timezone, viewport, navigator overrides).
    """
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "url": url, "error": f"browser hub unavailable: {e}"}

    wait_until = _normalize_wait_until(wait_until)
    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    try:
        result = await hub.acall_with_page(
            _probe_page,
            url,
            wait_until,
            wait_for_selector,
            int(extra_wait_ms or 0),
            fresh_context=bool(fresh_context),
        )
        if isinstance(result, dict):
            status = hub.status()
            result.setdefault("fingerprint", status.get("profile", {}))
            result.setdefault("profile_name", status.get("profile_name", ""))
            result.setdefault("browser_source", status.get("resolved_source", ""))
            result.setdefault("browser_label", status.get("resolved_label", ""))
        return result
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


async def browser_trace(
    url: str,
    *,
    scope: str | list[str] | None = None,
    headless: bool = True,
    wait_until: str = "networkidle",
    wait_for_selector: str = "",
    extra_wait_ms: int = 0,
    click_selector: str = "",
    fresh_context: bool = False,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
    max_events: int = 250,
) -> dict[str, Any]:
    """Render a scoped URL and collect runtime reverse-engineering traces."""
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "url": url, "error": f"browser hub unavailable: {e}"}
    wait_until = _normalize_wait_until(wait_until)
    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    try:
        result = await hub.acall_with_page(
            _trace_page,
            url,
            wait_until,
            wait_for_selector,
            int(extra_wait_ms or 0),
            click_selector,
            int(max_events or 250),
            fresh_context=bool(fresh_context),
        )
        if isinstance(result, dict):
            status = hub.status()
            result.setdefault("fingerprint", status.get("profile", {}))
            result.setdefault("profile_name", status.get("profile_name", ""))
            result.setdefault("browser_source", status.get("resolved_source", ""))
            result.setdefault("browser_label", status.get("resolved_label", ""))
        return result
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


async def browser_evaluate(
    url: str,
    expression: str,
    *,
    scope: str | list[str] | None = None,
    headless: bool = True,
    wait_until: str = "networkidle",
    wait_for_selector: str = "",
    extra_wait_ms: int = 0,
    fresh_context: bool = False,
    wait_for_navigation: bool = False,
    navigation_timeout_ms: int = 15000,
    post_eval_selector: str = "",
    post_eval_wait_ms: int = 0,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    """Load ``url`` in the managed browser and evaluate a JavaScript
    expression in the page context once it has rendered. Returns the
    JSON-serialised result plus console output.

    The expression is evaluated with Playwright's ``page.evaluate``: it
    must be either a JS expression that returns a value, or an arrow
    function such as ``() => document.title``. Anything not serialisable
    is coerced to a string preview.

    Set ``wait_for_navigation=True`` when the expression triggers a
    navigation (form.submit, location.href=..., link.click()) so the
    function waits for the resulting page to load before returning.
    ``post_eval_selector`` and ``post_eval_wait_ms`` let you wait for
    a specific element or pause for slow client-side rendering after
    the expression runs. ``profile`` selects a fingerprint preset.
    """
    if not expression or not expression.strip():
        return {"ok": False, "url": url, "error": "expression is required"}
    if not in_scope(url, normalize_scope(scope)):
        return {"ok": False, "error": f"target out of authorized scope: {url}", "url": url}
    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "url": url, "error": f"browser hub unavailable: {e}"}

    wait_until = _normalize_wait_until(wait_until)
    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    try:
        result = await hub.acall_with_page(
            _evaluate_page,
            url,
            expression,
            wait_until,
            wait_for_selector,
            int(extra_wait_ms or 0),
            bool(wait_for_navigation),
            int(navigation_timeout_ms or 15000),
            post_eval_selector or "",
            int(post_eval_wait_ms or 0),
            fresh_context=bool(fresh_context),
        )
        if isinstance(result, dict):
            status = hub.status()
            result.setdefault("fingerprint", status.get("profile", {}))
            result.setdefault("profile_name", status.get("profile_name", ""))
            result.setdefault("browser_source", status.get("resolved_source", ""))
            result.setdefault("browser_label", status.get("resolved_label", ""))
        return result
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


# ---------------------------------------------------------------------------
# Hook-related probes — drive ``HookManager`` against the live browser hub.
# ---------------------------------------------------------------------------

_HOOK_MANAGER_HOLDER: list[Any] = []


def _get_hook_manager(store: Any) -> Any:
    """Cached HookManager bound to the given ReverseJobStore."""
    from argusai.core.reverse.hook_manager import HookManager

    for hm in _HOOK_MANAGER_HOLDER:
        if getattr(hm, "store", None) is store:
            return hm
    hm = HookManager(store)
    _HOOK_MANAGER_HOLDER.append(hm)
    return hm


async def browser_hook_install(
    *,
    store: Any,
    job_id: str,
    target: str,
    kind: str = "function",
    replace_return_src: str = "",
    replace_get_src: str = "",
    filter_src: str = "",
    tag: str = "",
    recipe: str = "",
    capture_stack: bool = True,
    note: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    """Install a hook for a ReverseJob into the live browser context."""
    if not job_id or not target:
        return {"ok": False, "error": "job_id and target are required"}
    from argusai.core.browser.browser_pool import get_browser_hub

    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    hm = _get_hook_manager(store)
    return await hm.install_hook(
        hub,
        job_id,
        target=target,
        kind=kind,
        replace_return_src=replace_return_src,
        replace_get_src=replace_get_src,
        filter_src=filter_src,
        tag=tag,
        recipe=recipe,
        capture_stack=capture_stack,
        note=note,
    )


async def browser_hook_remove(
    *,
    store: Any,
    job_id: str,
    hook_id: str,
    headless: bool = True,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    if not job_id or not hook_id:
        return {"ok": False, "error": "job_id and hook_id are required"}
    from argusai.core.browser.browser_pool import get_browser_hub

    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    hm = _get_hook_manager(store)
    return await hm.remove_hook(hub, job_id, hook_id)


async def browser_hook_list(
    *,
    store: Any,
    job_id: str,
    headless: bool = True,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id is required"}
    try:
        from argusai.core.browser.browser_pool import get_browser_hub

        hub = get_browser_hub(
            headless=headless,
            engine=engine or "chromium",
            profile=profile,
            channel=channel or "",
            executable_path=executable_path or "",
        )
    except Exception:
        hub = None
    hm = _get_hook_manager(store)
    return await hm.list_hooks(hub, job_id)


async def browser_hook_drain(
    *,
    store: Any,
    job_id: str,
    hook_id: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    if not job_id:
        return {"ok": False, "error": "job_id is required"}
    try:
        from argusai.core.browser.browser_pool import get_browser_hub

        hub = get_browser_hub(
            headless=headless,
            engine=engine or "chromium",
            profile=profile,
            channel=channel or "",
            executable_path=executable_path or "",
        )
    except Exception as e:
        return {"ok": False, "error": f"browser hub unavailable: {e}"}
    hm = _get_hook_manager(store)
    return await hm.drain(hub, job_id, hook_id=hook_id)


async def browser_hook_reattach(
    *,
    store: Any,
    job_id: str,
    headless: bool = True,
    engine: str = "chromium",
    profile: str | dict[str, Any] | None = None,
    channel: str = "",
    executable_path: str = "",
) -> dict[str, Any]:
    """Re-inject every hook for a job. Useful after attaching to a fresh browser."""
    if not job_id:
        return {"ok": False, "error": "job_id is required"}
    from argusai.core.browser.browser_pool import get_browser_hub

    hub = get_browser_hub(
        headless=headless,
        engine=engine or "chromium",
        profile=profile,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    hm = _get_hook_manager(store)
    return await hm.reattach_all(hub, job_id)
