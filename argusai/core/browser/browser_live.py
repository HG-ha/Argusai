"""Shared control-lock for the in-app "live browser" viewer.

The AI assistant drives a **hidden** (headless / off-screen) browser. To
let a human watch and optionally take over, the GUI streams a CDP
screencast of whatever page the agent is driving into a Vue panel and
forwards the human's clicks/keys back over CDP.

Two actors want to drive the same browser, so we need an explicit
hand-off state machine:

* ``agent`` (default) — the assistant's browser tools acquire pages and
  dispatch actions. The human view is a **read-only mirror**; viewer
  input is rejected.
* ``user`` — the human holds control. Viewer input is dispatched over
  CDP, and the assistant's browser tools **refuse to acquire a page**
  (they raise :class:`BrowserControlHeld`) so the two never fight over
  the same tab.

Normally the human may only ``take`` control once the agent is idle
(no in-flight browser op for ``IDLE_GRACE_SECONDS``). ``take(force=True)``
is the "强制接管" button: it grabs control even mid-operation, and the
agent's current browser call errors out cleanly.

This module is intentionally free of Playwright / CDP imports so the
state machine can be unit-tested without a real browser. The actual
screencast + ``Input.dispatch*`` wiring lives on
:class:`argusai.core.browser.browser_pool.BrowserHub`.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# How long after the agent's last browser op we still consider it "busy"
# so a non-forced user take-over waits for a real idle window.
IDLE_GRACE_SECONDS = 1.5

# CDP mouse button names keyed by the DOM ``button`` integer.
_DOM_BUTTONS = {0: "left", 1: "middle", 2: "right"}


class BrowserControlHeld(RuntimeError):
    """Raised inside the hub loop when the human holds browser control.

    Surfaced to the assistant as a tool error so the model knows to stop
    driving and wait for the user to hand control back.
    """


class LiveControl:
    """Process-wide hand-off state for the single live viewer.

    There is at most one viewer open at a time, so a module-level
    singleton (``CONTROL``) is enough. The state is scoped to a specific
    hub object so a lock on the viewed browser never blocks an unrelated
    hub the agent might be using elsewhere.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hub: Any = None
        self._mode = "agent"  # "agent" | "user"
        self._inflight = 0
        self._last_agent_ts = 0.0
        self._screencast_on = False

    # ---- hub binding ------------------------------------------------

    def bind(self, hub: Any) -> None:
        """Point the viewer at ``hub`` (the browser the human is watching)."""
        with self._lock:
            if self._hub is hub:
                return
            self._hub = hub
            self._mode = "agent"
            self._inflight = 0
            self._last_agent_ts = 0.0
            self._screencast_on = False

    def unbind(self, hub: Any = None) -> None:
        with self._lock:
            if hub is not None and self._hub is not hub:
                return
            self._hub = None
            self._mode = "agent"
            self._inflight = 0
            self._screencast_on = False

    def set_screencast(self, on: bool) -> None:
        with self._lock:
            self._screencast_on = bool(on)

    # ---- agent side (called from the hub loop) ----------------------

    def blocks_agent(self, hub: Any) -> bool:
        """True when ``hub`` is the viewed browser and the human holds it."""
        with self._lock:
            return self._hub is hub and self._mode == "user"

    def agent_enter(self, hub: Any) -> None:
        """Mark the start of a browser op. Raises if the human holds control."""
        with self._lock:
            if self._hub is hub and self._mode == "user":
                raise BrowserControlHeld(
                    "用户已通过实时浏览器视图接管了浏览器控制权。"
                    "请停止浏览器操作并提示用户完成后点击“交还给 Agent”；"
                    "当用户表示已完成或让你继续时，可调用 browser_resume_control 主动取回控制权再继续。"
                )
            if self._hub is hub:
                self._inflight += 1
                self._last_agent_ts = time.monotonic()

    def agent_exit(self, hub: Any) -> None:
        with self._lock:
            if self._hub is hub and self._inflight > 0:
                self._inflight -= 1
                self._last_agent_ts = time.monotonic()

    def _agent_busy_locked(self) -> bool:
        if self._inflight > 0:
            return True
        if self._last_agent_ts <= 0.0:
            return False
        return (time.monotonic() - self._last_agent_ts) < IDLE_GRACE_SECONDS

    # ---- user side (called from the GUI bridge) ---------------------

    def can_user_take(self, force: bool = False) -> tuple[bool, str]:
        with self._lock:
            if self._hub is None:
                return False, "no_viewer"
            if self._mode == "user":
                return True, "already_user"
            if force:
                return True, "forced"
            if self._agent_busy_locked():
                return False, "agent_busy"
            return True, "agent_idle"

    def take(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            ok, reason = self.can_user_take(force)
            if ok:
                self._mode = "user"
                if force and self._inflight > 0:
                    # The agent's in-flight op will raise on its next page
                    # acquisition; drop the counter so idle math is sane.
                    self._inflight = 0
            return {"ok": ok, "reason": reason, **self._snapshot_locked()}

    def release(self) -> dict[str, Any]:
        with self._lock:
            self._mode = "agent"
            self._last_agent_ts = 0.0
            return {"ok": True, **self._snapshot_locked()}

    def user_holds(self, hub: Any) -> bool:
        with self._lock:
            return self._hub is hub and self._mode == "user"

    # ---- snapshot ---------------------------------------------------

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "bound": self._hub is not None,
            "mode": self._mode,
            "agent_busy": self._agent_busy_locked(),
            "screencast_on": self._screencast_on,
            "can_take": self._hub is not None and self._mode != "user" and not self._agent_busy_locked(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


# Process-wide singleton used by both the hub loop and the GUI bridge.
CONTROL = LiveControl()


def dom_button_name(button: Any) -> str:
    """Map a DOM ``MouseEvent.button`` integer to a CDP button name."""
    try:
        return _DOM_BUTTONS.get(int(button), "left")
    except (TypeError, ValueError):
        return "left"


def dom_buttons_mask(buttons: Any) -> int:
    """Pass through the DOM ``MouseEvent.buttons`` bitmask (CDP uses the same)."""
    try:
        return max(0, int(buttons))
    except (TypeError, ValueError):
        return 0


def build_mouse_event(event: dict[str, Any]) -> dict[str, Any]:
    """Translate a viewer mouse event into ``Input.dispatchMouseEvent`` params.

    ``event`` carries page-CSS-pixel ``x`` / ``y`` (already scaled by the
    frontend from the rendered frame) plus the DOM action fields.
    """
    kind = str(event.get("kind") or "").strip()
    cdp_type = {
        "down": "mousePressed",
        "up": "mouseReleased",
        "move": "mouseMoved",
        "wheel": "mouseWheel",
    }.get(kind)
    if not cdp_type:
        raise ValueError(f"unsupported mouse kind: {kind!r}")
    params: dict[str, Any] = {
        "type": cdp_type,
        "x": float(event.get("x") or 0.0),
        "y": float(event.get("y") or 0.0),
        "buttons": dom_buttons_mask(event.get("buttons")),
    }
    if cdp_type == "mouseWheel":
        params["deltaX"] = float(event.get("deltaX") or 0.0)
        params["deltaY"] = float(event.get("deltaY") or 0.0)
    else:
        params["button"] = dom_button_name(event.get("button")) if kind != "move" else "none"
        params["clickCount"] = int(event.get("clickCount") or (1 if kind in ("down", "up") else 0))
    if event.get("modifiers") is not None:
        params["modifiers"] = dom_buttons_mask(event.get("modifiers"))
    return params


def build_key_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a viewer key/text event into CDP ``Input`` calls.

    Returns a list because ``insertText`` is one call while a raw key is a
    keyDown/keyUp pair.
    """
    kind = str(event.get("kind") or "").strip()
    if kind == "text":
        text = str(event.get("text") or "")
        if not text:
            return []
        return [{"_method": "Input.insertText", "text": text}]
    if kind in ("keydown", "keyup", "key"):
        key = str(event.get("key") or "")
        code = str(event.get("code") or "")
        modifiers = dom_buttons_mask(event.get("modifiers"))
        base = {"key": key, "code": code, "modifiers": modifiers}
        if kind == "keydown":
            return [{"_method": "Input.dispatchKeyEvent", "type": "keyDown", **base}]
        if kind == "keyup":
            return [{"_method": "Input.dispatchKeyEvent", "type": "keyUp", **base}]
        return [
            {"_method": "Input.dispatchKeyEvent", "type": "keyDown", **base},
            {"_method": "Input.dispatchKeyEvent", "type": "keyUp", **base},
        ]
    raise ValueError(f"unsupported key kind: {kind!r}")
