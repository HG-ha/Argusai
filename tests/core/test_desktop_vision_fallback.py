"""Desktop vision fallback — screenshot grounding + image->desktop clicks."""

from __future__ import annotations

from unittest.mock import patch

from argusai.core.tools import desktop_automation as da


def test_grounding_full_virtual_desktop() -> None:
    cap = {"output_pixels": {"width": 960, "height": 540}}
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]
    vdesk = {"left": 0, "top": 0, "width": 1920, "height": 1080}
    g = da._screenshot_grounding(cap, monitors, vdesk, hwnd_i=0, monitor=-1)
    assert g is not None
    assert g["origin_x"] == 0 and g["origin_y"] == 0
    assert g["scale_x"] == 2.0 and g["scale_y"] == 2.0
    # A point at image (480, 270) maps to desktop (960, 540).
    assert da._ground_image_point(480, 270, g) == (960, 540)


def test_grounding_secondary_monitor_offset() -> None:
    cap = {"output_pixels": {"width": 1280, "height": 720}}
    monitors = [
        {"left": 0, "top": 0, "width": 1920, "height": 1080},
        {"left": -1920, "top": 0, "width": 1920, "height": 1080},
    ]
    vdesk = {"left": -1920, "top": 0, "width": 3840, "height": 1080}
    g = da._screenshot_grounding(cap, monitors, vdesk, hwnd_i=0, monitor=1)
    assert g["origin_x"] == -1920
    # Middle of the captured second monitor.
    x, y = da._ground_image_point(640, 360, g)
    assert x == -960 and y == 540


def test_grounding_window_bbox() -> None:
    cap = {
        "output_pixels": {"width": 400, "height": 300},
        "capture_bbox": {"left": 100, "top": 50, "right": 900, "bottom": 650},
    }
    g = da._screenshot_grounding(cap, [], {}, hwnd_i=1234, monitor=-1)
    assert g["origin_x"] == 100 and g["origin_y"] == 50
    assert g["scale_x"] == 2.0 and g["scale_y"] == 2.0
    assert da._ground_image_point(200, 150, g) == (500, 350)


def test_grounding_none_without_image_dims() -> None:
    assert da._screenshot_grounding({}, [], {}, hwnd_i=0, monitor=-1) is None


def test_desktop_click_converts_image_coords_via_grounding() -> None:
    grounding = {"origin_x": 100, "origin_y": 50, "scale_x": 2.0, "scale_y": 2.0}
    captured = {}

    def _fake_click(session_id, x, y, button):
        captured["x"] = x
        captured["y"] = y
        captured["button"] = button
        return {"ok": True, "clicked": True}

    with patch("argusai.core.workspace.desktop_control.mouse_click", side_effect=_fake_click):
        out = da.desktop_click(api=None, image_x=200, image_y=150, grounding=grounding)
    assert out["clicked"] is True
    # 100 + 200*2 = 500 ; 50 + 150*2 = 350
    assert captured["x"] == 500 and captured["y"] == 350


def test_desktop_click_image_without_grounding_falls_through_to_xy() -> None:
    captured = {}

    def _fake_click(session_id, x, y, button):
        captured["x"] = x
        captured["y"] = y
        return {"ok": True, "clicked": True}

    with patch("argusai.core.workspace.desktop_control.mouse_click", side_effect=_fake_click):
        # No grounding -> image_* ignored, plain x/y used.
        da.desktop_click(api=None, x=11, y=22, image_x=999, image_y=999)
    assert captured["x"] == 11 and captured["y"] == 22
