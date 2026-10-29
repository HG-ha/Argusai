---
name: security-workflow
description: End-to-end web application security audit workflow. Use when the user asks about audit strategy, process, methodology, what to do next, or how to approach security testing.
inject-priority: 8
trigger-keywords:
  - "\\b(security audit|web audit|audit strategy|audit workflow|methodology)\\b"
  - "\\b(pentest|penetration test|security testing)\\b"
  - "安全审计|渗透测试|审计流程|怎么测|如何测"
---

## Audit Workflow

Use `case-evidence-workflow` for any non-trivial audit. Todos track progress; cases store durable evidence.

### Phase 0: Source Available
If the user provides a local repository or asks for white-box/source-code audit, first use `repository-audit`.

1. Create or select a repository case with `case_create(target_type='repository', domain='common')`.
2. Call `codebase_scope_set` for the authorized source root. Keep all source reads inside that scope.
3. Use `codebase_tree` and `codebase_entrypoints` to build a code map before running broad black-box checks.
4. Convert source leads into evidence-backed todos. Source-only leads are not confirmed vulnerabilities until dynamically verified or otherwise proven.
5. When a deployed URL or traffic exists, map code routes/handlers to `api_inventory_build`, `business_surface_extract`, `web_request_*`, browser, proxy, or scan checks.

### Phase 1: Reconnaissance
1. Create or select a case with `case_create(target_type='url', domain='web')`.
2. Run `create_scan` with checks: `recon, headers, tls, secrets`.
3. Review with `get_task_detail` and `get_task_findings`.
4. Save important scan summaries or raw snippets with `artifact_add`; do not paste large output into chat.
5. Actively collect business evidence with the available tools: `browser_trace` / proxy or HAR traffic / `js_api_extract` / `api_inventory_build`. Prefer authenticated and role-specific traffic when authorized.
6. Call `business_surface_extract` only to structure candidate signals and evidence gaps. The tool output is not a final classification.
7. The Agent must create its own layered todos with `set_todos` / `add_todos`: generic baseline, evidence-backed business-specific tests, and system/network tests. Include evidence ids, URLs, artifact refs, or blocked reasons.
8. Focus on: sensitive paths, missing headers, hardcoded credentials, high-value business operations, and trust-boundary crossings.

### Phase 2: Deep Analysis
- JS bundles found → enable `js_reverse`.
- Login forms or auth redirects → enable `auth` and `identity`.
- APIs or object IDs found → enable `access_control`, `business`, and `injection`.
- Upload forms or media endpoints → enable `upload`.
- Broad input surface coverage → enable `fuzzer` after user confirms higher request volume.
- Strong SQLi lead → use `sqli-workflow`; enable `sqli_deep` or bundled `sqlmap_plan_scan` / `sqlmap_run_scan` only after confirmation.
- Request smuggling / cache poisoning lead → use `http-desync-workflow`; enable `http_desync` only with explicit authorization.
- Ready → choose a scan profile from `scan-setup` instead of blindly enabling every check.
- For API-heavy apps, build an API inventory first: combine `browser_trace`, proxy flows, HAR, and static `js_api_extract` output with `api_inventory_build`. If an OpenAPI/Swagger spec or Postman collection is available, `api_spec_import` turns it into the same inventory with replayable `request_template`s and per-parameter metadata — a fast way to seed the attack surface before fuzzing.
- After inventory, use `business_surface_extract` to organize candidate assets/operations/risks and missing evidence. The Agent decides the business meaning by reviewing browser UI, proxy traffic, JS routes, request bodies, and roles. Do not let this replace generic SQLi/XSS/SSRF/CORS/CSRF/upload/auth testing.
- Use `api_request_select` to narrow high-value endpoints before replay. For GraphQL, run `graphql_probe` / `graphql_batch_probe`; for WebSocket, run `websocket_connect_probe`, capture frames with the `websocket` hook recipe, then use `websocket_frame_analyze` / `websocket_replay_plan`; for gRPC-Web/protobuf, run `grpc_web_analyze` and search JS artifacts for schemas.
- For promising API leads, use `web_request_parse`, `web_request_mutate`, `web_request_replay`, and `web_response_diff` instead of hand-building repeated probes.

### Phase 3: Passive Monitoring
1. Start proxy with `start_proxy`.
2. Browse app manually.
3. Review proxy findings alongside active findings.
4. Convert proxy/browser/HAR traffic into `api_inventory_build` output and diff it after login or role changes with `api_inventory_diff`.
5. Re-run `business_surface_extract` after login, role changes, or new modules. Add new child todos only after the Agent has tied candidates to concrete evidence.
6. Attach decisive requests/responses/traces to the case as artifacts.

### Phase 4: Verification
1. For each lead, follow `recommended_tools` and `next_actions` from the finding.
2. Attach the raw proof material as artifacts.
3. Attach conclusions with `evidence_add`.
4. `rescan_task` to re-run after fixes.
5. Compare severity counts before/after.
6. Check for regressions.

### Phase 5: Reporting
- `analyze_findings` for final review.
- Use `case_get` and `case_events` to recover the evidence chain.
- Reports: HTML (interactive, filterable), Markdown (issue trackers), JSON.
