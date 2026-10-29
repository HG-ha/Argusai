"""In-page JavaScript runtime injected by the hook system.

A single self-contained string. ``HOOK_RUNTIME_JS`` is added via
``context.add_init_script`` *before* any page script runs, so the global
``__argusai_hook_bridge`` exists by the time we install individual hooks.

Design constraints:

1. Self-defending. The page may later redefine ``JSON.stringify``,
   ``Error``, ``Reflect.*``, etc. We capture references during init and
   stash them in a closure that the page can't reach.
2. No external dependencies. No imports, no fetches, no eval. Just a
   single anonymous IIFE.
3. Bounded memory. Each hook has a ring buffer (default 5,000 hits);
   when it fills we increment a ``dropped`` counter rather than allocate.
4. Replace-mode is opt-in. By default hooks only observe; for
   anti-fingerprint scenarios callers pass ``{replaceReturn: ...}`` or
   ``{replaceArgs: ...}``.
5. Serialisation is bounded. Strings >2KB are truncated; depth cap 6;
   cyclic refs annotated. Functions become ``"[function: name]"``.

Tools called from Python:

* ``__argusai_hook_bridge.install(spec)`` — install one hook
* ``__argusai_hook_bridge.uninstall(id)`` — remove
* ``__argusai_hook_bridge.list()`` — enumerate active
* ``__argusai_hook_bridge.drain(id, sinceTs)`` — pull buffered hits
* ``__argusai_hook_bridge.applyPlan(plan)`` — bulk install for boot

``install`` is idempotent on ``id``: reinstalling overwrites the spec.
"""

from __future__ import annotations


HOOK_RUNTIME_JS = r"""
(function () {
  if (typeof window === 'undefined') return;
  if (window.__argusai_hook_bridge) return;

  // ---- Captured natives. The page may later redefine these; we
  //      stash the originals so we keep working.
  var _Object = Object;
  var _defineProperty = _Object.defineProperty;
  var _getOwnPropertyDescriptor = _Object.getOwnPropertyDescriptor;
  var _getPrototypeOf = _Object.getPrototypeOf;
  var _keys = _Object.keys;
  var _hasOwn = _Object.prototype.hasOwnProperty;
  function _has(obj, key) { return _hasOwn.call(obj, key); }
  var _ReflectApply = (typeof Reflect !== 'undefined' && Reflect.apply) || Function.prototype.apply.bind(Function.prototype.apply);
  var _Error = Error;
  var _JSONStringify = JSON.stringify;
  var _ArrayIsArray = Array.isArray;
  var _DateNow = Date.now;
  var _PerformanceNow = (typeof performance !== 'undefined' && performance.now) ? performance.now.bind(performance) : _DateNow;
  var _setTimeout = setTimeout;

  function _safeStringify(value) {
    try {
      return _JSONStringify.call(JSON, value);
    } catch (e) {
      return '[unserializable: ' + (e && e.message) + ']';
    }
  }

  // Bounded serialiser with cycle detection. Truncates strings,
  // caps depth, summarises arrays/objects with > MAX_KEYS members.
  var MAX_STRING = 2048;
  var MAX_DEPTH = 6;
  var MAX_KEYS = 30;
  var MAX_ARRAY = 30;
  function _summarise(value, depth, seen) {
    if (value === null) return null;
    if (value === undefined) return null;
    var t = typeof value;
    if (t === 'string') {
      if (value.length > MAX_STRING) return value.slice(0, MAX_STRING) + '\u2026<' + (value.length - MAX_STRING) + ' more>';
      return value;
    }
    if (t === 'number' || t === 'boolean') return value;
    if (t === 'bigint') return String(value) + 'n';
    if (t === 'symbol') return 'Symbol(' + String(value.description || '') + ')';
    if (t === 'function') {
      var name = '';
      try { name = value.name || '(anonymous)'; } catch (e) { name = '(?)'; }
      return '[function: ' + name + ']';
    }
    if (depth >= MAX_DEPTH) return '[max-depth]';
    if (seen.indexOf(value) !== -1) return '[circular]';
    seen.push(value);
    try {
      if (_ArrayIsArray(value)) {
        var arr = [];
        var n = Math.min(value.length, MAX_ARRAY);
        for (var i = 0; i < n; i++) arr.push(_summarise(value[i], depth + 1, seen));
        if (value.length > MAX_ARRAY) arr.push('[\u2026' + (value.length - MAX_ARRAY) + ' more]');
        return arr;
      }
      // Special-cased common host objects.
      try {
        if (typeof Element !== 'undefined' && value instanceof Element) {
          return '[Element: <' + value.tagName.toLowerCase() + (value.id ? ' id=' + value.id : '') + '>]';
        }
        if (typeof Event !== 'undefined' && value instanceof Event) {
          return '[Event: type=' + value.type + ']';
        }
        if (typeof ArrayBuffer !== 'undefined' && value instanceof ArrayBuffer) {
          return '[ArrayBuffer: byteLength=' + value.byteLength + ']';
        }
        if (typeof Blob !== 'undefined' && value instanceof Blob) {
          return '[Blob: size=' + value.size + ' type=' + value.type + ']';
        }
        if (typeof URLSearchParams !== 'undefined' && value instanceof URLSearchParams) {
          return '[URLSearchParams: ' + value.toString().slice(0, MAX_STRING) + ']';
        }
        if (typeof FormData !== 'undefined' && value instanceof FormData) {
          return '[FormData]';
        }
      } catch (e) { /* ignore */ }
      // ArrayBufferView (Uint8Array etc.)
      if (value && typeof value.byteLength === 'number' && typeof value.buffer === 'object') {
        var head = [];
        var lim = Math.min(value.length || 0, 32);
        for (var k = 0; k < lim; k++) head.push(value[k]);
        return '[' + (value.constructor && value.constructor.name || 'TypedArray') + ': byteLength=' + value.byteLength + ' head=' + head.join(',') + (lim < (value.length || 0) ? '\u2026' : '') + ']';
      }
      var out = {};
      var written = 0;
      var keys;
      try { keys = _keys(value); } catch (e) { keys = []; }
      for (var j = 0; j < keys.length && written < MAX_KEYS; j++) {
        var key = keys[j];
        if (key === '__argusai_hook_bridge') continue;
        try {
          out[key] = _summarise(value[key], depth + 1, seen);
        } catch (e) {
          out[key] = '[accessor-threw: ' + (e && e.message) + ']';
        }
        written++;
      }
      if (keys.length > MAX_KEYS) out['__truncated__'] = '(' + (keys.length - MAX_KEYS) + ' more keys)';
      return out;
    } finally {
      seen.pop();
    }
  }
  function summarise(value) {
    var seen = [];
    try {
      return _summarise(value, 0, seen);
    } catch (e) {
      return '[summarise-error: ' + (e && e.message) + ']';
    }
  }

  function captureStack(skip) {
    try {
      var stack = (new _Error('argusai-hook')).stack || '';
      var lines = stack.split('\n');
      // Drop top: Error message + this frame + the wrapper.
      lines = lines.slice(2 + (skip || 0), 2 + (skip || 0) + 30);
      // Trim noise.
      return lines.map(function (l) { return String(l).trim(); }).filter(Boolean);
    } catch (e) {
      return [];
    }
  }

  // ---- Per-hook state ----
  // Each entry: { id, kind, target, mode, buffer: Array, head, dropped,
  //               installedAt, replaceReturn, replaceArgs, replaceSetter,
  //               propGetter, propSetter, original, uninstall: fn }
  var HOOKS = _Object.create(null);
  var BUFFER_CAP = 5000;

  function pushHit(hook, rec) {
    if (!hook) return;
    rec.t = _DateNow();
    var b = hook.buffer;
    if (b.length < BUFFER_CAP) {
      b.push(rec);
    } else {
      hook.dropped++;
      // Replace oldest (ring buffer behaviour).
      b[hook.head] = rec;
      hook.head = (hook.head + 1) % BUFFER_CAP;
    }
  }

  // ---- Resolve a dotted/bracket path on globalThis ----
  // e.g. "window.foo.bar", "navigator.webdriver", "JSON.stringify",
  // "XMLHttpRequest.prototype.setRequestHeader"
  function resolvePath(path) {
    if (!path) return null;
    var parts = String(path).replace(/^window\./, '').split('.');
    var ctx = window;
    var owner = window;
    var prop = parts[parts.length - 1];
    for (var i = 0; i < parts.length - 1; i++) {
      var p = parts[i];
      if (!ctx) return null;
      owner = ctx;
      ctx = ctx[p];
    }
    if (ctx == null) return null;
    return { owner: ctx, prop: prop, path: path };
  }

  // ---- Install: function hook ----
  function installFunctionHook(spec, hook) {
    var loc = resolvePath(spec.target);
    if (!loc) throw new _Error('target not found: ' + spec.target);
    var original = loc.owner[loc.prop];
    if (typeof original !== 'function') throw new _Error('target is not a function: ' + spec.target);
    hook.original = original;

    // Optional pre-filter (in JS source as string).
    var shouldRecord = null;
    if (spec.filterSrc) {
      try {
        shouldRecord = new Function('args', 'thisArg', spec.filterSrc);
      } catch (e) {
        // Bad filter: log once, treat as always record.
        shouldRecord = null;
      }
    }
    var replaceReturnFn = null;
    if (spec.replaceReturnSrc) {
      try {
        replaceReturnFn = new Function('args', 'thisArg', 'origReturn', spec.replaceReturnSrc);
      } catch (e) {
        replaceReturnFn = null;
      }
    }
    // sideEffectSrc: a snippet that runs every time the hook fires
    // (after the original returns) for stashing data into globalThis
    // without rewriting the return value. The WASM recipe uses this
    // to copy ArrayBuffers into globalThis.__argusai_wasm_samples so
    // ``browser_save_wasm`` can pull them back to Python.
    //
    // Signature: ``(args, thisArg, ret, threw) => void``. Errors are
    // swallowed so a misbehaving snippet can never break the hook.
    var sideEffectFn = null;
    if (spec.sideEffectSrc) {
      try {
        sideEffectFn = new Function('args', 'thisArg', 'ret', 'threw', spec.sideEffectSrc);
      } catch (e) {
        sideEffectFn = null;
      }
    }

    var wrapped = function () {
      var args = arguments;
      var record = true;
      if (shouldRecord) {
        try { record = !!shouldRecord(args, this); } catch (e) { record = true; }
      }
      var argsCopy = [];
      if (record) {
        for (var i = 0; i < args.length; i++) argsCopy.push(summarise(args[i]));
      }
      var t0 = _PerformanceNow();
      var ret;
      var threw = null;
      try {
        ret = _ReflectApply(original, this, args);
      } catch (e) {
        threw = e;
      }
      var dur = _PerformanceNow() - t0;
      if (replaceReturnFn && threw === null) {
        try { ret = replaceReturnFn(args, this, ret); } catch (e) { /* keep original ret */ }
      }
      if (sideEffectFn) {
        try { sideEffectFn(args, this, ret, threw); } catch (e) { /* ignore */ }
      }
      if (record) {
        var rec = {
          id: spec.id,
          kind: 'function',
          target: spec.target,
          args: argsCopy,
          duration_ms: Math.round(dur * 1000) / 1000,
        };
        if (threw) {
          rec.threw = String(threw && threw.message || threw);
        } else {
          rec.ret = summarise(ret);
        }
        if (spec.captureStack !== false) rec.stack = captureStack(1);
        if (spec.tag) rec.tag = spec.tag;
        pushHit(hook, rec);
      }
      if (threw) throw threw;
      return ret;
    };
    try {
      // Preserve length/name where possible.
      try { _defineProperty(wrapped, 'name', { value: original.name || spec.target, configurable: true }); } catch (e) {}
      try { _defineProperty(wrapped, 'length', { value: original.length, configurable: true }); } catch (e) {}
      // Make Function.prototype.toString return the original — anti-detection.
      try {
        var origToString = Function.prototype.toString.call(original);
        wrapped.toString = function () { return origToString; };
      } catch (e) {}
    } catch (e) {}

    loc.owner[loc.prop] = wrapped;
    hook.uninstall = function () {
      try { loc.owner[loc.prop] = original; } catch (e) {}
    };
  }

  // ---- Install: property hook ----
  function installPropertyHook(spec, hook) {
    var loc = resolvePath(spec.target);
    if (!loc) throw new _Error('target not found: ' + spec.target);
    var desc = null;
    var probe = loc.owner;
    while (probe) {
      desc = _getOwnPropertyDescriptor(probe, loc.prop);
      if (desc) break;
      probe = _getPrototypeOf(probe);
    }
    if (!desc) {
      // Treat as data property on owner.
      desc = { value: loc.owner[loc.prop], writable: true, configurable: true, enumerable: true };
    }
    if (!desc.configurable) throw new _Error('property is non-configurable: ' + spec.target);

    var replaceGet = null;
    if (spec.replaceGetSrc) {
      try { replaceGet = new Function('orig', spec.replaceGetSrc); } catch (e) { replaceGet = null; }
    }

    var origGet = desc.get;
    var origSet = desc.set;
    var origValue = desc.hasOwnProperty('value') ? desc.value : undefined;
    hook.original = { get: origGet, set: origSet, value: origValue, descriptorOwner: probe };

    var newDesc = {
      configurable: true,
      enumerable: desc.enumerable !== false,
      get: function () {
        var origRet;
        try {
          if (origGet) origRet = origGet.call(this);
          else if (origValue !== undefined) origRet = origValue;
          else origRet = undefined;
        } catch (e) {
          pushHit(hook, {
            id: spec.id,
            kind: 'property-get',
            target: spec.target,
            threw: String(e && e.message || e),
            stack: spec.captureStack !== false ? captureStack(1) : undefined,
          });
          throw e;
        }
        var ret = origRet;
        if (replaceGet) {
          try { ret = replaceGet(origRet); } catch (e) { /* keep origRet */ }
        }
        pushHit(hook, {
          id: spec.id,
          kind: 'property-get',
          target: spec.target,
          ret: summarise(ret),
          stack: spec.captureStack !== false ? captureStack(1) : undefined,
          tag: spec.tag,
        });
        return ret;
      },
      set: function (value) {
        pushHit(hook, {
          id: spec.id,
          kind: 'property-set',
          target: spec.target,
          value: summarise(value),
          stack: spec.captureStack !== false ? captureStack(1) : undefined,
          tag: spec.tag,
        });
        if (origSet) {
          origSet.call(this, value);
        } else {
          // Re-install on the owner directly to remember the new value
          // without losing our hook.
          origValue = value;
        }
      },
    };
    try {
      _defineProperty(loc.owner, loc.prop, newDesc);
    } catch (e) {
      throw new _Error('defineProperty failed: ' + (e && e.message));
    }
    hook.uninstall = function () {
      try {
        var restoreDesc;
        if (origGet || origSet) {
          restoreDesc = { configurable: true, enumerable: desc.enumerable !== false, get: origGet, set: origSet };
        } else {
          restoreDesc = { configurable: true, enumerable: desc.enumerable !== false, writable: desc.writable !== false, value: origValue };
        }
        _defineProperty(loc.owner, loc.prop, restoreDesc);
      } catch (e) {}
    };
  }

  // ---- Public bridge ----
  var bridge = {
    install: function (spec) {
      if (!spec || !spec.id) throw new _Error('spec.id is required');
      if (HOOKS[spec.id]) {
        try { HOOKS[spec.id].uninstall(); } catch (e) {}
        delete HOOKS[spec.id];
      }
      var hook = {
        id: spec.id,
        kind: spec.kind || 'function',
        target: spec.target,
        installedAt: _DateNow(),
        buffer: [],
        head: 0,
        dropped: 0,
      };
      try {
        if (hook.kind === 'function') installFunctionHook(spec, hook);
        else if (hook.kind === 'property') installPropertyHook(spec, hook);
        else throw new _Error('unknown hook kind: ' + hook.kind);
      } catch (e) {
        // Surface but don't keep half-installed state.
        return { ok: false, error: String(e && e.message || e) };
      }
      HOOKS[spec.id] = hook;
      return { ok: true, id: spec.id };
    },

    uninstall: function (id) {
      var hook = HOOKS[id];
      if (!hook) return { ok: false, error: 'unknown hook id' };
      try { hook.uninstall(); } catch (e) { /* swallow */ }
      delete HOOKS[id];
      return { ok: true };
    },

    list: function () {
      var out = [];
      for (var id in HOOKS) {
        if (!_has(HOOKS, id)) continue;
        var h = HOOKS[id];
        out.push({
          id: h.id,
          kind: h.kind,
          target: h.target,
          installedAt: h.installedAt,
          hits: h.buffer.length,
          dropped: h.dropped,
        });
      }
      return { ok: true, hooks: out };
    },

    drain: function (id, sinceTs) {
      var hook = HOOKS[id];
      if (!hook) return { ok: false, error: 'unknown hook id', hits: [] };
      var since = Number(sinceTs) || 0;
      var hits = [];
      for (var i = 0; i < hook.buffer.length; i++) {
        var r = hook.buffer[i];
        if (!r) continue;
        if (since && r.t <= since) continue;
        hits.push(r);
      }
      // Clear what we drained.
      if (!since) {
        hook.buffer.length = 0;
        hook.head = 0;
      } else {
        // Keep only entries newer than `since`.
        hook.buffer = hook.buffer.filter(function (r) { return r && r.t > since; });
        hook.head = 0;
      }
      var dropped = hook.dropped;
      hook.dropped = 0;
      return { ok: true, hits: hits, dropped: dropped };
    },

    drainAll: function () {
      var out = {};
      for (var id in HOOKS) {
        if (!_has(HOOKS, id)) continue;
        out[id] = bridge.drain(id, 0);
      }
      return { ok: true, batches: out };
    },

    applyPlan: function (plan) {
      var out = [];
      if (!plan || !plan.hooks) return { ok: true, installed: out };
      for (var i = 0; i < plan.hooks.length; i++) {
        out.push(bridge.install(plan.hooks[i]));
      }
      return { ok: true, installed: out };
    },

    version: '1',
  };

  try {
    _defineProperty(window, '__argusai_hook_bridge', {
      value: bridge,
      writable: false,
      configurable: false,
      enumerable: false,
    });
  } catch (e) {
    // Fallback if defineProperty is hostile.
    window.__argusai_hook_bridge = bridge;
  }
})();
"""


def runtime_bootstrap_script(hooks: list[dict] | None = None) -> str:
    """Return the runtime JS, optionally followed by a synchronous
    install plan that the runtime should apply once init has run.

    Used by ``add_init_script`` so a freshly-loaded page already has
    every active hook in place.
    """
    if not hooks:
        return HOOK_RUNTIME_JS
    import json

    encoded = json.dumps({"hooks": hooks}, ensure_ascii=False)
    return (
        HOOK_RUNTIME_JS
        + "\n;(function(){try{window.__argusai_hook_bridge.applyPlan("
        + encoded
        + ");}catch(e){}})();\n"
    )
