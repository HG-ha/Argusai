---
name: agent-todo
description: Manage structured todos for multi-step agent work. Use when the user asks to execute, investigate, scan, fix, analyze, or continue a task with multiple steps.
inject-priority: 10
trigger-keywords:
  - "\\b(pentest|penetration test|multi[- ]?step|continue|investigate)\\b"
  - "\\b(scan|analyze|audit|execute|fix|complete)\\b.*\\b(task|target|site|url)\\b"
  - "渗透|全面.*测试|多步|继续.*任务|调查|审计"
---

## Agent Todo

1. For multi-step work, call `set_todos` before the first action. The runtime will **stop and show a confirmation card** — do not call execution tools in the same response.
2. Wait for the user to confirm (`开始执行`), request changes (`修改计划` or free-text edits), or cancel. After edits, call `set_todos` / `add_todos` again and stop; never auto-start probes/scans before confirmation. After `开始执行`, do **not** call `set_todos` again — use `update_todo` / `complete_todo_with_refs` / `add_todos`.
3. For security analysis or reverse engineering, prefer hierarchical todos:
   - parent todos = phases or subtask groups (`recon`, `mapping`, `probing`, `validation`, `reporting`)
   - child todos = concrete executable actions
   - use `children` for planned child actions, or `add_todos(parent_id=...)` when a new branch appears
4. For security analysis or reverse engineering, pair todos with a case:
   - todos = progress and control flow
   - case/artifact/evidence = durable proof and recovery after compaction
   Use `case_create` early unless the user points to an existing case.
5. Keep todo items concrete and observable. Good examples:
   - "Create XSS scan task"
   - "Wait for scan completion"
   - "Review high severity findings"
   - "Summarize exploit evidence and remediation"
5. Status values:
   - `pending`: not started
   - `in_progress`: currently active
   - `completed`: finished
   - `blocked`: cannot continue without missing input or permission
   - `cancelled`: user cancelled or the plan is no longer relevant
6. Keep exactly one actionable child todo `in_progress` when possible (only after the user confirmed the plan).
7. After each meaningful tool call, call `update_todo` to reflect progress.
8. If a tool fails, mark the affected todo as `blocked` and explain what is needed in `blocked_reason`.
9. When a step produces proof material, save it with `artifact_add` / `evidence_add`; do not rely on the todo text as evidence.
10. Store proof on todos as references only: `evidence_refs`, `artifact_refs`, `finding_refs`.
11. After proof is saved, prefer `complete_todo_with_refs` to attach refs and complete the todo in one step.
12. For a finding that needs verification, call `create_validation_todo` before probing, then attach proof refs when done.
13. Use `asset_ref_normalize` for non-Web assets (`apk:...`, `ipa:...`, `exe:...`, `container:...`) before placing them in `target_ref`.
14. Use `case_link_add` / `case_graph` when the relationship between todo, evidence, artifact and finding matters for an attack chain.
15. When the task is complete, mark all relevant todos as `completed` and provide a concise final summary with stable refs when available.

## Confirmation discipline

- Treat the initial plan card as one consolidated approval. Include foreseeable
  branches and acceptance criteria up front instead of repeatedly presenting
  small follow-up plans.
- After the user approves the plan, continue until every item is completed,
  genuinely blocked, cancelled, or needs a material human decision. Never ask
  "should I continue?" merely because the task is long, difficult, expensive in
  reasoning, or has many files and tool steps.
- Use `add_todos` to capture newly discovered work that stays inside the
  approved goal and scope. Do not manufacture another approval boundary for
  routine implementation details, ordering choices, or reversible defaults.
- Before asking a question, inspect available UI, task, case, memory, sandbox,
  file, and tool state. Infer ordinary details or choose a safe reversible
  default when the requested outcome is unchanged.
- When a human choice is truly required, batch related questions into one
  `stop_for_user_decision_batch` card. Do not ask the same question again after
  the user or current context already answered it.
- Mandatory tool confirmations, authorization/scope boundaries, secrets,
  irreversible external effects, and materially different product choices
  remain valid hard stops.

## Todo list as the loop stop signal

The runtime uses the model-authored todo list to decide whether an open-ended turn should continue automatically.

- **Plan confirmation is mandatory**: after `set_todos`, execution tools do not run until the user confirms the plan.
- After confirmation, as long as any todo is `pending` or `in_progress`, the system may inject a hidden continue prompt.
- To actually stop, mark every remaining item `completed`, `blocked`, or `cancelled`, or clear the list.
- Do not mark a todo `blocked` just to escape the loop. `blocked` means a real external dependency prevents that specific item.
- Use `stop_for_user_decision` / `stop_for_user_decision_batch` when progress depends on a human choice but the todo itself is still valid.

## Broad audit coverage

For broad requests such as full-scope pentest, "all web vulnerabilities", API protection review, WAF bypass review, network/system exposure, or report-and-PoC delivery, use a coverage matrix rather than vague todos.

Include concrete completion criteria for phases such as:

- scope and rules of engagement;
- crawl/API/JS endpoint inventory;
- auth/session/CSRF/CORS/security headers;
- authorization/IDOR/object protection;
- injection families: SQLi, XSS, SSTI, command, LDAP, NoSQL where applicable;
- SSRF, XXE, file read/path traversal, upload, deserialization, RCE where applicable;
- business logic, rate-limit, and abuse checks;
- WAF/firewall fingerprinting and safe bypass attempts;
- network/service discovery, TLS/DNS/exposure correlation;
- evidence capture, minimal PoC, final report/export;
- explicit not-tested, blocked, or out-of-scope notes.

Do not mark a phase completed unless evidence satisfies its `done_when` condition.
