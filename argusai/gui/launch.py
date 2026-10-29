"""Entry point: bootstrap the webview, decide how to serve the SPA, hand off to pywebview.

Three host modes are supported, tried in order:

1. **Vite dev server** — if ``argusai/webui/node_modules/`` exists, we
   spawn ``npx vite`` and point pywebview at it. Best DX (HMR, source
   maps) and what contributors use day to day.
2. **Built bundle** — if ``argusai/webui/dist/`` exists (the wheel/installer
   ships this), we serve ``dist/`` over a tiny stdlib HTTP server.
3. **Raw source** — fall back to serving ``argusai/webui/`` directly.
   Mainly useful while bisecting the build pipeline.

The window itself is created frameless on Windows so the Vue side can
draw a custom title bar (drag region wired via the pywebview
``DRAG_REGION_SELECTOR`` setting).
"""

from __future__ import annotations

import os
import importlib.util
import json
import socket
import subprocess
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import webview

from argusai.core.infra.logging_setup import LoggingSettings, current_log_dir, init_logging, is_initialised
from argusai.core.infra.win_stdio import (
    ensure_valid_stdio,
    hide_self_allocated_console,
    quiet_subprocess_kwargs,
)

from . import Api
from ._helpers import WEBUI_DIR, resource_root
from .webview_permissions import bind_webview_media_permissions
from .window_geometry import resolve_startup_geometry


def _bind_native_file_drop(window: webview.Window) -> None:
    """Forward pywebview's native drop paths to the Vue input box.

    Browser ``DataTransfer`` intentionally hides absolute local paths.
    pywebview enriches Python-side DOM drop events with
    ``pywebviewFullPath``; forwarding those through a CustomEvent gives the
    frontend the real file/folder path without asking the user to choose it
    again.
    """
    import logging

    log = logging.getLogger("argusai.gui")
    try:
        from webview.dom import DOMEventHandler
    except Exception as exc:
        log.warning("native_file_drop unavailable: %s", exc)
        return

    def _on_drop(event):  # type: ignore[no-untyped-def]
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths: list[str] = []
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("pywebviewFullPath") or "").strip()
            if path:
                paths.append(path)
        if not paths:
            return None
        payload = json.dumps({"paths": paths}, ensure_ascii=False)
        window.evaluate_js(
            "window.dispatchEvent(new CustomEvent('argusai:native-file-drop', "
            f"{{ detail: {payload} }}));"
        )
        return None

    try:
        # Drop only. dragenter/dragover used to marshal the full native event
        # into Python on every child the cursor crossed — that stalls the
        # WebView while the user is still dragging.
        window.dom.document.events.drop += DOMEventHandler(_on_drop, True, True)
    except Exception as exc:
        log.warning("native_file_drop bind failed: %s", exc)


def _ensure_logging_started() -> None:
    """Idempotently initialise logging when launching the GUI directly.

    ``argusai.__main__`` calls this before ``gui.main``, but the package
    can also be entered via ``argusai-gui`` console script which lands
    here directly. We re-run the bootstrap defensively; init_logging is
    safe to call multiple times.
    """

    if is_initialised():
        return
    try:
        from argusai.core.settings import SettingsManager

        settings = SettingsManager().get()
        init_logging(settings.logging, data_root=settings.data_root)
    except Exception:
        init_logging(LoggingSettings())


def _install_pywebview_win32_helper() -> None:
    """Install pywebview's WinForms helper when frozen by Nuitka.

    Nuitka 4.1's pywebview plugin excludes ``webview.platforms.win32`` even
    though pywebview's WinForms backend imports it at runtime. The build script
    copies the helper as a data file; this shim registers it as a module before
    ``webview.start()`` asks pywebview to initialise the GUI backend.
    """
    if sys.platform != "win32":
        return
    module_name = "webview.platforms.win32"
    if module_name in sys.modules:
        return
    candidates = [
        Path(sys.executable).resolve().parent / "webview" / "platforms" / "win32.py",
    ]
    try:
        candidates.append(Path(webview.__file__).resolve().parent / "platforms" / "win32.py")
    except Exception:
        pass
    source_path = next((p for p in candidates if p.exists()), None)
    if source_path is None:
        return
    try:
        import webview.platforms as platforms

        spec = importlib.util.spec_from_file_location(module_name, source_path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        setattr(platforms, "win32", module)
    except Exception:
        sys.modules.pop(module_name, None)


def _find_free_port() -> int:
    """Bind to port 0 to let the kernel pick a free one, then return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_vite_dev(webui_dir: Path) -> tuple[str | None, subprocess.Popen | None]:
    """Try to start the Vite dev server. Returns ``(url, process)`` or ``(None, None)``.

    Invokes ``node node_modules/vite/bin/vite.js`` directly (not ``npx``) so
    Windows ``CREATE_NO_WINDOW`` launches still find the binary. No-ops when
    Node / vite are missing or the port never binds within ~10 s.
    """
    import logging
    import shutil
    import time as _time

    log = logging.getLogger("argusai.gui")
    vite_js = webui_dir / "node_modules" / "vite" / "bin" / "vite.js"
    node = shutil.which("node")
    if not vite_js.exists() or not node:
        log.info(
            "gui.boot vite skipped node=%s vite_js=%s",
            bool(node),
            vite_js.exists(),
        )
        return None, None
    port = _find_free_port()
    try:
        proc = subprocess.Popen(
            [node, str(vite_js), "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
            cwd=str(webui_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **quiet_subprocess_kwargs(),
        )
        url = f"http://127.0.0.1:{port}/"
        for _ in range(100):  # up to ~10 s of warm-up
            if proc.poll() is not None:
                log.warning("gui.boot vite exited early code=%s", proc.returncode)
                return None, None
            _time.sleep(0.1)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return url, proc
            except (ConnectionRefusedError, OSError):
                continue
        proc.kill()
        log.warning("gui.boot vite bind timeout port=%s", port)
        return None, None
    except FileNotFoundError:
        return None, None


def main() -> None:
    """Boot the GUI. Idempotent entry point used by ``argusai-gui`` script."""
    # Detach first so later stdio repair binds to NUL, not the leftover CMD.
    hide_self_allocated_console()
    ensure_valid_stdio()
    _ensure_logging_started()
    import logging

    log = logging.getLogger("argusai.gui")
    from argusai.gui.tray import TrayController, start_in_tray_requested

    tray = TrayController()
    if not tray.claim_or_activate():
        log.info("gui.boot handed off to the running instance")
        return
    start_hidden = start_in_tray_requested()
    log.info("gui.boot launching webview log_dir=%s tray=%s", current_log_dir(), start_hidden)
    _install_pywebview_win32_helper()
    api = Api()
    api._quit_requested = False
    is_windows = sys.platform == "win32"

    # The frontend marks the drag-handle element with this class so pywebview
    # can route OS-level window drags through it (frameless mode only).
    webview.settings["DRAG_REGION_SELECTOR"] = ".titlebar-drag"

    httpd: HTTPServer | None = None
    vite_proc: subprocess.Popen | None = None

    # Prefer Vite against the *source* webui tree (``WEBUI_DIR``), not
    # ``resource_root()`` — the latter returns ``webui/dist`` when a build
    # exists, which has no ``node_modules`` and silently skipped HMR. That
    # made ``uv run argusai`` keep serving a stale static bundle after Vue
    # edits until the next manual ``npm run build``.
    vite_url, vite_proc = _start_vite_dev(WEBUI_DIR)
    if vite_url:
        url = vite_url
        log.info("gui.boot frontend=vite url=%s", url)
    else:
        serve_dir = resource_root()
        port = _find_free_port()

        class _Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
                super().__init__(*a, directory=str(serve_dir), **k)

            def log_message(self, *a):  # type: ignore[no-untyped-def]  # silence access log
                pass

        httpd = HTTPServer(("127.0.0.1", port), _Handler)
        Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/index.html"
        log.info("gui.boot frontend=static dir=%s url=%s", serve_dir, url)

    geom = resolve_startup_geometry(api.settings_mgr.get())
    window = webview.create_window(
        title="ArgusAI",
        url=url,
        js_api=api,
        width=geom.width,
        height=geom.height,
        x=geom.x,
        y=geom.y,
        min_size=(1000, 680),
        frameless=is_windows,
        resizable=True,
        easy_drag=False,
        maximized=geom.maximized,
        hidden=start_hidden,
        focus=not start_hidden,
        # Match the dark theme's ``--bg`` (#1e1e1e) so that the brief window
        # areas the WebView hasn't painted yet — visible during drag, resize,
        # or before the first frame — don't flash the platform's default
        # white. The frontend overrides per theme but the host window
        # background is what's visible during those repaint gaps.
        # See webui/src/assets/theme.css ``--bg``.
        background_color="#1e1e1e",
    )
    api._window = window
    api._window_maximized = geom.maximized

    # Wire the backend->frontend live-push bus so panels can update without
    # polling / manual refresh. Best-effort: if this fails the UI falls back to
    # its slow polls. Core subsystems stay UI-agnostic by pushing through
    # injected callbacks (see ``set_ui_notify`` installers below).
    try:
        from argusai.gui.live_push import LIVE_PUSH, push_ui

        LIVE_PUSH.bind(window)
        from argusai.core.assistant import agent_threads as _agent_threads

        def _agent_ui_notify(channel: str, payload: dict | None = None) -> None:
            push_ui(channel, payload)
            # A child-Agent change on a ``team-<id>`` lead session also animates
            # the Team grid / sidebar status via the ``teams`` channel.
            try:
                sid = str((payload or {}).get("session_id") or "")
                if sid.startswith("team-"):
                    api.notify_team_changed_for_session(sid)
            except Exception:
                pass

        _agent_threads.set_ui_notify(_agent_ui_notify)
        _agent_threads.set_team_room_hooks(
            context_provider=api.team_member_room_context,
            output_publisher=api.publish_team_member_output,
        )
        from argusai.core.browser import browser_pool as _browser_pool

        _browser_pool.set_browser_ui_notify(push_ui)
        from argusai.core import tasks as _tasks

        _tasks.set_ui_notify(push_ui)
        from argusai.core.security import proxy_manager as _proxy_manager

        _proxy_manager.set_ui_notify(push_ui)
        from argusai.core.reverse import reverse_jobs as _reverse_jobs

        _reverse_jobs.set_ui_notify(push_ui)
        from argusai.core.workspace import user_todos as _user_todos

        _user_todos.set_ui_notify(push_ui)
        from argusai.core.workspace import knowledge as _knowledge

        _knowledge.set_ui_notify(push_ui)
    except Exception:
        log.debug("gui.boot live-push bind failed", exc_info=True)

    shutdown_worker: Thread | None = None

    def _run_shutdown_cleanup() -> None:
        try:
            api.shutdown_background_services()
        except Exception:
            log.debug("gui shutdown cleanup failed", exc_info=True)

    def _show_from_tray() -> None:
        from argusai.gui.tray import run_on_winforms_ui

        def _show() -> None:
            try:
                api.show_window_from_tray()
            except Exception:
                log.debug("gui.tray show failed", exc_info=True)

        run_on_winforms_ui(_show)

    def _quit_from_tray() -> None:
        from argusai.gui.tray import run_on_winforms_ui

        def _quit() -> None:
            try:
                api.quit_application()
            except Exception:
                log.debug("gui.tray quit failed", exc_info=True)

        run_on_winforms_ui(_quit)

    api._stop_tray = tray.stop
    api._tray = tray
    tray.bind(show=_show_from_tray, quit=_quit_from_tray)

    def _on_window_closing() -> bool:
        nonlocal shutdown_worker
        if not getattr(api, "_quit_requested", False) and api.close_to_tray_enabled():
            try:
                api.hide_window_to_tray()
            except Exception:
                log.debug("gui.close-to-tray hide failed", exc_info=True)
            return False
        try:
            api.persist_window_geometry()
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass
        # Tearing down channels, browser hubs and agent subprocesses takes a
        # couple of seconds. pywebview runs this handler on the GUI thread, so
        # doing it inline keeps the window on screen for that whole time. The
        # bounded join below still stops the process from outliving the window
        # by more than the cleanup budget.
        if shutdown_worker is None:
            shutdown_worker = Thread(
                target=_run_shutdown_cleanup,
                daemon=True,
                name="argusai-gui-shutdown",
            )
            shutdown_worker.start()
        return True

    try:
        window.events.closing += _on_window_closing
    except Exception:
        pass

    # Kick a background MCP probe so the AI's first prompt sees real
    # ``serverInfo`` (instead of "NOT_YET_PROBED") and the MCP panel can
    # render alive/dead badges without the user manually pressing test
    # on every server. Daemon thread, bounded concurrency, per-server
    # timeout — never blocks the GUI.
    try:
        api.start_mcp_startup_probe()
    except Exception:
        pass

    try:
        api.start_assistant_cron_scheduler()
    except Exception:
        log.exception("gui.boot cron scheduler failed to start")

    # Production-default: devtools / right-click inspect are OFF. Developers
    # who need to debug the WebView can opt in with ARGUSAI_WEBVIEW_DEBUG=1
    # (or any value other than 0/false/no).
    debug_webview = os.environ.get("ARGUSAI_WEBVIEW_DEBUG", "0").lower() not in {"0", "false", "no", ""}

    def _on_webview_start(w: webview.Window) -> None:
        try:
            from argusai.gui.macos_titlebar import apply_unified_titlebar

            api._macos_titlebar = apply_unified_titlebar(w)
        except Exception:
            log.debug("gui.boot macos titlebar apply failed", exc_info=True)
        try:
            _bind_native_file_drop(w)
        except Exception:
            log.debug("gui.boot file-drop bind failed", exc_info=True)
        try:
            bind_webview_media_permissions(w)
        except Exception:
            log.debug("gui.boot media permission bind failed", exc_info=True)
        try:
            tray.start()
        except Exception:
            log.exception("gui.tray start failed")
        if start_hidden and not tray.icon_visible():
            try:
                api.show_window_from_tray()
            except Exception:
                log.debug("gui.tray fallback show failed", exc_info=True)

    try:
        webview.start(_on_webview_start, window, debug=debug_webview)
    finally:
        try:
            tray.stop()
        except Exception:
            pass
        if shutdown_worker is not None:
            shutdown_worker.join(timeout=5)
        try:
            api.shutdown_background_services()
        except Exception:
            pass
        if httpd:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        if vite_proc:
            try:
                vite_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
