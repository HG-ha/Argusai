"""Generic text-file read / grep primitives for AI tools.

These are the "two-step" read primitives:

1. **`read_text_file`**: read part of a text file. Always returns
   line-numbered output (so the model can refer back by line number),
   always honours ``offset`` and ``limit``, and never inlines more than
   ``MAX_CHARS_PER_CALL`` bytes — the model has to make a *second* call
   if it wants more.

2. **`grep_text_file`**: search a text file for a regex pattern. Returns
   only matching lines (or just file metadata) — *not* the whole file.
   Modes mirror ripgrep: ``content`` (matches + line numbers),
   ``files_with_matches`` (only "yes/no" if a single path; useful when
   batched), ``count``.

3. **`grep_text_dir`**: recursive multi-file grep. Returns
   ``path:line:match`` records (line clipped) for the model to triage.

The goal is to make "show me the function ``foo`` in file X" cost ~2 KB
of context rather than 200 KB. Used directly by the AI; used internally
by tools like ``js_fetch_asset`` to honour ``head_lines`` / ``offset``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable


# ----------------------------------------------------------------------
# Safety / format constants
# ----------------------------------------------------------------------

# Per-call upper bounds — even if the caller says "read 10000 lines",
# we never give the model more than this many characters or lines in a
# single response. They can paginate via ``offset`` / ``limit``.
MAX_CHARS_PER_CALL = 24_000
MAX_LINES_PER_CALL = 800
DEFAULT_LIMIT_LINES = 200
DEFAULT_LINE_TRUNC = 400  # chars per displayed line

# Grep upper bounds.
MAX_GREP_MATCHES = 200
DEFAULT_GREP_MATCHES = 50
MAX_GREP_LINE_LEN = 300
MAX_FILES_SCANNED = 5000

# Slice / context-mode constants — these are how the model deals with
# single-line minified JS where ``read_text_file`` is useless because
# the whole 4 MB file is on "line 1".
DEFAULT_SLICE_LENGTH = 8_000
MAX_SLICE_LENGTH = 24_000
DEFAULT_GREP_CONTEXT = 200
MAX_GREP_CONTEXT = 2_000

# extract_strings constants.
DEFAULT_STRING_MIN_LEN = 16
DEFAULT_STRING_RESULTS = 50
MAX_STRING_RESULTS = 500
MAX_STRING_LEN_RETURNED = 600  # per string, clip the rest

# Write / stat helpers — aligned with session sandbox limits.
MAX_WRITE_CHARS = 500_000
LARGE_TEXT_READ_BYTES = 256 * 1024


def _coerce_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    """Numeric arguments from the LLM arrive as either int or string.
    Coerce, clamp, and never raise — fall back to ``default`` on garbage.
    """
    try:
        if isinstance(value, str):
            value = value.strip()
            if value == "" or value.lower() == "none":
                return default
        n = int(value)
    except Exception:
        return default
    if n < minimum:
        n = minimum
    if maximum is not None and n > maximum:
        n = maximum
    return n


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _resolve_path(path: str, base: str | None = None) -> Path:
    """Resolve a user-supplied path. If ``base`` is given, the result
    must be inside ``base`` (preventing path traversal). Otherwise the
    caller is responsible for sandboxing.
    """
    p = Path(path).expanduser()
    if base:
        root = Path(base).expanduser().resolve()
        candidate = (root / p) if not p.is_absolute() else p
        resolved = candidate.resolve()
        if not str(resolved).startswith(str(root)):
            raise ValueError(f"path {path!r} escapes base {base!r}")
        return resolved
    return p.resolve()


def _detect_encoding(data: bytes, hint: str | None) -> tuple[str, str | None]:
    """Best-effort encoding detection. Returns (encoding, bom_hint)."""
    if hint:
        return hint, None
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", "utf-8-bom"
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return "utf-16", "utf-16-bom"
    # The 99% case.
    return "utf-8", None


def _format_line_numbered(
    lines: list[str],
    start_line: int,
    line_trunc: int = DEFAULT_LINE_TRUNC,
) -> str:
    """Format lines with right-aligned line numbers, ``Read``-tool style:
    ``     5|content here``.
    Long lines are truncated to ``line_trunc`` chars + "...".
    """
    out: list[str] = []
    for i, raw in enumerate(lines):
        ln = start_line + i
        # Clip extremely long lines so a 5 MB minified-JS one-liner
        # doesn't blow the whole budget on a single line.
        text = raw
        if len(text) > line_trunc:
            text = text[:line_trunc] + f"...[{len(raw) - line_trunc} more chars on this line]"
        out.append(f"{ln:>6}|{text}")
    return "\n".join(out)


def content_raw_from_numbered(content: str) -> str:
    """Strip ``     5|`` line prefixes from ``read_text_file`` output."""
    if not content:
        return ""
    out: list[str] = []
    for line in content.splitlines():
        if re.match(r"^\s*\d+\|", line):
            out.append(re.sub(r"^\s*\d+\|", "", line, count=1))
        else:
            out.append(line)
    return "\n".join(out)


def _line_number_for_offset(text: str, offset: int) -> int:
    if offset <= 0:
        return 1
    return text.count("\n", 0, min(offset, len(text))) + 1


# ----------------------------------------------------------------------
# read_text_file
# ----------------------------------------------------------------------


def read_text_file(
    path: str,
    offset: Any = 1,
    limit: Any = DEFAULT_LIMIT_LINES,
    encoding: str | None = None,
    line_trunc: Any = DEFAULT_LINE_TRUNC,
    base: str | None = None,
) -> dict[str, Any]:
    """Read a text file with line numbers and pagination.

    Args:
        path: Path to the file. Absolute, or relative to ``base``.
        offset: 1-indexed line number to start at. Negative values count
            from the end (``-1`` = last line, ``-50`` = 50 lines before
            EOF).
        limit: Max lines to return. Internally capped at
            ``MAX_LINES_PER_CALL``.
        encoding: Optional encoding override.
        line_trunc: Max chars per displayed line; longer lines get
            "...[N more]".
        base: Optional sandbox root; if set, ``path`` must resolve
            inside it.

    Returns:
        ``{"ok": True, "content": "<line-numbered text>", "start_line",
        "end_line", "total_lines", "size_bytes", "encoding",
        "truncated": bool, "more": bool}``
    """
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    if not resolved.exists():
        return {"ok": False, "error": f"file not found: {resolved}"}
    if not resolved.is_file():
        return {"ok": False, "error": f"not a regular file: {resolved}"}

    try:
        size_bytes = resolved.stat().st_size
        with resolved.open("rb") as fh:
            data = fh.read()
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}

    enc, bom = _detect_encoding(data, encoding)
    try:
        text = data.decode(enc, errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")

    all_lines = text.splitlines()
    total_lines = len(all_lines)

    off = _coerce_int(offset, 1, minimum=-(total_lines or 1) - 1)
    lim = _coerce_int(limit, DEFAULT_LIMIT_LINES, minimum=1, maximum=MAX_LINES_PER_CALL)
    trunc = _coerce_int(line_trunc, DEFAULT_LINE_TRUNC, minimum=80, maximum=2000)

    if total_lines == 0:
        return {
            "ok": True, "content": "", "content_raw": "", "read_mode": "lines",
            "start_line": 0, "end_line": 0,
            "total_lines": 0, "size_bytes": size_bytes, "encoding": enc,
            "bom": bom, "truncated": False, "more": False, "path": str(resolved),
            "hint": "Empty file.",
        }

    if off < 0:
        # Negative offset: count back from end.
        start_idx = max(0, total_lines + off)
    elif off == 0:
        start_idx = 0
    else:
        start_idx = off - 1
    start_idx = min(start_idx, max(0, total_lines - 1))

    end_idx = min(total_lines, start_idx + lim)
    sliced = all_lines[start_idx:end_idx]

    # Char budget can still bite even with line limit (e.g. minified
    # source one-liner). Truncate aggressively on the per-line side.
    content = _format_line_numbered(sliced, start_idx + 1, line_trunc=trunc)

    char_truncated = False
    if len(content) > MAX_CHARS_PER_CALL:
        content = content[:MAX_CHARS_PER_CALL] + f"\n...[truncated {len(content) - MAX_CHARS_PER_CALL} chars of output; ask with a smaller limit]"
        char_truncated = True

    more = end_idx < total_lines

    content_raw = content_raw_from_numbered(content)

    return {
        "ok": True,
        "content": content,
        "content_raw": content_raw,
        "read_mode": "lines",
        "start_line": start_idx + 1,
        "end_line": end_idx,
        "total_lines": total_lines,
        "size_bytes": size_bytes,
        "encoding": enc,
        "bom": bom,
        "truncated": char_truncated,
        "more": more,
        "path": str(resolved),
        "hint": (
            f"Lines {start_idx + 1}-{end_idx} of {total_lines}. "
            "Copy patch anchors from content_raw. "
            + (f"Next page: offset={end_idx + 1}." if more else "End of file.")
        ),
    }


# ----------------------------------------------------------------------
# stat_text_file — cheap metadata before read/grep/patch
# ----------------------------------------------------------------------


def stat_text_file(path: str, base: str | None = None) -> dict[str, Any]:
    """Return size/line-count/encoding hints before reading or patching."""
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    if not resolved.exists():
        return {"ok": False, "error": f"file not found: {resolved}"}
    if not resolved.is_file():
        return {"ok": False, "error": f"not a regular file: {resolved}"}

    size = resolved.stat().st_size
    line_count: int | None = None
    if size <= 20_000_000:
        try:
            with resolved.open("rb") as fh:
                line_count = sum(1 for _ in fh)
        except Exception:
            line_count = None
    sample_bytes = b""
    try:
        with resolved.open("rb") as fh:
            sample_bytes = fh.read(512)
    except Exception:
        pass
    enc, bom = _detect_encoding(sample_bytes, None)
    large = size > LARGE_TEXT_READ_BYTES
    return {
        "ok": True,
        "path": str(resolved),
        "size_bytes": size,
        "line_count": line_count,
        "encoding_hint": enc,
        "bom": bom,
        "binary_hint": b"\x00" in sample_bytes,
        "large_file": large,
        "hint": (
            "Call grep_text to locate interesting lines, then read_text_file(offset=<line>, limit=...) "
            "for a small window. Use read_mode='chars' or read_text_slice only for minified single-line files."
            if large
            else "Use read_text_file(offset, limit) for line windows; patch_text_file(content_raw) for edits."
        ),
    }


# ----------------------------------------------------------------------
# patch_text_file / write_text_file — exact-edit disk writes
# ----------------------------------------------------------------------


def patch_text_file_resolved(
    resolved: Path,
    old_string: str,
    new_string: str = "",
    *,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Apply exact substring replacement on an already-resolved file path."""
    import hashlib

    needle = str(old_string if old_string is not None else "")
    if not needle:
        return {
            "ok": False,
            "error": "old_string is required",
            "error_code": "patch_old_string_empty",
            "retryable": True,
        }
    replacement = str(new_string if new_string is not None else "")
    if not resolved.exists() or not resolved.is_file():
        return {"ok": False, "error": "file not found", "path": str(resolved)}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}", "path": str(resolved)}

    match_count = text.count(needle)
    if match_count == 0:
        return {
            "ok": False,
            "error": "old_string not found in file",
            "error_code": "patch_old_string_not_found",
            "retryable": True,
            "path": str(resolved),
            "suggested_fix": (
                "Re-read with read_text_file and copy the exact substring from content_raw, "
                "or from read_text_slice when read_mode='chars'. Then retry patch_text_file."
            ),
        }
    if match_count > 1 and not replace_all:
        first_at = text.find(needle)
        return {
            "ok": False,
            "error": f"old_string matched {match_count} times; include more surrounding context or set replace_all=true",
            "error_code": "patch_ambiguous_match",
            "match_count": match_count,
            "first_match_line": _line_number_for_offset(text, first_at),
            "retryable": True,
            "path": str(resolved),
        }
    if replace_all:
        patched = text.replace(needle, replacement)
        replacements = match_count
    else:
        patched = text.replace(needle, replacement, 1)
        replacements = 1
    if len(patched) > MAX_WRITE_CHARS:
        return {
            "ok": False,
            "error": f"patched content too large; max {MAX_WRITE_CHARS} chars",
            "error_code": "patch_result_too_large",
            "path": str(resolved),
        }
    first_at = text.find(needle)
    try:
        resolved.write_text(patched, encoding="utf-8", newline="")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}", "path": str(resolved)}
    digest = hashlib.sha256(patched.encode("utf-8", errors="ignore")).hexdigest()
    return {
        "ok": True,
        "path": str(resolved),
        "replacements": replacements,
        "first_match_line": _line_number_for_offset(text, first_at),
        "size": len(patched.encode("utf-8", errors="ignore")),
        "sha256": digest,
        "hint": (
            "Applied exact substring replacement. Prefer patch_text_file for small edits; "
            "use write_text_file only for new files or full rewrites."
        ),
    }


def patch_text_file(
    path: str,
    old_string: str,
    new_string: str = "",
    *,
    replace_all: bool = False,
    base: str | None = None,
) -> dict[str, Any]:
    """Exact substring patch on a user/system file path (absolute or relative)."""
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    return patch_text_file_resolved(
        resolved,
        old_string,
        new_string,
        replace_all=replace_all,
    )


def write_text_file_resolved(resolved: Path, content: str) -> dict[str, Any]:
    """Create or replace a file at an already-resolved path."""
    import hashlib

    text = str(content if content is not None else "")
    if len(text) > MAX_WRITE_CHARS:
        return {
            "ok": False,
            "error": f"content too large; max {MAX_WRITE_CHARS} chars",
            "error_code": "write_content_too_large",
            "path": str(resolved),
        }
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(text, encoding="utf-8", newline="")
    except Exception as e:
        return {"ok": False, "error": f"write failed: {e}", "path": str(resolved)}
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    return {
        "ok": True,
        "path": str(resolved),
        "size": len(text.encode("utf-8", errors="ignore")),
        "sha256": digest,
        "hint": (
            "Wrote full file content. Prefer patch_text_file for small edits to existing files."
        ),
    }


def write_text_file(path: str, content: str, *, base: str | None = None) -> dict[str, Any]:
    """Create or replace a user/system text file (absolute or relative path)."""
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    return write_text_file_resolved(resolved, content)


# ----------------------------------------------------------------------
# read_text_slice — char-offset reader for minified / single-line files
# ----------------------------------------------------------------------


def read_text_slice(
    path: str,
    offset_chars: Any = 0,
    length: Any = DEFAULT_SLICE_LENGTH,
    encoding: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Read a character-offset slice of a text file.

    This is the "awk for minified files" primitive — when a JS bundle is
    a 4 MB one-liner, ``read_text_file`` is useless because everything
    sits on "line 1". Walk the file in fixed-size character windows
    instead. Use ``grep_text`` with ``mode='context'`` first to find the
    interesting offsets, then call this to fetch the actual code.

    Args:
        path: File to slice into.
        offset_chars: 0-indexed character offset to start at. Negative
            values count from end (``-1`` = last char, ``-1024`` = last
            1 KB).
        length: How many characters to return. Capped at
            ``MAX_SLICE_LENGTH``.
        encoding: Optional override; default is auto-detect.
        base: Optional sandbox root.

    Returns:
        ``{"ok": True, "content": "...", "offset_chars", "end_offset",
        "total_chars", "total_bytes", "more", "encoding", "path"}``.
        The content is NOT line-numbered — that would be misleading when
        the file is one giant line and would also waste bytes.
    """
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    if not resolved.exists() or not resolved.is_file():
        return {"ok": False, "error": f"file not found: {resolved}"}

    try:
        size_bytes = resolved.stat().st_size
        with resolved.open("rb") as fh:
            data = fh.read()
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}

    enc, bom = _detect_encoding(data, encoding)
    try:
        text = data.decode(enc, errors="replace")
    except Exception:
        text = data.decode("utf-8", errors="replace")
    total_chars = len(text)

    off = _coerce_int(offset_chars, 0, minimum=-(total_chars or 1) - 1)
    lim = _coerce_int(length, DEFAULT_SLICE_LENGTH, minimum=1, maximum=MAX_SLICE_LENGTH)

    if total_chars == 0:
        return {
            "ok": True, "content": "", "offset_chars": 0, "end_offset": 0,
            "length_returned": 0, "total_chars": 0, "total_bytes": size_bytes,
            "encoding": enc, "bom": bom, "more": False, "path": str(resolved),
            "hint": "Empty file.",
        }

    if off < 0:
        start = max(0, total_chars + off)
    else:
        start = min(off, total_chars)
    end = min(total_chars, start + lim)
    sliced = text[start:end]
    more = end < total_chars

    return {
        "ok": True,
        "content": sliced,
        "offset_chars": start,
        "end_offset": end,
        "length_returned": len(sliced),
        "total_chars": total_chars,
        "total_bytes": size_bytes,
        "encoding": enc,
        "bom": bom,
        "more": more,
        "path": str(resolved),
        "hint": (
            f"slice chars {start}-{end} of {total_chars}. "
            + (
                f"Call again with offset_chars={end} for the next page."
                if more else "End of file."
            )
        ),
    }


# ----------------------------------------------------------------------
# grep_text_file
# ----------------------------------------------------------------------


def _compile_pattern(pattern: str, case_insensitive: bool, regex: bool) -> "re.Pattern[str]":
    if not regex:
        pattern = re.escape(pattern)
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(pattern, flags)


def grep_text_file(
    pattern: str,
    path: str,
    mode: str = "content",
    max_matches: Any = DEFAULT_GREP_MATCHES,
    line_trunc: Any = MAX_GREP_LINE_LEN,
    context_chars: Any = DEFAULT_GREP_CONTEXT,
    case_insensitive: Any = False,
    regex: Any = True,
    encoding: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Grep a single text file.

    Args:
        pattern: Regex (or literal if ``regex=False``) to search for.
        path: File to grep.
        mode: ``"content"`` (default): return ``path:line:text`` records.
              ``"files_with_matches"``: only confirm whether the file
              contained the pattern. ``"count"``: return total hits.
              ``"context"``: return ``{offset, end, match, before, after}``
              records — line-agnostic, walks past newlines. Use for
              single-line minified files where line numbers are useless.
        max_matches: Cap on hits returned in ``"content"`` mode.
        line_trunc: Per-matched-line truncation.
        context_chars: For ``mode='context'``: how many chars to return
            on each side of the match. Capped at ``MAX_GREP_CONTEXT``.
        case_insensitive: Pass through to regex.
        regex: If False, treat ``pattern`` as a literal substring.
        encoding: Optional override.
        base: Optional sandbox root.
    """
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    if not resolved.exists() or not resolved.is_file():
        return {"ok": False, "error": f"file not found: {resolved}"}

    try:
        pat = _compile_pattern(pattern, bool(case_insensitive), bool(regex))
    except re.error as e:
        return {"ok": False, "error": f"bad regex: {e}"}

    mode = _coerce_str(mode, "content").lower()
    cap = _coerce_int(max_matches, DEFAULT_GREP_MATCHES, minimum=1, maximum=MAX_GREP_MATCHES)
    trunc = _coerce_int(line_trunc, MAX_GREP_LINE_LEN, minimum=40, maximum=2000)

    try:
        with resolved.open("rb") as fh:
            data = fh.read()
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}

    enc, _ = _detect_encoding(data, encoding)
    text = data.decode(enc, errors="replace")

    if mode == "files_with_matches":
        found = bool(pat.search(text))
        return {"ok": True, "matched": found, "path": str(resolved)}
    if mode == "count":
        n = sum(1 for _ in pat.finditer(text))
        return {"ok": True, "count": n, "path": str(resolved)}

    if mode == "context":
        # Character-offset matches with surrounding window — works fine
        # on single-line minified files where ``content`` mode would
        # just give you "line 1, line 1, line 1 ..." with line-trunc
        # eating everything past the first 300 chars.
        ctx = _coerce_int(context_chars, DEFAULT_GREP_CONTEXT, minimum=20, maximum=MAX_GREP_CONTEXT)
        hits: list[dict[str, Any]] = []
        total = 0
        # Hard budget on the response: the model doesn't actually want
        # cap*(2*ctx) bytes back when ctx is 2 KB. Stop early.
        out_budget = MAX_CHARS_PER_CALL
        out_used = 0
        for m in pat.finditer(text):
            total += 1
            if len(hits) >= cap:
                if total >= cap * 4:
                    break
                continue
            start = max(0, m.start() - ctx)
            end = min(len(text), m.end() + ctx)
            before = text[start:m.start()]
            match_text = text[m.start():m.end()]
            after = text[m.end():end]
            # Clip the match itself so a greedy regex matching half the
            # file doesn't single-handedly blow the response budget.
            if len(match_text) > MAX_GREP_LINE_LEN * 4:
                match_text = match_text[:MAX_GREP_LINE_LEN * 4] + f"...[{len(text[m.start():m.end()]) - MAX_GREP_LINE_LEN * 4} more]"
            entry = {
                "offset": m.start(),
                "end": m.end(),
                "before": before,
                "match": match_text,
                "after": after,
                "start_chars_clipped": m.start() - start,
                "end_chars_clipped": end - m.end(),
            }
            est = len(before) + len(match_text) + len(after) + 80
            if out_used + est > out_budget:
                # Soft-stop: keep the count honest but don't add this hit.
                break
            out_used += est
            hits.append(entry)
        return {
            "ok": True,
            "path": str(resolved),
            "mode": "context",
            "context_chars": ctx,
            "matches": hits,
            "match_count_returned": len(hits),
            "match_count_total_or_seen": total,
            "truncated": total > len(hits),
            "hint": (
                f"{len(hits)} context windows shown" + (
                    f" (of at least {total}; tighten the regex)" if total > len(hits) else ""
                ) + ". Each entry has offset/end so you can feed it to read_text_slice for a wider view."
            ),
        }

    # ``content`` mode — line-oriented.
    matches: list[dict[str, Any]] = []
    total = 0
    for i, line in enumerate(text.splitlines(), start=1):
        if pat.search(line):
            total += 1
            if len(matches) < cap:
                clipped = line if len(line) <= trunc else line[:trunc] + f"...[{len(line) - trunc} more]"
                matches.append({"line": i, "text": clipped})
            elif total >= cap * 4:
                # Stop walking — there are way more matches than we'd
                # ever return; the model should narrow its pattern.
                break

    return {
        "ok": True,
        "path": str(resolved),
        "mode": "content",
        "matches": matches,
        "match_count_returned": len(matches),
        "match_count_total_or_seen": total,
        "truncated": total > cap,
        "hint": (
            f"{len(matches)} matches shown" + (
                f" (of at least {total}; narrow the regex)" if total > cap else ""
            )
        ),
    }


# ----------------------------------------------------------------------
# grep_text_dir — recursive, multi-file
# ----------------------------------------------------------------------


def _walk_files(
    root: Path,
    glob: str | None,
    exclude_dirs: Iterable[str],
) -> Iterable[Path]:
    excl = {d.strip("/\\") for d in exclude_dirs if d}
    if glob:
        # Path.rglob does most of the work; pre-filter excluded dirs.
        for p in root.rglob(glob):
            if not p.is_file():
                continue
            if any(part in excl for part in p.parts):
                continue
            yield p
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutating dirnames prunes os.walk in-place.
        dirnames[:] = [d for d in dirnames if d not in excl]
        for fn in filenames:
            yield Path(dirpath) / fn


def grep_text_dir(
    pattern: str,
    root: str,
    glob: str = "",
    mode: str = "content",
    max_matches: Any = DEFAULT_GREP_MATCHES,
    max_files: Any = MAX_FILES_SCANNED,
    line_trunc: Any = MAX_GREP_LINE_LEN,
    case_insensitive: Any = False,
    regex: Any = True,
    exclude_dirs: Any = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Recursive grep across files under ``root``.

    Args:
        pattern: Regex (or literal) — see :func:`grep_text_file`.
        root: Directory to walk.
        glob: Optional filename glob (e.g. ``"*.py"``, ``"**/*.js"``).
            If empty, walk every file under root (skipping excluded
            directories).
        mode: Same as :func:`grep_text_file`. In ``"files_with_matches"``
            mode we return one entry per file with a hit. In
            ``"content"`` mode we return ``path:line:text`` records
            globally capped at ``max_matches``.
        max_matches: Global cap on returned hit records.
        max_files: Safety cap on files scanned.
        line_trunc: Per-matched-line truncation.
        case_insensitive, regex: as in :func:`grep_text_file`.
        exclude_dirs: List of directory basenames to skip (defaults to
            sensible values: ``.git``, ``node_modules``, etc.).
        base: Optional sandbox root.
    """
    if exclude_dirs is None:
        exclude_dirs = [
            ".git", "node_modules", ".venv", "venv", "__pycache__",
            "dist", "build", ".cache", ".cursor", "vendor",
        ]
    elif isinstance(exclude_dirs, str):
        exclude_dirs = [s for s in re.split(r"[,;\s]+", exclude_dirs) if s]

    try:
        root_path = _resolve_path(_coerce_str(root), base)
    except Exception as e:
        return {"ok": False, "error": f"bad root: {e}"}
    if not root_path.exists() or not root_path.is_dir():
        return {"ok": False, "error": f"not a directory: {root_path}"}

    try:
        pat = _compile_pattern(pattern, bool(case_insensitive), bool(regex))
    except re.error as e:
        return {"ok": False, "error": f"bad regex: {e}"}

    mode = _coerce_str(mode, "content").lower()
    cap = _coerce_int(max_matches, DEFAULT_GREP_MATCHES, minimum=1, maximum=MAX_GREP_MATCHES)
    file_cap = _coerce_int(max_files, MAX_FILES_SCANNED, minimum=10, maximum=MAX_FILES_SCANNED)
    trunc = _coerce_int(line_trunc, MAX_GREP_LINE_LEN, minimum=40, maximum=2000)

    glob_str = _coerce_str(glob).strip() or None

    matches: list[dict[str, Any]] = []
    files_seen = 0
    files_with_hits: list[str] = []
    total_seen = 0
    truncated_at_file_cap = False
    truncated_at_match_cap = False

    for p in _walk_files(root_path, glob_str, exclude_dirs):
        files_seen += 1
        if files_seen > file_cap:
            truncated_at_file_cap = True
            break
        # Skip obviously-binary files cheaply.
        try:
            with p.open("rb") as fh:
                head = fh.read(2048)
        except Exception:
            continue
        if b"\x00" in head:
            continue
        try:
            with p.open("rb") as fh:
                data = fh.read()
        except Exception:
            continue
        enc, _ = _detect_encoding(data, None)
        try:
            text = data.decode(enc, errors="replace")
        except Exception:
            continue

        if mode == "files_with_matches":
            if pat.search(text):
                files_with_hits.append(str(p))
                if len(files_with_hits) >= cap:
                    truncated_at_match_cap = True
                    break
            continue

        rel = str(p)
        for i, line in enumerate(text.splitlines(), start=1):
            if pat.search(line):
                total_seen += 1
                if len(matches) < cap:
                    clipped = line if len(line) <= trunc else line[:trunc] + f"...[{len(line) - trunc} more]"
                    matches.append({"path": rel, "line": i, "text": clipped})
                else:
                    truncated_at_match_cap = True
        if truncated_at_match_cap and total_seen > cap * 3:
            # Same logic as single-file: stop early when we're way over.
            break

    if mode == "files_with_matches":
        return {
            "ok": True,
            "mode": "files_with_matches",
            "files": files_with_hits,
            "file_count": len(files_with_hits),
            "files_scanned": files_seen,
            "truncated": truncated_at_match_cap or truncated_at_file_cap,
            "hint": (
                f"{len(files_with_hits)} files matched (scanned {files_seen})."
                + (" Result list was capped; narrow the glob/pattern for the rest." if (truncated_at_match_cap or truncated_at_file_cap) else "")
            ),
        }

    return {
        "ok": True,
        "mode": "content",
        "matches": matches,
        "match_count_returned": len(matches),
        "match_count_total_or_seen": total_seen,
        "files_scanned": files_seen,
        "truncated": truncated_at_match_cap or truncated_at_file_cap,
        "hint": (
            f"{len(matches)} matches across {files_seen} files"
            + (f" (cap hit at {cap}; narrow the pattern or use mode='files_with_matches')" if truncated_at_match_cap else "")
            + (f" (file-scan cap {file_cap} hit; pass max_files= or glob=)" if truncated_at_file_cap else "")
        ),
    }


# ----------------------------------------------------------------------
# extract_strings — `strings(1)` for minified JS / WASM / etc.
# ----------------------------------------------------------------------

# Quoted string literal in JS / Python / most C-likes. The four flavors:
#   "..." / '...' / `...` (template, but only non-substitution) / r"..."
# We deliberately walk character-by-character rather than use a single
# regex because greedy regex over a 4 MB minified file with millions
# of strings is much slower than a state machine.
_QUOTE_CHARS = ('"', "'", "`")

# Token patterns for ``kind`` filtering. Compiled lazily so the module
# stays cheap to import for users that never call ``extract_strings``.
_URL_RE: re.Pattern[str] | None = None
_DATA_URI_RE: re.Pattern[str] | None = None
_JWT_RE: re.Pattern[str] | None = None
_BASE64_RE: re.Pattern[str] | None = None
_HEX_RE: re.Pattern[str] | None = None


def _ensure_token_patterns() -> None:
    global _URL_RE, _DATA_URI_RE, _JWT_RE, _BASE64_RE, _HEX_RE
    if _URL_RE is None:
        _URL_RE = re.compile(r"https?://[^\s'\"`<>()\\]+", re.IGNORECASE)
        _DATA_URI_RE = re.compile(r"data:[a-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)
        # JWT: header.payload.signature, each segment base64url, at
        # least 10 chars each (so we don't false-match every dotted name).
        _JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
        # Generic long base64 / base64url — at least 24 chars, must
        # contain a mix of upper/lower/digits so we don't catch e.g.
        # all-lowercase function names.
        _BASE64_RE = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
        _HEX_RE = re.compile(r"\b(?:0x)?[A-Fa-f0-9]{16,}\b")


def _shannon_entropy(s: str) -> float:
    """Shannon entropy in bits/char — high entropy ≈ random looking.

    English prose ~ 4.0; random base64 ~ 5.5+; AES-128 hex ~ 4.0; ASCII
    boilerplate < 3.5. We use this to filter out chatty UI strings when
    the model wants 'crypto-shaped' candidates.
    """
    if not s:
        return 0.0
    from math import log2
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(s)
    return -sum((c / total) * log2(c / total) for c in counts.values())


def _iter_string_literals(text: str, min_len: int) -> Iterable[tuple[int, int, str, str]]:
    """Yield ``(offset, end, quote_char, body)`` for every quoted string
    literal in ``text`` whose body length >= ``min_len``.

    Handles ``\\``-escapes inside double/single quotes. Template
    literals only yield if they contain no ``${...}`` substitution
    (otherwise the body isn't a literal). We don't try to parse JS —
    just enough to walk past escapes and find the closing quote.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Skip line / block comments — they pollute results badly in
        # readable source. Cheap to do here.
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i + 2)
                i = n if j < 0 else j + 1
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
        if ch not in _QUOTE_CHARS:
            i += 1
            continue
        quote = ch
        start = i
        i += 1
        body_start = i
        body_chars: list[str] = []
        has_substitution = False
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                body_chars.append(text[i + 1])
                i += 2
                continue
            if quote == "`" and c == "$" and i + 1 < n and text[i + 1] == "{":
                has_substitution = True
                # Don't yield template literals containing substitutions;
                # but we still need to scan past them so the loop resumes
                # at the right place.
                depth = 1
                i += 2
                while i < n and depth > 0:
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                    i += 1
                continue
            if c == quote:
                # Closer.
                body = "".join(body_chars) if body_chars else text[body_start:i]
                # Newlines in '...' / "..." are usually illegal but we
                # tolerate them — minified JS doesn't have them and
                # heuristics shouldn't choke.
                if not has_substitution and len(body) >= min_len:
                    yield (start, i + 1, quote, body)
                i += 1
                break
            body_chars.append(c)
            i += 1
        else:
            # Unterminated string — bail out of the file.
            return


def extract_strings(
    path: str,
    min_len: Any = DEFAULT_STRING_MIN_LEN,
    max_results: Any = DEFAULT_STRING_RESULTS,
    kind: str = "any",
    min_entropy: Any = 0.0,
    encoding: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Extract string literals / URLs / tokens from a file.

    This is the ``strings(1)``-style triage tool for huge minified JS or
    obfuscated bundles: hand back the actual *data* sitting in literals
    (URLs, keys, base64 blobs, hex constants) without making the model
    read the full file.

    Args:
        path: File to scan.
        min_len: Minimum literal body length (default 16). Shorter than
            this and you'd get every variable name.
        max_results: Cap on returned strings.
        kind: Filter the result set:

            - ``"any"`` (default): all string literals >= min_len.
            - ``"url"``: only ``http(s)://...`` substrings (literal or
              embedded inside text).
            - ``"data_uri"``: only ``data:...;base64,...`` URIs.
            - ``"jwt"``: candidate JSON Web Tokens.
            - ``"base64"``: long base64-shaped runs (likely keys/payloads).
            - ``"hex"``: long hex blobs (likely keys/IVs/hashes).
            - ``"literal"``: same as ``"any"`` but ONLY literals
              (skip regex-style URL/JWT/base64 extraction).
            - ``"high_entropy"``: literals with entropy >= ``min_entropy``
              or >= 4.5 if not set. Useful for "find me the secret".

        min_entropy: Override entropy floor for ``high_entropy``.
        encoding: Optional override.
        base: Optional sandbox root.

    Returns:
        ``{"ok": True, "items": [{"offset", "end", "kind", "value",
        "entropy", "length"}], "more": bool, "count_seen", "path"}``.
    """
    try:
        resolved = _resolve_path(_coerce_str(path), base)
    except Exception as e:
        return {"ok": False, "error": f"bad path: {e}"}
    if not resolved.exists() or not resolved.is_file():
        return {"ok": False, "error": f"file not found: {resolved}"}

    try:
        with resolved.open("rb") as fh:
            data = fh.read()
    except Exception as e:
        return {"ok": False, "error": f"read failed: {e}"}

    enc, _ = _detect_encoding(data, encoding)
    text = data.decode(enc, errors="replace")

    min_l = _coerce_int(min_len, DEFAULT_STRING_MIN_LEN, minimum=4, maximum=4096)
    cap = _coerce_int(max_results, DEFAULT_STRING_RESULTS, minimum=1, maximum=MAX_STRING_RESULTS)
    kind = _coerce_str(kind, "any").lower()
    ent_floor = 0.0
    try:
        ent_floor = float(min_entropy) if min_entropy is not None else 0.0
    except Exception:
        ent_floor = 0.0
    if kind == "high_entropy" and ent_floor <= 0.0:
        ent_floor = 4.5

    _ensure_token_patterns()
    assert _URL_RE and _DATA_URI_RE and _JWT_RE and _BASE64_RE and _HEX_RE

    items: list[dict[str, Any]] = []
    total_seen = 0

    def _clip(s: str) -> str:
        if len(s) <= MAX_STRING_LEN_RETURNED:
            return s
        return s[:MAX_STRING_LEN_RETURNED] + f"...[{len(s) - MAX_STRING_LEN_RETURNED} more]"

    # Regex-mode extraction first — these run on raw file text and are
    # cheap, so we always try them when the kind matches.
    if kind in ("url", "any"):
        for m in _URL_RE.finditer(text):
            total_seen += 1
            if len(items) < cap:
                items.append({
                    "offset": m.start(), "end": m.end(),
                    "kind": "url", "value": _clip(m.group(0)),
                    "length": len(m.group(0)),
                })
    if kind in ("data_uri", "any"):
        for m in _DATA_URI_RE.finditer(text):
            total_seen += 1
            if len(items) < cap:
                items.append({
                    "offset": m.start(), "end": m.end(),
                    "kind": "data_uri", "value": _clip(m.group(0)),
                    "length": len(m.group(0)),
                })
    if kind in ("jwt", "any"):
        for m in _JWT_RE.finditer(text):
            total_seen += 1
            if len(items) < cap:
                items.append({
                    "offset": m.start(), "end": m.end(),
                    "kind": "jwt", "value": _clip(m.group(0)),
                    "length": len(m.group(0)),
                })
    if kind in ("base64",):
        for m in _BASE64_RE.finditer(text):
            val = m.group(0)
            # Skip obvious false positives — runs that look like a
            # function or variable name (all lower + underscores).
            if val.islower() and "_" in val:
                continue
            ent = _shannon_entropy(val)
            if ent < 3.5:
                continue
            total_seen += 1
            if len(items) < cap:
                items.append({
                    "offset": m.start(), "end": m.end(),
                    "kind": "base64", "value": _clip(val),
                    "length": len(val), "entropy": round(ent, 2),
                })
    if kind in ("hex",):
        for m in _HEX_RE.finditer(text):
            total_seen += 1
            if len(items) < cap:
                items.append({
                    "offset": m.start(), "end": m.end(),
                    "kind": "hex", "value": _clip(m.group(0)),
                    "length": len(m.group(0)),
                })

    # Literal extraction — walks the quote state machine. Skip in
    # the regex-only kinds.
    if kind in ("any", "literal", "high_entropy"):
        for off, end, quote, body in _iter_string_literals(text, min_l):
            ent = _shannon_entropy(body) if (kind == "high_entropy" or ent_floor > 0) else 0.0
            if ent_floor > 0 and ent < ent_floor:
                continue
            total_seen += 1
            if len(items) < cap:
                entry: dict[str, Any] = {
                    "offset": off,
                    "end": end,
                    "kind": "literal",
                    "quote": quote,
                    "value": _clip(body),
                    "length": len(body),
                }
                if ent > 0:
                    entry["entropy"] = round(ent, 2)
                items.append(entry)
            elif total_seen > cap * 4:
                # Way more than we'd return. Bail.
                break

    # Sort by offset so the model sees them in file order — easier to
    # correlate with read_text_slice.
    items.sort(key=lambda it: (it.get("offset") or 0, it.get("end") or 0))

    return {
        "ok": True,
        "path": str(resolved),
        "kind": kind,
        "min_len": min_l,
        "items": items,
        "count_returned": len(items),
        "count_seen": total_seen,
        "truncated": total_seen > len(items),
        "hint": (
            f"returned {len(items)} of {total_seen} candidate(s)"
            + (" — narrow `kind`, raise `min_len`, or set `min_entropy` to trim further." if total_seen > len(items) else ".")
        ),
    }
