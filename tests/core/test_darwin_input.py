"""darwin_input unit tests (CG calls mocked; runs on CI)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from argusai.core.workspace import darwin_input as di


def test_cocoa_point_to_cg() -> None:
    monitors = [{"bottom": 1080}]
    assert di.cocoa_point_to_cg(10, 80, monitors) == (10.0, 1000.0)


def test_mouse_click_posts_events() -> None:
    lib = MagicMock()
    lib.CGEventCreateMouseEvent.return_value = 1
    with patch.object(di, "_accessibility_denied", return_value=(True, "", None)), patch.object(
        di, "_cg_lib", return_value=lib
    ):
        ok, err, perm = di.mouse_click(50, 100, "left", monitors=[{"bottom": 900}])
    assert ok is True
    assert err == ""
    assert perm is None
    assert lib.CGWarpMouseCursorPosition.called
    assert lib.CGEventPost.call_count == 2


def test_type_text_uses_pbcopy_and_cmd_v() -> None:
    lib = MagicMock()
    lib.CGEventCreateKeyboardEvent.return_value = 1
    with patch.object(di, "_accessibility_denied", return_value=(True, "", None)), patch.object(
        di, "_cg_lib", return_value=lib
    ), patch.object(di, "_clipboard_copy", return_value=(True, "")):
        ok, err, perm = di.type_text("hello")
    assert ok is True
    assert err == ""
    assert perm is None
    assert lib.CGEventPost.call_count == 4
