---
name: scan-setup
description: Guide users through creating a security scan. Use ONLY when the user explicitly asks to scan, audit, test, or run security checks on a URL. Do not trigger this skill from vague requests like "看下这个 URL" or "analyze this page" — start with read-only inspection (fetch_page_source / extract_page_text) and only propose a scan after the user agrees.
trigger-keywords:
  - "\\b(create_scan|run (a )?scan|start scan|rescan)\\b"
  - "\\b(scan|audit|security check)\\b.*\\b(url|site|domain|target)\\b"
  - "创建.*扫描|发起.*扫描|扫描.*(url|站点|目标)"
---

## Scan Setup

0. Confirm the user actually wants an active scan. If they just shared a URL with vague intent ("看下", "看看", "分析一下", "look at this"), respond with a short text summary based on read-only tools first and ask whether to launch a scan. Do not call `create_scan` until the user confirms.
1. Get the target URL. Extract it from natural language if needed.
2. For non-trivial audits, create or reuse a case with `case_create(target_type='url', domain='web')`.
3. If the user also provides a local repository/source root, use `repository-audit` first: set `codebase_scope_set`, map routes/handlers with `codebase_tree` and `codebase_entrypoints`, then use those source leads to choose scan checks and extra paths.
4. Call `list_checks` to see available checks, then recommend a profile:

| Profile | Checks |
|---------|--------|
| Quick | recon, headers, tls, secrets |
| Standard | recon, headers, tls, secrets, auth, identity, access_control, business, injection, upload, js_reverse, scanner |
| Deep | Standard + fuzzer + sqli_deep after user confirms higher request volume |
| Full | all checks, including xss, http_desync, ai_recon, ai_poc |
| No-browser | all except xss |

5. Discuss scope (default: URL host only, add subdomains if relevant).
6. Discuss extra paths if user mentions specific endpoints or if codebase entrypoints reveal important routes.
7. Call out noisy / special checks before enabling them:
   - `xss` uses browser automation.
   - `fuzzer`, `sqli_deep`, and `http_desync` increase request volume or protocol sensitivity.
   - `ai_recon` / `ai_poc` require configured AI endpoints and may add latency.
8. Browser-driven checks (xss, etc.) always run headless when initiated via `create_scan`. If the user wants to watch the browser, point them at the "Observe XSS" checkbox on the New Scan form; do not attempt to flip headless from a tool argument. If they need an attached/authenticated Chrome, use `launch_chrome` for CDP.
9. Summarize the configuration, confirm with user, call `create_scan`.
10. Report the task ID and remind to check progress in the task list.
11. If a case exists, attach the task id / scan summary as an artifact and use findings' `recommended_tools` / `next_actions` for follow-up.
