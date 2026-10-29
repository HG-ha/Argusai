"""Localhost control API for a running proxy's breakpoints.

``argusai proxy`` and the desktop proxy both start this listener. A second
terminal reads ``control.json`` next to the CA and calls it; the token in that
file is the only credential, and the socket is bound to 127.0.0.1.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CONTROL_FILE = "control.json"
_MAX_BODY = 256_000


class ProxyControlServer:
    def __init__(self, manager: Any, directory: Path) -> None:
        self.manager = manager
        self.directory = Path(directory)
        self.token = secrets.token_hex(16)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> dict[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        handler = _handler_factory(self)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd = httpd
        port = int(httpd.server_address[1])
        info = {"host": "127.0.0.1", "port": port, "token": self.token}
        (self.directory / CONTROL_FILE).write_text(
            json.dumps(info),
            encoding="utf-8",
        )
        thread = threading.Thread(
            target=httpd.serve_forever,
            name="proxy-control",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return info

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        try:
            (self.directory / CONTROL_FILE).unlink(missing_ok=True)
        except OSError:
            pass


def _handler_factory(server: ProxyControlServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            return secrets.compare_digest(
                str(self.headers.get("X-Argus-Token") or ""),
                server.token,
            )

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > _MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            return data

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self) -> None:
            if not self._authorized():
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
            path = urlparse(self.path).path
            try:
                payload = self._read_json() if self.command == "POST" else {}
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            manager = server.manager
            if self.command == "GET" and path == "/v1/breakpoints":
                result = manager.breakpoint_list_paused()
                config = manager.get_breakpoint_config()
                if isinstance(result, dict):
                    result["config"] = (config or {}).get("config")
                self._send(200, result if isinstance(result, dict) else {"ok": True, "paused": result})
                return
            if self.command == "POST" and path == "/v1/breakpoints/config":
                result = manager.set_breakpoint_config(payload)
                self._send(200 if result.get("ok") else 400, result)
                return
            if self.command == "POST" and path == "/v1/breakpoints/resolve":
                result = manager.breakpoint_resolve(
                    str(payload.get("id") or ""),
                    str(payload.get("action") or "forward"),
                    payload.get("edits") if isinstance(payload.get("edits"), dict) else {},
                )
                self._send(200 if result.get("ok") else 400, result)
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

    return Handler
