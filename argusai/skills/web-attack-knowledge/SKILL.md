---
name: web-attack-knowledge
description: Practical web attack knowledge base for authorized testing. Use when analyzing XSS, SQL injection, SSRF, file inclusion, command injection, SSTI, IDOR, auth bypass, CSRF, CORS, deserialization, XXE, open redirect, upload bypass, request smuggling, cache poisoning, or choosing payloads and next validation steps.
inject-priority: 4
trigger-keywords:
  - "\\b(xss|ssrf|ssti|idor|csrf|cors|xxe|lfi|rfi|deserializ|smuggling|cache poison)\\b"
  - "\\b(sql injection|command injection|file inclusion|open redirect|upload bypass)\\b"
  - "跨站|文件包含|命令注入|请求走私|缓存投毒"
---

## Web Attack Knowledge Base

Use this knowledge only for authorized testing inside the user's declared scope. Prefer evidence from scan findings, page source, proxy traffic, and scoped probes over public writeups or guesses.

This knowledge base is aligned with common public testing references such as OWASP WSTG/ASVS, PayloadsAllTheThings-style payload methodology, and PortSwigger Web Security Academy vulnerability categories. Use it as internal methodology, not as permission to attack out-of-scope systems.

### General Workflow

1. Identify input surfaces: URL params, path params, fragments, forms, JSON bodies, headers, cookies, file uploads, redirects, and client-side state.
2. Establish a baseline request/response before injecting payloads. For replayable requests, use `web_request_parse` and `web_request_replay`.
3. For API-heavy targets, actively collect evidence from browser/proxy/HAR/static-JS sources with `browser_trace`, `get_proxy_traffic`, `js_api_extract`, and `api_inventory_build`, then run `business_surface_extract` to structure candidates and evidence gaps.
4. Treat business candidates as additive hints, not conclusions: keep generic baseline testing for SQLi, XSS, SSRF, CORS, CSRF, upload, auth, and protocol issues, then let the Agent create evidence-backed business-specific todos for flows such as payment, order, tenant, coupon, approval, export, upload, admin, and webhook.
5. Change one variable at a time with `web_request_mutate` where possible. Record payload, location, response delta, status code, timing, redirect, DOM reflection, browser signal, or server error.
6. Compare with `web_response_diff` for API/body differences and save decisive baseline/mutated material as artifacts.
7. Confirm exploitability with the least invasive proof. Do not escalate to destructive actions.
8. Explain results as: input surface -> business context -> payload -> observed signal -> impact -> remediation.
9. End with a copy-paste-ready test case whenever possible: exact payload, full URL, browser console snippet, curl command, or minimal local PoC page. **CORS is the exception**: do not use a local HTML file as proof of exploitability because `file://` has `Origin: null`; use `cors_exploit_verify` for decisive browser evidence. If the test failed or the target is patched, include a reproducible negative test and the exact reason it does not execute.

### Browser-driven Testing Modes

Two browsers are available; pick the right one before running `browser_probe` / `browser_evaluate` / `browser_trace`:

- **Managed headless Chromium** (default) — cold session, no user cookies, fingerprint chosen via `profile=` parameter. Use for unauthenticated targets, scans, fuzzing, and anything that should not touch the user's real accounts.
- **Attached real browser** (CDP) — drives the user's actual Chrome / Edge / Brave with all their cookies, logged-in sessions, extensions, and currently open tabs. Use only when the target requires the user's real session (SSO, MFA already completed, captcha just solved) or when the user explicitly asks you to look at a tab they have open. See `browser-attach-workflow` for the full procedure, safety rules, and decision tree.

When attached, use `browser_use_tab` only to inspect or operate on a specific existing tab. For new navigation, prefer `browser_open_tab(url=..., reuse_existing=true)` so the assistant creates/reuses a ArgusAI-owned tab instead of overwriting a user tab. `browser_list_tabs` returns window-aware `windows` plus flat `tabs`; when multiple windows exist, select by `target_id` or `window_id + tab_index`, not the legacy global `index`. Never close tabs the user opened unless they explicitly ask — then use `browser_close_owned_tabs(target_ids=[...], force=true)` after confirmation, or `browser_mark_tabs_owned` + `browser_close_owned_tabs`. At the end of a browser-driven task, call `browser_list_tabs` and then `browser_close_owned_tabs` to close only tabs marked `ai_owned=true`.

### XSS

- Reflected XSS: payload appears in the response/DOM and executes after navigation or user interaction.
- DOM XSS: source such as `location`, `hash`, `postMessage`, storage, or URLSearchParams flows into sinks such as `innerHTML`, `document.write`, `eval`, `setTimeout(string)`, `srcdoc`, or event attributes.
- Stored XSS: payload persists and executes for later viewers.
- Evidence hierarchy: dialog/canary execution > DOM event execution > dangerous raw reflection > encoded reflection.
- Context matters:
  - HTML text: try tag injection such as `<svg/onload=...>`.
  - Attribute value: try quote breakout plus event handler.
  - JavaScript string: try quote/backtick breakout and statement termination.
  - URL attribute: test `javascript:` only where browser and CSP allow it.
- If payload reflects but does not execute, inspect encoding and surrounding context before claiming high severity.
- Fix with context-aware output encoding, safe DOM APIs, input validation for URLs, and CSP as defense in depth.

### SQL Injection

- Test numeric, string, and order-by contexts separately.
- Signals: SQL error strings, boolean response delta, time delay, UNION column mismatch/success, or auth bypass.
- Use safe payloads first: single quote, double quote, boolean true/false, `ORDER BY`, and time-based probes with short delays.
- Distinguish in-band, inferential/blind, time-based, and out-of-band SQLi. If no visible error appears, compare response shape and timing against baseline.
- For NoSQL/ORM injection, look for JSON operators, object injection, filter bypass, type confusion, or query language fragments in parameters.
- Do not run destructive payloads. Avoid `DROP`, data modification, or heavy time delays.
- For sqlmap-level automation, use ArgusAI's bundled sqlmap tools (`sqlmap_status`, `sqlmap_plan_scan`, `sqlmap_run_scan`) so execution stays scoped, confirmed, and policy-filtered. If the bundled copy is missing in development, `sqlmap_install_update` is a fallback. Do not call an arbitrary system sqlmap binary.
- **Lab-only bulk extraction discipline.** In intentionally vulnerable labs or explicitly authorized proof exercises, when you have a working injection point and need one bounded proof value, prefer techniques that return the value in **one** request instead of long byte-by-byte loops:
    - **UNION-based**: `... UNION SELECT username, password FROM users--` — one query, all rows.
    - **Error-based (CAST/EXTRACTVALUE/UPDATEXML)**: `... AND (SELECT CAST((SELECT password FROM users WHERE username='administrator') AS INT))--` — leaks the whole string in the error message.
    - **GROUP_CONCAT bulk dump**: `... UNION SELECT GROUP_CONCAT(username,':',password SEPARATOR '|') FROM users--` — every credential pair in one cell.
    - **Header / multi-column leakage**: if the response renders multiple columns, stack the leak into one of them so you do not need a second request.
  - Do not use these patterns to dump production data. For customer or production assessments, stop at the minimal non-sensitive proof allowed by the ROE, such as DBMS fingerprint, current user/DB name when permitted, row count, or a synthetic marker row.
  - Only fall back to **boolean-blind / time-blind byte-by-byte binary search** in lab exercises or when the ROE explicitly allows bounded extraction. Even then, one turn of the agent loop has a finite ceiling (~80 tool calls), so a 20-character secret will not extract in a single turn — tell the user upfront and let them say `继续` between batches.
- Fix with parameterized queries, ORM binding, least-privilege DB accounts, and generic error handling.

### SSRF

- Inputs often named `url`, `redirect`, `callback`, `webhook`, `avatar`, `image`, `proxy`, `next`, or `uri`.
- Test allowed external URL fetch first, then blocked private targets if in scope.
- High-value targets include metadata services, localhost admin panels, cloud metadata, Redis/Elasticsearch, and internal HTTP services.
- Evidence: server-side fetch response, timing, DNS/OOB callback, internal banner, or cloud metadata fields.
- Fix with strict allowlists, DNS rebinding protection, private IP blocking after DNS resolution, and protocol restrictions.

### Out-of-Band (OOB) Interaction

When the target gives no in-band feedback (blind SSRF, blind SQLi, blind XXE, blind RCE, blind XSS), use ArgusAI's built-in Interactsh client via `oob_start` / `oob_payload_generate` / `oob_check_events` — no user setup needed. See the `oob-workflow` skill for the full call sequence, DBMS-specific one-liners (Oracle/MySQL/PostgreSQL/MSSQL), a worked blind-SQLi exfiltration example, and the local-reachability caveat for restricted networks; do not duplicate that detail here. Default to HTTP tools (`http_curl`, `raw_http_send`) for firing the payload — reach for the browser only when the surface needs an authenticated session you cannot replay via cookie copy.

### File Inclusion And Path Traversal

- Inputs often include `file`, `path`, `template`, `page`, `download`, `lang`, or `theme`.
- Try traversal variants, URL encoding, double encoding, null byte only for legacy stacks, and absolute path indicators.
- Evidence: known file content, stack trace path disclosure, or successful read of an allowed test file.
- Fix by mapping IDs to files, normalizing paths, enforcing base directories, and denying traversal after decoding.

### Command Injection

- Inputs used in shell commands may appear in ping, lookup, convert, archive, image processing, or admin tools.
- Signals: command output marker, timing delay, DNS/OOB callback, or syntax error from shell.
- Avoid destructive commands. Use benign markers such as `id`, `whoami`, short sleep, or DNS callback only when authorized.
- Fix by avoiding shells, using argument arrays, strict allowlists, and sandboxing.

### SSTI

- Probe with arithmetic markers such as `{{7*7}}`, `${7*7}`, `<%= 7*7 %>`, or engine-specific syntax.
- Evidence: evaluated output, template error, engine fingerprint, or access to safe object properties.
- Do not attempt file read or command execution unless explicitly authorized.
- Fix by not rendering user input as templates, using sandboxed engines, and separating templates from data.

### XXE And XML Injection

- Inputs include XML bodies, SOAP, SAML, SVG, DOCX/XLSX/PDF converters, and upload pipelines that parse XML.
- Safe signals: parser error changes, entity expansion behavior, blocked external entity warnings, or controlled OOB callback when authorized.
- High-risk impacts include file read, SSRF, denial of service, and credential disclosure from metadata endpoints.
- Fix by disabling external entities/DTDs, using hardened XML parsers, limiting expansion, and validating expected XML schemas.

### Deserialization

- Inputs include serialized cookies, session blobs, hidden fields, queue messages, Java/.NET/PHP/Python object formats, and signed tokens.
- Evidence: parser/class errors, gadget class names, changed server behavior after benign field modification, or signature bypass.
- Do not execute gadget chains or system commands unless explicitly authorized. Prefer demonstrating unsafe trust boundary or type confusion.
- Fix by avoiding native object deserialization for untrusted data, using allowlisted DTOs, signing/encrypting with rotation, and isolating parsers.

### Prototype Pollution

- Inputs include JSON merge/update APIs, query parsers, deep object assignment, and keys like `__proto__`, `constructor`, or `prototype`.
- Evidence: polluted property visible in response, changed client/server behavior, bypassed checks, or DOM sink triggered by polluted config.
- Distinguish client-side pollution from server-side pollution. Server-side impact needs a process-wide or request-impacting signal.
- Fix by blocking dangerous keys, using safe merge libraries, freezing prototypes where appropriate, and schema-validating JSON.

### IDOR And Access Control

- Compare two users or roles. Change object IDs, usernames, organization IDs, file IDs, order IDs, or UUIDs.
- Evidence: unauthorized read/write/delete of another user's object, not just presence of predictable IDs.
- Fix with server-side object ownership checks on every request.

### Auth, Session, And CSRF

- Check login throttling, user enumeration, password reset token leakage, session fixation, weak cookies, missing HttpOnly/Secure/SameSite, and privilege changes without re-auth.
- CSRF requires a state-changing action, ambient credentials, and missing/unvalidated anti-CSRF token or SameSite protection.
- Fix with robust session handling, anti-CSRF tokens, SameSite cookies, re-auth for sensitive actions, and rate limiting.

### OAuth, OIDC, SAML, And JWT

- OAuth/OIDC risks: weak redirect URI matching, missing/invalid `state`, code reuse, code leakage, token substitution, weak PKCE, confused client, and open redirect chains.
- SAML risks: unsigned assertions, signature wrapping, weak audience/recipient validation, replay, and XML parser issues.
- JWT risks: `none` alg, algorithm confusion, weak HMAC secret, missing `aud`/`iss`/`exp`, excessive lifetime, key ID injection, and token stored in unsafe locations.
- Evidence must show an authentication or authorization impact, not just token decodability.
- Fix with strict redirect allowlists, state/nonce/PKCE, signature validation, audience/issuer checks, short token lifetimes, and secure storage.

### CORS

- Dangerous when sensitive responses are readable cross-origin with credentials.
- Evidence requires attacker-controlled `Origin`, permissive `Access-Control-Allow-Origin`, `Access-Control-Allow-Credentials: true`, and browser-level proof that attacker-page JavaScript can read credentialed sensitive data.
- Use `cors_matrix_probe` for header candidates, then `cors_exploit_verify` for real browser verification. Do not confirm CORS from response headers alone.
- `*` without credentials is usually lower impact.
- Fix with exact origin allowlists and no reflection of arbitrary Origin.

### Upload Bypass

- Test extension, MIME type, magic bytes, polyglots, double extensions, path control, and server-side processing.
- Evidence: uploaded file executable/served as active content, path traversal on storage, malware-like processing risk, or stored XSS via SVG/HTML.
- Fix with server-side type validation, random storage names, non-executable storage, content disarm, and size limits.

### GraphQL

- Check introspection exposure, verbose errors, IDOR via node IDs, missing authorization on fields/resolvers, batching abuse, alias abuse, and depth/complexity limits.
- Use `graphql_probe` first for `__typename` and optional introspection, then `graphql_batch_probe` to identify batching support before any active abuse tests.
- If introspection works, save the schema preview as an artifact and derive field/resolver authorization tests from the schema instead of guessing endpoints.
- Evidence: unauthorized field/object access, sensitive schema disclosure, excessive resource use, or resolver-specific error leakage.
- Fix by enforcing resolver-level authorization, disabling introspection in production when appropriate, query cost limits, persisted queries, and safe error handling.

### WebSockets And Realtime APIs

- Test authentication on connect and per-message authorization, origin validation, message schema validation, replay, and cross-user channel access.
- Use `websocket_handshake_probe` or `websocket_connect_probe` to validate the endpoint, auth headers, and Origin behavior without sending application frames.
- Capture real traffic with the `websocket` reverse recipe, then run `websocket_frame_analyze` and `websocket_replay_plan` before any active replay.
- Use `websocket_send_receive` only for low-volume, non-destructive messages after explicit authorization. Use `websocket_authz_probe` for one-message comparison across two sessions or privilege levels.
- Do not fuzz subscriptions, brute-force channel IDs, flood messages, or replay state-changing actions unless the user has explicitly scoped and authorized that exact action.
- Evidence: receiving another user's events, sending unauthorized actions, or missing session invalidation after logout.
- Fix with authenticated handshakes, per-message ACL checks, strict origin checks, rate limits, and channel membership validation.

### gRPC-Web And Protobuf APIs

- Identify `application/grpc-web`, `application/grpc-web+proto`, or `x-grpc-web` traffic in browser/proxy/HAR inventory.
- Use `grpc_web_analyze` on captured headers/body and `protobuf_field_preview` on message bytes when no `.proto` schema is available.
- Prefer locating generated client stubs or descriptors in JS artifacts; schema-aware decoding is stronger evidence than raw wire guesses.
- Evidence: decoded sensitive fields, unauthenticated method access, per-message authorization bypass, or inconsistent validation between client/server.

### API And Business Logic

- API risks: mass assignment, excessive data exposure, broken object-level authorization, broken function-level authorization, rate-limit bypass, and inconsistent validation between client/server.
- Business logic risks: price/quantity tampering, coupon reuse, workflow skip, race conditions, negative values, inventory bypass, and privilege transitions.
- Use `business_surface_extract` on inventory/traffic/HTML to structure candidate assets, operations, risks, and missing evidence. The Agent must review candidates against UI/API context and then create baseline, business-specific, and system/network todos with `set_todos` / `add_todos`.
- If the business model is weak, do not stop testing. Keep baseline coverage and add a todo to collect more logged-in traffic, role-specific traffic, JS endpoints, proxy flows, or HAR samples.
- Evidence should show a violated business invariant or unauthorized effect.
- Fix with server-side state machines, invariant checks, idempotency controls, rate limits, and authorization at object/function level.

### Open Redirect

- Inputs often named `next`, `redirect`, `return`, `url`, or `continue`.
- Test scheme-relative URLs, encoded URLs, trusted-domain suffix tricks, backslashes, and nested redirects.
- Impact increases when used in OAuth, SSO, password reset, or phishing chains.
- Fix with relative paths or strict destination allowlists.

### Request Smuggling And Cache Poisoning

- Treat as advanced and high-risk. Prefer passive analysis unless authorized.
- Request smuggling indicators include conflicting `Content-Length`/`Transfer-Encoding`, proxy/backend disagreement, or desync timing.
- Cache poisoning indicators include unkeyed headers, reflected headers in cacheable responses, and shared cache hits.
- Fix with consistent proxy/backend parsing, disabling ambiguous requests, and strict cache keys.

### Evidence Confidence

- Confirmed: direct execution, unauthorized access, sensitive data exposure, server-side fetch callback, evaluated template, command marker, or reproducible state change.
- Probable: raw dangerous reflection, parser error with matching payload, clear timing delta, or schema/stack trace leakage.
- Suspicious: parameter name suggests risk but no response signal, blocked payload, encoded reflection, or generic error only.
- Ask for or run a scoped probe when confidence is below confirmed and the next step is safe.

### Payload Selection Principles

- Start with low-impact probes and canaries. Avoid destructive, persistent, or noisy payloads unless explicitly authorized.
- Select payloads by context, not popularity. Match HTML/JS/SQL/XML/shell/template/URL/API context.
- Prefer short payloads that produce clear evidence and are easy to explain.
- Keep one variable changed per request so the result is attributable.
- If a WAF or filter is present, first identify normalization and encoding behavior before trying bypasses.

### Concrete Testing Playbooks

Copy-paste-ready payloads and per-class decision rules (XSS, SQLi, SSRF, path traversal, command injection, SSTI, XXE, IDOR, upload bypass, open redirect, CORS, JWT) now live in the `web-attack-payloads` skill — read it once you've identified the vulnerability class and need concrete starting payloads. Replace `FUZZ` or parameter values in the scoped target only, and stop once evidence is sufficient.

### Remediation Mapping

- Injection classes: parameterization, output encoding, allowlists, parser hardening, and removing dangerous interpreters from user input.
- Access control classes: server-side authorization checks on every object/action, tenant isolation, and deny-by-default policies.
- Session/auth classes: secure cookies, token validation, re-authentication, MFA for sensitive actions, and robust recovery flows.
- Infrastructure-adjacent classes: strict proxy/cache parsing, private network egress controls, and secure file storage.

### Source-Inspired Coverage Checklist

- OWASP WSTG style: identify, test, understand impact, and document remediation for each vulnerability class.
- OWASP ASVS 5.0 style: map findings to verifiable security controls where possible.
- PayloadsAllTheThings style: use payload families as a menu, but choose only the minimal safe payload needed.
- PortSwigger Academy style: reason from source/sink, trust boundary, and concrete exploit condition instead of memorized answers.

### Reporting Standards

- Do not overstate severity without execution or unauthorized impact.
- Include exact affected URL, parameter, payload, and observed signal.
- Separate confirmed vulnerabilities from suspicious reflections or hardening recommendations.
- Recommend minimal safe next steps when evidence is incomplete.
