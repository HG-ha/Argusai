"""Pure-Python Windows PE static triage."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from argusai.core.reverse.native_common import (
    _SUSPICIOUS_IMPORTS,
    extract_ascii_strings,
    pe_machine_name,
    sha256_bytes,
    sha256_file,
)


def _read_cstring(data: bytes, offset: int, limit: int = 256) -> str:
    end = data.find(b"\x00", offset, offset + limit)
    if end < 0:
        end = min(offset + limit, len(data))
    return data[offset:end].decode("ascii", errors="ignore")


def analyze_pe_bytes(data: bytes, *, filename: str = "sample.exe") -> dict[str, Any]:
    digest = sha256_bytes(data)
    result = _parse_pe(data)
    result["sha256"] = digest
    result["filename"] = filename
    result["size"] = len(data)
    if result.get("ok"):
        result["findings"] = _triage_findings(result)
        result["risk_tags"] = sorted({f["tag"] for f in result["findings"] if f.get("tag")})
    return result


def analyze_pe_path(path: Path, *, max_string_scan: int = 512_000) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if len(data) > 100 * 1024 * 1024:
        return {"ok": False, "error": "PE file too large (>100MB)"}
    out = analyze_pe_bytes(data, filename=path.name)
    out["path"] = str(path)
    out["sha256"] = sha256_file(path)
    return out


def _parse_pe(data: bytes) -> dict[str, Any]:
    if len(data) < 64 or data[:2] != b"MZ":
        return {"ok": False, "error": "not a PE file (missing MZ header)"}

    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return {"ok": False, "error": "invalid PE signature"}

    file_header_offset = e_lfanew + 4
    machine, num_sections, _, _, _, size_opt_header, characteristics = struct.unpack_from(
        "<HHIIIHH", data, file_header_offset
    )
    opt_offset = file_header_offset + 20
    if opt_offset + 2 > len(data):
        return {"ok": False, "error": "truncated optional header"}

    magic = struct.unpack_from("<H", data, opt_offset)[0]
    is_pe32_plus = magic == 0x20B
    is_pe32 = magic == 0x10B
    if not (is_pe32 or is_pe32_plus):
        return {"ok": False, "error": f"unsupported PE magic: 0x{magic:04x}"}

    if is_pe32_plus:
        entry_point, = struct.unpack_from("<I", data, opt_offset + 16)
        image_base, = struct.unpack_from("<Q", data, opt_offset + 24)
        num_rva, = struct.unpack_from("<I", data, opt_offset + 108)
        import_rva_offset = opt_offset + 112 + 8 * 1  # DataDirectory[1] = imports
    else:
        entry_point, = struct.unpack_from("<I", data, opt_offset + 16)
        image_base, = struct.unpack_from("<I", data, opt_offset + 28)
        num_rva, = struct.unpack_from("<I", data, opt_offset + 92)
        import_rva_offset = opt_offset + 96 + 8 * 1

    import_rva, import_size = struct.unpack_from("<II", data, import_rva_offset)
    sections = _parse_sections(data, file_header_offset + 20 + size_opt_header, num_sections)
    imports = _parse_imports(data, sections, import_rva) if import_rva else []

    strings = extract_ascii_strings(data[:512_000], limit=200)
    suspicious = sorted({s for s in strings if s in _SUSPICIOUS_IMPORTS})

    return {
        "ok": True,
        "format": "pe",
        "confidence": "high" if imports else "probable",
        "tools_used": ["pe_parser"],
        "header": {
            "machine": pe_machine_name(machine),
            "characteristics": characteristics,
            "sections": num_sections,
            "entry_point_rva": entry_point,
            "image_base": image_base,
            "pe32_plus": is_pe32_plus,
        },
        "sections": sections,
        "imports": imports[:64],
        "import_dll_count": len(imports),
        "suspicious_imports": suspicious,
        "strings_preview": strings[:80],
    }


def _parse_sections(data: bytes, offset: int, count: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pos = offset
    for _ in range(min(count, 96)):
        if pos + 40 > len(data):
            break
        name = data[pos:pos + 8].split(b"\x00", 1)[0].decode("ascii", errors="ignore")
        virtual_size, virtual_addr, raw_size, raw_ptr = struct.unpack_from("<IIII", data, pos + 8)
        chars, = struct.unpack_from("<I", data, pos + 36)
        out.append({
            "name": name,
            "virtual_size": virtual_size,
            "virtual_address": virtual_addr,
            "raw_size": raw_size,
            "raw_ptr": raw_ptr,
            "characteristics": chars,
        })
        pos += 40
    return out


def _rva_to_offset(sections: list[dict[str, Any]], rva: int) -> int | None:
    for sec in sections:
        va = sec["virtual_address"]
        vs = max(sec["virtual_size"], sec["raw_size"])
        if va <= rva < va + vs:
            return sec["raw_ptr"] + (rva - va)
    return None


def _parse_imports(data: bytes, sections: list[dict[str, Any]], import_rva: int) -> list[dict[str, Any]]:
    offset = _rva_to_offset(sections, import_rva)
    if offset is None or offset + 20 > len(data):
        return []
    out: list[dict[str, Any]] = []
    pos = offset
    while pos + 20 <= len(data):
        _, _, _, name_rva, _ = struct.unpack_from("<IIIII", data, pos)
        if name_rva == 0:
            break
        name_off = _rva_to_offset(sections, name_rva)
        dll = _read_cstring(data, name_off or 0).lower() if name_off else ""
        if dll:
            out.append({"dll": dll, "symbols": []})
        pos += 20
        if len(out) >= 64:
            break
    return out


def _triage_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    suspicious = list(result.get("suspicious_imports") or [])
    if suspicious:
        out.append({
            "kind": "pe_import",
            "tag": "suspicious_api",
            "title": "PE imports potentially sensitive Win32 APIs",
            "confidence": "probable",
            "details": suspicious,
        })
    sections = result.get("sections") or []
    rx = [s for s in sections if s.get("name", "").lower() in {".text", "code"}]
    if not rx and sections:
        out.append({
            "kind": "note",
            "tag": "unusual_sections",
            "title": "PE section layout may be packed or unusual",
            "confidence": "low",
        })
    return out
