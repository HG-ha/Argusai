"""Pure-Python ELF static triage."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from argusai.core.reverse.native_common import extract_ascii_strings, sha256_bytes, sha256_file

_ELFMAG = b"\x7fELF"
_DT_NEEDED = 1
_DT_NULL = 0

_ELF_MACHINES = {
    0x03: "i386",
    0x3E: "x86_64",
    0x28: "arm",
    0xB7: "aarch64",
    0x14: "ppc",
    0x08: "mips",
    0xF3: "riscv",
}

_ELF_TYPES = {
    0x0000: "none",
    0x0001: "relocatable",
    0x0002: "executable",
    0x0003: "shared",
    0x0004: "core",
}


def elf_machine_name(machine: int) -> str:
    return _ELF_MACHINES.get(machine, f"unknown(0x{machine:x})")


def analyze_elf_bytes(data: bytes, *, filename: str = "sample.elf") -> dict[str, Any]:
    digest = sha256_bytes(data)
    result = _parse_elf(data)
    result["sha256"] = digest
    result["filename"] = filename
    result["size"] = len(data)
    if result.get("ok"):
        result["findings"] = _triage_findings(result)
        result["risk_tags"] = sorted({f["tag"] for f in result["findings"] if f.get("tag")})
    return result


def analyze_elf_path(path: Path, *, max_string_scan: int = 512_000) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if len(data) > 100 * 1024 * 1024:
        return {"ok": False, "error": "ELF file too large (>100MB)"}
    out = analyze_elf_bytes(data, filename=path.name)
    out["path"] = str(path)
    out["sha256"] = sha256_file(path)
    return out


def _parse_elf(data: bytes) -> dict[str, Any]:
    if len(data) < 52 or data[:4] != _ELFMAG:
        return {"ok": False, "error": "not an ELF file (missing \\x7fELF magic)"}

    ei_class = data[4]
    ei_data = data[5]
    if ei_class not in (1, 2):
        return {"ok": False, "error": f"unsupported ELF class: {ei_class}"}
    if ei_data not in (1, 2):
        return {"ok": False, "error": f"unsupported ELF endianness: {ei_data}"}

    is_64 = ei_class == 2
    endian = "<" if ei_data == 1 else ">"
    hdr_fmt = f"{endian}HHIQQQIHHHHHH" if is_64 else f"{endian}HHIIIIIIHHHHH"
    try:
        (
            e_type,
            e_machine,
            e_version,
            e_entry,
            e_phoff,
            e_shoff,
            e_flags,
            e_ehsize,
            e_phentsize,
            e_phnum,
            e_shentsize,
            e_shnum,
            e_shstrndx,
        ) = struct.unpack_from(hdr_fmt, data, 16)
    except struct.error as exc:
        return {"ok": False, "error": f"truncated ELF header: {exc}"}

    sections = _parse_sections(
        data,
        e_shoff,
        e_shnum,
        e_shentsize,
        e_shstrndx,
        is_64=is_64,
        endian=endian,
    )
    segments = _parse_segments(
        data,
        e_phoff,
        e_phnum,
        e_phentsize,
        is_64=is_64,
        endian=endian,
    )
    needed = _parse_needed_libraries(data, sections, is_64=is_64, endian=endian)
    strings = extract_ascii_strings(data[:max(4096, min(len(data), 512_000))], limit=200)

    return {
        "ok": True,
        "format": "elf",
        "confidence": "high" if sections else "probable",
        "tools_used": ["elf_parser"],
        "header": {
            "class": "ELF64" if is_64 else "ELF32",
            "endian": "little" if ei_data == 1 else "big",
            "type": _ELF_TYPES.get(e_type, f"unknown(0x{e_type:x})"),
            "machine": elf_machine_name(e_machine),
            "entry": e_entry,
            "sections": e_shnum,
            "segments": e_phnum,
        },
        "sections": sections[:64],
        "segments": segments[:32],
        "needed_libraries": needed[:64],
        "needed_count": len(needed),
        "strings_preview": strings[:80],
    }


def _parse_sections(
    data: bytes,
    shoff: int,
    shnum: int,
    shentsize: int,
    shstrndx: int,
    *,
    is_64: bool,
    endian: str,
) -> list[dict[str, Any]]:
    if shoff <= 0 or shnum <= 0 or shentsize <= 0:
        return []
    sh_fmt = f"{endian}IIQQQQIIQQ" if is_64 else f"{endian}IIIIIIIIII"
    sh_size = 64 if is_64 else 40
    if shentsize < sh_size:
        return []

    headers: list[dict[str, Any]] = []
    for idx in range(min(shnum, 128)):
        pos = shoff + idx * shentsize
        if pos + sh_size > len(data):
            break
        fields = struct.unpack_from(sh_fmt, data, pos)
        headers.append({
            "index": idx,
            "name_off": fields[0],
            "type": fields[1],
            "offset": fields[4],
            "size": fields[5],
        })

    names: dict[int, str] = {}
    if 0 <= shstrndx < len(headers):
        str_sec = headers[shstrndx]
        str_off = int(str_sec["offset"])
        str_size = int(str_sec["size"])
        blob = data[str_off:str_off + min(str_size, 65536)]
        for item in headers:
            name_off = int(item["name_off"])
            if name_off >= len(blob):
                names[item["index"]] = ""
                continue
            end = blob.find(b"\x00", name_off)
            names[item["index"]] = blob[name_off:end if end >= 0 else len(blob)].decode("ascii", errors="ignore")

    out: list[dict[str, Any]] = []
    for item in headers:
        out.append({
            "index": item["index"],
            "name": names.get(item["index"], ""),
            "type": item["type"],
            "offset": item["offset"],
            "size": item["size"],
        })
    return out


def _parse_segments(
    data: bytes,
    phoff: int,
    phnum: int,
    phentsize: int,
    *,
    is_64: bool,
    endian: str,
) -> list[dict[str, Any]]:
    if phoff <= 0 or phnum <= 0 or phentsize <= 0:
        return []
    ph_fmt = f"{endian}IIQQQQQQ" if is_64 else f"{endian}IIIIIIII"
    ph_size = 56 if is_64 else 32
    if phentsize < ph_size:
        return []
    out: list[dict[str, Any]] = []
    for idx in range(min(phnum, 64)):
        pos = phoff + idx * phentsize
        if pos + ph_size > len(data):
            break
        p_type, p_offset, p_vaddr, p_filesz = struct.unpack_from(ph_fmt, data, pos)[:4]
        out.append({
            "index": idx,
            "type": p_type,
            "offset": p_offset,
            "vaddr": p_vaddr,
            "filesz": p_filesz,
        })
    return out


def _parse_needed_libraries(
    data: bytes,
    sections: list[dict[str, Any]],
    *,
    is_64: bool,
    endian: str,
) -> list[str]:
    dyn_sec = next((s for s in sections if s.get("name") == ".dynamic"), None)
    dynstr_sec = next((s for s in sections if s.get("name") == ".dynstr"), None)
    if not dyn_sec or not dynstr_sec:
        return []

    dyn_off = int(dyn_sec["offset"])
    dyn_size = int(dyn_sec["size"])
    str_off = int(dynstr_sec["offset"])
    str_size = int(dynstr_sec["size"])
    if dyn_off + 16 > len(data) or str_off + 1 > len(data):
        return []

    dynstr = data[str_off:str_off + min(str_size, 65536)]
    entry_size = 16 if is_64 else 8
    fmt = f"{endian}QQ" if is_64 else f"{endian}II"
    needed: list[str] = []
    pos = dyn_off
    end = min(len(data), dyn_off + dyn_size)
    while pos + entry_size <= end:
        tag, val = struct.unpack_from(fmt, data, pos)
        if tag == _DT_NULL:
            break
        if tag == _DT_NEEDED and 0 <= val < len(dynstr):
            end_name = dynstr.find(b"\x00", val)
            name = dynstr[val:end_name if end_name >= 0 else len(dynstr)].decode("ascii", errors="ignore")
            if name and name not in needed:
                needed.append(name)
        pos += entry_size
    return needed


def _triage_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    needed = list(result.get("needed_libraries") or [])
    if needed:
        out.append({
            "kind": "note",
            "tag": "dynamic_libraries",
            "title": f"{len(needed)} dynamic libraries referenced",
            "confidence": "verified",
            "details": needed[:12],
        })
    header = result.get("header") or {}
    if header.get("type") == "executable":
        out.append({
            "kind": "note",
            "tag": "elf_executable",
            "title": f"ELF executable ({header.get('machine')})",
            "confidence": "verified",
        })
    suspicious = [s for s in (result.get("strings_preview") or []) if "password" in s.lower() or "api_key" in s.lower()]
    if suspicious:
        out.append({
            "kind": "note",
            "tag": "sensitive_strings",
            "title": "Potentially sensitive strings in binary",
            "confidence": "probable",
            "details": suspicious[:8],
        })
    return out
