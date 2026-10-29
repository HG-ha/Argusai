"""Python-first JavaScript execution.

This module gives the AI a way to **run JavaScript without opening a
browser**. The point isn't to compete with a real browser — we still
have Playwright for DOM / fetch / WebCrypto / SubtleCrypto scenarios —
the point is to let the assistant write end-to-end PoCs in pure-Python +
Node where the JS is doing pure computation (crypto, decoders,
deobfuscated string arrays, etc.) so the final exploit doesn't require
the operator to keep a Chrome instance open.

Flow:
- Vendored Node executes a generic ``run_js.mjs`` runner.
- Caller supplies ``source`` (JS string) + a ``mode``:
    - ``script``: run the source verbatim. Set a ``__result`` global to
      surface a return value.
    - ``eval``: evaluate a single expression and return its value.
    - ``call``: define the source, then call a named global function
      with JSON-serialised ``args``.
- Result is returned as JSON over stdout, then re-parsed here.

The Node runner enforces an inner ``vm.Script`` timeout AND captures
console.log/error so a noisy script doesn't blow up our pipe. Python
also enforces a hard subprocess timeout.

Design notes:
- We do **not** expose DOM/fetch/localStorage. If a script needs those,
  the caller should fall back to ``sandbox_eval_js`` (Playwright). The
  whole point of this module is the cases that *don't* need a browser.
- Globals can be injected. The classic use case: ``run_js_function``
  with the entire deobfuscated bundle as ``source`` and the entry
  function name + its arguments → get back the encrypted payload as a
  string, send it via ``http_request_impersonate``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("argusai.js_python")


def _runner_path() -> Path:
    from argusai.core.reverse.runtime_manager import _vendor_dir

    return _vendor_dir() / "runners" / "run_js.mjs"


def _node_modules_root(api: Any | None = None) -> Path | None:
    """The managed Node modules root, if any (for scripts that want to
    ``import 'crypto-js'`` etc.). For our default runner none of that
    is needed, but exposing this allows users to point their own JS at
    other managed packages."""
    from argusai.core.managed_runtimes import node_tools_dir
    from argusai.core.reverse.runtime_manager import _tools_dir

    candidate = node_tools_dir(_tools_dir(api))
    return candidate if candidate.exists() else None


def _coerce_int(value: Any, default: int, minimum: int = 0, maximum: int = 600_000) -> int:
    try:
        n = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, n))


def _parse_result(payload_str: str) -> dict[str, Any]:
    """Parse the runner's stdout. The runner ALWAYS writes a single JSON
    object on stdout, but we defend against partial writes anyway.
    """
    if not payload_str:
        return {"ok": False, "error": "empty stdout from runner", "result": None}
    s = payload_str.strip()
    # The runner only writes ONE object, but on some shells extra junk
    # can land in stdout (e.g. an unrelated warning). Try to peel out
    # the first complete JSON object.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Find the first '{' and balance braces. If that fails, give up.
        try:
            start = s.index("{")
            depth = 0
            for i, ch in enumerate(s[start:], start=start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(s[start : i + 1])
        except Exception:
            pass
        return {
            "ok": False,
            "error": "runner stdout was not valid JSON",
            "result": None,
            "raw_stdout_preview": s[:1000],
        }


def _invoke_js_runner(
    source: str,
    *,
    mode: str,
    function_name: str,
    args: list[Any] | None,
    globals_: dict[str, Any] | None,
    inner_timeout_ms: int,
    process_timeout_seconds: int,
    env: str,
    page_url: str,
    user_agent: str,
    profile: dict[str, Any] | None,
    api: Any | None,
    sandbox_root: Path | None,
) -> dict[str, Any]:
    from argusai.core.reverse.runtime_manager import run_node_script

    runner = _runner_path()
    if not runner.exists():
        return {
            "ok": False,
            "error": (
                f"JS runner asset is missing at {runner}. Reinstall or repair ArgusAI; "
                "the managed Node runtime does not replace application runner assets."
            ),
            "result": None,
        }

    payload: dict[str, Any] = {
        "mode": mode,
        "env": str(env or "node").lower(),
        "source": source,
        "function_name": function_name,
        "args": list(args or []),
        "globals": dict(globals_ or {}),
        "profile": dict(profile or {}),
        "timeout_ms": _coerce_int(inner_timeout_ms, 5000, 100, 60_000),
    }
    if page_url:
        payload["page_url"] = str(page_url)
    if user_agent:
        payload["user_agent"] = str(user_agent)
    try:
        input_text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as e:
        return {"ok": False, "error": f"args not JSON-serialisable: {e}", "result": None}

    sub = run_node_script(
        runner,
        input_text=input_text,
        timeout=_coerce_int(process_timeout_seconds, 30, 1, 600),
        api=api,
        node_modules_dir=_node_modules_root(api),
        cwd=sandbox_root,
    )
    if not sub.get("ok"):
        return {
            "ok": False,
            "error": sub.get("error") or "node subprocess failed",
            "returncode": sub.get("returncode"),
            "stdout": (sub.get("stdout") or "")[:2000],
            "stderr": (sub.get("stderr") or "")[:2000],
            "result": None,
            "elapsed_ms": sub.get("elapsed_ms"),
        }

    parsed = _parse_result(sub.get("stdout") or "")
    if "elapsed_ms" not in parsed:
        parsed["elapsed_ms"] = sub.get("elapsed_ms")
    return parsed


def _attach_mode_metadata(
    parsed: dict[str, Any],
    *,
    mode_requested: str,
    mode_effective: str,
    notice: str | None,
) -> dict[str, Any]:
    parsed["mode_requested"] = mode_requested
    parsed["mode"] = mode_effective
    if notice:
        parsed["mode_coerced"] = True
        parsed["mode_notice"] = notice
        hint = str(parsed.get("hint") or "").strip()
        parsed["hint"] = f"{notice} {hint}".strip() if hint else notice
    return parsed


def run_js(
    source: str,
    *,
    mode: str = "script",
    function_name: str = "",
    args: list[Any] | None = None,
    globals_: dict[str, Any] | None = None,
    inner_timeout_ms: int = 5000,
    process_timeout_seconds: int = 30,
    env: str = "node",
    page_url: str = "",
    user_agent: str = "",
    profile: dict[str, Any] | None = None,
    api: Any | None = None,
    sandbox_root: Path | None = None,
) -> dict[str, Any]:
    """Run ``source`` in managed Node, return ``{ok, result, ...}``.

    Args:
        source: JS source. For ``mode='call'`` this must define the
            function named by ``function_name`` (or assign it to
            ``globalThis``).
        mode: One of ``script | eval | call``.
        function_name: For ``mode='call'`` only.
        args: For ``mode='call'`` only — JSON-serialisable list.
        globals_: Optional pre-injected globals (JSON-serialisable).
        inner_timeout_ms: VM-level timeout (100ms - 60s). Applies to
            both the initial script and, for ``call`` mode, the
            function invocation.
        process_timeout_seconds: Wall-clock timeout on the whole Node
            process (1 - 600s).
        env: ``"node"`` (default) — clean Node-style sandbox with
            console, Buffer, crypto. Or ``"browser"`` — adds stub
            window / document / navigator / location / localStorage /
            performance / fetch / XMLHttpRequest globals AND removes
            process / Buffer / require / global so common anti-detection
            guards (e.g. ``typeof process !== 'undefined' &&
            process.versions.node`` or ``typeof window === 'undefined'``)
            pass. Not a real DOM — for that fall back to
            ``sandbox_eval_js`` (Playwright).
        page_url: ``env=browser`` only — value seeded into
            ``location.href`` / ``document.URL``. Default
            ``https://example.com/``.
        user_agent: ``env=browser`` only — ``navigator.userAgent``.
            Defaults to a recent Chrome on Windows so simple UA sniffs
            classify the run as "browser".
        api: Optional ``Api`` instance for ``runtime_manager`` user
            settings.

    Returns:
        ``{ok: bool, result: str|None, result_type, result_size_chars,
           stdout, stderr, error, stack, elapsed_ms, mode, mode_notice?}``
    """
    from argusai.core.reverse.js_run_mode import (
        coerce_js_run_mode,
        retry_eval_notice,
        should_retry_eval_as_script,
    )

    mode_requested = str(mode or "script").strip().lower() or "script"
    mode_effective, notice = coerce_js_run_mode(source, mode_requested)
    invoke_kw = dict(
        mode=mode_effective,
        function_name=function_name,
        args=args,
        globals_=globals_,
        inner_timeout_ms=inner_timeout_ms,
        process_timeout_seconds=process_timeout_seconds,
        env=env,
        page_url=page_url,
        user_agent=user_agent,
        profile=profile,
        api=api,
        sandbox_root=sandbox_root,
    )
    parsed = _invoke_js_runner(source, **invoke_kw)
    if not parsed.get("ok") and mode_effective == "eval" and should_retry_eval_as_script(parsed):
        notice = retry_eval_notice()
        parsed = _invoke_js_runner(source, **{**invoke_kw, "mode": "script"})
        mode_effective = "script"
    return _attach_mode_metadata(
        parsed,
        mode_requested=mode_requested,
        mode_effective=mode_effective,
        notice=notice,
    )


def run_js_function(
    source: str,
    function_name: str,
    args: list[Any] | None = None,
    *,
    globals_: dict[str, Any] | None = None,
    inner_timeout_ms: int = 5000,
    process_timeout_seconds: int = 30,
    env: str = "node",
    page_url: str = "",
    user_agent: str = "",
    profile: dict[str, Any] | None = None,
    api: Any | None = None,
    sandbox_root: Path | None = None,
) -> dict[str, Any]:
    """Convenience: define a JS module's source, then invoke one named
    function from it.

    Use this when the deobfuscated source exposes the entry point as a
    global (``window.entryFn = ...`` or ``var entryFn = ...`` in the
    outermost scope after deobfuscation). Returns the JSON-serialised
    result. If the result is a Promise, the runner awaits it within
    ``inner_timeout_ms``.

    Pass ``env='browser'`` when the script gates on
    ``typeof window !== 'undefined'`` / ``process.versions.node`` —
    common in anti-bot decoders that refuse to run outside a real page.

    For the classic "decrypt-then-call-entry" pattern in a reverse job,
    pair this with ``http_request_impersonate``:

      1. ``read_text_file(path=<deobfuscated bundle>)`` → fetch source
      2. ``run_js_function(source, 'entryFn', [{challengePack, payload}],
         env='browser')`` → encrypted body
      3. ``http_request_impersonate(target='...', data=<encrypted body>)``
    """
    return run_js(
        source,
        mode="call",
        function_name=function_name,
        args=args,
        globals_=globals_,
        inner_timeout_ms=inner_timeout_ms,
        process_timeout_seconds=process_timeout_seconds,
        env=env,
        page_url=page_url,
        user_agent=user_agent,
        profile=profile,
        api=api,
        sandbox_root=sandbox_root,
    )
