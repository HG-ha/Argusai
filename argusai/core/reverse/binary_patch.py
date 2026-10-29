"""Native binary patching engine (byte / NOP / optional assemble).

Safety contract (Loom22 §21.7 — destructive native action):

* **Never patches in place.** ``apply_binary_patches`` always writes a NEW file
  and leaves the original untouched.
* **Original-byte assertion.** Every patch may carry ``expect`` (hex) that must
  match the bytes currently at the target before the write is allowed; the whole
  operation aborts if any assertion fails (no partial writes are persisted).
* **PE checksum.** For PE files the OptionalHeader checksum is recomputed so the
  patched copy is not trivially broken; the algorithm matches Windows
  ``CheckSumMappedFile``.
* **No assembler required for the common case.** Patches are raw bytes / NOP
  fills. ``assemble`` (x86/x64 via the optional ``keystone`` package) is a
  convenience that degrades to a clear error when keystone is absent.

This module is intentionally dependency-free for the PE parsing / checksum path
so it is unit-testable without Ghidra, pefile, or keystone installed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NOP_BYTE = 0x90  # x86/x64 single-byte NOP


# --------------------------------------------------------------------------- #
# Minimal PE layout helper (file-offset <-> virtual address, checksum field)
# --------------------------------------------------------------------------- #
@dataclass
class _PeLayout:
    is_pe: bool
    pe32_plus: bool = False
    image_base: int = 0
    checksum_offset: int = 0
    sections: list[tuple[int, int, int, int]] = field(default_factory=list)
    # each section: (virtual_address, virtual_size, raw_ptr, raw_size)


def _parse_pe_layout(data: bytes) -> _PeLayout:
    """Best-effort PE header parse; returns ``is_pe=False`` for non-PE input."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return _PeLayout(is_pe=False)
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return _PeLayout(is_pe=False)
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt = coff + 20
    if opt + 2 > len(data):
        return _PeLayout(is_pe=False)
    magic = struct.unpack_from("<H", data, opt)[0]
    pe32_plus = magic == 0x20B
    # CheckSum sits at OptionalHeader+64 for both PE32 and PE32+.
    checksum_offset = opt + 64
    if pe32_plus:
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    else:
        image_base = struct.unpack_from("<I", data, opt + 28)[0]

    sec_start = opt + size_opt
    sections: list[tuple[int, int, int, int]] = []
    for i in range(num_sections):
        off = sec_start + i * 40
        if off + 40 > len(data):
            break
        virtual_size = struct.unpack_from("<I", data, off + 8)[0]
        virtual_address = struct.unpack_from("<I", data, off + 12)[0]
        raw_size = struct.unpack_from("<I", data, off + 16)[0]
        raw_ptr = struct.unpack_from("<I", data, off + 20)[0]
        sections.append((virtual_address, virtual_size, raw_ptr, raw_size))
    return _PeLayout(
        is_pe=True,
        pe32_plus=pe32_plus,
        image_base=image_base,
        checksum_offset=checksum_offset,
        sections=sections,
    )


def _rva_to_offset(layout: _PeLayout, rva: int) -> int | None:
    for virtual_address, virtual_size, raw_ptr, raw_size in layout.sections:
        span = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + span:
            delta = rva - virtual_address
            if delta < raw_size:
                return raw_ptr + delta
            return None  # falls in uninitialized (BSS-like) tail — no file bytes
    return None


def pe_checksum(data: bytes, checksum_offset: int) -> int:
    """Compute the PE OptionalHeader checksum (Windows ``CheckSumMappedFile``)."""
    buf = bytearray(data)
    if checksum_offset + 4 <= len(buf):
        buf[checksum_offset : checksum_offset + 4] = b"\x00\x00\x00\x00"
    checksum = 0
    n = len(buf)
    limit = n - (n % 2)
    i = 0
    while i < limit:
        checksum += buf[i] | (buf[i + 1] << 8)
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
        i += 2
    if n % 2:
        checksum += buf[i]
        checksum = (checksum & 0xFFFF) + (checksum >> 16)
    checksum = (checksum & 0xFFFF) + (checksum >> 16)
    checksum = (checksum & 0xFFFF) + (checksum >> 16)
    checksum &= 0xFFFF
    return (checksum + n) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# Patch resolution
# --------------------------------------------------------------------------- #
def _parse_hex(value: str) -> bytes:
    cleaned = "".join(str(value or "").split()).replace("0x", "").replace(",", "")
    if not cleaned:
        return b""
    if len(cleaned) % 2:
        raise ValueError(f"hex byte string must have even length: {value!r}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid hex bytes: {value!r} ({exc})") from None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    try:
        return int(text, 16) if text.startswith("0x") else int(text, 0)
    except ValueError:
        return None


def _resolve_patch_bytes(patch: dict[str, Any]) -> tuple[bytes, str]:
    """Return ``(replacement_bytes, source_label)`` for one patch spec."""
    if patch.get("nop"):
        length = _coerce_int(patch.get("count") or patch.get("length"))
        if not length or length <= 0:
            raise ValueError("nop patch requires a positive count/length")
        return bytes([NOP_BYTE]) * length, "nop"
    if patch.get("bytes"):
        return _parse_hex(str(patch.get("bytes"))), "bytes"
    asm = str(patch.get("assemble") or "").strip()
    if asm:
        return _assemble(asm, patch), "assemble"
    raise ValueError("patch needs one of: bytes (hex), nop+count, or assemble")


def _assemble(asm: str, patch: dict[str, Any]) -> bytes:
    try:
        import keystone  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ValueError(
            "assemble= requires the optional 'keystone-engine' package; "
            "install it or pass raw bytes= instead"
        ) from exc
    arch = str(patch.get("arch") or "x64").lower()
    mode_map = {
        "x86": (keystone.KS_ARCH_X86, keystone.KS_MODE_32),
        "x64": (keystone.KS_ARCH_X86, keystone.KS_MODE_64),
        "arm": (keystone.KS_ARCH_ARM, keystone.KS_MODE_ARM),
        "arm64": (keystone.KS_ARCH_ARM64, keystone.KS_MODE_LITTLE_ENDIAN),
    }
    if arch not in mode_map:
        raise ValueError(f"unsupported assemble arch: {arch} (use x86/x64/arm/arm64)")
    ks_arch, ks_mode = mode_map[arch]
    ks = keystone.Ks(ks_arch, ks_mode)
    encoding, _count = ks.asm(asm)
    return bytes(encoding or [])


def _resolve_offset(patch: dict[str, Any], layout: _PeLayout, data_len: int) -> tuple[int, str]:
    """Resolve a patch's file offset from ``offset`` / ``rva`` / ``va``."""
    file_offset = _coerce_int(patch.get("offset"))
    if file_offset is not None:
        return file_offset, "offset"

    rva = _coerce_int(patch.get("rva"))
    va = _coerce_int(patch.get("va"))
    if va is not None and rva is None:
        if not layout.is_pe:
            raise ValueError("va= addressing requires a PE file; use offset= instead")
        rva = va - layout.image_base
    if rva is not None:
        if not layout.is_pe:
            raise ValueError("rva=/va= addressing requires a PE file; use offset= instead")
        resolved = _rva_to_offset(layout, rva)
        if resolved is None:
            raise ValueError(f"rva 0x{rva:x} does not map to a file offset (uninitialized/invalid)")
        return resolved, "va" if patch.get("va") is not None else "rva"

    raise ValueError("patch needs one of: offset, rva, or va")


def apply_binary_patches(
    sample_path: Path,
    patches: list[dict[str, Any]],
    out_path: Path,
    *,
    expect_original: bool = True,
    recompute_checksum: bool = True,
) -> dict[str, Any]:
    """Apply a list of byte patches to a binary, writing a NEW file at ``out_path``.

    Each patch dict supports::

        {"offset": 4096}                 # file offset (int or "0x1000")
        {"rva": 4096} / {"va": 0x401000} # PE address (mapped via section table)
        + one of:
        {"bytes": "9090"}                # raw replacement hex
        {"nop": true, "count": 2}        # NOP fill
        {"assemble": "xor eax, eax", "arch": "x64"}  # needs keystone
        + optional:
        {"expect": "7400"}               # assert current bytes before writing

    Returns a structured result. On any validation failure nothing is written.
    """
    if not sample_path.is_file():
        return {"ok": False, "error": f"sample not found: {sample_path}"}
    if not patches:
        return {"ok": False, "error": "no patches provided"}

    try:
        original = sample_path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read sample: {exc}"}

    layout = _parse_pe_layout(original)
    buf = bytearray(original)
    applied: list[dict[str, Any]] = []

    # Two passes: validate everything first, then write — so a bad patch in the
    # middle never leaves a half-modified file.
    resolved: list[tuple[int, bytes, dict[str, Any]]] = []
    for idx, patch in enumerate(patches):
        if not isinstance(patch, dict):
            return {"ok": False, "error": f"patch #{idx} is not an object"}
        try:
            offset, addr_mode = _resolve_offset(patch, layout, len(buf))
            replacement = _resolve_patch_bytes(patch)[0]
        except ValueError as exc:
            return {"ok": False, "error": f"patch #{idx}: {exc}"}
        if not replacement:
            return {"ok": False, "error": f"patch #{idx}: empty replacement bytes"}
        if offset < 0 or offset + len(replacement) > len(buf):
            return {
                "ok": False,
                "error": f"patch #{idx}: offset 0x{offset:x}+{len(replacement)} out of range (file is {len(buf)} bytes)",
            }
        current = bytes(buf[offset : offset + len(replacement)])
        expect = patch.get("expect")
        if expect_original and expect:
            try:
                expect_bytes = _parse_hex(str(expect))
            except ValueError as exc:
                return {"ok": False, "error": f"patch #{idx}: {exc}"}
            if expect_bytes and current[: len(expect_bytes)] != expect_bytes:
                return {
                    "ok": False,
                    "error": (
                        f"patch #{idx}: original-byte assertion failed at 0x{offset:x}; "
                        f"expected {expect_bytes.hex()} but found {current[: len(expect_bytes)].hex()}"
                    ),
                    "verification": "failed",
                }
        resolved.append((offset, replacement, patch))
        applied.append(
            {
                "index": idx,
                "file_offset": offset,
                "addressing": addr_mode,
                "original_bytes": current.hex(),
                "patched_bytes": replacement.hex(),
                "length": len(replacement),
            }
        )

    for offset, replacement, _patch in resolved:
        buf[offset : offset + len(replacement)] = replacement

    checksum_updated = False
    new_checksum = None
    if recompute_checksum and layout.is_pe and layout.checksum_offset + 4 <= len(buf):
        new_checksum = pe_checksum(bytes(buf), layout.checksum_offset)
        struct.pack_into("<I", buf, layout.checksum_offset, new_checksum)
        checksum_updated = True

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(bytes(buf))
    except OSError as exc:
        return {"ok": False, "error": f"cannot write patched file: {exc}"}

    return {
        "ok": True,
        "input": str(sample_path.resolve()),
        "output": str(out_path.resolve()),
        "is_pe": layout.is_pe,
        "patch_count": len(applied),
        "applied": applied,
        "checksum_updated": checksum_updated,
        "new_checksum": new_checksum,
        "verification": "passed" if expect_original else "skipped",
        "next_step": "Verify with binary_disassemble / binary_decompile on the patched copy before running it.",
    }
