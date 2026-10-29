---
name: js-reverse
description: Use for any JavaScript reverse-engineering task — deobfuscation, source map recovery, webpack/Vite/Rollup unpacking, dynamic browser hooks, custom sign/encrypt scheme recovery, WASM disassembly, Python-only PoC generation. Use when the user asks "how does this site sign requests", "deobfuscate this bundle", "what algorithm is in this JS", "reproduce this token in Python", or anything similar.
trigger-keywords:
  - "\\b(deobfuscat|sourcemap|source map|webpack|js[- ]?reverse|webcrack)\\b"
  - "\\b(sign requests?|encrypt|token|wasm|bundle)\\b.*\\b(js|javascript|python poc)\\b"
  - "反混淆|签名.*(算法|参数)|逆向.*js|webpack.*打包"
---

## JS Reverse Workflow

This skill orchestrates ArgusAI's full reverse-engineering toolchain. Every serious task creates a **Case** for cross-domain evidence and a **ReverseJob** for JS/WASM-specific artifacts, hooks, findings, and reproducers.

The hard rule: **bytes go to disk, not into chat**. Raw bundles, WAT dumps, base64 WASM, and multi-MB JSON traces all live as artifacts on disk; you reference them by path or job_id, never by inlining them into a prompt.

### Always start here

1. **Open or create the Case.** Use `case_create(target_type='url', domain='web')` for new investigations. Case artifacts/evidence are the cross-session proof chain.
2. **Open or create the ReverseJob.** Call `reverse_job_list` to see if one already exists for the target's eTLD+1. If not, call `reverse_job_create({target, scope, intent})`. Every JS-specific artifact later attaches to this job.
3. **Prefer source when the matching frontend repository is available.** If the user provides a local source root for the same app, call `codebase_scope_set`, inspect `codebase_tree` / `codebase_entrypoints`, and use `codebase_search` / `codebase_read` before pulling large remote bundles. Keep ReverseJob for deployed bundles, generated output, missing source maps, obfuscation, or source/build mismatches.
4. **Snapshot the page.** Call `browser_probe(url=...)` (or `browser_use_tab` in attach mode — preferred when the user is already logged in). Save the rendered HTML, inline `<script>` bodies, link list, and form list into the job via `reverse_artifact_add(job_id, content=..., kind='entry')`; attach high-level snapshots to the case with `artifact_add` when they support the final conclusion. Treat page code as business evidence too: extract visible text, menus, buttons, forms, route links, data attributes, inline config, feature flags, role labels, and API base URLs before deciding what to reverse.
5. **Inventory the JS surface.** Use `js_asset_inventory(html=..., source_url=...)` to enumerate every external script + map URL. For each interesting asset, fetch with `js_fetch_asset(url=..., meta_only=true)` first to learn size, then page through with `offset` / `limit`, or pull the full body only when you've confirmed it's small. Add each as an artifact tagged `entry` / `chunk` / `worker` / `inline` / `wasm`.
6. **Bootstrap the business surface before deep reversing.** Extract APIs/routes and page semantics from source code, rendered HTML, static JS, proxy/HAR/browser traces, or fetch/XHR hooks with `js_api_extract` and `api_inventory_build`, then call `business_surface_extract`. The Agent must review candidate assets, operations, roles, UI labels, forms, and evidence gaps before choosing which bundles, hooks, signatures, or protocol flows deserve priority.

After step 6 you have both a technical reverse surface and an initial business surface. Now decide a path.

### Business-aware reverse default

Web reverse is not only "find the signing function". For every web target, keep a business hypothesis alongside the JS/WASM hypothesis:

- Create or update todos in three layers: reverse primitives (bundles, obfuscation, signing, anti-bot), business flows (login, order, payment, approval, tenant, export, upload, admin, webhook, role transition), and security validation (authz, tampering, replay, injection, CORS/CSRF, protocol issues).
- Prioritize reverse work by business impact: a signed coupon/order/payment/admin request is more valuable than an isolated utility hash; a role/tenant boundary endpoint is more valuable than a public config fetch.
- Read page semantics before chasing low-level code: UI copy, navigation hierarchy, form names, button labels, route names, embedded config, and feature flags often reveal the real business operation behind an obfuscated API or signature function.
- Treat `business_surface_extract` output as candidate evidence, not a conclusion. If the surface is weak, add discovery todos for authenticated browsing, role-specific browsing, proxy capture, HAR import, or dynamic fetch/WebSocket hooks.
- Re-run `api_inventory_build` and `business_surface_extract` after dynamic harvesting, login, role changes, route transitions, or newly unpacked chunks. Do not let the first static inventory become the final business model.

### Decision tree

For each artifact, ask these questions **in order**. Stop at the first match.

#### Q1 — Is a source map available?

1. Call `source_map_discover(source_url=..., code=...)` to find any `# sourceMappingURL=` comment, sibling `.map`, or data URI.
2. If a candidate exists, fetch it (with `js_fetch_asset` or `http_curl`) and pass to `source_map_recover(map_text=..., source_url=...)`.
3. If the map is **indexed/sectioned** (has `sections` or `x_google_ignoreList`), use `source_map_recover_indexed` instead — it walks the sub-sections and surfaces partial originals.
4. If it's a `data:application/json;base64,...` URI, use `source_map_decode_data_uri(uri=...)` first.
5. If the map is auth-protected, switch to attach mode (see `browser-attach-workflow` skill) and fetch via `browser_evaluate`.

If sources are recovered: **stop**. You have the original code; jump to Q6 for high-level analysis. Save each recovered original as its own artifact.

#### Q2 — Is it packed / trivially encoded?

- Use `js_decode_literal(text=...)` on a suspicious string literal — handles `\x` / `\u` / hex / base64 quickly without firing up Node.
- Use `js_deobfuscate(code=...)` for safe static passes (constant folding preview, dead-code stripping, string-array detection).
- For p.a.c.k.e.r. style `eval(function(p,a,c,k,e,d){...}(...))`, call `js_eval_unpack(code=...)` — it identifies the wrapper, extracts the literal, and unpacks safely.
- For HEAVY cases (obfuscator.io, control-flow flattening, rotated arrays, multi-stage string arrays), call `js_deobfuscate_heavy(code=..., job_id=...)` — that's the webcrack-backed Node deobfuscator. It writes the unpacked output as a new `kind='deob'` artifact.

#### Q3 — Is it a webpack / Vite / Rollup / Parcel / SystemJS / browserify bundle?

- Call `js_detect_bundler(code=..., source_url=...)` first — that's the canonical bundler classifier (handles webpack4/5, vite, rollup, parcel, systemjs, browserify, obfuscator.io). It returns the detected family + confidence.
- For **webpack**: call `webpack_modules_extract(code=..., source_url=...)` to inventory modules, then `webpack_module_read(code=..., module_id=...)` for each one you care about.
- For **other bundlers**: call `js_unpack_bundle(code=..., job_id=...)` — runs the right unpacker (webcrack handles webpack/browserify; for vite/rollup it falls back to chunk splitting). Each emitted chunk is auto-added as a `kind='chunk'` artifact under the job.

#### Q4 — Is it obfuscated (`obfuscator.io`, control-flow flattening, mangled names)?

- Run `js_string_array_map(code=...)` to recover the string dictionary.
- Run `js_constant_fold_preview(expression=...)` to check whether a suspicious arithmetic expression folds to a constant before you trust the value.
- Run `js_control_flow_hints(code=...)` to find switch-dispatcher shapes and rotated-array patterns.
- For the heavy cases, `js_deobfuscate_heavy` (webcrack + restringer) is the canonical answer — same call as Q2.

For AST-aware investigation:
- `js_ast_inventory(code=...)` — list every function / class / top-level let with offsets.
- `js_ast_string_array_decode(code=...)` — pull out the obfuscator-style decoded array with confidence scores.
- `js_function_dependency_graph(code=..., function_name=...)` — see who calls who.

#### Q5 — Is there a `while (true) switch (op)` dispatcher reading from a byte-code array?

That's a **JSVMP** (JavaScript Virtual-Machine Protected) script. Do not assume browser replay is the final answer. First try to unpack, patch, instrument, collect oracle samples, and reproduce the inner transforms in Python. Only fall back to boundary replay when the VM depends on hard browser state, live server state, or non-reproducible fingerprint entropy.

1. Use `js_control_flow_hints(code=...)` to confirm the dispatcher shape and locate the bytecode array offset.
2. Save the bundle as a ReverseJob artifact, then use `js_runtime_create` + `js_runtime_load(path=...)` for offline execution. Large bundles do not go through `sandbox_eval_js`.
3. Patch anti-Node or anti-debug gates with `js_runtime_patch`, and inspect `js_runtime_patches` so the patch history is reproducible.
4. Install probes with `js_runtime_instrument` or `js_runtime_hook`, drive candidate functions with `js_runtime_call` / `js_runtime_eval`, then `js_runtime_drain`.
5. Persist every useful hit with `reverse_oracle_add`; do not leave oracle values only in chat or temporary files.
6. Use `python_reproducer_verify` against persisted oracle stages to find the first divergent layer. If internal reproduction fails due to real browser state, then hook the boundary with `browser_hook_function` and document the limitation.

#### Q6 — Now that source is readable, what is it actually doing?

Look for the high-value primitives. Use grep first, never read the whole file:

- `js_search(code=..., pattern='CryptoJS.|crypto.subtle.|KJUR.|jsrsasign|JSEncrypt')` — crypto entry points.
- `js_crypto_detect(code=..., source_url=...)` — classifier that flags AES / HMAC / RSA / SM4 / DJB2 / MD5 chains and where the inputs come from. Single best starting move when the goal is "reproduce the signature".
- `js_api_extract(code=..., source_url=...)` — every `fetch(...)` / `XMLHttpRequest` / `axios.post(...)` URL and method.
- `api_inventory_build(items_json=..., source='static_js')` — normalize extracted API endpoints together with browser/proxy/HAR traffic so replay and protocol probes start from one inventory.
- `api_spec_import(spec=..., base_url=...)` — when the app ships an OpenAPI/Swagger spec or Postman collection, parse it into the same inventory shape with replayable `request_template`s and parameter lists.
- `business_surface_extract(items_json=...)` — structure extracted endpoints into candidate business signals with evidence ids and gaps. Treat this as input for Agent reasoning, not final classification; prioritize signature-sensitive flows only after reviewing UI/API context.
- `js_signature_candidates(code=..., source_url=...)` — heuristic list of functions that look like they build `X-Sign` / `X-Token` style headers.
- `js_dependency_trace(code=..., symbol='theFunctionName')` — pull the call graph slice around one function, with surrounding context.

For surgical reads use `js_slice_context(code=..., symbol=...)` or `js_slice_context(code=..., offset=..., context=1600)` to grab just the relevant window.

### Dynamic harvesting (the most powerful lever)

Any value that exists at runtime can be captured even if the producing code is fully obfuscated. Hooks persist across navigation (CDP `addScriptToEvaluateOnNewDocument`), so reloads don't lose them.

Use this when:

- The site checks `navigator.webdriver`, canvas / audio / WebGL fingerprint, mouse trajectory (coverage A2–A4).
- A signed cookie / header is set dynamically (A7, A8) and you need a fresh value to feed into a crypto reproducer.
- `eval(...)` / `new Function(...)` produces code at runtime (O10).
- The page corrupts state if `location.host` doesn't match (O9) — hook the getter.
- Bytecode for a JSVMP is fetched from XHR / WebSocket / `WebAssembly.compile` (V4, W5).

#### Recipe-based (one call → 1–4 hooks installed)

This is the FAST path. We ship pre-baked recipes for the common patterns. Always check here first:

1. `reverse_recipe_list()` — catalogue. Pick the id matching your scenario. Common recipes:
   - `canvas_fingerprint`, `canvas_fingerprint_spoof`, `audio_fingerprint`, `webdriver` — anti-bot probes.
   - `mouse_trajectory` — mouse-move trajectory recorders.
   - `signed_header`, `signed_cookie`, `eval_harvest` — dynamic signing and runtime code generation.
   - `location_host_getter` — origin checks.
   - `websocket`, `fetch`, `eventsource` — transport interception.
   - `webassembly_compile` — captures every `WebAssembly.compile/instantiate/(*)Streaming` ArrayBuffer into `globalThis.__argusai_wasm_samples` so `browser_save_wasm` can pull them out.
   - `vm_dispatcher_trace` — instruments the inner dispatcher of a JSVMP-style switch.
2. `reverse_recipe_apply(job_id, recipe_id)` — persists every hook into `job.json`. Does NOT inject yet.
3. `browser_hook_reattach(job_id)` — actually injects into the live browser (use after `browser_attach_existing` or `browser_launch_for_attach` from `browser-attach-workflow`).
4. Drive the page (`browser_use_tab` click / reload / submit).
5. `browser_hook_drain(job_id)` — pull hits from the in-page buffer into `<job>/hooks/<hook_id>.hits.jsonl`. Then `reverse_hook_hits(job_id, hook_id, tail=200)` to read what was captured.
6. Feed captured fetch/XHR/WebSocket/EventSource URLs, methods, headers, and bodies back into `api_inventory_build`, then re-run `business_surface_extract` before choosing the next hook or replay target.

#### Custom hooks (when no recipe fits)

For ad-hoc functions or properties:

- `browser_hook_function(job_id, target='window._sign')` — records `args`, `return`, `stack`, `duration` for every call. Set `replace_return_src='return false;'` to also spoof the return (e.g. `navigator.webdriver`). Set `filter_src='return args[0].includes("/api/")'` to record only matching calls.
- `browser_hook_property(job_id, target='navigator.webdriver')` — records every get/set. Set `replace_get_src='(orig)=>false'` to lie to the page.

After installing, run `browser_hook_list(job_id)` to see what's active (with live hit counts + dropped counters). Use `browser_hook_remove(job_id, hook_id)` to uninstall + persist the removal.

Use `js_hook_template_generate(function_name=..., expression=...)` to get a ready-to-paste `replace_return_src` template for the trickier patterns.

### Signed request replay

When the goal is "make a valid mutated request", combine ReverseJob oracles with Web replay:

1. Save the baseline HTTP request as a case artifact.
2. Call `js_signature_oracle_plan(job_id=..., request_artifact_ref=..., signature_fields='sign,timestamp,nonce')`.
3. Apply fetch/XHR/crypto recipes, drive the page, then `browser_hook_drain`.
4. Persist runtime samples with `reverse_oracle_add(stage='signature')`.
5. Mutate the request with `web_request_mutate`; regenerate the signature with the captured oracle or `js_runtime_call`.
6. Replay with `web_request_replay`, compare with `web_response_diff`, and save decisive results with `evidence_add`.

### Reproducing crypto (P3 — Python-first, browser-last)

Once you know the algorithm, the FINAL PoC must NOT need the user to open Chrome.

1. **Identify the primitives.** Use `js_crypto_detect(code=..., source_url=...)` — it reports the algorithm (AES-CBC, HMAC-SHA256, RSA-OAEP, SM4, DJB2, …) and where the inputs come from.
2. **Capture one (input, output) pair from the live page.** Either via `browser_trace_crypto(url=...)` (snapshot mode — clicks through the page recording `crypto.subtle` and CryptoJS calls), or via a hook drain from the previous step.
3. **Verify the Python reproduction**. Call `crypto_reproduce(algorithm='aes-cbc', direction='encrypt', key=..., iv=..., data=..., expected=...)`. Supported: `xor | aes-cbc | aes-ecb | aes-ctr | aes-gcm | des-cbc | des-ecb | 3des-cbc | 3des-ecb | rc4 | hmac-sha256 | hmac-sha1 | hmac-sha512 | hmac-md5 | sha256 | sha1 | sha512 | md5`. Inputs accept hex / base64 / base64url / utf-8. If `matched: true`, the returned `python_snippet` is verified and ready for the PoC. If `matched: false`, the response's `match_hint` points at the first diverging byte — usually a key/IV encoding mismatch.
4. **Cross-check from the JS side.** Sanity-check candidate hashes with `hash_text(text=..., algorithm='sha256')` and `hmac_text(text=..., key=..., algorithm='sha256')` from `web-crypto-tools`.

For decomposed chains (e.g. `base64(reverse(aes_cbc(input)))`), call `crypto_reproduce` once per layer and verify each step.

### When the JS itself must run (but a browser is overkill)

You have an obfuscated decoder you can't easily port to Python — e.g. the deobfuscated `_decode_payload(packed)` function does 30 XOR-and-rotate steps. Don't reach for a browser; run it in the managed Node sandbox. If Node is absent, approve the single sandbox-environment installation card; the same tool call continues after installation.

- `run_node_script(path=<bundle.js>, mode='script')` — preferred for large source; `source=<js>` is only for small snippets. Set `globalThis.__result = ...` to surface a value.
- `run_node_script(source=<js>, mode='eval')` — evaluate one expression.
- `run_js_function(source=<js>, function_name='entryFn', args=[<json>])` — define the source, call one named function, return JSON. Promises are awaited.

For large bundles or iterative debugging, prefer the hosted runtime instead of one-shot Node:

1. `js_runtime_create(session_id='target-main', env='browser', page_url='https://target/login')`.
2. `js_runtime_load(session_id='target-main', path='<sandbox>/bundle.deob.js')` — loads 700 KB+ bundles once and keeps globals alive.
3. `js_runtime_sources(session_id='target-main')` to get source id / version / hash.
4. `js_runtime_patch(session_id='target-main', source_id='src-1', mode='string'|'regex'|'offset', ...)` for anti-debug gates; verify with `js_runtime_patches`.
5. `js_runtime_instrument(session_id='target-main', targets_json='["_decodeOrSign"]')` or `js_runtime_hook(...)` to record args / return / stack as oracle records.
6. `js_runtime_call(session_id='target-main', target='entryFn', args_json='[...]')` or `js_runtime_eval(expression='...')`.
7. `js_runtime_drain(session_id='target-main')`, then immediately `reverse_oracle_add(job_id=..., records_json=...)`. Use `reverse_oracle_list/read/compare` for paging and checks.
8. `python_reproducer_verify(job_id=..., script_path='reproducer.py', stage='...')` to validate Python pure-computation code layer by layer.

Use this when you need the same style of debugging as a CDP browser session, but for local/offline JS: load once, patch repeatedly, harvest oracle samples, and compare a Python reproduction layer by layer. It is not a real DOM; switch to Playwright/CDP only when the algorithm truly depends on real layout, Canvas/WebGL/Audio entropy, or solved user session state. If Canvas/WebGL/Audio values are needed but deterministic, collect them first with `js_runtime_sample_profile(...)` and pass the returned `profile_json` into `js_runtime_create(profile_json=...)`.

**Anti-detection guards.** Many bundles refuse to initialise outside a real browser using shapes like:

```js
var _CT9PK = !!(typeof process !== 'undefined' && process.versions && process.versions.node);
var _PCCjKzz = !!(typeof window !== 'undefined' && typeof document !== 'undefined');
if (_CT9PK || !_PCCjKzz) return;  // <-- aborts the decoder
```

When you see this pattern, switch the runner to browser mode in **one parameter**:

```
run_node_script(source=..., env='browser', page_url='https://target.com/login')
```

That injects stub `window` / `document` / `navigator` / `location` / `history` / `screen` / `localStorage` / `sessionStorage` / `performance` / `fetch` / `XMLHttpRequest` AND hides `process` / `Buffer` / `require` / `global`. Both guards pass. NOT a real DOM (no parser, no layout, no event loop semantics) — only escalate to `sandbox_eval_js` (Playwright) if the script genuinely calls `document.body.innerHTML = ...` and reads back parsed children.

Use `page_url` to seed `location.href` when the script reads `location.hostname` / `location.protocol`. Override `user_agent` if it sniffs `navigator.userAgent`. For deterministic fingerprint-sensitive code, pass the same `profile_json` returned by `js_runtime_sample_profile` to `run_node_script`, `run_js_function`, or `js_runtime_create`.

For browser traces, use `browser_trace(out_path='trace.jsonl', max_events=100000)` or `browser_trace(save_to_job=<job_id>, max_events=100000)` when you need complete evidence. The inline response is only a preview; persisted JSONL is the source of truth.

### One-click Python PoC delivery (P3 finale)

Every meaningful reverse-engineering session ends with a self-contained Python bundle the user can run forever:

```
create_python_poc(
  out_dir='',  # defaults to <session_sandbox>/poc
  title='Replay signed login',
  description='Reproduces site X-Sign and POSTs a fresh login.',
  walkthrough='1. ... 2. ...',  # for README.md
  run_py='<full run.py source — pulls the verified python_snippet from crypto_reproduce + an http_request_impersonate call>',
  requirements=['curl_cffi>=0.7', 'requests>=2.34'],
  usage_args='--user alice --pass <password>',
)
```

The user gets `run.py + requirements.txt + README.md` (+ optional `helpers/`) on disk in their session sandbox. They run it with `pip install -r requirements.txt && python run.py ...`. No browser. No Chrome devtools.

If the PoC needs a captured JS function, call `run_js_function(source=..., function_name=..., args=...)` through ArgusAI. It uses the shared managed Node base with session-isolated cwd/HOME/cache; an exported standalone PoC should port the function or document its own Node dependency instead of assuming ArgusAI vendor files.

### Transport interception

- WebSocket framed bytecode: run `websocket_handshake_probe` to confirm the endpoint and auth headers, apply the `websocket` recipe, then drain. The recipe hooks both `WebSocket.prototype.send` and `onmessage`. Feed captured frames into `websocket_replay_plan` before any mutation.
- EventSource / SSE: apply the `eventsource` recipe.
- gRPC-Web / `application/grpc-web+proto`: run `grpc_web_analyze` on captured headers/body, then `protobuf_field_preview` on individual messages. If schemas are reachable from JS bundles, save them as artifacts and reference them from the PoC.
- For TLS / HTTP2 fingerprint (JA3, Akamai BMP): **do NOT** try to reproduce inside a browser. Use `http_curl` / `http_request_impersonate` directly — they're already TLS-impersonated.

### WASM workflow (P5) — bytes go to disk, never to chat

The classic mistake is to ask the browser for `Array.from(uint8).map(b=>b.toString(16))` or base64 the whole module through `browser_evaluate`. A 6 KB module already produces 8 KB base64 / 12 KB hex, which gets sliced by the value cap, and the model receives an UNUSABLE truncated blob. Use the disk-based path instead:

1. Apply the WASM recipe: `reverse_recipe_apply(job_id, recipe_id='webassembly_compile')`. The recipe's side-effect snippet stashes every captured `ArrayBuffer` into `globalThis.__argusai_wasm_samples`.
2. `browser_hook_reattach(job_id)` to inject; trigger the WASM load (page interaction).
3. `browser_save_wasm(out_path='<sandbox>/<name>.wasm', expression='globalThis.__argusai_wasm_samples[0]')` — streams the bytes chunk-by-chunk into the sandbox and returns ONLY size + sha256 + magic. No binary in the chat.
4. `wasm_inspect(path=...)` for instant triage (magic / version / sections / imports / exports counts) — pure Python, no WABT needed.
5. `wasm_disassemble(path=...)` for low-level WAT (uses managed `wasm2wat`), `wasm_decompile(path=...)` for the preferred high-level C-like rendering (uses managed `wasm-decompile`). Both write text artifacts next to the input and return only a short preview.
6. `wasm_strings(path=..., kind='data')` for embedded literals; `wasm_call_graph(path=...)` for dispatchers / hotspots — both pure Python.
7. Navigate the artifacts with `read_text_file(path=..., offset=..., limit=200)` and `grep_text(path=..., pattern=...)` — never inline the multi-MB WAT.

Identify the toolchain from the imports: `env._emscripten_*` → Emscripten; `__wbindgen_*` → Rust wasm-bindgen; `runtime.*` + `syscall/js.*` → Go; everything else → AssemblyScript or hand-written.

WABT is not bundled. The first `wasm_disassemble` / `wasm_decompile` call raises a one-time confirmation card that downloads it (~5 MB) into `data_root/tools/runtimes/wabt`, then continues with the original tool; users can also install or repair it from Settings → Sandbox runtimes, and you may call `runtime_install(runtime='wabt')` directly. `wasm_inspect`, `wasm_strings`, and `wasm_call_graph` are pure Python and work with no install at all — reach for them first.

### File I/O — grep first, read second

NEVER dump a whole artifact into your context. The cost of one over-eager 200 KB read compounds across rounds.

The right flow:
1. `stat_text_file(path=...)` if you don't know the size. `line_count == 1` (or close) means it's minified — switch to the character-oriented tools below.
2. `grep_text(pattern=..., path=...)` (or `grep_text_dir(pattern=..., root=..., glob='*.js')`) to locate hits. Returns `path:line:text` records; defaults are tiny.
3. `read_text_file(path=..., offset=<line>, limit=40)` for the matched region. Limit stays small (default 200 lines).

For **minified / single-line** files (typical for compiled bundles, packed JSON, base64 blobs):
- `grep_text(pattern=..., path=..., mode='context', context_chars=400)` — returns `{offset, end, before, match, after}` windows that ignore newlines.
- `read_text_slice(path=..., offset_chars=..., length=...)` — raw character window, no line numbers. Page with `offset_chars += length_returned`.
- `extract_strings(path=..., kind='url'|'base64'|'jwt'|'data_uri'|'hex'|'high_entropy')` — pulls just the *data* out of literals, each with its `offset` so you can `read_text_slice` for surrounding code.

Triage flow for a 4 MB minified bundle:
```
stat_text_file → extract_strings(kind='url') → extract_strings(kind='high_entropy')
              → grep_text(mode='context', pattern='secretKey') → read_text_slice around offsets
```

For artifacts attached to a ReverseJob, `reverse_artifact_read(job_id, artifact_id, offset_bytes=..., max_bytes=...)` pages through the stored copy and reports `more / end_bytes` so you know when to continue. You can also grab the underlying `path_abs` and switch to the line-oriented tools above for cheaper reads.

### Anti-bot, fingerprinting, captchas

- TLS / HTTP2 fingerprint (JA3, Akamai BMP): use `http_curl` / `http_request_impersonate`. Already impersonated.
- Canvas / WebGL / audio / mouse fingerprint: apply the matching recipe (`canvas_fingerprint_spoof`, `audio_fingerprint`, `mouse_trajectory`) and feed fixed values.
- `navigator.webdriver` and trivial bot flags: the browser profile already neutralises them; verify with `browser_evaluate(expression='navigator.webdriver')` before installing extra hooks.
- Commercial CAPTCHAs (Cloudflare Turnstile, hCaptcha, GeeTest, Tencent, FunCaptcha): solving is **out of scope**. Detect with `captcha_detect(html=..., code=..., source_url=...)`, then either ask the user for a fresh token or fall back to attach mode and reuse their solved session.

### Source recovery from stripped builds

When maps are stripped and obfuscation is heavy:
1. Pull the bundle as an artifact.
2. Run the full Q3 + Q4 chain (`js_detect_bundler` → `js_unpack_bundle` → `js_deobfuscate_heavy` + `js_string_array_map` + `js_ast_string_array_decode`).
3. Look for **path leaks**: webpack paths sometimes survive in `/* harmony export */` comments, `__webpack_require__("./src/foo/bar")` strings, or runtime error stacks. Use `grep_text` to scan.
4. Pair surviving function shapes with known-good versions of common libraries (lodash, axios, jsrsasign) by signature using `js_function_dependency_graph`.
5. Save the result back into the job as `kind='recovered'`.

### Saving conclusions

Every meaningful finding (e.g. "this site signs requests with HMAC-SHA256(secret, ts||nonce||url||body)") MUST be saved with `reverse_finding_add(job_id, title=..., body=..., kind='algorithm'|'flow'|'vuln'|'fingerprint'|'note', confidence='unverified'|'probable'|'verified', coverage_id='C9')`. Findings are what the user comes back to read; raw artifacts are not.

Final reproducer scripts go into `<job>/reproduce/` via `reverse_reproduce_add(job_id, name=..., content=..., lang='python', from_finding=..., verified=true)` — they show up in the panel's **Reproduce** tab — AND/OR you ship a one-click bundle via `create_python_poc(...)` in the session sandbox. For a serious deliverable, do both: a small per-step reproducer attached to the job, and the consolidated PoC for the user to run.

### What this skill will NOT do

- It will not solve commercial CAPTCHAs (Turnstile / hCaptcha / GeeTest / Tencent / FunCaptcha). Detect, then ask the user for a token.
- It will not de-virtualise native PE / ELF binaries (VMProtect, Themida) — JavaScript only.
- It will not run symbolic execution over arbitrary JS (no z3, no angr).
- It will not statically decompile JSVMP bytecode — the toolkit deliberately exposes no `js_vm_decompile` tool. Use dynamic harvesting (Q5) instead.
- It will not include `protobuf` decoders out of the box for gRPC-Web — capture the bytes, write the decoder yourself in `run.py`.
