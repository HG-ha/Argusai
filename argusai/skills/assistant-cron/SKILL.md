---
name: assistant-cron
description: Schedule recurring or durable continuation turns (interval/cron). Use for patrols, briefings, scans, and periodically resuming unfinished Agent work; manage via cron_* tools.
trigger-keywords:
  - "\\b(cron|scheduled task|recurring|every hour|every day|daily|weekly)\\b"
  - "定时|计划任务|周期执行|每小时|每天|每日|每周|巡检"
  - "自动续跑|定时续跑|持续续跑|无限续跑|后台继续"
  - '(?:每|每隔)\s*(?:\d+|[零一二三四五六七八九十百两]+)\s*(?:秒钟?|分钟?|分|小时|天|日|周|星期|月)'
trigger-tools:
  - cron_list
  - cron_runs
  - read_assistant_session
  - cron_upsert
  - cron_set_enabled
  - cron_delete
  - cron_grant_high_risk
---

# Assistant Cron

Platform scheduler for **any** prompt on a schedule. Defense patrol is one client; so are proxy reviews, scan loops, KB hygiene, MCP health, daily briefings.

## When to schedule

- User asks for recurring / every N hours / daily / loop / 定时 / 巡检
- A playbook is stable enough that each tick only needs a short prompt + KB ref
- Prefer **not** packing full SOPs into `prompt` — store playbook in KB, reference it
- Interval min is **1 second** (scheduler ticks ~1s). Prefer minutes+ for real patrols; 1s is mainly for tight loops / tests.
- Do **not** create Cron for a bounded sequence that can finish in the current Agent run. Use task-specific polling or `wait_for_duration` for a deliberate 1–300 second observation interval, then continue directly.

## Tools

| Tool | Confirm? | Purpose |
|------|----------|---------|
| `cron_list` / `cron_runs` | No | List jobs / recent runs |
| `read_assistant_session` | No | Read the linked run transcript only when its compact callback is insufficient |
| `cron_upsert` | Yes | Create or update (interval/cron/webhook/file change) |
| `cron_set_enabled` / `cron_delete` | Yes | Pause or remove |
| `cron_grant_high_risk` | Always double-confirm | One-time unattended high-risk grant |

## Destination (Codex-aligned)

Three modes:

| Mode | Codex analog | Context | How results return |
|------|--------------|---------|-------------------|
| `new_session` (default) | Standalone scheduled task | A distinct cron chat per tick; **not** the creating chat history | Each `cron_runs` row links its exact run chat; **report_to_origin** posts a short result into the creating chat |
| `dedicated_session` | Job-owned thread | One private chat is created on first fire and reused by this job only | Run history accumulates in that private chat; optional origin report-back |
| `session` | Thread automation / scheduled message | Wakes that chat (omit `session_id` → current conversation) | Result messages land **in that chat**; busy/WAITING_USER → defer |

Rules:

- Creating chat is always recorded as `origin_session_id`.
- Need prior turns / “continue this review” → use `destination=session`.
- Need cross-run history without mixing into an existing user conversation →
  `destination=dedicated_session`.
- “固定到当前对话 / continue this conversation” means
  `destination=session`; omit `session_id` so the tool binds `_session_id`.
- Independent patrol / avoid colliding with interactive chat → `new_session` (still get origin report-back).
- Every `new_session` tick gets a unique session so historical “Open session” links remain immutable. Persist cross-run state in `context_json`, KB, artifacts, or `cron_runs` rather than relying on the previous tick's chat history.
- `new_session` does not clone origin history or transient UI state. Freeze only the required facts in `context_json` (maximum 32 KiB) or reference a KB entry; never include passwords or API keys.
- `context_json` is injected on every tick as typed **Scheduled Task Context**, separately from current UI context.
- **High-frequency loops (seconds)** should use `new_session`. `destination=session` every few seconds will fight the user for the same chat (defer / already-running). Interactive chat **preempts** cron on that session.
- After runs, call `cron_runs(job_id)` anytime; don’t assume the next user message already includes the full cron transcript when using `new_session`.
- A `cron_result` callback is a compact notification, not the authoritative transcript. Normally use it directly. If it is marked `summary_truncated`, is materially ambiguous, or lacks facts needed for a decision, call `read_assistant_session(session_id=run_session_id, role='assistant', order='desc')`. Do not load every run routinely.

## Prompt pattern (playbook)

1. Confirm scope with the user once.
2. `kb_save` a short playbook (checks, tool whitelist, alert thresholds, do-nots, skill refs).
3. `cron_upsert` with a thin prompt, e.g.:

```
按 KB「kb-xxxx」执行；需要时 skill_read assistant-cron / 场景 skill；
对比 cron_runs；无变化则安静结束（summary: no change）。
```

4. On each tick: `kb_read` → execute → update baseline notes → rely on `cron_runs` for history.

## Durable continuation of the current Agent

When the user asks the current Agent to keep resuming unfinished work across
multiple turns, configure thread automation directly; do not create a
standalone cron chat that sends another synthetic message:

- `destination=session`
- omit `session_id` to target the current conversation
- `max_runs=0` for no run-count limit
- use a minutes-level interval unless the user explicitly needs tighter timing
- keep the next action in Todo/checkpoint; persist large state in KB or artifacts

Use a thin continuation prompt:

```
读取当前 Todo 和最近 checkpoint，继续执行下一项未完成工作；不要重复已完成步骤。
若任务已全部完成，调用 cron_list 找到本续跑任务并用 cron_set_enabled 停用，然后总结结果。
若等待用户决定或必要确认，保持阻塞状态，不得自行绕过。
```

The main chat may preempt a tick. Busy, confirmation, and WAITING_USER states
must defer rather than start a competing run. This is durable segmented
continuation, not one unbounded model turn, and it only runs while the
application scheduler is available.

## SSH remote patrol (no fake local bash)

When the target is a remote Linux host and the user authorized SSH:

1. Freeze `ssh_target` in cron `context_json` (e.g. `user@host` or ssh config Host). Do **not** put passwords in the prompt.
2. `shell_session_start` → `ssh …` using agent / `~/.ssh/config`.
3. `shell_session_exec` **read-only** checks only (listen ports, firewall status, failed logins summary, service versions).
4. Summarize + `artifact_add`; do not dump full SSH output into chat.
5. Never run remote hardening/restart/write commands from a cron tick unless the user explicitly authorized a separate engagement.

If there is no SSH: use `network_discover_hosts` / `network_profile_host` / `network_assess_exposure` / light `create_scan` for exposure — still not local sandbox `ss`/`ufw` as if they were the remote.

## Examples (non-defense)

- Proxy: every 6h remind to review passive findings / export summary
- Scan: interval light profile on a frozen URL; compare finding counts
- KB: weekly cleanup / user-profile reminder
- MCP: `mcp_server_health` digest
- Daily productivity: morning briefing via daily-productivity skill

## Safety

- A `new_session` job inherits the origin chat's explicit tool/MCP approval state on every tick; revocation in the origin propagates. It never gains broader permission than the origin.
- Cron ticks are fully unattended: authorized tools, todo plans, and
  `stop_for_user_decision` cards are auto-approved / auto-continued. Creating
  or enabling the schedule is the ordinary authorization gate; high-risk tools
  additionally require the one-time grant below.
- Todo plans and `stop_for_user_decision` single/batch cards auto-continue.
  Put the recommended choice first; explicit recommended/default values win.
- Ordinary confirmable tools are covered by the schedule. For
  `NO_AUTO_APPROVE` tools, call `cron_grant_high_risk(job_id)` after
  `cron_upsert`; this creates the required separate double-confirmation card.
  Once granted, ticks run silently. Without it, a high-risk request fails fast
  and notifies instead of leaving the schedule stuck.
- Prefer low-interaction ticks: missing info → blocked + notify, do not spam decision cards
- Use `max_runs` for finite schedules; disable with `cron_set_enabled` only for an explicit user-requested pause
- Delete stale jobs when their history is no longer needed
