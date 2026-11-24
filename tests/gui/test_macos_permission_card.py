"""Permission cards must reference the running app, never a generated shim."""

from __future__ import annotations

import plistlib
import sys
from types import SimpleNamespace

import pytest

from argusai.gui import macos_permission_card as card


def bundle(tmp_path, *, name="Renamed ArgusAI.app", identifier="com.argusai.app"):
    root = tmp_path / name
    binary = root / "Contents/MacOS/ArgusAI"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fixture only, never executed")
    (root / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": binary.name,
        "CFBundlePackageType": "APPL",
        "CFBundleIdentifier": identifier,
    }))
    return root, binary


def test_target_is_the_actual_bundle_even_after_rename(tmp_path, monkeypatch):
    root, binary = bundle(tmp_path)
    monkeypatch.setattr(sys, "executable", str(binary))
    assert card.running_app_bundle() == root


def test_executable_symlink_resolves_to_the_real_bundle(tmp_path, monkeypatch):
    root, binary = bundle(tmp_path)
    link = tmp_path / "launcher"
    link.symlink_to(binary)
    monkeypatch.setattr(sys, "executable", str(link))
    assert card.running_app_bundle() == root


@pytest.mark.parametrize("mode", ["source", "python_app", "missing_plist", "wrong_executable", "invalid_plist"])
def test_does_not_invent_or_guess_permission_target(tmp_path, monkeypatch, mode):
    root, binary = bundle(tmp_path, identifier="org.python.python" if mode == "python_app" else "com.argusai.app")
    if mode == "source":
        binary = tmp_path / "venv/bin/python"
    elif mode == "missing_plist":
        (root / "Contents/Info.plist").unlink()
    elif mode == "wrong_executable":
        binary = root / "Contents/MacOS/unrelated-helper"
        binary.write_bytes(b"fixture")
    elif mode == "invalid_plist":
        (root / "Contents/Info.plist").write_bytes(plistlib.dumps(["not an app manifest"]))
    monkeypatch.setattr(sys, "executable", str(binary))
    assert card.running_app_bundle() is None


def test_unsupported_host_and_source_run_do_not_open_settings(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(card, "running_app_bundle", lambda: pytest.fail("must not inspect a non-Mac host"))
    assert card.show_permission_card("accessibility")["ok"] is False
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(card, "running_app_bundle", lambda: None)
    assert card.show_permission_card("screen_capture")["error_code"] == "macos_app_bundle_unavailable"


def test_native_card_and_settings_receive_only_current_bundle(tmp_path, monkeypatch):
    from argusai.core.workspace import darwin_permissions

    root, binary = bundle(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(binary))
    calls = []
    monkeypatch.setattr(card, "_show_on_main", lambda target, labels: calls.append((target, labels)))
    monkeypatch.setattr(darwin_permissions, "open_privacy_settings", lambda pane: {"ok": True, "pane": pane})
    result = card.show_permission_card("screen_capture", {"title": "拖拽授权", "path": "/wrong.app"})
    assert result == {"ok": True, "pane": "screen_capture", "app_name": root.name, "card_shown": True}
    assert calls[0][0] == root
    assert calls[0][1]["title"] == "拖拽授权"
    assert "path" not in calls[0][1]
    assert card.show_permission_card("unknown")["ok"] is False
    assert len(calls) == 1


def test_native_drag_writes_the_app_file_and_cannot_move_it(monkeypatch):
    class ImageView:
        def __init_subclass__(cls, **kwargs):
            pass

        def bounds(self):
            return (0, 0, 72, 72)

        def image(self):
            return "app-icon"

        def beginDraggingSessionWithItems_event_source_(self, items, event, source):
            self.drag = (items, event, source)

    class DragItem:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithPasteboardWriter_(self, writer):
            self.writer = writer
            return self

        def setDraggingFrame_contents_(self, frame, image):
            self.preview = (frame, image)

    revealed = []
    appkit = SimpleNamespace(NSImageView=ImageView, NSDraggingItem=DragItem, NSDragOperationCopy=1,
                            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(
                                activateFileViewerSelectingURLs_=revealed.append)))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(protocolNamed=lambda name: name))
    monkeypatch.setattr(card, "_icon_class", None)
    view = card._permission_icon_class()()
    view.app_url = object()  # The NSURL file reference; the image is preview only.
    event = object()
    view.mouseDown_(event)
    assert view.drag[0][0].writer is view.app_url
    assert view.drag[0][0].preview == ((0, 0, 72, 72), "app-icon")
    assert view.drag[1] is event
    assert view.drag[2] is view
    assert view.draggingSession_sourceOperationMaskForDraggingContext_(None, None) == 1
    assert view.ignoreModifierKeysForDraggingSession_(None) is True
    view.revealInFinder_(None)
    assert revealed == [[view.app_url]]
