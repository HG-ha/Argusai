---
name: assistant-cron
description: Choose waits versus scheduled Agent work; combine interval/cron/webhook/file_change with new_session, dedicated_session, or session for patrols and durable continuation.
trigger-keywords:
  - '\b(cron|scheduled task|recurring|every hour|every day|daily|weekly)\b'
  - '定时|计划任务|周期执行|每小时|每天|每日|每周|巡检'
  - '自动续跑|定时续跑|持续续跑|无限续跑|后台继续'
  - '(?:每|每隔)\s*(?:\d+|[零一二三四五六七八九十百两]+)\s*(?:秒钟?|分钟?|分|小时|天|日|周|星期|月)'
  - '(?:\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万两半]+)\s*(?:秒钟?|分钟?|小时|天|日|周|星期)\s*后.{0,30}(?:提醒|通知|唤醒|继续|复查|检查|执行|重试)'
  - '\b(?:remind|notify|wake|resume|review|check|run|retry)\b.{0,80}\b(?:in|after)\s+(?:\d+(?:\.\d+)?|[a-z]+(?:[ -][a-z]+){0,3})\s+(?:seconds?|minutes?|hours?|days?)\b'
  - '(?:外部(?:告警|事件|回调)|告警(?:回调|事件)|webhook).{0,50}(?:触发|唤醒|自动(?:执行|分析|检查|运行|处理))'
  - '(?:文件|目录|文件夹|报告|样本|日志).{0,40}(?:变化|变更|新增|新文件|更新|落盘).{0,40}(?:自动|触发|唤醒|就分析|时分析|后分析)'
  - '\b(?:run|review|analy[sz]e|check|execute|notify|process|wake)\b.{0,100}\b(?:when|whenever|on|after|upon)\b.{0,60}\b(?:webhook|callback|external alert|external event)\b'
  - '\b(?:when|whenever|on|after|upon)\b.{0,60}\b(?:webhook|callback|external alert|external event)\b.{0,70}\b(?:run|review|analy[sz]e|check|execute|notify|process|wake)\b'
  - '\b(?:analy[sz]e|review|check|process|notify|run)\b.{0,100}\b(?:when|whenever|on|after|upon)\b.{0,70}\b(?:files?|folders?|director(?:y|ies)|reports?|logs?|samples?)\b.{0,50}\b(?:chang(?:e[sd]?|ing)|arriv(?:e[sd]?|ing)|appear|created|updated|added)\b'
  - '\b(?:when|whenever|on|after|upon)\b.{0,70}\b(?:files?|folders?|director(?:y|ies)|reports?|logs?|samples?)\b.{0,50}\b(?:chang(?:e[sd]?|ing)|arriv(?:e[sd]?|ing)|appear|created|updated|added)\b.{0,70}\b(?:analy[sz]e|review|check|process|notify|run)\b'
trigger-tools:
  - cron_list
  - cron_runs
  - read_assistant_session
  - cron_upsert
  - cron_set_enabled
  - cron_delete
  - cron_grant_high_risk
---

# Waiting, scheduling, and durable continuation

Choose the execution model first. Cron supports any authorized workflow, not just
security patrols; a long task alone is not a reason to schedule another chat.

## Choose execution before choosing a schedule

| Situation | Use | Continue when |
|---|---|---|
| Work can advance now, including a bounded task taking hours | Current Agent run, Todo, checkpoints | Keep doing the next useful step |
| A domain task or child Agent is running | `wait_for_task` / `wait_for_agents` | Inspect the actual completion/result, then continue |
| A tool returned a `job://` background handle | `background_job_status` | Check at a useful cadence; do independent work meanwhile |
| A short time dependency requires another observation | `wait_for_duration(seconds=..., reason=...)` | Reobserve and continue in this run; supported range is 0–300 seconds |
| Work must fire later, recur, react to events, or resume across turns | `cron_upsert` | The configured trigger fires into the chosen destination |
| Essential information or authorization needs a person | The proper question / confirmation flow | The person supplies it; neither waiting nor Cron bypasses it |

Discover nonresident tools with `tool_search` / `invoke_catalogue_tool`.
`wait_for_duration` may need discovery. There is no `background_job_wait` tool.
Use task/Agent waits when available instead of blind timer polling. Do not use a
shell/Python sleep to substitute for Agent wait tools.

The 300-second timer limit is **not** a threshold for switching to Cron. A
bounded long task can use task-specific waits and observations in the same run.
Use a reasonable cadence and bounded timeout; cancellation or changed user
instructions take priority. Do not claim completion from a start receipt.

A `job_ref` is a session-local **in-memory** handle. It cannot be transferred to
a fresh Cron chat or relied on after an app restart. Persist the real domain
task ID, artifact paths, and checkpoint needed to rediscover the actual work.
Do not restart an expensive operation merely because its old handle is stale.

## Two independent choices: trigger and destination

Any supported trigger can use any supported destination. An interval does not
imply a new chat, and a webhook does not imply the current chat.

| `schedule_kind` | Relevant parameters | Suitable use |
|---|---|---|
| `interval` | `every_seconds` | Relative delay or recurring checks; prefer minutes for patrols |
| `cron` | Five-field `cron_expr` | Calendar timing in the scheduler host's local timezone |
| `webhook` | `webhook_debounce_seconds` | Signed external events / alert callbacks |
| `file_change` | `watch_path`, `watch_glob`, `watch_recursive`, `watch_poll_seconds`, `watch_debounce_seconds` | New or changed local reports, samples, logs |

Intervals permit a 1-second minimum, mainly useful for tests or special cases.
Do not create tight infinite polling loops by default. `max_runs=1` makes a
relative interval a single scheduled attempt; a positive limit bounds repeats.

A webhook requires a configured, reachable listener and a sender wired to it.
`cron_upsert` returns the endpoint and local-secret availability, not the secret;
the user can copy/rotate the signing secret in the app/CLI. A loopback endpoint
is not remotely reachable. Verify a real signed event rather than declaring
the integration complete from saving the job.

File watching polls path metadata (mtime/size), not full content integrity.
Saving establishes a baseline; existing files do not all fire immediately.
Check the returned `file_watch.ok`, use a narrow glob and debounce, and keep
generated output outside the watched inputs to avoid a feedback loop. A deleted
file can be reported as changed but is no longer readable.

| `destination` | Context | Result location |
|---|---|---|
| `new_session` (default) | A distinct chat per fire; does **not** inherit the creating chat's history | Exact run chat in `cron_runs`; optional origin report-back |
| `dedicated_session` | A job-owned chat created on first fire and reused | Its accumulated run history; optional origin report-back |
| `session` | An existing chat; omit `session_id` to bind the current chat | Messages in that same chat; busy/WAITING_USER can defer execution |

“固定唤醒当前对话 / continue this conversation” means `destination=session`.
Use `dedicated_session` for one ongoing investigation kept separate from the
interactive chat; use `new_session` for independent patrols with explicit state.
Do not contend with the interactive chat using seconds-level session schedules.

`new_session` does not clone origin history or transient UI state. Freeze only
needed facts in `context_json` (JSON object, maximum 32 KiB), KB, or artifacts.
The context is provided as Scheduled Task Context on each tick. Never store
passwords/API keys there. Keep checkpoints outside an ephemeral `job_ref`.

The origin is recorded as `origin_session_id`; `report_to_origin=true` sends a
compact result there. `cron_runs(job_id)` links each exact run, including when a
new session is used. Inspect it to verify execution, not just schedule creation.

## Combinations and concrete parameters

The following are `cron_upsert` arguments. Replace example paths, KB references,
and targets with actual authorized inputs. Keep prompts thin; put large SOPs in
KB and read the relevant scenario skill as needed.

### One later check in this conversation

“十分钟后检查结果” is one future attempt, not an unbounded timer loop:

```json
{
  "name": "稍后检查结果",
  "schedule_kind": "interval", "every_seconds": 600,
  "destination": "session", "max_runs": 1,
  "prompt": "读取本对话检查点，检查已记录任务的实际结果并汇报；不要重复启动任务。"
}
```

### Continue unfinished work in the fixed current chat

For explicitly requested cross-turn continuation, preserve Todo/checkpoints and
stop on completion according to the agreed contract:

```json
{
  "name": "当前对话续跑",
  "schedule_kind": "interval", "every_seconds": 600,
  "destination": "session", "max_runs": 0, "on_change_only": true,
  "prompt": "读取 Todo 与最近检查点，继续下一项未完成工作并保存证据；全部完成时找到本任务并调用 cron_set_enabled 停用后汇报。必要信息或确认缺失时报告阻塞，不绕过。"
}
```

This is durable segmented continuation, not one unbounded model turn. Keep
working in the current run while useful work remains; do not wait for a tick to
perform work that is already possible now.

### Independent weekday patrol

```json
{
  "name": "工作日巡检",
  "schedule_kind": "cron", "cron_expr": "0 9 * * 1-5",
  "destination": "new_session", "max_runs": 5,
  "on_change_only": true, "report_to_origin": true,
  "prompt": "按 KB 中已批准的巡检范围执行；读取并更新基线与证据。无变化安静结束，新增问题或执行失败时通知；不要重复报告旧发现。"
}
```

Use this five-attempt example only when that finite lifetime is agreed; state
the host timezone. For an ongoing patrol or “until the objective is complete”,
use `max_runs=0` and the agreed completion/pause contract. Do not invent a
24-run limit or another cutoff that can abandon the objective unfinished.

### Analyze arriving files

```json
{
  "name": "新增报告分析",
  "schedule_kind": "file_change", "watch_path": "/path/to/authorized-inputs",
  "watch_glob": "*.json", "watch_recursive": false,
  "watch_poll_seconds": 5, "watch_debounce_seconds": 5,
  "destination": "new_session", "max_runs": 0,
  "on_change_only": true,
  "prompt": "只处理本次触发中新增或修改的授权报告，结合已存基线分析并归档证据；输出写入监听目录之外。无新发现不打扰，读取或分析失败时通知。"
}
```

### External alerts into one ongoing investigation

```json
{
  "name": "外部告警复核",
  "schedule_kind": "webhook", "webhook_debounce_seconds": 5,
  "destination": "dedicated_session", "max_runs": 0,
  "on_change_only": true, "report_to_origin": true,
  "prompt": "复核本次已签名回调提供的事件，按授权范围收集证据并与历史基线比较；保存下一步检查点。新风险、失败或需人工处置时通知，否则安静结束。"
}
```

The destination choices are interchangeable where the needed context warrants
it. A one-shot job may end after a failed/blocked attempt: `max_runs=1` does not
guarantee that its business objective succeeded.

## Checkpoints, results, and stopping

1. Establish scope, allowed actions, per-run bounds, notification policy, and
   completion/stop criteria. Keep a short playbook in KB when the workflow needs it.
2. Persist inputs, baseline, completed steps, next action, domain task IDs, and
   evidence paths. Each fire must know what changed and what remains unfinished.
3. Verify `enabled`, trigger, destination, next timing or event listener/watcher,
   then inspect real `cron_runs`. A saved plan is not an executed task.
   A run marked `done`/`completed` means this tick ended, not that the overall
   objective is complete or the schedule stopped. Check the job's enabled,
   run-count, and next-trigger state separately before making that claim.
4. With `on_change_only=true`, compare real baselines and return a concise
   no-change summary when nothing changed. Preserve “no change → quiet” by
   default; report completion, failures, new risks, or required user action.
   Do not disguise missing evidence or failed checks as no change.
5. Positive `max_runs` stops at the configured count. `max_runs=0` means no
   run-count limit; it does not infer business completion. On pre-authorized
   completion, or an explicit user pause/cancel, use `cron_set_enabled` to stop
   the matching job. Do not disable unfinished work just to avoid complexity.
   Preserve history unless deletion is requested or clearly part of cleanup.
6. A `cron_result` callback is a compact notification. Usually use it directly;
   if `summary_truncated`, ambiguous, or missing decision-critical facts, call
   `read_assistant_session(session_id=run_session_id, role='assistant', order='desc')`.
   Do not load every full run transcript routinely.

Scheduling guidance belongs in this progressive skill and stable system policy.
Do not repeatedly copy the full policy into chat history or inject reminder
messages every few turns. The schedule prompt should describe the actual job.

## Authorization and unattended execution

- `cron_list` / `cron_runs` / `read_assistant_session` are read-only.
  `cron_upsert`, `cron_set_enabled`, and `cron_delete` use ordinary confirmation.
- Creating/enabling a schedule authorizes its ordinary unattended actions within
  that scope. It does not expand the target scope or waive high-risk gates.
  For `NO_AUTO_APPROVE` actions, `cron_grant_high_risk(job_id)` requires a separate
  double confirmation; do not route around it via another schedule/chat/tool.
- A `new_session` job inherits origin tool/MCP approval state; origin revocation
  propagates. Scheduled plans/decision cards can auto-continue, so do not rely
  on an unattended question to obtain facts or new authorization. Essential
  missing information means blocked + notify, not guessing or card spam.
- Scheduler, model endpoint, and required tools must be available. Persisting a
  plan does not wake a powered-off machine or preserve an in-memory process.
- For remote patrols use the authorized remote connection/target. Local `ss` or
  `ufw` output is not evidence about a remote host. Keep secrets in configured
  credentials, archive concise evidence, and preserve write/restart confirmations.
