"""Passive WebSocket frame relay for the MITM proxy.

After a 101 upgrade the proxy stops speaking HTTP and both sides exchange raw
WebSocket frames (RFC 6455). These helpers read one frame from a stream, relay
its exact bytes to the other side unchanged (so the connection stays faithful),
and return a bounded, unmasked record for the flow history. Nothing here mutates
the traffic — it only observes it.
"""

from __future__ import annotations

import asyncio
from typing import Any

WS_OPCODES = {0: "continuation", 1: "text", 2: "binary", 8: "close", 9: "ping", 10: "pong"}
_WS_RELAY_CHUNK = 65536
# Bytes recorded per frame for the history (the full frame is still relayed).
WS_CAPTURE_PER_FRAME = 4096


async def relay_ws_frame(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    *,
    capture_cap: int = WS_CAPTURE_PER_FRAME,
) -> dict[str, Any]:
    """Read one frame from ``src``, relay its raw bytes to ``dst``, record a prefix.

    Raises ``asyncio.IncompleteReadError`` when the stream ends mid-frame (the
    caller treats that as the tunnel closing).
    """
    header = await src.readexactly(2)
    b0, b1 = header[0], header[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    ext = b""
    if length == 126:
        ext = await src.readexactly(2)
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = await src.readexactly(8)
        length = int.from_bytes(ext, "big")
    mask_key = await src.readexactly(4) if masked else b""
    dst.write(header + ext + mask_key)

    kept = bytearray()
    remaining = length
    offset = 0
    while remaining > 0:
        chunk = await src.read(min(_WS_RELAY_CHUNK, remaining))
        if not chunk:
            raise asyncio.IncompleteReadError(partial=b"", expected=remaining)
        remaining -= len(chunk)
        dst.write(chunk)
        if len(kept) < capture_cap:
            seg = chunk[: capture_cap - len(kept)]
            if masked:
                seg = bytes(byte ^ mask_key[(offset + i) % 4] for i, byte in enumerate(seg))
            kept += seg
        offset += len(chunk)
    await dst.drain()
    return {
        "fin": fin,
        "opcode": opcode,
        "size": length,
        "data": bytes(kept),
        "truncated": length > len(kept),
    }


def frame_record(raw: dict[str, Any], direction: str) -> dict[str, Any]:
    """Turn a :func:`relay_ws_frame` result into a display/search record."""
    opcode = int(raw.get("opcode", 0))
    kind = WS_OPCODES.get(opcode, f"opcode-{opcode}")
    data: bytes = raw.get("data", b"") or b""
    record: dict[str, Any] = {
        "dir": direction,
        "opcode": opcode,
        "kind": kind,
        "fin": bool(raw.get("fin", True)),
        "size": int(raw.get("size", 0)),
        "truncated": bool(raw.get("truncated")),
    }
    if opcode in (1, 8, 9, 10) or opcode == 0:
        try:
            record["text"] = data.decode("utf-8")
            return record
        except UnicodeDecodeError:
            pass
    record["hex"] = data.hex()
    return record
