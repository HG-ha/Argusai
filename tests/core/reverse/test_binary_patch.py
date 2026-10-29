"""Tests for the native binary patch engine (offset/VA, assertions, PE checksum)."""

from __future__ import annotations

import struct
from pathlib import Path

from argusai.core.reverse.binary_patch import (
    apply_binary_patches,
    pe_checksum,
    _parse_pe_layout,
)

IMAGE_BASE = 0x00400000
SECTION_VA = 0x1000
SECTION_RAW_PTR = 0x400
FILE_LEN = 0x600


def _build_min_pe() -> bytearray:
    """A minimal but structurally valid PE32 with one .text section."""
    data = bytearray(FILE_LEN)
    data[0:2] = b"MZ"
    e_lfanew = 0x80
    struct.pack_into("<I", data, 0x3C, e_lfanew)
    data[e_lfanew : e_lfanew + 4] = b"PE\x00\x00"
    coff = e_lfanew + 4  # 0x84
    struct.pack_into("<H", data, coff + 0, 0x014C)   # Machine i386
    struct.pack_into("<H", data, coff + 2, 1)         # NumberOfSections
    struct.pack_into("<H", data, coff + 16, 0xE0)     # SizeOfOptionalHeader
    opt = coff + 20  # 0x98
    struct.pack_into("<H", data, opt + 0, 0x010B)     # Magic PE32
    struct.pack_into("<I", data, opt + 28, IMAGE_BASE)
    struct.pack_into("<I", data, opt + 64, 0)         # CheckSum (recomputed)
    sec = opt + 0xE0  # 0x178
    data[sec : sec + 8] = b".text\x00\x00\x00"
    struct.pack_into("<I", data, sec + 8, 0x200)          # VirtualSize
    struct.pack_into("<I", data, sec + 12, SECTION_VA)    # VirtualAddress
    struct.pack_into("<I", data, sec + 16, 0x200)         # SizeOfRawData
    struct.pack_into("<I", data, sec + 20, SECTION_RAW_PTR)  # PointerToRawData
    # function-ish prologue at the start of the section's raw data
    data[SECTION_RAW_PTR : SECTION_RAW_PTR + 3] = b"\x55\x8b\xec"
    return data


def _write_pe(tmp_path: Path) -> Path:
    p = tmp_path / "sample.exe"
    p.write_bytes(bytes(_build_min_pe()))
    return p


def test_parse_pe_layout_reads_sections_and_checksum() -> None:
    layout = _parse_pe_layout(bytes(_build_min_pe()))
    assert layout.is_pe is True
    assert layout.pe32_plus is False
    assert layout.image_base == IMAGE_BASE
    assert layout.checksum_offset == 0x98 + 64
    assert layout.sections == [(SECTION_VA, 0x200, SECTION_RAW_PTR, 0x200)]


def test_parse_pe_layout_rejects_non_pe() -> None:
    assert _parse_pe_layout(b"not a pe file at all").is_pe is False


def test_pe_checksum_is_deterministic_and_nonzero() -> None:
    data = bytes(_build_min_pe())
    cs1 = pe_checksum(data, 0x98 + 64)
    cs2 = pe_checksum(data, 0x98 + 64)
    assert cs1 == cs2
    assert cs1 != 0


def test_patch_by_file_offset_writes_new_file(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    res = apply_binary_patches(
        sample,
        [{"offset": SECTION_RAW_PTR, "bytes": "9090", "expect": "558b"}],
        out,
    )
    assert res["ok"] is True
    assert res["verification"] == "passed"
    assert res["checksum_updated"] is True
    patched = out.read_bytes()
    assert patched[SECTION_RAW_PTR : SECTION_RAW_PTR + 2] == b"\x90\x90"
    # original is never modified in place
    assert sample.read_bytes()[SECTION_RAW_PTR : SECTION_RAW_PTR + 2] == b"\x55\x8b"


def test_patch_by_va_maps_to_offset(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    va = IMAGE_BASE + SECTION_VA  # -> rva 0x1000 -> raw 0x400
    res = apply_binary_patches(sample, [{"va": hex(va), "bytes": "cc"}], out)
    assert res["ok"] is True
    assert res["applied"][0]["file_offset"] == SECTION_RAW_PTR
    assert res["applied"][0]["addressing"] == "va"
    assert out.read_bytes()[SECTION_RAW_PTR] == 0xCC


def test_expect_assertion_failure_aborts_without_writing(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    res = apply_binary_patches(
        sample,
        [{"offset": SECTION_RAW_PTR, "bytes": "90", "expect": "ffff"}],
        out,
    )
    assert res["ok"] is False
    assert res["verification"] == "failed"
    assert "assertion failed" in res["error"]
    assert not out.exists()


def test_nop_fill(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    res = apply_binary_patches(sample, [{"offset": SECTION_RAW_PTR, "nop": True, "count": 3}], out)
    assert res["ok"] is True
    assert out.read_bytes()[SECTION_RAW_PTR : SECTION_RAW_PTR + 3] == b"\x90\x90\x90"


def test_out_of_range_offset_is_rejected(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    res = apply_binary_patches(sample, [{"offset": FILE_LEN + 10, "bytes": "90"}], out)
    assert res["ok"] is False
    assert "out of range" in res["error"]
    assert not out.exists()


def test_multiple_patches_all_or_nothing(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    # second patch has a bad assertion -> the whole batch must abort
    res = apply_binary_patches(
        sample,
        [
            {"offset": SECTION_RAW_PTR, "bytes": "90"},
            {"offset": SECTION_RAW_PTR + 1, "bytes": "90", "expect": "00"},
        ],
        out,
    )
    assert res["ok"] is False
    assert not out.exists()


def test_patch_requires_target_and_replacement(tmp_path: Path) -> None:
    sample = _write_pe(tmp_path)
    out = tmp_path / "patched.exe"
    assert apply_binary_patches(sample, [{"bytes": "90"}], out)["ok"] is False
    assert apply_binary_patches(sample, [{"offset": SECTION_RAW_PTR}], out)["ok"] is False
