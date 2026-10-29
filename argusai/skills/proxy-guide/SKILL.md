---
name: proxy-guide
description: Help users set up and use the passive proxy. Use when the user asks about proxy, passive scanning, traffic capture, MITM, or intercepting HTTP/HTTPS.
trigger-keywords:
  - "\\b(passive proxy|mitm|traffic capture|intercept)\\b"
  - "\\bproxy\\b.*\\b(start|stop|traffic|被动)\\b"
  - "被动代理|流量捕获|抓包.*代理"
---

## Proxy Setup

1. Call `get_proxy_status` / check GUI **HTTPS 证书** section.
2. Install the ArgusAI root CA (`install_proxy_ca` or GUI **安装根证书**). To remove trust later, call `uninstall_proxy_ca` or GUI **卸载根证书**. HTTPS MITM requires user trust — we do **not** promise interception of any specific application (certificate pinning, custom TLS stacks, etc.). Automatic CA install/uninstall is supported on **Windows and macOS** only.
3. Call `start_proxy` (default `127.0.0.1:8866`). ArgusAI automatically bypasses loopback traffic and configured AI endpoint hosts so the assistant keeps working while system proxy is enabled.
   - `intercept_mode`: `all` | `allowlist` | `denylist`
   - `scope`: comma-separated host patterns (`example.com`, `*.example.com`)
   - `deny_hosts`: hosts to exclude — honoured in **every** mode (and always wins over `scope`)
   - `mime_allow`: comma-separated MIME or kind filters (`video/*`, `api`, `application/json`)
   - `use_system_proxy: true` to set OS proxy automatically (restored on `stop_proxy`, Windows/macOS).
4. Tell user: set browser HTTP/HTTPS proxy to `127.0.0.1:8866` unless system proxy was enabled.
5. Call `get_proxy_status` to confirm `ca.ca_trusted` and `running`.

> **Sensitive data**: `<data_root>/proxy/` holds the root CA private key (`ca.key`) and per-host signing material under `proxy/certs/`. Treat this directory as a secret — never share, never commit, never copy off the machine. Anyone with `ca.key` can MITM TLS traffic from any client that has installed the ArgusAI root CA.

## During Capture

- User browses target normally.
- Call `get_proxy_traffic` for the newest flows (supports `resource_kind` filter: api, video, script, image, document).
- To find specific flows use `proxy_search_traffic(query)` — field syntax with regex, see skill **proxy-traffic-query** (`host:/^api\./ method:POST status:4xx resp.body:/token/`).
- Call `get_proxy_flow_detail(flow_id)` for headers and a body preview of one flow (text capped at 48 KB for the model; no inline media).
- Encrypted or encoded bodies: `proxy_decode_flow(flow_id)` shows zero-config decoding and ciphertext hints; build a decryption rule with skill **proxy-decrypt-workflow**.
- Call `get_proxy_findings` for passive findings.
- Call `export_proxy_har` to write a HAR 1.2 file under `<data_root>/proxy/exports/`.
- Call `save_proxy_flow_body(flow_id, part="response")` to save raw request/response bytes under `<data_root>/proxy/captures/`.
- GUI **另存为…** uses `save_proxy_flow_body_as` (native save dialog; not exposed as an AI tool).
- Use `open_proxy_exports_dir` / `open_proxy_captures_dir` to reveal saved files in the file manager.
- Analyzer checks: headers, cookies, CORS, info disclosure, SQL errors, secrets, tokens, cache.
- For meaningful flows, attach request/response summaries to a case with `artifact_add`.
- For replayable API traffic, convert the flow to a canonical request with `web_request_parse` and continue with `web_request_replay` / `web_request_mutate`.

## Stopping

Call `stop_proxy`. Traffic and findings preserved; system proxy settings are restored if they were changed.

Closing the ArgusAI app also attempts a fast proxy shutdown and restores system proxy. If the process is force-killed (Task Manager / kill), restore may not run — disable system proxy manually in Windows/macOS network settings.

## Limitations

- **WebSocket** is proxied passively: after the `101` upgrade, frames are relayed unchanged and recorded on the flow (`resource_kind: websocket`, `websocket_frames` with per-frame `dir`/`opcode`/`text`/`hex`; up to 2000 frames, ~4 KB captured each). Find them with `proxy_search_traffic("type:websocket")` and read the flow detail for frames.
- **HTTP/2** is MITM'd: the browser↔proxy leg negotiates h2 via ALPN and each stream is captured as a normal flow (`http_version: h2`); the proxy↔origin leg uses HTTP/1.1. **gRPC** (which rides h2 with binary framing) is captured as flows but not decoded. Bypass hosts that must not be intercepted with `bypass_hosts`.
- Bodies stream through the proxy at full size, but only the first **1 MiB** (decoded) of each body is kept for analysis, preview and saving. Saving a truncated body yields only that prefix; to get the full file, download it in the browser or app itself.
- Captured bodies share a **192 MiB** memory budget; the oldest flows lose their bodies first (`body_evicted`), and their metadata stays.
- Certificate-pinned native apps will fail TLS — this is by design.

## Troubleshooting

- No traffic → check browser/system proxy settings.
- HTTPS fails → install root CA (`install_proxy_ca`); confirm `ca_trusted` in status.
- Done auditing → uninstall root CA (`uninstall_proxy_ca`) so the system no longer trusts ArgusAI MITM.
- Chrome shows `ERR_CERT_INVALID` even after install → re-export the CA (extension upgrades the cert) and trust again.
- Too much noise → use `intercept_mode` + `scope` / `deny_hosts`, or `mime_allow` (e.g. `api,video/*`).
- Port in use → use different port. `start_proxy` now returns the bind error immediately instead of hanging.
- WebSocket frames captured but not streaming live to the GUI → the flow records frames in place; re-open the flow detail to refresh.
