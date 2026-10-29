---
name: proxy-rewrite-workflow
description: Actively rewrite or block live proxied HTTP traffic with ArgusAI rewrite rules — inject/strip headers, change method/query/status, replace body text, or short-circuit a request with a canned response. Use when the user wants to tamper with, mock, block, or force responses through the passive proxy (the active, Repeater/mock-style counterpart to read-only replay).
trigger-tools:
  - proxy_rewrite_rule_save
  - proxy_rewrite_rule_test
  - proxy_rewrite_rules_list
  - proxy_rewrite_rule_delete
  - proxy_breakpoint_config
  - proxy_breakpoints_list
  - proxy_breakpoint_resolve
trigger-keywords:
  - "\\b(rewrite|tamper|intercept|modify|mock|block|inject|force)\\b.*\\b(request|response|header|traffic|proxy)\\b"
  - "(改写|篡改|拦截|修改|注入|伪造|阻断|mock).*(请求|响应|流量|头|代理)"
---

## Active traffic rewriting

The MITM proxy is passive by default. A **rewrite rule** makes it active for the
flows it matches: the request is edited before it goes upstream and/or the
response is edited before it reaches the client. This is the active counterpart
to `web_request_replay` (which only replays a copy) — rewrite rules change what
the real browser/app actually sends and receives.

Only use this on traffic the user is authorized to test, and tell the user
exactly what each rule does: it changes live traffic, not a saved copy.

### Rule shape

```json
{
  "name": "force admin flag",
  "match": "host:api.example.com path:/v2/profile",
  "apply_to": "response",
  "actions": [
    {"op": "replace_body", "find": "\"role\":\"user\"", "replace": "\"role\":\"admin\""}
  ]
}
```

- `match` uses the **proxy-traffic-query** syntax (same as `proxy_search_traffic`). Put cheap terms first (`host:` / `path:` / `method:`).
- `apply_to`: `request`, `response`, or `both`. An action must be legal for every part it applies to, or the rule is rejected.
- Rules are saved to user settings and apply to every matching flow until disabled (`enabled: false`) or deleted.

### Actions

| Part | Action |
|---|---|
| request | `{op:set_header, name, value}` · `{op:remove_header, name}` |
| request | `{op:set_query, name, value}` · `{op:remove_query, name}` |
| request | `{op:set_method, method}` |
| request | `{op:short_circuit, status, headers?, body?}` — answer the client directly, never contact the origin (block or mock) |
| response | `{op:set_status, status}` |
| both | `{op:set_header, name, value}` · `{op:remove_header, name}` |
| both | `{op:replace_body, find, replace, regex?, count?}` |

Body rewrite notes:

- Runs only on an already-buffered, **uncompressed** body up to 2 MiB. Larger or content-encoded (gzip/br) bodies keep streaming; non-body actions still apply. If a body edit "did nothing", the body was compressed or too large — change the matching response's `Accept-Encoding` with a request rule first, or edit headers/status instead.
- `regex: true` treats `find` as a Python regex (`replace` may use `\1` backrefs); otherwise it is a literal substring. `count` limits replacements (0 = all).
- `Content-Length` is recarried automatically after a body change.

### Workflow

1. Capture the target flow first (`proxy_search_traffic` / `get_proxy_traffic`) so you have a real `flow_id` and know the exact bytes to match.
2. `proxy_rewrite_rule_test(rule, flow_id)` — dry-run against that captured flow. It never touches live traffic; read `matched`, and the `request` / `response` block's `trace` and `body_preview` to confirm the edit lands where you expect.
3. `proxy_rewrite_rule_save(rule, test_flow_id=<id>)` — install it (always user-confirmed, because it modifies live traffic). Re-issue with the same `id` to update a rule.
4. Reproduce the request in the browser/app and confirm with `proxy_search_traffic("has:...")`; matched flows are stamped `rewritten` = `request` / `response` / `both` / `short_circuit`.
5. `proxy_rewrite_rule_delete(rule_id)` or save with `enabled:false` when done, so the proxy goes passive again. List current rules with `proxy_rewrite_rules_list` (secret-looking header values are masked there).

### Common recipes

| Goal | apply_to | action |
|---|---|---|
| Strip a CSP to test injection | response | `{op:remove_header, name:Content-Security-Policy}` |
| Force an auth header on every call | request | `{op:set_header, name:Authorization, value:"Bearer …"}` |
| Downgrade a role in the response | response | `{op:replace_body, find:"\"role\":\"user\"", replace:"\"role\":\"admin\""}` |
| Block a tracker/endpoint | request | `{op:short_circuit, status:204}` |
| Mock an API while backend is down | request | `{op:short_circuit, status:200, headers:{"Content-Type":"application/json"}, body:"{\"ok\":true}"}` |
| Flip a feature flag | response | `{op:set_status, status:200}` + `{op:replace_body, find:"\"enabled\":false", replace:"\"enabled\":true"}` |

### Rules of thumb

- Keep `match` narrow. A broad rule rewrites far more than intended and is easy to forget; prefer several specific rules.
- Secrets in rule values (a bearer token in `set_header`) live only in user settings and are masked when rules are listed — never paste them into chat summaries unless the user asks.
- Rewrite is for authorized testing and local mocking, not for attacking third parties. Say what changed and offer to remove the rule afterward.

## Interactive breakpoints (hold-edit-forward)

Rewrite rules are automatic. **Breakpoints** are the manual, one-request-at-a-time
counterpart: a matching request is *parked* until you (or the user) forward it —
optionally edited — or drop it. Use this when the exact bytes aren't known ahead
of time, or the user wants to inspect/modify each request by hand.

1. `proxy_breakpoint_config(enabled=true, url_contains=…, host_contains=…, methods=[…])`
   — turn it on, **narrow** the filter so you don't park every request (parked
   requests hold live traffic). This is user-confirmed. `enabled=false` turns it off.
2. `proxy_breakpoints_list()` — see parked requests (id, method, url, headers,
   body_text). They also appear in the Proxy panel for the user to handle.
3. `proxy_breakpoint_resolve(pause_id, action="forward", edits={…})` — forward it
   (edits may set `method`, `url`, `headers`, `body_text`; absent fields keep the
   original) or `action="drop"`. Resolve promptly: a parked request auto-forwards
   after ~2 minutes so a forgotten breakpoint never wedges a tab.

Only request bodies already buffered as bytes are editable (streamed uploads are
not parked). Prefer a rewrite rule when the change is repeatable; prefer a
breakpoint for a one-off, inspect-then-decide edit.
