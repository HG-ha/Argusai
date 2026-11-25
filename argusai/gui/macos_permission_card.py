"""A native floating file-drag source for macOS permission setup.

WebView image/HTML drags do not reliably produce a file pasteboard item. Keep
this small AppKit surface separate from permission checks and the titlebar.
Only the running app bundle is offered; no placeholder app is generated.
"""

from __future__ import annotations

import logging
import plistlib
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
_panel: Any = None
_icon_class: Any = None


def running_app_bundle() -> Path | None:
    """Find the actual .app containing this process, including renamed apps."""
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix.lower() != ".app":
            continue
        try:
            with (parent / "Contents" / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            if not isinstance(info, dict) or info.get("CFBundlePackageType") != "APPL":
                continue
            main = parent / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or "")
            if main.resolve() != executable or not main.is_file():
                continue
            # A source checkout can run inside Python.app. Its responsible TCC
            # identity can be Terminal/IDE, so do not guess a permission target.
            if str(info.get("CFBundleIdentifier") or "").startswith("org.python."):
                return None
            return parent
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return None


def running_app_identity() -> dict[str, Any]:
    """Identify the current process, never a same-name installed copy."""
    if sys.platform != "darwin":
        return {"available": False}
    bundle = running_app_bundle()
    if bundle is None:
        return {"available": False}
    try:
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        if not isinstance(info, dict):
            return {"available": False}
        return {"available": True, "name": bundle.name, "path": str(bundle),
                "bundle_id": str(info.get("CFBundleIdentifier") or ""),
                "version": str(info.get("CFBundleShortVersionString") or "")}
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {"available": False}


def _permission_icon_class() -> Any:
    global _icon_class
    if _icon_class is not None:
        return _icon_class
    import AppKit
    import objc

    class ArgusPermissionDragIcon(AppKit.NSImageView, protocols=[objc.protocolNamed("NSDraggingSource")]):
        def mouseDown_(self, event: Any) -> None:
            # NSURL writes public.file-url, not a PNG, bookmark or text path.
            item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(self.app_url)
            item.setDraggingFrame_contents_(self.bounds(), self.image())
            self.beginDraggingSessionWithItems_event_source_([item], event, self)

        def draggingSession_sourceOperationMaskForDraggingContext_(self, session: Any, context: Any) -> int:
            return AppKit.NSDragOperationCopy

        def ignoreModifierKeysForDraggingSession_(self, session: Any) -> bool:
            return True

        def revealInFinder_(self, sender: Any) -> None:
            AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([self.app_url])

    _icon_class = ArgusPermissionDragIcon
    return _icon_class


def _show_on_main(bundle: Path, labels: dict[str, str]) -> None:
    global _panel
    import AppKit
    from Foundation import NSURL

    if _panel is not None:
        _panel.close()
    style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
             | AppKit.NSWindowStyleMaskUtilityWindow)
    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, 430, 370), style, AppKit.NSBackingStoreBuffered, False,
    )
    panel.setTitle_(labels["title"])
    panel.setReleasedWhenClosed_(False)
    panel.setHidesOnDeactivate_(False)
    panel.setLevel_(AppKit.NSFloatingWindowLevel)
    content = panel.contentView()

    def label(text: str, frame: Any, size: float, *, selectable: bool = False, lines: int = 3) -> None:
        field = AppKit.NSTextField.alloc().initWithFrame_(frame)
        field.setStringValue_(text)
        field.setEditable_(False)
        field.setSelectable_(selectable)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setAlignment_(AppKit.NSTextAlignmentCenter)
        field.setFont_(AppKit.NSFont.systemFontOfSize_(size))
        field.setTextColor_(AppKit.NSColor.labelColor())
        field.setMaximumNumberOfLines_(lines)
        field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
        content.addSubview_(field)

    label(labels["hint"], AppKit.NSMakeRect(18, 310, 394, 45), 12)
    icon = _permission_icon_class().alloc().initWithFrame_(AppKit.NSMakeRect(179, 230, 72, 72))
    icon.app_url = NSURL.fileURLWithPath_(str(bundle))
    icon.setImage_(AppKit.NSWorkspace.sharedWorkspace().iconForFile_(str(bundle)))
    icon.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
    content.addSubview_(icon)
    label(bundle.name, AppKit.NSMakeRect(18, 202, 394, 22), 13)
    label(str(bundle), AppKit.NSMakeRect(18, 148, 394, 48), 11, selectable=True)
    label(labels["repair"], AppKit.NSMakeRect(18, 57, 394, 83), 12, lines=5)

    reveal = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(75, 16, 280, 30))
    reveal.setTitle_(labels["reveal"])
    reveal.setBezelStyle_(AppKit.NSBezelStyleRounded)
    reveal.setTarget_(icon)
    reveal.setAction_("revealInFinder:")
    content.addSubview_(reveal)

    screen = AppKit.NSScreen.mainScreen()
    if screen is not None:
        area = screen.visibleFrame()
        panel.setFrameTopLeftPoint_(AppKit.NSMakePoint(area.origin.x + 24,
                                                      area.origin.y + area.size.height - 80))
    else:
        panel.center()
    _panel = panel  # Retain the native views after this bridge call returns.
    panel.orderFrontRegardless()


def show_permission_card(pane: str, labels: dict[str, Any] | None = None) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"ok": False, "error": "not macOS"}
    if pane not in {"accessibility", "screen_capture", "listen_event"}:
        return {"ok": False, "error": "unknown privacy pane"}
    bundle = running_app_bundle()
    if bundle is None:
        return {"ok": False, "error_code": "macos_app_bundle_unavailable"}
    raw = labels if isinstance(labels, dict) else {}
    text = {key: str(raw.get(key) or fallback)[:400] for key, fallback in {
        "title": "Add app permissions",
        "hint": "Drag this app into the permissions list in System Settings, then enable it.",
        "reveal": "Show in Finder",
        "repair": "Already enabled but still unavailable? Remove the old app entry in System Settings, then add this exact app and enable it. Quit other copies, and restart this app if macOS asks.",
    }.items()}
    done = threading.Event()
    outcome = {"ok": False, "cancelled": False}

    def invoke() -> None:
        if outcome["cancelled"]:
            return
        try:
            _show_on_main(bundle, text)
            outcome["ok"] = True
        except Exception:
            log.exception("macOS permission drag card failed")
        finally:
            done.set()

    try:
        if threading.current_thread() is threading.main_thread():
            invoke()
        else:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(invoke)
            if not done.wait(3.0):
                outcome["cancelled"] = True
    except Exception:
        log.exception("macOS permission card dispatch failed")
    if not outcome["ok"]:
        return {"ok": False, "error_code": "macos_permission_card_failed"}
    from argusai.core.workspace.darwin_permissions import open_privacy_settings
    result = open_privacy_settings(pane)
    return {**result, "app_name": bundle.name, "app_path": str(bundle), "card_shown": True}
