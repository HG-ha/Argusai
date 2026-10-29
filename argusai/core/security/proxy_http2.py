"""HTTP/2 MITM front end for the proxy.

When the client-facing TLS leg negotiates ALPN ``h2``, the browser speaks
HTTP/2 to the proxy. This module runs a server-side ``h2`` connection,
reconstructs each stream's request, forwards it upstream over the shared
httpx client (HTTP/1.1 to the origin is fine — only the browser leg is h2),
and re-frames the response back onto the stream with proper flow control.
Captured flows look like any other flow in history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import (
    ConnectionTerminated,
    DataReceived,
    RemoteSettingsChanged,
    RequestReceived,
    StreamEnded,
    StreamReset,
    WindowUpdated,
)
from h2.exceptions import ProtocolError, StreamClosedError

from argusai.core.security.proxy_constants import (
    _MAX_BUFFERED_REQUEST_BYTES,
    _MAX_FLOW_BODY_BYTES,
    _MAX_RAW_CAPTURE_BYTES,
    _RELAY_CHUNK_BYTES,
    decode_body_prefix,
)
from argusai.core.security.proxy_flow import TrafficFlow
from argusai.core.security.proxy_media import classify_flow

log = logging.getLogger("argusai.proxy.h2")

# Request headers that are hop-by-hop / recomputed and must not be forwarded.
_REQ_SKIP = frozenset({"host", "connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "content-length"})
# Response headers that cannot cross into h2 unchanged.
_RESP_SKIP = frozenset({"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer"})


class _Stream:
    __slots__ = ("headers", "body", "truncated", "started")

    def __init__(self, headers: list[tuple[str, str]]) -> None:
        self.headers = headers
        self.body = bytearray()
        self.truncated = False
        self.started = False  # handler dispatched


async def serve_http2(server: Any, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, default_host: str, default_port: int) -> None:
    conn = H2Connection(config=H2Configuration(client_side=False, header_encoding="utf-8"))
    conn.initiate_connection()
    send_lock = asyncio.Lock()
    streams: dict[int, _Stream] = {}
    window_events: dict[int, asyncio.Event] = {}
    tasks: set[asyncio.Task[Any]] = set()

    async def flush() -> None:
        data = conn.data_to_send()
        if data:
            writer.write(data)
            await writer.drain()

    async with send_lock:
        await flush()

    def wake(stream_id: int) -> None:
        ev = window_events.get(stream_id)
        if ev is not None:
            ev.set()

    def wake_all() -> None:
        for ev in window_events.values():
            ev.set()

    def dispatch(stream_id: int) -> None:
        st = streams.get(stream_id)
        if st is None or st.started:
            return
        st.started = True
        task = asyncio.ensure_future(_handle_stream(server, conn, send_lock, flush, window_events, writer, st, stream_id, default_host))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        while True:
            data = await reader.read(_RELAY_CHUNK_BYTES)
            if not data:
                break
            try:
                events = conn.receive_data(data)
            except ProtocolError:
                async with send_lock:
                    await flush()
                break
            for event in events:
                if isinstance(event, RequestReceived):
                    streams[event.stream_id] = _Stream(list(event.headers))
                    if event.stream_ended:
                        dispatch(event.stream_id)
                elif isinstance(event, DataReceived):
                    st = streams.get(event.stream_id)
                    if st is not None:
                        room = _MAX_BUFFERED_REQUEST_BYTES - len(st.body)
                        if room > 0:
                            st.body += event.data[:room]
                        if len(event.data) > room:
                            st.truncated = True
                    if event.flow_controlled_length:
                        conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, StreamEnded):
                    dispatch(event.stream_id)
                elif isinstance(event, WindowUpdated):
                    wake_all() if event.stream_id == 0 else wake(event.stream_id)
                elif isinstance(event, RemoteSettingsChanged):
                    wake_all()  # initial window / max frame size may have grown
                elif isinstance(event, StreamReset):
                    wake(event.stream_id)
                elif isinstance(event, ConnectionTerminated):
                    raise _ConnectionDone
            async with send_lock:
                await flush()
    except (_ConnectionDone, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        for task in list(tasks):
            task.cancel()
        for task in list(tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — teardown
                pass


class _ConnectionDone(Exception):
    pass


async def _handle_stream(
    server: Any,
    conn: H2Connection,
    send_lock: asyncio.Lock,
    flush: Any,
    window_events: dict[int, asyncio.Event],
    writer: asyncio.StreamWriter,
    st: _Stream,
    stream_id: int,
    default_host: str,
) -> None:
    from argusai.core.security.proxy_filters import should_capture_host, should_capture_mime

    pseudo = {k: v for k, v in st.headers if k.startswith(":")}
    method = pseudo.get(":method", "GET")
    scheme = pseudo.get(":scheme", "https")
    authority = pseudo.get(":authority", "") or default_host
    path = pseudo.get(":path", "/")
    host = authority.split(":", 1)[0]
    url = f"{scheme}://{authority}{path}"
    regular = [(k, v) for k, v in st.headers if not k.startswith(":")]
    request_headers_dict = {k.lower(): v for k, v in regular}
    in_scope = should_capture_host(server.config, host)
    started = time.monotonic()

    client = await server._origin_client()
    forward = [(k, v) for k, v in regular if k.lower() not in _REQ_SKIP]
    request = client.build_request(method, url, headers=forward, content=bytes(st.body) or None)
    try:
        resp = await client.send(request, stream=True)
    except Exception as exc:  # noqa: BLE001 — upstream failures become a 502 on this stream
        log.debug("h2 upstream error for %s %s: %s", method, url, exc)
        async with send_lock:
            try:
                conn.send_headers(stream_id, [(":status", "502")], end_stream=True)
                await flush()
            except (StreamClosedError, ProtocolError):
                pass
        if in_scope:
            _record(server, method, url, host, regular, request_headers_dict, bytes(st.body), st.truncated,
                    status=0, resp_headers=[], raw=b"", encoding="", error=str(exc),
                    duration_ms=(time.monotonic() - started) * 1000.0)
        return

    try:
        resp_items = [(str(k), str(v)) for k, v in resp.headers.multi_items()]
        encoding = str(resp.headers.get("content-encoding", ""))
        out_headers = [(":status", str(resp.status_code))]
        out_headers += [(k, v) for k, v in resp_items if k.lower() not in _RESP_SKIP and not k.startswith(":")]
        async with send_lock:
            conn.send_headers(stream_id, out_headers)
            await flush()

        captured = bytearray()
        wire = 0
        try:
            async for chunk in resp.aiter_raw(_RELAY_CHUNK_BYTES):
                if not chunk:
                    continue
                wire += len(chunk)
                room = _MAX_RAW_CAPTURE_BYTES - len(captured)
                if room > 0:
                    captured += chunk[:room]
                await _send_data(conn, send_lock, flush, window_events, stream_id, chunk)
        except (StreamClosedError, ProtocolError):
            pass
        async with send_lock:
            try:
                conn.end_stream(stream_id)
                await flush()
            except (StreamClosedError, ProtocolError):
                pass
    finally:
        await resp.aclose()

    if not in_scope:
        return
    meta = classify_flow(url=url, response_headers={k.lower(): v for k, v in resp_items}, method=method)
    if not should_capture_mime(server.config, meta["content_type"], meta["resource_kind"]):
        return
    decoded, decoded_complete = decode_body_prefix(encoding, bytes(captured), _MAX_FLOW_BODY_BYTES)
    if encoding.strip().lower() in ("", "identity"):
        exact, full = True, wire
    else:
        exact, full = decoded_complete and wire == len(captured), (len(decoded) if decoded_complete else 0)
    _record(server, method, url, host, regular, request_headers_dict, bytes(st.body), st.truncated,
            status=resp.status_code, resp_headers=resp_items, raw=decoded, encoding=encoding,
            error=None, duration_ms=(time.monotonic() - started) * 1000.0,
            meta=meta, wire=wire, exact=exact, full=full)


async def _send_data(conn, send_lock, flush, window_events, stream_id, chunk: bytes) -> None:
    offset = 0
    while offset < len(chunk):
        try:
            window = conn.local_flow_control_window(stream_id)
        except (StreamClosedError, ProtocolError):
            return
        if window <= 0:
            ev = window_events.setdefault(stream_id, asyncio.Event())
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=30)
            except asyncio.TimeoutError:
                return
            continue
        n = min(window, conn.max_outbound_frame_size, len(chunk) - offset)
        async with send_lock:
            try:
                conn.send_data(stream_id, chunk[offset:offset + n])
                await flush()
            except (StreamClosedError, ProtocolError):
                return
        offset += n


def _record(server, method, url, host, regular, request_headers_dict, body, truncated, *,
            status, resp_headers, raw, encoding, error, duration_ms,
            meta=None, wire=0, exact=True, full=0) -> None:
    flat = {k.lower(): v for k, v in resp_headers}
    flow = TrafficFlow(
        id=server._next_flow_id(),
        timestamp=time.time(),
        method=method,
        url=url,
        host=host,
        request_headers=request_headers_dict,
        request_body=bytes(body),
        is_https=True,
        response_status=status,
        response_headers=flat,
        response_body=raw,
        error=error,
        content_type=(meta or {}).get("content_type", ""),
        resource_kind=(meta or {}).get("resource_kind", "other"),
        filename_hint=(meta or {}).get("filename_hint", ""),
        raw_request_header_items=list(regular),
        raw_response_header_items=list(resp_headers),
        response_encoding=encoding,
        duration_ms=duration_ms,
        request_full_size=len(body),
        response_full_size=full,
        response_wire_size=wire,
        response_size_exact=exact,
        http_version="h2",
    )
    server._append_flow(flow, analyze=error is None)
