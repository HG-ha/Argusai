"""GUI tray / close-to-tray helpers."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import argusai.gui.tray as tray_mod
from argusai.gui.tray import (
    _ID_OPEN,
    _ID_QUIT,
    _WindowsTray,
    _tray_event_is_menu,
    _tray_event_is_show,
    apply_menu_command,
    close_to_tray_supported,
    start_in_tray_requested,
    tray_supported,
)
from argusai.gui.window import WindowMixin


class _Settings:
    def __init__(self, close_to_tray: bool = True) -> None:
        self.gui_close_to_tray = close_to_tray


class _SettingsMgr:
    def __init__(self, close_to_tray: bool = True) -> None:
        self._settings = _Settings(close_to_tray)

    def get(self) -> _Settings:
        return self._settings

    def update(self, fn) -> None:  # noqa: ANN001
        fn(self._settings)


class _Api(WindowMixin):
    def __init__(self, close_to_tray: bool = True) -> None:
        self.settings_mgr = _SettingsMgr(close_to_tray)
        self._window = SimpleNamespace(
            hidden=False,
            hide=lambda: setattr(self._window, "hidden", True),
            show=lambda: setattr(self._window, "hidden", False),
            restore=lambda: None,
            maximize=lambda: None,
            destroy=lambda: setattr(self._window, "destroyed", True),
        )
        self._window.destroyed = False
        self._window_maximized = False
        self._quit_requested = False
        self._app_attention_state = {"focused": True, "visible": True}

    def persist_window_geometry(self) -> dict:
        return {"ok": True}

    def set_app_attention_state(self, state: dict) -> dict:
        self._app_attention_state = dict(state)
        return {"ok": True}


def test_import_skips_windows_dlls_off_win32() -> None:
    if sys.platform == "win32":
        assert tray_mod._user32 is not None
        assert tray_supported() is True
    else:
        assert tray_mod._user32 is None
        assert tray_supported() is False


def test_start_in_tray_flag_and_env(monkeypatch) -> None:
    assert start_in_tray_requested(["--gui"]) is False
    assert start_in_tray_requested(["--gui", "--tray"]) is True
    monkeypatch.setenv("ARGUSAI_START_IN_TRAY", "1")
    assert start_in_tray_requested(["--gui"]) is True


def test_start_in_tray_macos_login_item(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.sys.platform", "darwin")
    monkeypatch.setattr(
        "argusai.cli.macos_login_item.launched_hidden_at_login",
        lambda: True,
    )
    assert start_in_tray_requested(["--gui"]) is True


def test_show_window_from_tray_unhides() -> None:
    api = _Api()
    api._window.hidden = True
    result = api.show_window_from_tray()
    assert result["ok"] is True
    assert api._window.hidden is False


def test_close_window_hides_when_tray_enabled(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.close_to_tray_supported", lambda: True)
    api = _Api(close_to_tray=True)
    result = api.close_window()
    assert result["ok"] is True
    assert result.get("hidden") is True
    assert api._window.hidden is True
    assert api._window.destroyed is False
    assert api._quit_requested is False
    assert api._app_attention_state["visible"] is False


def test_close_window_quits_when_tray_icon_missing(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.close_to_tray_supported", lambda: True)
    api = _Api(close_to_tray=True)
    api._tray = SimpleNamespace(icon_visible=lambda: False)
    result = api.close_window()
    assert result.get("quit") is True
    assert api._quit_requested is True
    assert api._window.destroyed is True


def test_close_window_hides_when_tray_icon_visible(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.close_to_tray_supported", lambda: True)
    api = _Api(close_to_tray=True)
    api._tray = SimpleNamespace(icon_visible=lambda: True)
    result = api.close_window()
    assert result.get("hidden") is True
    assert api._window.destroyed is False


def test_close_window_quits_when_tray_disabled(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.close_to_tray_supported", lambda: True)
    api = _Api(close_to_tray=False)
    result = api.close_window()
    assert result["ok"] is True
    assert result.get("quit") is True
    assert api._quit_requested is True
    assert api._window.destroyed is True


def test_quit_application_destroys_even_when_tray_enabled(monkeypatch) -> None:
    monkeypatch.setattr("argusai.gui.tray.close_to_tray_supported", lambda: True)
    api = _Api(close_to_tray=True)
    result = api.quit_application()
    assert result["ok"] is True
    assert api._quit_requested is True
    assert api._window.destroyed is True


def test_set_gui_close_to_tray() -> None:
    api = _Api(close_to_tray=True)
    result = api.set_gui_close_to_tray(False)
    assert result == {"ok": True, "close_to_tray": False}
    assert api.settings_mgr.get().gui_close_to_tray is False


def test_close_to_tray_supported_platforms() -> None:
    assert close_to_tray_supported() in {True, False}


def test_windows_tray_icon_roundtrip() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetDesktopWindow.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    # Twice: the window class must be re-registered with the new WndProc.
    for _ in range(2):
        hits: list[str] = []
        tray = _WindowsTray(
            on_show=lambda: hits.append("show"),
            on_quit=lambda: hits.append("quit"),
        )
        try:
            tray.add_icon()
            assert tray._icon_added is True
            assert tray._hwnd
            assert tray._shell_icon is True
            # Top-level (not HWND_MESSAGE) so Explorer's TaskbarCreated reaches it.
            assert user32.GetAncestor(ctypes.c_void_p(tray._hwnd), 1) == user32.GetDesktopWindow()
            assert tray._taskbar_created
            user32.SendMessageW(ctypes.c_void_p(tray._hwnd), tray._taskbar_created, 0, 0)
            assert tray._shell_icon is True
            assert tray._icon_added is True
        finally:
            tray.remove()
        assert tray._icon_added is False
        assert tray._hwnd == 0


def test_handoff_shows_running_copy(monkeypatch) -> None:
    monkeypatch.setattr(tray_mod, "_win_request_show", lambda: True)
    monkeypatch.setattr(tray_mod, "_win_wait_mutex", lambda *_a: True)
    assert tray_mod._win_hand_off_or_take_over(SimpleNamespace(_mutex=1)) is False


def test_handoff_boots_once_previous_copy_exits(monkeypatch) -> None:
    waits = iter([False, False, True])
    monkeypatch.setattr(tray_mod, "_win_request_show", lambda: False)
    monkeypatch.setattr(tray_mod, "_win_wait_mutex", lambda *_a: next(waits))
    assert tray_mod._win_hand_off_or_take_over(SimpleNamespace(_mutex=1)) is True


def test_handoff_gives_up_after_deadline(monkeypatch) -> None:
    monkeypatch.setattr(tray_mod, "_HANDOFF_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(tray_mod, "_win_request_show", lambda: False)
    monkeypatch.setattr(tray_mod, "_win_wait_mutex", lambda *_a: False)
    assert tray_mod._win_hand_off_or_take_over(SimpleNamespace(_mutex=1)) is False


def test_tray_menu_commands_dispatch() -> None:
    hits: list[str] = []
    assert apply_menu_command(_ID_OPEN, lambda: hits.append("show"), lambda: hits.append("quit"))
    assert apply_menu_command(_ID_QUIT, lambda: hits.append("show"), lambda: hits.append("quit"))
    assert apply_menu_command(0, lambda: hits.append("show"), lambda: hits.append("quit")) is False
    assert hits == ["show", "quit"]


def test_tray_callback_events() -> None:
    assert _tray_event_is_show(0x0202)  # WM_LBUTTONUP
    assert _tray_event_is_show(0x0203)  # WM_LBUTTONDBLCLK
    assert _tray_event_is_show(0x0400)  # NIN_SELECT
    assert _tray_event_is_menu(0x0205)  # WM_RBUTTONUP
    assert _tray_event_is_menu(0x007B)  # WM_CONTEXTMENU
    assert not _tray_event_is_show(0x0205)
    assert not _tray_event_is_menu(0x0202)
