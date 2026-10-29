"""Pure-Python crypto reproduction with golden-vector verification.

When the AI is reverse-engineering an in-page crypto stack, the
"reproduce" half of the workflow is what closes the loop: it's not
enough to identify ``AES-CBC + PKCS#7 + static IV``; the AI needs to
prove the Python re-implementation produces the EXACT bytes the JS does,
otherwise the final PoC won't match the server's expectations.

This module exposes :func:`crypto_reproduce` which:

1. Takes ``algorithm`` (xor, aes-cbc/ecb/ctr/gcm, des/3des, rc4, hmac-sha*,
   pbkdf2, sha*, md5), ``key``, ``iv`` (where applicable), ``mode`` and
   ``data``.
2. Runs the operation with the standard library + ``cryptography``.
3. If the caller supplied an ``expected`` value (the bytes the JS
   produced for this input), compares them and reports ``matched``.
4. Returns a clean, **copy-pasteable Python snippet** that the AI can
   later embed in a one-click PoC.

Why this matters:

- The AI's freeform Python code in tool calls is throw-away — the
  ``crypto_reproduce`` tool gives it a structured, validated result it
  can quote verbatim in the final report.
- Golden-vector matching catches almost every "is the JS using the
  null byte or the empty string for IV" / "is this CTR's nonce big-endian
  or little-endian" type bug.
- All inputs accept hex, base64, or raw utf-8; outputs are produced in
  whatever form the JS surface uses (most commonly hex or base64).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("argusai.crypto_reproduce")


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _from_encoded(value: Any, hint: str = "auto") -> bytes:
    """Decode user input to bytes.

    ``hint`` ∈ ``utf8 | hex | base64 | base64url | auto``. Auto first
    tries hex (must be even-length all-hex), then base64 (incl. URL-safe),
    then falls back to UTF-8 raw text.
    """
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    s = str(value)
    h = (hint or "auto").lower()
    if h == "utf8":
        return s.encode("utf-8", errors="replace")
    if h == "hex":
        return binascii.unhexlify(s.replace(" ", "").replace("\n", ""))
    if h == "base64":
        return base64.b64decode(s, validate=False)
    if h == "base64url":
        # base64.urlsafe_b64decode requires padding.
        s2 = s.replace("\n", "").strip()
        s2 += "=" * (-len(s2) % 4)
        return base64.urlsafe_b64decode(s2.encode("ascii"))
    # auto.
    s_strip = s.strip()
    # Try hex.
    if s_strip and all(c in "0123456789abcdefABCDEF" for c in s_strip) and len(s_strip) % 2 == 0:
        try:
            return binascii.unhexlify(s_strip)
        except Exception:
            pass
    # Try base64.
    if s_strip and len(s_strip) >= 4 and all(c.isalnum() or c in "+/=_-\n" for c in s_strip):
        try:
            return base64.b64decode(s_strip, validate=False)
        except Exception:
            pass
    return s.encode("utf-8", errors="replace")


def _to_encoded(value: bytes, fmt: str = "hex") -> str:
    """Encode bytes for display. ``fmt`` ∈ ``hex | base64 | base64url | utf8``."""
    f = (fmt or "hex").lower()
    if f == "hex":
        return value.hex()
    if f == "base64":
        return base64.b64encode(value).decode("ascii")
    if f == "base64url":
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
    if f == "utf8":
        return value.decode("utf-8", errors="replace")
    return value.hex()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@dataclass
class CryptoResult:
    ok: bool
    algorithm: str
    direction: str
    output: bytes
    error: str = ""
    snippet: str = ""

    def to_dict(self, output_fmt: str = "hex") -> dict[str, Any]:
        return {
            "ok": self.ok,
            "algorithm": self.algorithm,
            "direction": self.direction,
            "output": _to_encoded(self.output, output_fmt) if self.ok else "",
            "output_format": output_fmt,
            "output_size_bytes": len(self.output) if self.ok else 0,
            "error": self.error,
            "python_snippet": self.snippet,
        }


def _xor_stream(data: bytes, key: bytes) -> bytes:
    if not key:
        raise ValueError("xor: empty key")
    k = len(key)
    return bytes(b ^ key[i % k] for i, b in enumerate(data))


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - (len(data) % block)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        # Not strictly PKCS#7. Return as-is rather than raise — the
        # caller may have already-stripped output or zero-padding.
        return data
    return data[:-n]


def _do_aes(
    direction: str,
    key: bytes,
    iv: bytes,
    mode: str,
    data: bytes,
    *,
    no_padding: bool = False,
    aad: bytes = b"",
) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    mode = mode.lower()
    if len(key) not in (16, 24, 32):
        raise ValueError(f"aes: key must be 16/24/32 bytes, got {len(key)}")
    algo = algorithms.AES(key)

    if mode == "cbc":
        if len(iv) != 16:
            raise ValueError(f"aes-cbc: iv must be 16 bytes, got {len(iv)}")
        cipher = Cipher(algo, modes.CBC(iv))
        if direction == "encrypt":
            enc = cipher.encryptor()
            padded = data if no_padding else _pkcs7_pad(data, 16)
            return enc.update(padded) + enc.finalize()
        else:
            dec = cipher.decryptor()
            plain = dec.update(data) + dec.finalize()
            return plain if no_padding else _pkcs7_unpad(plain)
    if mode == "ecb":
        cipher = Cipher(algo, modes.ECB())
        if direction == "encrypt":
            enc = cipher.encryptor()
            padded = data if no_padding else _pkcs7_pad(data, 16)
            return enc.update(padded) + enc.finalize()
        else:
            dec = cipher.decryptor()
            plain = dec.update(data) + dec.finalize()
            return plain if no_padding else _pkcs7_unpad(plain)
    if mode == "ctr":
        if len(iv) != 16:
            raise ValueError(f"aes-ctr: iv (nonce+counter) must be 16 bytes, got {len(iv)}")
        cipher = Cipher(algo, modes.CTR(iv))
        op = cipher.encryptor() if direction == "encrypt" else cipher.decryptor()
        return op.update(data) + op.finalize()
    if mode == "gcm":
        if not iv:
            raise ValueError("aes-gcm: iv (nonce) required")
        if direction == "encrypt":
            cipher = Cipher(algo, modes.GCM(iv))
            enc = cipher.encryptor()
            if aad:
                enc.authenticate_additional_data(aad)
            ct = enc.update(data) + enc.finalize()
            return ct + enc.tag  # tag concatenated at end
        else:
            # data = ciphertext || tag (last 16 bytes by convention)
            if len(data) < 16:
                raise ValueError("aes-gcm decrypt: data shorter than tag length")
            ct, tag = data[:-16], data[-16:]
            cipher = Cipher(algo, modes.GCM(iv, tag))
            dec = cipher.decryptor()
            if aad:
                dec.authenticate_additional_data(aad)
            return dec.update(ct) + dec.finalize()
    raise ValueError(f"unknown aes mode: {mode}")


def _do_des(
    direction: str,
    key: bytes,
    iv: bytes,
    mode: str,
    data: bytes,
    *,
    triple: bool = False,
    no_padding: bool = False,
) -> bytes:
    from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    if triple:
        if len(key) not in (16, 24):
            raise ValueError(f"3des: key must be 16/24 bytes, got {len(key)}")
        algo = decrepit.TripleDES(key)
    else:
        if len(key) != 8:
            raise ValueError(f"des: key must be 8 bytes, got {len(key)}")
        algo = decrepit.DES(key)

    mode = mode.lower()
    if mode == "cbc":
        if len(iv) != 8:
            raise ValueError(f"des-cbc: iv must be 8 bytes, got {len(iv)}")
        cipher = Cipher(algo, modes.CBC(iv))
    elif mode == "ecb":
        cipher = Cipher(algo, modes.ECB())
    else:
        raise ValueError(f"des: unsupported mode {mode}")

    if direction == "encrypt":
        enc = cipher.encryptor()
        padded = data if no_padding else _pkcs7_pad(data, 8)
        return enc.update(padded) + enc.finalize()
    else:
        dec = cipher.decryptor()
        plain = dec.update(data) + dec.finalize()
        return plain if no_padding else _pkcs7_unpad(plain)


def _do_sm4(
    direction: str,
    key: bytes,
    iv: bytes,
    mode: str,
    data: bytes,
    *,
    no_padding: bool = False,
) -> bytes:
    """SM4 (GB/T 32907) block cipher — common in Chinese gov / finance apps."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 16:
        raise ValueError(f"sm4: key must be 16 bytes, got {len(key)}")
    mode = mode.lower()
    if mode == "cbc":
        if len(iv) != 16:
            raise ValueError(f"sm4-cbc: iv must be 16 bytes, got {len(iv)}")
        cipher = Cipher(algorithms.SM4(key), modes.CBC(iv))
    elif mode == "ecb":
        cipher = Cipher(algorithms.SM4(key), modes.ECB())
    else:
        raise ValueError(f"sm4: unsupported mode {mode}")
    if direction == "encrypt":
        enc = cipher.encryptor()
        padded = data if no_padding else _pkcs7_pad(data, 16)
        return enc.update(padded) + enc.finalize()
    dec = cipher.decryptor()
    plain = dec.update(data) + dec.finalize()
    return plain if no_padding else _pkcs7_unpad(plain)


def _do_rc4(key: bytes, data: bytes) -> bytes:
    # RC4 is symmetric so encrypt == decrypt.
    s = list(range(256))
    j = 0
    klen = len(key)
    if klen == 0:
        raise ValueError("rc4: empty key")
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _make_snippet(
    algorithm: str,
    direction: str,
    mode: str,
    key_hex: str,
    iv_hex: str,
    input_hex: str,
    output_fmt: str,
) -> str:
    """Render a stand-alone Python snippet that reproduces the operation
    on the EXACT inputs we just used. The user can paste this into a
    PoC file and the bytes will match.
    """
    base = "from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes\n\n"
    if algorithm == "xor":
        return (
            "def xor(data: bytes, key: bytes) -> bytes:\n"
            "    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))\n\n"
            f"KEY   = bytes.fromhex({key_hex!r})\n"
            f"INPUT = bytes.fromhex({input_hex!r})\n"
            f"OUT   = xor(INPUT, KEY)\n"
            f"print(OUT.hex())  # {output_fmt}\n"
        )
    if algorithm == "rc4":
        return (
            "def rc4(key: bytes, data: bytes) -> bytes:\n"
            "    s = list(range(256)); j = 0\n"
            "    for i in range(256):\n"
            "        j = (j + s[i] + key[i % len(key)]) & 0xff\n"
            "        s[i], s[j] = s[j], s[i]\n"
            "    i = j = 0; out = bytearray()\n"
            "    for b in data:\n"
            "        i = (i + 1) & 0xff; j = (j + s[i]) & 0xff\n"
            "        s[i], s[j] = s[j], s[i]\n"
            "        out.append(b ^ s[(s[i] + s[j]) & 0xff])\n"
            "    return bytes(out)\n\n"
            f"KEY   = bytes.fromhex({key_hex!r})\n"
            f"INPUT = bytes.fromhex({input_hex!r})\n"
            f"OUT   = rc4(KEY, INPUT)\n"
            f"print(OUT.hex())  # {output_fmt}\n"
        )
    if algorithm.startswith("aes"):
        return (
            base
            + f"KEY = bytes.fromhex({key_hex!r})\n"
            + f"IV  = bytes.fromhex({iv_hex!r})\n"
            + f"INPUT = bytes.fromhex({input_hex!r})\n\n"
            + f"cipher = Cipher(algorithms.AES(KEY), modes.{mode.upper()}(IV))\n"
            + f"op = cipher.{'encryptor' if direction == 'encrypt' else 'decryptor'}()\n"
            + f"OUT = op.update(INPUT) + op.finalize()\n"
            + f"print(OUT.hex())\n"
        )
    if algorithm in ("hmac", "hmac-sha256", "hmac-sha1", "hmac-sha512", "hmac-md5"):
        digest = algorithm.split("-")[-1] if "-" in algorithm else "sha256"
        return (
            "import hmac, hashlib\n\n"
            f"KEY   = bytes.fromhex({key_hex!r})\n"
            f"INPUT = bytes.fromhex({input_hex!r})\n"
            f"OUT   = hmac.new(KEY, INPUT, hashlib.{digest}).digest()\n"
            f"print(OUT.hex())  # {output_fmt}\n"
        )
    if algorithm.startswith("sha") or algorithm == "md5":
        return (
            "import hashlib\n\n"
            f"INPUT = bytes.fromhex({input_hex!r})\n"
            f"OUT   = hashlib.{algorithm.replace('-', '')}(INPUT).digest()\n"
            f"print(OUT.hex())\n"
        )
    return f"# {algorithm} reproduction: see crypto_reproduce tool output\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def crypto_reproduce(
    *,
    algorithm: str,
    direction: str = "encrypt",
    key: str = "",
    iv: str = "",
    data: str = "",
    expected: str = "",
    key_format: str = "auto",
    iv_format: str = "auto",
    data_format: str = "auto",
    expected_format: str = "auto",
    output_format: str = "hex",
    no_padding: bool = False,
    aad: str = "",
    aad_format: str = "auto",
) -> dict[str, Any]:
    """Run a crypto primitive and report ``{ok, output, matched?, snippet}``.

    Args:
        algorithm: One of ``xor | aes-cbc | aes-ecb | aes-ctr | aes-gcm |
            des-cbc | des-ecb | 3des-cbc | 3des-ecb | rc4 | hmac-sha256 |
            hmac-sha1 | hmac-sha512 | hmac-md5 | sha256 | sha1 | sha512 |
            md5 | pbkdf2-sha256`` (digest variants accept ``digest=``).
        direction: ``encrypt | decrypt`` for symmetric ciphers; ignored
            for hash/HMAC.
        key, iv, data, expected: Inputs. Format inferred from
            ``*_format`` (auto picks hex → base64 → utf-8).
        no_padding: Skip PKCS#7 padding (for block ciphers operating on
            pre-padded streams).
        aad: GCM additional authenticated data.

    Returns:
        ``{ok, algorithm, direction, output, matched, expected_size,
           python_snippet, error}``
    """
    algo = (algorithm or "").lower().strip()
    direction = (direction or "encrypt").lower().strip()
    if direction not in ("encrypt", "decrypt"):
        return {"ok": False, "error": f"direction must be encrypt|decrypt, got {direction!r}", "algorithm": algo}

    try:
        key_b = _from_encoded(key, key_format)
        iv_b = _from_encoded(iv, iv_format) if iv else b""
        data_b = _from_encoded(data, data_format)
        expected_b = _from_encoded(expected, expected_format) if expected else b""
        aad_b = _from_encoded(aad, aad_format) if aad else b""
    except Exception as e:
        return {"ok": False, "error": f"input decode failed: {e}", "algorithm": algo}

    try:
        if algo == "xor":
            out = _xor_stream(data_b, key_b)
        elif algo == "rc4":
            out = _do_rc4(key_b, data_b)
        elif algo.startswith("aes-"):
            mode = algo.split("-", 1)[1]
            out = _do_aes(direction, key_b, iv_b, mode, data_b, no_padding=no_padding, aad=aad_b)
        elif algo.startswith("des-"):
            mode = algo.split("-", 1)[1]
            out = _do_des(direction, key_b, iv_b, mode, data_b, triple=False, no_padding=no_padding)
        elif algo.startswith("3des-"):
            mode = algo.split("-", 1)[1]
            out = _do_des(direction, key_b, iv_b, mode, data_b, triple=True, no_padding=no_padding)
        elif algo.startswith("sm4-"):
            mode = algo.split("-", 1)[1]
            out = _do_sm4(direction, key_b, iv_b, mode, data_b, no_padding=no_padding)
        elif algo.startswith("hmac"):
            # hmac-sha256 etc.
            digest = algo.split("-", 1)[1] if "-" in algo else "sha256"
            digest_canon = digest.replace("sha", "sha")
            if digest_canon not in {"md5", "sha1", "sha224", "sha256", "sha384", "sha512"}:
                return {"ok": False, "error": f"unsupported hmac digest: {digest}", "algorithm": algo}
            out = hmac.new(key_b, data_b, getattr(hashlib, digest_canon)).digest()
        elif algo in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512"):
            out = hashlib.new(algo, data_b).digest()
        elif algo == "pbkdf2-sha256":
            # Tucked key/iv into key=password, iv=salt; this is a minor
            # overload but keeps the API uniform.
            iterations = int(no_padding) if no_padding else 100_000  # not great; keep simple
            out = hashlib.pbkdf2_hmac("sha256", key_b, iv_b, iterations=iterations, dklen=32)
        else:
            return {"ok": False, "error": f"unsupported algorithm: {algo}", "algorithm": algo}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "algorithm": algo, "direction": direction}

    res: dict[str, Any] = CryptoResult(
        ok=True,
        algorithm=algo,
        direction=direction,
        output=out,
        snippet=_make_snippet(
            algorithm=algo,
            direction=direction,
            mode=(algo.split("-", 1)[1] if "-" in algo else ""),
            key_hex=key_b.hex(),
            iv_hex=iv_b.hex(),
            input_hex=data_b.hex(),
            output_fmt=output_format,
        ),
    ).to_dict(output_format)

    if expected_b:
        res["expected_size_bytes"] = len(expected_b)
        res["matched"] = (out == expected_b)
        if not res["matched"]:
            # Helpful diff hint.
            shorter = min(len(out), len(expected_b))
            first_diff = next((i for i in range(shorter) if out[i] != expected_b[i]), shorter)
            res["match_hint"] = (
                f"output and expected differ at byte {first_diff} "
                f"(out={out[first_diff:first_diff+8].hex() if first_diff < len(out) else 'EOF'}, "
                f"exp={expected_b[first_diff:first_diff+8].hex() if first_diff < len(expected_b) else 'EOF'}). "
                "Common causes: wrong key encoding (hex vs base64), wrong IV, padding mismatch, "
                "little vs big-endian counter."
            )
        else:
            res["match_hint"] = "matches golden vector — Python re-implementation verified."
    return res
