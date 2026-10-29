"""Passive WebSocket capture: frames relayed intact and recorded in the flow."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import struct
from pathlib import Path

from argusai.core.events import EventBus
from argusai.core.security.proxy_analyzer import PassiveAnalyzer
from argusai.core.security.proxy_flow import ProxyConfig
from argusai.core.security.proxy_server import ProxyServer

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def _encode_frame(payload: bytes, *, opcode: int = 0x1, mask: bool = False) -> bytes:
    header = bytes([0x80 | opcode])
    ln = len(payload)
    if ln < 126:
        header += bytes([(0x80 if mask else 0) | ln])
    elif ln < 65536:
        header += bytes([(0x80 if mask else 0) | 126]) + struct.pack(">H", ln)
    else:
        header += bytes([(0x80 if mask else 0) | 127]) + struct.pack(">Q", ln)
    if mask:
        key = b"\x01\x02\x03\x04"
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return header + key + masked
    return header + payload


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b0, b1 = await reader.readexactly(2)
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", await reader.readexactly(8))[0]
    key = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(ln) if ln else b""
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return opcode, data


async def _ws_origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
    key = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + _accept_key(key).encode() + b"\r\n\r\n"
    )
    await writer.drain()
    # Greet, then echo one client frame back, then close.
    writer.write(_encode_frame(b"hello from server", opcode=0x1))
    await writer.drain()
    opcode, data = await _read_frame(reader)
    writer.write(_encode_frame(b"echo:" + data, opcode=0x1))
    await writer.drain()
    writer.write(_encode_frame(b"", opcode=0x8))  # close
    await writer.drain()
    writer.close()


async def _run(tmp_path: Path) -> ProxyServer:
    origin = await asyncio.start_server(_ws_origin, "127.0.0.1", 0)
    origin_port = origin.sockets[0].getsockname()[1]
    proxy_port = _free_port()
    bus = EventBus()
    config = ProxyConfig(listen_host="127.0.0.1", listen_port=proxy_port, ca_dir=tmp_path, allow_loopback=True)
    proxy = ProxyServer(config, PassiveAnalyzer(bus), bus)
    proxy._load_ca_material()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write((
            f"GET http://127.0.0.1:{origin_port}/chat HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin_port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        await writer.drain()
        status_line = await reader.readline()
        assert b"101" in status_line
        await reader.readuntil(b"\r\n\r\n")
        op1, greet = await _read_frame(reader)
        assert greet == b"hello from server"
        writer.write(_encode_frame(b"ping-from-client", opcode=0x1, mask=True))
        await writer.drain()
        op2, echoed = await _read_frame(reader)
        assert echoed == b"echo:ping-from-client"
        await _read_frame(reader)  # close
        writer.close()
        await asyncio.sleep(0.2)
        return proxy
    finally:
        await proxy.stop()
        origin.close()
        await origin.wait_closed()


def test_websocket_frames_relayed_and_recorded(tmp_path: Path) -> None:
    proxy = asyncio.run(_run(tmp_path))
    ws_flows = [f for f in proxy.flows if f.resource_kind == "websocket"]
    assert len(ws_flows) == 1
    flow = ws_flows[0]
    assert flow.response_status == 101
    frames = flow.websocket_frames or []
    texts = {(f["dir"], f.get("text")) for f in frames}
    assert ("down", "hello from server") in texts
    assert ("up", "ping-from-client") in texts
    assert ("down", "echo:ping-from-client") in texts
    assert flow.websocket_open is False

    detail = flow.to_dict()
    assert detail["resource_kind"] == "websocket"
    assert detail["websocket_frame_count"] >= 3
