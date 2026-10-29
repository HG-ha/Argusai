"""Shared constants and body decode helpers for the passive proxy."""

from __future__ import annotations

import datetime as _dt
import logging
import zlib

log = logging.getLogger("argusai.proxy")

_FLOW_BODY_PREVIEW = 8192  # short text preview kept for older callers / AI summaries
_MAX_FLOW_BODY_BYTES = 1024 * 1024  # 1 MiB per decoded body — enough to preview most media inline
# Still-encoded prefix kept while relaying; decoding it yields at most _MAX_FLOW_BODY_BYTES.
_MAX_RAW_CAPTURE_BYTES = 4 * _MAX_FLOW_BODY_BYTES
# Uploads above this (and every chunked upload) are streamed upstream instead of buffered.
_MAX_BUFFERED_REQUEST_BYTES = 8 * 1024 * 1024
# All captured bodies in the history list together; oldest bodies are released past this.
_HISTORY_BODY_BUDGET = 192 * 1024 * 1024
_RELAY_CHUNK_BYTES = 64 * 1024
_INLINE_BINARY_BYTES = _MAX_FLOW_BODY_BYTES  # send the whole captured payload to the UI
_HEX_DUMP_BYTES = 16 * 1024  # cap on hex dump bytes returned to the UI for unknown binaries
_DEFAULT_MAX_HISTORY = 500
_SEEN_FINGERPRINT_CAP = 10_000
# Brotli has no output cap per call; tiny input slices bound a decompression bomb's overshoot.
_BROTLI_SLICE_BYTES = 256
def _utcnow() -> _dt.datetime:
    """Timezone-aware UTC ``datetime`` (replaces deprecated ``utcnow()``)."""
    return _dt.datetime.now(_dt.timezone.utc)


def _decode_response_body(content_encoding: str, raw: bytes, limit: int = 64 * 1024 * 1024) -> bytes:
    """Best-effort decompression of an HTTP response body, capped at ``limit`` bytes.

    The proxy forwards raw compressed bytes to the client (so the browser keeps
    handling ``Content-Encoding`` itself), but the passive analyzer, HAR export
    and "save body" feature all want **plaintext**.
    """
    return decode_body_prefix(content_encoding, raw, limit)[0]


def decode_body_prefix(content_encoding: str, raw: bytes, limit: int) -> tuple[bytes, bool]:
    """Decode at most ``limit`` bytes of an (optionally truncated) encoded body.

    Returns ``(decoded, complete)``; ``complete`` is False when the output cap
    was hit. Handles ``gzip`` / ``deflate`` / ``br`` / ``zstd`` and falls back
    to the raw bytes whenever the decoder is unavailable or the data is
    malformed. Never inflates past ``limit``, so a decompression bomb costs at
    most ``limit`` bytes of memory.
    """
    limit = max(0, int(limit))
    if not raw:
        return b"", True
    encoding = (content_encoding or "").strip().lower()
    if encoding and encoding != "identity":
        try:
            if encoding in ("gzip", "x-gzip"):
                return _zlib_prefix(raw, 16 + zlib.MAX_WBITS, limit)
            if encoding == "deflate":
                try:
                    return _zlib_prefix(raw, zlib.MAX_WBITS, limit)
                except zlib.error:
                    return _zlib_prefix(raw, -zlib.MAX_WBITS, limit)
            if encoding == "br":
                decoded = _brotli_prefix(raw, limit)
                if decoded is not None:
                    return decoded
            elif encoding in ("zstd", "zstandard"):
                decoded = _zstd_prefix(raw, limit)
                if decoded is not None:
                    return decoded
        except Exception as exc:
            log.debug("proxy body decode failed (encoding=%s): %s", encoding, exc)
    return raw[:limit], len(raw) <= limit


def _zlib_prefix(raw: bytes, wbits: int, limit: int) -> tuple[bytes, bool]:
    decoder = zlib.decompressobj(wbits)
    out = decoder.decompress(raw, limit) if limit else b""
    return out, not decoder.unconsumed_tail and (decoder.eof or len(out) < limit)


def _brotli_prefix(raw: bytes, limit: int) -> tuple[bytes, bool] | None:
    try:
        import brotli  # type: ignore[import-not-found]
    except ImportError:
        return None
    decoder = brotli.Decompressor()
    out = bytearray()
    for start in range(0, len(raw), _BROTLI_SLICE_BYTES):
        out += decoder.process(raw[start:start + _BROTLI_SLICE_BYTES])
        if len(out) >= limit:
            return bytes(out[:limit]), False
    return bytes(out), True


def _zstd_prefix(raw: bytes, limit: int) -> tuple[bytes, bool] | None:
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError:
        return None
    import io

    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True)
    out = bytearray()
    while len(out) < limit:
        try:
            chunk = reader.read(min(_RELAY_CHUNK_BYTES, limit - len(out)))
        except zstandard.ZstdError:
            # Truncated capture: keep whatever decoded cleanly.
            return bytes(out), False
        if not chunk:
            return bytes(out), True
        out += chunk
    return bytes(out), False
