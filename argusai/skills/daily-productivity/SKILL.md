---
name: daily-productivity
description: Use when the user asks to organize files, transform documents, summarize a file, generate tables/checklists, manage reminders, or handle ordinary desktop productivity chores.
inject-priority: 6
trigger-keywords:
  - "\\b(daily task|productivity|organize files?|rename files?|convert files?)\\b"
  - "\\bsummarize (a )?(document|file)\\b"
  - "日常任务|整理文件|批量重命名|文件转换|处理文件|生成表格|整理资料|待办事项"
trigger-tools:
  - user_todo_create
  - sandbox_write_file
  - sandbox_patch_file
  - sandbox_open_file
  - desktop_notify
---

# Daily Productivity Workflow

Handle ordinary work reliably while keeping user files and desktop actions under user control.

## Files and documents

- Identify the source files, intended output format, naming rule, and whether originals must be preserved.
- Inspect file metadata and a focused preview before bulk changes. Do not guess encoding, delimiter, worksheet, or document layout.
- Default to creating transformed copies in the session sandbox. Use clear names such as `input-cleaned.csv`, `summary.md`, or `renamed-preview.json`.
- For batches, first produce a dry-run manifest mapping each source to its proposed output. Apply user-disk changes only after the user confirms the mapping.
- Preserve originals by default. Never delete, overwrite, or mass-rename user files without explicit confirmation.
- Summaries must distinguish source facts from inferences, include file references, and flag unreadable/corrupted input rather than silently dropping it.

## Plans, reminders, and repeatable work

- Use user todos for personal reminders and milestones; use agent todos only for the assistant's internal execution plan.
- Include a title, a short actionable body, and a due time only when the user provides one.
- For recurring or lengthy work, record a compact progress note and an explicit next action so the user can resume later.
- Use `desktop_notify` only for meaningful completion, failure, or attention-required events; do not spam notifications for routine steps.

## Desktop and external actions

- Desktop observation, opening applications, typing, clicking, or sending messages are user-visible actions. Describe the target and wait for the required confirmation.
- Do not use desktop automation to bypass logins, approvals, payment flows, or application security controls.
- Prefer generating a file, draft, table, or checklist over operating another application when either would satisfy the request.

## Completion

State what was produced, where it is stored, what source inputs were used, and whether any originals remain untouched. Open/reveal the finished sandbox artifact when requested.
