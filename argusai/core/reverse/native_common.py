"""Shared helpers for mobile/native static analysis."""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable

_READABLE_RE = re.compile(rb"[\x20-\x7e]{4,}")
_PERMISSION_RE = re.compile(r"android\.permission\.[A-Za-z0-9_.]+")
_PACKAGE_LIKE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,6}$", re.I)
_SUSPICIOUS_IMPORTS = frozenset({
    "VirtualAlloc", "VirtualProtect", "WriteProcessMemory", "CreateRemoteThread",
    "NtWriteVirtualMemory", "URLDownloadToFile", "WinExec", "ShellExecute",
    "IsDebuggerPresent", "CheckRemoteDebuggerPresent",
})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_ascii_strings(data: bytes, *, min_len: int = 4, limit: int = 500) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _READABLE_RE.finditer(data):
        s = m.group(0).decode("ascii", errors="ignore")
        if len(s) < min_len or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def extract_utf16le_strings(data: bytes, *, min_len: int = 4, limit: int = 200) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(data) - min_len * 2:
        if data[i + 1] != 0 or data[i] < 0x20 or data[i] > 0x7e:
            i += 1
            continue
        chars: list[str] = []
        j = i
        while j < len(data) - 1 and data[j + 1] == 0 and 0x20 <= data[j] <= 0x7e:
            chars.append(chr(data[j]))
            j += 2
            if len(chars) >= 256:
                break
        if len(chars) >= min_len:
            s = "".join(chars)
            if s not in seen:
                seen.add(s)
                out.append(s)
                if len(out) >= limit:
                    break
        i = max(i + 1, j)
    return out


def guess_package_names(strings: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in strings:
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        if _PACKAGE_LIKE_RE.match(s) and s.count(".") >= 1:
            seen.add(s)
            candidates.append(s)
        if len(candidates) >= 8:
            break
    return candidates


def summarize_zip_entries(zf: zipfile.ZipFile) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        total += info.file_size
        if len(entries) < 200:
            entries.append({
                "name": info.filename,
                "size": info.file_size,
                "compressed": info.compress_size,
            })
    return {
        "entry_count": len([i for i in zf.infolist() if not i.is_dir()]),
        "uncompressed_bytes": total,
        "entries_preview": entries,
        "truncated": len(zf.infolist()) > 200,
    }


def read_limited(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file too large ({size} bytes > {max_bytes})")
    return path.read_bytes()


def pe_machine_name(machine: int) -> str:
    return {
        0x014C: "i386",
        0x8664: "amd64",
        0xAA64: "arm64",
        0x01C0: "arm",
        0x01C4: "armnt",
    }.get(machine, f"unknown(0x{machine:04x})")
