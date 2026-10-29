"""Heavy JavaScript deobfuscation + unpacking + advanced source map recovery.

This is the Python side of P2. Each public function in here corresponds
to one entry in ``_tools.py``:

- :func:`deobfuscate_heavy` ← ``js_deobfuscate_heavy``
- :func:`unpack_bundle`     ← ``js_unpack_bundle``
- :func:`recover_indexed_source_map` ← ``source_map_recover`` (now
  handles indexed/sectioned maps as well as the existing flat case)
- :func:`detect_bundler`    ← used by ``unpack_bundle`` and reported
  as a "stack" field on the ReverseJob.

The heavy lifting itself is in ``runtime_manager`` (subprocess to
webcrack / restringer) and in Python regexp heuristics for the
bundlers webcrack does not handle (Vite, Rollup, Parcel, SystemJS,
Module Federation, UMD).

Why don't we lean on webcrack for everything? Because webcrack handles
Webpack and Browserify well, but its support for Vite / Rollup /
Parcel / SystemJS / Module Federation is limited or missing. Our own
unpackers fill the gap with rule-based detection + slicing that
mirrors what an analyst would do by hand (find the module boundary
markers; emit each module as its own artifact).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from argusai.core.reverse import runtime_manager

log = logging.getLogger("argusai.js_heavy")


# ---------------------------------------------------------------------------
# Detection — bundler / packer / obfuscator fingerprints
# ---------------------------------------------------------------------------


# We use multiple regexes per format because real-world bundles often
# minify the boilerplate enough that single-pattern detection misses.

_BUNDLER_PATTERNS: dict[str, list[re.Pattern]] = {
    "webpack5": [
        # W5-unique global. Webpack 4 used `webpackJsonp`, W5 uses
        # `webpackChunk_<app-name>`.
        re.compile(r"webpackChunk[_a-zA-Z0-9$]*"),
        # ESM helper functions injected by webpack 5 runtimes.
        re.compile(r"__webpack_require__\.(?:r|d|e|f|o)\s*[(=]"),
        # W5 renames installedModules to __webpack_modules__.
        re.compile(r"__webpack_modules__"),
    ],
    "webpack4": [
        # webpack4-unique global. Webpack 5 uses webpackChunk_<name>.
        re.compile(r"\bwebpackJsonp\b"),
        # webpack4 ships an installedModules cache (renamed in W5).
        re.compile(r"\binstalledModules\b"),
        # Classic webpack4 entry comment.
        re.compile(r"function\s+__webpack_require__\s*\("),
    ],
    "vite": [
        re.compile(r'<script[^>]*type=["\']module["\'][^>]*src=["\']/assets/index-[a-z0-9]+\.js["\']', re.IGNORECASE),
        re.compile(r"link\s+rel=\s*[\"']modulepreload[\"'][^>]*?/assets/[\w-]+\.js", re.IGNORECASE),
        re.compile(r"\b_vite_legacy_polyfill\b"),
        re.compile(r"\b__vite__mapDeps\b"),
        re.compile(r"\b__vitePreload\b"),
    ],
    "rollup_systemjs": [
        re.compile(r"\bSystem\.register\s*\(\s*\[[^\]]*\]\s*,\s*function\s*\("),
        re.compile(r"\bSystem\.import\s*\("),
    ],
    "rollup_iife": [
        # Rollup IIFE outputs include a banner with module ids; not always
        # diagnostic on its own but combined with no webpack signature
        # and the {} factory pattern it's a strong hint.
        re.compile(r"^!?\(function\s*\([^)]*\)\s*\{\s*[\"']use strict[\"'];", re.MULTILINE),
    ],
    "parcel": [
        # parcelRequire is parcel 1; parcel 2 keeps the same root name
        # but mangles the suffix per-build.
        re.compile(r"\bparcelRequire\s*=\s*function"),
        re.compile(r'\bparcelRequire["a-zA-Z0-9_]*\s*='),
    ],
    "module_federation": [
        re.compile(r"__webpack_require__\.federation"),
        re.compile(r"__webpack_share_scopes__"),
        re.compile(r'remoteEntry\.js'),
        re.compile(r'\b__federation_method_(getRemote|setRemote|ensure|wrapDefault)\b'),
    ],
    "umd": [
        # The boilerplate emitted by every umd-wrapped library.
        re.compile(
            r"\(function\s*\([^)]*?\)\s*\{[\s\S]{0,200}?"
            r"typeof\s+exports\s*===?\s*[\"']object[\"'][\s\S]{0,200}?"
            r"typeof\s+define\s*===?\s*[\"']function[\"']"
        ),
    ],
    "browserify": [
        re.compile(r"function\s+e\(t,n,r\)\s*\{\s*function\s+i\("),
        re.compile(r"\(function\s*\([^)]+\)\s*\{[^}]*\bfunction\s+r\(s,a\)\s*\{"),
    ],
    "closure_advanced": [
        re.compile(r"\bgoog\.exportSymbol\("),
        re.compile(r"\bgoog\.scope\("),
    ],
    "packer": [
        # The classic p.a.c.k.e.r signature.
        re.compile(
            r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[dr]\s*\)"
        ),
    ],
    "obfuscator_io": [
        # obfuscator.io ships a short header comment with its version when
        # debugging artefacts aren't stripped.
        re.compile(r"obfuscator\.io|javascript-obfuscator"),
        # Heavy rotated-string-array shape: the IIFE that rotates the
        # global array using ``while (--x)`` is a strong tell.
        re.compile(r"while\s*\(\s*--[a-zA-Z_$][\w$]*\s*\)"),
        # Heavy use of hex-prefixed identifiers `_0x4321` followed by an
        # array literal of strings — classic obfuscator.io baseline.
        re.compile(r"_0x[0-9a-f]{3,8}\s*=\s*\[\s*[\"'][^\"']+[\"']"),
        # The decoder function it injects: takes a numeric index and
        # returns a string from the rotated array.
        re.compile(r"function\s+_0x[0-9a-f]{3,8}\s*\([^)]*\)\s*\{[^}]*\.shift\(\)"),
    ],
}


def detect_bundler(source: str, *, top_n: int = 4) -> dict[str, Any]:
    """Probe ``source`` for bundler / obfuscator / packer fingerprints.

    Returns
    -------
    ``{primary: str, signals: list[{name, hits, kind}], all_matches: dict}``

    ``primary`` is the highest-scoring family. Ties are broken in this
    priority order (specific > generic):

      module_federation > webpack5 > webpack4 > vite > rollup_systemjs
      > parcel > closure_advanced > obfuscator_io > packer
      > rollup_iife > umd > browserify
    """
    priority = [
        "module_federation", "webpack5", "webpack4", "vite",
        "rollup_systemjs", "parcel", "closure_advanced",
        "obfuscator_io", "packer",
        "rollup_iife", "umd", "browserify",
    ]
    matches: dict[str, int] = {}
    for name, patterns in _BUNDLER_PATTERNS.items():
        n = 0
        for p in patterns:
            n += len(p.findall(source))
        if n > 0:
            matches[name] = n

    signals: list[dict[str, Any]] = []
    for name in priority:
        if name in matches:
            kind = (
                "bundler" if name in {"webpack4", "webpack5", "vite", "rollup_systemjs",
                                      "rollup_iife", "parcel", "browserify", "umd",
                                      "closure_advanced", "module_federation"}
                else "obfuscator"
            )
            signals.append({"name": name, "hits": matches[name], "kind": kind})

    primary = signals[0]["name"] if signals else "unknown"
    return {
        "primary": primary,
        "signals": signals[:top_n],
        "all_matches": matches,
    }


# ---------------------------------------------------------------------------
# Heavy deobfuscation (webcrack + restringer cross-validation)
# ---------------------------------------------------------------------------


def deobfuscate_heavy(
    source: str,
    *,
    api: Any | None = None,
    use_restringer: bool = True,
    options: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Run webcrack on ``source``; optionally cross-validate with restringer.

    Strategy:

      1. Detect what we're looking at (``detect_bundler``).
      2. Run webcrack. This handles webpack/browserify unpack, the
         standard string-array / control-flow / dead-code reverse
         passes, and unminification.
      3. If webcrack reports zero deobfuscation and the source is
         clearly obfuscated (``obfuscator_io`` signals), try restringer
         as a second-chance.
      4. Return the better of the two (or just webcrack's output if
         restringer is unavailable / didn't improve things).

    Returns:
        ``{ok, code, fingerprint, applied: {webcrack, restringer},
           markers, error, elapsed_ms}``
    """
    import time
    t_start = time.time()

    fingerprint = detect_bundler(source)

    wc = runtime_manager.webcrack_run(
        source, api=api, options=options or {}, timeout=timeout,
    )
    if not wc["ok"]:
        # webcrack failure mode: report cleanly. Don't try restringer
        # in this case — if webcrack can't parse the input, restringer
        # is even less likely to.
        return {
            "ok": False,
            "code": "",
            "fingerprint": fingerprint,
            "applied": {"webcrack": False, "restringer": False},
            "markers": [],
            "error": wc.get("error") or "webcrack failed",
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }

    code = wc["code"] or source  # webcrack returns original code unchanged if it bailed.
    applied = {
        "webcrack": bool(wc.get("deobfuscated") or wc.get("unminified") or wc.get("unpacked")),
        "restringer": False,
    }
    markers = list(wc.get("markers") or [])

    # Decide whether restringer is worth running.
    should_restring = (
        use_restringer
        and not applied["webcrack"]   # webcrack didn't think anything happened
        and any(s["name"] == "obfuscator_io" for s in fingerprint["signals"])
    )
    if should_restring:
        rs = runtime_manager.restringer_run(code, api=api, timeout=timeout)
        if rs["ok"] and rs.get("code"):
            # Only adopt restringer's output if it actually changed anything.
            if rs["code"] != code:
                code = rs["code"]
                applied["restringer"] = True

    return {
        "ok": True,
        "code": code,
        "fingerprint": fingerprint,
        "applied": applied,
        "markers": markers,
        "bundle": wc.get("bundle"),
        "error": "",
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


# ---------------------------------------------------------------------------
# Bundle unpacking — multi-format
# ---------------------------------------------------------------------------


def unpack_bundle(
    source: str,
    *,
    api: Any | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Detect bundler format and split the bundle into individual modules.

    For formats webcrack supports natively (webpack 4/5, browserify) we
    delegate to webcrack and use its ``result.bundle`` output. For the
    rest (vite, rollup, parcel, systemjs, umd, module federation) we use
    rule-based slicing.

    Returns:
        ``{ok, bundler, modules: [{id, path, size, code, is_entry}],
           count, error, elapsed_ms}``

    For very large bundles we cap at 2000 modules to stop runaway memory
    use on accidentally-packed-twice inputs.
    """
    import time
    t_start = time.time()

    fingerprint = detect_bundler(source)
    primary = fingerprint["primary"]

    if primary in {"webpack4", "webpack5", "browserify"}:
        return _unpack_via_webcrack(source, fingerprint, api, timeout, t_start)

    if primary == "vite":
        return _unpack_vite_inline(source, fingerprint, t_start)
    if primary in {"rollup_systemjs"}:
        return _unpack_rollup_systemjs(source, fingerprint, t_start)
    if primary == "rollup_iife":
        return _unpack_rollup_iife(source, fingerprint, t_start)
    if primary == "parcel":
        return _unpack_parcel(source, fingerprint, t_start)
    if primary == "umd":
        return _unpack_umd(source, fingerprint, t_start)
    if primary == "module_federation":
        # MF needs network resolution; the dedicated tool handles it.
        return {
            "ok": False,
            "bundler": "module_federation",
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": (
                "Module Federation bundles reference remote chunks. "
                "Use js_unpack_module_federation to fetch and unpack remotes."
            ),
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }
    if primary == "closure_advanced":
        return {
            "ok": False,
            "bundler": "closure_advanced",
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": (
                "Closure Compiler ADVANCED mode does not produce splittable "
                "modules — names are globally minified and inlined. "
                "Run js_deobfuscate_heavy to unminify and then analyse statically."
            ),
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }

    # Unknown / IIFE — try webcrack anyway as a "maybe it's actually
    # webpack but our heuristic failed" last-ditch.
    return _unpack_via_webcrack(source, fingerprint, api, timeout, t_start, fallback=True)


def _unpack_via_webcrack(
    source: str,
    fingerprint: dict[str, Any],
    api: Any | None,
    timeout: int,
    t_start: float,
    *,
    fallback: bool = False,
) -> dict[str, Any]:
    import time
    wc = runtime_manager.webcrack_run(source, api=api, timeout=timeout, options={})
    if not wc["ok"]:
        return {
            "ok": False,
            "bundler": fingerprint["primary"],
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": wc.get("error") or "webcrack failed",
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }
    bundle = wc.get("bundle")
    if not bundle or not bundle.get("modules"):
        if fallback:
            return {
                "ok": False,
                "bundler": "unknown",
                "modules": [],
                "count": 0,
                "fingerprint": fingerprint,
                "error": "webcrack did not detect any bundle; format not recognised.",
                "elapsed_ms": int((time.time() - t_start) * 1000),
            }
        return {
            "ok": False,
            "bundler": fingerprint["primary"],
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": "webcrack reported no bundle structure",
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }
    # webcrack's summary doesn't include the actual code per module
    # (we'd need to call result.bundle.modules.get(id).code from inside
    # Node). For now we record the index and leave a note. In the next
    # iteration we'll either teach the runner to dump full module
    # source on demand, or split the bundle ourselves using the offsets.
    modules = []
    for m in bundle.get("modules") or []:
        modules.append({
            "id": str(m.get("id") or ""),
            "path": m.get("path") or "",
            "size": int(m.get("size") or 0),
            "is_entry": bool(m.get("is_entry")),
            "code": "",  # placeholder — see note above
        })
    return {
        "ok": True,
        "bundler": fingerprint["primary"],
        "modules": modules,
        "count": len(modules),
        "fingerprint": fingerprint,
        "error": "",
        "note": (
            "Module bodies are NOT yet inlined in the response. Use "
            "webpack_module_read for individual modules or wait for the "
            "next runner iteration that dumps all bodies."
        ),
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


# Vite-specific extraction. Vite shipped builds use ES modules; the
# entry HTML imports ``./assets/index-<hash>.js``. The index module
# itself contains a static manifest at the top:
#
#   const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f={})),f=...)=>...
#
# We pull out the manifest and the dynamic-import URLs.

_VITE_DYNAMIC_IMPORT_RE = re.compile(r'import\(\s*["\']([^"\']+\.js)["\']\s*\)')
_VITE_PRELOAD_RE = re.compile(r'_vite__mapDeps\(\d+\)|__vitePreload\(')


def _unpack_vite_inline(source: str, fingerprint: dict[str, Any], t_start: float) -> dict[str, Any]:
    """Vite inline JS — extract its module graph hints."""
    import time
    dynamic = list({m for m in _VITE_DYNAMIC_IMPORT_RE.findall(source)})
    preload_count = len(_VITE_PRELOAD_RE.findall(source))

    modules: list[dict[str, Any]] = []
    # The entry source itself counts as a single module.
    modules.append({
        "id": "entry",
        "path": "(entry)",
        "size": len(source),
        "is_entry": True,
        "code": source[:32_000],  # cap inline code; the artifact has full source
        "truncated": len(source) > 32_000,
    })
    for i, ref in enumerate(dynamic[:200], 1):
        modules.append({
            "id": f"chunk-{i}",
            "path": ref,
            "size": 0,
            "is_entry": False,
            "code": "",
            "note": "dynamic import — fetch this URL with browser_probe / http_curl",
        })

    return {
        "ok": True,
        "bundler": "vite",
        "modules": modules,
        "count": len(modules),
        "fingerprint": fingerprint,
        "error": "",
        "note": (
            f"Vite entry recorded with {len(dynamic)} dynamic chunk URLs to fetch. "
            f"Preload helper invocations: {preload_count}."
        ),
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


# Rollup SystemJS: System.register(deps, factory) per module.
_SYSTEM_REGISTER_RE = re.compile(
    r"System\.register\s*\(\s*(\[[^\]]*\])\s*,\s*function\s*\(([^)]*)\)\s*\{",
    re.DOTALL,
)


def _unpack_rollup_systemjs(source: str, fingerprint: dict[str, Any], t_start: float) -> dict[str, Any]:
    """Rollup SystemJS module table — slice on every System.register call."""
    import time
    modules: list[dict[str, Any]] = []
    matches = list(_SYSTEM_REGISTER_RE.finditer(source))
    if not matches:
        return {
            "ok": False,
            "bundler": "rollup_systemjs",
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": "no System.register calls found",
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
        body = source[start:end]
        try:
            deps_raw = m.group(1)
            deps = json.loads(deps_raw) if deps_raw.strip().startswith("[") else []
        except Exception:
            deps = []
        modules.append({
            "id": f"system-{i}",
            "path": deps[0] if deps and isinstance(deps[0], str) else f"module-{i}",
            "size": len(body),
            "is_entry": i == 0,
            "code": body[:32_000],
            "truncated": len(body) > 32_000,
            "deps": deps,
        })
    return {
        "ok": True,
        "bundler": "rollup_systemjs",
        "modules": modules,
        "count": len(modules),
        "fingerprint": fingerprint,
        "error": "",
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


def _unpack_rollup_iife(source: str, fingerprint: dict[str, Any], t_start: float) -> dict[str, Any]:
    """Rollup IIFE — no module boundaries inside the IIFE; just unwrap it."""
    import time
    # Strip the outer ``(function (exports) { ... })(typeof window..)`` wrapper.
    m = re.match(r"^\s*!?\(function\s*\([^)]*\)\s*\{\s*[\"']use strict[\"'];?", source)
    body_start = m.end() if m else 0
    body = source[body_start:]
    return {
        "ok": True,
        "bundler": "rollup_iife",
        "modules": [{
            "id": "iife-body",
            "path": "(iife body)",
            "size": len(body),
            "is_entry": True,
            "code": body[:64_000],
            "truncated": len(body) > 64_000,
        }],
        "count": 1,
        "fingerprint": fingerprint,
        "error": "",
        "note": "Rollup IIFE has a single output module; no further splitting possible.",
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


# Parcel: every module is a key in the parcelRequire factory map.
# Parcel 1: parcelRequire={"id":[function(...){...}, {...}], ...}
# Parcel 2: similar, but hashed keys (e.g. "8x4Lh") and slightly different runtime.
_PARCEL_MODULE_RE = re.compile(
    r"[\"']([a-zA-Z0-9_$\-]{1,32})[\"']\s*:\s*\[\s*function\s*\(",
)


def _unpack_parcel(source: str, fingerprint: dict[str, Any], t_start: float) -> dict[str, Any]:
    import time
    matches = list(_PARCEL_MODULE_RE.finditer(source))
    if not matches:
        return {
            "ok": False,
            "bundler": "parcel",
            "modules": [],
            "count": 0,
            "fingerprint": fingerprint,
            "error": "parcel signature detected but no module IDs found",
            "elapsed_ms": int((time.time() - t_start) * 1000),
        }
    modules: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        mod_id = m.group(1)
        start = m.start()
        # Find matching end by brace counting up from the inner `function` body.
        end = _find_module_end(source, m.end())
        body = source[start:end]
        modules.append({
            "id": mod_id,
            "path": f"parcel:{mod_id}",
            "size": len(body),
            "is_entry": i == 0,
            "code": body[:32_000],
            "truncated": len(body) > 32_000,
        })
        if len(modules) >= 2000:
            break
    return {
        "ok": True,
        "bundler": "parcel",
        "modules": modules,
        "count": len(modules),
        "fingerprint": fingerprint,
        "error": "",
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


def _unpack_umd(source: str, fingerprint: dict[str, Any], t_start: float) -> dict[str, Any]:
    """UMD wrapper — single module after stripping the outer factory call."""
    import time
    # Find the start of the inner factory call's body: ``factory(this.foo = {})``
    # The pattern we look for is the ``})(this, function (exports) { ... });``
    # tail that closes the UMD wrapper and reveals the inner body.
    inner = source
    # Drop everything up through ``function (\s*\w*\s*) {`` that follows the
    # ``define / typeof exports`` boilerplate.
    m = re.search(r"function\s*\([^)]*\)\s*\{", source)
    if m:
        # Try to find the matching closing brace for the factory body.
        body_start = m.end()
        body_end = _find_balanced_brace(source, body_start - 1)
        if body_end > body_start:
            inner = source[body_start:body_end]
    return {
        "ok": True,
        "bundler": "umd",
        "modules": [{
            "id": "umd-body",
            "path": "(umd factory body)",
            "size": len(inner),
            "is_entry": True,
            "code": inner[:64_000],
            "truncated": len(inner) > 64_000,
        }],
        "count": 1,
        "fingerprint": fingerprint,
        "error": "",
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


def _find_module_end(source: str, start_after_function_paren: int) -> int:
    """Given a position right after ``function (`` in a module factory,
    find the index of the closing ``)`` of the surrounding ``[function() {...}, {...}]``
    pair. This is approximate — heavy nesting may overshoot.
    """
    # Step 1: skip past the function-argument list.
    p = start_after_function_paren
    depth = 1
    while p < len(source) and depth > 0:
        c = source[p]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        p += 1
    # Now at the start of the function body's `{`.
    while p < len(source) and source[p] != "{":
        p += 1
    if p >= len(source):
        return min(start_after_function_paren + 4000, len(source))
    body_end = _find_balanced_brace(source, p)
    if body_end <= p:
        return min(p + 4000, len(source))
    # Then advance through ``,`` and a second ``{...}`` (the deps map).
    q = body_end + 1
    while q < len(source) and source[q] in " \t\n,":
        q += 1
    if q < len(source) and source[q] == "{":
        deps_end = _find_balanced_brace(source, q)
        q = deps_end + 1 if deps_end > q else q + 1
    # Final closing ``]`` for the `[function, {deps}]` array.
    while q < len(source) and source[q] != "]":
        q += 1
    return q + 1 if q < len(source) else len(source)


def _find_balanced_brace(source: str, opening_idx: int) -> int:
    """Return the index of the brace that matches ``source[opening_idx]``."""
    if opening_idx >= len(source) or source[opening_idx] != "{":
        return opening_idx
    depth = 0
    i = opening_idx
    in_str: str | None = None
    while i < len(source):
        c = source[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        else:
            if c in '"\'`':
                in_str = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(source) - 1


# ---------------------------------------------------------------------------
# Indexed / sectioned source maps (V3 with ``sections``) + ignoreList
# ---------------------------------------------------------------------------


def recover_indexed_source_map(map_text: str) -> dict[str, Any]:
    """Recover an entire V3 source map, supporting both flat and indexed/sectioned formats.

    This replaces the trivial "read top-level sources/sourcesContent"
    logic in ``js_reverse_tools.recover_sourcemap`` for the indexed
    case, while remaining a strict superset of the flat case.

    For a **flat** map (no ``sections`` key) we recover:

      - version, file, sourceRoot
      - sources, sourcesContent, names, mappings (lengths only)
      - x_google_ignoreList — the list of "framework/vendor" sources to
        skip when displaying original code.

    For an **indexed** map (``sections`` key) we recursively walk each
    section's embedded ``map`` and concatenate the recovered originals,
    tracking the byte offset each section starts at so the user knows
    which originals correspond to which region of generated output.

    Returns:
        ``{ok, kind ("flat" | "indexed"), sources: [{name, content,
           offset, ignored, in_section}], section_count, x_google_ignoreList,
           error}``
    """
    try:
        data = json.loads(map_text)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "kind": "unknown",
            "sources": [],
            "section_count": 0,
            "x_google_ignoreList": [],
            "error": f"map is not valid JSON: {e}",
        }

    if not isinstance(data, dict):
        return {
            "ok": False, "kind": "unknown", "sources": [], "section_count": 0,
            "x_google_ignoreList": [], "error": "top-level is not an object",
        }

    if "sections" in data and isinstance(data["sections"], list):
        return _recover_indexed(data)
    return _recover_flat(data)


def _recover_flat(data: dict[str, Any]) -> dict[str, Any]:
    sources_list = list(data.get("sources") or [])
    contents_list = list(data.get("sourcesContent") or [])
    ignored = set()
    for idx in data.get("x_google_ignoreList") or []:
        try:
            ignored.add(int(idx))
        except (TypeError, ValueError):
            continue
    source_root = str(data.get("sourceRoot") or "")
    files: list[dict[str, Any]] = []
    for i, name in enumerate(sources_list):
        content = contents_list[i] if i < len(contents_list) else None
        files.append({
            "name": (source_root + str(name)) if source_root else str(name),
            "content": content if isinstance(content, str) else None,
            "offset": None,
            "ignored": i in ignored,
            "in_section": None,
            "has_content": isinstance(content, str),
            "size": len(content) if isinstance(content, str) else 0,
        })
    return {
        "ok": True,
        "kind": "flat",
        "version": data.get("version"),
        "file": str(data.get("file") or ""),
        "sources": files,
        "section_count": 0,
        "x_google_ignoreList": sorted(ignored),
        "error": "",
    }


def _recover_indexed(data: dict[str, Any]) -> dict[str, Any]:
    sections = data.get("sections") or []
    files: list[dict[str, Any]] = []
    section_summaries: list[dict[str, Any]] = []
    aggregate_ignored: list[int] = []
    for sec_idx, sec in enumerate(sections):
        if not isinstance(sec, dict):
            continue
        offset = sec.get("offset") or {}
        line = int(offset.get("line") or 0)
        col = int(offset.get("column") or 0)
        sub_map = sec.get("map")
        if isinstance(sec.get("url"), str) and sub_map is None:
            # External (linked) sub-map — we cannot fetch it from here.
            section_summaries.append({
                "index": sec_idx,
                "offset_line": line,
                "offset_column": col,
                "external": True,
                "url": sec["url"],
                "file_count": 0,
            })
            continue
        if not isinstance(sub_map, dict):
            continue
        sub = _recover_flat(sub_map)
        for f in sub["sources"]:
            f["offset"] = {"line": line, "column": col}
            f["in_section"] = sec_idx
            files.append(f)
        section_summaries.append({
            "index": sec_idx,
            "offset_line": line,
            "offset_column": col,
            "external": False,
            "file_count": len(sub["sources"]),
        })
        aggregate_ignored.extend(sub.get("x_google_ignoreList") or [])
    return {
        "ok": True,
        "kind": "indexed",
        "version": data.get("version"),
        "file": str(data.get("file") or ""),
        "sources": files,
        "section_count": len(section_summaries),
        "sections": section_summaries,
        "x_google_ignoreList": aggregate_ignored,
        "error": "",
    }


def decode_data_uri_map(uri: str) -> str:
    """Decode a ``data:application/json;base64,...`` source-map URI.

    Inline maps embedded with ``sourceMappingURL=data:...`` are extremely
    common in dev / "tree-shaken but with maps" production builds and
    are not handled by ``source_map_discover`` until we plug this in.
    Returns the decoded JSON text or raises ``ValueError``.
    """
    m = re.match(r"^data:application/json(?:;charset=[^;]+)?;base64,(.+)$", uri.strip())
    if not m:
        raise ValueError("not a base64 data URI for application/json")
    try:
        return base64.b64decode(m.group(1)).decode("utf-8")
    except Exception as e:
        raise ValueError(f"failed to decode data URI: {e}") from e
