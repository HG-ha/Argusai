---
name: web-crypto-tools
description: Use web search, page source extraction, encoding/decoding, hashing, HMAC, and JWT helper tools. Use when the user asks to search the web, inspect page source, decode/encode strings, calculate hashes, inspect JWTs, or analyze web payloads.
---

## Web And Crypto Tools

1. For public information lookup, call `web_search` with a concise query only when the user explicitly asks to search externally, look up public information, or find writeups. Do not use web search to solve CTF/lab targets by default.
2. For a known URL, prefer scoped tools:
   - `fetch_page_source` for raw HTML/source (no JS execution).
   - `extract_page_text` for title, visible text, links, forms, scripts (no JS execution).
   - `browser_probe` when the page builds DOM at runtime (SPA, hydration, JS-driven CTF levels). Returns rendered HTML, inline `<script>` bodies, links, forms, console output.
   - `browser_evaluate` when you need to read a JS-side value (e.g. `() => xss.get('xss')`, `JSON.stringify(window.__INITIAL_STATE__)`) or check that a specific runtime object exists. Pass `wait_for_selector` or `extra_wait_ms` if the value is only set after hydration.
   - If the value only exists inside the user's logged-in session, or the user wants you to look at the tab they already have open, switch to attach mode and use `browser_use_tab` instead (see `browser-attach-workflow`).
3. Always provide or infer `scope` for URL-fetching tools when the target is part of a scan or authorized test.
4. For transformations:
   - Base64, URL, HTML, hex: call `codec_transform`. Hex decoding accepts `0x` / `\x` prefixes, spaces, commas and line breaks.
   - Hexdump view (offset / hex / ascii) for binary payloads, file headers, shellcode, JWT raw fragments, multipart bodies, or any input that may contain non-printable bytes: call `hex_view`. Pass `input_format="hex"` if the input already looks like hex, `input_format="base64"` for base64, otherwise leave the default `"text"`.
   - Hashes: call `hash_text`.
   - HMAC/signatures: call `hmac_text`.
   - JWT inspection: call `jwt_decode`; explain that it does not verify signatures.
5. Do not claim encrypted data is decrypted unless a key/algorithm is available. For one-way hashes, explain that they can only be compared or cracked offline.
6. When analyzing payloads, show both the original and transformed value, and mention the exact operation used.
7. When you suspect non-printable bytes (e.g. control chars in a leaked secret, magic numbers like `\x89PNG`, raw shellcode, JWT signature bytes), use `hex_view` to surface them clearly instead of dumping mojibake from `codec_transform`.
8. For signing/encryption workflows, save known input/output pairs as artifacts or ReverseJob oracles. Use `js_signature_oracle_plan` when the goal is replaying signed Web requests.
