---
name: multi-agent-orchestration
description: Decide between a persistent Team-sidebar squad (team_*) and temporary child Agents inside this chat (spawn_agent / spawn_team / run_subagent). Words like 小队, 团队, 建队, or "let the team do it" mean the persistent Team. spawn_team is only a one-off split of the current task.
inject-priority: 9
trigger-keywords:
  - "\\b(multi[- ]?agent|sub[- ]?agent|child agent|parallel agents?|delegate task|spawn agent|agent team|team of agents?)\\b"
  - "\\b(comprehensive|end[- ]to[- ]end|large[- ]scale|multi[- ]component|multiple (components|modules|areas)|full (review|audit|analysis|optimization))\\b"
  - "多.?Agent|多智能体|子.?Agent|子智能体|并行.?Agent|任务委派|独立.?Agent"
  - "全面.*(检查|审查|审计|优化|分析|排查)|完整.*(检查|审查|分析|流程)|多个.*(模块|方面|目标|界面)"
  - "组队|团队|小队|分队|战队|一支队|一个队"
trigger-tools:
  - run_subagent
  - spawn_agent
  - spawn_team
  - team_list
  - team_get
  - team_chat_get
  - team_message_send
  - team_create
  - team_update
  - team_delete
  - team_add_member
  - team_member_get
  - team_member_update
  - team_member_send_input
  - team_member_control
  - send_agent_input
  - pause_agent
  - resume_agent
  - wait_for_agents
  - list_agents
  - get_agent
  - close_agent
  - background_job_status
  - background_job_list
  - background_job_cancel
---

# Multi-Agent orchestration

ArgusAI has two mechanisms. Pick from what the user is referring to, not from
the word "team" alone.

| User means | Use | Lives where | Members talk to each other |
|---|---|---|---|
| 小队 / 团队 / 建队 / 看团队 / 让团队做某事 / the Team sidebar | `team_create`, `team_add_member`, `team_message_send` | Team menu, across chats | Yes, in the group room |
| Split **this** task into parallel workers | `spawn_agent`, or `spawn_team` for a named one-off squad | This chat only | No |
| One short investigation whose answer you need now | `run_subagent` | This chat, sync | No |

If the user names an existing team, calls a role such as static-analyst, or
says the work should stay in the team, stay on `team_*`. Do not also
`spawn_team` or `spawn_agent` for that request.

`run_subagent` creates a separate session with its own history. It is not a
role-play prompt inside the parent context.

## Delegation is proactive

Do not wait for the user to say "use sub-agents". For non-trivial work,
proactively delegate when there are at least two independent, judgeable
objectives and parallelism will materially reduce latency, protect the parent
context, or add useful independent verification:

- parallel review of separate components or attack surfaces;
- independent verification of a tentative conclusion;
- specialist analysis that would pollute the parent context;
- several research questions whose outputs can be merged afterward.

Keep work in the parent Agent when steps are tightly sequential, trivial, need
the same mutable state, or require frequent user decisions.

Readonly delegation does not require a separate user question. Choose sensible
roles, objectives, and configured models yourself. Ask the user only when the
choice would materially change the requested outcome or cross an authorization,
scope, cost, or side-effect boundary. Difficulty, workload, and the number of
files are reasons to decompose the work, not reasons to ask whether to continue.

## Parent / child contract

The parent Agent owns the user goal, Todo plan, authorization boundary, final
decision, and final response. A child receives only:

- one specialist `role`;
- one concrete `objective`;
- minimal `context`;
- an `expected_output` shape.

Children cannot ask the user questions, delegate further, or silently widen
scope. They return a bounded summary plus artifact/finding references; their
raw transcript is not injected into the parent context.

## Parallel pattern

For genuinely independent work, start several durable children with `spawn_agent`:

1. Launch independent branches early, preferably in the same turn.
2. Assign non-overlapping objectives and explicit expected outputs.
3. Record every returned `agent_id`.
4. Continue useful parent work instead of tight polling.
5. Call `wait_for_agents(agent_ids=[...])` once when results are needed.
6. Use `send_agent_input` to redirect a live child at its next safe boundary.
7. Use `pause_agent` to stop after a safe boundary and `resume_agent` for an
   explicit new turn after pause, failure, or completion.
8. Use `get_agent` to inspect evidence/transcript and `close_agent` when the
   thread is no longer needed.
9. Merge conclusions, reconcile disagreements, and cite returned evidence.

Legacy `run_subagent(mode="sync")` remains useful for one short investigation
whose answer is needed immediately. Do not create multiple Agents merely to make
one serial workflow look parallel.

## Standing team (`team_*`) — the Team sidebar

This is what the user means by 小队 / 团队 unless they clearly ask to split only
the current task. The team is global: it stays in the Team menu after this chat
ends, and members share a group room.

- `team_list()` → every standing team, live status, `members_preview`.
- `team_get(team_id)` → full roster (`agent_id`, `role`, `status`, `summary`).
- `team_chat_get(team_id)` → one chronological group transcript.
- `team_message_send(team_id, message, agent_ids=[], visibility="group")` → group command. `visibility="private"` needs exactly one member id.
- `team_create(name, objective, preset="")` → create the team. Optional preset `web_audit` | `recon_sweep` | `reverse_triage` spawns those roles when `objective` is set. Preset members are readonly and wait for the first command.
- `team_update` / `team_delete` → rename or delete.
- `team_add_member(team_id, role, objective, permission, requested_tools, model)` → add one member and leave them waiting. They start on the first room message, not at creation. `model` is a configured endpoint name or `端点名::模型名`. There is no "all tools" permission: `readonly`, or `scoped` plus an explicit `requested_tools` whitelist. Do not put spawn/team-control tools in that list.
- `team_member_update` → change role, objective, permission, tools, or model (model applies next turn).
- `team_member_send_input` → steer one member. `team_member_control` → pause, resume, close, or delete.

Members are depth-1. They can talk in the room. They cannot recruit further agents. Only you or the user add members. To give an order, `team_message_send` or `team_member_send_input` — do not `spawn_agent` a duplicate.

Public member replies are delivered to the other team members. Busy members
finish their current turn first; ordinary messages can arrive as one ordered
batch. Members decide whether their role, new evidence, a correction, or a
concrete dependency needs a reply. Otherwise they use `team_room_pass` silently,
without acknowledgements or agreement-only posts. An explicit `@exact-role`
request is a separate work item and must be addressed; private messages remain
private. Shared context is unread room history supplied at the next turn, not
every member's full private history or an unlimited replay on every message.

### Lead the objective through completion

`completed` means a member's current turn ended; `idle` means no member is
currently running. Neither proves the team objective or even that member's
whole assignment is complete. A progress paragraph, a promise to investigate,
or a request for a baseline is not a finished deliverable.

1. Keep the overall plan in your Todo/checkpoint. Give each assignment a concrete
   completion condition and expected evidence. For broad audits, track coverage,
   findings, reproduction, review, and unresolved gaps; a quiet team does not
   establish that every vulnerability has been found.
2. Use `team_get` to inspect status, then `team_chat_get` or `team_member_get`
   when the compact summaries do not establish the outcome. Reconcile each
   result against the plan and cited artifacts; distinguish finished work,
   unfinished work, and a real dependency or authorization block.
3. If actionable work remains, send a specific next assignment with
   `team_message_send` / `team_member_send_input` to the relevant existing
   members. Include the missing input or evidence reference and acceptance
   condition. Do not resume everyone with an empty "continue" instruction.
4. While members work, advance independent lead tasks. For bounded observation,
   inspect team status with sensible `wait_for_duration` backoff and a deadline.
   `wait_for_agents` is for children of this chat, not a different standing
   team's member IDs. A timeout is not completion; do not busy-poll or loop
   empty resumes when the only remaining action needs user input.
5. Standing-team completions appear in the Team room; they do not automatically
   wake this operator conversation. If the user wants cross-turn supervision,
   read `assistant-cron` and arrange `destination=session` for this conversation.
   Persist the exact team ID, next actions, baseline, notification policy, and
   actual stop condition. Each tick must inspect results and advance unfinished
   assignments, rather than merely report that everyone stopped.
6. An arbitrary `max_runs` or wall-clock estimate does not satisfy "until done".
   Use a count limit only when it matches the agreed scope; if reached with work
   remaining, report that gap. Disable outcome-based supervision when the
   agreed acceptance conditions are met. Missing information or a mandatory
   confirmation remains a block; neither team messages nor schedules bypass it.

Before the final answer, account for every assigned branch with its evidence,
remaining limitations, and any required user action. Do not declare the whole
mission complete solely because all member statuses say `completed`.

## One-off squad (`spawn_team` / `spawn_agent`)

Use these only to parallelize the current task. They are not the Team sidebar.

`spawn_team` groups children under one header in **this** Agent chat. They
cannot talk to each other or to the user, and the roster goes away with the
conversation. Prefer it over several `spawn_agent` calls when the branches are
the preset specialties. Prefer separate `spawn_agent` calls when the branches
are heterogeneous or should start at different times.

- Presets: `web_audit`, `recon_sweep`, `reverse_triage`. Readonly unless a role sets `permission: "scoped"` and `requested_tools`.
- `scope_ref` is optional. Spawning is the authorization for that whitelist; engagement and `Target.in_scope` still apply.
- Afterwards use `list_agents`, `send_agent_input`, `pause_agent`, `resume_agent`, `close_agent`.

## Safety and limitations

- `permission="readonly"` is the safe read set. `permission="scoped"` requires an
  exact `requested_tools` whitelist; `scope_ref` is optional. The runtime rejects
  delegation controls (`spawn_agent`, `spawn_team`, `run_subagent`, and the
  send/pause/list/close family). Do not whitelist `team_*` either: members must
  not recruit. Parent delegation is the confirmation gate for the whitelist.
  Tools that always need a second confirm still do. Engagement and scope checks
  still apply.
- Delegation depth is capped at one: child Agents cannot create grandchildren.
- Child threads persist checkpoints and transcripts under the parent session.
  Interrupted `queued`/`running` threads recover as `paused` and require an
  explicit `resume_agent` message after restart.
- Children use isolated AI-provider clients and may run concurrently. `model`
  may select an already configured endpoint name or exact model id.
- Each parent session can have at most twelve running child Agents; additional
  spawn requests are rejected until a slot is available.
- A child has a 30-minute emergency wall-clock deadline, not a fixed reasoning
  round limit. Progress and anti-loop guards remain active.
- A child conclusion is evidence to assess, not an instruction that overrides
  the parent, system policy, scope, or user decision.
- Never include secrets or an entire conversation dump in `context`; pass
  stable case, artifact, task, KB, or session references instead.
