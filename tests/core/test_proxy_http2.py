"""HTTP/2 MITM: an h2 browser leg is captured and relayed (1.1 upstream)."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import socket
import ssl
from pathlib import Path

import httpx

from argusai.core.events import EventBus
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_flow import ProxyConfig
from argusai.core.security.proxy_server import ProxyServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_cert(tmp_path: Path) -> Path:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "origin.pem"
    pem.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    )
    return pem


async def _origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # Minimal HTTP/1.1 origin (the proxy->origin leg is 1.1).
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    first = head.split("\r\n", 1)[0]
    method, target = first.split()[0], first.split()[1]
    length = 0
    for line in head.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip() or "0")
    if length:
        await reader.readexactly(length)
    if "/big" in target:
        body = b"x" * (200 * 1024)
    else:
        body = f"method={method} path={target}".encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    await writer.drain()
    writer.close()


async def _run(tmp_path: Path):
    pem = _make_cert(tmp_path)
    octx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    octx.load_cert_chain(str(pem))
    origin = await asyncio.start_server(_origin, "127.0.0.1", 0, ssl=octx)
    origin_port = origin.sockets[0].getsockname()[1]

    proxy_port = _free_port()
    bus = EventBus()
    config = ProxyConfig(listen_host="127.0.0.1", listen_port=proxy_port, ca_dir=tmp_path, allow_loopback=True)
    proxy = ProxyServer(config, PassiveAnalyzer(bus), bus)
    proxy._load_ca_material()
    await proxy.start()
    ca_path = tmp_path / "ca.pem"
    try:
        async with httpx.AsyncClient(
            http2=True,
            proxy=f"http://127.0.0.1:{proxy_port}",
            verify=str(ca_path),
            trust_env=False,
            timeout=20,
        ) as client:
            r1 = await client.get(f"https://127.0.0.1:{origin_port}/hello")
            r2 = await client.post(f"https://127.0.0.1:{origin_port}/submit", content=b"payload")
            r3 = await client.get(f"https://127.0.0.1:{origin_port}/big")
        return proxy, (r1, r2, r3)
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_http2_browser_leg_is_captured_and_relayed(tmp_path: Path) -> None:
    proxy, (r1, r2, r3) = asyncio.run(_run(tmp_path))

    # The client negotiated HTTP/2 with the proxy.
    assert r1.http_version == "HTTP/2"
    assert r1.status_code == 200 and "method=GET" in r1.text
    assert r2.status_code == 200 and "method=POST" in r2.text
    # Large body survived flow-controlled re-framing intact.
    assert r3.status_code == 200 and len(r3.content) == 200 * 1024

    h2_flows = [f for f in proxy.flows if f.http_version == "h2"]
    assert len(h2_flows) == 3
    paths = sorted(f.url.rsplit("/", 1)[-1] for f in h2_flows)
    assert paths == ["big", "hello", "submit"]
    big = next(f for f in h2_flows if f.url.endswith("/big"))
    assert big.response_size == 200 * 1024
