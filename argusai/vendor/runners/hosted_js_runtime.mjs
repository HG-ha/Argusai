// Hosted JS runtime for ArgusAI reverse engineering.
//
// This is a line-delimited JSON protocol over stdin/stdout. The Python side
// keeps this process warm so large bundles can be loaded once, patched, and
// queried repeatedly without paying Node startup or re-evaluation cost.

import readline from 'node:readline';
import vm from 'node:vm';

const MAX_TEXT = 200_000;
const MAX_LOGS = 1000;
const MAX_ORACLES = 10_000;

const sessions = new Map();

function safeSummary(value, depth = 0, seen = new Set()) {
  if (value === null || value === undefined) return value;
  const t = typeof value;
  if (t === 'string') return value.length > 2048 ? value.slice(0, 2048) + `...[${value.length - 2048} more]` : value;
  if (t === 'number' || t === 'boolean') return value;
  if (t === 'bigint') return String(value) + 'n';
  if (t === 'function') return `[function ${value.name || 'anonymous'}]`;
  if (depth >= 5) return '[max-depth]';
  if (seen.has(value)) return '[circular]';
  seen.add(value);
  try {
    if (value instanceof ArrayBuffer) return `[ArrayBuffer byteLength=${value.byteLength}]`;
    if (ArrayBuffer.isView(value)) {
      const head = Array.from(value.slice ? value.slice(0, 32) : new Uint8Array(value.buffer).slice(0, 32));
      return `[${value.constructor?.name || 'TypedArray'} byteLength=${value.byteLength} head=${head.join(',')}]`;
    }
    if (Array.isArray(value)) return value.slice(0, 30).map((v) => safeSummary(v, depth + 1, seen));
    const out = {};
    for (const k of Object.keys(value).slice(0, 30)) {
      try {
        out[k] = safeSummary(value[k], depth + 1, seen);
      } catch (e) {
        out[k] = `[accessor-threw ${e?.message || e}]`;
      }
    }
    return out;
  } finally {
    seen.delete(value);
  }
}

function safeStringify(value) {
  try {
    return JSON.stringify(value, (_k, v) => safeSummary(v));
  } catch (e) {
    try { return String(value); } catch { return '[unserialisable]'; }
  }
}

function clipText(text, limit = MAX_TEXT) {
  const s = String(text ?? '');
  return s.length > limit ? s.slice(0, limit) + `...[truncated ${s.length - limit} chars]` : s;
}

function makeStorage() {
  const map = new Map();
  return {
    getItem(k) { return map.has(String(k)) ? map.get(String(k)) : null; },
    setItem(k, v) { map.set(String(k), String(v)); },
    removeItem(k) { map.delete(String(k)); },
    clear() { map.clear(); },
    key(i) { return [...map.keys()][i | 0] || null; },
    get length() { return map.size; },
  };
}

function makeElement(tag) {
  return {
    tagName: String(tag || 'div').toUpperCase(),
    nodeName: String(tag || 'div').toUpperCase(),
    nodeType: 1,
    style: {},
    dataset: {},
    children: [],
    childNodes: [],
    innerHTML: '',
    outerHTML: '',
    textContent: '',
    appendChild(c) { this.children.push(c); this.childNodes.push(c); return c; },
    removeChild(c) {
      this.children = this.children.filter((x) => x !== c);
      this.childNodes = this.childNodes.filter((x) => x !== c);
      return c;
    },
    setAttribute(k, v) { this[String(k)] = String(v); },
    getAttribute(k) { return this[String(k)] ?? null; },
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
    getBoundingClientRect() { return { x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getContext(kind) {
      if (String(kind).includes('webgl')) return makeWebGLContext();
      return makeCanvas2DContext();
    },
    toDataURL() { return 'data:image/png;base64,'; },
    toBlob(cb) { if (typeof cb === 'function') cb({ size: 0, type: 'image/png' }); },
  };
}

function makeCanvas2DContext() {
  return {
    fillRect() {}, clearRect() {}, strokeRect() {}, fillText() {}, strokeText() {},
    measureText: (text) => ({ width: String(text || '').length * 8 }),
    getImageData: (_x, _y, w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(Math.max(0, w * h * 4)) }),
    putImageData() {}, drawImage() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
  };
}

function makeWebGLContext() {
  const profile = makeWebGLContext.profile || {};
  return {
    getParameter(p) {
      if (p === 37445) return profile.vendor || '';
      if (p === 37446) return profile.renderer || '';
      return typeof p === 'number' ? (profile.parameters?.[String(p)] ?? 0) : '';
    },
    getExtension() { return null; },
    getSupportedExtensions() { return []; },
    getContextAttributes() { return { alpha: true, antialias: true, depth: true, stencil: false }; },
    clear() {}, clearColor() {}, drawArrays() {}, drawElements() {},
  };
}

function makeBrowserGlobals(session) {
  const pageUrl = session.pageUrl || 'https://example.com/';
  const url = new URL(pageUrl);
  const profile = session.profile || {};
  const navigatorProfile = profile.navigator || {};
  const screenProfile = profile.screen || {};
  const canvasProfile = profile.canvas || {};
  const webglProfile = profile.webgl || {};
  makeWebGLContext.profile = webglProfile;
  const navigator = {
    userAgent: session.userAgent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    platform: 'Win32',
    vendor: 'Google Inc.',
    language: 'en-US',
    languages: ['en-US', 'en'],
    webdriver: false,
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    plugins: { length: 0 },
    mimeTypes: { length: 0 },
    ...navigatorProfile,
  };
  const location = {
    href: url.href, origin: url.origin, protocol: url.protocol, host: url.host,
    hostname: url.hostname, port: url.port, pathname: url.pathname, search: url.search,
    hash: url.hash, assign(u) { this.href = String(u); }, replace(u) { this.href = String(u); },
    reload() {}, toString() { return this.href; },
  };
  const document = {
    nodeType: 9,
    nodeName: '#document',
    documentElement: makeElement('html'),
    head: makeElement('head'),
    body: makeElement('body'),
    title: '',
    URL: url.href,
    documentURI: url.href,
    baseURI: url.href,
    referrer: '',
    readyState: 'complete',
    cookie: '',
    currentScript: null,
    createElement: (t) => makeElement(t),
    createElementNS: (_ns, t) => makeElement(t),
    createTextNode: (text) => ({ nodeType: 3, nodeValue: String(text), textContent: String(text) }),
    getElementById: () => null,
    getElementsByTagName: () => [],
    getElementsByClassName: () => [],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
  };
  const win = {
    navigator,
    location,
    document,
    screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24, ...screenProfile },
    history: { length: 1, state: null, back() {}, forward() {}, go() {}, pushState() {}, replaceState() {} },
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    performance: { now: () => Date.now() - session.startedAt, timeOrigin: session.startedAt, getEntries: () => [] },
    fetch: (...args) => {
      recordOracle(session, 'fetch', { args: safeSummary(args) });
      return Promise.resolve({
        ok: true, status: 200, statusText: 'OK', url: pageUrl,
        headers: { get: () => null, has: () => false, forEach() {} },
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
        clone() { return this; },
      });
    },
    XMLHttpRequest: function XMLHttpRequest() {
      const state = {};
      return {
        readyState: 4, status: 200, statusText: 'OK', responseText: '', response: '',
        open(method, url) { state.method = method; state.url = url; },
        send(body) { recordOracle(session, 'xhr', { ...state, body: safeSummary(body) }); if (typeof this.onload === 'function') this.onload(); },
        setRequestHeader() {}, abort() {}, getResponseHeader() { return null; }, getAllResponseHeaders() { return ''; },
        addEventListener() {}, removeEventListener() {},
      };
    },
    HTMLCanvasElement: function HTMLCanvasElement() {},
    CanvasRenderingContext2D: function CanvasRenderingContext2D() {},
    WebGLRenderingContext: function WebGLRenderingContext() {},
    Image: function Image() { return makeElement('img'); },
    Audio: function Audio() { return makeElement('audio'); },
    AudioContext: function AudioContext() { return { __argusai_profile: profile.audio || {} }; },
    matchMedia: (q) => ({ matches: false, media: String(q), addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
    cancelAnimationFrame: clearTimeout,
    atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
    btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() { return true; },
  };
  if (canvasProfile.toDataURL) {
    const originalCreate = document.createElement;
    document.createElement = (t) => {
      const el = originalCreate(t);
      if (String(t).toLowerCase() === 'canvas') {
        el.toDataURL = () => String(canvasProfile.toDataURL);
      }
      return el;
    };
  }
  win.window = win;
  win.self = win;
  win.globalThis = win;
  win.top = win;
  win.parent = win;
  document.defaultView = win;
  return win;
}

function recordOracle(session, kind, data) {
  const rec = { id: ++session.oracleSeq, ts: Date.now(), kind, ...data };
  if (session.oracles.length >= MAX_ORACLES) session.oracles.shift();
  session.oracles.push(rec);
  return rec;
}

function makeSession(id, opts = {}) {
  const session = {
    id,
    env: String(opts.env || 'browser').toLowerCase(),
    pageUrl: String(opts.page_url || 'https://example.com/'),
    userAgent: String(opts.user_agent || ''),
    startedAt: Date.now(),
    logs: [],
    oracles: [],
    oracleSeq: 0,
    hooks: new Map(),
    sources: new Map(),
    patches: [],
    sourceSeq: 0,
    patchSeq: 0,
    profile: (opts.profile && typeof opts.profile === 'object') ? opts.profile : {},
  };
  const consoleProxy = {
    log: (...a) => session.logs.push({ level: 'log', text: clipText(a.map((x) => typeof x === 'string' ? x : safeStringify(x)).join(' '), 4000), ts: Date.now() }),
    warn: (...a) => session.logs.push({ level: 'warn', text: clipText(a.map((x) => typeof x === 'string' ? x : safeStringify(x)).join(' '), 4000), ts: Date.now() }),
    error: (...a) => session.logs.push({ level: 'error', text: clipText(a.map((x) => typeof x === 'string' ? x : safeStringify(x)).join(' '), 4000), ts: Date.now() }),
  };
  const base = {
    console: consoleProxy,
    setTimeout: (fn, delay = 0, ...args) => {
      if (typeof fn === 'string') {
        const code = String(fn);
        const gen = registerGeneratedSource(session, code, 'setTimeout-string.js', 'setTimeout(string)');
        recordOracle(session, 'generated_code', { mechanism: 'setTimeout(string)', code: clipText(code, 8000), source_id: gen.id });
        return setTimeout(() => {
          try { evaluate(session, code, 'setTimeout-string.js', 5000); } catch (e) { recordOracle(session, 'generated_code_error', { mechanism: 'setTimeout(string)', error: String(e?.message || e) }); }
        }, delay);
      }
      return setTimeout(fn, delay, ...args);
    },
    clearTimeout, setInterval, clearInterval, queueMicrotask,
    eval: (code) => {
      const src = String(code || '');
      const gen = registerGeneratedSource(session, src, 'eval-generated.js', 'eval');
      recordOracle(session, 'generated_code', { mechanism: 'eval', code: clipText(src, 8000), source_id: gen.id });
      const script = new vm.Script(src, { filename: 'eval-generated.js' });
      return script.runInContext(session.context, { timeout: 5000 });
    },
    Function: function FunctionConstructor(...parts) {
      const body = String(parts.pop() || '');
      const params = parts.map((p) => String(p)).join(',');
      const src = `(function(${params}){\n${body}\n})`;
      const gen = registerGeneratedSource(session, src, 'function-generated.js', 'Function');
      recordOracle(session, 'generated_code', { mechanism: 'Function', code: clipText(src, 8000), source_id: gen.id });
      const script = new vm.Script(src, { filename: 'function-generated.js' });
      return script.runInContext(session.context, { timeout: 5000 });
    },
    URL, URLSearchParams, TextEncoder, TextDecoder,
    crypto: globalThis.crypto,
    Promise, Symbol, Map, Set, WeakMap, WeakSet, Proxy, Reflect, JSON, Math, Date, RegExp,
    Error, TypeError, RangeError, SyntaxError, ReferenceError,
    Array, Object, String, Number, Boolean,
    ArrayBuffer, Uint8Array, Uint8ClampedArray, Uint16Array, Uint32Array,
    Int8Array, Int16Array, Int32Array, Float32Array, Float64Array,
    DataView, BigInt, BigInt64Array, BigUint64Array,
  };
  if (session.env === 'browser') {
    const win = makeBrowserGlobals(session);
    Object.assign(base, win, {
      window: win, self: win, globalThis: win,
      document: win.document,
      navigator: win.navigator,
      location: win.location,
      localStorage: win.localStorage,
      sessionStorage: win.sessionStorage,
      performance: win.performance,
      fetch: win.fetch,
      XMLHttpRequest: win.XMLHttpRequest,
      atob: win.atob,
      btoa: win.btoa,
    });
  } else {
    Object.assign(base, {
      Buffer,
      atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
      btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
      process: {
        versions: {},
        exit(code = 0) {
          recordOracle(session, 'process_exit', { code });
          throw new Error(`process.exit intercepted: ${code}`);
        },
      },
    });
    base.globalThis = base;
  }
  Object.assign(base, opts.globals && typeof opts.globals === 'object' ? opts.globals : {});
  session.sandbox = base;
  session.context = vm.createContext(base, { name: `argusai-hosted-${id}` });
  return session;
}

function shaPreview(text) {
  let h = 2166136261;
  const s = String(text || '');
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

function sourceSummary(src) {
  return {
    id: src.id,
    filename: src.filename,
    origin: src.origin,
    version: src.version,
    size: src.text.length,
    hash: shaPreview(src.text),
    loaded_at: src.loaded_at,
    patched_at: src.patched_at || null,
    generated: !!src.generated,
  };
}

function registerGeneratedSource(session, code, filename, mechanism) {
  const rec = {
    id: `src-${++session.sourceSeq}`,
    filename,
    origin: mechanism,
    version: 1,
    text: String(code || ''),
    loaded_at: Date.now(),
    generated: true,
  };
  session.sources.set(rec.id, rec);
  return rec;
}

function resolvePath(session, rawPath) {
  const path = String(rawPath || '').replace(/^(window|self|globalThis)\./, '');
  const parts = path.split('.').filter(Boolean);
  let owner = session.sandbox;
  for (let i = 0; i < parts.length - 1; i++) {
    owner = owner?.[parts[i]];
    if (owner == null) return null;
  }
  const prop = parts[parts.length - 1];
  if (!prop) return null;
  return { owner, prop, value: owner?.[prop] };
}

async function evaluate(session, code, filename = 'runtime.js', timeoutMs = 5000) {
  const script = new vm.Script(String(code || ''), { filename });
  let out = script.runInContext(session.context, { timeout: timeoutMs, breakOnSigint: true });
  if (out && typeof out.then === 'function') out = await out;
  return out;
}

async function loadSource(session, code, filename = 'bundle.js', origin = '', timeoutMs = 5000) {
  const sourceId = `src-${++session.sourceSeq}`;
  const text = String(code || '');
  const rec = {
    id: sourceId,
    filename,
    origin,
    version: 1,
    text,
    loaded_at: Date.now(),
  };
  session.sources.set(sourceId, rec);
  await evaluate(session, text, filename, timeoutMs);
  return sourceSummary(rec);
}

async function reevalSource(session, rec, timeoutMs) {
  await evaluate(session, rec.text, rec.filename || `${rec.id}.js`, timeoutMs);
}

function patchText(text, req) {
  const mode = String(req.mode || 'string');
  const replacement = String(req.replacement ?? '');
  const original = String(text || '');
  if (mode === 'offset') {
    const start = Math.max(0, Math.min(original.length, parseInt(req.start, 10) || 0));
    const end = Math.max(start, Math.min(original.length, parseInt(req.end, 10) || start));
    return {
      text: original.slice(0, start) + replacement + original.slice(end),
      count: 1,
      summary: { start, end, removed_chars: end - start, inserted_chars: replacement.length },
    };
  }
  if (mode === 'regex') {
    const pattern = String(req.pattern || '');
    if (!pattern) throw new Error('pattern is required for regex patch');
    const flagsRaw = String(req.flags || 'g');
    const flags = flagsRaw.includes('g') ? flagsRaw : flagsRaw + 'g';
    const re = new RegExp(pattern, flags);
    let count = 0;
    const out = original.replace(re, (...args) => {
      count += 1;
      if (req.max_replacements && count > parseInt(req.max_replacements, 10)) return args[0];
      return replacement;
    });
    return { text: out, count: Math.min(count, parseInt(req.max_replacements, 10) || count), summary: { pattern, flags } };
  }
  const search = String(req.search || '');
  if (!search) throw new Error('search is required for string patch');
  const max = Math.max(1, parseInt(req.max_replacements, 10) || 1);
  let out = original;
  let count = 0;
  while (out.includes(search) && count < max) {
    out = out.replace(search, replacement);
    count += 1;
  }
  return { text: out, count, summary: { search_chars: search.length, replacement_chars: replacement.length } };
}

async function patchSource(session, req, timeoutMs) {
  const sourceId = String(req.source_id || '');
  const rec = sourceId ? session.sources.get(sourceId) : [...session.sources.values()].at(-1);
  if (!rec) throw new Error(`source not found: ${sourceId || '(latest)'}`);
  const before = rec.text;
  const patched = patchText(before, req);
  if (!patched.count) {
    return { source: sourceSummary(rec), patch: null, count: 0 };
  }
  rec.text = patched.text;
  rec.version += 1;
  rec.patched_at = Date.now();
  const patch = {
    id: `patch-${++session.patchSeq}`,
    source_id: rec.id,
    mode: String(req.mode || 'string'),
    count: patched.count,
    before_size: before.length,
    after_size: rec.text.length,
    before_hash: shaPreview(before),
    after_hash: shaPreview(rec.text),
    note: String(req.note || ''),
    summary: patched.summary,
    applied_at: Date.now(),
  };
  session.patches.push(patch);
  if (req.reevaluate !== false) {
    const activeHooks = [...session.hooks.values()];
    session.hooks.clear();
    await reevalSource(session, rec, timeoutMs);
    for (const hook of activeHooks) {
      try {
        installHook(session, hook.target, {
          hook_id: hook.id,
          capture_stack: hook.capture_stack,
          tag: hook.tag,
          source: hook.source || 'reinstall',
        });
      } catch (e) {
        recordOracle(session, 'hook_reinstall_error', { hook_id: hook.id, target: hook.target, error: String(e?.message || e) });
      }
    }
  }
  return { source: sourceSummary(rec), patch, count: patched.count };
}

function listSources(session) {
  return [...session.sources.values()].map(sourceSummary);
}

function listHooks(session) {
  return [...session.hooks.values()];
}

function snapshotSession(session) {
  return {
    id: session.id,
    env: session.env,
    page_url: session.pageUrl,
    sources: listSources(session),
    patches: session.patches.slice(-200),
    hooks: listHooks(session),
    oracle_count: session.oracles.length,
    log_count: session.logs.length,
    age_ms: Date.now() - session.startedAt,
  };
}

async function evalExpression(session, expression, inputs, timeoutMs = 5000) {
  session.sandbox.__argusai_inputs = inputs;
  const script = new vm.Script(`(${String(expression || 'undefined')})`, { filename: 'expression.js' });
  let out = script.runInContext(session.context, { timeout: timeoutMs, breakOnSigint: true });
  if (typeof out === 'function') out = out.call(session.sandbox, inputs);
  if (out && typeof out.then === 'function') out = await out;
  return out;
}

async function callFunction(session, target, args, timeoutMs = 5000) {
  const loc = resolvePath(session, target);
  if (!loc || typeof loc.value !== 'function') throw new Error(`target is not a function: ${target}`);
  let out = loc.value.apply(session.sandbox, Array.isArray(args) ? args : []);
  if (out && typeof out.then === 'function') out = await Promise.race([
    out,
    new Promise((_r, rej) => setTimeout(() => rej(new Error('call timeout')), timeoutMs)),
  ]);
  return out;
}

function installHook(session, target, opts = {}) {
  const loc = resolvePath(session, target);
  if (!loc || typeof loc.value !== 'function') throw new Error(`target is not a function: ${target}`);
  const id = String(opts.hook_id || `h-${session.hooks.size + 1}`);
  const original = loc.value;
  const filterSrc = String(opts.filter_src || '');
  const filter = filterSrc ? new Function('args', 'thisArg', filterSrc) : null;
  const wrapped = function (...args) {
    const started = Date.now();
    let shouldRecord = true;
    if (filter) {
      try { shouldRecord = !!filter(args, this); } catch { shouldRecord = false; }
    }
    try {
      const ret = original.apply(this, args);
      if (ret && typeof ret.then === 'function') {
        ret.then((value) => {
          if (shouldRecord) recordOracle(session, 'hook', {
            hook_id: id, target, phase: 'async_return',
            args: safeSummary(args), return_value: safeSummary(value),
            duration_ms: Date.now() - started,
          });
        }).catch((e) => {
          if (shouldRecord) recordOracle(session, 'hook_error', { hook_id: id, target, error: String(e?.message || e) });
        });
      } else if (shouldRecord) {
        recordOracle(session, 'hook', {
          hook_id: id, target, phase: 'return',
          args: safeSummary(args), return_value: safeSummary(ret),
          duration_ms: Date.now() - started,
          stack: opts.capture_stack === false ? [] : String(new Error('hook').stack || '').split('\n').slice(2, 18).map((s) => s.trim()),
        });
      }
      return ret;
    } catch (e) {
      if (shouldRecord) recordOracle(session, 'hook_error', { hook_id: id, target, args: safeSummary(args), error: String(e?.message || e) });
      throw e;
    }
  };
  loc.owner[loc.prop] = wrapped;
  session.hooks.set(id, {
    id,
    target,
    installed_at: Date.now(),
    tag: String(opts.tag || ''),
    source: String(opts.source || 'manual'),
    capture_stack: opts.capture_stack !== false,
  });
  return { id, target };
}

function installInstrument(session, req) {
  const targets = Array.isArray(req.targets) ? req.targets : [req.target].filter(Boolean);
  const installed = [];
  for (const target of targets) {
    installed.push(installHook(session, String(target), {
      hook_id: req.hook_id ? `${req.hook_id}-${installed.length + 1}` : '',
      filter_src: req.filter_src || '',
      capture_stack: req.capture_stack,
      tag: req.tag || '',
      source: 'instrument',
    }));
  }
  return installed;
}

function serialiseResult(value) {
  if (value === undefined) return { result: null, result_type: 'undefined', result_size_chars: 0 };
  if (typeof value === 'string') return { result: clipText(value), result_type: 'string', result_size_chars: value.length };
  const text = safeStringify(value);
  return { result: clipText(text), result_type: 'json', result_size_chars: text.length };
}

async function handle(req) {
  const op = String(req.op || '');
  if (op === 'ping') return { ok: true, sessions: sessions.size };
  if (op === 'create') {
    const id = String(req.session_id || `rt-${Date.now()}`);
    if (sessions.has(id)) return { ok: true, session_id: id, reused: true };
    sessions.set(id, makeSession(id, req));
    return { ok: true, session_id: id, env: sessions.get(id).env };
  }
  if (op === 'close') {
    const id = String(req.session_id || '');
    const existed = sessions.delete(id);
    return { ok: true, session_id: id, closed: existed };
  }
  if (op === 'status') {
    return {
      ok: true,
      sessions: [...sessions.values()].map((s) => ({
        id: s.id, env: s.env, age_ms: Date.now() - s.startedAt,
        hooks: [...s.hooks.values()], sources: listSources(s),
        oracle_count: s.oracles.length, log_count: s.logs.length,
      })),
    };
  }
  const session = sessions.get(String(req.session_id || ''));
  if (!session) return { ok: false, error: `session not found: ${req.session_id || ''}` };
  const timeoutMs = Math.min(Math.max(parseInt(req.timeout_ms, 10) || 5000, 100), 120_000);
  if (op === 'load') {
    const source = await loadSource(
      session,
      req.source || '',
      String(req.filename || 'bundle.js'),
      String(req.origin || ''),
      timeoutMs,
    );
    return { ok: true, session_id: session.id, source };
  }
  if (op === 'sources') {
    return { ok: true, session_id: session.id, sources: listSources(session), count: session.sources.size };
  }
  if (op === 'patches') {
    return { ok: true, session_id: session.id, patches: session.patches.slice(-500), count: session.patches.length };
  }
  if (op === 'hooks') {
    return { ok: true, session_id: session.id, hooks: listHooks(session), count: session.hooks.size };
  }
  if (op === 'patch') {
    return { ok: true, session_id: session.id, ...(await patchSource(session, req, timeoutMs)) };
  }
  if (op === 'eval') {
    return { ok: true, session_id: session.id, ...serialiseResult(await evalExpression(session, req.expression, req.inputs, timeoutMs)) };
  }
  if (op === 'call') {
    return { ok: true, session_id: session.id, ...serialiseResult(await callFunction(session, req.target, req.args, timeoutMs)) };
  }
  if (op === 'hook') {
    return { ok: true, session_id: session.id, hook: installHook(session, req.target, req) };
  }
  if (op === 'instrument') {
    const installed = installInstrument(session, req);
    return { ok: true, session_id: session.id, installed, count: installed.length };
  }
  if (op === 'snapshot') {
    return { ok: true, session_id: session.id, snapshot: snapshotSession(session) };
  }
  if (op === 'drain') {
    const limit = Math.min(Math.max(parseInt(req.limit, 10) || 200, 1), 5000);
    const records = session.oracles.splice(0, limit);
    return { ok: true, session_id: session.id, count: records.length, records, remaining: session.oracles.length };
  }
  if (op === 'logs') {
    const limit = Math.min(Math.max(parseInt(req.limit, 10) || 100, 1), 1000);
    const logs = session.logs.splice(0, limit);
    return { ok: true, session_id: session.id, count: logs.length, logs, remaining: session.logs.length };
  }
  return { ok: false, error: `unknown op: ${op}` };
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', async (line) => {
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, id: null, error: `invalid JSON: ${e.message}` }) + '\n');
    return;
  }
  try {
    const res = await handle(req);
    process.stdout.write(JSON.stringify({ id: req.id ?? null, ...res }) + '\n');
  } catch (e) {
    process.stdout.write(JSON.stringify({
      id: req.id ?? null,
      ok: false,
      error: String(e?.message || e),
      stack: String(e?.stack || '').slice(0, 8192),
    }) + '\n');
  }
});
