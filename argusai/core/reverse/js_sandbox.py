"""Lightweight JavaScript sandbox for reverse-engineering helpers.

This is not a strong security boundary. It is a convenience layer around the
managed Chromium process that runs snippets in an isolated about:blank page,
blocks network by default, applies time/output limits, and returns structured
console/result/error data for the AI assistant.
"""

from __future__ import annotations

import json
import time
from typing import Any


CODE_LIMIT = 200_000
OUTPUT_LIMIT = 12_000
CASE_LIMIT = 50


def _clip(value: Any, limit: int = OUTPUT_LIMIT) -> str:
    try:
        if isinstance(value, str):
            return value[:limit]
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return repr(value)[:limit]


async def _sandbox_page(page, code: str, expression: str, inputs: Any, timeout_ms: int, libraries: list[str]) -> dict[str, Any]:
    started = time.time()
    logs: list[str] = []
    page.on("console", lambda m: logs.append(f"{m.type}:{m.text}"[:800]) if len(logs) < 100 else None)
    page.on("pageerror", lambda e: logs.append(f"error:{e}"[:800]) if len(logs) < 100 else None)
    # Cap *navigation* / *route* / *waitForFunction* style operations at the
    # caller-requested budget. Playwright's ``page.evaluate(...)`` itself
    # does NOT accept a ``timeout`` kwarg (only goto/route-fulfill/etc do),
    # so we instead enforce the JS-side budget via the Promise.race below.
    try:
        page.set_default_timeout(timeout_ms)
    except Exception:
        pass
    await page.route("**/*", lambda route: route.abort())
    await page.goto("about:blank")
    bootstrap = """
    () => {
      window.__argusai_inputs = undefined;
      window.__argusai_result = undefined;
      window.__argusai_error = undefined;
      window.alert = (...args) => console.log('alert:' + args.join(' '));
      window.confirm = () => false;
      window.prompt = () => '';
      window.open = () => null;
    }
    """
    await page.evaluate(bootstrap)
    await page.evaluate("(v) => { window.__argusai_inputs = v; }", inputs)
    try:
        for lib in libraries[:10]:
            if lib:
                await page.add_script_tag(content=lib[:CODE_LIMIT])
        if code:
            await page.add_script_tag(content=code[:CODE_LIMIT])
        eval_expr = expression.strip() if expression else "undefined"
        # IMPORTANT: ``page.evaluate(pageFunction, arg)`` has exactly two
        # positional parameters in Playwright; it does NOT take ``timeout``.
        # We pass both the expression and the caller's budget as a single
        # object so the JS side can race the snippet against a setTimeout
        # whose duration honors the user-configured ``timeout_ms``.
        value = await page.evaluate(
            """
            async ({ expr, timeoutMs }) => {
              const run = new Function('inputs', `return (${expr});`);
              return await Promise.race([
                Promise.resolve(run(window.__argusai_inputs)),
                new Promise((_, reject) => setTimeout(() => reject(new Error('sandbox timeout')), timeoutMs))
              ]);
            }
            """,
            {"expr": eval_expr, "timeoutMs": int(timeout_ms)},
        )
        return {
            "ok": True,
            "elapsed_ms": int((time.time() - started) * 1000),
            "value": value,
            "value_preview": _clip(value),
            "console": logs,
            "network": "blocked",
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": str(e),
            "console": logs,
            "network": "blocked",
        }


async def sandbox_eval_js(
    code: str = "",
    expression: str = "",
    inputs: Any = None,
    *,
    timeout_ms: int = 5000,
    libraries: list[str] | None = None,
) -> dict[str, Any]:
    """Run JS in an isolated browser page with network blocked."""
    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "error": f"browser hub unavailable: {e}"}
    if len(code or "") > CODE_LIMIT:
        return {"ok": False, "error": f"code too large; max {CODE_LIMIT} chars"}
    timeout_ms = max(1000, min(int(timeout_ms or 5000), 30000))
    expression = expression or "undefined"
    hub = get_browser_hub(headless=True)
    return await hub.acall_with_page(
        _sandbox_page,
        code or "",
        expression,
        inputs,
        timeout_ms,
        libraries or [],
        fresh_context=True,
    )


async def sandbox_test_function(
    code: str = "",
    function_name: str = "",
    cases: list[Any] | None = None,
    *,
    timeout_ms: int = 5000,
    libraries: list[str] | None = None,
) -> dict[str, Any]:
    """Call a named JS function for multiple inputs in the lightweight sandbox."""
    if not function_name:
        return {"ok": False, "error": "function_name is required"}
    cases = (cases or [])[:CASE_LIMIT]
    expression = f"""
    (() => {{
      const fn = {function_name};
      if (typeof fn !== 'function') throw new Error('not a function: {function_name}');
      return (window.__argusai_inputs || []).map((item) => {{
        try {{
          const args = Array.isArray(item) ? item : [item];
          return {{ ok: true, input: item, output: fn(...args) }};
        }} catch (e) {{
          return {{ ok: false, input: item, error: String(e && e.message || e) }};
        }}
      }});
    }})()
    """
    return await sandbox_eval_js(
        code=code,
        expression=expression,
        inputs=cases,
        timeout_ms=timeout_ms,
        libraries=libraries or [],
    )


async def sandbox_compare_outputs(
    code: str = "",
    function_name: str = "",
    cases: list[Any] | None = None,
    *,
    repeats: int = 2,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    repeats = max(1, min(int(repeats or 2), 5))
    all_runs = []
    for _ in range(repeats):
        result = await sandbox_test_function(code=code, function_name=function_name, cases=cases or [], timeout_ms=timeout_ms)
        all_runs.append(result.get("value") if result.get("ok") else {"error": result.get("error")})
    stable = len({json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) for r in all_runs}) == 1
    return {"ok": True, "stable": stable, "runs": all_runs}


def sandbox_status() -> dict[str, Any]:
    try:
        from argusai.core.browser.browser_pool import get_browser_hub

        return {"ok": True, **get_browser_hub().status(), "network": "blocked by default"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def sandbox_reset() -> dict[str, Any]:
    try:
        from argusai.core.browser.browser_pool import get_browser_hub
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **(await get_browser_hub().areset())}

