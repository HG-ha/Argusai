"""Multi-monitor helpers for desktop control."""

from __future__ import annotations

from argusai.core.workspace.desktop_control import (
    monitor_at_point,
    virtual_desktop_bounds,
)


def test_virtual_desktop_bounds_dual_monitor() -> None:
    mons = [
        {"index": 0, "primary": True, "left": 0, "top": 0, "width": 1920, "height": 1080, "right": 1920, "bottom": 1080},
        {"index": 1, "primary": False, "left": -1920, "top": 0, "width": 1920, "height": 1080, "right": 0, "bottom": 1080},
    ]
    v = virtual_desktop_bounds(mons)
    assert v["left"] == -1920
    assert v["width"] == 3840
    assert v["monitor_count"] == 2
    assert monitor_at_point(-1000, 500, mons) == 1
    assert monitor_at_point(100, 100, mons) == 0
