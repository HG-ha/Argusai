"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *

async def http_probe(api: Any, url: str = "", scope: str = "", method: str = "GET", body: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run a temporary scoped HTTP probe."""
    from argusai.core.security.probes import http_probe as _http_probe

    return await _http_probe(url, scope=scope, method=method, body=body)


async def payload_probe(api: Any, url: str = "", payload: str = "", scope: str = "", parameter: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run a temporary scoped payload probe against one URL parameter."""
    from argusai.core.security.probes import payload_probe as _payload_probe

    return await _payload_probe(url, payload, scope=scope, parameter=parameter)


async def browser_probe(
    api: Any,
    url: str = "",
    scope: str = "",
    headless: bool = True,
    wait_until: str = "networkidle",
    wait_for_selector: str = "",
    extra_wait_ms: int = 0,
    fresh_context: bool = False,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Open a scoped URL in the managed shared browser and return the rendered DOM, inline scripts, links and forms after JS executes. Reuses one browser per (engine, profile, binary) across calls. Pass profile=desktop_chrome_win|desktop_chrome_zh|mobile_iphone_safari|... to spoof UA/locale/timezone (see browser_profile_list). Pass channel=chrome|msedge|chrome-beta|msedge-beta|... to drive a specific system browser; pass executable_path=C:\\\\Program Files\\\\... to drive an arbitrary Chromium-family binary; both empty = fall back to the default configured in Settings, then auto-detect system Chrome/Edge, then Playwright bundle."""
    from argusai.core.security.probes import browser_probe as _browser_probe

    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await _browser_probe(
        url,
        scope=scope,
        headless=headless,
        wait_until=wait_until,
        wait_for_selector=wait_for_selector,
        extra_wait_ms=_safe_int(extra_wait_ms, 0, 0, 10000),
        fresh_context=fresh_context,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_evaluate(
    api: Any,
    url: str = "",
    expression: str = "",
    scope: str = "",
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
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    max_value_chars: int = 8192,
    **kwargs: Any,
) -> dict[str, Any]:
    """Load a scoped URL in the managed shared browser and evaluate a JavaScript expression in the page context once rendered, returning the result. Use wait_for_navigation/post_eval_selector/post_eval_wait_ms when the expression triggers a page change. Pass profile=... to spoof the fingerprint, channel=chrome|msedge|... or executable_path=... to pick a specific browser binary.

    The evaluated ``value`` is summarised down to ``max_value_chars``
    (default 8 KB) in the response — full objects with megabytes of
    nested state would otherwise flood the LLM context. If you need to
    inspect a big result, write a tighter expression (e.g. extract one
    field, or compute a hash + length) rather than dumping the whole
    object.
    """
    import json
    from argusai.core.security.probes import browser_evaluate as _browser_evaluate

    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    cap = _safe_int(max_value_chars, 8192, 256, 50_000)
    out = await _browser_evaluate(
        url,
        expression,
        scope=scope,
        headless=headless,
        wait_until=wait_until,
        wait_for_selector=wait_for_selector,
        extra_wait_ms=_safe_int(extra_wait_ms, 0, 0, 60000),
        fresh_context=fresh_context,
        wait_for_navigation=bool(wait_for_navigation),
        navigation_timeout_ms=_safe_int(navigation_timeout_ms, 15000, 1000, 60000),
        post_eval_selector=post_eval_selector or "",
        post_eval_wait_ms=_safe_int(post_eval_wait_ms, 0, 0, 60000),
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )
    if isinstance(out, dict) and "value" in out:
        v = out.get("value")
        if not isinstance(v, (str, int, float, bool)) and v is not None:
            try:
                serialised = json.dumps(v, ensure_ascii=False, default=str)
            except Exception:
                serialised = repr(v)
            out["value_size_chars"] = len(serialised)
            if len(serialised) > cap:
                out["value"] = serialised[:cap] + f"...[truncated {len(serialised) - cap} chars; pass max_value_chars or narrow the expression]"
                out["value_truncated"] = True
            else:
                out["value"] = serialised
                out["value_truncated"] = False
        elif isinstance(v, str) and len(v) > cap:
            out["value_size_chars"] = len(v)
            out["value"] = v[:cap] + f"...[truncated {len(v) - cap} chars]"
            out["value_truncated"] = True
    return out


def browser_status(api: Any, engine: str = "", **kwargs: Any) -> dict[str, Any]:
    """Report the status of the managed shared headless browser. Pass engine=chromium|firefox|webkit to inspect a specific engine; omit it to list all active hubs (one per engine + fingerprint profile + binary combination)."""
    from argusai.core.browser.browser_pool import get_browser_hub, list_browser_hubs

    if engine:
        return {"ok": True, **get_browser_hub(engine=engine).status()}
    hubs = list_browser_hubs()
    return {"ok": True, "hubs": hubs, "engines_available": ["chromium", "firefox", "webkit"]}


async def browser_reset(api: Any, engine: str = "chromium", profile: str = "", **kwargs: Any) -> dict[str, Any]:
    """Drop the shared browser context (cookies, storage, cache) for a given engine + profile so the next browser_probe / browser_evaluate starts clean. Keeps the browser process warm."""
    from argusai.core.browser.browser_pool import get_browser_hub

    hub = get_browser_hub(engine=engine or "chromium", profile=profile or None)
    return {"ok": True, **(await hub.areset())}


# ---------------------------------------------------------------------------
# Browser hook tools — drive ``HookManager`` for a ReverseJob.
# Persistent across navigation (CDP addScriptToEvaluateOnNewDocument via
# Playwright add_init_script). Hits are stored in the job's hooks/<id>.hits.jsonl.
# ---------------------------------------------------------------------------


def _resolve_reverse_store(api: Any) -> Any:
    """Locate the active ReverseJobStore from the gui Api (or the assistant
    session — both keep a ``reverse_jobs`` attribute)."""
    store = getattr(api, "reverse_jobs", None)
    if store is not None:
        return store
    # Some assistant sessions proxy through .api
    inner = getattr(api, "api", None)
    if inner is not None:
        return getattr(inner, "reverse_jobs", None)
    return None


async def browser_hook_function(
    api: Any,
    job_id: str = "",
    target: str = "",
    replace_return_src: str = "",
    filter_src: str = "",
    tag: str = "",
    recipe: str = "",
    capture_stack: bool = True,
    note: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Install a function hook against a live browser tab. ``target`` is a dotted path on the page (e.g. ``window.foo``, ``JSON.stringify``, ``XMLHttpRequest.prototype.setRequestHeader``). Hits (args, return, stack, duration) are buffered in the page and pulled into the ReverseJob via ``browser_hook_drain``. Set ``replace_return_src`` to a JS function body returning the value you want to see (use ``args``, ``thisArg``, ``origReturn`` parameters) — useful for spoofing canvas/audio fingerprints. Set ``filter_src`` to a JS function body returning truthy to record. Persistent across reload (re-injected on every navigation)."""
    from argusai.core.security.probes import browser_hook_install

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await browser_hook_install(
        store=store,
        job_id=job_id,
        target=target,
        kind="function",
        replace_return_src=replace_return_src or "",
        filter_src=filter_src or "",
        tag=tag or "",
        recipe=recipe or "",
        capture_stack=bool(capture_stack),
        note=note or "",
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_hook_property(
    api: Any,
    job_id: str = "",
    target: str = "",
    replace_get_src: str = "",
    tag: str = "",
    recipe: str = "",
    capture_stack: bool = True,
    note: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Install a property getter/setter hook. ``target`` is a dotted path (e.g. ``navigator.webdriver``, ``document.cookie``, ``location.host``). Every get and every set is recorded. Set ``replace_get_src`` to a JS function body that takes ``orig`` and returns the replacement (e.g. ``return false;`` to spoof ``navigator.webdriver``). Persistent across reload."""
    from argusai.core.security.probes import browser_hook_install

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await browser_hook_install(
        store=store,
        job_id=job_id,
        target=target,
        kind="property",
        replace_get_src=replace_get_src or "",
        tag=tag or "",
        recipe=recipe or "",
        capture_stack=bool(capture_stack),
        note=note or "",
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_hook_list(
    api: Any,
    job_id: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """List active hooks for a ReverseJob with both persisted definitions and live in-page status (hit counts, dropped counters). Use this to see what's currently installed before adding more."""
    from argusai.core.security.probes import browser_hook_list as _list

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await _list(
        store=store,
        job_id=job_id,
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_hook_remove(
    api: Any,
    job_id: str = "",
    hook_id: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Uninstall a hook by id. Restores the original function/property on every open page and removes the persisted definition (so it won't re-install on next page load)."""
    from argusai.core.security.probes import browser_hook_remove as _remove

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await _remove(
        store=store,
        job_id=job_id,
        hook_id=hook_id,
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_hook_drain(
    api: Any,
    job_id: str = "",
    hook_id: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Pull buffered hits from the live browser into the ReverseJob's hits log. Pass ``hook_id`` to drain just one hook; omit to drain all. Hits are appended to ``hooks/<id>.hits.jsonl`` under the job folder and a ``dropped`` counter is reported when the in-page buffer was full."""
    from argusai.core.security.probes import browser_hook_drain as _drain

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await _drain(
        store=store,
        job_id=job_id,
        hook_id=hook_id or "",
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


async def browser_hook_reattach(
    api: Any,
    job_id: str = "",
    headless: bool = True,
    engine: str = "chromium",
    profile: str = "",
    channel: str = "",
    executable_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Re-inject every hook for a job. Use after attaching to a fresh browser process, or if the user manually clears the page."""
    from argusai.core.security.probes import browser_hook_reattach as _reattach

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not channel and not executable_path:
        channel, executable_path = _browser_defaults(api)
    return await _reattach(
        store=store,
        job_id=job_id,
        headless=headless,
        engine=engine or "chromium",
        profile=profile or None,
        channel=channel or "",
        executable_path=executable_path or "",
    )


# ---------------------------------------------------------------------------
# Reverse-job orchestration tools — full CRUD over the on-disk ReverseJob.
# These mirror gui.Api.reverse_* but live here so the assistant can drive
# the entire reverse-engineering workflow end-to-end without UI clicks.
# ---------------------------------------------------------------------------


def reverse_job_list(
    api: Any,
    scope: str = "",
    status: str = "",
    include_trashed: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """List existing reverse-engineering jobs (most-recently-updated first). Each summary includes ``id``, ``title``, ``target_url``, ``scope`` (usually eTLD+1), ``status`` (active/paused/done/abandoned), tag list, and counts (artifacts/hooks/findings). Use this BEFORE creating a new job to avoid duplicates for the same target. Filter by ``scope`` (substring match) or ``status``."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jobs = store.list(
        scope=str(scope or ""),
        status=str(status or ""),
        include_trashed=bool(include_trashed),
    )
    return {"ok": True, "count": len(jobs), "jobs": jobs}


def reverse_job_create(
    api: Any,
    title: str = "",
    target_url: str = "",
    scope: str = "",
    tags: list[str] | None = None,
    intent: str = "",
    framework: str = "",
    bundler: str = "",
    obfuscation: str = "",
    antibot: list[str] | None = None,
    linked_task_id: str = "",
    target_type: str = "",
    platform: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a new reverse-engineering job. ``title`` is required (short, concrete — e.g. ``site.com signed request analysis``). ``target_url`` is the entry URL you'll probe. ``scope`` is the eTLD+1 (e.g. ``example.com``) used to group jobs. ``intent`` is the one-paragraph goal (``recover the X-Sign algorithm``). ``framework`` / ``bundler`` / ``obfuscation`` / ``antibot`` populate ``stack`` for later analysis. ``linked_task_id`` ties this back to a scan/task if you started from one. ``target_type`` / ``platform`` classify mobile/native jobs (``android`` / ``ios`` / ``windows`` / ``web_js``). Returns the created job record (with ``id`` you'll pass to every later reverse_* tool)."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(title or "").strip():
        return {"ok": False, "error": "title is required"}
    stack: dict[str, Any] = {}
    if framework:
        stack["framework"] = str(framework)
    if bundler:
        stack["bundler"] = str(bundler)
    if obfuscation:
        stack["obfuscation"] = str(obfuscation)
    if antibot:
        stack["antibot"] = list(antibot)
    try:
        job = store.create(
            title=str(title),
            target_url=str(target_url or ""),
            scope=str(scope or ""),
            tags=list(tags or []),
            stack=stack or None,
            intent=str(intent or ""),
            linked_task_id=str(linked_task_id or ""),
            target_type=str(target_type or ""),
            platform=str(platform or ""),
        )
        return {"ok": True, "job": job}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def reverse_job_get(
    api: Any,
    job_id: str = "",
    include_trashed: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read the full state of a job: title, scope, status, intent, stack, tags, the indices of every artifact / hook / finding / reproduce script, and timestamps. Use this whenever you re-enter a job (e.g. across sessions) to remember what you've already done. To read the body of any specific artifact / finding / reproduce script, call the corresponding ``reverse_artifact_read`` / ``reverse_finding_read`` / ``reverse_reproduce_read``."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    job = store.get(str(job_id or ""), include_trashed=bool(include_trashed))
    if job is None:
        return {"ok": False, "error": f"job not found: {job_id}"}
    return {"ok": True, "job": job}


def reverse_job_update(
    api: Any,
    job_id: str = "",
    title: str | None = None,
    scope: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    intent: str | None = None,
    framework: str | None = None,
    bundler: str | None = None,
    obfuscation: str | None = None,
    antibot: list[str] | None = None,
    linked_task_id: str | None = None,
    linked_kb_ids: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Mutate fields on an existing job. Only pass the fields you want to change. ``status`` accepts ``active`` / ``paused`` / ``done`` / ``abandoned``. Use ``done`` once you've extracted the findings and saved any reproducers; use ``abandoned`` if the target turned out to be out of scope or the user moved on. ``linked_kb_ids`` lets you tie this job back to KB entries you saved while working on it."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    stack_patch: dict[str, Any] | None = None
    if framework is not None or bundler is not None or obfuscation is not None or antibot is not None:
        stack_patch = {}
        if framework is not None:
            stack_patch["framework"] = framework
        if bundler is not None:
            stack_patch["bundler"] = bundler
        if obfuscation is not None:
            stack_patch["obfuscation"] = obfuscation
        if antibot is not None:
            stack_patch["antibot"] = list(antibot or [])
    try:
        job = store.update(
            str(job_id),
            title=title,
            scope=scope,
            status=status,
            tags=tags,
            stack=stack_patch,
            intent=intent,
            linked_task_id=linked_task_id,
            linked_kb_ids=linked_kb_ids,
        )
        return {"ok": True, "job": job}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def reverse_job_trash(
    api: Any,
    job_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Soft-delete a job — moves it to ``_trash/`` so the user can restore from the UI. Use this if you accidentally created a duplicate or the user confirms they want to discard the work."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    ok = store.trash(str(job_id))
    return {"ok": bool(ok), "trashed": bool(ok)}


def reverse_job_events(
    api: Any,
    job_id: str = "",
    tail: int = 100,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read the recent event log for a job (created / updated / artifact_added / hook_installed / hook_hit_drained / finding_added etc.). Useful to remember what you did last time you opened this job in a previous session."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        events = store.list_events(str(job_id or ""), tail=_safe_int(tail, 100, 1, 2000))
        return {"ok": True, "count": len(events), "events": events}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_artifact_add(
    api: Any,
    job_id: str = "",
    content: str = "",
    kind: str = "js",
    filename: str = "",
    url: str = "",
    from_artifact: str = "",
    tool: str = "",
    note: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach a piece of source / data / output to a job. ``content`` is the raw text (utf-8). ``kind`` controls how the artifact is stored — common values: ``entry`` (main page JS), ``chunk`` (split bundle), ``inline`` (inline script), ``worker``, ``wasm``, ``sourcemap``, ``deob`` (deobfuscated output), ``webpack_module``, ``trace`` (jsonl trace from js_vm_trace), ``recovered`` (recovered source). Provide ``url`` if the artifact was fetched from the web; ``from_artifact`` if it's a derivative (set to the parent artifact id); ``tool`` if it was produced by another reverse_* tool (``js_deobfuscate``, ``source_map_recover`` etc.); ``note`` for free-form text. Returns the artifact record (with new ``id`` you'll reference later)."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    if not isinstance(content, str) or content == "":
        return {"ok": False, "error": "content is required (utf-8 string)"}
    try:
        rec = store.add_artifact(
            str(job_id),
            content=content,
            kind=str(kind or "js"),
            filename=str(filename or ""),
            url=str(url or ""),
            from_artifact=str(from_artifact or ""),
            tool=str(tool or ""),
            note=str(note or ""),
        )
        return {"ok": True, "artifact": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": str(e)}


def reverse_artifact_read(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    max_bytes: int = 32_768,
    offset_bytes: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a SLICE of an artifact.

    Defaults to the first 32 KB. Use ``offset_bytes`` to page through;
    the response includes ``end_bytes`` and ``more`` so you know where
    to continue. For line-oriented reads or grep, you can also get the
    artifact's absolute disk path from ``path_abs`` and then call
    ``grep_text`` / ``read_text_file`` for a much smaller targeted
    response.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        rec = store.read_artifact(
            str(job_id or ""),
            str(artifact_id or ""),
            max_bytes=_safe_int(max_bytes, 32_768, 256, 1_000_000),
            offset_bytes=_safe_int(offset_bytes, 0, 0, 1_000_000_000),
        )
        if rec is None:
            return {"ok": False, "error": "artifact not found"}
        return {"ok": True, "artifact": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_artifact_remove(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Detach an artifact from a job (deletes the underlying file). Irreversible — use only after the user confirms they want it gone."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        ok = store.remove_artifact(str(job_id or ""), str(artifact_id or ""))
        return {"ok": bool(ok), "removed": bool(ok)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_finding_add(
    api: Any,
    job_id: str = "",
    title: str = "",
    body: str = "",
    kind: str = "note",
    confidence: str = "probable",
    tags: list[str] | None = None,
    coverage_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Record a meaningful conclusion against a job. ``title`` is a one-line summary (``site signs requests with HMAC-SHA256(secret, ts||nonce||url||body)``). ``body`` is the full markdown explanation — include payload examples, source snippets, and reproducer notes. ``kind`` describes what kind of finding: ``algorithm`` (a crypto / signing recovery), ``flow`` (request/response flow), ``vuln`` (security weakness), ``fingerprint`` (anti-bot detection vector), ``note`` (general observation). ``confidence`` is ``unverified`` / ``probable`` / ``verified``. ``coverage_id`` is the row from §3.5 coverage matrix in JS-REVERSE-PLAN (e.g. ``C9``, ``B3``) so we can measure capability coverage. Findings are what the user comes back to read; raw artifacts are not — always promote your conclusions into findings."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(title or "").strip():
        return {"ok": False, "error": "title is required"}
    try:
        rec = store.add_finding(
            str(job_id or ""),
            title=str(title),
            body=str(body or ""),
            kind=str(kind or "note"),
            confidence=str(confidence or "probable"),
            tags=list(tags or []),
            coverage_id=str(coverage_id or ""),
        )
        return {"ok": True, "finding": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_finding_read(
    api: Any,
    job_id: str = "",
    finding_id: str = "",
    max_chars: int = 32_768,
    offset: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a SLICE of a finding's markdown body.

    Default returns the first 32 KB; use ``offset`` + ``max_chars`` to
    page. The response carries ``body_offset / body_total_chars /
    body_more`` so you know whether to continue.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        rec = store.read_finding(str(job_id or ""), str(finding_id or ""))
        if rec is None:
            return {"ok": False, "error": "finding not found"}
        rec = dict(rec)
        _truncate_text_field(
            rec,
            "body",
            max_chars=_safe_int(max_chars, 32_768, 256, 200_000),
            offset=_safe_int(offset, 0, 0, 100_000_000),
        )
        return {"ok": True, "finding": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_finding_remove(
    api: Any,
    job_id: str = "",
    finding_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Retract a finding (deletes its markdown file). Use sparingly — prefer ``reverse_finding_add`` with a corrective body, since findings carry the historical record."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        ok = store.remove_finding(str(job_id or ""), str(finding_id or ""))
        return {"ok": bool(ok), "removed": bool(ok)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_hook_hits(
    api: Any,
    job_id: str = "",
    hook_id: str = "",
    tail: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read the persisted hits log for one hook (the ones already drained from the page into ``hooks/<id>.hits.jsonl``). Returns the last ``tail`` hits. To pull fresh hits from a live browser, call ``browser_hook_drain`` first; this tool reads what's already on disk."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        hits = store.read_hook_hits(
            str(job_id or ""),
            str(hook_id or ""),
            tail=_safe_int(tail, 200, 1, 5000),
        )
        return {"ok": True, "count": len(hits), "hits": hits}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_oracle_add(
    api: Any,
    job_id: str = "",
    records: Any = None,
    records_json: str = "",
    stage: str = "",
    source: str = "",
    runtime: str = "",
    tags: list[str] | None = None,
    note: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Persist runtime oracle records under a ReverseJob. Use after ``js_runtime_drain`` or browser trace extraction so input/output/key/stage evidence survives outside chat context."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    parsed = records if isinstance(records, list) else []
    if records_json:
        import json
        try:
            raw = json.loads(records_json)
            parsed = raw if isinstance(raw, list) else [raw]
        except Exception as e:
            return {"ok": False, "error": f"invalid records_json: {e}"}
    if not parsed:
        return {"ok": False, "error": "records or records_json is required"}
    try:
        rec = store.add_oracle(
            str(job_id),
            records=[r for r in parsed if isinstance(r, dict)],
            stage=str(stage or ""),
            source=str(source or ""),
            runtime=str(runtime or ""),
            tags=list(tags or []),
            note=str(note or ""),
        )
        return {"ok": True, "oracle": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_oracle_list(
    api: Any,
    job_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """List oracle logs attached to a ReverseJob."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        items = store.list_oracles(str(job_id or ""))
        return {"ok": True, "count": len(items), "oracles": items}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_oracle_read(
    api: Any,
    job_id: str = "",
    oracle_id: str = "",
    stage: str = "",
    kind: str = "",
    tail: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read oracle records from a ReverseJob, optionally filtered by oracle id, stage or kind."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        records = store.read_oracle(
            str(job_id or ""),
            str(oracle_id or ""),
            tail=_safe_int(tail, 200, 1, 5000),
            stage=str(stage or ""),
            kind=str(kind or ""),
        )
        return {"ok": True, "count": len(records), "records": records}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_oracle_compare(
    api: Any,
    job_id: str = "",
    oracle_id: str = "",
    actual_field: str = "actual",
    expected_field: str = "expected",
    tail: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compare two fields across oracle records. This is a lightweight verifier for Python reproducer output written back as ``actual`` against captured JS ``expected``."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        return store.compare_oracle_records(
            str(job_id or ""),
            str(oracle_id or ""),
            actual_field=str(actual_field or "actual"),
            expected_field=str(expected_field or "expected"),
            tail=_safe_int(tail, 200, 1, 5000),
        )
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_reproduce_add(
    api: Any,
    job_id: str = "",
    name: str = "",
    content: str = "",
    lang: str = "python",
    from_finding: str = "",
    verified: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach a reproducer script to a job. Used for "Python reproduces what the site does" deliverables (e.g. a script that re-creates the X-Sign HMAC). ``name`` is a short slug used for the filename (``sign_request``, ``decrypt_response``). ``content`` is the full script source (utf-8). ``lang`` defaults to ``python`` (also accepts ``js``, ``shell``, ``go`` etc.). ``from_finding`` is the id of the finding this reproducer implements. Mark ``verified=true`` only AFTER you ran the script in the sandbox (P3) and confirmed it matches a captured input/output pair."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    if not str(name or "").strip():
        return {"ok": False, "error": "name is required"}
    if not isinstance(content, str) or content == "":
        return {"ok": False, "error": "content is required"}
    try:
        rec = store.add_reproduce(
            str(job_id),
            name=str(name),
            content=str(content),
            lang=str(lang or "python"),
            from_finding=str(from_finding or ""),
            verified=bool(verified),
        )
        return {"ok": True, "reproduce": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_reproduce_read(
    api: Any,
    job_id: str = "",
    rep_id: str = "",
    max_chars: int = 32_768,
    offset: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a SLICE of a reproducer script.

    Default returns the first 32 KB; use ``offset`` + ``max_chars`` to
    page. Look at ``content_offset / content_total_chars / content_more``
    in the response to know where to continue.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        rec = store.read_reproduce(str(job_id or ""), str(rep_id or ""))
        if rec is None:
            return {"ok": False, "error": "reproduce script not found"}
        rec = dict(rec)
        # ``read_reproduce`` returns ``content`` for the script text.
        _truncate_text_field(
            rec,
            "content",
            max_chars=_safe_int(max_chars, 32_768, 256, 200_000),
            offset=_safe_int(offset, 0, 0, 100_000_000),
        )
        return {"ok": True, "reproduce": rec}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def reverse_recipe_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Catalogue of pre-baked hook recipes. Each entry is ``{id, title, description, coverage[], hooks[]}`` where ``coverage`` lists the §3.5 matrix rows this recipe covers (e.g. ``["A2","A3"]`` for canvas fingerprinting). Use ``reverse_recipe_apply`` to install all hooks from a recipe in one call."""
    try:
        from argusai.core.reverse.hook_recipes import list_recipes
        return {"ok": True, "recipes": list_recipes()}
    except Exception as e:
        return {"ok": False, "error": str(e), "recipes": []}


# ---------------------------------------------------------------------------
# P2 — Heavy deobfuscation, multi-format bundle unpacking, indexed sourcemaps.
# All three delegate to ``argusai.core.reverse.js_heavy``, which in turn calls
# managed Node.js + webcrack / restringer via runtime_manager. Missing or
# damaged runtimes are handled by the sandbox-environment confirmation gate.
# ---------------------------------------------------------------------------


def js_deobfuscate_heavy(
    api: Any,
    source: str = "",
    use_restringer: bool = True,
    timeout: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run managed webcrack (and optionally restringer) on a JS bundle."""
    from argusai.core.reverse.js_heavy import deobfuscate_heavy
    src = str(source or "")
    if not src.strip():
        return {"ok": False, "error": "source is required (utf-8 string)"}
    return deobfuscate_heavy(
        src, api=api,
        use_restringer=bool(use_restringer),
        timeout=_safe_int(timeout, 120, 10, 600),
    )


def js_unpack_bundle(
    api: Any,
    source: str = "",
    timeout: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Detect a bundle format and split it into modules, using managed Node where required."""
    from argusai.core.reverse.js_heavy import unpack_bundle
    src = str(source or "")
    if not src.strip():
        return {"ok": False, "error": "source is required (utf-8 string)"}
    return unpack_bundle(
        src, api=api,
        timeout=_safe_int(timeout, 120, 10, 600),
    )


def js_detect_bundler(
    api: Any,
    source: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Quickly fingerprint a JS bundle WITHOUT running the heavy deobfuscator. Returns ``{primary, signals: [{name, hits, kind}], all_matches}``. Pure-Python regex — fast even for 5 MB bundles. Use this to populate ``ReverseJob.stack.bundler`` early in the workflow."""
    from argusai.core.reverse.js_heavy import detect_bundler
    src = str(source or "")
    if not src.strip():
        return {"primary": "unknown", "signals": [], "all_matches": {}}
    fp = detect_bundler(src)
    fp["ok"] = True
    return fp


def source_map_recover_indexed(
    api: Any,
    map_text: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Recover originals from a V3 source map, including indexed/sectioned maps with ``sections`` and ``x_google_ignoreList`` annotations. This handles modern bundler outputs (multiple chunks merged into one map, framework vendor code marked as 'ignored' so devtools skips them). Returns ``{ok, kind ("flat" | "indexed"), version, sources: [{name, content, offset?, ignored, in_section?, has_content, size}], section_count, sections?, x_google_ignoreList, error}``. ``map_text`` is the full ``.map`` file as JSON text. For inline ``data:application/json;base64,...`` source maps, decode them first with ``source_map_decode_data_uri``."""
    from argusai.core.reverse.js_heavy import recover_indexed_source_map
    text = str(map_text or "")
    if not text.strip():
        return {"ok": False, "error": "map_text is required"}
    return recover_indexed_source_map(text)


def source_map_decode_data_uri(
    api: Any,
    uri: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Decode a ``data:application/json;base64,...`` source-map URI (the kind embedded with ``//# sourceMappingURL=data:...`` in production-with-maps builds). Returns ``{ok, map_text, error}`` — pipe ``map_text`` into ``source_map_recover_indexed``."""
    from argusai.core.reverse.js_heavy import decode_data_uri_map
    u = str(uri or "").strip()
    if not u:
        return {"ok": False, "error": "uri is required"}
    try:
        text = decode_data_uri_map(u)
        return {"ok": True, "map_text": text, "size": len(text)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def runtime_status(
    api: Any,
    force_refresh: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Inspect the managed Python / Node / WABT toolchains."""
    from argusai.core.managed_runtimes import all_runtime_status
    from argusai.core.paths import PathManager
    from argusai.core.reverse.runtime_manager import vendor_status

    settings = api.settings_mgr.get()
    tools_dir = PathManager(data_root=settings.data_root).tools_dir
    managed = all_runtime_status(tools_dir)
    s = vendor_status(api, force=bool(force_refresh))
    retained = s.to_dict()
    return {
        "ok": True,
        "overall_ready": bool(
            managed["python"].get("healthy")
            and managed["node"].get("healthy")
            and s.wabt.found
        ),
        "managed_root": managed["root"],
        "python": managed["python"],
        "node": managed["node"],
        "wabt": retained.get("wabt") or {},
        "install_action": {
            "tool": "runtime_install",
            "settings_page": "Sandbox runtimes",
            "missing": [
                name
                for name, ready in (
                    ("python", managed["python"].get("healthy")),
                    ("node", managed["node"].get("healthy")),
                    ("wabt", s.wabt.found),
                )
                if not ready
            ],
            "reason": "Approve the install card on first use, or install/repair it in Settings.",
        },
    }


def _effective_js_runtime_name(runtime_name: str, kwargs: dict[str, Any]) -> str:
    explicit = str(runtime_name or "").strip()
    if explicit and explicit != "default":
        return explicit
    return str(kwargs.get("_session_id") or explicit or "default")


def js_runtime_status(
    api: Any,
    runtime_name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Inspect warm hosted JS runtimes and their sessions. Use this like browser_status, but for the Node-hosted reverse-engineering runtime that can keep large bundles, hooks and oracle records alive across calls."""
    from argusai.core.reverse.js_runtime import runtime_status as _status

    return _status(api=api, name=(str(runtime_name or "") if not runtime_name else _effective_js_runtime_name(runtime_name, kwargs)))


def js_runtime_stop(
    api: Any,
    runtime_name: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """Stop a hosted JS runtime process. This drops every JS session, loaded bundle, hook and in-memory oracle record in that runtime."""
    from argusai.core.reverse.js_runtime import runtime_stop

    return runtime_stop(_effective_js_runtime_name(runtime_name, kwargs))


def js_runtime_create(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    env: str = "browser",
    page_url: str = "",
    user_agent: str = "",
    globals: Any = None,
    profile: Any = None,
    profile_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Create or reuse a JS runtime session. ``env='browser'`` gives a browser-like global object (window/document/navigator/crypto/fetch/canvas stubs); ``env='node'`` gives a Node-like context with Buffer and a process.exit interceptor. Sessions persist until ``js_runtime_close`` or ``js_runtime_stop``."""
    from argusai.core.reverse.js_runtime import create_session
    parsed_profile = dict(profile) if isinstance(profile, dict) else {}
    if profile_json:
        import json
        try:
            raw = json.loads(profile_json)
            parsed_profile = raw if isinstance(raw, dict) else {}
        except Exception:
            parsed_profile = {}

    return create_session(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        env=str(env or "browser"),
        page_url=str(page_url or ""),
        user_agent=str(user_agent or ""),
        globals_=dict(globals) if isinstance(globals, dict) else {},
        profile=parsed_profile,
    )


def js_runtime_close(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """Close one hosted JS session while keeping the warm runtime process alive."""
    from argusai.core.reverse.js_runtime import close_session

    return close_session(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
    )


def js_runtime_load(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    source: str = "",
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    filename: str = "",
    timeout_ms: int = 30000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Load a large JS source file or inline source into a hosted session. This is the replacement for trying to push 700 KB+ bundles through ``sandbox_eval_js``: the code is evaluated once and its globals remain available for later ``js_runtime_call`` / ``js_runtime_hook`` / ``js_runtime_eval`` calls."""
    from argusai.core.reverse.js_runtime import load_code

    resolved_path = ""
    if path:
        try:
            resolved_path = str(_resolve_session_input_path(api, kwargs, path))
        except Exception:
            resolved_path = str(path or "")
    if artifact_id and not source and not resolved_path:
        store = _resolve_reverse_store(api)
        if store is None:
            return {"ok": False, "error": "reverse-job store unavailable"}
        try:
            art = store.read_artifact(str(job_id or ""), str(artifact_id or ""), max_bytes=50 * 1024 * 1024)
            if not art:
                return {"ok": False, "error": f"artifact not found: {artifact_id}"}
            resolved_path = str(art.get("path_abs") or "")
            filename = filename or str(art.get("filename") or artifact_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return load_code(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        source=str(source or ""),
        path=resolved_path,
        filename=str(filename or ""),
        timeout_ms=_safe_int(timeout_ms, 30000, 100, 120000),
    )


def js_runtime_eval(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    expression: str = "",
    inputs: Any = None,
    inputs_json: str = "",
    timeout_ms: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate an expression inside an existing hosted JS session. If the expression evaluates to a function, it is called with ``inputs``. Use this for surgical probes after loading or patching a bundle."""
    from argusai.core.reverse.js_runtime import eval_expression

    parsed_inputs = inputs
    if inputs_json:
        import json
        try:
            parsed_inputs = json.loads(inputs_json)
        except Exception:
            parsed_inputs = inputs_json
    return eval_expression(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        expression=str(expression or ""),
        inputs=parsed_inputs,
        timeout_ms=_safe_int(timeout_ms, 5000, 100, 120000),
    )


def js_runtime_call(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    target: str = "",
    args: Any = None,
    args_json: str = "",
    timeout_ms: int = 5000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call a function already loaded in a hosted JS session, e.g. ``target='window.sign'`` or ``target='_ufe79ufe76ufe77'``. This avoids reloading the bundle for each test vector."""
    from argusai.core.reverse.js_runtime import call_function

    parsed_args = args if isinstance(args, list) else []
    if args_json:
        import json
        try:
            raw = json.loads(args_json)
            parsed_args = raw if isinstance(raw, list) else [raw]
        except Exception:
            parsed_args = [args_json]
    return call_function(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        target=str(target or ""),
        args=list(parsed_args or []),
        timeout_ms=_safe_int(timeout_ms, 5000, 100, 120000),
    )


def js_runtime_hook(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    target: str = "",
    hook_id: str = "",
    filter_src: str = "",
    capture_stack: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install an in-runtime function hook. Calls are recorded as oracle records with args, return value, duration and stack. Use this for local bundle instrumentation when browser_hook_function cannot see the obfuscated local function."""
    from argusai.core.reverse.js_runtime import hook_function

    return hook_function(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        target=str(target or ""),
        hook_id=str(hook_id or ""),
        filter_src=str(filter_src or ""),
        capture_stack=bool(capture_stack),
    )


def js_runtime_drain(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    limit: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """Drain captured oracle records from a hosted JS session. Records include hook hits, intercepted fetch/XHR/process.exit events, and other runtime observations."""
    from argusai.core.reverse.js_runtime import drain_oracles

    return drain_oracles(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        limit=_safe_int(limit, 200, 1, 5000),
    )


def js_runtime_logs(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    limit: int = 100,
    **kwargs: Any,
) -> dict[str, Any]:
    """Drain console logs captured inside a hosted JS session."""
    from argusai.core.reverse.js_runtime import read_logs

    return read_logs(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        limit=_safe_int(limit, 100, 1, 1000),
    )


def js_runtime_sources(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """List sources loaded into a hosted JS runtime session, including ids, versions, sizes and hashes."""
    from argusai.core.reverse.js_runtime import list_sources

    return list_sources(api=api, runtime_name=_effective_js_runtime_name(runtime_name, kwargs), session_id=str(session_id or "default"))


def js_runtime_patches(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """List patch history for a hosted JS runtime session."""
    from argusai.core.reverse.js_runtime import list_patches

    return list_patches(api=api, runtime_name=_effective_js_runtime_name(runtime_name, kwargs), session_id=str(session_id or "default"))


def js_runtime_hooks(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """List function hooks installed in a hosted JS runtime session."""
    from argusai.core.reverse.js_runtime import list_hooks

    return list_hooks(api=api, runtime_name=_effective_js_runtime_name(runtime_name, kwargs), session_id=str(session_id or "default"))


def js_runtime_patch(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    source_id: str = "",
    mode: str = "string",
    search: str = "",
    pattern: str = "",
    flags: str = "g",
    start: int = 0,
    end: int = 0,
    replacement: str = "",
    max_replacements: int = 1,
    reevaluate: bool = True,
    timeout_ms: int = 30000,
    note: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Patch a loaded hosted-runtime source by string, regex or offset range. The patched source is re-evaluated by default and patch history is retained in the session."""
    from argusai.core.reverse.js_runtime import patch_source

    return patch_source(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        source_id=str(source_id or ""),
        mode=str(mode or "string"),
        search=str(search or ""),
        pattern=str(pattern or ""),
        flags=str(flags or "g"),
        start=_safe_int(start, 0, 0, 100_000_000),
        end=_safe_int(end, 0, 0, 100_000_000),
        replacement=str(replacement or ""),
        max_replacements=_safe_int(max_replacements, 1, 1, 1000),
        reevaluate=bool(reevaluate),
        timeout_ms=_safe_int(timeout_ms, 30000, 100, 120000),
        note=str(note or ""),
    )


def js_runtime_instrument(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    target: str = "",
    targets: Any = None,
    targets_json: str = "",
    hook_id: str = "",
    filter_src: str = "",
    tag: str = "",
    capture_stack: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install one or more hosted-runtime function hooks for local bundle instrumentation. Captured calls drain via ``js_runtime_drain`` and should be persisted with ``reverse_oracle_add``."""
    from argusai.core.reverse.js_runtime import instrument_functions

    parsed_targets = list(targets or []) if isinstance(targets, list) else []
    if targets_json:
        import json
        try:
            raw = json.loads(targets_json)
            parsed_targets = raw if isinstance(raw, list) else [str(raw)]
        except Exception:
            parsed_targets = [targets_json]
    return instrument_functions(
        api=api,
        runtime_name=_effective_js_runtime_name(runtime_name, kwargs),
        session_id=str(session_id or "default"),
        target=str(target or ""),
        targets=[str(t) for t in parsed_targets],
        hook_id=str(hook_id or ""),
        filter_src=str(filter_src or ""),
        tag=str(tag or ""),
        capture_stack=bool(capture_stack),
    )


def js_runtime_snapshot(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    **kwargs: Any,
) -> dict[str, Any]:
    """Return a lightweight snapshot of one hosted JS runtime session: sources, patches, hooks and buffered counts."""
    from argusai.core.reverse.js_runtime import snapshot

    return snapshot(api=api, runtime_name=_effective_js_runtime_name(runtime_name, kwargs), session_id=str(session_id or "default"))


def js_runtime_restore(
    api: Any,
    runtime_name: str = "default",
    session_id: str = "default",
    env: str = "browser",
    page_url: str = "",
    source: str = "",
    path: str = "",
    filename: str = "",
    timeout_ms: int = 30000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Reset one hosted JS session and optionally reload a source/path. This is a pragmatic restore point for repeatable debugging after aggressive patches."""
    close = js_runtime_close(api, runtime_name=runtime_name, session_id=session_id, **kwargs)
    created = js_runtime_create(api, runtime_name=runtime_name, session_id=session_id, env=env, page_url=page_url, **kwargs)
    if not created.get("ok"):
        return {"ok": False, "closed": close, "created": created}
    if source or path:
        loaded = js_runtime_load(
            api,
            runtime_name=runtime_name,
            session_id=session_id,
            source=source,
            path=path,
            filename=filename,
            timeout_ms=timeout_ms,
            **kwargs,
        )
        return {"ok": bool(loaded.get("ok")), "closed": close, "created": created, "loaded": loaded}
    return {"ok": True, "closed": close, "created": created}


async def js_runtime_sample_profile(
    api: Any,
    url: str = "",
    scope: str = "",
    engine: str = "chromium",
    channel: str = "",
    executable_path: str = "",
    headless: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sample a real browser fingerprint profile from a scoped page so it can be injected into ``js_runtime_create(profile_json=...)`` for offline replay."""
    expr = r"""() => {
      const canvas = document.createElement('canvas');
      canvas.width = 64; canvas.height = 16;
      const ctx = canvas.getContext('2d');
      if (ctx) { ctx.textBaseline = 'top'; ctx.font = '14px Arial'; ctx.fillText('argusai', 2, 2); }
      let glVendor = '', glRenderer = '';
      try {
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
        if (gl && dbg) {
          glVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || '';
          glRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '';
        }
      } catch (_) {}
      return {
        navigator: {
          userAgent: navigator.userAgent,
          platform: navigator.platform,
          vendor: navigator.vendor,
          language: navigator.language,
          languages: Array.from(navigator.languages || []),
          hardwareConcurrency: navigator.hardwareConcurrency,
          deviceMemory: navigator.deviceMemory || null,
          maxTouchPoints: navigator.maxTouchPoints,
          webdriver: navigator.webdriver,
        },
        screen: {
          width: screen.width, height: screen.height,
          availWidth: screen.availWidth, availHeight: screen.availHeight,
          colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
        },
        canvas: { toDataURL: canvas.toDataURL() },
        webgl: { vendor: glVendor, renderer: glRenderer },
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      };
    }"""
    result = await browser_evaluate(
        api,
        url=url,
        scope=scope,
        expression=expr,
        engine=engine,
        channel=channel,
        executable_path=executable_path,
        headless=headless,
        fresh_context=kwargs.get("fresh_context", False),
        extra_wait_ms=_safe_int(kwargs.get("extra_wait_ms", 0), 0, 0, 10000),
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "profile": result.get("value"),
        "profile_json": __import__("json").dumps(result.get("value") or {}, ensure_ascii=False, default=str),
        "source_url": result.get("final_url") or result.get("url"),
    }


def js_runtime_to_reproducer_hint(
    api: Any,
    job_id: str = "",
    stage: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Generate a compact Python-reproducer workflow hint from persisted oracle records."""
    sample: list[dict[str, Any]] = []
    if job_id:
        store = _resolve_reverse_store(api)
        if store is not None:
            try:
                sample = store.read_oracle(str(job_id), stage=str(stage or ""), tail=5)
            except Exception:
                sample = []
    return {
        "ok": True,
        "workflow": [
            "Capture JS intermediate values with js_runtime_instrument or js_runtime_hook.",
            "Drain records with js_runtime_drain and persist them with reverse_oracle_add.",
            "Implement the suspected Python transform as a callable.",
            "Use python_reproducer_verify to compare each oracle stage and stop at the first mismatch.",
        ],
        "python_callable_shape": "def reproduce(record: dict) -> object: ...",
        "sample_records": sample,
    }


def python_reproducer_verify(
    api: Any,
    job_id: str = "",
    oracle_id: str = "",
    stage: str = "",
    script_path: str = "",
    function_name: str = "reproduce",
    expected_field: str = "return_value",
    input_field: str = "",
    tail: int = 200,
    timeout_seconds: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Verify a Python reproducer function against ReverseJob oracle records. The script must define ``function_name(record)`` unless ``input_field`` is set, in which case it receives ``record[input_field]``."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    if not str(script_path or "").strip():
        return {"ok": False, "error": "script_path is required"}
    try:
        records = store.read_oracle(str(job_id), str(oracle_id or ""), stage=str(stage or ""), tail=_safe_int(tail, 200, 1, 5000))
        script = _resolve_session_input_path(api, kwargs, script_path)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    import importlib.util
    import time as _time
    started = _time.time()
    try:
        spec = importlib.util.spec_from_file_location("_argusai_reproducer", script)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": f"cannot import script: {script}"}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, str(function_name or "reproduce"))
    except Exception as e:
        return {"ok": False, "error": f"failed to load reproducer: {e}"}
    compared = matched = 0
    mismatches: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, rec in enumerate(records):
        if _time.time() - started > _safe_int(timeout_seconds, 30, 1, 600):
            return {"ok": False, "error": "verification timeout", "compared": compared, "matched": matched, "mismatches": mismatches, "errors": errors}
        if expected_field not in rec:
            continue
        compared += 1
        try:
            arg = rec.get(input_field) if input_field else rec
            actual = fn(arg)
        except Exception as e:
            errors.append({"index": idx, "stage": rec.get("stage"), "error": str(e)})
            continue
        expected = rec.get(expected_field)
        if actual == expected:
            matched += 1
        elif len(mismatches) < 20:
            mismatches.append({"index": idx, "stage": rec.get("stage"), "actual": actual, "expected": expected})
    return {
        "ok": True,
        "record_count": len(records),
        "compared": compared,
        "matched": matched,
        "mismatched": compared - matched,
        "errors": errors[:20],
        "all_matched": compared > 0 and matched == compared and not errors,
        "mismatches": mismatches,
    }


def reverse_recipe_apply(
    api: Any,
    job_id: str = "",
    recipe_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Install every hook from a recipe into a job in one call. Persisted only — the hooks are saved to ``job.json`` and will be injected the next time the user opens the target URL or runs ``browser_hook_reattach``. To install AND inject immediately, follow with ``browser_hook_reattach`` on an already-open browser. Available ``recipe_id`` values come from ``reverse_recipe_list`` (``canvas_fingerprint``, ``canvas_fingerprint_spoof``, ``audio_fingerprint``, ``webdriver``, ``mouse_trajectory``, ``signed_header``, ``signed_cookie``, ``eval_harvest``, ``location_host_getter``, ``websocket``, ``fetch``, ``eventsource``, ``webassembly_compile``, ``vm_dispatcher_trace`` …)."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not str(job_id or "").strip():
        return {"ok": False, "error": "job_id is required"}
    if not str(recipe_id or "").strip():
        return {"ok": False, "error": "recipe_id is required"}
    try:
        from argusai.core.reverse.hook_recipes import get_recipe
        recipe = get_recipe(str(recipe_id))
        if recipe is None:
            return {"ok": False, "error": f"unknown recipe: {recipe_id}"}
        installed: list[dict[str, Any]] = []
        for spec in recipe.get("hooks") or []:
            rec = store.add_hook(
                str(job_id),
                target=str(spec.get("target") or ""),
                kind=str(spec.get("kind") or "function"),
                expression=str(
                    spec.get("replace_return_src")
                    or spec.get("replace_get_src")
                    or ""
                ),
                recipe=str(recipe_id),
                persisted=True,
                note=str(spec.get("note") or ""),
            )
            # Carry the extended JS behaviour into the hook record.
            extras = {
                k: spec.get(k)
                for k in ("replace_return_src", "replace_get_src", "filter_src", "side_effect_src", "tag")
                if spec.get(k)
            }
            if extras:
                try:
                    job_dir, data = store._load_or_raise(str(job_id))  # noqa: SLF001
                    for h in data.get("hooks") or []:
                        if h.get("id") == rec["id"]:
                            h.update(extras)
                            break
                    store._write_job(job_dir, data)  # noqa: SLF001
                except Exception:
                    pass
            installed.append(rec)
        return {"ok": True, "recipe": str(recipe_id), "installed": installed, "count": len(installed)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
