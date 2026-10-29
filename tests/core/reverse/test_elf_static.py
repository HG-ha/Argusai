"""Tests for pure-Python ELF static triage."""

from __future__ import annotations

import struct
from pathlib import Path

from argusai.core.reverse.elf_static import analyze_elf_bytes, analyze_elf_path


def _build_mini_elf64(*, needed: list[str] | None = None) -> bytes:
    needed = needed or ["libc.so.6"]
    shstr = b"\x00.shstrtab\0.dynamic\0.dynstr\0"
    dynstr = b"\x00" + b"\x00".join(n.encode("ascii") for n in needed) + b"\x00"
    for name in needed:
        dynstr += name.encode("ascii") + b"\x00"

    # Rebuild dynstr cleanly
    dynstr = b"\x00" + b"".join(n.encode("ascii") + b"\x00" for n in needed)

    dynamic_entries = bytearray()
    str_cursor = 1
    name_offsets: dict[str, int] = {}
    for name in needed:
        name_offsets[name] = str_cursor
        str_cursor += len(name) + 1
    for name in needed:
        dynamic_entries.extend(struct.pack("<QQ", 1, name_offsets[name]))
    dynamic_entries.extend(struct.pack("<QQ", 0, 0))

    ehdr_size = 64
    phdr_size = 56
    shdr_size = 64
    shnum = 4
    shoff = ehdr_size
    dyn_off = shoff + shnum * shdr_size
    dynstr_off = dyn_off + len(dynamic_entries)
    shstr_off = dynstr_off + len(dynstr)

    data = bytearray(shstr_off + len(shstr))
    data[0:4] = b"\x7fELF"
    data[4] = 2
    data[5] = 1
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        2,
        0x3E,
        1,
        0x1000,
        0,
        shoff,
        0,
        ehdr_size,
        phdr_size,
        0,
        shdr_size,
        shnum,
        1,
    )

    def write_sh(index: int, name_off: int, sh_type: int, offset: int, size: int) -> None:
        pos = shoff + index * shdr_size
        struct.pack_into("<IIQQQQIIQQ", data, pos, name_off, sh_type, 0, 0, offset, size, 0, 0, 1, 0)

    write_sh(0, 0, 0, 0, 0)
    write_sh(1, 1, 3, shstr_off, len(shstr))
    write_sh(2, 11, 6, dyn_off, len(dynamic_entries))
    write_sh(3, 20, 3, dynstr_off, len(dynstr))
    data[dyn_off:dynstr_off] = dynamic_entries
    data[dynstr_off:shstr_off] = dynstr
    data[shstr_off:] = shstr
    return bytes(data)


def test_analyze_elf_bytes_reads_header_and_needed() -> None:
    blob = _build_mini_elf64(needed=["libc.so.6", "libm.so.6"])
    out = analyze_elf_bytes(blob, filename="mini.so")
    assert out["ok"] is True
    assert out["header"]["machine"] == "x86_64"
    assert out["header"]["class"] == "ELF64"
    assert "libc.so.6" in out["needed_libraries"]
    assert any(f["tag"] == "dynamic_libraries" for f in out["findings"])


def test_analyze_elf_path_rejects_missing_file(tmp_path: Path) -> None:
    out = analyze_elf_path(tmp_path / "missing.so")
    assert out["ok"] is False


def test_analyze_elf_bytes_rejects_non_elf() -> None:
    out = analyze_elf_bytes(b"not elf")
    assert out["ok"] is False
