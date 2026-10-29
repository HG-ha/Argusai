"""Exact-substring sandbox patch/read helpers."""

from __future__ import annotations

from argusai.core.infra import text_io
from argusai.core.workspace.session_sandbox import (
    _content_raw_from_numbered,
    patch_file,
    read_file,
    stat_file,
    write_file,
)


def test_patch_file_replaces_unique_substring(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    write_file(root, "a.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n", "py")
    result = patch_file(root, "a.py", "return 1", "return 42")
    assert result["ok"] is True
    assert result["replacements"] == 1
    assert result["first_match_line"] == 2
    text = (root / "a.py").read_text(encoding="utf-8")
    assert "return 42" in text
    assert "return 1" not in text


def test_patch_file_rejects_ambiguous_match(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    write_file(root, "a.py", "x=1\nx=1\n", "py")
    result = patch_file(root, "a.py", "x=1", "x=2")
    assert result["ok"] is False
    assert result["error_code"] == "patch_ambiguous_match"
    assert result["match_count"] == 2


def test_patch_file_replace_all(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    write_file(root, "a.py", "x=1\nx=1\n", "py")
    result = patch_file(root, "a.py", "x=1", "x=2", replace_all=True)
    assert result["ok"] is True
    assert result["replacements"] == 2
    assert (root / "a.py").read_text(encoding="utf-8") == "x=2\nx=2\n"


def test_content_raw_strips_line_number_prefix() -> None:
    numbered = "     1|alpha\n     2|beta\n"
    assert _content_raw_from_numbered(numbered) == "alpha\nbeta"


def test_stat_file_and_grep_read_workflow(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    body = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    write_file(root, "a.py", body, "py")

    meta = stat_file(root, "a.py")
    assert meta["ok"] is True
    assert meta["line_count"] == 5
    assert meta["large_file"] is False

    hits = text_io.grep_text_file(pattern=r"def bar", path="a.py", base=str(root))
    assert hits["ok"] is True
    assert hits["matches"][0]["line"] == 4

    window = text_io.read_text_file(path="a.py", offset=4, limit=2, base=str(root))
    assert window["ok"] is True
    assert "bar" in window["content"]
    raw = _content_raw_from_numbered(window["content"])
    assert raw.startswith("def bar")

    patched = patch_file(root, "a.py", "return 2", "return 9")
    assert patched["ok"] is True
    assert "return 9" in (root / "a.py").read_text(encoding="utf-8")


def test_read_file_char_mode_still_available(tmp_path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    write_file(root, "b.py", "line1\nline2\nline3\n", "py")
    first = read_file(root, "b.py", max_chars=10, offset_chars=0, line_numbers=True)
    assert first["ok"] is True
    assert first["content"] == "line1\nline"
    assert first["start_line"] == 1
    assert "1|line1" in first["content_numbered"]

    second = read_file(root, "b.py", max_chars=20, offset_chars=first["end_offset"], line_numbers=True)
    assert second["offset_chars"] == first["end_offset"]
    assert second["start_line"] >= 2
