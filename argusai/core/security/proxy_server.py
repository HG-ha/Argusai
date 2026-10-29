"""Asyncio HTTP/HTTPS MITM proxy server."""

from __future__ import annotations

import asyncio
import datetime as _dt
import ipaddress
import logging
import re
import ssl
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from argusai.core.events import EventBus
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_constants import (
    _HISTORY_BODY_BUDGET,
    _MAX_BUFFERED_REQUEST_BYTES,
    _MAX_FLOW_BODY_BYTES,
    _MAX_RAW_CAPTURE_BYTES,
    _RELAY_CHUNK_BYTES,
    _utcnow,
    decode_body_prefix,
)
from argusai.core.security.proxy_flow import ProxyConfig, TrafficFlow
from argusai.core.security.proxy_media import classify_flow

log = logging.getLogger("argusai.proxy")


def _rewrite_label(request_changed: bool, response_changed: bool) -> str:
    if request_changed and response_changed:
        return "both"
    if request_changed:
        return "request"
    if response_changed:
        return "response"
    return ""


@dataclass
class _RelayResult:
    status: int
    reason: str
    header_items: list[tuple[str, str]]
    captured_raw: bytes  # content-encoded prefix, at most _MAX_RAW_CAPTURE_BYTES
    wire_size: int
    encoding: str
    must_close: bool = False
    # Upstream broke after the status line went out; no 502 can follow.
    error: str | None = None
    rewritten: bool = False  # an active response-rewrite rule changed this response

    @property
    def fully_captured(self) -> bool:
        return self.error is None and self.wire_size == len(self.captured_raw)


class _StreamedRequestBody:
    """Large or chunked upload relayed upstream as it arrives.

    Only the first ``_MAX_FLOW_BODY_BYTES`` are kept for the flow record, so an
    upload of any size costs bounded memory.
    """

    def __init__(self, reader: asyncio.StreamReader, *, length: int | None) -> None:
        self._reader = reader
        self._length = length  # None → chunked transfer coding
        self.captured = bytearray()
        self.total = 0
        self.exhausted = False

    def _keep(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = _MAX_FLOW_BODY_BYTES - len(self.captured)
        if room > 0:
            self.captured += chunk[:room]

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        if self._length is None:
            async for chunk in self._iter_chunked():
                yield chunk
        else:
            remaining = self._length
            while remaining > 0:
                chunk = await self._reader.read(min(_RELAY_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._keep(chunk)
                yield chunk
        self.exhausted = True

    async def _iter_chunked(self) -> AsyncIterator[bytes]:
        while True:
            line = await self._reader.readline()
            if not line:
                return
            size_text = line.decode("latin-1", errors="replace").strip().split(";", 1)[0]
            try:
                size = int(size_text, 16)
            except ValueError:
                return
            if size == 0:
                while True:
                    trailer = await self._reader.readline()
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                return
            remaining = size
            while remaining > 0:
                chunk = await self._reader.read(min(_RELAY_CHUNK_BYTES, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                self._keep(chunk)
                yield chunk
            await self._reader.readline()

class ProxyServer:
    """Asyncio HTTP/HTTPS forward proxy with passive analysis.

    HTTP: direct request parsing and forwarding.
    HTTPS: CONNECT tunneling with MITM using a dynamically generated CA.
    """

    def __init__(
        self,
        config: ProxyConfig,
        analyzer: PassiveAnalyzer,
        bus: EventBus,
    ) -> None:
        self.config = config
        self.analyzer = analyzer
        self.bus = bus
        self._server: asyncio.Server | None = None
        self._flow_counter = 0
        self._flows: list[TrafficFlow] = []
        self._body_bytes = 0  # captured bodies currently held by ``_flows``
        self._ca_cert_pem: bytes | None = None
        self._ca_key_pem: bytes | None = None
        self._ca_cert_obj: Any = None
        self._ca_key_obj: Any = None
        # Host -> (SSLContext, cert_path, key_path). The SSLContext is cached so
        # repeated CONNECTs to the same host don't re-load the chain from disk.
        self._host_cert_cache: dict[str, tuple[ssl.SSLContext, Path, Path]] = {}
        self._upstream_client: Any = None
        self._running = False
        from argusai.core.security.proxy_breakpoint import BreakpointEngine
        from argusai.core.security.proxy_rewrite import ProxyRewriteEngine

        # Opt-in active rewriting. Empty by default → the proxy stays passive.
        self._rewrite = ProxyRewriteEngine()
        # Opt-in interactive request breakpoints. Disabled by default.
        self._breakpoints = BreakpointEngine()

    def set_rewrite_rules(self, raw_rules: Any) -> None:
        self._rewrite.set_rules(raw_rules)

    @property
    def rewrite_errors(self) -> dict[str, str]:
        return self._rewrite.errors

    # ---- interactive breakpoints ------------------------------------

    def set_breakpoint_config(self, config: Any) -> dict[str, Any]:
        return self._breakpoints.set_config(config)

    def get_breakpoint_config(self) -> dict[str, Any]:
        return self._breakpoints.get_config()

    def set_breakpoint_on_change(self, callback: Any) -> None:
        self._breakpoints.set_on_change(callback)

    def breakpoint_list_paused(self) -> list[dict[str, Any]]:
        return self._breakpoints.list_paused()

    def breakpoint_resolve(self, pause_id: str, action: str = "forward", edits: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._breakpoints.resolve(pause_id, action, edits)

    def _build_match_flow(
        self,
        method: str,
        url: str,
        host: str,
        request_headers_dict: dict[str, str],
        header_items: list[tuple[str, str]],
        body: "bytes | _StreamedRequestBody",
        is_https: bool,
    ) -> TrafficFlow:
        """A transient flow used only to evaluate rewrite match expressions."""
        req_body = body if isinstance(body, (bytes, bytearray)) else b""
        return TrafficFlow(
            id=0,
            timestamp=time.time(),
            method=method,
            url=url,
            host=host,
            request_headers=dict(request_headers_dict),
            request_body=bytes(req_body),
            is_https=is_https,
            raw_request_header_items=list(header_items),
        )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                self.config.listen_host,
                self.config.listen_port,
            )
        except Exception:
            self._running = False
            raise
        self.bus.emit(
            "proxy.started",
            host=self.config.listen_host,
            port=self.config.listen_port,
        )

    async def stop(self) -> None:
        self._running = False
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                log.debug("proxy server wait_closed timed out during shutdown")
            except Exception as exc:
                log.debug("proxy server wait_closed failed: %s", exc)
        client = self._upstream_client
        self._upstream_client = None
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=2.0)
            except Exception as exc:
                log.debug("proxy upstream client close failed: %s", exc)
        self.bus.emit("proxy.stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def flows(self) -> list[TrafficFlow]:
        return self._flows

    def flows_since(self, idx: int) -> list[TrafficFlow]:
        return self._flows[idx:]

    @property
    def last_flow_id(self) -> int:
        return self._flow_counter

    def adopt_history(self, flows: list[TrafficFlow], last_flow_id: int) -> None:
        """Continue a previous run's capped list and id sequence."""
        cap = max(0, int(self.config.max_history))
        self._flows = list(flows[-cap:]) if cap else []
        self._body_bytes = sum(f.body_bytes() for f in self._flows)
        self._flow_counter = max(self._flow_counter, int(last_flow_id or 0))

    def clear_flows(self) -> None:
        # Rebinding (not .clear()) keeps the proxy thread's cap check from
        # popping an emptied list mid-append.
        self._flows = []
        self._body_bytes = 0

    def note_decoded_bytes(self, delta: int) -> None:
        """Account plaintext a decrypt pass attached to a flow after capture."""
        self._body_bytes += int(delta)
        if self._body_bytes > _HISTORY_BODY_BUDGET:
            self._release_oldest_bodies(self._flows)

    def _release_oldest_bodies(self, flows: list[TrafficFlow]) -> None:
        """Free bodies oldest-first until the history fits the memory budget."""
        newest = flows[-1] if flows else None
        for old in flows:
            if self._body_bytes <= _HISTORY_BODY_BUDGET or old is newest:
                break
            self._body_bytes -= old.evict_bodies()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single incoming TCP connection (HTTP or CONNECT)."""
        if not self._running:
            self._safe_close(writer)
            return
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=30)
            if not first_line:
                return
            line = first_line.decode("latin-1", errors="replace").strip()
            if not line:
                return

            parts = line.split()
            if len(parts) < 2:
                return

            method = parts[0].upper()
            url_or_host = parts[1]
            version = parts[2] if len(parts) > 2 else "HTTP/1.1"

            if method == "CONNECT":
                await self._handle_connect(url_or_host, reader, writer)
            else:
                await self._handle_http_loop(
                    reader,
                    writer,
                    first_method=method,
                    first_target=url_or_host,
                    first_version=version,
                    origin_scheme="http",
                )
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug("proxy connection error: %s", e)
        finally:
            await self._drain_close(writer)

    @staticmethod
    def _safe_close(writer: asyncio.StreamWriter) -> None:
        try:
            if not writer.is_closing():
                writer.close()
        except Exception:
            pass

    @staticmethod
    async def _drain_close(writer: asyncio.StreamWriter) -> None:
        """Close a writer and wait for the transport to fully release.

        Avoids the Windows-only ``Event loop is closed`` GC warning emitted
        when StreamWriter.__del__ races with loop shutdown.
        """
        try:
            if not writer.is_closing():
                writer.close()
        except Exception:
            return
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except Exception:
            pass

    async def _handle_connect(
        self,
        host_port: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle HTTPS CONNECT — TLS MITM, then serve many HTTP/1.1 requests."""
        host, _, port_str = host_port.partition(":")
        try:
            port = int(port_str) if port_str else 443
        except ValueError:
            port = 443

        # Consume the (empty) header block of the CONNECT request.
        await self._read_raw_headers(reader)

        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return

        try:
            ssl_ctx = self._ssl_for_host(host)
        except Exception as exc:
            log.warning("proxy: failed to provision TLS cert for %s: %s", host, exc)
            return

        try:
            loop = asyncio.get_running_loop()
            transport = writer.transport
            protocol = transport.get_protocol()
            tls_transport = await loop.start_tls(
                transport,
                protocol,
                ssl_ctx,
                server_side=True,
            )
            tls_reader = asyncio.StreamReader(limit=2**20)
            tls_protocol = asyncio.StreamReaderProtocol(tls_reader, loop=loop)
            tls_transport.set_protocol(tls_protocol)
            tls_protocol.connection_made(tls_transport)
            tls_writer = asyncio.StreamWriter(tls_transport, tls_protocol, tls_reader, loop)
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError, OSError) as exc:
            log.debug("proxy TLS handshake failed for %s: %s", host, exc)
            return
        except Exception as exc:
            log.debug("proxy CONNECT setup error for %s: %s", host, exc)
            return

        alpn = ""
        try:
            ssl_object = tls_transport.get_extra_info("ssl_object")
            alpn = (ssl_object.selected_alpn_protocol() or "") if ssl_object else ""
        except Exception:
            alpn = ""
        try:
            if alpn == "h2":
                from argusai.core.security.proxy_http2 import serve_http2

                await serve_http2(self, tls_reader, tls_writer, default_host=host, default_port=port)
            else:
                await self._serve_http_keepalive(
                    tls_reader,
                    tls_writer,
                    origin_scheme="https",
                    default_host=host,
                    default_port=port,
                    is_https=True,
                )
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug("proxy CONNECT MITM error for %s: %s", host, e)
        finally:
            await self._drain_close(tls_writer)

    async def _handle_http_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        first_method: str,
        first_target: str,
        first_version: str,
        origin_scheme: str,
    ) -> None:
        """Serve one or more plain-HTTP proxy requests on the same TCP connection."""
        request: tuple[str, str, str] | None = (first_method, first_target, first_version)
        while self._running and not reader.at_eof():
            if request is None:
                parsed = await self._read_request_line(reader, timeout=120)
                if parsed is None:
                    return
                method, target, version = parsed
            else:
                method, target, version = request
                request = None

            header_items = await self._read_raw_headers(reader)
            host_header = self._first_header(header_items, "host")

            if target.startswith("/"):
                if not host_header:
                    await self._send_error_response(writer, 400, "Proxy expects absolute URI or Host header.")
                    return
                url = f"http://{host_header}{target}"
            elif "://" in target:
                url = target
            else:
                await self._send_error_response(writer, 400, "Unsupported proxy request target.")
                return

            await self._maybe_send_continue(header_items, writer)
            body = await self._read_body(header_items, reader)
            close_after = await self._dispatch_request(
                writer,
                method=method,
                url=url,
                version=version,
                header_items=header_items,
                body=body,
                is_https=False,
                reader=reader,
            )
            if close_after:
                return

    async def _serve_http_keepalive(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        origin_scheme: str,
        default_host: str,
        default_port: int,
        is_https: bool,
    ) -> None:
        """HTTP/1.1 keep-alive loop on an established (possibly TLS) leg."""
        while self._running and not reader.at_eof():
            parsed = await self._read_request_line(reader, timeout=120)
            if parsed is None:
                return
            method, target, version = parsed
            header_items = await self._read_raw_headers(reader)
            host_header = self._first_header(header_items, "host") or default_host
            if target.startswith("/"):
                if default_port not in (80, 443):
                    hp = host_header if ":" in host_header else f"{host_header}:{default_port}"
                else:
                    hp = host_header
                url = f"{origin_scheme}://{hp}{target}"
            elif "://" in target:
                url = target
            else:
                await self._send_error_response(writer, 400, "Unsupported request target.")
                return

            await self._maybe_send_continue(header_items, writer)
            body = await self._read_body(header_items, reader)
            close_after = await self._dispatch_request(
                writer,
                method=method,
                url=url,
                version=version,
                header_items=header_items,
                body=body,
                is_https=is_https,
                reader=reader,
            )
            if close_after:
                return

    async def _dispatch_request(
        self,
        writer: asyncio.StreamWriter,
        *,
        method: str,
        url: str,
        version: str,
        header_items: list[tuple[str, str]],
        body: bytes | _StreamedRequestBody,
        is_https: bool,
        reader: asyncio.StreamReader | None = None,
    ) -> bool:
        """Passively tunnel websocket upgrades, else delegate to ``_forward_request``."""
        client_wants_close = self._client_wants_close(header_items, version)

        upgrade = self._first_header(header_items, "upgrade")
        if upgrade.lower() == "websocket" and reader is not None:
            from argusai.core.security.proxy_filters import should_capture_host

            host = urlsplit(url).hostname or ""
            try:
                await self._tunnel_websocket(
                    method=method,
                    url=url,
                    host=host,
                    header_items=header_items,
                    client_reader=reader,
                    client_writer=writer,
                    is_https=is_https,
                    in_scope=should_capture_host(self.config, host),
                )
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception as exc:
                log.debug("proxy websocket tunnel error for %s: %s", url, exc)
            return True  # the connection became a WebSocket; never reuse it for HTTP

        try:
            must_close = await self._forward_request(
                method=method,
                url=url,
                header_items=header_items,
                body=body,
                writer=writer,
                is_https=is_https,
                client_wants_close=client_wants_close,
                client_http10=version.upper().endswith("/1.0"),
            )
        except (ConnectionResetError, BrokenPipeError):
            return True
        return client_wants_close or must_close

    async def _maybe_send_continue(
        self, header_items: list[tuple[str, str]], writer: asyncio.StreamWriter
    ) -> None:
        # Uploaders (curl > 1 KiB, .NET) wait for this before sending the body;
        # ``expect`` itself is not forwarded because httpx does not speak it.
        if "100-continue" not in self._first_header(header_items, "expect").lower():
            return
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        await writer.drain()

    @staticmethod
    def _first_header(items: list[tuple[str, str]], name: str) -> str:
        target = name.lower()
        for k, v in items:
            if k.lower() == target:
                return v
        return ""

    @staticmethod
    def _client_wants_close(header_items: list[tuple[str, str]], version: str) -> bool:
        conn = ""
        for k, v in header_items:
            if k.lower() == "connection":
                conn = v
                break
        tokens = {t.strip().lower() for t in conn.split(",") if t.strip()}
        if "close" in tokens:
            return True
        if version.upper().endswith("/1.0") and "keep-alive" not in tokens:
            return True
        return False

    # Hop-by-hop / forbidden headers per RFC 7230 §6.1, plus a few proxy-only ones.
    _ORIGIN_SKIP_HEADERS = frozenset({
        "host",
        "proxy-connection",
        "proxy-authorization",
        "connection",
        "keep-alive",
        "te",
        "transfer-encoding",
        "upgrade",
        "expect",
    })

    _RELAY_SKIP_HEADERS = frozenset({
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "content-length",
    })

    @staticmethod
    def _split_target_url(url: str) -> tuple[str, int, str, bool]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        use_ssl = parts.scheme == "https"
        port = parts.port or (443 if use_ssl else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return host, port, path, use_ssl

    @classmethod
    def _build_origin_request(
        cls,
        method: str,
        path: str,
        headers: dict[str, str] | list[tuple[str, str]],
        body: bytes,
        *,
        host: str,
        port: int,
    ) -> bytes:
        """Pure helper: serialize an HTTP/1.1 request to bytes.

        Retained for test coverage; production forwarding goes through
        :meth:`_relay_via_httpx`.
        """
        items: list[tuple[str, str]] = (
            list(headers.items()) if isinstance(headers, dict) else list(headers)
        )
        host_header = ""
        for k, v in items:
            if k.lower() == "host":
                host_header = v
                break
        if not host_header:
            host_header = host
        if port not in (80, 443) and ":" not in host_header:
            host_header = f"{host_header}:{port}"
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host_header}"]
        for key, value in items:
            if key.lower() in cls._ORIGIN_SKIP_HEADERS:
                continue
            lines.append(f"{key}: {value}")
        lines.append("Connection: close")
        block = "\r\n".join(lines).encode("latin-1", errors="replace") + b"\r\n\r\n"
        return block + body

    @classmethod
    def _origin_forward_headers(
        cls, header_items: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        return [(k, v) for k, v in header_items if k.lower() not in cls._ORIGIN_SKIP_HEADERS]

    @classmethod
    def _relay_response_header_items(
        cls,
        headers: Any,
        body_len: int | None,
        *,
        client_keepalive: bool = False,
        chunked: bool = False,
    ) -> list[tuple[str, str]]:
        items = [
            (str(k), str(v))
            for k, v in (
                headers.multi_items() if hasattr(headers, "multi_items") else list(headers.items())
            )
            if str(k).lower() not in cls._RELAY_SKIP_HEADERS
        ]
        if body_len is not None:
            items.append(("Content-Length", str(body_len)))
        if chunked:
            items.append(("Transfer-Encoding", "chunked"))
        items.append(("Connection", "keep-alive" if client_keepalive else "close"))
        return items

    @staticmethod
    async def _write_http_response(
        writer: asyncio.StreamWriter,
        status_code: int,
        reason: str,
        header_items: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        writer.write(f"HTTP/1.1 {status_code} {reason}\r\n".encode("latin-1", errors="replace"))
        for k, v in header_items:
            writer.write(f"{k}: {v}\r\n".encode("latin-1", errors="replace"))
        writer.write(b"\r\n")
        if body:
            writer.write(body)
        await writer.drain()

    async def _origin_client(self) -> Any:
        """Long-lived ``httpx.AsyncClient`` reused across forwarded requests."""
        if self._upstream_client is None:
            import httpx

            from argusai.core.infra.httpx_client import direct_async_client

            limits = httpx.Limits(max_connections=128, max_keepalive_connections=32)
            self._upstream_client = direct_async_client(
                verify=False,
                follow_redirects=False,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
                limits=limits,
                http2=False,
            )
        return self._upstream_client

    @staticmethod
    def _declared_length(headers: Any) -> int | None:
        if "transfer-encoding" in headers:
            return None
        try:
            value = int(str(headers.get("content-length", "")).strip())
        except ValueError:
            return None
        return value if value >= 0 else None

    async def _relay_via_httpx(
        self,
        method: str,
        url: str,
        header_items: list[tuple[str, str]],
        body: bytes | _StreamedRequestBody,
        writer: asyncio.StreamWriter,
        *,
        client_keepalive: bool,
        client_http10: bool = False,
        rewrite_flow: TrafficFlow | None = None,
    ) -> _RelayResult:
        """Forward through httpx and stream the still-encoded response to the client.

        Bytes reach the browser as they arrive (no whole-body buffering, so a
        multi-GB download costs no more memory than a small page); only the
        first ``_MAX_RAW_CAPTURE_BYTES`` are kept for analysis and preview.
        Errors before the status line propagate (caller answers 502); errors
        after it are reported in ``_RelayResult.error``.
        """
        client = await self._origin_client()
        forward_items = self._origin_forward_headers(header_items)
        if isinstance(body, _StreamedRequestBody):
            content: Any = body.iter_chunks()
        else:
            content = body or None
        request = client.build_request(method, url, headers=forward_items, content=content)
        resp = await client.send(request, stream=True)
        try:
            response_header_items = [(str(k), str(v)) for k, v in resp.headers.multi_items()]
            encoding = str(resp.headers.get("content-encoding", ""))
            status = resp.status_code
            declared = self._declared_length(resp.headers)
            no_body = method.upper() == "HEAD" or status in (204, 304) or 100 <= status < 200
            must_close = False
            if no_body:
                # HEAD keeps the entity length it describes; nothing follows.
                relay_items = self._relay_response_header_items(
                    resp.headers, declared, client_keepalive=client_keepalive
                )
                mode = "none"
            elif declared is not None:
                relay_items = self._relay_response_header_items(
                    resp.headers, declared, client_keepalive=client_keepalive
                )
                mode = "length"
            elif client_http10:
                relay_items = self._relay_response_header_items(resp.headers, None, client_keepalive=False)
                mode = "close"
                must_close = True
            else:
                relay_items = self._relay_response_header_items(
                    resp.headers, None, client_keepalive=client_keepalive, chunked=True
                )
                mode = "chunked"
            # Opt-in active rewrite of the response. Header/status edits are free;
            # body edits need the whole (uncompressed, length-declared) body, so
            # that one response is buffered instead of streamed.
            resp_actions: list[dict[str, Any]] = []
            if rewrite_flow is not None and self._rewrite.has_response_rules and not no_body:
                rewrite_flow.response_status = status
                rewrite_flow.response_headers = {str(k).lower(): str(v) for k, v in response_header_items}
                rewrite_flow.raw_response_header_items = list(response_header_items)
                for rule in self._rewrite.response_rules_for(rewrite_flow):
                    resp_actions.extend(rule.actions)
            from argusai.core.security.proxy_rewrite import (
                MAX_REWRITE_BODY_BYTES,
                apply_response_actions,
            )

            can_body = (
                bool(resp_actions)
                and any(a.get("op") == "replace_body" for a in resp_actions)
                and mode == "length"
                and declared is not None
                and declared <= MAX_REWRITE_BODY_BYTES
                and encoding.strip().lower() in ("", "identity")
            )
            rewritten_resp = False
            captured = bytearray()
            wire_size = 0
            error: str | None = None

            if can_body:
                full = bytearray()
                try:
                    async for chunk in resp.aiter_raw(_RELAY_CHUNK_BYTES):
                        full += chunk
                except (ConnectionResetError, BrokenPipeError):
                    raise
                except Exception as exc:
                    error = f"upstream body interrupted: {exc or type(exc).__name__}"
                    must_close = True
                rr = apply_response_actions(
                    resp_actions,
                    status=status,
                    reason=resp.reason_phrase or "",
                    header_items=relay_items,
                    body=bytes(full),
                    rule_name="",
                )
                status = rr.status
                rewritten_resp = True
                await self._write_http_response(writer, status, resp.reason_phrase or "", rr.header_items, rr.body)
                captured += rr.body[:_MAX_RAW_CAPTURE_BYTES]
                wire_size = len(rr.body)
            else:
                if resp_actions:
                    rr = apply_response_actions(
                        resp_actions,
                        status=status,
                        reason=resp.reason_phrase or "",
                        header_items=relay_items,
                        body=None,
                        rule_name="",
                    )
                    status = rr.status
                    relay_items = rr.header_items
                    rewritten_resp = True
                await self._write_http_response(writer, status, resp.reason_phrase or "", relay_items, b"")
                if mode != "none":
                    try:
                        async for chunk in resp.aiter_raw(_RELAY_CHUNK_BYTES):
                            if not chunk:
                                continue
                            wire_size += len(chunk)
                            room = _MAX_RAW_CAPTURE_BYTES - len(captured)
                            if room > 0:
                                captured += chunk[:room]
                            if mode == "chunked":
                                writer.write(b"%x\r\n" % len(chunk))
                                writer.write(chunk)
                                writer.write(b"\r\n")
                            else:
                                writer.write(chunk)
                            await writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        raise
                    except Exception as exc:
                        error = f"upstream body interrupted: {exc or type(exc).__name__}"
                        must_close = True
                    if mode == "chunked" and error is None:
                        writer.write(b"0\r\n\r\n")
                        await writer.drain()
                    if mode == "length" and error is None and wire_size != declared:
                        must_close = True
        finally:
            await resp.aclose()
        return _RelayResult(
            status=status,
            reason=resp.reason_phrase or "",
            header_items=response_header_items,
            captured_raw=bytes(captured),
            wire_size=wire_size,
            encoding=encoding,
            must_close=must_close,
            error=error,
            rewritten=rewritten_resp,
        )

    async def _forward_request(
        self,
        *,
        method: str,
        url: str,
        header_items: list[tuple[str, str]],
        body: bytes | _StreamedRequestBody,
        writer: asyncio.StreamWriter,
        is_https: bool,
        client_wants_close: bool,
        client_http10: bool = False,
    ) -> bool:
        """Forward one request, relay response, analyze it.

        Returns ``True`` when the caller must close the client connection
        (e.g. we wrote a ``502`` and advertised ``Connection: close``).
        """
        host = urlsplit(url).hostname or ""
        client_keepalive = not client_wants_close

        from argusai.core.security.proxy_filters import (
            should_capture_host,
            should_capture_mime,
        )

        in_scope = should_capture_host(self.config, host)
        request_headers_dict: dict[str, str] = {}
        for k, v in header_items:
            # Last-write-wins for the flat dict is acceptable because the canonical
            # multi-value source is ``raw_request_header_items``.
            request_headers_dict[k.lower()] = v

        # Active rewrite (opt-in): request-side rules run before we go upstream;
        # a short-circuit action answers the client without contacting origin.
        rewrite_flow: TrafficFlow | None = None
        req_rewritten = False
        if self._rewrite.active:
            rewrite_flow = self._build_match_flow(
                method, url, host, request_headers_dict, header_items, body, is_https
            )
            request_rules = (
                self._rewrite.request_rules_for(rewrite_flow)
                if self._rewrite.has_request_rules
                else []
            )
            if request_rules:
                from argusai.core.security.proxy_rewrite import apply_request_actions

                req_rewritten = True
                req_body = body if isinstance(body, (bytes, bytearray)) else None
                result_body = bytes(req_body) if req_body is not None else None
                for rule in request_rules:
                    rewritten = apply_request_actions(
                        rule.actions,
                        method=method,
                        url=url,
                        header_items=header_items,
                        body=result_body,
                        rule_name=rule.name,
                    )
                    method, url, header_items = rewritten.method, rewritten.url, rewritten.header_items
                    if req_body is not None:
                        result_body = rewritten.body
                    if rewritten.short_circuit is not None:
                        await self._serve_short_circuit(
                            writer,
                            rewritten.short_circuit,
                            method=method,
                            url=url,
                            host=host,
                            header_items=header_items,
                            is_https=is_https,
                            in_scope=in_scope,
                            client_keepalive=client_keepalive,
                        )
                        return not client_keepalive
                if req_body is not None:
                    body = result_body
                request_headers_dict = {k.lower(): v for k, v in header_items}

        # Interactive breakpoint (opt-in): park the request so a human / the
        # agent can edit or drop it before it goes upstream. Only bytes bodies
        # are editable; a streamed upload is forwarded without parking.
        if self._breakpoints.enabled and isinstance(body, (bytes, bytearray)):
            resolution = await self._breakpoints.pause_request(
                method=method,
                url=url,
                host=host,
                header_items=header_items,
                body=bytes(body),
                is_https=is_https,
            )
            if str(resolution.get("action")) == "drop":
                try:
                    writer.close()
                except Exception:
                    pass
                return True
            edits = resolution.get("edits") or {}
            if edits:
                from argusai.core.security.proxy_breakpoint import apply_request_edits

                method, url, header_items, new_body = apply_request_edits(
                    edits, method=method, url=url, header_items=header_items, body=bytes(body)
                )
                body = new_body if new_body is not None else body
                host = urlsplit(url).hostname or host
                request_headers_dict = {k.lower(): v for k, v in header_items}
                in_scope = should_capture_host(self.config, host)

        started = time.monotonic()
        try:
            relay = await self._relay_via_httpx(
                method,
                url,
                header_items,
                body,
                writer,
                client_keepalive=client_keepalive,
                client_http10=client_http10,
                rewrite_flow=rewrite_flow,
            )
        except (ConnectionResetError, BrokenPipeError):
            raise
        except Exception as e:
            log.debug("proxy upstream error for %s %s: %s", method, url, e)
            await self._send_error_response(writer, 502, f"Proxy upstream error: {e}")
            if in_scope:
                req_body, req_total = self._request_capture(body)
                self._record_error_flow(
                    method=method,
                    url=url,
                    host=host,
                    header_items=header_items,
                    request_headers_dict=request_headers_dict,
                    body=req_body,
                    request_full_size=req_total,
                    is_https=is_https,
                    error=str(e),
                    duration_ms=(time.monotonic() - started) * 1000.0,
                )
            return True  # 502 advertises ``Connection: close``
        duration_ms = (time.monotonic() - started) * 1000.0
        # Unread upload bytes would be parsed as the next request.
        must_close = relay.must_close or (
            isinstance(body, _StreamedRequestBody) and not body.exhausted
        )

        if not in_scope:
            return must_close

        flat_response: dict[str, str] = {}
        for k, v in relay.header_items:
            flat_response[k.lower()] = v
        meta = classify_flow(
            url=url,
            response_headers=flat_response,
            method=method,
        )
        if not should_capture_mime(self.config, meta["content_type"], meta["resource_kind"]):
            return must_close

        decoded, decoded_complete = decode_body_prefix(
            relay.encoding, relay.captured_raw, _MAX_FLOW_BODY_BYTES
        )
        if relay.encoding.strip().lower() in ("", "identity"):
            # Unencoded: the wire size is the body size, captured or not.
            exact = relay.error is None
            full_size = relay.wire_size
        else:
            exact = relay.fully_captured and decoded_complete
            full_size = len(decoded) if exact else 0
        req_body, req_total = self._request_capture(body)
        flow = TrafficFlow(
            id=self._next_flow_id(),
            timestamp=time.time(),
            method=method,
            url=url,
            host=host,
            request_headers=request_headers_dict,
            request_body=req_body,
            is_https=is_https,
            response_status=relay.status,
            response_headers=flat_response,
            response_body=decoded,
            error=relay.error,
            content_type=meta["content_type"],
            resource_kind=meta["resource_kind"],
            filename_hint=meta["filename_hint"],
            raw_request_header_items=list(header_items),
            raw_response_header_items=relay.header_items,
            response_encoding=relay.encoding,
            duration_ms=duration_ms,
            request_full_size=req_total,
            response_full_size=full_size,
            response_wire_size=relay.wire_size,
            response_size_exact=exact,
            rewritten=_rewrite_label(req_rewritten, relay.rewritten),
        )
        self._append_flow(flow)
        return must_close

    @staticmethod
    def _request_capture(body: bytes | _StreamedRequestBody) -> tuple[bytes, int]:
        if isinstance(body, _StreamedRequestBody):
            return bytes(body.captured), body.total
        return body[:_MAX_FLOW_BODY_BYTES], len(body)

    async def _serve_short_circuit(
        self,
        writer: asyncio.StreamWriter,
        canned: dict[str, Any],
        *,
        method: str,
        url: str,
        host: str,
        header_items: list[tuple[str, str]],
        is_https: bool,
        in_scope: bool,
        client_keepalive: bool,
    ) -> None:
        """Answer a request from a rewrite rule without contacting the origin."""
        status = int(canned.get("status", 200))
        reason = str(canned.get("reason") or "")
        body = canned.get("body") or b""
        items = [
            (str(k), str(v))
            for k, v in canned.get("headers", [])
            if str(k).lower() not in self._RELAY_SKIP_HEADERS
        ]
        items.append(("Content-Length", str(len(body))))
        items.append(("Connection", "keep-alive" if client_keepalive else "close"))
        await self._write_http_response(writer, status, reason, items, body)
        if not in_scope:
            return
        flat_response = {k.lower(): v for k, v in items}
        flow = TrafficFlow(
            id=self._next_flow_id(),
            timestamp=time.time(),
            method=method,
            url=url,
            host=host,
            request_headers={k.lower(): v for k, v in header_items},
            request_body=b"",
            is_https=is_https,
            response_status=status,
            response_headers=flat_response,
            response_body=bytes(body),
            content_type=flat_response.get("content-type", ""),
            resource_kind="other",
            raw_request_header_items=list(header_items),
            raw_response_header_items=items,
            response_full_size=len(body),
            response_wire_size=len(body),
            response_size_exact=True,
            rewritten="short_circuit",
        )
        self._append_flow(flow)

    _WS_MAX_FRAMES = 2000  # recorded per connection; the tunnel keeps relaying past this

    async def _tunnel_websocket(
        self,
        *,
        method: str,
        url: str,
        host: str,
        header_items: list[tuple[str, str]],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        is_https: bool,
        in_scope: bool,
    ) -> None:
        """Relay a WebSocket upgrade to the origin and passively record frames."""
        from argusai.core.security.proxy_ws import frame_record, relay_ws_frame

        _host, port, path, use_ssl = self._split_target_url(url)
        ssl_ctx: Any = None
        if use_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        up_reader, up_writer = await asyncio.open_connection(
            host, port, ssl=ssl_ctx, server_hostname=host if use_ssl else None
        )
        try:
            # Forward the upgrade verbatim: Upgrade / Connection / Sec-WebSocket-*
            # must survive, so the hop-by-hop header filter is skipped here.
            lines = [f"{method} {path} HTTP/1.1"]
            for key, value in header_items:
                if key.lower() in ("proxy-connection", "proxy-authorization"):
                    continue
                lines.append(f"{key}: {value}")
            up_writer.write("\r\n".join(lines).encode("latin-1", errors="replace") + b"\r\n\r\n")
            await up_writer.drain()

            status_line = await up_reader.readline()
            resp_items = await self._read_raw_headers(up_reader)
            client_writer.write(status_line)
            for key, value in resp_items:
                client_writer.write(f"{key}: {value}\r\n".encode("latin-1", errors="replace"))
            client_writer.write(b"\r\n")
            await client_writer.drain()

            try:
                status = int(status_line.decode("latin-1", errors="replace").split()[1])
            except (IndexError, ValueError):
                status = 0
            if status != 101:
                return  # origin declined the upgrade; the response is already relayed

            flow: TrafficFlow | None = None
            if in_scope:
                flow = TrafficFlow(
                    id=self._next_flow_id(),
                    timestamp=time.time(),
                    method=method,
                    url=url,
                    host=host,
                    request_headers={k.lower(): v for k, v in header_items},
                    is_https=is_https,
                    response_status=101,
                    response_headers={k.lower(): v for k, v in resp_items},
                    content_type="websocket",
                    resource_kind="websocket",
                    raw_request_header_items=list(header_items),
                    raw_response_header_items=list(resp_items),
                    websocket_frames=[],
                    websocket_open=True,
                )
                self._append_flow(flow, analyze=False)

            def record(raw: dict[str, Any], direction: str) -> None:
                if flow is None or flow.websocket_frames is None:
                    return
                if len(flow.websocket_frames) >= self._WS_MAX_FRAMES:
                    return
                frame = frame_record(raw, direction)
                for key in ("text", "hex"):
                    value = frame.get(key)
                    if isinstance(value, str) and len(value) > 4096:
                        frame[key] = value[:4096]
                        frame["truncated"] = True
                flow.websocket_frames.append(frame)

            async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, direction: str) -> None:
                while True:
                    raw = await relay_ws_frame(src, dst)
                    record(raw, direction)
                    if raw["opcode"] == 0x8:  # close frame
                        return

            down = asyncio.ensure_future(pump(up_reader, client_writer, "down"))
            up = asyncio.ensure_future(pump(client_reader, up_writer, "up"))
            try:
                _done, pending = await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in (down, up):
                    try:
                        await task
                    except (asyncio.CancelledError, asyncio.IncompleteReadError,
                            ConnectionResetError, BrokenPipeError, OSError):
                        pass
            finally:
                if flow is not None:
                    flow.websocket_open = False
        finally:
            await self._drain_close(up_writer)

    def _record_error_flow(
        self,
        *,
        method: str,
        url: str,
        host: str,
        header_items: list[tuple[str, str]],
        request_headers_dict: dict[str, str],
        body: bytes,
        is_https: bool,
        error: str,
        duration_ms: float,
        request_full_size: int | None = None,
    ) -> None:
        flow = TrafficFlow(
            id=self._next_flow_id(),
            timestamp=time.time(),
            method=method,
            url=url,
            host=host,
            request_headers=request_headers_dict,
            request_body=body,
            is_https=is_https,
            raw_request_header_items=list(header_items),
            error=error,
            duration_ms=duration_ms,
            request_full_size=len(body) if request_full_size is None else request_full_size,
        )
        self._append_flow(flow, analyze=False)

    def _next_flow_id(self) -> int:
        self._flow_counter += 1
        return self._flow_counter

    def _append_flow(self, flow: TrafficFlow, *, analyze: bool = True) -> None:
        flows = self._flows
        if flows and len(flows) >= self.config.max_history:
            self._body_bytes -= flows.pop(0).body_bytes()
        flows.append(flow)
        self._body_bytes += flow.body_bytes()
        if self._body_bytes > _HISTORY_BODY_BUDGET:
            self._release_oldest_bodies(flows)
        self.bus.emit("proxy.flow", flow_id=flow.id, url=flow.url, method=flow.method)
        if analyze:
            try:
                self.analyzer.analyze(flow)
            except Exception as e:
                log.debug("passive analysis error: %s", e)

    async def _send_error_response(
        self, writer: asyncio.StreamWriter, status: int, message: str
    ) -> None:
        body = message.encode("utf-8", errors="replace")
        reason = "Bad Gateway" if status == 502 else (
            "Bad Request" if status == 400 else "Proxy Error"
        )
        try:
            writer.write(
                (
                    f"HTTP/1.1 {status} {reason}\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("latin-1")
            )
            writer.write(body)
            await writer.drain()
        except Exception:
            pass

    async def _read_raw_headers(
        self, reader: asyncio.StreamReader
    ) -> list[tuple[str, str]]:
        """Read CRLF-terminated header block, preserving multi-value headers."""
        items: list[tuple[str, str]] = []
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            decoded = line.decode("latin-1", errors="replace").rstrip("\r\n")
            if not decoded:
                break
            if decoded[:1] in (" ", "\t") and items:
                # RFC 7230 §3.2.4 forbids obs-fold for new senders, but we tolerate it.
                last_key, last_val = items[-1]
                items[-1] = (last_key, f"{last_val} {decoded.strip()}")
                continue
            if ":" not in decoded:
                continue
            k, _, v = decoded.partition(":")
            items.append((k.strip(), v.strip()))
        return items

    async def _read_request_line(
        self,
        reader: asyncio.StreamReader,
        *,
        timeout: float,
    ) -> tuple[str, str, str] | None:
        try:
            first_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if not first_line:
            return None
        line = first_line.decode("latin-1", errors="replace").strip()
        if not line:
            return None
        parts = line.split()
        if len(parts) < 2:
            return None
        method = parts[0].upper()
        target = parts[1]
        version = parts[2] if len(parts) > 2 else "HTTP/1.1"
        return method, target, version

    async def _read_body(
        self,
        headers: list[tuple[str, str]] | dict[str, str],
        reader: asyncio.StreamReader,
    ) -> bytes | _StreamedRequestBody:
        """Buffer small bodies; hand big or chunked ones back unread for streaming."""
        if isinstance(headers, dict):
            te = str(headers.get("transfer-encoding", "")).lower().strip()
            cl = headers.get("content-length", "0") or "0"
        else:
            te = self._first_header(headers, "transfer-encoding").lower().strip()
            cl = self._first_header(headers, "content-length") or "0"
        if te == "chunked":
            return _StreamedRequestBody(reader, length=None)
        try:
            length = int(cl)
        except (TypeError, ValueError):
            length = 0
        if length > _MAX_BUFFERED_REQUEST_BYTES:
            return _StreamedRequestBody(reader, length=length)
        if length > 0:
            try:
                data = await reader.readexactly(length)
            except asyncio.IncompleteReadError as exc:
                return exc.partial
            return data
        return b""

    # ------------------------------------------------------------------
    # CA / per-host certificate plumbing
    # ------------------------------------------------------------------

    def _load_ca_material(self) -> None:
        """Ensure the CA cert/key exist on disk and are loaded into memory."""
        ca_dir = self.config.ca_dir or Path("proxy_ca")
        ca_dir.mkdir(parents=True, exist_ok=True)
        cert_path = ca_dir / "ca.pem"
        key_path = ca_dir / "ca.key"

        if not cert_path.exists() or not key_path.exists():
            self._generate_ca(cert_path, key_path)

        self._ca_cert_pem = cert_path.read_bytes()
        self._ca_key_pem = key_path.read_bytes()

        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            self._ca_cert_obj = x509.load_pem_x509_certificate(self._ca_cert_pem)
            self._ca_key_obj = serialization.load_pem_private_key(
                self._ca_key_pem, password=None
            )
        except ImportError as exc:
            raise RuntimeError("HTTPS MITM requires 'cryptography' package") from exc

    # Back-compat alias kept for tests / external callers that imported the
    # historical name. Returns an SSLContext seeded with the CA for verification
    # use (it is **not** suitable as a server-side context).
    def _create_ssl_context(self) -> ssl.SSLContext:
        self._load_ca_material()
        ca_dir = self.config.ca_dir or Path("proxy_ca")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_verify_locations(cafile=str(ca_dir / "ca.pem"))
        return ctx

    def _ssl_for_host(self, host: str) -> ssl.SSLContext:
        """Return a server SSLContext for the given host (memoized)."""
        if self._ca_cert_obj is None or self._ca_key_obj is None:
            self._load_ca_material()

        cached = self._host_cert_cache.get(host)
        if cached is not None:
            return cached[0]

        cert_path, key_path = self._provision_host_cert(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        # Offer h2 so modern sites keep negotiating HTTP/2; fall back to 1.1.
        try:
            ctx.set_alpn_protocols(["h2", "http/1.1"])
        except (AttributeError, NotImplementedError):
            pass
        self._host_cert_cache[host] = (ctx, cert_path, key_path)
        return ctx

    def _provision_host_cert(self, host: str) -> tuple[Path, Path]:
        """Return ``(cert_path, key_path)`` for ``host``, reusing disk copies."""
        ca_dir = self.config.ca_dir or Path("proxy_ca")
        cert_dir = ca_dir / "certs"
        cert_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", host)[:120] or "host"
        cert_path = cert_dir / f"{safe}.pem"
        key_path = cert_dir / f"{safe}.key"

        if cert_path.exists() and key_path.exists() and self._cert_still_valid(cert_path, host):
            return cert_path, key_path

        cert_pem, key_pem = self._generate_host_cert(host)
        cert_path.write_text(cert_pem, encoding="utf-8")
        key_path.write_text(key_pem, encoding="utf-8")
        # Tighten the key file permissions where the platform supports it.
        try:
            import stat as _stat

            key_path.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass
        return cert_path, key_path

    def _cert_still_valid(self, cert_path: Path, host: str) -> bool:
        try:
            from cryptography import x509
            from cryptography.x509.oid import ExtensionOID
        except ImportError:
            return True  # without cryptography we have to trust whatever is on disk
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        except Exception:
            return False
        now = _utcnow()
        try:
            nva = cert.not_valid_after_utc  # type: ignore[attr-defined]
            nvb = cert.not_valid_before_utc  # type: ignore[attr-defined]
        except AttributeError:  # cryptography < 42
            nva = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
            nvb = cert.not_valid_before.replace(tzinfo=_dt.timezone.utc)
        # Refresh certs less than 14 days from expiry so we never serve an
        # expired chain mid-session.
        if nva <= now + _dt.timedelta(days=14) or nvb > now:
            return False
        try:
            san_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            ).value
        except x509.ExtensionNotFound:
            return False
        san_names: set[str] = set()
        try:
            from cryptography.x509 import DNSName, IPAddress

            for entry in san_ext:
                if isinstance(entry, DNSName):
                    san_names.add(entry.value.lower())
                elif isinstance(entry, IPAddress):
                    san_names.add(str(entry.value))
        except Exception:
            return False
        return host.lower() in san_names

    def _generate_host_cert(self, host: str) -> tuple[str, str]:
        """Generate a per-host certificate signed by the CA.

        The leaf cert carries the full set of extensions modern browsers
        require: BasicConstraints, KeyUsage, EKU=serverAuth, plus DNS or IP
        SAN, AKI / SKI. Without these Chromium-based browsers reject the chain
        even when the root CA is trusted.
        """
        try:
            from cryptography import x509
            from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError as exc:
            raise RuntimeError("HTTPS MITM requires 'cryptography' package") from exc

        assert self._ca_cert_obj is not None and self._ca_key_obj is not None

        host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, host[:64] or "argusai-host"),
        ])

        san_entries: list[Any] = []
        try:
            ip_obj = ipaddress.ip_address(host)
            san_entries.append(x509.IPAddress(ip_obj))
        except ValueError:
            san_entries.append(x509.DNSName(host))

        not_before = _utcnow() - _dt.timedelta(minutes=5)
        not_after = _utcnow() + _dt.timedelta(days=397)  # CA/B Forum max for leaves

        ski = x509.SubjectKeyIdentifier.from_public_key(host_key.public_key())
        try:
            ca_ski = self._ca_cert_obj.extensions.get_extension_for_class(
                x509.SubjectKeyIdentifier
            ).value
            aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski)
        except x509.ExtensionNotFound:
            aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(
                self._ca_cert_obj.public_key()
            )

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert_obj.subject)
            .public_key(host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(ski, critical=False)
            .add_extension(aki, critical=False)
        )
        host_cert = builder.sign(self._ca_key_obj, hashes.SHA256())

        cert_pem = host_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        key_pem = host_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode("ascii")
        return cert_pem, key_pem

    def _generate_ca(self, cert_path: Path, key_path: Path) -> None:
        """Generate a self-signed CA certificate with the extensions a modern
        trust store expects (BasicConstraints CA, KeyUsage cert-sign, SKI)."""
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError as exc:
            log.warning(
                "cryptography library not available; proxy HTTPS MITM will not work. "
                "Install with: uv add cryptography"
            )
            raise RuntimeError("HTTPS MITM requires 'cryptography' package") from exc

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "argusai proxy CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ArgusAI"),
        ])
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        not_before = _utcnow() - _dt.timedelta(minutes=5)
        not_after = _utcnow() + _dt.timedelta(days=3650)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:
            import stat as _stat

            key_path.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass
        log.info("Generated proxy CA cert at %s", cert_path)
