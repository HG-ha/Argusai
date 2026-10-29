"""End-to-end relay tests for the passive MITM proxy.

All origins are local, no network egress. We exercise:
- plain HTTP forwarding and capture
- HTTPS MITM with a synthetic self-signed origin
- gzip body decompression for the analyzer (not for the wire)
- multi-value Set-Cookie preservation + per-cookie analysis
- HTTP keep-alive across two requests on the same TCP connection
- WebSocket Upgrade refusal (502)
- per-host certificate reuse from disk on restart
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import gzip
import ipaddress
import socket
import ssl
from pathlib import Path

import httpx
import pytest

from argusai.core.events import EventBus
from argusai.core.security.proxy import PassiveAnalyzer, ProxyConfig, ProxyServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_origin_cert(tmp_path: Path) -> Path:
    """Create a self-signed cert (loopback IP SAN) for the local TLS origin."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "origin.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return pem


async def _start_origin(
    handler,
    *,
    tls: ssl.SSLContext | None = None,
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _start_proxy(tmp_path: Path) -> tuple[ProxyServer, EventBus, int]:
    proxy_port = _free_port()
    bus = EventBus()
    config = ProxyConfig(
        listen_host="127.0.0.1",
        listen_port=proxy_port,
        ca_dir=tmp_path,
        allow_loopback=True,  # tests use local origins on 127.0.0.1
    )
    proxy = ProxyServer(config, PassiveAnalyzer(bus), bus)
    proxy._load_ca_material()
    await proxy.start()
    return proxy, bus, proxy_port


# ---------------------------------------------------------------------------
# Plain HTTP forwarding
# ---------------------------------------------------------------------------


def _serve_plain(body: bytes, *, extra_headers: bytes = b"") -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        + extra_headers
        + b"\r\n"
        + body
    )


async def _async_http_forward(tmp_path: Path) -> None:
    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_serve_plain(b"hello"))
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}",
            trust_env=False,
            timeout=10,
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{origin_port}/hello")
        assert resp.status_code == 200
        assert resp.text == "hello"
        assert len(proxy.flows) == 1
        flow = proxy.flows[0]
        assert flow.url.endswith("/hello")
        assert flow.response_body == b"hello"
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_http_forward_and_capture(tmp_path: Path) -> None:
    asyncio.run(_async_http_forward(tmp_path))


# ---------------------------------------------------------------------------
# HTTPS MITM, local origin
# ---------------------------------------------------------------------------


async def _async_https_mitm(tmp_path: Path) -> None:
    pem = _make_origin_cert(tmp_path)
    origin_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    origin_ctx.load_cert_chain(str(pem))

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(_serve_plain(b"secure"))
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler, tls=origin_ctx)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    ca_path = tmp_path / "ca.pem"
    assert ca_path.is_file()
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}",
            verify=str(ca_path),
            trust_env=False,
            timeout=20,
        ) as client:
            resp = await client.get(f"https://127.0.0.1:{origin_port}/secret")
        assert resp.status_code == 200
        assert resp.text == "secure"
        hosts = {flow.host for flow in proxy.flows}
        assert "127.0.0.1" in hosts
        flow = proxy.flows[-1]
        assert flow.is_https is True
        assert flow.response_body == b"secure"
        assert flow.duration_ms >= 0
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_https_mitm_local(tmp_path: Path) -> None:
    asyncio.run(_async_https_mitm(tmp_path))


# ---------------------------------------------------------------------------
# Gzip decoding for the analyzer (raw bytes forwarded to client)
# ---------------------------------------------------------------------------


async def _async_gzip_decoded(tmp_path: Path) -> None:
    payload = b'{"sql_error":"You have an error in your SQL syntax near LINE 1"}'
    compressed = gzip.compress(payload)
    body_response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Encoding: gzip\r\n"
        b"Content-Length: " + str(len(compressed)).encode() + b"\r\n\r\n"
        + compressed
    )

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(body_response)
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}",
            trust_env=False,
            timeout=10,
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{origin_port}/api/q")
        # httpx auto-decodes gzip on the wire.
        assert resp.json()["sql_error"].startswith("You have an error")
        assert len(proxy.flows) == 1
        flow = proxy.flows[0]
        # response_body is the *decoded* body so PassiveAnalyzer regexes work.
        assert b"You have an error in your SQL syntax" in flow.response_body
        assert flow.response_encoding.lower() == "gzip"
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_decodes_gzip_for_analyzer(tmp_path: Path) -> None:
    asyncio.run(_async_gzip_decoded(tmp_path))


# ---------------------------------------------------------------------------
# Multi-value Set-Cookie
# ---------------------------------------------------------------------------


async def _async_multi_set_cookie(tmp_path: Path) -> None:
    body = b"ok"
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Set-Cookie: sid=abc; Path=/\r\n"
        b"Set-Cookie: csrf=xyz; Path=/; Secure; HttpOnly; SameSite=Lax\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        + body
    )

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(response)
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, bus, proxy_port = await _start_proxy(tmp_path)

    cookie_findings: list[dict] = []

    def _capture(evt) -> None:
        if evt.type == "finding":
            data = evt.payload.get("finding") or {}
            if data.get("id") == "proxy.cookie-flags":
                cookie_findings.append(data)

    unsub = bus.subscribe(_capture)
    try:
        async with httpx.AsyncClient(
            proxy=f"http://127.0.0.1:{proxy_port}",
            trust_env=False,
            timeout=10,
        ) as client:
            resp = await client.get(f"http://127.0.0.1:{origin_port}/login")
        cookies = resp.headers.get_list("set-cookie")
        assert any("sid=abc" in c for c in cookies)
        assert any("csrf=xyz" in c for c in cookies)

        flow = proxy.flows[0]
        raw_cookies = [v for k, v in flow.raw_response_header_items if k.lower() == "set-cookie"]
        assert len(raw_cookies) == 2

        # The analyzer was run inline by the proxy. We should see exactly one
        # cookie-flags finding (for ``sid``), not for the well-formed ``csrf``.
        flagged_names = {f["evidence"]["cookie_name"] for f in cookie_findings}
        assert "sid" in flagged_names
        assert "csrf" not in flagged_names
    finally:
        unsub()
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_preserves_multi_set_cookie(tmp_path: Path) -> None:
    asyncio.run(_async_multi_set_cookie(tmp_path))


# ---------------------------------------------------------------------------
# HTTP keep-alive across two pipelined requests on one TCP connection
# ---------------------------------------------------------------------------


async def _async_http_keepalive(tmp_path: Path) -> None:
    seen = 0
    lock = asyncio.Lock()

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal seen
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                # Drain remaining headers.
                while True:
                    h = await reader.readline()
                    if not h or h in (b"\r\n", b"\n"):
                        break
                async with lock:
                    seen += 1
                writer.write(_serve_plain(b"ok"))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            # Server.wait_closed() waits for connection *transports*, not for
            # handler coroutines. Leaving this open hangs teardown forever.
            with contextlib.suppress(Exception):
                writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        # Open a raw TCP connection to the proxy and pipeline two GETs.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        url_path = "/keepalive"
        for _ in range(2):
            writer.write(
                f"GET http://127.0.0.1:{origin_port}{url_path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{origin_port}\r\n"
                f"Connection: keep-alive\r\n\r\n".encode()
            )
        await writer.drain()

        # Read 2 responses framed by Content-Length.
        for _ in range(2):
            status_line = await reader.readline()
            assert status_line.startswith(b"HTTP/1.1 200"), status_line
            length = 0
            while True:
                hdr = await reader.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
                if hdr.lower().startswith(b"content-length:"):
                    length = int(hdr.split(b":", 1)[1].strip())
            body = await reader.readexactly(length)
            assert body == b"ok"

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await asyncio.sleep(0.05)
        # Both requests must have reached the proxy and the origin.
        assert len(proxy.flows) == 2
        assert seen == 2
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_supports_http_keepalive(tmp_path: Path) -> None:
    asyncio.run(_async_http_keepalive(tmp_path))


# ---------------------------------------------------------------------------
# WebSocket Upgrade to an unreachable origin closes without a 502 hijack
# (successful capture is covered in test_proxy_websocket.py)
# ---------------------------------------------------------------------------


async def _async_websocket_unreachable(tmp_path: Path) -> None:
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            b"GET http://127.0.0.1:1/ws HTTP/1.1\r\n"  # port 1: nothing listens
            b"Host: 127.0.0.1:1\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        await writer.drain()
        # The tunnel could not reach the origin, so the client leg is closed
        # rather than served a fake HTTP response.
        assert await reader.read() == b""
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await proxy.stop()


def test_proxy_websocket_unreachable_origin_closes(tmp_path: Path) -> None:
    asyncio.run(_async_websocket_unreachable(tmp_path))


# ---------------------------------------------------------------------------
# Per-host certificate is reused after restart
# ---------------------------------------------------------------------------


def test_host_cert_persisted_and_reused(tmp_path: Path) -> None:
    bus = EventBus()
    config = ProxyConfig(listen_host="127.0.0.1", listen_port=_free_port(), ca_dir=tmp_path)
    p1 = ProxyServer(config, PassiveAnalyzer(bus), bus)
    p1._load_ca_material()
    ctx1 = p1._ssl_for_host("example.test")
    cert_path = tmp_path / "certs" / "example.test.pem"
    assert cert_path.is_file()
    mtime1 = cert_path.stat().st_mtime

    # Simulate a fresh process: new ProxyServer instance reading from disk.
    p2 = ProxyServer(config, PassiveAnalyzer(bus), bus)
    p2._load_ca_material()
    ctx2 = p2._ssl_for_host("example.test")
    mtime2 = cert_path.stat().st_mtime
    assert mtime2 == mtime1, "host cert should be reused, not regenerated"
    assert ctx1 is not ctx2  # SSLContext is per-instance, but the on-disk cert is shared.


# ---------------------------------------------------------------------------
# Modern leaf certificate has the extensions Chromium requires
# ---------------------------------------------------------------------------


def test_leaf_cert_has_required_extensions_and_ip_san(tmp_path: Path) -> None:
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID

    bus = EventBus()
    config = ProxyConfig(listen_host="127.0.0.1", listen_port=_free_port(), ca_dir=tmp_path)
    proxy = ProxyServer(config, PassiveAnalyzer(bus), bus)
    proxy._load_ca_material()
    proxy._ssl_for_host("127.0.0.1")

    cert = x509.load_pem_x509_certificate((tmp_path / "certs" / "127.0.0.1.pem").read_bytes())
    eku = cert.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in list(eku)
    bc = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    assert bc.ca is False
    san = cert.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    ips = [str(e.value) for e in san if isinstance(e, x509.IPAddress)]
    assert "127.0.0.1" in ips


# ---------------------------------------------------------------------------
# Manager-level startup error surfaces immediately (port in use)
# ---------------------------------------------------------------------------


def test_manager_reports_bind_error_promptly(tmp_path: Path) -> None:
    from argusai.core.security.proxy import ProxyManager

    # Hold a port so the proxy fails to bind.
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        mgr = ProxyManager(ca_dir=tmp_path)
        result = mgr.start_proxy(
            ProxyConfig(listen_host="127.0.0.1", listen_port=port),
            startup_timeout=3.0,
        )
        assert result["ok"] is False
        assert "bind" in (result["error"] or "").lower() or "address" in (result["error"] or "").lower()
        assert mgr.is_running is False
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# Streaming: big bodies pass through without being buffered or truncated
# ---------------------------------------------------------------------------

_BIG = 12 * 1024 * 1024


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            return headers
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()


async def _read_request_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        out = bytearray()
        while True:
            size = int((await reader.readline()).split(b";")[0].strip(), 16)
            if size == 0:
                await reader.readline()
                return bytes(out)
            out += await reader.readexactly(size)
            await reader.readline()
    return await reader.readexactly(int(headers.get("content-length", "0") or 0))


async def _async_download_streams_before_origin_finishes(tmp_path: Path) -> None:
    from argusai.core.security.proxy_constants import _MAX_FLOW_BODY_BYTES

    first_part_seen = asyncio.Event()
    head = b"a" * 65536
    tail = b"b" * (_BIG - len(head))

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
            + f"Content-Length: {_BIG}\r\n\r\n".encode()
            + head
        )
        await writer.drain()
        # A buffering proxy never forwards ``head`` and this times out.
        await asyncio.wait_for(first_part_seen.wait(), timeout=10)
        writer.write(tail)
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", trust_env=False, timeout=20) as client:
            async with client.stream("GET", f"http://127.0.0.1:{origin_port}/big.bin") as resp:
                received = 0
                async for chunk in resp.aiter_raw():
                    received += len(chunk)
                    first_part_seen.set()
        assert received == _BIG
        flow = proxy.flows[-1]
        assert len(flow.response_body) == _MAX_FLOW_BODY_BYTES
        assert flow.response_size == _BIG
        assert flow.response_size_exact is True
        assert flow.response_body_truncated is True
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_streams_large_download_without_buffering(tmp_path: Path) -> None:
    asyncio.run(_async_download_streams_before_origin_finishes(tmp_path))


async def _async_chunked_and_gzip_bomb(tmp_path: Path) -> None:
    from argusai.core.security.proxy_constants import _MAX_FLOW_BODY_BYTES

    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
            b"Content-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        for start in range(0, len(bomb), 4096):
            part = bomb[start:start + 4096]
            writer.write(b"%x\r\n" % len(part) + part + b"\r\n")
        writer.write(b"0\r\n\r\n")
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", trust_env=False, timeout=20) as client:
            async with client.stream("GET", f"http://127.0.0.1:{origin_port}/bomb") as resp:
                assert resp.headers.get("transfer-encoding") == "chunked"
                raw = b"".join([chunk async for chunk in resp.aiter_raw()])
        assert raw == bomb  # still compressed on the wire, byte for byte
        flow = proxy.flows[-1]
        assert len(flow.response_body) == _MAX_FLOW_BODY_BYTES
        assert flow.response_size_exact is False
        assert flow.response_wire_size == len(bomb)
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_relays_chunked_and_bounds_gzip_bomb(tmp_path: Path) -> None:
    asyncio.run(_async_chunked_and_gzip_bomb(tmp_path))


async def _async_large_uploads(tmp_path: Path) -> None:
    import hashlib

    from argusai.core.security.proxy_constants import _MAX_FLOW_BODY_BYTES

    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                if not await reader.readline():
                    return
                headers = await _read_headers(reader)
                body = await _read_request_body(reader, headers)
                answer = f"{len(body)} {hashlib.sha256(body).hexdigest()}".encode()
                writer.write(_serve_plain(answer))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    payload = bytes(range(256)) * (_BIG // 256)

    async def chunked_payload():
        for start in range(0, len(payload), 1 << 20):
            yield payload[start:start + (1 << 20)]

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    expected = f"{len(payload)} {hashlib.sha256(payload).hexdigest()}"
    try:
        async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", trust_env=False, timeout=30) as client:
            fixed = await client.post(f"http://127.0.0.1:{origin_port}/upload", content=payload)
            chunked = await client.post(f"http://127.0.0.1:{origin_port}/upload", content=chunked_payload())
        assert fixed.text == expected
        assert chunked.text == expected
        for flow in proxy.flows[-2:]:
            assert len(flow.request_body) == _MAX_FLOW_BODY_BYTES
            assert flow.request_full_size == len(payload)
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_streams_large_uploads_intact(tmp_path: Path) -> None:
    asyncio.run(_async_large_uploads(tmp_path))


async def _async_expect_continue(tmp_path: Path) -> None:
    async def origin_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        headers = await _read_headers(reader)
        assert "expect" not in headers
        body = await _read_request_body(reader, headers)
        writer.write(_serve_plain(body.upper()))
        await writer.drain()
        writer.close()

    origin, origin_port = await _start_origin(origin_handler)
    proxy, _bus, proxy_port = await _start_proxy(tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"POST http://127.0.0.1:{origin_port}/form HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin_port}\r\nContent-Length: 5\r\n"
            "Expect: 100-continue\r\n\r\n".encode()
        )
        await writer.drain()
        interim = await asyncio.wait_for(reader.readline(), timeout=5)
        assert interim.startswith(b"HTTP/1.1 100"), interim
        await reader.readline()
        writer.write(b"hello")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout=10)
        assert status.startswith(b"HTTP/1.1 200"), status
        headers = await _read_headers(reader)
        assert await reader.readexactly(int(headers["content-length"])) == b"HELLO"
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_proxy_answers_expect_continue(tmp_path: Path) -> None:
    asyncio.run(_async_expect_continue(tmp_path))


# Suppress noisy "unused" warning when pytest auto-collects only test_* names.
_ = pytest
