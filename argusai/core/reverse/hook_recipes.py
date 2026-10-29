"""Hook recipes — pre-baked hook specs for the most common scenarios.

A *recipe* is a small named bundle of one or more hook installs that
the user (or AI) can drop onto a page with one click. Each entry
declares the target, kind, optional replacement source, a default
``tag``, and a friendly description. The UI lists these for the user;
the AI can pick them by id when answering a fingerprint / sign /
WebSocket question.

Coverage matrix mapping (local JS-reverse plan docs when present):

* canvas_fingerprint   →  A2
* audio_fingerprint    →  A3
* webdriver            →  A1 (already neutralised by stealth, but useful for diagnostics)
* mouse_trajectory     →  A4
* signed_header        →  A8
* signed_cookie        →  A7
* eval_harvest         →  O10
* function_constructor →  O10
* location_host_getter →  O9
* xhr_send             →  T1
* fetch                →  T1
* websocket            →  T1, V4
* eventsource          →  T4
* webassembly_compile  →  W5
"""

from __future__ import annotations

from typing import Any


# JS snippet for WebAssembly.compile / instantiate hooks. Runs AFTER the
# original call returns so we don't mutate the page's view of things.
# Stashes a *copy* of the bytes (the original ArrayBuffer might be
# transferred or detached after the call) plus light metadata into
# ``globalThis.__argusai_wasm_samples``. ``browser_save_wasm`` reads
# from there.
_WASM_HARVEST_SIDE_EFFECT = r"""
try {
  var stash = (globalThis.__argusai_wasm_samples = globalThis.__argusai_wasm_samples || []);
  var meta  = (globalThis.__argusai_wasm_meta    = globalThis.__argusai_wasm_meta    || []);
  var src = args[0];
  var u8 = null;
  if (src && src.constructor && src.constructor.name === 'Uint8Array') {
    // Copy because the caller may reuse the buffer.
    u8 = new Uint8Array(src.byteLength);
    u8.set(src);
  } else if (src instanceof ArrayBuffer) {
    u8 = new Uint8Array(src.byteLength);
    u8.set(new Uint8Array(src));
  } else if (src && src.buffer instanceof ArrayBuffer) {
    var view = new Uint8Array(src.buffer, src.byteOffset || 0, src.byteLength || src.buffer.byteLength);
    u8 = new Uint8Array(view.byteLength);
    u8.set(view);
  }
  if (u8 && u8.byteLength >= 8) {
    // Quick magic sanity check before stashing.
    var ok = u8[0] === 0x00 && u8[1] === 0x61 && u8[2] === 0x73 && u8[3] === 0x6d;
    stash.push(u8);
    meta.push({
      ok_magic: ok,
      size: u8.byteLength,
      captured_at: Date.now(),
      tag: 'compile/instantiate',
    });
    // Keep the stash bounded — only the most recent 8 modules. Without
    // this a busy SPA could leak memory by harvesting forever.
    while (stash.length > 8) { stash.shift(); meta.shift(); }
  }
} catch (_e) { /* swallow */ }
"""

# Streaming variants: the first arg is a Response (or a Promise for one).
# We tee the response body so the original instantiate still gets its
# bytes, and stash a fresh Uint8Array for ourselves. Best-effort: if
# tee/clone aren't available we fall through silently.
_WASM_STREAMING_SIDE_EFFECT = r"""
try {
  var stash = (globalThis.__argusai_wasm_samples = globalThis.__argusai_wasm_samples || []);
  var meta  = (globalThis.__argusai_wasm_meta    = globalThis.__argusai_wasm_meta    || []);
  var first = args[0];
  Promise.resolve(first).then(function (resp) {
    if (!resp || typeof resp.clone !== 'function') return;
    try {
      resp.clone().arrayBuffer().then(function (buf) {
        try {
          var u8 = new Uint8Array(buf.byteLength);
          u8.set(new Uint8Array(buf));
          if (u8.byteLength >= 8) {
            var ok = u8[0] === 0x00 && u8[1] === 0x61 && u8[2] === 0x73 && u8[3] === 0x6d;
            stash.push(u8);
            meta.push({
              ok_magic: ok,
              size: u8.byteLength,
              captured_at: Date.now(),
              tag: 'streaming',
              url: (resp.url || ''),
            });
            while (stash.length > 8) { stash.shift(); meta.shift(); }
          }
        } catch (_e) { /* swallow */ }
      });
    } catch (_e) { /* swallow */ }
  });
} catch (_e) { /* swallow */ }
"""


_RECIPES: dict[str, dict[str, Any]] = {
    # -------- Anti-fingerprint observation/spoofing -----------------
    "canvas_fingerprint": {
        "title": "Canvas fingerprint (A2)",
        "description": "Observe (and optionally neutralise) canvas-based fingerprint reads via HTMLCanvasElement.prototype.toDataURL / getImageData. Hits show the call stack so you can locate the fingerprinting code.",
        "coverage": ["A2"],
        "hooks": [
            {
                "kind": "function",
                "target": "HTMLCanvasElement.prototype.toDataURL",
                "tag": "canvas-toDataURL",
            },
            {
                "kind": "function",
                "target": "CanvasRenderingContext2D.prototype.getImageData",
                "tag": "canvas-getImageData",
            },
        ],
    },
    "canvas_fingerprint_spoof": {
        "title": "Canvas fingerprint — spoof (A2)",
        "description": "Same as canvas_fingerprint but returns a constant value so the fingerprint is neutralised. Useful for testing whether your hook is what triggers downstream behaviour.",
        "coverage": ["A2"],
        "hooks": [
            {
                "kind": "function",
                "target": "HTMLCanvasElement.prototype.toDataURL",
                "tag": "canvas-toDataURL-spoof",
                "replace_return_src": "return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';",
            },
        ],
    },
    "audio_fingerprint": {
        "title": "Audio fingerprint (A3)",
        "description": "Hook AudioContext / OfflineAudioContext channel data extraction. Watch which sites read the audio fingerprint.",
        "coverage": ["A3"],
        "hooks": [
            {
                "kind": "function",
                "target": "AudioBuffer.prototype.getChannelData",
                "tag": "audio-getChannelData",
            },
            {
                "kind": "function",
                "target": "OfflineAudioContext.prototype.startRendering",
                "tag": "audio-offline-render",
            },
        ],
    },
    "webdriver": {
        "title": "navigator.webdriver (A1)",
        "description": "Spoof navigator.webdriver to false and log every read. Our stealth profile already does this but this lets you diagnose stale checks.",
        "coverage": ["A1"],
        "hooks": [
            {
                "kind": "property",
                "target": "navigator.webdriver",
                "tag": "navigator-webdriver",
                "replace_get_src": "return false;",
            },
        ],
    },
    "mouse_trajectory": {
        "title": "Mouse trajectory capture (A4)",
        "description": "Record every mousemove/keydown/click that page handlers attach via addEventListener. Use the captured traces as a fixture when reproducing behavioural fingerprints.",
        "coverage": ["A4"],
        "hooks": [
            {
                "kind": "function",
                "target": "EventTarget.prototype.addEventListener",
                "tag": "event-add",
                "filter_src": "var t = args[0]; return t === 'mousemove' || t === 'keydown' || t === 'click' || t === 'pointerdown';",
            },
        ],
    },
    # -------- Signing / transport -----------------------------------
    "signed_header": {
        "title": "Signed XHR / fetch header (A8)",
        "description": "Capture every XMLHttpRequest.setRequestHeader and fetch() init header set. Reveals which header(s) the page signs and when.",
        "coverage": ["A8"],
        "hooks": [
            {
                "kind": "function",
                "target": "XMLHttpRequest.prototype.setRequestHeader",
                "tag": "xhr-set-header",
            },
            {
                "kind": "function",
                "target": "fetch",
                "tag": "fetch-call",
            },
            {
                "kind": "function",
                "target": "Request",
                "tag": "request-ctor",
            },
        ],
    },
    "signed_cookie": {
        "title": "Signed cookie set (A7)",
        "description": "Hook document.cookie setter. Reveals when the page writes a fresh signed cookie (e.g. after JS challenge solves).",
        "coverage": ["A7"],
        "hooks": [
            {
                "kind": "property",
                "target": "document.cookie",
                "tag": "document-cookie-set",
            },
        ],
    },
    # -------- Runtime code generation -------------------------------
    "eval_harvest": {
        "title": "eval() / Function() runtime code harvest (O10)",
        "description": "Record every eval() and new Function(...) call. Use to capture self-modifying code that only appears at runtime. The hits' first argument IS the runtime source.",
        "coverage": ["O10"],
        "hooks": [
            {
                "kind": "function",
                "target": "eval",
                "tag": "eval-call",
            },
            {
                "kind": "function",
                "target": "Function",
                "tag": "function-ctor",
            },
        ],
    },
    "location_host_getter": {
        "title": "location.host getter (O9)",
        "description": "Log every read of location.host (and the call stack). Helpful when a domain-locked script corrupts itself if it doesn't recognise the host.",
        "coverage": ["O9"],
        "hooks": [
            {
                "kind": "property",
                "target": "location.host",
                "tag": "location-host",
            },
            {
                "kind": "property",
                "target": "location.hostname",
                "tag": "location-hostname",
            },
        ],
    },
    # -------- Real-time transport -----------------------------------
    "websocket": {
        "title": "WebSocket send/receive (T1, V4)",
        "description": "Hook WebSocket.prototype.send and the onmessage prototype. Captures both directions so you can rebuild the framing.",
        "coverage": ["T1", "V4"],
        "hooks": [
            {
                "kind": "function",
                "target": "WebSocket.prototype.send",
                "tag": "ws-send",
            },
            {
                "kind": "function",
                "target": "WebSocket.prototype.addEventListener",
                "tag": "ws-addEvent",
                "filter_src": "return args[0] === 'message';",
            },
        ],
    },
    "fetch": {
        "title": "fetch() request inspection (T1)",
        "description": "Capture every fetch() call (URL, init, body). Lighter than browser_trace_form when you just want a per-request log.",
        "coverage": ["T1"],
        "hooks": [
            {
                "kind": "function",
                "target": "fetch",
                "tag": "fetch-trace",
            },
        ],
    },
    "eventsource": {
        "title": "EventSource (SSE) inspection (T4)",
        "description": "Hook EventSource.prototype.addEventListener to record server-sent event subscriptions.",
        "coverage": ["T4"],
        "hooks": [
            {
                "kind": "function",
                "target": "EventSource.prototype.addEventListener",
                "tag": "sse-addEvent",
            },
        ],
    },
    # -------- WebAssembly -------------------------------------------
    "webassembly_compile": {
        "title": "WebAssembly.compile bytes harvest (W5)",
        "description": (
            "Hook WebAssembly.compile / instantiate and stash a copy of the "
            ".wasm bytes into ``globalThis.__argusai_wasm_samples`` so "
            "``browser_save_wasm`` can pull them out to disk for "
            "wasm_disassemble / wasm_decompile / wasm_call_graph. Streaming "
            "variants (compileStreaming / instantiateStreaming) are also "
            "covered. The recipe records source URL, size, and sha256 "
            "metadata in the hit but the actual bytes go to the stash, NOT "
            "into the hit JSON (binary in JSON would blow up the chat "
            "context — see the August 2026 retro)."
        ),
        "coverage": ["W5"],
        "hooks": [
            {
                "kind": "function",
                "target": "WebAssembly.compile",
                "tag": "wasm-compile",
                "side_effect_src": _WASM_HARVEST_SIDE_EFFECT,
            },
            {
                "kind": "function",
                "target": "WebAssembly.instantiate",
                "tag": "wasm-instantiate",
                "side_effect_src": _WASM_HARVEST_SIDE_EFFECT,
            },
            {
                "kind": "function",
                "target": "WebAssembly.compileStreaming",
                "tag": "wasm-compile-streaming",
                "side_effect_src": _WASM_STREAMING_SIDE_EFFECT,
            },
            {
                "kind": "function",
                "target": "WebAssembly.instantiateStreaming",
                "tag": "wasm-instantiate-streaming",
                "side_effect_src": _WASM_STREAMING_SIDE_EFFECT,
            },
        ],
    },
    # -------- VM / dispatcher trace placeholder ---------------------
    "vm_dispatcher_trace": {
        "title": "VM dispatcher trace (V1–V3) — placeholder",
        "description": "Hook a specific JSVMP dispatcher function and log every (pc, op) tuple. Edit `target` to your dispatcher's path. Filled in fully by P4's js_vm_trace.",
        "coverage": ["V1", "V2", "V3"],
        "hooks": [
            {
                "kind": "function",
                "target": "REPLACE_ME.dispatcher",
                "tag": "vm-dispatcher",
            },
        ],
    },
}


def list_recipes() -> list[dict[str, Any]]:
    """Public catalogue surface for the UI."""
    out: list[dict[str, Any]] = []
    for rid, body in _RECIPES.items():
        out.append({
            "id": rid,
            "title": body["title"],
            "description": body["description"],
            "coverage": body.get("coverage") or [],
            "hooks": body["hooks"],
        })
    return out


def get_recipe(recipe_id: str) -> dict[str, Any] | None:
    return _RECIPES.get(recipe_id)
