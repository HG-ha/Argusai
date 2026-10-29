"""User/system path text file read/write helpers."""

from __future__ import annotations

from argusai.core.infra import text_io


def test_read_text_file_includes_content_raw(tmp_path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = text_io.read_text_file(path=str(target))
    assert result["ok"] is True
    assert "foo" in result["content"]
    assert result["content_raw"] == "def foo():\n    return 1"
    assert result["read_mode"] == "lines"


def test_stat_text_file_large_file_hint(tmp_path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("x" * (text_io.LARGE_TEXT_READ_BYTES + 1), encoding="utf-8")

    meta = text_io.stat_text_file(path=str(target))
    assert meta["ok"] is True
    assert meta["large_file"] is True
    assert "grep_text" in meta["hint"]


def test_patch_and_write_text_file(tmp_path) -> None:
    target = tmp_path / "edit.py"
    target.write_text("a=1\nb=2\n", encoding="utf-8")

    patched = text_io.patch_text_file(path=str(target), old_string="a=1", new_string="a=9")
    assert patched["ok"] is True
    assert target.read_text(encoding="utf-8") == "a=9\nb=2\n"

    created = tmp_path / "new.txt"
    written = text_io.write_text_file(path=str(created), content="hello\n")
    assert written["ok"] is True
    assert created.read_text(encoding="utf-8") == "hello\n"


def test_patch_text_file_rejects_ambiguous(tmp_path) -> None:
    target = tmp_path / "dup.py"
    target.write_text("x=1\nx=1\n", encoding="utf-8")

    result = text_io.patch_text_file(path=str(target), old_string="x=1", new_string="x=2")
    assert result["ok"] is False
    assert result["error_code"] == "patch_ambiguous_match"
