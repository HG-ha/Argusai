"""Tests for ephemeral sandbox explain message building."""

from __future__ import annotations

import pytest

from argusai.gui.assistant_explain import build_explain_messages


def test_build_explain_messages_requires_file_selection():
    with pytest.raises(ValueError):
        build_explain_messages({"kind": "message", "role": "user", "text": "hi"})


def test_build_explain_messages_includes_selection_and_context():
    msgs = build_explain_messages(
        {
            "kind": "file_selection",
            "file_path": "scripts/foo.py",
            "start_line": 2,
            "end_line": 4,
            "text": "print('x')",
        },
        context={"source": "sandbox_preview", "route": "assistant"},
        surrounding="# File: scripts/foo.py\nline1\nprint('x')\nline3\n",
    )
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    user = msgs[1]["content"]
    assert "scripts/foo.py" in user
    assert "print('x')" in user
    assert "sandbox_preview" in user
    assert "Surrounding file context" in user
    assert "User question" in user


def test_build_explain_messages_uses_custom_question():
    custom = "这段逻辑是否存在 SQL 注入风险？"
    msgs = build_explain_messages(
        {
            "kind": "file_selection",
            "file_path": "a.py",
            "start_line": 1,
            "end_line": 1,
            "text": "cursor.execute(q)",
        },
        question=custom,
    )
    assert custom in msgs[1]["content"]
