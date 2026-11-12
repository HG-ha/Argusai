"""Regression tests for lightweight JavaScript reverse-analysis helpers."""

from __future__ import annotations

import time

from argusai.core.reverse.js_reverse_tools import extract_strings


def test_extract_strings_preserves_escaped_delimiters() -> None:
    code = r'''const a = "say \"hello\""; const b = 'C:\\temp'; const c = `/api/${id}`;'''

    result = extract_strings(code)
    values = [item["value"] for item in result["strings"]]
    urls = [item["value"] for item in result["urls"]]

    assert 'say "hello"' in values
    assert r"C:\temp" in values
    assert "/api/${id}" in urls


def test_extract_strings_avoids_escape_backtracking() -> None:
    # An unterminated literal made the old ambiguous ``\\.|.`` repetition try
    # exponentially many escape/unescaped combinations.  Twenty-four pairs
    # already took noticeable CPU; real minified bundles could spin for hours.
    code = 'const broken = "' + (r"\a" * 24) + "!"

    started = time.perf_counter()
    result = extract_strings(code)
    elapsed = time.perf_counter() - started

    assert result["summary"]["strings"] == 0
    assert elapsed < 0.5, f"string scan unexpectedly took {elapsed:.3f}s"
