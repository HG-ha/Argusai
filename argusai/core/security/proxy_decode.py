"""Zero-config decoding of common encodings found in captured bodies.

No keys involved: peels Base64 / Base64URL / hex / URL / ``\\uXXXX`` escapes /
JWT and gzip / zlib / zstd layers off the whole body, JSON string fields,
form fields and URL query parameters. When the last layer is high-entropy
binary it is reported as probable ciphertext with a block-size guess, which is
the cue to write a decryption rule.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from argusai.core.security.proxy_constants import decode_body_prefix

MAX_LAYERS = 4
MAX_CANDIDATES = 64
MIN_ENCODED_CHARS = 16
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_CHARS = 256 * 1024

_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]+$")
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_UNI_RE = re.compile(r"\\u[0-9A-Fa-f]{4}")


def _printable_ratio(text: str) -> float:
    if not text:
        return 0.0
    good = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return good / len(text)


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def _as_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if _printable_ratio(text) >= 0.9 else None


def _b64url(segment: str) -> bytes:
    pad = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(pad.encode("ascii"))


# File formats that are high-entropy but not ciphertext.
_KNOWN_MAGIC = (
    b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"%PDF", b"PK\x03\x04", b"RIFF", b"\x00asm",
    b"OggS", b"fLaC", b"ID3", b"wOFF", b"wOF2", b"7z\xbc\xaf", b"Rar!", b"\x1aE\xdf\xa3",
)


def _cipher_hint(data: bytes) -> str:
    if data.startswith(b"Salted__"):
        return "openssl-salted"
    if data.startswith(_KNOWN_MAGIC) or data[4:8] == b"ftyp":
        return ""
    if len(data) < 16 or _entropy(data) < 6.0:
        return ""
    if len(data) % 16 == 0:
        return "block16"
    if len(data) % 8 == 0:
        return "block8"
    return "stream"


def _decompress(data: bytes) -> tuple[str, bytes] | None:
    if data[:2] == b"\x1f\x8b":
        out, _complete = decode_body_prefix("gzip", data, MAX_INPUT_BYTES)
        return ("gzip", out) if out else None
    if len(data) > 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
        out, _complete = decode_body_prefix("deflate", data, MAX_INPUT_BYTES)
        return ("zlib", out) if out else None
    if data[:4] == b"\x28\xb5\x2f\xfd":
        out, _complete = decode_body_prefix("zstd", data, MAX_INPUT_BYTES)
        return ("zstd", out) if out else None
    return None


def _decode_text_layer(value: str) -> tuple[str, Any] | None:
    """One text→(bytes|str) decode step, or None when ``value`` is not encoded."""
    v = value.strip()
    if not v:
        return None
    if _JWT_RE.match(v):
        try:
            header_b64, payload_b64, _sig = v.split(".")
            header = json.loads(_b64url(header_b64))
            payload = json.loads(_b64url(payload_b64))
        except Exception:
            return None
        return "jwt", json.dumps({"header": header, "payload": payload}, ensure_ascii=False, indent=2)
    escapes = _UNI_RE.findall(v)
    # Mostly-escaped strings only; a page that merely contains ``\u0026`` is not encoded.
    if escapes and len(escapes) * 6 >= len(v) * 0.5:
        decoded = _UNI_RE.sub(lambda m: chr(int(m.group(0)[2:], 16)), v)
        return "unicode-escape", decoded.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if len(_PCT_RE.findall(v)) >= 3:
        from urllib.parse import unquote

        try:
            decoded = unquote(v, errors="strict")
        except UnicodeDecodeError:
            decoded = ""
        if decoded and decoded != v and _printable_ratio(decoded) >= 0.9:
            return "url", decoded
    compact = re.sub(r"\s+", "", v)
    if len(compact) >= MIN_ENCODED_CHARS and _HEX_RE.match(compact):
        body = compact.removeprefix("0x")
        if len(body) % 2 == 0:
            try:
                raw = binascii.unhexlify(body)
            except (binascii.Error, ValueError):
                raw = b""
            if raw and _meaningful(raw):
                return "hex", raw
            return None
    if len(compact) >= MIN_ENCODED_CHARS and len(compact) % 4 != 1 and _B64_RE.match(compact):
        decoded = _try_b64(compact, urlsafe=False)
        if decoded is not None:
            return "base64", decoded
    if len(compact) >= MIN_ENCODED_CHARS and ("-" in compact or "_" in compact) and _B64URL_RE.match(compact):
        decoded = _try_b64(compact, urlsafe=True)
        if decoded is not None:
            return "base64url", decoded
    return None


def _try_b64(text: str, *, urlsafe: bool) -> bytes | None:
    try:
        raw = (base64.urlsafe_b64decode if urlsafe else base64.b64decode)(
            (text + "=" * (-len(text) % 4)).encode("ascii"), validate=True
        )
    except (binascii.Error, ValueError):
        return None
    return raw if _meaningful(raw) else None


def _meaningful(raw: bytes) -> bool:
    """Decoded bytes worth showing: text, a compressed stream, or probable ciphertext.

    Hashes, random ids and words that merely happen to be valid base64/hex
    decode to short low-entropy noise and are rejected.
    """
    if len(raw) < 8:
        return False
    return _as_text(raw) is not None or _decompress(raw) is not None or _cipher_hint(raw) != ""


def decode_chain(value: Any) -> dict[str, Any]:
    """Peel encoding layers off one value. Returns the decode trace + verdict."""
    layers: list[dict[str, Any]] = []
    current: Any = value
    final_text: str | None = None
    cipher = ""
    for _ in range(MAX_LAYERS):
        if isinstance(current, bytes):
            dec = _decompress(current)
            if dec is not None:
                name, out = dec
                layers.append({"op": name, "size": len(out)})
                current = out
                continue
            text = _as_text(current)
            if text is not None:
                layers.append({"op": "utf8", "size": len(current)})
                current = text
                continue
            cipher = _cipher_hint(current)
            break
        step = _decode_text_layer(current)
        if step is None:
            final_text = current
            break
        name, out = step
        if isinstance(out, bytes):
            layers.append({"op": name, "size": len(out)})
        else:
            layers.append({"op": name, "chars": len(out)})
        current = out
    else:
        final_text = current if isinstance(current, str) else None

    if final_text is None and isinstance(current, str):
        final_text = current
    result: dict[str, Any] = {"layers": layers, "decoded": bool(layers)}
    if final_text is not None:
        result["text"] = final_text[:MAX_OUTPUT_CHARS]
        result["truncated"] = len(final_text) > MAX_OUTPUT_CHARS
        result["kind"] = "json" if _looks_json(final_text) else "text"
    elif isinstance(current, bytes):
        result["kind"] = "binary"
        result["size"] = len(current)
        result["entropy"] = round(_entropy(current), 2)
        if cipher:
            result["cipher_hint"] = cipher
            result["ciphertext_b64"] = base64.b64encode(current[:4096]).decode("ascii")
    return result


def _looks_json(text: str) -> bool:
    head = text.lstrip()[:1]
    return head in ("{", "[")


def _walk_json(node: Any, path: str, out: list[tuple[str, str]]) -> None:
    if len(out) >= MAX_CANDIDATES:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_json(value, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for idx, value in enumerate(node[:32]):
            _walk_json(value, f"{path}[{idx}]", out)
    elif isinstance(node, str) and len(node) >= MIN_ENCODED_CHARS:
        out.append((path, node))


def _candidate_strings(data: bytes, content_type: str) -> list[tuple[str, str]]:
    """``(location, value)`` pairs worth trying: whole body, JSON fields, form/query values."""
    candidates: list[tuple[str, str]] = []
    text = _as_text(data)
    ct = (content_type or "").lower()
    if text is not None:
        candidates.append(("<body>", text))
        if _looks_json(text):
            try:
                _walk_json(json.loads(text), "", candidates)
            except (json.JSONDecodeError, ValueError):
                pass
        elif "application/x-www-form-urlencoded" in ct or re.match(r"^[\w.\-~%+]+=[^&\s]*(&|$)", text):
            for key, value in parse_qsl(text, keep_blank_values=False):
                if len(value) >= MIN_ENCODED_CHARS:
                    candidates.append((f"form.{key}", value))
    else:
        # Whole-body binary: still worth a decompress/ciphertext verdict.
        candidates.append(("<body>", data))  # type: ignore[arg-type]
    return candidates[:MAX_CANDIDATES]


def auto_decode_body(data: bytes, content_type: str = "", url: str = "") -> dict[str, Any]:
    """Find decodable fragments in one request/response body.

    Returns ``{items: [{location, from, ...decode_chain}], texts: [...],
    ciphertext: [...]}``. ``texts`` feeds the query engine's ``dec:`` field;
    ``ciphertext`` is what a decryption rule should target.
    """
    items: list[dict[str, Any]] = []
    texts: list[str] = []
    ciphertext: list[dict[str, Any]] = []
    seen: set[str] = set()
    ct = (content_type or "").lower()
    if len(data) > MAX_INPUT_BYTES or ct.startswith(("image/", "video/", "audio/", "font/")):
        data = b""
    for location, value in (_candidate_strings(data, content_type) if data else []):
        chain = decode_chain(value)
        useful_text = chain.get("decoded") and chain.get("text") is not None
        if not useful_text and not chain.get("cipher_hint"):
            continue
        source = value if isinstance(value, str) else "<binary>"
        entry = {"location": location, "from": source[:120], **chain}
        items.append(entry)
        if chain.get("text") and chain.get("decoded"):
            text = chain["text"]
            if text not in seen:
                seen.add(text)
                texts.append(text)
        if chain.get("cipher_hint"):
            ciphertext.append({
                "location": location,
                "cipher_hint": chain["cipher_hint"],
                "size": chain.get("size", 0),
                "entropy": chain.get("entropy", 0.0),
                "ciphertext_b64": chain.get("ciphertext_b64", ""),
            })
    # Query-string parameters on the URL itself (tokens, signed payloads).
    if url:
        query = urlsplit(url).query
        for key, value in parse_qsl(query, keep_blank_values=False):
            if len(value) < MIN_ENCODED_CHARS:
                continue
            chain = decode_chain(value)
            useful_text = chain.get("decoded") and chain.get("text") is not None
            if not useful_text and not chain.get("cipher_hint"):
                continue
            items.append({"location": f"query.{key}", "from": value[:120], **chain})
            if useful_text and chain["text"] not in seen:
                seen.add(chain["text"])
                texts.append(chain["text"])
            if chain.get("cipher_hint"):
                ciphertext.append({
                    "location": f"query.{key}",
                    "cipher_hint": chain["cipher_hint"],
                    "size": chain.get("size", 0),
                    "entropy": chain.get("entropy", 0.0),
                    "ciphertext_b64": chain.get("ciphertext_b64", ""),
                })
    return {
        "items": items[:MAX_CANDIDATES],
        "texts": texts,
        "ciphertext": ciphertext,
    }
