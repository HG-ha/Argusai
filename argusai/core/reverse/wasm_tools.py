"""WebAssembly reverse-engineering primitives.

P5 toolset, designed to NEVER force binary blobs through the chat
context. All five tools take a filesystem path in and return either a
short JSON summary or a path to a generated artifact on disk.

Tools:

- :func:`wasm_disassemble` — runs vendored ``wasm2wat`` to produce
  human-readable WAT text; writes the WAT to ``<wasm>.wat`` next to
  the input by default and returns metadata + the first N lines.
- :func:`wasm_decompile` — runs vendored ``wasm-decompile`` to produce
  a higher-level C-like view; same on-disk contract.
- :func:`wasm_strings` — pure-Python: parses the binary directly and
  pulls printable strings out of the data section. Doesn't need WABT.
- :func:`wasm_call_graph` — pure-Python: walks the code section to
  build caller→callee edges, plus imports/exports inventory. Result
  ships as JSON + a markdown summary on disk.
- :func:`wasm_inspect` — fast metadata-only probe (sections, version,
  size, sha256). Replaces / supersedes the old ``wasm_inventory``
  triage path for "is this even a real wasm file".

The pure-Python parsers handle the subset of the WASM 1.0 binary
format we need for triage. They deliberately do NOT validate or
execute — they just walk section headers and pull data. A malformed
binary that lies about its section sizes will produce an
``error`` field but won't crash the process.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argusai.core.reverse import runtime_manager


# ---------------------------------------------------------------------------
# Binary format constants (https://webassembly.github.io/spec/core/binary)
# ---------------------------------------------------------------------------

WASM_MAGIC = b"\x00asm"
WASM_VERSION_1 = b"\x01\x00\x00\x00"

SECTION_NAMES = {
    0: "custom",
    1: "type",
    2: "import",
    3: "function",
    4: "table",
    5: "memory",
    6: "global",
    7: "export",
    8: "start",
    9: "element",
    10: "code",
    11: "data",
    12: "data_count",
}

IMPORT_KINDS = {0: "func", 1: "table", 2: "memory", 3: "global"}
EXPORT_KINDS = {0: "func", 1: "table", 2: "memory", 3: "global"}

# How much body to slip into responses for the model. Larger artifacts
# go to disk only — the chat carries the path, not the content.
WAT_PREVIEW_LINES = 200
DECOMP_PREVIEW_LINES = 200
STRINGS_PREVIEW = 100
CALLGRAPH_PREVIEW_EDGES = 80

# Pure-Python parsers refuse to allocate more than this for any single
# section vector. A hostile wasm could lie about a section size and
# claim it has 4 billion functions; we bail at this cap.
MAX_SECTION_VEC = 2_000_000
MAX_BINARY_BYTES = 256 * 1024 * 1024  # 256 MB hard cap


# ---------------------------------------------------------------------------
# LEB128 + binary cursor — minimal, fast enough for triage
# ---------------------------------------------------------------------------


class _Cursor:
    """Tiny binary cursor with LEB128 readers. Doesn't validate types
    beyond magic; meant for forensic walking, not strict spec compliance.
    """

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0) -> None:
        self.buf = buf
        self.pos = pos

    def eof(self) -> bool:
        return self.pos >= len(self.buf)

    def remaining(self) -> int:
        return max(0, len(self.buf) - self.pos)

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise ValueError(f"unexpected EOF reading {n} bytes at {self.pos}")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def read_u8(self) -> int:
        b = self.read(1)[0]
        return b

    def read_uleb128(self, max_bits: int = 33) -> int:
        result = 0
        shift = 0
        while True:
            b = self.read_u8()
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
            if shift >= max_bits + 7:
                raise ValueError(f"uleb128 overflow at {self.pos}")
        return result

    def read_sleb128(self, max_bits: int = 33) -> int:
        result = 0
        shift = 0
        size = max_bits
        while True:
            b = self.read_u8()
            result |= (b & 0x7F) << shift
            shift += 7
            if (b & 0x80) == 0:
                # Sign-extend if the high bit of the last group is set.
                if (b & 0x40) and shift < size:
                    result |= -(1 << shift)
                return result
            if shift >= size + 7:
                raise ValueError(f"sleb128 overflow at {self.pos}")

    def read_name(self) -> str:
        n = self.read_uleb128()
        if n > 1_000_000:
            raise ValueError(f"name too long: {n}")
        raw = self.read(n)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Module summary — used by every tool here
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    id: int
    name: str
    offset: int
    size: int


@dataclass
class WasmSummary:
    path: str = ""
    size_bytes: int = 0
    sha256: str = ""
    valid_magic: bool = False
    version: int = 0
    sections: list[_Section] = field(default_factory=list)
    imports: list[dict[str, str]] = field(default_factory=list)
    exports: list[dict[str, Any]] = field(default_factory=list)
    type_count: int = 0
    function_count: int = 0
    table_count: int = 0
    memory_count: int = 0
    global_count: int = 0
    data_segment_count: int = 0
    custom_section_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "valid_magic": self.valid_magic,
            "version": self.version,
            "sections": [
                {"id": s.id, "name": s.name, "offset": s.offset, "size": s.size}
                for s in self.sections
            ],
            "imports": self.imports,
            "exports": self.exports,
            "type_count": self.type_count,
            "function_count": self.function_count,
            "table_count": self.table_count,
            "memory_count": self.memory_count,
            "global_count": self.global_count,
            "data_segment_count": self.data_segment_count,
            "custom_section_names": self.custom_section_names,
            "errors": self.errors,
        }


def _read_wasm_bytes(path: str | Path) -> tuple[Path, bytes]:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    size = resolved.stat().st_size
    if size > MAX_BINARY_BYTES:
        raise ValueError(
            f"file too large ({size} bytes > {MAX_BINARY_BYTES}); use wasm_inspect first"
        )
    with resolved.open("rb") as fh:
        data = fh.read()
    return resolved, data


def _scan_module(data: bytes, path: str = "") -> WasmSummary:
    """Walk every section header so callers can branch on what's present
    without re-parsing the file. Failures inside individual sections are
    captured in ``errors`` and don't abort the scan.
    """

    s = WasmSummary(
        path=path, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    if len(data) < 8 or data[:4] != WASM_MAGIC:
        s.valid_magic = False
        s.errors.append("missing or invalid magic '\\0asm'")
        return s
    s.valid_magic = True
    s.version = struct.unpack("<I", data[4:8])[0]

    cur = _Cursor(data, 8)
    while not cur.eof():
        sec_start = cur.pos
        try:
            section_id = cur.read_u8()
            section_size = cur.read_uleb128()
        except Exception as exc:
            s.errors.append(f"section header at {sec_start}: {exc}")
            break
        body_start = cur.pos
        body_end = body_start + section_size
        if body_end > len(data):
            s.errors.append(
                f"section {section_id} at {sec_start} claims size {section_size} "
                f"but only {len(data) - body_start} bytes remain"
            )
            break
        name = SECTION_NAMES.get(section_id, f"section_{section_id}")
        s.sections.append(_Section(section_id, name, body_start, section_size))

        body = data[body_start:body_end]
        try:
            _parse_section_body(s, section_id, body)
        except Exception as exc:
            s.errors.append(f"section {section_id} ({name}): {exc}")
        cur.pos = body_end
    return s


def _parse_section_body(s: WasmSummary, sid: int, body: bytes) -> None:
    if sid == 0:  # custom
        c = _Cursor(body)
        try:
            s.custom_section_names.append(c.read_name())
        except Exception:
            s.custom_section_names.append("<unreadable>")
        return
    if sid == 1:  # type — just count
        c = _Cursor(body)
        n = c.read_uleb128()
        s.type_count = min(n, MAX_SECTION_VEC)
        return
    if sid == 2:  # import
        c = _Cursor(body)
        n = c.read_uleb128()
        n = min(n, MAX_SECTION_VEC)
        for _ in range(n):
            mod = c.read_name()
            nm = c.read_name()
            kind = c.read_u8()
            # Skip the type-specific descriptor — we only need name/kind for triage.
            try:
                if kind == 0:
                    c.read_uleb128()  # type index
                elif kind == 1:
                    # table: reftype + limits
                    c.read_u8()
                    flags = c.read_u8()
                    c.read_uleb128()
                    if flags & 1:
                        c.read_uleb128()
                elif kind == 2:
                    flags = c.read_u8()
                    c.read_uleb128()
                    if flags & 1:
                        c.read_uleb128()
                elif kind == 3:
                    c.read_u8()  # valtype
                    c.read_u8()  # mutability
            except Exception:
                pass
            s.imports.append({
                "module": mod, "name": nm,
                "kind": IMPORT_KINDS.get(kind, str(kind)),
            })
        return
    if sid == 3:  # function — count
        c = _Cursor(body)
        s.function_count = min(c.read_uleb128(), MAX_SECTION_VEC)
        return
    if sid == 4:  # table
        c = _Cursor(body)
        s.table_count = min(c.read_uleb128(), MAX_SECTION_VEC)
        return
    if sid == 5:  # memory
        c = _Cursor(body)
        s.memory_count = min(c.read_uleb128(), MAX_SECTION_VEC)
        return
    if sid == 6:  # global
        c = _Cursor(body)
        s.global_count = min(c.read_uleb128(), MAX_SECTION_VEC)
        return
    if sid == 7:  # export
        c = _Cursor(body)
        n = c.read_uleb128()
        n = min(n, MAX_SECTION_VEC)
        for _ in range(n):
            nm = c.read_name()
            kind = c.read_u8()
            idx = c.read_uleb128()
            s.exports.append({
                "name": nm,
                "kind": EXPORT_KINDS.get(kind, str(kind)),
                "index": idx,
            })
        return
    if sid == 11:  # data
        c = _Cursor(body)
        s.data_segment_count = min(c.read_uleb128(), MAX_SECTION_VEC)
        return


def _slug(path: Path) -> str:
    """Pick a stable basename for derived artifacts (``foo.wasm`` →
    ``foo.wat`` next to it).
    """
    return re.sub(r"\.wasm$", "", path.name, flags=re.IGNORECASE) or path.stem


# ---------------------------------------------------------------------------
# wasm_inspect — pure-Python metadata probe
# ---------------------------------------------------------------------------


def wasm_inspect(path: str, *, base: str | None = None) -> dict[str, Any]:
    """Fast 'is this a real wasm + what's inside' probe. Always cheap;
    never runs WABT.
    """
    try:
        resolved, data = _read_wasm_bytes(path)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    summary = _scan_module(data, path=str(resolved))
    return {"ok": True, **summary.to_dict()}


# ---------------------------------------------------------------------------
# wasm_disassemble — wasm2wat
# ---------------------------------------------------------------------------


def _wat_output_path(path: Path, out_dir: Path | None = None) -> Path:
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{_slug(path)}.wat"
    return path.with_suffix(".wat") if path.suffix.lower() == ".wasm" else path.with_name(f"{path.name}.wat")


def wasm_disassemble(
    path: str,
    *,
    out_path: str | None = None,
    fold_exprs: bool = True,
    no_check: bool = True,
    api: Any | None = None,
) -> dict[str, Any]:
    """Run ``wasm2wat`` on ``path`` and write WAT text to disk.

    Returns a small dict with the artifact path, summary metadata, and
    the first ``WAT_PREVIEW_LINES`` lines of WAT so the model can sanity-
    check the disassembly without paying the cost of the whole file.

    ``no_check=True`` (the default) lets us disassemble obfuscated /
    illegal-but-still-loadable modules that fail strict validation —
    this is reverse engineering, we want to look at the bytes the
    runtime actually accepts.
    """
    try:
        resolved, _ = _read_wasm_bytes(path)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = Path(out_path).expanduser().resolve() if out_path else _wat_output_path(resolved)
    args = [str(resolved), "-o", str(out)]
    if fold_exprs:
        args.append("--fold-exprs")
    if no_check:
        args.append("--no-check")
    r = runtime_manager.wabt_run("wasm2wat", args=args, timeout=60, api=api)
    if not r.get("ok"):
        return {
            "ok": False,
            "error": r.get("error") or r.get("stderr") or "wasm2wat failed",
            "stderr": (r.get("stderr") or "")[:2000],
            "returncode": r.get("returncode", -1),
        }
    if not out.exists() or out.stat().st_size == 0:
        return {
            "ok": False,
            "error": "wasm2wat reported success but produced no output",
            "stderr": (r.get("stderr") or "")[:2000],
        }
    text = out.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    preview = "\n".join(lines[:WAT_PREVIEW_LINES])
    return {
        "ok": True,
        "wat_path": str(out),
        "wat_size_bytes": out.stat().st_size,
        "wat_total_lines": len(lines),
        "preview_lines": min(WAT_PREVIEW_LINES, len(lines)),
        "preview": preview,
        "more": len(lines) > WAT_PREVIEW_LINES,
        "elapsed_ms": r.get("elapsed_ms", 0),
        "hint": (
            f"WAT written to {out}. Use read_text_file(path='{out}', offset=...) "
            "or grep_text(pattern=..., path=...) for targeted reads — the full "
            "file is too big to inline."
        ),
    }


# ---------------------------------------------------------------------------
# wasm_decompile — wasm-decompile
# ---------------------------------------------------------------------------


def wasm_decompile(
    path: str,
    *,
    out_path: str | None = None,
    api: Any | None = None,
) -> dict[str, Any]:
    """Run ``wasm-decompile`` for a higher-level C-like view. Most
    reverse-engineering work starts here — it's much more readable than
    raw WAT for non-trivial modules.
    """
    try:
        resolved, _ = _read_wasm_bytes(path)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out_base = Path(out_path).expanduser().resolve() if out_path else None
    if out_base is None:
        out_base = resolved.with_suffix(".dcmp") if resolved.suffix.lower() == ".wasm" else resolved.with_name(f"{resolved.name}.dcmp")
    out_base.parent.mkdir(parents=True, exist_ok=True)
    args = [str(resolved), "-o", str(out_base)]
    r = runtime_manager.wabt_run("wasm-decompile", args=args, timeout=120, api=api)
    if not r.get("ok"):
        return {
            "ok": False,
            "error": r.get("error") or r.get("stderr") or "wasm-decompile failed",
            "stderr": (r.get("stderr") or "")[:2000],
            "returncode": r.get("returncode", -1),
        }
    if not out_base.exists() or out_base.stat().st_size == 0:
        return {
            "ok": False,
            "error": "wasm-decompile reported success but produced no output",
            "stderr": (r.get("stderr") or "")[:2000],
        }
    text = out_base.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    preview = "\n".join(lines[:DECOMP_PREVIEW_LINES])
    return {
        "ok": True,
        "decompiled_path": str(out_base),
        "decompiled_size_bytes": out_base.stat().st_size,
        "decompiled_total_lines": len(lines),
        "preview_lines": min(DECOMP_PREVIEW_LINES, len(lines)),
        "preview": preview,
        "more": len(lines) > DECOMP_PREVIEW_LINES,
        "elapsed_ms": r.get("elapsed_ms", 0),
        "hint": (
            f"Decompiled output at {out_base}. "
            "Use read_text_file / grep_text to navigate; for huge modules "
            "grep_text(mode='context') around interesting symbols is much "
            "cheaper than a full read."
        ),
    }


# ---------------------------------------------------------------------------
# wasm_strings — strings(1) for wasm, pure Python
# ---------------------------------------------------------------------------


_PRINTABLE_RUN = re.compile(rb"[\x20-\x7e\t]{4,}")


def wasm_strings(
    path: str,
    *,
    min_len: int = 6,
    max_results: int = 200,
    where: str = "data",
    out_path: str | None = None,
) -> dict[str, Any]:
    """Extract printable strings from a .wasm file. Pure Python — no
    WABT dependency.

    ``where``:

    - ``"data"`` (default): only walk the data section(s). Cleanest
      results — these are the real string literals the module ships.
    - ``"all"``: scan every byte. Use this if the binary uses a
      non-standard packer / encoder.

    Long string runs go to disk if there are more than ``max_results``
    so the chat response stays small.
    """
    try:
        resolved, data = _read_wasm_bytes(path)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    where = (where or "data").lower()
    min_len = max(2, min(int(min_len or 6), 256))
    cap = max(1, min(int(max_results or 200), 5000))

    if where == "data":
        s = _scan_module(data, path=str(resolved))
        ranges: list[tuple[int, int]] = []
        for sec in s.sections:
            if sec.id == 11:  # data
                ranges.append((sec.offset, sec.offset + sec.size))
        if not ranges:
            return {
                "ok": True, "path": str(resolved), "where": "data",
                "items": [], "count": 0, "truncated": False,
                "hint": "no data section in this module",
            }
        blob = b"".join(data[a:b] for a, b in ranges)
    else:
        blob = data

    seen: set[bytes] = set()
    items: list[dict[str, Any]] = []
    total = 0
    for m in _PRINTABLE_RUN.finditer(blob):
        run = m.group(0)
        if len(run) < min_len:
            continue
        total += 1
        if run in seen:
            continue
        seen.add(run)
        if len(items) < cap:
            items.append({
                "offset": m.start(),
                "length": len(run),
                "value": run.decode("utf-8", errors="replace"),
            })
        elif total > cap * 4:
            break

    written_to: str | None = None
    if out_path or len(items) >= cap:
        target = Path(out_path).expanduser().resolve() if out_path else resolved.with_suffix(".strings.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{it['offset']:08x}  {it['value']}" for it in items]
        target.write_text("\n".join(lines), encoding="utf-8")
        written_to = str(target)

    return {
        "ok": True,
        "path": str(resolved),
        "where": where,
        "items": items[:STRINGS_PREVIEW],
        "count_returned": min(len(items), STRINGS_PREVIEW),
        "count_total_or_seen": total,
        "truncated": total > len(items),
        "written_to": written_to,
        "hint": (
            (f"{total} candidate strings; first {min(len(items), STRINGS_PREVIEW)} shown" if total else "no printable runs >= min_len")
            + (f". Full list written to {written_to}; read with read_text_file." if written_to else "")
        ),
    }


# ---------------------------------------------------------------------------
# wasm_call_graph — pure Python parser walking the code section
# ---------------------------------------------------------------------------


def _walk_code_section(body: bytes) -> dict[int, list[int]]:
    """Return ``{func_local_index: [callee_indices...]}`` by walking each
    function body and recording every ``call`` (opcode 0x10) target.
    We don't decode the full opcode stream — we just look for the
    distinctive shape of ``0x10 <uleb128>`` while respecting the
    declared body lengths so we don't drift into immediates.

    This is intentionally a 'best-effort heuristic over function
    bodies', not a real decoder. It catches direct calls cleanly,
    which is what reverse engineers want; indirect calls (0x11) are
    reported as ``-1`` so they show up in the graph as
    "unresolved → ?".
    """
    c = _Cursor(body)
    nfuncs = c.read_uleb128()
    nfuncs = min(nfuncs, MAX_SECTION_VEC)
    edges: dict[int, list[int]] = {}
    for fn_idx in range(nfuncs):
        body_size = c.read_uleb128()
        body_end = c.pos + body_size
        if body_end > len(body):
            break
        # Skip local declarations.
        try:
            nlocals = c.read_uleb128()
            for _ in range(min(nlocals, 10000)):
                c.read_uleb128()  # count
                c.read_u8()  # valtype
        except Exception:
            c.pos = body_end
            continue
        callees: list[int] = []
        # Greedy scan: look for 0x10 / 0x11 opcodes followed by uleb128.
        # We can't perfectly parse all opcodes here, but ``call`` /
        # ``call_indirect`` are common enough that a byte-by-byte
        # sweep finds them faithfully for the vast majority of code.
        scan = body[c.pos:body_end]
        i = 0
        n = len(scan)
        while i < n:
            op = scan[i]
            i += 1
            if op == 0x10:  # call
                # decode uleb128 from i
                val = 0
                shift = 0
                start = i
                while i < n:
                    b = scan[i]
                    i += 1
                    val |= (b & 0x7F) << shift
                    if (b & 0x80) == 0:
                        break
                    shift += 7
                    if shift > 28:
                        i = start  # malformed, give up on this call
                        break
                else:
                    break
                callees.append(val)
            elif op == 0x11:  # call_indirect — type idx + table idx
                # Best-effort: record as unresolved.
                callees.append(-1)
        edges[fn_idx] = callees
        c.pos = body_end
    return edges


def wasm_call_graph(
    path: str,
    *,
    out_path: str | None = None,
    max_edges_preview: int = CALLGRAPH_PREVIEW_EDGES,
) -> dict[str, Any]:
    """Build a function-level call graph from a .wasm file. Pure Python.

    Resolves indices to names where possible:

    - Imported function names → ``mod.name`` from the import section.
    - Exported names → ``export:name`` from the export section.
    - Internal functions → ``func:<index>``.

    Writes the full graph JSON to disk; returns a markdown preview and
    summary so the model sees enough to navigate without paying for
    the whole graph.
    """
    try:
        resolved, data = _read_wasm_bytes(path)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"file not found: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    summary = _scan_module(data, path=str(resolved))
    if not summary.valid_magic:
        return {"ok": False, "error": "not a valid wasm module", **summary.to_dict()}

    # Find code section body.
    code_body: bytes | None = None
    for sec in summary.sections:
        if sec.id == 10:
            code_body = data[sec.offset:sec.offset + sec.size]
            break
    if code_body is None:
        return {
            "ok": True,
            "path": str(resolved),
            "edges": {},
            "edge_count": 0,
            "hint": "module has no code section (likely import-only or stub)",
        }

    try:
        edges = _walk_code_section(code_body)
    except Exception as e:
        return {
            "ok": False,
            "error": f"failed to parse code section: {type(e).__name__}: {e}",
            **summary.to_dict(),
        }

    n_imports = sum(1 for im in summary.imports if im["kind"] == "func")
    name_for: dict[int, str] = {}
    for i, im in enumerate(summary.imports):
        if im["kind"] != "func":
            continue
        name_for[i] = f"{im['module']}.{im['name']}"
    for ex in summary.exports:
        if ex.get("kind") == "func":
            name_for[int(ex.get("index", -1))] = f"export:{ex.get('name')}"

    def resolve(idx: int) -> str:
        if idx < 0:
            return "indirect:?"
        if idx in name_for:
            return name_for[idx]
        if idx < n_imports:
            return f"import:{idx}"
        return f"func:{idx}"

    # Local function global index = n_imports + local.
    graph: dict[str, dict[str, Any]] = {}
    total_edges = 0
    for local_idx, callees in edges.items():
        gi = n_imports + local_idx
        from_name = resolve(gi)
        callee_names = [resolve(c) for c in callees]
        graph[from_name] = {
            "global_index": gi,
            "calls": callee_names,
            "call_count": len(callees),
        }
        total_edges += len(callees)

    # Inverted index — "who calls X" — much more useful for triage.
    callers: dict[str, list[str]] = {}
    for from_name, info in graph.items():
        for callee in info["calls"]:
            callers.setdefault(callee, []).append(from_name)

    # Hotspots: functions with the most outgoing calls and the most
    # incoming calls. These are usually the dispatchers / glue code
    # the reverse engineer should look at first.
    hot_out = sorted(
        ((info["call_count"], name) for name, info in graph.items()),
        reverse=True,
    )[:10]
    hot_in = sorted(
        ((len(v), name) for name, v in callers.items()),
        reverse=True,
    )[:10]

    payload = {
        "path": str(resolved),
        "function_count_local": len(edges),
        "function_count_imported": n_imports,
        "edge_count": total_edges,
        "exports": [{"name": e["name"], "kind": e["kind"], "index": e["index"]}
                    for e in summary.exports],
        "imports": summary.imports,
        "graph": graph,
        "callers": callers,
        "hotspots_outgoing": [{"name": n, "calls_out": c} for c, n in hot_out],
        "hotspots_incoming": [{"name": n, "calls_in": c} for c, n in hot_in],
    }

    if out_path:
        target = Path(out_path).expanduser().resolve()
    else:
        target = resolved.with_suffix(".callgraph.json") if resolved.suffix.lower() == ".wasm" else resolved.with_name(f"{resolved.name}.callgraph.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown sibling for human / model browsing.
    md_target = target.with_suffix(".md")
    md = io.StringIO()
    md.write(f"# Call graph for `{resolved.name}`\n\n")
    md.write(f"- Local functions: **{len(edges)}**\n")
    md.write(f"- Imported functions: **{n_imports}**\n")
    md.write(f"- Total call edges (direct + indirect placeholders): **{total_edges}**\n")
    md.write(f"- Exports: **{len(summary.exports)}**\n\n")
    md.write("## Hotspots — most outgoing calls (likely dispatchers)\n\n")
    for h in payload["hotspots_outgoing"]:
        md.write(f"- `{h['name']}` → {h['calls_out']} calls\n")
    md.write("\n## Hotspots — most incoming calls (likely utility / runtime)\n\n")
    for h in payload["hotspots_incoming"]:
        md.write(f"- `{h['name']}` ← {h['calls_in']} callers\n")
    md.write("\n## Exports\n\n")
    for ex in summary.exports:
        md.write(f"- `{ex['name']}` ({ex['kind']}, index {ex['index']})\n")
    md.write("\n## Imports\n\n")
    for im in summary.imports[:200]:
        md.write(f"- `{im['module']}.{im['name']}` ({im['kind']})\n")
    md_target.write_text(md.getvalue(), encoding="utf-8")

    # Preview edges sample for the response itself.
    preview_lines = []
    seen = 0
    for name, info in graph.items():
        if seen >= max_edges_preview:
            break
        if not info["calls"]:
            continue
        preview_lines.append(f"{name} -> {', '.join(info['calls'][:6])}")
        seen += 1

    return {
        "ok": True,
        "path": str(resolved),
        "function_count_local": len(edges),
        "function_count_imported": n_imports,
        "edge_count": total_edges,
        "export_count": len(summary.exports),
        "import_count": len(summary.imports),
        "callgraph_json_path": str(target),
        "callgraph_md_path": str(md_target),
        "hotspots_outgoing": payload["hotspots_outgoing"],
        "hotspots_incoming": payload["hotspots_incoming"],
        "edge_preview": preview_lines,
        "hint": (
            f"Full call graph JSON at {target}; markdown summary at {md_target}. "
            "Use read_text_file on the markdown for the human-readable view, "
            "grep_text on the JSON to look up specific function names."
        ),
    }
