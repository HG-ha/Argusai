// Generic JS runner — reads JSON from stdin describing what to run,
// emits JSON on stdout. Lives next to webcrack_run.mjs / restringer_run.mjs
// so the existing runtime_manager treats it the same way.
//
// Stdin payload:
//   {
//     "mode":  "eval" | "script" | "call",
//     "env":   "node" | "browser",     // optional, default "node"
//     "source": "<js source>",        // script body or expression
//     "function_name": "<name>",      // mode="call" only
//     "args": [ ... ],                 // mode="call" only, JSON
//     "globals": { name: value, ... }, // optional pre-injected globals
//     "timeout_ms": 5000,              // soft inner timeout
//     "page_url": "https://...",       // env=browser: location.href
//     "user_agent": "..."              // env=browser: navigator.userAgent
//   }
//
// env="browser":
//   Injects stub window/document/navigator/location/history/screen/
//   localStorage/sessionStorage/performance/fetch + hides Node-only
//   surfaces (process / Buffer / require / global) from the sandbox so
//   common anti-bot guards like
//      typeof process !== "undefined" && process.versions.node
//      typeof window === "undefined" || typeof document === "undefined"
//   pass. NOT a real DOM — no layout, no innerHTML parsing, no
//   getBoundingClientRect. JSDOM is intentionally not bundled; use the
//   browser tools when a real DOM implementation is required.
//
// Stdout JSON:
//   {
//     "ok": bool,
//     "result": <JSON-serialised return value, or string>,
//     "result_type": "json" | "string" | "undefined" | "function" | "error",
//     "result_size_chars": int,
//     "stdout": "<captured console.log>",
//     "stderr": "<captured console.error>",
//     "error": "<message if !ok>",
//     "stack": "<stack if available>",
//     "elapsed_ms": int
//   }
//
// Limits enforced by THIS runner (in addition to Python's subprocess
// wall-clock timeout): the script is evaluated inside vm.Script with
// timeout=timeout_ms, console.log/error are captured (not echoed), and
// the serialised result is capped at 200 000 chars to defend our pipe.

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const MAX_RESULT_CHARS = 200_000;

function readStdin() {
  try {
    return readFileSync(0, 'utf8');
  } catch (e) {
    return '';
  }
}

function safeStringify(v) {
  // JSON.stringify dies on cycles and BigInt; cover both.
  try {
    return JSON.stringify(v, (_k, x) => {
      if (typeof x === 'bigint') return String(x) + 'n';
      if (typeof x === 'function') return `[function ${x.name || 'anonymous'}]`;
      if (x instanceof Uint8Array) return { __uint8array: Array.from(x.slice(0, 256)), length: x.length };
      return x;
    });
  } catch (e) {
    try { return String(v); } catch { return '[unserialisable]'; }
  }
}

const t0 = Date.now();

let payload;
try {
  payload = JSON.parse(readStdin() || '{}');
} catch (e) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: 'invalid JSON on stdin: ' + e.message,
    result: null, result_type: 'error', result_size_chars: 0,
    stdout: '', stderr: '', elapsed_ms: Date.now() - t0,
  }));
  process.exit(0);
}

const mode = String(payload.mode || 'script');
const env = String(payload.env || 'node').toLowerCase();
const source = String(payload.source || '');
const functionName = payload.function_name ? String(payload.function_name) : '';
const args = Array.isArray(payload.args) ? payload.args : [];
const globals = (payload.globals && typeof payload.globals === 'object') ? payload.globals : {};
const timeoutMs = Math.min(Math.max(parseInt(payload.timeout_ms, 10) || 5000, 100), 60_000);
const pageUrl = String(payload.page_url || 'https://example.com/');
const userAgent = String(payload.user_agent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36');
const profile = (payload.profile && typeof payload.profile === 'object') ? payload.profile : {};

// Captured stdout/stderr for the CALLER (not Node itself).
const capturedOut = [];
const capturedErr = [];

const consoleProxy = {
  log:   (...a) => capturedOut.push(a.map(x => typeof x === 'string' ? x : safeStringify(x)).join(' ')),
  error: (...a) => capturedErr.push(a.map(x => typeof x === 'string' ? x : safeStringify(x)).join(' ')),
  warn:  (...a) => capturedErr.push('[warn] ' + a.map(x => typeof x === 'string' ? x : safeStringify(x)).join(' ')),
  info:  (...a) => capturedOut.push('[info] ' + a.map(x => typeof x === 'string' ? x : safeStringify(x)).join(' ')),
  debug: (...a) => capturedOut.push('[debug] ' + a.map(x => typeof x === 'string' ? x : safeStringify(x)).join(' ')),
};

// ---------------------------------------------------------------------------
// Browser-like environment: injects enough fake globals to satisfy the
// most common anti-detection guards (typeof window, typeof document,
// process.versions.node, navigator.userAgent, localStorage.*). This is
// NOT a real DOM — no layout, no event loop semantics, no parser.
// Purpose: get past gatekeeping ifs so the actual decrypt/decode logic
// runs in Node-speed pure JS. If the script needs real DOM, route to
// `sandbox_eval_js` (real browser) instead.
// ---------------------------------------------------------------------------

function makeStorage() {
  // localStorage / sessionStorage are spec'd as Storage interface;
  // scripts commonly call getItem/setItem/removeItem/clear/key/length.
  const map = new Map();
  return {
    getItem(k)    { return map.has(String(k)) ? map.get(String(k)) : null; },
    setItem(k, v) { map.set(String(k), String(v)); },
    removeItem(k) { map.delete(String(k)); },
    clear()       { map.clear(); },
    key(i)        { return [...map.keys()][i|0] || null; },
    get length()  { return map.size; },
  };
}

function makeStubElement(tag) {
  const isCanvas = String(tag || '').toLowerCase() === 'canvas';
  const el = {
    tagName: String(tag || 'div').toUpperCase(),
    nodeName: String(tag || 'div').toUpperCase(),
    nodeType: 1,
    childNodes: [],
    children: [],
    attributes: {},
    style: {},
    classList: { add(){}, remove(){}, contains(){return false;}, toggle(){} },
    dataset: {},
    innerHTML: '',
    outerHTML: '',
    textContent: '',
    appendChild(c) { this.childNodes.push(c); this.children.push(c); return c; },
    removeChild(c) {
      this.childNodes = this.childNodes.filter(x => x !== c);
      this.children = this.children.filter(x => x !== c);
      return c;
    },
    setAttribute(k, v) { this.attributes[String(k)] = String(v); },
    getAttribute(k) { return this.attributes[String(k)] ?? null; },
    removeAttribute(k) { delete this.attributes[String(k)]; },
    hasAttribute(k) { return String(k) in this.attributes; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    getBoundingClientRect() { return {x:0,y:0,width:0,height:0,top:0,left:0,right:0,bottom:0}; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    cloneNode() { return makeStubElement(tag); },
    getContext(kind) {
      if (String(kind || '').includes('webgl')) {
        const webgl = profile.webgl || {};
        return {
          getParameter(p) {
            if (p === 37445) return webgl.vendor || '';
            if (p === 37446) return webgl.renderer || '';
            return 0;
          },
          getExtension() { return null; },
          getSupportedExtensions() { return []; },
        };
      }
      return { measureText: (text) => ({ width: String(text || '').length * 8 }), fillText(){}, getImageData: () => ({ data: new Uint8ClampedArray(0) }) };
    },
    toDataURL() { return isCanvas && profile.canvas?.toDataURL ? String(profile.canvas.toDataURL) : 'data:image/png;base64,'; },
    focus() {}, blur() {}, click() {},
  };
  return el;
}

function makeBrowserGlobals() {
  // Build a self-referential window so window === window.window etc.
  // Many guards do ``window.window === window`` to detect node.
  const url = new URL(pageUrl);
  const navigator = {
    userAgent,
    appName: 'Netscape',
    appVersion: '5.0 (Windows)',
    appCodeName: 'Mozilla',
    platform: 'Win32',
    product: 'Gecko',
    productSub: '20030107',
    vendor: 'Google Inc.',
    vendorSub: '',
    language: 'en-US',
    languages: ['en-US', 'en'],
    onLine: true,
    cookieEnabled: true,
    doNotTrack: null,
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    webdriver: false,  // <-- the big one
    plugins: { length: 0, item(){return null;}, namedItem(){return null;}, refresh(){} },
    mimeTypes: { length: 0, item(){return null;}, namedItem(){return null;} },
    geolocation: { getCurrentPosition(){}, watchPosition(){return 0;}, clearWatch(){} },
    permissions: { query: () => Promise.resolve({ state: 'prompt' }) },
    clipboard: { writeText: () => Promise.resolve(), readText: () => Promise.resolve('') },
    sendBeacon: () => true,
    ...(profile.navigator || {}),
  };
  const location = {
    href: url.href, origin: url.origin, protocol: url.protocol, host: url.host,
    hostname: url.hostname, port: url.port, pathname: url.pathname,
    search: url.search, hash: url.hash,
    assign(u) { this.href = String(u); },
    replace(u) { this.href = String(u); },
    reload() {},
    toString() { return this.href; },
  };
  const history = {
    length: 1, state: null, scrollRestoration: 'auto',
    back() {}, forward() {}, go() {},
    pushState(s, _t, u) { this.state = s; if (u) location.assign(u); },
    replaceState(s, _t, u) { this.state = s; if (u) location.replace(u); },
  };
  const screen = {
    width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
    colorDepth: 24, pixelDepth: 24, orientation: { type: 'landscape-primary', angle: 0 },
    ...(profile.screen || {}),
  };
  const document = {
    nodeType: 9,
    nodeName: '#document',
    documentElement: makeStubElement('html'),
    head: makeStubElement('head'),
    body: makeStubElement('body'),
    title: '',
    URL: url.href, documentURI: url.href, baseURI: url.href,
    referrer: '', readyState: 'complete',
    cookie: '',
    domain: url.hostname,
    visibilityState: 'visible', hidden: false,
    characterSet: 'UTF-8', charset: 'UTF-8',
    contentType: 'text/html',
    compatMode: 'CSS1Compat',
    currentScript: null,
    activeElement: null,
    scripts: [], forms: [], images: [], links: [], embeds: [], anchors: [],
    createElement: (t) => makeStubElement(t),
    createElementNS: (_ns, t) => makeStubElement(t),
    createTextNode: (text) => ({ nodeType: 3, nodeValue: String(text), textContent: String(text) }),
    createDocumentFragment: () => ({ childNodes: [], appendChild(c){ this.childNodes.push(c); return c; } }),
    createEvent: () => ({ initEvent(){}, type: '' }),
    getElementById: () => null,
    getElementsByTagName: () => [],
    getElementsByClassName: () => [],
    getElementsByName: () => [],
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    write() {}, writeln() {}, open() { return this; }, close() {},
    execCommand() { return false; },
    hasFocus() { return true; },
    elementFromPoint() { return null; },
  };
  // Window itself.
  const win = {
    name: '',
    closed: false,
    innerWidth: 1920, innerHeight: 1040,
    outerWidth: 1920, outerHeight: 1080,
    devicePixelRatio: 1,
    scrollX: 0, scrollY: 0, pageXOffset: 0, pageYOffset: 0,
    navigator, location, history, screen, document,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    performance: {
      now: () => (Date.now() - t0) + Math.random(),
      timeOrigin: t0,
      timing: { navigationStart: t0, loadEventEnd: t0 + 100, domContentLoadedEventEnd: t0 + 50 },
      getEntries: () => [], getEntriesByName: () => [], getEntriesByType: () => [],
      mark() {}, measure() {}, clearMarks() {}, clearMeasures() {},
    },
    Image: function Image() { return makeStubElement('img'); },
    Audio: function Audio() { return makeStubElement('audio'); },
    XMLHttpRequest: function XMLHttpRequest() {
      // Stub that "succeeds" with empty body. Anti-bot probes that ping
      // a server URL and check the response are common; we don't want
      // them to throw.
      return {
        readyState: 4, status: 200, statusText: 'OK', responseText: '', response: '',
        open() {}, send() { setTimeout(() => { if (typeof this.onload === 'function') try{this.onload();}catch(_e){} }, 0); },
        setRequestHeader() {}, abort() {}, getResponseHeader() { return null; }, getAllResponseHeaders() { return ''; },
        addEventListener() {}, removeEventListener() {},
      };
    },
    fetch: () => Promise.resolve({
      ok: true, status: 200, statusText: 'OK', url: pageUrl,
      headers: { get: () => null, has: () => false, forEach: () => {} },
      text: () => Promise.resolve(''),
      json: () => Promise.resolve({}),
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
      blob: () => Promise.resolve({size: 0, type: ''}),
      clone() { return this; },
    }),
    matchMedia: (q) => ({
      matches: false, media: String(q), onchange: null,
      addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    requestAnimationFrame: (cb) => setTimeout(() => cb(Date.now()), 16),
    cancelAnimationFrame: clearTimeout,
    requestIdleCallback: (cb) => setTimeout(() => cb({didTimeout: false, timeRemaining: () => 50}), 1),
    cancelIdleCallback: clearTimeout,
    alert() {}, confirm() { return true; }, prompt() { return ''; },
    open() { return null; }, close() {}, focus() {}, blur() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    postMessage() {},
    btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
    atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  };
  win.self = win;
  win.window = win;
  win.parent = win;
  win.top = win;
  win.frames = win;
  document.defaultView = win;
  return { win, document, navigator, location, history, screen };
}

let sandbox;
if (env === 'browser') {
  const b = makeBrowserGlobals();
  sandbox = {
    console: consoleProxy,
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    // Browser-only globals — these MUST be present for anti-detection to pass.
    window: b.win,
    self: b.win,
    document: b.document,
    navigator: b.navigator,
    location: b.location,
    history: b.history,
    screen: b.screen,
    localStorage: b.win.localStorage,
    sessionStorage: b.win.sessionStorage,
    performance: b.win.performance,
    fetch: b.win.fetch,
    XMLHttpRequest: b.win.XMLHttpRequest,
    Image: b.win.Image, Audio: b.win.Audio,
    matchMedia: b.win.matchMedia, getComputedStyle: b.win.getComputedStyle,
    requestAnimationFrame: b.win.requestAnimationFrame,
    cancelAnimationFrame: b.win.cancelAnimationFrame,
    requestIdleCallback: b.win.requestIdleCallback,
    cancelIdleCallback: b.win.cancelIdleCallback,
    alert: b.win.alert, confirm: b.win.confirm, prompt: b.win.prompt,
    // Standard-library globals scripts still need.
    URL, URLSearchParams, TextEncoder, TextDecoder,
    atob: b.win.atob, btoa: b.win.btoa,
    crypto: globalThis.crypto,
    Promise, Symbol, Map, Set, WeakMap, WeakSet, Proxy, Reflect, JSON, Math, Date, RegExp,
    Error, TypeError, RangeError, SyntaxError, ReferenceError,
    Array, Object, Function, String, Number, Boolean,
    ArrayBuffer, Uint8Array, Uint16Array, Uint32Array,
    Int8Array, Int16Array, Int32Array, Float32Array, Float64Array,
    DataView, BigInt, BigInt64Array, BigUint64Array,
    // NOTE: NO process, NO Buffer, NO require, NO global, NO __filename
    // ON PURPOSE. ``typeof process !== 'undefined'`` guards will see
    // undefined and skip the Node-detection branch.
    globalThis: undefined,
    // User overrides win. Place after the defaults.
    ...globals,
  };
} else {
  sandbox = {
    console: consoleProxy,
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    // Common globals scripts may expect. We do NOT inject DOM / fetch /
    // localStorage; those are browser-only and we want call sites to know
    // they need a real browser if their script touches them.
    globalThis: undefined,
    Buffer, TextEncoder, TextDecoder, URL, URLSearchParams,
    atob: (b) => Buffer.from(String(b), 'base64').toString('binary'),
    btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
    crypto: globalThis.crypto, // Node 19+ has WebCrypto on globalThis
    // User-provided globals win against the defaults below.
    ...globals,
  };
}
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox, { name: 'argusai-js-runner' });

let resultRaw;
let resultType = 'undefined';
let okFlag = true;
let errorMsg = '';
let stackMsg = '';

try {
  if (mode === 'eval') {
    // Evaluate an expression and return its value.
    const script = new vm.Script(`(${source})`, { filename: 'expr.js' });
    resultRaw = script.runInContext(context, { timeout: timeoutMs, breakOnSigint: true });
  } else if (mode === 'script') {
    // Run a script body verbatim — last-expression style not enforced;
    // the script should ``console.log(JSON.stringify(...))`` or set a
    // sentinel global (e.g. ``__result``) for the runner to pick up.
    const script = new vm.Script(source, { filename: 'main.js' });
    script.runInContext(context, { timeout: timeoutMs, breakOnSigint: true });
    resultRaw = sandbox.__result;
  } else if (mode === 'call') {
    if (!functionName) throw new Error('mode=call requires function_name');
    // Run the source first to define the function, then call it.
    const script = new vm.Script(source, { filename: 'main.js' });
    script.runInContext(context, { timeout: timeoutMs, breakOnSigint: true });
    const fn = sandbox[functionName];
    if (typeof fn !== 'function') {
      throw new Error(`global "${functionName}" is not a function (got ${typeof fn})`);
    }
    resultRaw = fn.apply(sandbox, args);
    // If it returned a Promise, race it against the remaining budget.
    if (resultRaw && typeof resultRaw.then === 'function') {
      const remaining = Math.max(50, timeoutMs - (Date.now() - t0));
      resultRaw = await Promise.race([
        resultRaw,
        new Promise((_r, rej) => setTimeout(() => rej(new Error(`call timed out after ${remaining}ms`)), remaining)),
      ]);
    }
  } else {
    throw new Error(`unknown mode: ${mode}`);
  }
} catch (e) {
  okFlag = false;
  errorMsg = (e && e.message) ? String(e.message) : String(e);
  stackMsg = (e && e.stack) ? String(e.stack) : '';
  if (
    mode === 'eval'
    && (errorMsg.includes('Unexpected token') || (stackMsg && stackMsg.includes('expr.js')))
  ) {
    errorMsg += (
      ' — eval 只接受单个 JavaScript 表达式，不能使用 var/let/const、require、'
      + 'function、console.log 或多行语句；请改用 mode=script，'
      + '返回值请设置 globalThis.__result = <value>'
    );
  }
  resultRaw = undefined;
  resultType = 'error';
}

// Classify + serialise.
let resultJson = null;
let resultSize = 0;
if (okFlag) {
  if (resultRaw === undefined) {
    resultType = 'undefined';
    resultJson = null;
  } else if (resultRaw === null) {
    resultType = 'json';
    resultJson = 'null';
  } else if (typeof resultRaw === 'string') {
    resultType = 'string';
    resultJson = resultRaw;
  } else if (typeof resultRaw === 'function') {
    resultType = 'function';
    resultJson = `[function ${resultRaw.name || 'anonymous'}]`;
  } else {
    resultType = 'json';
    resultJson = safeStringify(resultRaw);
  }
  resultSize = resultJson == null ? 0 : resultJson.length;
  if (resultJson != null && resultJson.length > MAX_RESULT_CHARS) {
    resultJson = resultJson.slice(0, MAX_RESULT_CHARS) + `...[truncated ${resultJson.length - MAX_RESULT_CHARS} chars by runner]`;
  }
}

const payload_out = {
  ok: okFlag,
  result: resultJson,
  result_type: resultType,
  result_size_chars: resultSize,
  stdout: capturedOut.join('\n').slice(0, 32_768),
  stderr: capturedErr.join('\n').slice(0, 32_768),
  error: errorMsg,
  stack: stackMsg.slice(0, 8_192),
  elapsed_ms: Date.now() - t0,
};
process.stdout.write(JSON.stringify(payload_out));
