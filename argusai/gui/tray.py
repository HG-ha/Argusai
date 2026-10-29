"""Desktop tray + single-instance activation for the GUI.

Windows is the first-class target: a NotifyIcon plus a hidden top-level window
so a second Start Menu launch can unhide the running copy instead of taking
the channel host lock. Close-to-tray lives in ``launch`` / ``window``; this
module only owns the icon, the mutex, and the show/quit callbacks.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("argusai.gui")

TRAY_WINDOW_CLASS = "ArgusAITrayWnd"
_MUTEX_NAME = "Local\\ArgusAI.GUI.SingleInstance"
_WM_APP_SHOW = 0x8001  # WM_APP + 1
_WM_TRAY_STOP = 0x8002
_ID_OPEN = 1
_ID_QUIT = 2
_HWND_MESSAGE = -3
# A running copy that is still booting WebView2 (logon) or still tearing down
# after Quit holds the mutex without a tray window for several seconds.
_HANDOFF_WAIT_SECONDS = 20.0
_HANDOFF_POLL_MS = 500
# Right after logon Explorer can reject NIM_ADD until the taskbar is up.
_SHELL_ICON_ATTEMPTS = 3
_SHELL_ICON_RETRY_SECONDS = 1.0
_TRAY_READY_TIMEOUT = 10.0

_user32 = None
_kernel32 = None
_shell32 = None
_WNDPROC = None
_WNDCLASSW = None
wintypes = None

if sys.platform == "win32":
    from ctypes import wintypes as wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        wintypes.HWND,
        ctypes.c_uint,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
    _user32.UnregisterClassW.restype = wintypes.BOOL
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_uint, wintypes.LPCWSTR]
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = ctypes.c_int
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT
    _user32.ChangeWindowMessageFilterEx.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
    _user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowW.restype = wintypes.HWND
    _user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    _user32.FindWindowExW.restype = wintypes.HWND
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    _kernel32.SetLastError.argtypes = [wintypes.DWORD]
    _kernel32.GetLastError.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

ShowFn = Callable[[], None]
QuitFn = Callable[[], None]


def start_in_tray_requested(argv: list[str] | None = None) -> bool:
    """True when this process should boot with the window hidden."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--tray" in args:
        return True
    env = str(os.environ.get("ARGUSAI_START_IN_TRAY") or "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if sys.platform == "darwin":
        try:
            from argusai.cli.macos_login_item import launched_hidden_at_login

            return launched_hidden_at_login()
        except Exception:
            return False
    return False


def tray_supported() -> bool:
    return sys.platform == "win32"


def close_to_tray_supported() -> bool:
    """Hide-on-close is useful wherever a restore surface exists.

    Windows restores from the tray icon. macOS keeps the Dock icon. Linux
    has neither unless a desktop file is registered, so close still quits.
    """
    return sys.platform in {"win32", "darwin"}


class TrayController:
    """Optional native tray. Safe to construct on every platform."""

    def __init__(self) -> None:
        self._mutex: Any = None
        self._impl: _WindowsTray | None = None
        self._show: ShowFn | None = None
        self._quit: QuitFn | None = None
        self._pending_show = False

    def claim_or_activate(self) -> bool:
        """Return True if this process should boot the GUI.

        A False return means another GUI is already running and has been
        asked to show itself.

        The native icon is **not** created here. pywebview's WinForms
        backend pumps messages on a different STA than this boot thread;
        a NotifyIcon HWND created now never sees clicks.
        """
        if sys.platform != "win32":
            return True
        if not _win_claim_mutex(self) and not _win_hand_off_or_take_over(self):
            return False
        self._impl = _WindowsTray(
            on_show=self._dispatch_show,
            on_quit=self._dispatch_quit,
        )
        return True

    def bind(self, *, show: ShowFn, quit: QuitFn) -> None:
        self._show = show
        self._quit = quit
        if self._pending_show:
            self._pending_show = False
            show()

    def start(self) -> None:
        if self._impl is None:
            return
        try:
            self._impl.add_icon()
        except Exception:
            log.exception("gui.tray failed to add NotifyIcon")

    def icon_visible(self) -> bool:
        return bool(self._impl is not None and getattr(self._impl, "_icon_added", False))

    def stop(self) -> None:
        if self._impl is None:
            return
        try:
            self._impl.remove()
        except Exception:
            log.debug("gui.tray remove failed", exc_info=True)
        self._impl = None

    def _dispatch_show(self) -> None:
        if self._show is None:
            self._pending_show = True
            return
        self._show()

    def _dispatch_quit(self) -> None:
        if self._quit is not None:
            self._quit()


def _win_claim_mutex(controller: TrayController) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        return True
    controller._mutex = handle
    last_error = int(kernel32.GetLastError() or 0)
    return last_error != 183  # ERROR_ALREADY_EXISTS


def _win_wait_mutex(handle: Any, timeout_ms: int) -> bool:
    """Own ``handle`` once the previous holder releases it or exits."""
    if not handle:
        return False
    result = int(_kernel32.WaitForSingleObject(handle, int(timeout_ms)))
    return result in {0x00000000, 0x00000080}  # WAIT_OBJECT_0 / WAIT_ABANDONED


def _win_hand_off_or_take_over(controller: TrayController) -> bool:
    """Show the running copy (False) or boot once it is gone (True)."""
    deadline = time.monotonic() + _HANDOFF_WAIT_SECONDS
    while True:
        if _win_request_show():
            log.info("gui.boot existing instance asked to show")
            return False
        if _win_wait_mutex(controller._mutex, _HANDOFF_POLL_MS):
            log.info("gui.boot previous instance released the mutex; booting")
            return True
        if time.monotonic() >= deadline:
            log.warning("gui.boot another instance holds the mutex but no tray window replied")
            return False


def _tray_event_is_show(event: int) -> bool:
    """Classic mouse ups plus NOTIFYICON_VERSION_4 NIN_SELECT / NIN_KEYSELECT."""
    return event in {0x0202, 0x0203, 0x0400, 0x0401}


def _tray_event_is_menu(event: int) -> bool:
    return event in {0x0205, 0x007B}  # WM_RBUTTONUP / WM_CONTEXTMENU


def apply_menu_command(cmd: int, on_show: ShowFn, on_quit: QuitFn) -> bool:
    """``TrackPopupMenu(..., TPM_RETURNCMD)`` returns the id; it does not post WM_COMMAND."""
    if cmd == _ID_OPEN:
        on_show()
        return True
    if cmd == _ID_QUIT:
        on_quit()
        return True
    return False


def run_on_winforms_ui(fn: Callable[[], None]) -> None:
    """Marshal onto the pywebview form thread. Safe to call from the tray thread."""
    try:
        from System.Windows.Forms import MethodInvoker  # type: ignore[import-untyped]
        from webview.platforms.winforms import BrowserView  # type: ignore[import-untyped]

        form = next(iter(BrowserView.instances.values()), None)
        if form is None:
            fn()
            return
        if getattr(form, "InvokeRequired", False):
            form.BeginInvoke(MethodInvoker(fn))
            return
        fn()
    except Exception:
        log.debug("gui.tray UI marshal failed; running inline", exc_info=True)
        fn()


def _find_tray_window() -> int:
    hwnd = _user32.FindWindowW(TRAY_WINDOW_CLASS, None)
    if not hwnd:
        # Builds up to 0.4.9 parked the tray window on HWND_MESSAGE, which
        # FindWindowW never searches.
        hwnd = _user32.FindWindowExW(_HWND_MESSAGE, None, TRAY_WINDOW_CLASS, None)
    return int(hwnd or 0)


def _win_request_show() -> bool:
    hwnd = _find_tray_window()
    if not hwnd:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "ArgusAI")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    return bool(_user32.PostMessageW(hwnd, _WM_APP_SHOW, 0, 0))


def _tray_strings() -> tuple[str, str, str]:
    from argusai.core.channels.i18n import detect_system_locale

    if detect_system_locale() == "zh":
        return ("ArgusAI", "打开", "退出")
    return ("ArgusAI", "Open", "Quit")


class _WindowsTray:
    """NotifyIcon on a dedicated thread that pumps its own messages.

    pywebview's ``start(func)`` runs ``func`` on a worker thread, then the
    WinForms form lives on a different STA. Creating the icon on either of
    those threads makes it invisible or dead. This thread is independent.
    """

    def __init__(self, *, on_show: ShowFn, on_quit: QuitFn) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._hwnd = 0
        self._hicon = 0
        self._nid_size = 0
        self._wndproc = None
        self._taskbar_created = 0
        self._icon_added = False
        self._shell_icon = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def add_icon(self) -> None:
        if self._icon_added or (self._thread is not None and self._thread.is_alive()):
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="argusai-tray",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(_TRAY_READY_TIMEOUT):
            raise RuntimeError("tray thread did not become ready")
        if self._icon_added:
            return
        if self._error is not None:
            raise self._error
        raise RuntimeError("tray icon was not added")

    def _thread_main(self) -> None:
        try:
            self.create()
        except BaseException as exc:
            self._error = exc
            log.exception("gui.tray thread failed to start")
            self._ready.set()
            return
        try:
            self._add_shell_icon()
            self._icon_added = True
            log.info("gui.tray icon added hwnd=%s", self._hwnd)
        except Exception as exc:
            # Keep pumping: TaskbarCreated adds the icon once Explorer is up.
            self._error = exc
            log.warning("gui.tray NIM_ADD failed; waiting for TaskbarCreated: %s", exc)
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._teardown_on_thread()

    def _on_taskbar_created(self) -> None:
        """Explorer restarted (or finished starting): its tray forgot our icon."""
        if self._shell_icon:
            nid = _notify_icon_data(self._hwnd, 0, "")
            _shell32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
            self._shell_icon = False
        try:
            self._add_shell_icon()
        except Exception as exc:
            log.warning("gui.tray re-add after TaskbarCreated failed: %s", exc)
            return
        self._icon_added = True
        self._error = None
        log.info("gui.tray icon re-added after TaskbarCreated hwnd=%s", self._hwnd)

    def create(self) -> None:
        """Hidden top-level window. Must run on the tray thread.

        Not HWND_MESSAGE: message-only windows miss the TaskbarCreated
        broadcast and are invisible to FindWindowW (single-instance handoff).
        """

        def _wnd_proc(hwnd, msg, wparam, lparam):
            msg_i = int(msg)
            if self._taskbar_created and msg_i == self._taskbar_created:
                self._on_taskbar_created()
                return 0
            if msg_i == 0x0401:  # WM_USER + 1 (tray callback)
                event = int(lparam) & 0xFFFF
                if _tray_event_is_show(event):
                    self._on_show()
                elif _tray_event_is_menu(event):
                    self._popup_menu(int(hwnd))
                return 0
            if msg_i == _WM_APP_SHOW:
                self._on_show()
                return 0
            if msg_i == _WM_TRAY_STOP:
                _user32.PostQuitMessage(0)
                return 0
            if msg_i == 0x0111:  # WM_COMMAND
                apply_menu_command(int(wparam) & 0xFFFF, self._on_show, self._on_quit)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = _WNDPROC(_wnd_proc)
        hinst = _kernel32.GetModuleHandleW(None)
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = TRAY_WINDOW_CLASS
        _kernel32.SetLastError(0)
        atom = _user32.RegisterClassW(ctypes.byref(wc))
        if not atom and int(_kernel32.GetLastError() or 0) not in {0, 1410}:
            raise ctypes.WinError()
        self._taskbar_created = int(_user32.RegisterWindowMessageW("TaskbarCreated") or 0)
        hwnd = _user32.CreateWindowExW(
            0x00000080,  # WS_EX_TOOLWINDOW: no taskbar / Alt+Tab entry
            TRAY_WINDOW_CLASS,
            "ArgusAI tray",
            0x80000000,  # WS_POPUP, never shown
            0,
            0,
            0,
            0,
            None,
            None,
            hinst,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        self._hwnd = int(hwnd)
        if self._taskbar_created:
            # Elevated processes drop the broadcast from Explorer (UIPI) otherwise.
            _user32.ChangeWindowMessageFilterEx(hwnd, self._taskbar_created, 1, None)  # MSGFLT_ALLOW

    def _add_shell_icon(self) -> None:
        if not self._hwnd:
            raise RuntimeError("tray window missing")
        tip, _, _ = _tray_strings()
        if not self._hicon:
            self._hicon = _load_hicon()
        if not self._hicon:
            raise RuntimeError("tray icon handle is empty")
        nid = _notify_icon_data(self._hwnd, self._hicon, tip)
        self._nid_size = nid.cbSize
        error = 0
        for attempt in range(_SHELL_ICON_ATTEMPTS):
            if _shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):  # NIM_ADD
                self._shell_icon = True
                return
            error = ctypes.get_last_error()
            if attempt + 1 < _SHELL_ICON_ATTEMPTS:
                time.sleep(_SHELL_ICON_RETRY_SECONDS)
        raise ctypes.WinError(error or None)

    def remove(self) -> None:
        hwnd = self._hwnd
        thread = self._thread
        if hwnd:
            _user32.PostMessageW(hwnd, _WM_TRAY_STOP, 0, 0)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self._icon_added or self._hwnd:
            self._teardown_on_thread()
        self._thread = None

    def _teardown_on_thread(self) -> None:
        if self._hwnd and self._shell_icon:
            nid = _notify_icon_data(self._hwnd, 0, "")
            _shell32.Shell_NotifyIconW(2, ctypes.byref(nid))  # NIM_DELETE
            self._shell_icon = False
        self._icon_added = False
        if self._hicon:
            _user32.DestroyIcon(self._hicon)
            self._hicon = 0
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            self._hwnd = 0
            # The class keeps a pointer to this instance's ctypes WndProc thunk.
            _user32.UnregisterClassW(TRAY_WINDOW_CLASS, _kernel32.GetModuleHandleW(None))

    def _popup_menu(self, hwnd: int) -> None:
        _, open_label, quit_label = _tray_strings()
        menu = _user32.CreatePopupMenu()
        if not menu:
            return
        _user32.AppendMenuW(menu, 0, _ID_OPEN, open_label)
        _user32.AppendMenuW(menu, 0, _ID_QUIT, quit_label)
        point = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        _user32.SetForegroundWindow(hwnd)
        # TPM_RIGHTBUTTON | TPM_RETURNCMD — the selected id is the return value.
        cmd = int(_user32.TrackPopupMenu(menu, 0x0002 | 0x0100, point.x, point.y, 0, hwnd, None) or 0)
        _user32.DestroyMenu(menu)
        _user32.PostMessageW(hwnd, 0, 0, 0)
        apply_menu_command(cmd, self._on_show, self._on_quit)


def _notify_icon_data(hwnd: int, hicon: int, tip: str) -> Any:
    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint32),
            ("hWnd", ctypes.c_void_p),
            ("uID", ctypes.c_uint32),
            ("uFlags", ctypes.c_uint32),
            ("uCallbackMessage", ctypes.c_uint32),
            ("hIcon", ctypes.c_void_p),
            ("szTip", ctypes.c_wchar * 128),
            ("dwState", ctypes.c_uint32),
            ("dwStateMask", ctypes.c_uint32),
            ("szInfo", ctypes.c_wchar * 256),
            ("uVersion", ctypes.c_uint32),
            ("szInfoTitle", ctypes.c_wchar * 64),
            ("dwInfoFlags", ctypes.c_uint32),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", ctypes.c_void_p),
        ]

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = 0x01 | 0x02 | 0x04  # NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = 0x0401
    nid.hIcon = hicon or None
    nid.szTip = (tip or "ArgusAI")[:127]
    return nid


def _load_hicon() -> int:
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    exe = ""
    try:
        from argusai.core.infra.runtime_exec import frozen_self_executable

        exe = frozen_self_executable()
    except Exception:
        exe = str(sys.executable or "")
    if exe:
        large = ctypes.c_void_p()
        small = ctypes.c_void_p()
        extracted = shell32.ExtractIconExW(exe, 0, ctypes.byref(large), ctypes.byref(small), 1)
        if extracted:
            handle = int(small.value or 0) or int(large.value or 0)
            if handle:
                return handle
    for candidate in _icon_candidates():
        if not candidate.is_file():
            continue
        handle = user32.LoadImageW(None, str(candidate), 1, 16, 16, 0x0010)
        if handle:
            return int(handle)
    return int(user32.LoadIconW(None, ctypes.c_void_p(32512)) or 0)


def _icon_candidates() -> list[Path]:
    names = (
        "argusai-icon-Transparency.ico",
        "argusai-icon.ico",
    )
    roots = [
        Path(__file__).resolve().parents[2] / "assets" / "brand",
        Path(sys.executable).resolve().parent,
        Path(sys.executable).resolve().parent / "assets" / "brand",
    ]
    out: list[Path] = []
    for root in roots:
        for name in names:
            out.append(root / name)
    return out
