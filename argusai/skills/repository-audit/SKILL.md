---
name: repository-audit
description: Use for white-box testing, source-code audit, repository review, SAST-like investigation, code review for security, or when the user provides a local source directory. Triggers include 白盒, 白盒测试, 源码审计, 代码审计, 仓库审计, source code audit, repository audit, codebase review.
---

## Repository Audit Workflow

White-box testing in ArgusAI is model-led. Tools provide scoped code access, candidate signals, and durable evidence; the Agent performs the security reasoning.

### Start Here

1. Confirm the source root, audit goal, authorization, and whether there is a matching deployed URL or captured traffic.
2. Create or reuse a case with `case_create(target_type='repository', domain='common')`.
3. Call `codebase_scope_set(root=..., case_id=..., audit_goal=...)`. Do not read arbitrary absolute paths after this; use the scoped codebase tools.
4. Build an initial map with `codebase_tree` and `codebase_entrypoints`.
5. Create layered todos with `set_todos` / `add_todos`: architecture map, entrypoints, auth/session, data flow, dangerous sinks, configuration/secrets, business flows, and dynamic verification candidates.

### Operating Rules

- Do not treat keyword hits as findings. `codebase_entrypoints` returns candidate signals only.
- Use `codebase_search` before `codebase_read`. Read small windows around evidence, not whole files.
- Store important snippets with `codebase_evidence_add`; large raw material belongs in case artifacts, not chat.
- Preserve scope. If the user changes repository, branch, or target URL, update the scope and todos explicitly.
- For secrets, report evidence type and location, but do not paste full secret values unless the user explicitly asks and it is necessary for remediation.

### Audit Loop

1. **Map the repository.** Use `codebase_tree(max_depth=2 or 3)` to identify application roots, config files, routes/controllers, frontend/backend split, tests, and generated/vendor areas.
2. **Find entrypoints.** Use `codebase_entrypoints`; then focus with `codebase_search` for route names, permission checks, request handlers, upload/download, exports, callbacks, payment/order/admin/tenant flows.
3. **Trace high-value flows.** For each candidate, read the handler, validation layer, authz checks, storage query, and side effects. Add child todos when a flow branches.
4. **Identify risky primitives.** Search for dynamic SQL, template rendering, command execution, file path joins, deserialization, SSRF-capable clients, token/signature code, weak crypto, and security-relevant config.
5. **Check supply-chain risk (OWASP A03:2025).** Read pinned versions in `package.json`/`requirements.txt`/`go.mod`/lockfiles, `Dockerfile` base images, and CI workflow files (`.github/workflows`, `.gitlab-ci.yml`) for known-vulnerable dependencies, unpinned/floating versions, injectable CI inputs, and secrets committed to workflow config. Only cross-check flagged packages against public CVE data with `web_search` if the user explicitly asks for an external lookup.
6. **Cross-check business context.** Feed route summaries, handler names, UI labels, or API snippets into `business_surface_extract` when useful. The Agent decides business meaning from evidence.
7. **Verify dynamically when possible.** If a deployed URL, proxy capture, or request sample exists, turn source leads into `web_request_*`, `browser_*`, `create_scan`, or access-control checks.
8. **Record conclusions.** Use `codebase_evidence_add` for decisive snippets and `evidence_add` for dynamic proof. Final findings must distinguish source-only leads from verified vulnerabilities.

### Deliverable Standard

For each issue or lead, report:

- Source path and line window
- Trust boundary and attacker-controlled input
- Missing or flawed invariant
- Impact and affected business flow
- Verification status: source-only, probable, or dynamically confirmed
- Remediation and a minimal test case

End with "not covered / blocked" items for missing credentials, unavailable deployment, generated code, excluded directories, or unsupported runtime assumptions.
