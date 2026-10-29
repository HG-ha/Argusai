"""Desktop automation permission and session gates."""

from __future__ import annotations

from argusai.core.assistant.streaming import _CONFIRM_TOOLS, _NO_AUTO_APPROVE_TOOLS
from argusai.core.assistant.tools import _tool_risk
from argusai.core.tools import desktop_automation as da
from argusai.core.workspace import desktop_control as dc


def test_save_compressed_image_honors_quality_and_no_resize(tmp_path) -> None:
    from PIL import Image

    img = Image.new("RGB", (2400, 1200), color=(40, 80, 120))
    sharp = tmp_path / "sharp.jpg"
    soft = tmp_path / "soft.jpg"
    native = tmp_path / "native.jpg"

    high = dc._save_compressed_image(img.copy(), sharp, max_width=1920, jpeg_quality=100)
    low = dc._save_compressed_image(img.copy(), soft, max_width=1920, jpeg_quality=40)
    full = dc._save_compressed_image(img.copy(), native, max_width=0, jpeg_quality=95)

    assert high["jpeg_quality"] == 100
    assert low["jpeg_quality"] == 40
    assert high["output_pixels"][0] == 1920
    assert full["output_pixels"] == (2400, 1200)
    assert sharp.stat().st_size > soft.stat().st_size


def test_desktop_observe_forwards_screenshot_compression(monkeypatch, tmp_path) -> None:
    class Settings:
        data_root = str(tmp_path)

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()

    seen: dict[str, object] = {}

    def fake_capture(path, *, monitor=None, max_width=1920, jpeg_quality=90):
        seen["max_width"] = max_width
        seen["jpeg_quality"] = jpeg_quality
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpeg")
        return {
            "ok": True,
            "path": str(path),
            "mime": "image/jpeg",
            "size": path.stat().st_size,
            "jpeg_quality": jpeg_quality,
        }

    monkeypatch.setattr(dc, "list_monitors", lambda: [{
        "index": 0, "primary": True, "left": 0, "top": 0, "width": 100, "height": 80, "right": 100, "bottom": 80,
    }])
    monkeypatch.setattr(dc, "foreground_window", lambda: {"ok": True, "title": "ArgusAI"})
    monkeypatch.setattr(dc, "capture_screenshot", fake_capture)

    da.desktop_observe(
        Api(),
        mode="foreground",
        screenshot=True,
        screenshot_max_width=0,
        screenshot_quality=98,
        _session_id="sess-compress",
    )
    assert seen["max_width"] == 0
    assert seen["jpeg_quality"] == 98


def test_desktop_observe_auto_max_width_for_4k(monkeypatch, tmp_path) -> None:
    class Settings:
        data_root = str(tmp_path)

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()

    seen: dict[str, object] = {}

    def fake_capture(path, *, monitor=None, max_width=1920, jpeg_quality=90):
        seen["max_width"] = max_width
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpeg")
        return {"ok": True, "path": str(path), "mime": "image/jpeg", "size": 9}

    monkeypatch.setattr(dc, "list_monitors", lambda: [{
        "index": 0, "primary": True, "left": 0, "top": 0,
        "width": 3840, "height": 2160, "right": 3840, "bottom": 2160,
    }])
    monkeypatch.setattr(dc, "foreground_window", lambda: {"ok": True, "title": "ArgusAI"})
    monkeypatch.setattr(dc, "capture_screenshot", fake_capture)

    result = da.desktop_observe(
        Api(),
        mode="foreground",
        screenshot=True,
        _session_id="sess-4k",
    )
    assert seen["max_width"] == 3840
    assert result["screenshot_max_width"] == 3840


def test_desktop_observe_auto_max_width_uses_stitched_desktop(monkeypatch, tmp_path) -> None:
    class Settings:
        data_root = str(tmp_path)

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()

    seen: dict[str, object] = {}

    def fake_capture(path, *, monitor=None, max_width=1920, jpeg_quality=90):
        seen["max_width"] = max_width
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpeg")
        return {"ok": True, "path": str(path), "mime": "image/jpeg", "size": 9}

    monkeypatch.setattr(dc, "list_monitors", lambda: [
        {
            "index": 0, "primary": True, "left": 0, "top": 0,
            "width": 2560, "height": 1440, "right": 2560, "bottom": 1440,
        },
        {
            "index": 1, "primary": False, "left": 2560, "top": 0,
            "width": 2560, "height": 1440, "right": 5120, "bottom": 1440,
        },
    ])
    monkeypatch.setattr(dc, "virtual_desktop_bounds", lambda _monitors=None: {
        "left": 0, "top": 0, "right": 5120, "bottom": 1440,
        "width": 5120, "height": 1440, "monitor_count": 2,
    })
    monkeypatch.setattr(dc, "foreground_window", lambda: {"ok": True, "title": "ArgusAI"})
    monkeypatch.setattr(dc, "list_windows", lambda: [])
    monkeypatch.setattr(dc, "running_programs_summary", lambda: [])
    monkeypatch.setattr(dc, "capture_screenshot", fake_capture)

    result = da.desktop_observe(
        Api(),
        mode="all",
        screenshot=True,
        monitor=-1,
        _session_id="sess-dual",
    )
    assert seen["max_width"] == 3840
    assert result["screenshot_max_width"] == 3840


def test_desktop_tools_in_confirm_sets() -> None:
    for name in (
        "desktop_observe",
        "desktop_notify",
        "desktop_mouse_move",
        "desktop_click",
        "desktop_type",
        "desktop_focus_window",
        "desktop_uia_find",
        "desktop_uia_action",
        "user_profile_apply_update",
    ):
        assert name in _CONFIRM_TOOLS
    for name in (
        "desktop_action_session_start",
        "desktop_action_session_stop",
        "desktop_action_session_status",
    ):
        assert name not in _CONFIRM_TOOLS


def test_desktop_no_auto_approve() -> None:
    assert "desktop_observe" not in _NO_AUTO_APPROVE_TOOLS
    assert "desktop_uia_find" not in _NO_AUTO_APPROVE_TOOLS
    for name in (
        "desktop_mouse_move",
        "desktop_click",
        "desktop_focus_window",
        "desktop_uia_action",
        "user_todo_bulk_delete",
    ):
        assert name in _NO_AUTO_APPROVE_TOOLS
    assert "desktop_action_session_start" not in _NO_AUTO_APPROVE_TOOLS


def test_desktop_action_session_not_required() -> None:
    dc.stop_desktop_action_session("sess-a")
    started = dc.start_desktop_action_session("sess-a", minutes=1)
    assert started.get("required") is False
    assert started.get("deprecated") is True
    assert started.get("active") is False
    status = dc.desktop_session_status("sess-a")
    assert status.get("active") is False
    assert status.get("required") is False
    # Input must work without a timed authorization session.
    result = dc.mouse_click("sess-a", x=1, y=1)
    assert isinstance(result, dict)
    moved = dc.mouse_move("sess-a", x=2, y=2)
    assert isinstance(moved, dict)


def test_desktop_mouse_move_is_registered() -> None:
    from argusai.core.assistant.skills import EXPLICIT_SCHEMAS
    from argusai.core.assistant.tools import build_tool_registry
    from argusai.core.tools import EXECUTORS

    assert "desktop_mouse_move" in EXECUTORS
    assert "desktop_mouse_move" in EXPLICIT_SCHEMAS
    assert _tool_risk("desktop_mouse_move") == "high"

    registry = build_tool_registry(None)
    search = registry.get("tool_search").fn
    found = search(query="", group="desktop", limit=40)
    names = {item["name"] for item in found.get("tools") or []}
    assert "desktop_mouse_move" in names
    assert "desktop_click" in names
    assert "desktop_action_session_start" not in names
    assert "desktop_action_session_stop" not in names
    assert "desktop_action_session_status" not in names


def test_windows_screenshot_fallback_uses_integer_copyfromscreen(monkeypatch, tmp_path) -> None:
    seen: dict[str, str] = {}

    def fake_win_powershell(script: str, *, timeout: int = 60) -> tuple[bool, str]:
        seen["script"] = script
        return True, ""

    monkeypatch.setattr(dc, "_win_powershell", fake_win_powershell)
    dc._capture_all_screens_powershell(tmp_path / "desktop.png")
    script = seen["script"]
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "$g.CopyFromScreen(" in script
    assert "[int]$s.Bounds.X" in script
    assert "[int]$s.Bounds.Y" in script
    assert "$s.Bounds.Location" not in script
    assert "[Drawing.CopyPixelOperation]::Copy" not in script


def test_capture_bbox_pil_uses_all_screens_on_windows(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_grab(**kwargs):
        seen.update(kwargs)
        return object()

    import PIL.ImageGrab as image_grab

    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(image_grab, "grab", fake_grab)

    out = dc._capture_bbox_pil((-1920, 0, 0, 1080))
    assert out is not None
    assert seen.get("all_screens") is True
    assert seen.get("bbox") == (-1920, 0, 0, 1080)


def test_capture_monitors_stitched_pastes_secondary_offset(monkeypatch) -> None:
    from PIL import Image

    monitors = [
        {"index": 0, "primary": True, "left": 0, "top": 0, "width": 100, "height": 80, "right": 100, "bottom": 80},
        {"index": 1, "primary": False, "left": -50, "top": 0, "width": 50, "height": 80, "right": 0, "bottom": 80},
    ]

    def fake_one(bbox):
        left, top, right, bottom = bbox
        w, h = right - left, bottom - top
        color = (255, 0, 0) if left < 0 else (0, 255, 0)
        return Image.new("RGB", (w, h), color)

    monkeypatch.setattr(dc, "_capture_one_bbox", fake_one)
    img = dc._capture_monitors_stitched(monitors)
    assert img is not None
    assert img.size == (150, 80)
    # Secondary (red) should sit at dest x=0; primary (green) at x=50.
    assert img.getpixel((10, 10)) == (255, 0, 0)
    assert img.getpixel((60, 10)) == (0, 255, 0)


def test_capture_screenshot_prefers_stitch_for_multi_monitor(monkeypatch, tmp_path) -> None:
    from PIL import Image

    monitors = [
        {"index": 0, "primary": True, "left": 0, "top": 0, "width": 40, "height": 30, "right": 40, "bottom": 30},
        {"index": 1, "primary": False, "left": 40, "top": 0, "width": 40, "height": 30, "right": 80, "bottom": 30},
    ]
    monkeypatch.setattr(dc, "list_monitors", lambda: monitors)
    monkeypatch.setattr(dc.sys, "platform", "win32")
    monkeypatch.setattr(dc, "_ensure_win_dpi_awareness", lambda: None)
    monkeypatch.setattr(
        dc,
        "_capture_monitors_stitched",
        lambda mons: Image.new("RGB", (80, 30), (12, 34, 56)),
    )
    monkeypatch.setattr(dc, "_capture_one_bbox", lambda bbox: (_ for _ in ()).throw(AssertionError("should stitch")))
    monkeypatch.setattr(dc, "_capture_all_screens_powershell", lambda path: (False, "unused"))

    out = dc.capture_screenshot(tmp_path / "desk.jpg", monitor=None)
    assert out["ok"] is True
    assert out["capture_backend"] == "win_stitch"
    assert (tmp_path / "desk.jpg").exists()


def test_desktop_observe_hwnd_uses_window_capture(monkeypatch, tmp_path) -> None:
    class Settings:
        data_root = str(tmp_path)

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()

    seen: dict[str, object] = {}

    def fake_window_capture(path, *, hwnd, max_width=1920, jpeg_quality=82):
        seen["hwnd"] = hwnd
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-window-jpeg")
        return {"ok": True, "path": str(path), "mime": "image/jpeg", "size": path.stat().st_size, "hwnd": hwnd}

    monkeypatch.setattr(dc, "list_monitors", lambda: [{
        "index": 0, "primary": True, "left": 0, "top": 0, "width": 100, "height": 80, "right": 100, "bottom": 80,
    }])
    monkeypatch.setattr(dc, "foreground_window", lambda: {"ok": True, "title": "ArgusAI"})
    monkeypatch.setattr(dc, "capture_window_screenshot", fake_window_capture)
    monkeypatch.setattr(dc, "capture_screenshot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("full screen")))

    result = da.desktop_observe(Api(), mode="foreground", screenshot=True, hwnd=4242, _session_id="sess-a")
    assert seen["hwnd"] == 4242
    assert result["attached_to_model"] is True


def test_focus_window_rejects_zombie_hwnd(monkeypatch) -> None:
    class FakeUser32:
        def IsWindow(self, hwnd):  # noqa: ANN001
            return True

        def ShowWindow(self, *a):  # noqa: ANN001
            return True

        def GetForegroundWindow(self):
            return 0

        def GetWindowThreadProcessId(self, *a):  # noqa: ANN001
            return 1

        def AttachThreadInput(self, *a):  # noqa: ANN001
            return False

        def BringWindowToTop(self, *a):  # noqa: ANN001
            return True

        def SetForegroundWindow(self, *a):  # noqa: ANN001
            return True

        def GetWindowRect(self, hwnd, rect):  # noqa: ANN001
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 10, 10
            return True

    class FakeKernel:
        def GetCurrentThreadId(self):
            return 9

    class FakeWindll:
        user32 = FakeUser32()
        kernel32 = FakeKernel()

    class FakeCtypes:
        windll = FakeWindll()

        class c_void_p:
            def __init__(self, v):  # noqa: ANN001
                self.value = v

    class FakeWintypes:
        class RECT:
            def __init__(self):
                self.left = self.top = self.right = self.bottom = 0

    monkeypatch.setattr(dc, "_win_imports", lambda: (FakeCtypes(), FakeWintypes, True))
    monkeypatch.setattr(dc, "_hwnd_process_id", lambda hwnd: 123)
    monkeypatch.setattr(dc, "_win_pid_alive", lambda pid: False)
    out = dc.focus_window(99)
    assert out["ok"] is False
    assert "zombie" in (out.get("error") or "")


def test_desktop_observe_attaches_screenshot_to_model(monkeypatch, tmp_path) -> None:
    class Settings:
        data_root = str(tmp_path)

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()

    monkeypatch.setattr(dc, "list_monitors", lambda: [{
        "index": 0,
        "primary": True,
        "left": 0,
        "top": 0,
        "width": 100,
        "height": 80,
        "right": 100,
        "bottom": 80,
    }])
    monkeypatch.setattr(dc, "foreground_window", lambda: {"ok": True, "title": "ArgusAI"})

    def fake_capture(path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-jpeg")
        return {"ok": True, "path": str(path), "mime": "image/jpeg", "size": path.stat().st_size}

    monkeypatch.setattr(dc, "capture_screenshot", fake_capture)
    result = da.desktop_observe(Api(), mode="foreground", screenshot=True, _session_id="sess-a")
    assert result["screenshot_path"].startswith("_media/")
    assert result["attached_to_model"] is True
    assert result["_assistant_attachments"][0]["type"] == "image"
    assert result["_assistant_attachments"][0]["data_base64"]


def test_windows_hotkey_supports_launcher_keys(monkeypatch) -> None:
    class User32:
        def keybd_event(self, *args) -> None:
            return None

    class Windll:
        user32 = User32()

    class Ctypes:
        windll = Windll()

    monkeypatch.setattr(dc, "_win_imports", lambda: (Ctypes(), object(), True))
    ok, err = dc._send_win_hotkey(["win"])
    assert ok is True
    assert err == ""
    ok, err = dc._send_win_hotkey(["enter"])
    assert ok is True
    assert err == ""
    ok, err = dc._send_win_hotkey(["definitely-not-a-key"])
    assert ok is False
    assert "unsupported" in err


def test_normalize_hotkey_keys_does_not_split_named_keys() -> None:
    assert dc.normalize_hotkey_keys("enter") == ["enter"]
    assert dc.normalize_hotkey_keys("down") == ["down"]
    assert dc.normalize_hotkey_keys("escape") == ["escape"]
    assert dc.normalize_hotkey_keys("ctrl+a") == ["ctrl", "a"]
    assert dc.normalize_hotkey_keys("ctrl,a") == ["ctrl", "a"]
    assert dc.normalize_hotkey_keys(["enter"]) == ["enter"]
    assert dc.normalize_hotkey_keys(["ctrl", "a"]) == ["ctrl", "a"]
    assert dc.normalize_hotkey_keys(["ctrl+a"]) == ["ctrl", "a"]
    # Must NOT become e,n,t,e,r
    assert dc.normalize_hotkey_keys("enter") != list("enter")


def test_desktop_hotkey_accepts_string_and_key_alias(monkeypatch) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(dc, "_require_action_session", lambda _sid: None)
    monkeypatch.setattr(
        dc,
        "_send_win_hotkey",
        lambda keys: (seen.update({"keys": list(keys)}) or (True, "")),
    )
    monkeypatch.setattr(dc.sys, "platform", "win32")

    out = da.desktop_hotkey(object(), keys="enter", _session_id="s1")
    assert out["ok"] is True
    assert out["keys"] == ["enter"]
    assert seen["keys"] == ["enter"]

    out = da.desktop_hotkey(object(), key="ctrl+a", _session_id="s1")
    assert out["ok"] is True
    assert out["keys"] == ["ctrl", "a"]


def test_desktop_notify_uses_environment_for_windows_message(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class Session:
        _current_turn_source = ""

    class Settings:
        assistant_desktop_notifications = True

    class SettingsMgr:
        def get(self) -> Settings:
            return Settings()

    class Api:
        settings_mgr = SettingsMgr()
        _assistant_sessions = {"gui-session": Session()}

        @staticmethod
        def _app_needs_attention() -> bool:
            return True

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(da.sys, "platform", "win32")
    monkeypatch.setattr(da.subprocess, "run", fake_run)
    result = da.desktop_notify(
        Api(),
        title="Done",
        message='x"; Remove-Item C:\\',
        level="success",
        _session_id="gui-session",
    )

    assert result == {"ok": True, "sent": True, "title": "Done", "level": "success"}
    assert 'x"; Remove-Item C:\\' not in " ".join(seen["args"])
    assert seen["kwargs"]["env"]["ARGUSAI_NOTIFY_MESSAGE"] == 'x"; Remove-Item C:\\'


def test_desktop_notify_suppresses_channel_turns_and_focused_gui() -> None:
    class Session:
        def __init__(self, source: str) -> None:
            self._current_turn_source = source

    class Api:
        def __init__(self, source: str, background: bool) -> None:
            self._assistant_sessions = {"session": Session(source)}
            self._background = background

        def _app_needs_attention(self) -> bool:
            return self._background

    channel = da.desktop_notify(
        Api("channel", True),
        message="done",
        _session_id="session",
    )
    focused = da.desktop_notify(
        Api("", False),
        message="done",
        _session_id="session",
    )

    assert channel == {"ok": True, "sent": False, "reason": "channel_turn"}
    assert focused == {"ok": True, "sent": False, "reason": "app_focused"}
