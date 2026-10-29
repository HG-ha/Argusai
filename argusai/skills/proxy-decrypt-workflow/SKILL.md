---
name: proxy-decrypt-workflow
description: Decrypt application-layer encrypted proxy traffic (AES/DES/3DES/SM4/RC4/XOR/RSA, base64/hex/gzip layers, custom JS) by building ArgusAI decryption rules. Use when captured requests/responses look encrypted or encoded, or the user asks to auto-decrypt traffic like Fiddler.
trigger-tools:
  - proxy_decode_flow
  - proxy_decrypt_rules_list
  - proxy_decrypt_rule_test
  - proxy_decrypt_rule_save
  - proxy_decrypt_script_rule_save
trigger-keywords:
  - "\\b(decrypt|encrypted|ciphertext)\\b.*\\b(traffic|request|response|body|proxy|flow)\\b"
  - "(解密|加密|密文).*(流量|请求|响应|报文|抓包)|自动解密"
---

## Proxy decryption workflow

HTTPS is already decrypted by the MITM CA. This skill is for the **next layer**: apps that encrypt or encode the body themselves.

### 1. Look before writing anything

1. Find candidates: `proxy_search_traffic` with e.g. `type:api resp.body:/^\s*\{?"?\w*"?:?\s*"?[A-Za-z0-9+\/=]{64,}/`.
2. Call `proxy_decode_flow(flow_id)`. Zero-config decoding already peels base64 / base64url / hex / URL / `\uXXXX` / JWT / gzip / zlib / zstd, per JSON field, form field and query parameter.
   - Items with `text` are solved; they are already searchable with `dec:`.
   - `ciphertext` entries give `cipher_hint`:
     - `block16`: AES or SM4.
     - `block8`: DES or 3DES.
     - `stream`: RC4, XOR, AES-CTR, or AES-GCM (data plus a 16-byte tag).
     - `openssl-salted`: CryptoJS / OpenSSL passphrase format. The key is derived from a password via EVP_BytesToKey, so it usually needs a `script` step.
3. Note **where** the ciphertext sits (JSON path, form field, query parameter, header) and **which part** (request/response). That becomes `field` and `apply_to`.

### 2. Recover the algorithm and key

Never guess keys. Get them from the client:

- `js-reverse` skill:
  - `js_crypto_detect` / `js_signature_candidates` on the bundle.
  - `browser_trace_crypto` or `js_runtime_hook` on `CryptoJS.AES.decrypt`, `crypto.subtle.decrypt`, `sm4.decrypt`.
  - Capture key, IV, mode and padding at runtime.
- Mobile apps: the `mobile-reverse` skill (Frida hooks on `javax.crypto.Cipher`).
- Prove it with `crypto_reproduce(algorithm, direction='decrypt', key, iv, data, expected=...)` against one captured ciphertext before writing a rule.

### 3. Write the rule

```json
{
  "name": "shop app AES",
  "match": "host:api.shop.example path:/v2/",
  "apply_to": "response",
  "field": "data",
  "steps": [
    {"op": "decode", "codec": "base64"},
    {"op": "sym", "algo": "aes-cbc", "key": "0123456789abcdef", "key_format": "utf8",
     "iv": "fedcba9876543210", "iv_format": "utf8"},
    {"op": "decompress", "algo": "gzip"}
  ]
}
```

- `match` uses the **proxy-traffic-query** syntax. Keep it narrow (`host:` plus `path:`) so the rule does not run on every flow.
- `field`:
  - `""` means the whole body.
  - A JSON path: `data`, `data.payload`, `list[*].enc`, `json.items[0].v`.
  - `form.NAME`, `query.NAME` (request only), `header.NAME`.
- `iv: "@prefix"` means the IV is the first block of the ciphertext (12 bytes for GCM).
- Use `padding: "none"` when the client uses zero / no padding.
- AES-GCM expects the 16-byte tag appended at the end of the ciphertext.

Step reference:

| op | Fields |
|---|---|
| `decode` | `codec`: base64 base64url hex url |
| `sym` | `algo`: aes-cbc aes-ecb aes-ctr aes-gcm des-cbc des-ecb 3des-cbc 3des-ecb sm4-cbc sm4-ecb rc4 xor; `key`, `key_format` (auto utf8 hex base64 base64url), `iv`, `iv_format`, `padding` (pkcs7 / none) |
| `rsa` | `private_key` (PEM), `padding` (pkcs1 / oaep / oaep-sha256), optional `password`; long data is decrypted block by block |
| `decompress` | `algo`: gzip zlib deflate zstd br |
| `slice` | `start`, `end` byte offsets (strip a prefix / tag / length header) |
| `script` | `code`: JS defining `function decrypt(input, ctx)` |

### 4. Script steps (bespoke algorithms)

Use a `script` step only when the built-in steps cannot express the algorithm: modified AES, custom key derivation, keys taken from response headers, or CryptoJS passphrases.

```js
function decrypt(input, ctx) {
  // input: Buffer (output of the previous step)
  // ctx: {part, location, url, method, status, requestHeaders, responseHeaders}
  // Available: Buffer, crypto, zlib, TextEncoder, TextDecoder, atob, btoa
  const key = Buffer.from(ctx.responseHeaders['x-key'] || '0123456789abcdef');
  const d = crypto.createDecipheriv('aes-128-ecb', key, null);
  return Buffer.concat([d.update(input), d.final()]).toString('utf8');
}
```

- Must be synchronous; it may return a string, Buffer/Uint8Array, or an object (JSON-encoded).
- Each call is capped at 2 s; there is no network or `require`.
- It runs in the managed Node runtime (ask the user to install it from Settings if it is missing).
- Pasting a library (for example the site's own CryptoJS) above `decrypt` is fine; the limit is 512 KB.
- Script rules can only be saved through `proxy_decrypt_script_rule_save`, which always asks the user to confirm.

### 5. Test, save, use

1. `proxy_decrypt_rule_test(rule, flow_id)` dry-runs without saving. Check `matched`, `targets` and each item's `ok` / `text` / `error`. The error names the failing step (for example `第 2 步（aes-cbc）`), so fix that step.
2. `proxy_decrypt_rule_save(rule, test_flow_id)` persists the rule to user settings and re-decrypts the whole history in the background. Update a rule by passing its `id` (from `proxy_decrypt_rules_list`; keys there are masked).
3. Decrypted plaintext:
   - Feeds the passive analyzer; its findings carry `metadata.source = "decrypted"`.
   - Is searchable with `dec:` and `resp.body:`. Example: `proxy_search_traffic("has:decrypted dec.resp:/\"idCard\":/")`.
4. Tell the user what was decrypted and where to see it: the **流量记录** inspector shows a **解密** view, and rules are managed under **配置 → 解密规则**.

### Rules of thumb

- Keys are secrets. They live only in the user's settings. Never paste a key into chat summaries, reports, or artifacts unless the user asks.
- One rule per app or endpoint family. Prefer several narrow rules over one broad rule with a script.
- If decryption yields binary, check the step order. A missing `decompress`, or a wrong `key_format` (hex vs utf8), is the usual cause.
- Request-side encryption works the same way: set `apply_to: "request"` and point `field` at the encrypted parameter.
