"""Static contracts for the frameless pywebview title bar."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_interactive_titlebar_controls_are_not_inside_drag_region() -> None:
    app = (ROOT / "argusai/webui/src/components/App.vue").read_text(encoding="utf-8")

    assert 'class="titlebar titlebar-drag"' not in app
    drag_start = app.index('class="titlebar-drag titlebar-drag-surface"')
    drag_end = app.index("</div>", drag_start)
    controls = app.index('class="titlebar-btns"')
    window_controls = app.index('class="titlebar-win-controls"')
    assert drag_end < controls < window_controls
    assert '@pointerdown.stop @dblclick.stop' in app[controls:window_controls]


def test_titlebar_drag_selector_targets_only_the_dedicated_surface() -> None:
    launch = (ROOT / "argusai/gui/launch.py").read_text(encoding="utf-8")
    theme = (ROOT / "argusai/webui/src/assets/theme.css").read_text(encoding="utf-8")

    assert 'DRAG_REGION_SELECTOR"] = ".titlebar-drag"' in launch
    assert ".titlebar-drag-surface" in theme
    assert "-webkit-app-region: no-drag" in theme
