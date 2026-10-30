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


def test_macos_titlebar_state_is_pushed_after_native_window_resolution() -> None:
    launch = (ROOT / "argusai/gui/launch.py").read_text(encoding="utf-8")
    main = (ROOT / "argusai/webui/src/main.js").read_text(encoding="utf-8")
    macos = (ROOT / "argusai/gui/macos_titlebar.py").read_text(encoding="utf-8")

    assert "argusai:app-info-updated" in launch
    assert "argusai:app-info-updated" in main
    assert "TITLEBAR_RECONCILE_DELAYS_MS" in main
    assert "reconcileMacTitlebarState()" in main
    assert "await refreshAppInfo()" in main
    assert "AppHelper.callAfter(invoke)" in macos
    assert "wait_seconds = 4.0" in macos
    assert "NSColor.clearColor()" in macos
