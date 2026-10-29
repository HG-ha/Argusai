"""Tests for run_node_script eval → script mode coercion."""

from __future__ import annotations

from argusai.core.reverse.js_run_mode import (
    coerce_js_run_mode,
    should_retry_eval_as_script,
)


def test_coerce_var_require_to_script() -> None:
    mode, notice = coerce_js_run_mode("var fs = require('fs')", "eval")
    assert mode == "script"
    assert notice


def test_coerce_multiline_to_script() -> None:
    mode, notice = coerce_js_run_mode("a()\nb()", "eval")
    assert mode == "script"
    assert notice


def test_coerce_console_log_to_script() -> None:
    mode, notice = coerce_js_run_mode("console.log(1)", "eval")
    assert mode == "script"
    assert notice


def test_keeps_simple_expression() -> None:
    mode, notice = coerce_js_run_mode("decodeURIComponent('abc')", "eval")
    assert mode == "eval"
    assert notice is None


def test_script_mode_unchanged() -> None:
    mode, notice = coerce_js_run_mode("var x = 1", "script")
    assert mode == "script"
    assert notice is None


def test_should_retry_on_expr_js_error() -> None:
    assert should_retry_eval_as_script({
        "ok": False,
        "error": "Unexpected token 'var'",
        "stack": "at expr.js:1",
    })


def test_should_not_retry_on_other_errors() -> None:
    assert not should_retry_eval_as_script({
        "ok": False,
        "error": "ReferenceError: foo is not defined",
        "stack": "at main.js:2",
    })
