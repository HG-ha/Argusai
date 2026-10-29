"""Tests for main-window placement helpers."""

from __future__ import annotations

import json

from argusai.core.settings import AppSettings
from argusai.gui import window_geometry as wg
from argusai.gui.window_geometry import (
    center_position,
    clamp_to_work_area,
    resolve_startup_geometry,
)


def test_center_position_within_work_area(monkeypatch):
    monkeypatch.setattr(
        "argusai.gui.window_geometry.get_work_area",
        lambda: (0, 0, 1600, 900),
    )
    x, y = center_position(1280, 820)
    assert x == 160
    assert y == 40


def test_resolve_startup_geometry_uses_saved_position(monkeypatch):
    monkeypatch.setattr(
        "argusai.gui.window_geometry.get_work_area",
        lambda: (0, 0, 1920, 1080),
    )
    settings = AppSettings(
        gui_window_x=120,
        gui_window_y=80,
        gui_window_width=1280,
        gui_window_height=820,
    )
    geom = resolve_startup_geometry(settings)
    assert geom.x == 120
    assert geom.y == 80
    assert geom.width == 1280
    assert geom.height == 820


def test_resolve_startup_geometry_centers_when_unset(monkeypatch):
    monkeypatch.setattr(
        "argusai.gui.window_geometry.get_work_area",
        lambda: (0, 0, 1920, 1080),
    )
    geom = resolve_startup_geometry(AppSettings())
    assert geom.x == 320
    assert geom.y == 70
    assert geom.width == 1280
    assert geom.height == 940


def test_clamp_to_work_area_keeps_window_on_screen(monkeypatch):
    monkeypatch.setattr(
        "argusai.gui.window_geometry.get_work_area",
        lambda: (0, 0, 1920, 1080),
    )
    x, y = clamp_to_work_area(-500, -200, 1280, 820)
    assert x == -500
    assert y == 0


def test_macos_work_area_parses_osascript_json(monkeypatch):
    payload = json.dumps({"x": 0, "y": 25, "width": 1512, "height": 982})

    def _fake_check_output(cmd, **kwargs):
        assert cmd[0] == "osascript"
        return payload + "\n"

    monkeypatch.setattr(wg.subprocess, "check_output", _fake_check_output)
    assert wg._macos_work_area() == (0, 25, 1512, 982)


def test_get_work_area_uses_macos_helper(monkeypatch):
    monkeypatch.setattr(wg.sys, "platform", "darwin")
    monkeypatch.setattr(wg, "_macos_work_area", lambda: (12, 28, 1440, 900))
    assert wg.get_work_area() == (12, 28, 1440, 900)


def test_get_work_area_falls_back_when_macos_helper_fails(monkeypatch):
    monkeypatch.setattr(wg.sys, "platform", "darwin")
    monkeypatch.setattr(wg, "_macos_work_area", lambda: None)
    assert wg.get_work_area() == (0, 0, 1920, 1080)
