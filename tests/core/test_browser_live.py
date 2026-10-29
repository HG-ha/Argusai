"""Live-browser hand-off state machine + CDP input payload builders."""

from __future__ import annotations

import time

import pytest

from argusai.core.browser import browser_live as bl


class _Hub:
    """Stand-in for a BrowserHub (identity is all the lock cares about)."""


def _fresh() -> bl.LiveControl:
    return bl.LiveControl()


def test_attached_browser_is_watchable_even_when_not_in_the_hub_cache() -> None:
    from argusai.core.browser.browser_pool import (
        BrowserHub,
        set_attached_hub,
        watchable_browser_hubs,
    )
    import argusai.core.browser.browser_pool as pool

    attached = BrowserHub(attached_endpoint="http://127.0.0.1:9222")
    member = BrowserHub(profile_name="member-a")
    member._pages_served = 1
    previous_attached = pool._ATTACHED_HUB
    previous_hubs = dict(pool._HUBS)
    try:
        pool._HUBS.clear()
        pool._HUBS["member"] = member
        set_attached_hub(attached)
        watched = watchable_browser_hubs()
    finally:
        set_attached_hub(previous_attached)
        pool._HUBS.clear()
        pool._HUBS.update(previous_hubs)
    assert attached in watched
    assert member in watched


def test_default_agent_mode_blocks_user_input_but_not_agent() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    assert ctrl.snapshot()["mode"] == "agent"
    # Agent may serve pages; user does not hold control.
    assert not ctrl.blocks_agent(hub)
    assert not ctrl.user_holds(hub)


def test_agent_enter_exit_tracks_busy() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    ctrl.agent_enter(hub)
    assert ctrl.snapshot()["agent_busy"] is True
    ctrl.agent_exit(hub)
    # Still "busy" within the idle grace window right after exit.
    assert ctrl.snapshot()["agent_busy"] is True


def test_user_cannot_take_while_agent_busy_without_force() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    ctrl.agent_enter(hub)  # in-flight op
    ok, reason = ctrl.can_user_take(force=False)
    assert ok is False and reason == "agent_busy"
    took = ctrl.take(force=False)
    assert took["ok"] is False
    assert ctrl.snapshot()["mode"] == "agent"


def test_force_takeover_grabs_control_mid_operation() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    ctrl.agent_enter(hub)
    took = ctrl.take(force=True)
    assert took["ok"] is True
    assert ctrl.snapshot()["mode"] == "user"
    # Now the agent's next page acquisition must raise.
    assert ctrl.blocks_agent(hub) is True
    with pytest.raises(bl.BrowserControlHeld):
        ctrl.agent_enter(hub)


def test_user_can_take_after_agent_goes_idle() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    ctrl.agent_enter(hub)
    ctrl.agent_exit(hub)
    # Force the idle timestamp into the past so the grace window elapses.
    ctrl._last_agent_ts = time.monotonic() - (bl.IDLE_GRACE_SECONDS + 0.5)
    ok, reason = ctrl.can_user_take(force=False)
    assert ok is True and reason == "agent_idle"


def test_release_hands_back_to_agent() -> None:
    ctrl = _fresh()
    hub = _Hub()
    ctrl.bind(hub)
    ctrl.take(force=True)
    assert ctrl.snapshot()["mode"] == "user"
    ctrl.release()
    snap = ctrl.snapshot()
    assert snap["mode"] == "agent"
    assert ctrl.blocks_agent(hub) is False


def test_lock_is_scoped_to_bound_hub() -> None:
    ctrl = _fresh()
    hub_a, hub_b = _Hub(), _Hub()
    ctrl.bind(hub_a)
    ctrl.take(force=True)
    # Another hub the agent might use elsewhere is unaffected.
    assert ctrl.blocks_agent(hub_b) is False
    ctrl.agent_enter(hub_b)  # must not raise
    ctrl.agent_exit(hub_b)


def test_no_viewer_means_no_takeover() -> None:
    ctrl = _fresh()
    ok, reason = ctrl.can_user_take(force=False)
    assert ok is False and reason == "no_viewer"


def test_build_mouse_event_click_and_wheel() -> None:
    down = bl.build_mouse_event({"kind": "down", "x": 12.5, "y": 30, "button": 0, "buttons": 1, "clickCount": 1})
    assert down["type"] == "mousePressed"
    assert down["button"] == "left"
    assert down["x"] == 12.5 and down["y"] == 30.0
    assert down["clickCount"] == 1

    wheel = bl.build_mouse_event({"kind": "wheel", "x": 5, "y": 5, "deltaX": 0, "deltaY": -120})
    assert wheel["type"] == "mouseWheel"
    assert wheel["deltaY"] == -120.0
    assert "button" not in wheel

    move = bl.build_mouse_event({"kind": "move", "x": 1, "y": 2, "buttons": 0})
    assert move["type"] == "mouseMoved"
    assert move["button"] == "none"


def test_build_mouse_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        bl.build_mouse_event({"kind": "teleport", "x": 0, "y": 0})


def test_build_key_events_text_and_special() -> None:
    text = bl.build_key_events({"kind": "text", "text": "hi"})
    assert text == [{"_method": "Input.insertText", "text": "hi"}]

    key = bl.build_key_events({"kind": "key", "key": "Enter", "code": "Enter"})
    assert [e["type"] for e in key] == ["keyDown", "keyUp"]
    assert all(e["_method"] == "Input.dispatchKeyEvent" for e in key)

    down = bl.build_key_events({"kind": "keydown", "key": "Backspace", "code": "Backspace"})
    assert len(down) == 1 and down[0]["type"] == "keyDown"


def test_dom_button_mapping() -> None:
    assert bl.dom_button_name(0) == "left"
    assert bl.dom_button_name(1) == "middle"
    assert bl.dom_button_name(2) == "right"
    assert bl.dom_button_name("weird") == "left"
    assert bl.dom_buttons_mask(5) == 5
    assert bl.dom_buttons_mask(None) == 0
