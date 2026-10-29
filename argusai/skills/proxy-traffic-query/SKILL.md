---
name: proxy-traffic-query
description: Write ArgusAI's field-style traffic filter expressions (host:/re/ method:POST status:4xx resp.body:/token/ …) for proxy_search_traffic, decryption-rule match fields and the GUI filter box. Use when searching, filtering or selecting captured proxy flows, or when the user asks for a regex over traffic.
trigger-tools:
  - proxy_search_traffic
  - proxy_decrypt_rule_save
  - proxy_decrypt_rule_test
  - proxy_decrypt_script_rule_save
trigger-keywords:
  - "\\b(filter|search|find|grep)\\b.*\\b(traffic|flows?|requests?|proxy)\\b"
  - "\\bproxy\\b.*\\b(regex|query|filter)\\b"
  - "(筛选|过滤|搜索|查找).*(流量|请求|抓包)|流量.*(正则|表达式)"
---

## Traffic filter expressions

One syntax everywhere: the `proxy_search_traffic(query, limit)` tool, the `match` field of decryption rules, and the GUI **流量记录** filter box. Always show the user the exact expression you used so they can paste it into the GUI.

### Grammar

- Whitespace between terms means **AND**: `host:api.example.com method:POST`
- `OR` (or `|`) alternates; AND binds tighter than OR: `status:401 OR status:403`
- Negate with a leading `-`, `!` or `NOT`: `-host:google.com`, `NOT has:body`, `!(type:img OR type:css)`
- Parentheses group: `(path:/login OR path:/oauth) method:POST`
- A term is `field:value`. A bare word or bare `/regex/` matches the full URL: `login`, `/\/v\d+\//`

### Values

| Form | Meaning | Example |
|---|---|---|
| `word` | case-insensitive substring | `host:pay` |
| `a,b,c` | any of (no spaces) | `method:POST,PUT` |
| `*` inside | glob over the whole value | `host:*.example.cn` |
| `"quoted value"` | substring with spaces / commas kept | `resp.body:"not found"` |
| `/regex/flags` | regex search; flags `i m s x` | `host:/^api\./` |

Regex rules:

- A value is a regex only when the closing `/` ends the term. `path:/login` and `path:/api/v1/user` are plain substrings; `path:/v\d+/` is a regex.
- Regexes are **case-sensitive** unless you add `i`: `header:/x-token/i`.
- Escape a literal slash as `\/`. Engine is Python `regex` (PCRE-like; `\p{Han}`, lookarounds, `(?i)` work).
- Each search has a 0.25 s timeout. A catastrophic pattern such as `(a|aa)+$` gives up instead of hanging; the result then reports `regex_timeouts`.

### Fields

| Field | Matches | Examples |
|---|---|---|
| `url` (default) | full URL | `url:/[?&]token=/` |
| `host` | host name | `host:*.example.com` |
| `path` | path + query | `path:/v2/order` |
| `method` | HTTP method | `method:POST,PUT` |
| `status` | status code | `status:4xx` `status:>=500` `status:200-299` `status:failed` |
| `type` | api doc js css img media other | `type:api` `-type:img,css` |
| `ct` | response Content-Type | `ct:json` |
| `size` / `reqsize` | response / request bytes | `size:>100k` `reqsize:>1m` |
| `duration` | ms (`s`, `ms` units) | `duration:>2s` |
| `id` | flow id | `id:>120` `id:5,9` |
| `header` / `req.header` / `resp.header` | header exists / value / regex | `req.header:authorization` `header:cookie=/sid=/` `resp.header:/set-cookie:.*httponly/i` |
| `body` / `req.body` / `resp.body` | raw body **and** decrypted text | `resp.body:/"code":\s*-1/` `req.body:password` |
| `dec` / `dec.req` / `dec.resp` | decrypted / auto-decoded text only | `dec.resp:orderId` |
| `finding` | passive finding id or title | `finding:cors` `finding:*` |
| `has` | finding error body reqbody decrypted cookie auth | `has:decrypted` `-has:body` |
| `tls` | yes / no | `tls:no` |

Body search covers the first 1 MiB of each captured body. Released bodies (memory budget) and bodies of media flows are not searchable.

### Recipes

| Goal | Expression |
|---|---|
| Login / auth calls | `(path:/login OR path:/oauth OR path:/token) method:POST` |
| Server errors on the target only | `host:*.target.com status:>=500` |
| API calls carrying a bearer token | `type:api req.header:authorization=/^Bearer /` |
| JSON business errors | `resp.body:/"(code|errcode)":\s*-?[1-9]/` |
| Possible IDOR parameters | `url:/[?&](uid|user_?id|order_?id)=\d+/i` |
| Large responses, no media noise | `size:>500k -type:img,media` |
| Flows a decryption rule opened | `has:decrypted dec.resp:/"phone":/` |
| Still-encrypted payloads | `-has:decrypted resp.body:/^[A-Za-z0-9+\/=]{64,}$/` |
| Everything except CDNs | `-host:*.cdn.* -host:*.qpic.cn -type:img,css,js` |

### Using the tool

1. Call `proxy_search_traffic(query=..., limit=30)`. It returns `matched`, `total`, the newest `flows` (summaries) and `ids`.
2. On `ok: false` read `error` and `position` (character offset), fix that part and retry. Do not guess field names; use the table above.
3. `timed_out: true` means the 4 s budget ran out; narrow the query (add `host:` / `type:` first, body regexes last).
4. Open interesting flows with `get_proxy_flow_detail(flow_id)` or `proxy_decode_flow(flow_id)`.
5. Put cheap terms first. `host:x type:api resp.body:/…/` is faster than leading with the body regex, because AND stops at the first false term.
