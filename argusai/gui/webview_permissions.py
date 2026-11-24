"""Media permission hooks for the desktop shell.

Native Chromium permission bars cannot be styled from Vue. On Win32 we
subscribe to ``CoreWebView2.PermissionRequested`` and allow microphone
access for this app's origin so ``getUserMedia`` works without repeated
prompts. Disable via ``ARGUSAI_AUTO_GRANT_MIC=0``.

All CoreWebView2 access MUST run on the WinForms UI thread.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("argusai.gui")

_AUTO_GRANT_MIC = os.environ.get("ARGUSAI_AUTO_GRANT_MIC", "1").lower() not in {
    "0",
    "false",
    "no",
    "",
}


def _auto_grant_mic_enabled() -> bool:
    return _AUTO_GRANT_MIC


def _run_on_ui_thread(control: Any, fn: Any) -> None:
    """Marshal ``fn`` onto the WinForms control's UI thread."""
    if control is None:
        return
    try:
        from System.Windows.Forms import MethodInvoker  # type: ignore[import-untyped]

        if control.InvokeRequired:
            control.Invoke(MethodInvoker(fn))
        else:
            fn()
    except Exception as exc:
        log.warning("WebView2 UI-thread dispatch failed: %s", exc)


def _bind_permission_handler(core: Any) -> None:
    if core is None or getattr(core, "_argusai_mic_permission_bound", False):
        return

    from Microsoft.Web.WebView2.Core import (  # type: ignore[import-untyped]
        CoreWebView2PermissionKind,
        CoreWebView2PermissionState,
    )

    def on_permission_requested(_sender: Any, args: Any) -> None:
        try:
            if args.PermissionKind == CoreWebView2PermissionKind.Microphone:
                args.State = CoreWebView2PermissionState.Allow
        except Exception as exc:
            log.warning("PermissionRequested handler failed: %s", exc)

    core.PermissionRequested += on_permission_requested
    core._argusai_mic_permission_bound = True  # type: ignore[attr-defined]
    log.info("WebView2 microphone permission auto-grant enabled")


def _setup_on_webview_control(webview_ctl: Any) -> None:
    """Hook initialization on the UI thread (safe for CoreWebView2)."""
    if webview_ctl is None or getattr(webview_ctl, "_argusai_perm_setup_done", False):
        return
    webview_ctl._argusai_perm_setup_done = True  # type: ignore[attr-defined]

    def on_init_completed(_sender: Any, args: Any) -> None:
        if not getattr(args, "IsSuccess", True):
            log.warning(
                "WebView2 init failed; mic auto-grant skipped: %s",
                getattr(args, "InitializationException", None),
            )
            return
        try:
            _bind_permission_handler(webview_ctl.CoreWebView2)
        except Exception as exc:
            log.warning("WebView2 permission bind on init failed: %s", exc)

    webview_ctl.CoreWebView2InitializationCompleted += on_init_completed

    try:
        _bind_permission_handler(webview_ctl.CoreWebView2)
    except Exception:
        # Not ready yet — on_init_completed will run when it is.
        pass


def _resolve_webview_control(window: Any) -> Any | None:
    from webview.platforms.winforms import BrowserView  # type: ignore[import-untyped]

    uid = getattr(window, "uid", None)
    if not uid:
        return None
    instance = BrowserView.instances.get(uid)
    if instance is None:
        return None
    browser = getattr(instance, "browser", None)
    return getattr(browser, "webview", None) if browser else None


def bind_webview_media_permissions(window: Any) -> None:
    """Schedule permission hooks on the WinForms UI thread."""
    if sys.platform == "darwin":
        _bind_cocoa_media_permissions(window)
        return
    if sys.platform != "win32":
        return
    if not _auto_grant_mic_enabled():
        log.info("WebView2 microphone auto-grant disabled (ARGUSAI_AUTO_GRANT_MIC=0)")
        return

    webview_ctl = _resolve_webview_control(window)
    if webview_ctl is None:
        log.warning("WebView2 media permission bind skipped: webview control not ready")
        return

    _run_on_ui_thread(webview_ctl, lambda: _setup_on_webview_control(webview_ctl))


def _origin_tuple(url: str) -> tuple[str, str, int] | None:
    try:
        value = urlsplit(url)
        if value.scheme not in {"http", "https"} or value.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return None
        return value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80)
    except (ValueError, TypeError):
        return None


def _trusted_microphone_request(expected: Any, origin: Any, frame: Any) -> bool:
    if expected is None or not frame.isMainFrame():
        return False
    frame_origin = frame.securityOrigin()

    def origin_key(value: Any) -> tuple[str, str, int]:
        scheme = str(value.protocol())
        return scheme, str(value.host()), int(value.port()) or (443 if scheme == "https" else 80)

    return origin_key(origin) == expected == origin_key(frame_origin)


_cocoa_media_delegate_class: Any = None


def _media_delegate_class() -> Any:
    global _cocoa_media_delegate_class
    if _cocoa_media_delegate_class is not None:
        return _cocoa_media_delegate_class
    import objc
    import WebKit
    from webview.platforms.cocoa import BrowserView

    class ArgusMediaDelegate(BrowserView.BrowserDelegate, protocols=[objc.protocolNamed("WKUIDelegate")]):
        def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
            self, webview, origin, frame, capture_type, decision,
        ):
            from argusai.core.workspace.darwin_permissions import microphone_status

            # This skips only WebKit's per-origin prompt AFTER native TCC
            # consent. Remote pages, iframes and camera capture never inherit it.
            allowed = (
                capture_type == WebKit.WKMediaCaptureTypeMicrophone
                and _trusted_microphone_request(self.app_origin, origin, frame)
                and microphone_status() == "granted"
            )
            decision(WebKit.WKPermissionDecisionGrant if allowed else WebKit.WKPermissionDecisionDeny)

    _cocoa_media_delegate_class = ArgusMediaDelegate
    return ArgusMediaDelegate


def _bind_cocoa_media_permissions(window: Any) -> None:
    from PyObjCTools import AppHelper

    def install() -> None:
        try:
            from webview.platforms.cocoa import BrowserView

            view = BrowserView.instances.get(getattr(window, "uid", ""))
            if view is None or getattr(view, "_argusai_media_delegate", None) is not None:
                return
            origin = _origin_tuple(str(getattr(window, "real_url", "") or ""))
            if origin is None:
                return
            delegate = _media_delegate_class().alloc().init()
            delegate.app_origin = origin
            view._argusai_media_delegate = delegate
            view.webview.setUIDelegate_(delegate)
        except Exception:
            log.warning("WKWebView microphone permission bind failed", exc_info=True)

    AppHelper.callAfter(install)
