"""Per-pixel alpha animation for the Windows desktop companion.

WebView2 in WinForms cannot reliably composite a transparent top-level window.
Keep the existing host/bridge but paint its companion with UpdateLayeredWindow.
"""
from __future__ import annotations

import ctypes as ct
import time
from ctypes import wintypes as wt

from PIL import Image, ImageChops


class WindowsPetAnimation:
    def __init__(self, controller):
        from System.Windows.Forms import Timer, MouseButtons
        self.controller = controller
        self.form = controller.window.native
        self.form.webview.Visible = False
        self.form.ShowInTaskbar = False
        self.closed = False
        self.state = ''
        self.image = None
        self.frame = 0
        self.next_frame = 0
        self.next_status = 0
        self.drag = None
        self.drag_pointer = None
        self.drag_intro = False
        self.snap_timer = None
        self.work_area = None
        self.clip_started = 0
        self.user = ct.windll.user32
        self.gdi = ct.windll.gdi32
        self.hwnd = self.form.Handle.ToInt64()
        from .desktop_pet import WINDOW_SIZE
        self.width, self.height = WINDOW_SIZE
        self.label = '点击小龙，开始聊天'

        class BitmapInfo(ct.Structure):
            _fields_ = [('size', wt.DWORD), ('width', wt.LONG), ('height', wt.LONG),
                       ('planes', wt.WORD), ('bits', wt.WORD), ('compression', wt.DWORD),
                       ('image_size', wt.DWORD), ('xppm', wt.LONG), ('yppm', wt.LONG),
                       ('used', wt.DWORD), ('important', wt.DWORD)]
        class Blend(ct.Structure):
            _fields_ = [('operation', ct.c_ubyte), ('flags', ct.c_ubyte),
                       ('alpha', ct.c_ubyte), ('format', ct.c_ubyte)]
        self.blend = Blend(0, 0, 255, 1)
        self.bitmap_info = BitmapInfo
        self.user.GetDC.argtypes = [wt.HWND]
        self.user.GetDC.restype = wt.HDC
        self.user.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        self.gdi.CreateCompatibleDC.argtypes = [wt.HDC]
        self.gdi.CreateCompatibleDC.restype = wt.HDC
        self.gdi.CreateDIBSection.argtypes = [wt.HDC, ct.c_void_p, wt.UINT, ct.c_void_p, wt.HANDLE, wt.DWORD]
        self.gdi.CreateDIBSection.restype = wt.HBITMAP
        self.gdi.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        self.gdi.SelectObject.restype = wt.HGDIOBJ
        self.gdi.DeleteObject.argtypes = [wt.HGDIOBJ]
        self.gdi.DeleteDC.argtypes = [wt.HDC]
        self.user.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ct.c_void_p, ct.c_void_p,
                                                  wt.HDC, ct.c_void_p, wt.DWORD, ct.c_void_p, wt.DWORD]
        get_style = self.user.GetWindowLongW
        get_style.argtypes = [wt.HWND, ct.c_int]
        set_style = self.user.SetWindowLongW
        set_style.argtypes = [wt.HWND, ct.c_int, wt.LONG]
        set_style(self.hwnd, -20, get_style(self.hwnd, -20) | 0x80000)
        screen = self.user.GetDC(None)
        try:
            self.dc = self.gdi.CreateCompatibleDC(screen)
            self.pixels = ct.c_void_p()
            info = BitmapInfo(ct.sizeof(BitmapInfo), self.width, -self.height, 1, 32)
            self.bitmap = self.gdi.CreateDIBSection(screen, ct.byref(info), 0, ct.byref(self.pixels), None, 0)
            if not self.bitmap:
                raise ct.WinError()
            self.original = self.gdi.SelectObject(self.dc, self.bitmap)
        finally:
            self.user.ReleaseDC(None, screen)

        def down(sender, event):
            if event.Button == MouseButtons.Left:
                controller.snap_generation += 1
                controller.dock_side = None
                point = self.form.PointToScreen(event.Location)
                self.drag = (point.X, point.Y, self.form.Left, self.form.Top, False)
                self.drag_pointer = (point.X, point.Y)
                self.form.Capture = True
            elif event.Button == MouseButtons.Right:
                import threading
                from .desktop_pet import _PetBridge
                threading.Thread(target=_PetBridge(controller).menu, daemon=True).start()
        def move(sender, event):
            if self.drag is None:
                return
            x, y, left, top, moved = self.drag
            point = self.form.PointToScreen(event.Location)
            dx, dy = point.X - x, point.Y - y
            moved = moved or abs(dx) + abs(dy) > 3
            self.drag = (x, y, left, top, moved)
            if moved:
                from .desktop_pet import drag_direction
                pointer = (point.X, point.Y)
                last = self.drag_pointer or pointer
                direction = drag_direction(pointer[0] - last[0], pointer[1] - last[1], controller.drag_direction)
                self.drag_pointer = pointer
                if direction != controller.drag_direction:
                    controller.drag_direction = direction
                    self.next_status = 0
                if not controller.dragging:
                    controller.dragging = True
                    self.next_status = 0
                    if controller.chat_visible:
                        import threading
                        from .desktop_pet import _PetBridge
                        threading.Thread(target=_PetBridge(controller).chat_hide, daemon=True).start()
                from System.Windows.Forms import Screen
                from .desktop_pet import contain_body, body_bounds
                area = Screen.FromPoint(point).WorkingArea
                self.work_area = (area.X, area.Y, area.Width, area.Height)
                self.form.Left, self.form.Top = contain_body(left+dx, top+dy,
                    body_bounds('drag-'+direction, self.width), self.work_area)
        def up(sender, event):
            if self.drag is None:
                return
            point = self.form.PointToScreen(event.Location)
            moved = self.drag[-1] or abs(point.X-self.drag[0]) + abs(point.Y-self.drag[1]) > 3
            self.drag = None
            self.drag_pointer = None
            self.form.Capture = False
            controller.dragging = False
            if moved:
                controller.release_until = time.monotonic() + .45
            self.next_status = 0
            if moved:
                from .desktop_pet import _PetBridge
                _PetBridge(controller).save_position()
                controller.snap_to_edge()
            else:
                self._open()
        self.form.MouseDown += down
        self.form.MouseMove += move
        self.form.MouseUp += up
        self.timer = Timer()
        self.timer.Interval = 16
        self.timer.Tick += self._tick
        self.timer.Start()
        self._tick(None, None)
        self._contain()

    def _update_size(self):
        from System.Windows.Forms import Screen
        from .desktop_pet import pet_dimensions
        rect = Screen.FromHandle(self.form.Handle).WorkingArea
        scale = self.form._scale or 1
        logical = pet_dimensions((0, 0, rect.Width/scale, rect.Height/scale),
                                 getattr(self.controller.api.settings_mgr.get(), 'gui_pet_scale', 1))
        width, height = (round(v*scale) for v in logical)
        self.controller.pet_width, self.controller.pet_height = logical
        if (width, height) == (self.width, self.height):
            return
        screen = self.user.GetDC(None)
        try:
            pixels = ct.c_void_p()
            info = self.bitmap_info(ct.sizeof(self.bitmap_info), width, -height, 1, 32)
            bitmap = self.gdi.CreateDIBSection(screen, ct.byref(info), 0, ct.byref(pixels), None, 0)
            if not bitmap:
                raise ct.WinError()
            self.gdi.SelectObject(self.dc, bitmap)
            self.gdi.DeleteObject(self.bitmap)
            self.bitmap, self.pixels = bitmap, pixels
            self.width, self.height = width, height
        finally:
            self.user.ReleaseDC(None, screen)

    def _contain(self):
        from System.Windows.Forms import Screen
        from .desktop_pet import contain_body, body_bounds, body_dock
        if self.work_area is None:
            area = Screen.FromHandle(self.form.Handle).WorkingArea
            self.work_area = (area.X, area.Y, area.Width, area.Height)
        bounds = body_bounds(self.state or 'idle', self.width, self.controller.dock_side == 'right' and not self.controller.dragging)
        x, y = contain_body(self.form.Left, self.form.Top, bounds, self.work_area)
        if self.controller.dock_side and not self.controller.dragging:
            _, x, y = body_dock(x, y, bounds, self.work_area, side=self.controller.dock_side)
        self.form.Left, self.form.Top = x, y

    def position_chat(self):
        from .tray import run_on_winforms_ui
        def place():
            chat = getattr(self.controller.chat_window, 'native', None)
            if chat is None or self.closed:
                return
            from System.Windows.Forms import Screen
            from .desktop_pet import contain_position
            from .desktop_pet import body_bounds
            area = Screen.FromHandle(self.form.Handle).WorkingArea
            left, top, right, bottom = body_bounds(self.state or 'idle', self.width, self.controller.dock_side == 'right' and not self.controller.dragging)
            x = self.form.Left+left-chat.Width-8
            if x < area.Left:
                x = self.form.Left+right+8
            y = self.form.Top+bottom-chat.Height
            if self.controller.chat_mode == 'toolbar':
                x = round(self.form.Left+(left+right-chat.Width)/2)
                y = self.form.Top+bottom+6
                if y+chat.Height > area.Bottom:
                    y = self.form.Top+top-chat.Height-6
            chat.Left, chat.Top = contain_position(x, y, chat.Width, chat.Height,
                                                   (area.X, area.Y, area.Width, area.Height))
        run_on_winforms_ui(place)

    def reset_position(self):
        from .tray import run_on_winforms_ui
        def reset():
            from System.Windows.Forms import Screen
            area = Screen.PrimaryScreen.WorkingArea
            self.work_area = (area.X, area.Y, area.Width, area.Height)
            self.controller.dock_side = None
            self.drag = None
            self.form.Capture = False
            self.form.Left, self.form.Top = area.Right-self.width-24, area.Bottom-self.height-24
            from .desktop_pet import _PetBridge
            _PetBridge(self.controller).save_position()
        run_on_winforms_ui(reset)

    def snap_to_edge(self):
        from .tray import run_on_winforms_ui
        def start():
            from System.Windows.Forms import Screen, Timer
            from .desktop_pet import body_dock, body_bounds, _PetBridge
            self._contain()
            x, y = self.form.Left, self.form.Top
            # The attraction distance is a logical UI distance, independent of
            # the pet's user-selected size and the monitor's physical DPI.
            dock = body_dock(x, y, body_bounds(self.state or 'idle', self.width, self.controller.dock_side == 'right' and not self.controller.dragging),
                             self.work_area, threshold=120*(self.form._scale or 1))
            if dock is None:
                return
            side = dock[0]
            if self.snap_timer is not None:
                self.snap_timer.Stop()
                self.snap_timer.Dispose()
            generation = self.controller.snap_generation
            started = time.monotonic()
            timer = Timer()
            self.snap_timer = timer
            timer.Interval = 16
            def tick(*_):
                if self.closed or generation != self.controller.snap_generation:
                    timer.Stop();timer.Dispose()
                    if self.snap_timer is timer:self.snap_timer = None
                    return
                progress = min(1, (time.monotonic()-started)/.22)
                eased = 1-(1-progress)**3
                _, tx, ty = body_dock(x, y, body_bounds(self.state or 'idle', self.width, self.controller.dock_side == 'right' and not self.controller.dragging), self.work_area, side=side)
                self.form.Left = round(x+(tx-x)*eased)
                self.form.Top = round(y+(ty-y)*eased)
                if progress >= 1:
                    timer.Stop();timer.Dispose()
                    if self.snap_timer is timer:self.snap_timer = None
                    self.controller.dock_side = side
                    self._contain()
                    _PetBridge(self.controller).save_position()
            timer.Tick += tick
            timer.Start()
        run_on_winforms_ui(start)

    def _open(self):
        # Bridge evaluation marshals back to WinForms; keep it off its UI thread.
        import threading
        from .desktop_pet import _PetBridge
        threading.Thread(target=_PetBridge(self.controller).interact, daemon=True).start()

    def _open_main(self):
        import threading
        from .desktop_pet import _PetBridge
        threading.Thread(target=_PetBridge(self.controller).open_assistant, daemon=True).start()

    def _hide(self):
        import threading
        threading.Thread(target=lambda: self.controller.api.set_desktop_pet_enabled(False), daemon=True).start()

    def _tick(self, *_):
        if self.closed or not self.form.Visible:
            return
        now = time.monotonic()
        if now >= self.next_status:
            self._update_size()
            from .desktop_pet import _PetBridge, ASSET_DIR
            status = _PetBridge(self.controller).status()
            state = status.get('animation', status['state'])
            labels = {'idle': '点击小龙，开始聊天', 'sleep': '休息中 · 点击聊天',
                      'thinking': '正在思考', 'working': '正在执行', 'wave': '你好 · 点击聊天',
                      'complete': '任务完成', 'alert': '需要查看'}
            self.label = ('松手放下我' if state.startswith('drag-') else '放好啦' if state == 'release' else status.get('task') or labels[state])[:23]
            self.next_status = now + .75
            if state != self.state:
                if self.image is not None:
                    self.image.close()
                asset = ASSET_DIR / f'{state}.webp'
                self.drag_intro = state == 'drag'
                if self.drag_intro:
                    asset = ASSET_DIR / 'drag-start.webp'
                self.image = Image.open(asset)
                self.state, self.frame, self.next_frame = state, 0, now
                self.clip_started = now
                self.clip_key = asset.stem
        if self.image is None or now + .002 < self.next_frame:
            return
        from .desktop_pet import _bounds_manifest, animation_frame
        durations = _bounds_manifest()['_durations'][self.clip_key]
        elapsed = now-self.clip_started
        if self.state == 'release' and elapsed*1000 >= sum(durations):
            self.controller.release_until = 0.0
            self.next_status = 0
        self.frame = animation_frame(elapsed, durations)
        self.image.seek(self.frame)
        self._contain()
        rgba = self.image.convert('RGBA').resize((self.width, self.width), Image.Resampling.LANCZOS)
        if self.controller.dock_side == 'right' and not self.controller.dragging:
            rgba = rgba.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        canvas = Image.new('RGBA', (self.width, self.height))
        canvas.paste(rgba, (0, 0))
        rgba = canvas
        red, green, blue, alpha = rgba.split()
        premultiplied = Image.merge('RGBA', tuple(ImageChops.multiply(c, alpha) for c in (red, green, blue)) + (alpha,))
        raw = premultiplied.tobytes('raw', 'BGRA')
        ct.memmove(self.pixels, raw, len(raw))
        size, source = wt.SIZE(self.width, self.height), wt.POINT(0, 0)
        if not self.user.UpdateLayeredWindow(self.hwnd, None, None, ct.byref(size), self.dc,
                                              ct.byref(source), 0, ct.byref(self.blend), 2):
            raise ct.WinError()
        self.next_frame = now + 1/60

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.Stop()
        self.timer.Dispose()
        if self.snap_timer is not None:
            self.snap_timer.Stop()
            self.snap_timer.Dispose()
        if self.image is not None:
            self.image.close()
        self.gdi.SelectObject(self.dc, self.original)
        self.gdi.DeleteObject(self.bitmap)
        self.gdi.DeleteDC(self.dc)
