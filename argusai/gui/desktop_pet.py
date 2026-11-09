"""Optional desktop companion. Reads run projections; never controls approvals."""
from __future__ import annotations

import json
import sys
import threading
import time
import math
import os
from functools import lru_cache
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Any

ASSET_DIR = Path(__file__).with_name('pet_assets')
WINDOW_SIZE = (240, 260)
PET_SCALE = 0.7
STATES = frozenset({'idle', 'thinking', 'working', 'wave', 'complete', 'sleep', 'alert'})


def pet_dimensions(area, scale=1.0):
    """Logical points, not backing pixels: keep smaller laptop screens compact."""
    factor = min(1.0, max(2/3, min(area[2]/1920, area[3]/1080)))
    factor *= PET_SCALE  # Ignore legacy per-user size preferences.
    factor = min(factor, area[2]*.4/WINDOW_SIZE[0], area[3]*.4/WINDOW_SIZE[1])
    return round(WINDOW_SIZE[0]*factor), round(WINDOW_SIZE[1]*factor)


def prepare_pet_display_backend():
    # Wayland forbids global toplevel placement. Prefer XWayland when present;
    # respect an explicitly selected backend instead of silently overriding it.
    if sys.platform.startswith('linux') and os.environ.get('WAYLAND_DISPLAY') and os.environ.get('DISPLAY'):
        os.environ.setdefault('GDK_BACKEND', 'x11')
        os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')


def configure_pet_window(window):
    window.evaluate_js = window.run_js
    if sys.platform == 'win32':
        # Apply before first Show(), so auxiliary windows never get a taskbar tab.
        def prepare_tool_window():
            window.native.ShowInTaskbar = False
        window.events.before_show += prepare_tool_window
    if sys.platform.startswith('linux') and getattr(window.gui, '__name__', '').endswith('gtk'):
        # The pinned GTK backend adds monitor-0's origin to move(). Our shared
        # monitor geometry is already global, including negative coordinates.
        original_move = window.move
        def move_global(x, y):
            instance = window.gui.BrowserView.instances.get(window.uid)
            origin = instance.screen if instance is not None else None
            original_move(x-(origin.x if origin else 0), y-(origin.y if origin else 0))
        window.move = move_global
        if hasattr(window, 'resize'):
            original_resize = window.resize
            def resize_fixed(width, height, *args, **kwargs):
                from gi.repository import GLib
                # GTK gives non-resizable windows a fixed initial size request.
                # Update that request before programmatic companion/menu resize.
                GLib.idle_add(lambda: window.native.set_size_request(width, height))
                original_resize(width, height, *args, **kwargs)
            window.resize = resize_fixed


@lru_cache(maxsize=1)
def _bounds_manifest():
    return json.loads((ASSET_DIR / 'bounds.json').read_text(encoding='utf-8'))


def body_bounds(state='idle', size=240, mirrored=False):
    values = _bounds_manifest().get(state, _bounds_manifest()['idle'])
    left, top, right, bottom = (math.floor(values[0]*size), math.floor(values[1]*size),
                               math.ceil(values[2]*size), math.ceil(values[3]*size))
    return (size-right, top, size-left, bottom) if mirrored else (left, top, right, bottom)


@lru_cache(maxsize=32)
def frame_timeline(durations):
    return tuple(accumulate(durations))


def animation_frame(elapsed, durations):
    timeline = frame_timeline(tuple(durations))
    return min(len(timeline)-1, bisect_right(timeline, max(0, elapsed)*1000 % timeline[-1]))


def contain_body(x, y, bounds, area, margin=6):
    left, top, right, bottom = bounds
    ax, ay, aw, ah = area
    return (round(max(ax+margin-left, min(ax+aw-margin-right, x))),
            round(max(ay+margin-top, min(ay+ah-margin-bottom, y))))


def body_dock(x, y, bounds, area, threshold=84, side=None):
    left, top, right, bottom = bounds
    ax, ay, aw, ah = area
    cx, cy = contain_body(x, y, bounds, area)
    edges = [('left', ax+6-left, cy), ('right', ax+aw-6-right, cy),
             ('top', cx, ay+6-top), ('bottom', cx, ay+ah-6-bottom)]
    candidate = next((e for e in edges if e[0] == side), None)
    if candidate is None:
        candidate = min(edges, key=lambda e: abs(e[1]-x)+abs(e[2]-y))
    return candidate if side or abs(candidate[1]-x)+abs(candidate[2]-y) <= threshold else None


def nearest_area(x, y, areas):
    return min(areas, key=lambda r: max(r[0]-x, 0, x-(r[0]+r[2]))**2 +
               max(r[1]-y, 0, y-(r[1]+r[3]))**2)


def pet_work_areas():
    """Use every monitor's work area in the backend's top-left coordinates."""
    try:
        if sys.platform == 'darwin':
            import AppKit
            screens = list(AppKit.NSScreen.screens())
            top = screens[0].frame().origin.y + screens[0].frame().size.height
            return [(round(s.visibleFrame().origin.x),
                     round(top-s.visibleFrame().origin.y-s.visibleFrame().size.height),
                     round(s.visibleFrame().size.width), round(s.visibleFrame().size.height)) for s in screens]
        if sys.platform.startswith('linux'):
            import webview
            if getattr(webview.guilib, '__name__', '').endswith('gtk'):
                from gi.repository import Gdk
                display = Gdk.Display.get_default()
                rectangles = [display.get_monitor(i).get_workarea() for i in range(display.get_n_monitors())]
                return [(r.x, r.y, r.width, r.height) for r in rectangles]
            from qtpy.QtWidgets import QApplication
            rectangles = [s.availableGeometry() for s in QApplication.screens()]
            return [(r.x(), r.y(), r.width(), r.height()) for r in rectangles]
    except Exception:
        pass
    return [pet_work_area()]


def drag_direction(dx, dy, previous='up'):
    """Keep the last meaningful movement direction while the pointer pauses."""
    if abs(dx) + abs(dy) < 3:
        return previous
    if abs(dx) >= abs(dy):
        return 'right' if dx > 0 else 'left'
    return 'down' if dy > 0 else 'up'


def contain_position(x, y, width, height, area):
    ax, ay, aw, ah = area
    return (max(ax, min(ax + max(0, aw - width), round(x))),
            max(ay, min(ay + max(0, ah - height), round(y))))


def pet_work_area(window=None):
    """Return logical coordinates; WinForms screen rectangles are physical."""
    from .window_geometry import get_work_area
    area = get_work_area()
    if sys.platform == 'win32':
        import ctypes
        scale = ctypes.windll.user32.GetDpiForSystem() / 96 or 1
        native = getattr(window, 'native', None)
        if native is not None:
            from System.Windows.Forms import Screen
            rect = Screen.FromHandle(native.Handle).WorkingArea
            area = (rect.X, rect.Y, rect.Width, rect.Height)
            scale = native._scale or 1
        return tuple(round(v / scale) for v in area)
    return area


def snap_position(x, y, width, height, area, threshold=56):
    """Return the closest nearby work-area edge; never hide the character."""
    ax, ay, aw, ah = area
    right, bottom = ax + max(0, aw - width), ay + max(0, ah - height)
    edges = [(abs(x - ax), ax, min(bottom, max(ay, y))),
             (abs(x - right), right, min(bottom, max(ay, y))),
             (abs(y - ay), min(right, max(ax, x)), ay),
             (abs(y - bottom), min(right, max(ax, x)), bottom)]
    distance, tx, ty = min(edges, key=lambda item: item[0])
    return (tx, ty) if distance <= threshold else None


def read_activity(api: Any) -> tuple[list[dict], str]:
    """Copy the existing in-memory projections without consuming stream events."""
    pending = ''
    for sid, session in list(getattr(api, '_assistant_sessions', {}).items()):
        if getattr(session, '_pending', None) or getattr(session, '_user_decision_requested', False):
            pending = str(sid)
            break
    hub = getattr(api, '_assistant_stream_hub', None)
    if hub is None:
        return [], pending
    with hub._records_lock:
        records = list(hub.records.items())
    snapshots = []
    for rid, record in records:
        with record['lock']:
            events = record.get('events', [])[-80:]
            tool = next((e for e in reversed(events) if e.get('kind') == 'tool_activity'), {})
            terminal = next((e for e in reversed(events) if e.get('kind') in {'run_done', 'run_error'}), {})
            result = terminal.get('result') or {}
            stage = next((e for e in reversed(events) if e.get('kind') == 'stage'), {})
            snapshots.append({
                'run_id': rid, 'session_id': record.get('session_id', ''),
                'created_at': record.get('created_at', 0), 'done': bool(record.get('done')),
                'working': tool.get('data', {}).get('status') == 'start',
                'failed': terminal.get('kind') == 'run_error' or result.get('ok') is False,
                'cancelled': result.get('status') in {'cancelled', 'interrupted'},
                'activity': str(tool.get('data', {}).get('tool') or stage.get('data', {}).get('label') or '')[:160],
            })
    return snapshots, pending


class PetActivity:
    """Small read-only view model; transitions use monotonic time."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.started = False
        self.last_active = time.monotonic()
        self.notice_until = 0.0
        self.notice = 'idle'
        self.session_id = ''

    def update(self, runs: list[dict], pending: str = '', *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        if not self.started:
            self.seen.update(r['run_id'] for r in runs if r.get('done'))
            self.last_active = now
            self.started = True
        for run in sorted(runs, key=lambda r: r.get('created_at', 0)):
            if run.get('done') and run['run_id'] not in self.seen:
                self.notice = 'idle' if run.get('cancelled') else ('alert' if run.get('failed') else 'complete')
                self.notice_until = now + 6
                self.last_active = now
                self.session_id = run.get('session_id', '')
                self.seen.add(run['run_id'])
        # Bound memory even when the companion runs for days.
        if len(self.seen) > 512:
            self.seen.intersection_update(r['run_id'] for r in runs)
        active = [r for r in runs if not r.get('done')]
        if pending:
            self.session_id = pending
            self.last_active = now
            state = 'wave'
        elif active:
            run = max(active, key=lambda r: r.get('created_at', 0))
            self.session_id = run.get('session_id', '')
            self.last_active = now
            state = 'working' if run.get('working') else 'thinking'
        elif now < self.notice_until:
            state = self.notice
        else:
            state = 'sleep' if now - self.last_active >= 180 else 'idle'
        return {'state': state, 'session_id': self.session_id}


class _PetBridge:
    """Only companion-specific methods are exposed in the secondary WebView."""

    def __init__(self, controller: DesktopPet) -> None:
        self._controller = controller

    def status(self) -> dict:
        c = self._controller
        runs, pending = read_activity(c.api)
        with c.activity_lock:
            state = c.activity.update(runs, pending)
            if time.monotonic() < c.interaction_until and not pending:
                state = {**state, 'state': 'wave'}
        active = next((r for r in runs if r['session_id'] == state['session_id'] and not r['done']), {})
        animation = ('drag-' + getattr(c, 'drag_direction', 'up')) if c.dragging else 'release' if time.monotonic() < c.release_until else state['state']
        c.animation_state = animation
        if sys.platform != 'win32' and c.window is not None:
            x, y = int(c.window.x), int(c.window.y)
            bounds = body_bounds(animation, c.pet_width, c.dock_side == 'right' and not c.dragging)
            area = nearest_area(x+(bounds[0]+bounds[2])/2, y+(bounds[1]+bounds[3])/2, pet_work_areas())
            size = pet_dimensions(area, getattr(c.api.settings_mgr.get(), 'gui_pet_scale', 1))
            if size != (c.pet_width, c.pet_height):
                c.pet_width, c.pet_height = size
                c.window.resize(*size)
                bounds = body_bounds(animation, c.pet_width, c.dock_side == 'right' and not c.dragging)
            target = contain_body(x, y, bounds, area)
            if c.dock_side and not c.dragging:
                _, tx, ty = body_dock(x, y, bounds, area, side=c.dock_side)
                target = (tx, ty)
            if target != (x, y):
                c.window.move(*target)
        return {**state, 'animation': animation, 'dragging': c.dragging,
                'mirrored': c.dock_side == 'right' and not c.dragging,
                'task': c.task_names.get(state['session_id'], ''),
                'activity': active.get('activity', ''), 'locale': getattr(c.api, '_locale', 'en')}

    def interact(self) -> dict:
        c = self._controller
        c.interaction_until = time.monotonic() + 4
        if c.chat_visible:
            self.chat_hide()
        else:
            c.show_chat(toolbar=True)
        return {'ok': True}

    def chat_open(self) -> dict:
        self._controller.show_chat()
        return {'ok': True}

    def chat_snapshot(self, session_id: str = '') -> dict:
        from .desktop_pet_chat import snapshot
        return {**snapshot(self._controller, session_id), 'mode': self._controller.chat_mode,
                'locale': getattr(self._controller.api, '_locale', 'zh')}

    def menu(self) -> dict:
        self._controller.show_chat(menu=True)
        return {'ok': True}

    def dismiss_actions(self) -> dict:
        # A delayed blur from the icon strip must not close a newly opened chat.
        if self._controller.chat_mode != 'chat':
            return self.chat_hide()
        return {'ok': True}

    def chat_hide(self) -> dict:
        c = self._controller
        c.chat_visible = False
        if c.chat_window is not None:
            c.chat_window.run_js('window.petChatVisibility && window.petChatVisibility(false)')
            c.chat_window.hide()
        return {'ok': True}

    def chat_layout(self, height: int = 52, width: int = 280) -> dict:
        c = self._controller
        c.chat_height = min(420, max(44, int(height)))
        c.chat_width = min(420, max(132, int(width)))
        if c.chat_window is not None:
            c.chat_window.resize(c.chat_width, c.chat_height)
            c.position_chat()
        return {'ok': True}

    def chat_new(self) -> dict:
        import uuid
        sid = 'pet-' + uuid.uuid4().hex
        self._controller.chat_session_id = sid
        self._controller.chat_session_ids.add(sid)
        self._controller.chat_created_ids.add(sid)
        return {'ok': True, 'session_id': sid}

    def chat_send(self, session_id: str, message: str) -> dict:
        from .desktop_pet_chat import send
        return send(self._controller, session_id, message)

    def chat_events(self, session_id: str, run_id: str, offset: int = 0) -> dict:
        from .desktop_pet_chat import events
        return events(self._controller, session_id, run_id, offset)

    def open_assistant(self) -> dict:
        c = self._controller
        result = c.api.show_window_from_tray()
        sid = c.chat_session_id or c.activity.session_id
        if sid not in c.chat_session_ids and sid not in (getattr(c.api, "_app_attention_state", {}).get("open_session_ids") or []):
            sid = ""
        detail = json.dumps({'sessionId': sid}, ensure_ascii=False)
        event = 'argusai:open-assistant-session' if sid else 'argusai:open-assistant'
        if result.get('ok'):
            c.api._window.evaluate_js(f'window.dispatchEvent(new CustomEvent({json.dumps(event)}, {{detail:{detail}}}))')
        return result

    def move(self, dx: int, dy: int) -> None:
        c = self._controller
        c.drag_direction = drag_direction(dx, dy, getattr(c, 'drag_direction', 'up'))
        if not c.dragging and c.chat_visible:
            self.chat_hide()
        c.dragging = True
        c.dock_side = None
        c.snap_generation += 1
        if c.window is not None:
            x, y = int(c.window.x) + max(-2000, min(2000, int(dx))), int(c.window.y) + max(-2000, min(2000, int(dy)))
            bounds = body_bounds('drag-'+c.drag_direction, c.pet_width)
            c.work_area = nearest_area(x+(bounds[0]+bounds[2])/2, y+(bounds[1]+bounds[3])/2, pet_work_areas())
            position = contain_body(x, y, bounds, c.work_area)
            c.window.move(*position)

    def save_position(self) -> None:
        c = self._controller
        if c.window is None:
            return
        x, y = int(c.window.x), int(c.window.y)
        def save(settings):
            settings.gui_pet_x, settings.gui_pet_y = x, y
        c.api.settings_mgr.update(save)
        c.position_chat()

    def hide(self) -> dict:
        return self._controller.api.set_desktop_pet_enabled(False)

    def finish_drag(self) -> None:
        self._controller.dragging = False
        self._controller.release_until = time.monotonic() + .45
        self.save_position()
        self._controller.snap_to_edge()

    def snap_enabled(self, enabled: bool) -> dict:
        return self._controller.api.set_desktop_pet_snap(bool(enabled))

    def reset_position(self) -> dict:
        self._controller.reset_position()
        return {'ok': True}


class DesktopPet:
    def __init__(self, api: Any) -> None:
        self.api = api
        self.window = None
        self.lock = threading.RLock()
        self.activity_lock = threading.RLock()
        self.activity = PetActivity()
        self._closing = False
        self._native_animation = None
        self.interaction_until = 0.0
        self.chat_window = None
        self.chat_visible = False
        self.chat_height = 64
        self.chat_width = 280
        self.chat_mode = 'chat'
        self.dock_side = None
        self.work_area = None
        self.animation_state = 'idle'
        self.pet_width, self.pet_height = WINDOW_SIZE
        self.chat_session_id = ''
        self.chat_session_ids: set[str] = set()
        self.chat_created_ids: set[str] = set()
        self.snap_generation = 0
        self.dragging = False
        self.drag_direction = 'up'
        self.release_until = 0.0
        self.task_names = {}

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            if not enabled:
                if self.window is not None:
                    self.window.run_js("window.petVisibility && window.petVisibility(false)")
                    self.window.hide()
                if self.chat_window is not None:
                    self.chat_window.run_js('window.petChatVisibility && window.petChatVisibility(false)')
                    self.chat_window.hide()
                return
            if sys.platform.startswith('linux'):
                import webview
                if getattr(webview.guilib, '__name__', '').endswith('gtk'):
                    from gi.repository import Gdk
                    if 'Wayland' in type(Gdk.Display.get_default()).__name__:
                        raise RuntimeError('Wayland 不支持桌宠绝对定位；请使用 GDK_BACKEND=x11 argusai --gui 启动后启用桌宠。')
            if self.window is not None:
                self.window.show()
                self.window.run_js("window.petVisibility && window.petVisibility(true)")
                return
            import webview
            from .window_geometry import get_work_area
            settings = self.api.settings_mgr.get()
            ax, ay, aw, ah = pet_work_area()
            width, height = pet_dimensions((ax, ay, aw, ah), getattr(settings, 'gui_pet_scale', 1))
            self.pet_width, self.pet_height = width, height
            x = settings.gui_pet_x if settings.gui_pet_x is not None else ax + aw - width
            y = settings.gui_pet_y if settings.gui_pet_y is not None else ay + ah - height
            x = max(ax, min(ax + max(0, aw - width), int(x)))
            y = max(ay, min(ay + max(0, ah - height), int(y)))
            if not (ASSET_DIR / 'idle.webp').is_file():
                raise FileNotFoundError('Desktop companion assets are missing')
            self.window = webview.create_window(
                'ArgusAI companion', url=(ASSET_DIR / 'index.html').resolve().as_uri(),
                js_api=_PetBridge(self), width=width, height=height, x=x, y=y,
                min_size=(64, 64), frameless=True, transparent=True, on_top=True,
                resizable=False, easy_drag=False, shadow=False, focus=False,
                background_color='#000000',
            )
            # The companion needs only fire-and-forget bridge callbacks.
            # pywebview's default evaluate_js wraps callbacks in eval, which
            # WebKit correctly rejects under these pages' strict CSP.
            configure_pet_window(self.window)

            self.window.events.closing += self._on_closing
            self.window.events.loaded += self._prepare_native_window

    def _prepare_native_window(self) -> None:
        if sys.platform != 'win32' or self.window is None:
            return
        from .tray import run_on_winforms_ui
        from .desktop_pet_windows import WindowsPetAnimation
        def prepare():
            if self.window is not None:
                self._native_animation = WindowsPetAnimation(self)
        run_on_winforms_ui(prepare)

    def show_chat(self, menu=False, toolbar=False) -> None:
        from .window_geometry import get_work_area
        import webview
        with self.lock:
            self.chat_mode = 'toolbar' if toolbar else 'menu' if menu else 'chat'
            self.chat_width, self.chat_height = (132, 44) if toolbar else (196, 210) if menu else (280, 52)
            if self.chat_window is not None:
                self.chat_window.resize(self.chat_width, self.chat_height)
                self.chat_visible = True
                self.position_chat()
                self.chat_window.show()
                self.chat_window.run_js('window.petChatVisibility && window.petChatVisibility(true)')
                return
            ax, ay, aw, ah = pet_work_area(self.window)
            width, height = self.chat_width, self.chat_height
            pet_x = int(self.window.x) if self.window is not None else ax + aw
            pet_y = int(self.window.y) if self.window is not None else ay + ah
            x = pet_x - width - 12 if pet_x - width >= ax else pet_x + WINDOW_SIZE[0] + 12
            x = max(ax, min(ax + max(0, aw - width), x))
            y = max(ay, min(ay + max(0, ah - height), pet_y + WINDOW_SIZE[1] - height))
            self.chat_window = webview.create_window(
                'ArgusAI · 桌宠气泡', url=(ASSET_DIR / 'chat.html').resolve().as_uri(),
                js_api=_PetBridge(self), width=width, height=height, x=x, y=y,
                min_size=(132, 44), on_top=True, frameless=True, resizable=False,
                easy_drag=False, background_color='#191b20', hidden=True,
            )
            configure_pet_window(self.chat_window)
            self.chat_visible = True
            def reveal_chat():
                def reveal():
                    if self.chat_window is None or self._closing:
                        return
                    if sys.platform == 'win32':
                        self.chat_window.native.ShowInTaskbar = False
                    self.position_chat()
                    if self.chat_visible:
                        self.chat_window.show()
                if sys.platform == 'win32':
                    from .tray import run_on_winforms_ui
                    run_on_winforms_ui(reveal)
                else:
                    reveal()
            self.chat_window.events.loaded += reveal_chat
            if self.chat_window.events.loaded.is_set():
                reveal_chat()
            self.chat_window.events.closing += self._on_chat_closing

    def _on_chat_closing(self) -> bool:
        # ``destroy`` is dispatched asynchronously by Cocoa. The controller
        # clears ``chat_window`` before scheduling it, so a later native close
        # callback must be allowed through. Otherwise this auxiliary window
        # vetoes the whole application Quit.
        if (
            self._closing
            or self.chat_window is None
            or getattr(self.api, '_quit_requested', False)
        ):
            return True

        def hide_chat():
            self.chat_visible = False
            if self.chat_window is not None:
                self.chat_window.run_js('window.petChatVisibility && window.petChatVisibility(false)')
                self.chat_window.hide()

        threading.Thread(target=hide_chat, daemon=True).start()
        return False

    def position_chat(self) -> None:
        if self.chat_window is None or self.window is None:
            return
        if self._native_animation is not None:
            self._native_animation.position_chat()
            return
        from .window_geometry import get_work_area
        bounds = body_bounds(self.animation_state, self.pet_width, self.dock_side == 'right' and not self.dragging)
        ax, ay, aw, ah = nearest_area(int(self.window.x)+(bounds[0]+bounds[2])/2,
                                     int(self.window.y)+(bounds[1]+bounds[3])/2, pet_work_areas())
        px, py = int(self.window.x), int(self.window.y)
        width, height = self.chat_width, self.chat_height
        x = px+bounds[0]-width-8 if px+bounds[0]-width >= ax else px+bounds[2]+8
        y = py + bounds[3] - height
        if self.chat_mode == 'toolbar':
            x = px + (bounds[0] + bounds[2] - width) / 2
            y = py + bounds[3] + 6
            if y + height > ay + ah:
                y = py + bounds[1] - height - 6
        self.chat_window.move(round(max(ax, min(ax + max(0, aw - width), x))),
                              round(max(ay, min(ay + max(0, ah - height), y))))

    def _on_closing(self) -> bool:
        # On Cocoa, ``Window.destroy`` schedules ``NSWindow.close`` for the
        # next main-loop turn. ``close`` has already cleared ``self.window``
        # by then, which identifies this as programmatic teardown. Let it
        # close instead of vetoing application termination and merely hiding
        # the companion.
        if (
            self._closing
            or self.window is None
            or getattr(self.api, '_quit_requested', False)
        ):
            return True
        threading.Thread(target=lambda: self.api.set_desktop_pet_enabled(False), daemon=True).start()
        return False

    def snap_to_edge(self, area=None) -> None:
        if self.window is None or not self.api.settings_mgr.get().gui_pet_snap:
            return
        if self._native_animation is not None:
            self._native_animation.snap_to_edge()
            return
        from .window_geometry import get_work_area
        x, y = int(self.window.x), int(self.window.y)
        dock = body_dock(x, y, body_bounds(self.animation_state, self.pet_width, self.dock_side == 'right' and not self.dragging), area or self.work_area or pet_work_area(self.window))
        if dock is None:
            return
        self.dock_side, tx, ty = dock
        target = (tx, ty)
        self.snap_generation += 1
        generation = self.snap_generation
        def animate():
            started = time.monotonic()
            while self.window is not None and generation == self.snap_generation:
                progress = min(1, (time.monotonic() - started) / .28)
                eased = 1 - (1 - progress) ** 3
                self.window.move(round(x + (target[0] - x) * eased), round(y + (target[1] - y) * eased))
                if progress >= 1:
                    _PetBridge(self).save_position()
                    return
                time.sleep(1 / 60)
        threading.Thread(target=animate, daemon=True).start()

    def reset_position(self) -> None:
        self.snap_generation += 1
        self.dragging = False
        if self._native_animation is not None:
            self._native_animation.reset_position()
        elif self.window is not None:
            ax, ay, aw, ah = pet_work_area(self.window)
            self.window.move(*contain_position(ax + aw - WINDOW_SIZE[0] - 24,
                                               ay + ah - WINDOW_SIZE[1] - 24,
                                               *WINDOW_SIZE, (ax, ay, aw, ah)))
            _PetBridge(self).save_position()

    def close(self) -> None:
        with self.lock:
            self.snap_generation += 1
            # Terminal lifecycle latch. Cocoa dispatches ``destroy`` to the
            # main loop and may ask every auxiliary window for permission
            # only after this method returns. Resetting the flag here lets a
            # pet/chat window veto the parent application's Quit.
            self._closing = True
            if self.chat_window is not None:
                chat, self.chat_window = self.chat_window, None
                chat.destroy()
            if self.window is not None:
                if self._native_animation is not None:
                    from .tray import run_on_winforms_ui
                    animation, self._native_animation = self._native_animation, None
                    run_on_winforms_ui(animation.close)
                window, self.window = self.window, None
                window.destroy()
