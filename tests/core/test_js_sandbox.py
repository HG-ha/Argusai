"""Regression tests for ``argusai.core.reverse.js_sandbox``.

The single bug this file exists to guard against is real:
in Playwright's Python API, ``Page.evaluate(pageFunction, arg)`` has
EXACTLY two positional parameters and accepts NO ``timeout`` kwarg.
A previous build passed ``timeout=timeout_ms`` to it and the model only
discovered the breakage when an actual run produced::

    Page.evaluate() got an unexpected keyword argument 'timeout'

These tests fake a Playwright page object and replay the real call
sequence, asserting:

1. NO ``page.evaluate`` call carries a ``timeout`` kwarg.
2. The user-requested timeout budget reaches JS via the snippet args
   (so a slow snippet can still be aborted by ``Promise.race``).
3. ``page.set_default_timeout`` is wired up so navigation / route
   helpers still honour the same budget.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from argusai.core.reverse import js_sandbox


class _FakePage:
    """Minimal stand-in for ``playwright.async_api.Page``.

    Implements only what ``_sandbox_page`` touches. Every ``evaluate``
    call is recorded so a test can replay the arguments and assert
    invariants on them.
    """

    def __init__(self, eval_return: Any = "ok"):
        self.evaluate_calls: list[dict[str, Any]] = []
        self.route_calls: list[str] = []
        self.add_script_calls: list[str] = []
        self.goto_calls: list[str] = []
        self.default_timeouts: list[int] = []
        self._eval_return = eval_return
        self._handlers: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def set_default_timeout(self, ms: int) -> None:
        self.default_timeouts.append(int(ms))

    async def route(self, pattern: str, _handler: Any) -> None:
        self.route_calls.append(pattern)

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    async def add_script_tag(self, *, content: str) -> None:
        self.add_script_calls.append(content)

    async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        # Reject the historical bug shape on the spot so the assertion
        # fires before any other check; the rest of the test suite then
        # confirms the *expected* call shape positively.
        if "timeout" in kwargs:
            raise TypeError(
                "Page.evaluate() got an unexpected keyword argument 'timeout'"
            )
        self.evaluate_calls.append({"args": list(args), "kwargs": dict(kwargs)})
        return self._eval_return


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_sandbox_page_does_not_pass_timeout_kwarg_to_evaluate() -> None:
    """The bug the user actually hit: timeout= must NEVER reach evaluate()."""
    page = _FakePage(eval_return=42)

    result = asyncio.new_event_loop().run_until_complete(
        js_sandbox._sandbox_page(
            page,
            code="function fn(x) { return x + 1; }",
            expression="fn(inputs[0])",
            inputs=[5],
            timeout_ms=7000,
            libraries=[],
        )
    )

    assert result["ok"] is True, result
    # _FakePage.evaluate raises if timeout= shows up, but be explicit so
    # the failure message is obvious when this regresses.
    for call in page.evaluate_calls:
        assert "timeout" not in call["kwargs"], (
            f"page.evaluate received a forbidden 'timeout' kwarg: {call!r}"
        )


def test_sandbox_page_forwards_user_timeout_to_js_promise_race() -> None:
    """Caller's budget must travel into the JS snippet as ``timeoutMs``.

    Otherwise a snippet that loops forever would never be aborted —
    deleting the Python-side ``timeout=`` kwarg would silently degrade
    the safety net unless we also wire the budget through the args.
    """
    page = _FakePage(eval_return="ignored")
    asyncio.new_event_loop().run_until_complete(
        js_sandbox._sandbox_page(
            page,
            code="",
            expression="1 + 1",
            inputs=None,
            timeout_ms=4321,
            libraries=[],
        )
    )

    # The race-against-setTimeout call is the LAST evaluate() invocation;
    # bootstrap + inputs upload happen before it.
    final = page.evaluate_calls[-1]
    assert any("Promise.race" in str(a) for a in final["args"]), (
        f"expected the final evaluate to contain the race snippet, got {final!r}"
    )
    payload = next(
        (a for a in final["args"] if isinstance(a, dict) and "timeoutMs" in a),
        None,
    )
    assert payload is not None, f"timeoutMs payload missing in {final!r}"
    assert payload["timeoutMs"] == 4321
    assert payload["expr"] == "1 + 1"


def test_sandbox_page_sets_default_timeout_on_page() -> None:
    """``set_default_timeout`` covers route/goto/etc, which DO support timeout.

    Failing softly (try/except) is fine, but the call itself must still
    happen so navigation budgets stay honest.
    """
    page = _FakePage(eval_return=None)
    asyncio.new_event_loop().run_until_complete(
        js_sandbox._sandbox_page(
            page,
            code="",
            expression="undefined",
            inputs=None,
            timeout_ms=9000,
            libraries=[],
        )
    )
    assert page.default_timeouts == [9000]


def test_sandbox_page_reports_error_without_crash_when_evaluate_throws() -> None:
    """If the model's snippet throws, we capture the error, not bubble it."""
    class _ThrowingPage(_FakePage):
        async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
            if "timeout" in kwargs:
                raise TypeError("must not pass timeout to evaluate()")
            self.evaluate_calls.append({"args": list(args), "kwargs": dict(kwargs)})
            # Throw only on the final race snippet, not on bootstrap.
            if len(self.evaluate_calls) >= 3:
                raise RuntimeError("ReferenceError: foo is not defined")
            return None

    page = _ThrowingPage()
    result = asyncio.new_event_loop().run_until_complete(
        js_sandbox._sandbox_page(
            page,
            code="",
            expression="foo()",
            inputs=None,
            timeout_ms=5000,
            libraries=[],
        )
    )
    assert result["ok"] is False
    assert "foo is not defined" in result["error"]
    assert result["network"] == "blocked"
