---
name: proactive-defense
description: "Plans and executes authorized defensive security operations: business security checks, log review, attack-surface baselines, patrols, alert triage, incident response, and attack tracing. Use when the user asks for 主动防御、业务安全检查、日志检查、安全巡检、告警处置、攻击溯源 or equivalent defensive work."
inject-priority: 9
trigger-keywords:
  - "\\b(proactive defense|defensive security|attack surface|security patrol|log review|alert triage|incident response|threat hunting|attack tracing|hardening)\\b"
  - "主动防御|业务安全|日志检查|日志审计|攻击面|安全巡检|告警处置|应急响应|威胁狩猎|攻击溯源|加固|暴露面"
trigger-tools:
  - local_attack_surface_snapshot
  - network_profile_host
  - network_assess_exposure
  - cron_list
  - cron_runs
  - cron_upsert
  - kb_search
  - kb_save
  - kb_read
  - case_create
  - artifact_add
---

# Proactive Defense

When the user asks for defensive work, **lead the workflow** but ground it in their business objective, authorized assets, available logs, and desired operating cadence. This is an Agent capability, not a session mode or UI state.

Default posture: **non-destructive**, read-only collection + recommendations. No exploit verification, persistence, or destructive hardening unless the user opens a separate offensive engagement.

## Phase 0 — Intent intake (mandatory first)

**Do not** dive into deep scans, SSH, or `cron_upsert` until Phase 0 is done (except tiny, free discovery that helps you ask better questions — see Phase 0b).

Ask clearly (one short decision card / few questions; Chinese if the user writes Chinese):

1. **目标是什么？** 本机加固 / 内网主机 / 云主机 / Web 对外暴露 / 办公网 / 全部？
2. **资产范围？** IP/CIDR、域名、URL、SSH Host、本机。哪些**明确授权**、哪些禁止碰？
3. **期望产出？** 一次性报告 / 基线对比 / 持续巡检告警 / 加固清单 / 事件时间线 / 攻击溯源报告？
4. **巡检怎么跑？** 要不要定时？频率（每小时 / 每天 / 每周）？有变化才通知还是每次摘要？安静时段？
5. **远程怎么采？** 仅外网探测（network_*） / 已配 SSH（`~/.ssh/config` / agent） / 暂不远程？
6. **可用证据？** Web/业务日志、WAF/EDR/SIEM 告警、代理流量、主机审计、云审计或已有 Case。
7. **敏感边界？** 生产 vs 测试、可否轻量端口扫描、是否允许 `create_scan`、禁止操作清单。

If the user already answered these in the message, profile, Case, or KB playbook, **do not re-ask** — confirm in one sentence and proceed.

If answers are partial: ask only the missing critical items (scope + whether cron is wanted). Do not block forever on nice-to-haves.

## Phase 0b — When intent is vague: auto-discover, then propose

If the user only says「帮我做主动防御 / 看看安全」without assets or goals:

1. **Actively gather context** (read-only, parallel where safe):
   - `user_profile_read` / habits; prior KB (`kb_search` / skill list) for playbooks or assets
   - Session todos / memory; linked cases or reverse/proxy context if any
   - `local_attack_surface_snapshot` for **this machine** (not for remotes)
   - Light env signals: hostname/OS from snapshot; running services of interest; toolchain / MCP health only if relevant
   - If URLs or hosts already appear in chat/history/proxy findings — treat as **candidates**, not confirmed scope
2. **Synthesize a written proposal** before heavy work:
   - Assumed business/system picture (what this host/role looks like)
   - Recommended coverage matrix (local / network / web / identity / supply-chain as applicable)
   - Suggested first-pass checks vs later deeper checks
   - Suggested patrol cadence + notify-on-diff vs one-shot
   - Explicit assumptions and **open questions** (what still needs user OK)
3. Present the proposal and wait for confirm / adjust **unless** the user already supplied scope and said「直接开始」— then execute the proposed first pass and note assumptions in the report.

Never pretend remote Linux state came from the local sandbox (`ss`/`ufw` on ArgusAI host ≠ cloud VM).

## Phase 1 — Coverage matrix + KB playbook

After intent (or accepted proposal):

1. `set_todos` / `add_todos`: layered matrix — business surface, logs/detections, local baseline, authorized remotes, web exposure, identity/edge, open questions.
2. `kb_save` a short **defense playbook** before deep collection:
   - Assets & out-of-scope
   - Goals & success criteria
   - Tool whitelist (read-only first)
   - Alert thresholds / what counts as “change”
   - Do-nots; skill refs (`proactive-defense`, `assistant-cron`, others)
3. Keep the playbook as the source of truth; cron prompts only **reference** the KB id.

## Phase 2 — Collect evidence

| Target | Do | Don't |
|--------|----|-------|
| This machine | `local_attack_surface_snapshot` | Treat results as a remote host |
| Remote, no SSH | `network_discover_hosts` / `network_profile_host` / `network_assess_exposure`; light `create_scan` only if authorized | Local `ss`/`ufw` claimed as remote |
| Remote + authorized SSH | `shell_session_*` → `ssh` via agent/config; **read-only** cmds | Passwords in cron/prompt; restart/firewall writes on patrol ticks |
| Business/Web | Proxy findings, authorized scan/task history, app/API evidence, auth and edge controls | Generic port output presented as business risk without mapping |
| Logs/alerts | Focused time window, normalize entities, build timeline, correlate Case artifacts | Dump entire sensitive logs into chat or claim attribution without evidence |

SSH read-only examples (skip if missing): listen ports, firewall summary, failed-login count, critical service versions. Summarize + `artifact_add`; no full dump in chat.

Prefer tools: snapshot, `network_*`, `shell_session_*`, `kb_*`, `cron_*`, todos, `artifact_add` / case evidence when durable.

For alert triage and attack tracing: create/reuse a Case, preserve raw references, correlate time/user/IP/process/request identifiers, separate observed facts from hypotheses, and state confidence plus evidence gaps. Do not claim actor identity from a single IOC.

## Execution cadence — do not default to Cron

Choose the lightest mechanism that matches the requested duration:

- **Immediate / one-off defense:** execute the inspection and response plan in the current Agent run. No Cron.
- **Bounded rapid loop:** after a scan or action, use `wait_for_duration(seconds=1..300)` only when a real observation/settling interval is needed, then inspect again and continue. Set a concrete cycle count, deadline, success condition, or stop condition; do not turn short waits into an unbounded busy loop.
- **Long-running OS process:** use the task-specific job/status tools or `shell_session_*` lifecycle when an actual scanner, watcher, or service must remain alive. The Agent should poll status rather than sleep blindly.
- **Recurring / calendar / restart-resilient patrol:** use Cron only when the user wants work to wake again after the current run, at a future cadence, or after application restart.

Multiple scan-and-verify steps do not by themselves justify creating a Cron job. A one-second wait followed by another authorized observation is normal Agent execution.

## Phase 3 — Optional cron patrol

Only after the user wants recurring checks (or accepts that part of the proposal):

1. Playbook must exist in KB.
2. `cron_upsert`: `destination=new_session`, `tags=["defense"]`, `on_change_only=true` by default.
3. Thin prompt, e.g.「按 KB「kb-xxxx」执行；对比 cron_runs；无变化安静结束（summary: no change）」.
4. Freeze assets / `ssh_target` in `context_json`. Frequency matches user expectation (e.g. 3600 / 86400).
5. Teach ticks via `assistant-cron` skill; do not pack the full SOP into every prompt.
6. Finite patrols use Cron's native `max_runs`; never ask a scheduled turn to disable itself with `cron_set_enabled`.

## Phase 4 — Report

- Exposure summary + risk hints + hardening suggestions prioritized
- What was checked vs skipped (and why)
- Baseline fingerprint / refs for next diff
- If cron registered: job id, cadence, notify policy
- Remaining questions for the user

## Safety (never skip)

- Defensive workflows do **not** bypass confirmations, scope gates, or `NO_AUTO_APPROVE`
- Missing authorization → mark todo blocked + ask; do not guess production OK
- Prefer notify-on-diff for patrols; avoid noisy “all clear” spam
- Cron ticks: low interaction; if blocked on missing info → notify once, do not stack decision cards
