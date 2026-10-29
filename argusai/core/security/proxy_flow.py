"""Proxy configuration and captured traffic model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argusai.core.security.proxy_constants import (
    _FLOW_BODY_PREVIEW,
    _HEX_DUMP_BYTES,
    _INLINE_BINARY_BYTES,
    _MAX_FLOW_BODY_BYTES,
    _DEFAULT_MAX_HISTORY,
)

# Server-side pretty JSON doubles the payload; only worth it for small bodies.
_PRETTY_JSON_CHARS = 64 * 1024


@dataclass
class ProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8866
    ca_dir: Path | None = None      # dir to store CA cert/key
    max_history: int = _DEFAULT_MAX_HISTORY
    scope: list[str] = field(default_factory=list)  # host patterns (mode-dependent)
    deny_hosts: list[str] = field(default_factory=list)
    bypass_hosts: list[str] = field(default_factory=list)  # never capture (AI endpoints, etc.)
    intercept_mode: str = "all"  # all | allowlist | denylist
    mime_allow: list[str] = field(default_factory=list)  # e.g. video/*, api, application/json
    # Opt-in escape hatch for tests / advanced workflows where the user wants
    # to MITM loopback origins. NEVER enable this from the GUI without a very
    # loud confirmation — it lets the proxy capture traffic to ArgusAI itself.
    allow_loopback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "scope": self.scope,
            "deny_hosts": self.deny_hosts,
            "bypass_hosts": self.bypass_hosts,
            "intercept_mode": self.intercept_mode,
            "mime_allow": self.mime_allow,
            "max_history": self.max_history,
        }


# ---------------------------------------------------------------------------
# Traffic model
# ---------------------------------------------------------------------------

@dataclass
class TrafficFlow:
    """A single HTTP request/response pair captured by the proxy.

    ``response_body`` is always the **decoded** payload (gzip/br already
    expanded). ``raw_response_header_items`` preserves the wire-order multi
    value headers (Set-Cookie, WWW-Authenticate, ...) that the flattened
    ``response_headers`` dict cannot represent.
    """

    id: int
    timestamp: float
    method: str
    url: str
    host: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: bytes = b""
    response_status: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: bytes = b""
    error: str | None = None
    is_https: bool = False
    http_version: str = "http/1.1"  # "h2" for flows captured on the HTTP/2 front end
    content_type: str = ""
    resource_kind: str = "other"
    filename_hint: str = ""
    raw_request_header_items: list[tuple[str, str]] = field(default_factory=list)
    raw_response_header_items: list[tuple[str, str]] = field(default_factory=list)
    response_encoding: str = ""  # original Content-Encoding (gzip/br/...) if any
    duration_ms: float = 0.0
    response_full_size: int = 0  # bytes received from origin (after decoding) before cap
    request_full_size: int = 0   # bytes received from client before cap
    response_wire_size: int = 0  # bytes relayed to the client, still content-encoded
    # False when only a prefix of an encoded body was decoded, so the decoded
    # total is unknown and ``response_wire_size`` is the best size we have.
    response_size_exact: bool = True
    body_evicted: bool = False  # bodies released by the history memory budget
    # Decoded / decrypted views filled in by ProxyDecryptService:
    # {"request": {"texts": [...], "items": [...]}, "response": {...}}
    decoded: dict[str, Any] | None = None
    # ProxyDecryptService.seq at the last change of ``decoded``; lets readers
    # re-check only the flows whose plaintext changed.
    decode_seq: int = 0
    # Non-empty when an active rewrite rule changed this flow ("request",
    # "response", "both", or "short_circuit").
    rewritten: str = ""
    # WebSocket frames captured passively after a 101 upgrade. Each item:
    # {dir: up|down, opcode, kind: text|binary|ping|pong|close, size, data, truncated}.
    websocket_frames: list[dict[str, Any]] | None = None
    websocket_open: bool = False  # the tunnel is still live

    @property
    def response_size(self) -> int:
        if self.response_size_exact:
            return self.response_full_size or len(self.response_body)
        return max(self.response_wire_size, len(self.response_body))

    @property
    def response_body_truncated(self) -> bool:
        return not self.response_size_exact or self.response_full_size > len(self.response_body)

    @property
    def request_body_truncated(self) -> bool:
        return self.request_full_size > len(self.request_body)

    @property
    def decrypted(self) -> bool:
        decoded = self.decoded or {}
        return any((decoded.get(part) or {}).get("texts") for part in ("request", "response"))

    def decoded_bytes(self) -> int:
        decoded = self.decoded or {}
        return sum(
            len(text)
            for part in ("request", "response")
            for text in (decoded.get(part) or {}).get("texts") or []
        )

    def body_bytes(self) -> int:
        return len(self.request_body) + len(self.response_body) + self.decoded_bytes()

    def evict_bodies(self) -> int:
        """Drop captured bodies (metadata stays); returns the bytes released."""
        released = self.body_bytes()
        if released:
            self.request_body = b""
            self.response_body = b""
            self.decoded = None
            self.body_evicted = True
        return released

    def to_dict(
        self,
        *,
        include_body_preview: bool = False,
        text_limit: int | None = None,
        inline_media: bool = True,
    ) -> dict[str, Any]:
        """``text_limit`` caps text bodies (chars) and ``inline_media=False``
        skips base64 payloads, so callers can bound what crosses the bridge."""
        payload = {
            "id": self.id,
            "timestamp": self.timestamp,
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "status": self.response_status,
            "request_size": self.request_full_size or len(self.request_body),
            "response_size": self.response_size,
            "response_wire_size": self.response_wire_size,
            "response_size_exact": self.response_size_exact,
            "is_https": self.is_https,
            "http_version": self.http_version,
            "content_type": self.content_type,
            "resource_kind": self.resource_kind,
            "filename_hint": self.filename_hint,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 2),
            "body_evicted": self.body_evicted,
            "decrypted": self.decrypted,
            "rewritten": self.rewritten,
        }
        if self.websocket_frames is not None:
            frames = self.websocket_frames
            payload["websocket_open"] = self.websocket_open
            payload["websocket_frame_count"] = len(frames)
            # Summaries stay light; the inspector/detail view shows full frames.
            limit = 40 if not include_body_preview else len(frames)
            payload["websocket_frames"] = [dict(f) for f in frames[:limit]]
            if len(frames) > limit:
                payload["websocket_frames_truncated"] = True
        if include_body_preview:
            req_ct = self._first_request_content_type()
            req_kind = self._classify_body(self.request_body, req_ct)
            resp_kind = self._classify_body(self.response_body, self.content_type)
            req_meta = self._render_body(self.request_body, req_kind, req_ct, text_limit, inline_media)
            resp_meta = self._render_body(self.response_body, resp_kind, self.content_type, text_limit, inline_media)
            payload.update({
                "request_headers": dict(self.request_headers),
                "response_headers": self._display_response_headers(),
                "request_body": req_meta,
                "response_body": resp_meta,
                "request_body_preview": self._preview(self.request_body),
                "response_body_preview": self._preview(self.response_body),
                "request_body_truncated": self.request_body_truncated,
                "response_body_truncated": self.response_body_truncated,
                "filename_hint": self.filename_hint,
            })
        return payload

    def _first_request_content_type(self) -> str:
        for k, v in self.raw_request_header_items:
            if k.lower() == "content-type":
                return v.split(";", 1)[0].strip().lower()
        ct = self.request_headers.get("content-type", "")
        return ct.split(";", 1)[0].strip().lower() if ct else ""

    @staticmethod
    def _classify_body(data: bytes, content_type: str) -> str:
        """Return one of: empty | image | video | audio | json | html | xml |
        javascript | css | text | binary."""
        if not data:
            return "empty"
        ct = (content_type or "").split(";", 1)[0].strip().lower()
        if ct.startswith("image/"):
            return "image"
        if ct.startswith("video/"):
            return "video"
        if ct.startswith("audio/"):
            return "audio"
        probe = data[:1024]
        nontext = sum(1 for b in probe if b < 9 or (13 < b < 32) or b == 127)
        # Declared-text bodies are trusted unless mostly control bytes (mislabelled
        # binaries, zero-filled payloads). Not a UTF-8 check: GBK pages are text.
        if nontext > len(probe) * 0.3:
            return "binary"
        if ct == "application/json" or ct.endswith("+json"):
            return "json"
        if ct == "text/html" or ct == "application/xhtml+xml":
            return "html"
        if ct == "text/xml" or ct == "application/xml" or ct.endswith("+xml"):
            return "xml"
        if ct in ("application/javascript", "text/javascript") or ct.endswith("/ecmascript"):
            return "javascript"
        if ct == "text/css":
            return "css"
        if ct.startswith("text/"):
            return "text"
        # Heuristic: probe whether bytes are mostly printable UTF-8.
        try:
            probe.decode("utf-8")
        except UnicodeDecodeError as exc:
            # The 1 KiB slice may split a multi-byte character at its end.
            if exc.start < len(probe) - 3 or len(data) <= len(probe):
                return "binary"
        if nontext > len(probe) * 0.1:
            return "binary"
        return "text"

    def _render_body(
        self,
        data: bytes,
        kind: str,
        content_type: str,
        text_limit: int | None = None,
        inline_media: bool = True,
    ) -> dict[str, Any]:
        """Produce a UI-friendly payload for a body of the given kind."""
        meta: dict[str, Any] = {
            "kind": kind,
            "size": len(data),
            "content_type": content_type or "",
        }
        if kind == "empty" or not data:
            return meta
        if kind in ("image", "video", "audio", "binary"):
            import base64

            if inline_media and len(data) <= _INLINE_BINARY_BYTES:
                meta["base64"] = base64.b64encode(data).decode("ascii")
            else:
                meta["base64_truncated"] = True
            if kind == "binary":
                hex_bytes = _HEX_DUMP_BYTES if inline_media else _HEX_DUMP_BYTES // 16
                meta["hex"] = data[:hex_bytes].hex()
                meta["hex_truncated"] = len(data) > hex_bytes
            return meta
        # Text-like body. Capture-time truncation is signalled separately via
        # ``*_body_truncated``; ``text_limited`` only means this response was
        # shortened for transport and the caller can ask again without a limit.
        text = data.decode("utf-8", errors="replace")
        if text_limit and len(text) > text_limit:
            meta["text_limited"] = True
            meta["text_chars"] = len(text)
            text = text[:text_limit]
        meta["text"] = text
        if kind == "json" and not meta.get("text_limited") and len(text) <= _PRETTY_JSON_CHARS:
            try:
                import json as _json

                meta["pretty"] = _json.dumps(
                    _json.loads(text),
                    ensure_ascii=False,
                    indent=2,
                )
            except Exception:
                # Leave only the raw text when JSON is malformed.
                pass
        return meta

    def _display_response_headers(self) -> dict[str, str]:
        """Flatten multi-value response headers for the UI."""
        if not self.raw_response_header_items:
            return dict(self.response_headers)
        merged: dict[str, list[str]] = {}
        for name, value in self.raw_response_header_items:
            merged.setdefault(name, []).append(value)
        out: dict[str, str] = {}
        for name, values in merged.items():
            if len(values) == 1:
                out[name] = values[0]
            elif name.lower() == "set-cookie":
                # Set-Cookie values may contain commas (Expires=...). Use \n so the
                # UI shows one cookie per line without ambiguity.
                out[name] = "\n".join(values)
            else:
                out[name] = ", ".join(values)
        return out

    @staticmethod
    def _preview(data: bytes, limit: int = _FLOW_BODY_PREVIEW) -> str:
        if not data:
            return ""
        text = data[:limit].decode("utf-8", errors="replace")
        if len(data) > limit:
            text += f"\n…[{len(data) - limit} bytes truncated]"
        return text
