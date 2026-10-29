"""Windows UI Automation helpers."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from argusai.core.assistant.streaming import _CONFIRM_TOOLS, _NO_AUTO_APPROVE_TOOLS, _WRITE_TOOLS
from argusai.core.tools import EXECUTORS
from argusai.core.workspace import desktop_uia as du


def test_uia_tools_registered() -> None:
    assert "desktop_uia_find" in EXECUTORS
    assert "desktop_uia_action" in EXECUTORS
    assert "desktop_uia_find" in _CONFIRM_TOOLS
    assert "desktop_uia_action" in _CONFIRM_TOOLS
    assert "desktop_uia_action" in _WRITE_TOOLS
    assert "desktop_uia_action" in _NO_AUTO_APPROVE_TOOLS
    assert "desktop_uia_find" not in _NO_AUTO_APPROVE_TOOLS


def test_uia_unavailable_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(du.sys, "platform", "linux")
    out = du.uia_available()
    assert out["available"] is False


def test_build_condition_unknown_control_type() -> None:
    if not sys.platform.startswith("win"):
        pytest.skip("Windows only")
    with pytest.raises(ValueError, match="unknown control_type"):
        du._build_condition(control_type="not-a-real-type")


def test_element_action_requires_locator() -> None:
    out = du.element_action(action="focus")
    assert out["ok"] is False
    assert out.get("error_code") == "invalid_arguments"


def test_element_action_click_uses_rect_center(monkeypatch) -> None:
    class FakeRoot:
        CurrentControlType = 50007
        CurrentName = "联系人"
        CurrentAutomationId = ""
        CurrentClassName = "ListItem"
        CurrentIsEnabled = True
        CurrentIsOffscreen = False
        CurrentBoundingRectangle = SimpleNamespace(left=100, top=200, right=180, bottom=240)

        def GetRuntimeId(self):
            return [1, 9]

        def GetCurrentPattern(self, _pid):
            return None

        def FindAll(self, _scope, _cond):
            class Arr:
                Length = 1

                def GetElement(self, _i):
                    return FakeRoot()

            return Arr()

        def FindFirst(self, _scope, _cond):
            return FakeRoot()

    seen: dict[str, object] = {}

    monkeypatch.setattr(du, "_root_element", lambda _hwnd=0: FakeRoot())
    monkeypatch.setattr(du, "_match_element", lambda *_a, **_k: FakeRoot())
    monkeypatch.setattr(du, "_patterns", lambda _el: [])
    monkeypatch.setattr(du, "_ensure_uia", lambda: (SimpleNamespace(), SimpleNamespace()))

    import argusai.core.workspace.desktop_control as dc

    monkeypatch.setattr(
        dc,
        "mouse_click",
        lambda sid, *, x, y, button="left": seen.update({"sid": sid, "x": x, "y": y, "button": button})
        or {"ok": True, "x": x, "y": y},
    )

    out = du.element_action(action="click", hwnd=1, runtime_id="1.9", session_id="s1")
    assert out.get("ok") is True, out
    assert seen["x"] == 140  # (100+180)//2 via left+width//2 = 100+40
    assert seen["y"] == 220
    assert "click" in (out.get("hint") or "").lower() or out.get("click")


def test_find_elements_on_foreground_when_windows() -> None:
    if not sys.platform.startswith("win"):
        pytest.skip("Windows only")
    avail = du.uia_available()
    if not avail.get("available"):
        pytest.skip(avail.get("error") or "UIA unavailable")
    import ctypes

    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if hwnd <= 0:
        pytest.skip("no foreground window")
    out = du.find_elements(hwnd=hwnd, limit=5)
    assert out.get("ok") is True, out
    assert "elements" in out


def test_assess_window_uia_shape() -> None:
    if not sys.platform.startswith("win"):
        pytest.skip("Windows only")
    avail = du.uia_available()
    if not avail.get("available"):
        pytest.skip(avail.get("error") or "UIA unavailable")
    import ctypes

    hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if hwnd <= 0:
        pytest.skip("no foreground window")
    out = du.assess_window_uia(hwnd)
    assert out.get("ok") is True, out
    assert out.get("uia_surface") in {"rich", "partial", "shallow"}
    assert "semantic_controls" in out


def test_empty_edit_find_attaches_surface_hints(monkeypatch) -> None:
    """Filtered empty finds must surface shallow-UIA warnings for the agent."""
    fake_assess = {
        "ok": True,
        "hwnd": 42,
        "uia_surface": "shallow",
        "descendant_estimate": 2,
        "semantic_controls": {"edit": 0, "listitem": 0, "text": 0, "button": 0, "document": 0},
        "named_sample": ["Weixin"],
        "chat_input_relative": {"x": 100, "y": 200},
        "avoid_left_fraction": 0.30,
        "warning": "shallow UIA warning",
        "root": {"name": "App", "control_type": "window", "rect": {"width": 200, "height": 300}},
    }

    class FakeArr:
        Length = 0

        def GetElement(self, _i):
            raise IndexError

    class FakeRoot:
        CurrentControlType = 50032
        CurrentName = "App"
        CurrentAutomationId = ""
        CurrentClassName = "Window"
        CurrentIsEnabled = True
        CurrentIsOffscreen = False
        CurrentBoundingRectangle = SimpleNamespace(left=0, top=0, right=200, bottom=300)

        def GetRuntimeId(self):
            return [1, 2]

        def GetCurrentPattern(self, _pid):
            return None

        def FindAll(self, _scope, _cond):
            return FakeArr()

    monkeypatch.setattr(du, "_root_element", lambda _hwnd=0: FakeRoot())
    monkeypatch.setattr(du, "assess_window_uia", lambda _hwnd=0: fake_assess)
    monkeypatch.setattr(du, "_patterns", lambda _el: [])
    monkeypatch.setattr(du, "_build_condition", lambda **_kw: (object(), ""))
    monkeypatch.setattr(
        du,
        "_ensure_uia",
        lambda: (SimpleNamespace(), SimpleNamespace(TreeScope_Descendants=4)),
    )

    out = du.find_elements(hwnd=42, control_type="edit", limit=5)
    assert out.get("ok") is True
    assert out.get("count") == 0
    assert out.get("uia_surface") == "shallow"
    assert out.get("warning") == "shallow UIA warning"
    assert out["window_action_hints"]["chat_input_relative"]["x"] == 100


def test_element_public_shapes_rect() -> None:
    class Rect:
        left, top, right, bottom = 10, 20, 110, 70

    el = SimpleNamespace(
        CurrentControlType=50004,
        CurrentName="Input",
        CurrentAutomationId="box",
        CurrentClassName="Edit",
        CurrentIsEnabled=True,
        CurrentIsOffscreen=False,
        CurrentBoundingRectangle=Rect(),
        GetRuntimeId=lambda: [42, 7],
        GetCurrentPattern=lambda _pid: None,
    )
    pub = du._element_public(el, index=0)
    assert pub["control_type"] == "edit"
    assert pub["runtime_id"] == "42.7"
    assert pub["rect"]["width"] == 100
    assert pub["rect"]["height"] == 50
