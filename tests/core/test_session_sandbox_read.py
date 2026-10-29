"""Tests for sandbox file preview windowed reads."""

from __future__ import annotations

import asyncio
from pathlib import Path

from argusai.core.workspace.session_sandbox import (
    _read_text_window,
    read_file,
    run_python_snippet,
    run_shell_snippet,
)


def test_read_text_window_small_file_paginates(tmp_path: Path) -> None:
    p = tmp_path / "small.txt"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")
    first = _read_text_window(p, 0, 5)
    assert first["content"] == "line1"
    assert first["truncated"] is True
    second = _read_text_window(p, first["end_offset"], 20)
    assert "line2" in second["content"]
    assert second["offset_chars"] == first["end_offset"]


def test_read_text_window_large_file_streams_without_full_load(tmp_path: Path) -> None:
  # 512 KiB of ASCII — must not require loading entire string for first window.
    p = tmp_path / "big.js"
    chunk = b"x" * 1024
    with p.open("wb") as fh:
        for _ in range(512):
            fh.write(chunk)
    size = p.stat().st_size
    assert size > 256 * 1024

    win = _read_text_window(p, 0, 10_000)
    assert len(win["content"]) == 10_000
    assert win["truncated"] is True
    assert win["end_offset"] == 10_000

    win2 = _read_text_window(p, 10_000, 10_000)
    assert len(win2["content"]) == 10_000
    assert win2["offset_chars"] == 10_000


def test_read_file_exposes_offset_metadata(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    target = root / "a.js"
    target.write_text("a" * 50_000, encoding="utf-8")
    r = read_file(root, "a.js", max_chars=1000, offset_chars=0)
    assert r["ok"] is True
    assert len(r["content"]) == 1000
    assert r["more"] is True
    assert r["end_offset"] == 1000


def test_empty_snippets_are_rejected_before_materialization(tmp_path: Path) -> None:
    shell = asyncio.run(run_shell_snippet(tmp_path, " \n", shell="powershell"))
    python = asyncio.run(run_python_snippet(tmp_path, "\t"))

    assert shell["error_code"] == "invalid_arguments"
    assert python["error_code"] == "invalid_arguments"
    assert not (tmp_path / ".tmp").exists()
