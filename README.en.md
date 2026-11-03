<p align="center">
  <img src="assets/brand/readme-banner.svg" width="100%" alt="ArgusAI: a local workbench connecting Agent execution, browsers, proxy traffic, reverse engineering and evidence">
</p>

<h1 align="center">ArgusAI</h1>

<p align="center">
  <strong>Give the Agent a goal. Let it do the work in ArgusAI.</strong><br>
  Local workspace · Long autonomous runs · Proactive defense · Continuous security exercises
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.7-0967a6?style=flat-square" alt="Version 0.1.7">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-555f70?style=flat-square" alt="Windows, macOS and Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPLv3-0078a8?style=flat-square" alt="License GPL-3.0-only"></a>
</p>

<p align="center">
  <a href="wiki/Home.md"><strong>Documentation (中文)</strong></a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#workbench-architecture">Architecture</a> ·
  <a href="#in-action">In action</a> ·
  <a href="#continuing-execution-and-proactive-discovery">Automation</a> ·
  <a href="https://github.com/HG-ha/Argusai/releases">Releases</a> ·
  <a href="README.md">简体中文</a> ·
  <a href="https://dawahezhi.com/">Website</a>
</p>

## Agent across the workbench

ArgusAI is an Agent-native security workbench open-sourced by [DAWA Technology](https://dawahezhi.com/) (达瓦科技). It puts an AI assistant, scan tasks, browsers, proxy traffic, reverse engineering samples and evidence in a local workspace, supporting internal-network defense, application defense, attack tracing and continuing security exercises through material review, controlled validation, reporting and remediation testing.

Give the Agent the goal, materials and authorized scope. It organizes scanning, browser, proxy, reverse engineering, knowledge and scheduled tasks, continuing through analysis, validation and delivery. The Agent is the default entry point for work; feature pages let you observe progress, inspect evidence and intervene when needed. The GUI, CLI and Python core connect to the same application capabilities.

The full conversation entry in the sidebar and the movable floating button both open the same Agent workspace. The Agent understands the current application page, the task or reverse job being viewed, and related work state. Reference specific materials so it can continue the work in front of you. Feature pages and conversations share the same tasks, samples and evidence.

The project is intended for internal security teams, application security engineers, researchers and explicitly authorized testers. Its general Agent runtime also supports file processing, routine information work and desktop collaboration; the built-in tools and workflows currently focus on security analysis and reverse engineering.

## Workbench architecture

The main Agent plans around the goal, reads Skills, discovers tools and delegates to subagents or a persistent team as needed. Workbench capabilities return results with references to materials and evidence. The Agent reviews them, updates the plan and delivers a report or continues the next stage.

```mermaid
flowchart TB
    Input["Desktop / floating entry / CLI<br/>Paired chat on a phone or other device"] --> Session["Session execution control<br/>session_id / run lock / runtime binding"]
    Events["Interval / Cron<br/>Webhook / file changes"] --> Session
    Session --> Native["AssistantSession<br/>Native endpoints / streaming provider<br/>Tool loop / progress guards"]
    Session --> ACP["Local Agent via ACP<br/>Codex / Cursor<br/>Claude Code"]
    Native --> Main["Main Agent / task coordination<br/>Plan, delegate and call tools<br/>Review state and results"]
    ACP --> Main

    Main -->|Delegate / collect results| Sub["Temporary subagents<br/>Separate history / workbench tool scope<br/>Native or ACP workers"]
    Main -->|Coordinate / review| Team["Persistent Agent Team<br/>Message sequence / read cursors<br/>Member execution inboxes"]
    Main -->|Read as needed| Skills["Skills<br/>Task methods and workflows"]
    Main --> Tools["Frozen core tools[] / tool catalog<br/>Discover schemas, dispatch the real tool"]
    Sub --> Tools
    Team --> Tools
    Skills -.->|Methods| Tools

    Tools --> Web["Browser / MITM proxy<br/>Page actions, capture,<br/>breakpoints and replay"]
    Tools --> Analysis["Scans / reverse engineering<br/>Checks, JS / WASM,<br/>mobile and binary analysis"]
    Tools --> Execution["Sandbox / hosts / desktop<br/>Files, scripts, SSH and devices"]
    Tools --> Knowledge["Knowledge / evidence / reports<br/>Cases, artifacts and evidence"]
    Tools -.-> Extensions["Optional tools and integrations<br/>MCP / external scanners<br/>Ghidra / Frida / JADX"]
    Web --> Review["Main Agent reviews results and evidence"]
    Analysis --> Review
    Execution --> Review
    Knowledge --> Review
    Extensions -.-> Review
    Review --> Delivery["Reports / PoCs<br/>Summaries and session sandbox files"]
    Delivery -.->|Remote delivery as agreed in the task| Outbox["Paired identity → Outbox<br/>Adapter sends<br/>Delivery status checks"]
    Review -->|Update plan, continue / retest| Main
    Sub -.->|Completion notice / eligible parent wake| Main
    Team -->|Queue public messages / respond or pass| Team
    Team -.->|Read chat and artifacts| Review
```

Domain tools and Skills are discovered and used as needed. The session sandbox provides files and a script environment; browser, proxy and scan services, and external Agents, use their own execution environments. See [execution loops and wakeups](#continuing-execution-and-proactive-discovery) for control flow, and [architecture and development](wiki/Architecture-and-Development.md) for module entry points.

### Runtime design

Conversation history, execution state, streaming events and domain artifacts are managed separately, connected by run identities and material references. Long tasks and multi-Agent work use these mechanisms:

| Design | Implementation |
| --- | --- |
| [Run identity and execution ownership](argusai/core/assistant/run_jobs.py) | `session_id` identifies the conversation, durable `job_id` the run, and temporary `run_id` the incremental event stream. A resumed run can open a new stream. The session lock is released after the worker actually exits; atomic snapshots and ordered JSONL events preserve run state. |
| [Stable control surface and capability catalog](argusai/core/assistant/session_chat_loop.py) | The native session's core `tools[]` and system tool catalog are frozen. `tool_search` returns domain schemas and `invoke_catalogue_tool` executes them without growing the permanent tool array. Skills use a shortlist followed by reading selected content. |
| [Context budgets and protocol repair](argusai/core/assistant/session_budget.py) | Before native model calls, old tool results and early conversation chunks are compacted, and tool-call/result adjacency is repaired. Selected high-value results retain complete Artifacts while the model continues with summaries and `artifact_ref`, reading originals when needed. |
| [Resource-aware tool batches](argusai/core/assistant/session_loop_control.py) | The native tool loop batches consecutive read-only calls according to permitted concurrency and non-conflicting resource keys. Mutating, control and pending-confirmation calls retain their execution boundaries. Results are committed in the original call order. |
| [Permissions and capability projection](argusai/core/assistant/confirmation_broker.py) | Catalog calls resolve the real tool before policy, confirmation and execution. Todo plan approval, individual tool decisions and readonly/scoped child tool sets have separate roles; delegated capabilities and confirmation mode determine child permissions. |
| [ACP sessions and the tool bridge](argusai/core/agent_runtimes/tool_broker.py) | ACP negotiates `initialize → session/new or load → prompt`. Workbench tools use a loopback MCP broker with a random session token and a per-binding lock for ordered confirmation and execution. External Agents manage their own tools through their runtime. |
| [Collaboration queues and conditional wakeups](argusai/core/assistant/agent_threads.py) | Subagents retain separate histories and tool scopes. Team Room sequence numbers and read cursors are separate from member execution inboxes. Busy members retain input, with batching, deduplication and optional passing; completion notices are recorded and batched before attempting an eligible parent wakeup. |
| [Durable scheduling control](argusai/core/assistant/cron.py) | A data-root host lease and execution lock coordinate scheduling. Jobs, run records, event queues and file baselines use atomic persistence. Signed Webhooks, file changes and time rules target existing, job-owned or new sessions, carrying checkpoints and task materials forward. |
| [Material references and derivation](argusai/core/workspace/cases.py) | Case Artifacts store original bytes, SHA-256 and size; Evidence links materials, interactions and Findings. ReverseJob tracks samples and derived files. Scan reports, `artifact_ref` and `rev://` have their own storage and retrieval paths and can be explicitly linked. |
| [Streaming events and the UI protocol](argusai/core/assistant/ui_protocol.py) | Text deltas produce `text_chunk`; tool results and confirmations produce `block_upsert`. The UI protocol normalizes block identity, versions and actions for each surface. `StreamHub` serves in-memory events by offset, separately from the durable RunJob journal. |
| [Channel outbox and delivery receipts](argusai/core/channels/store.py) | Files from the executing session's sandbox are routed by paired identity into a durable Outbox. A sender worker claims a lease, calls the Adapter, records part-level progress and retries within a limit. `channel_delivery_status` distinguishes `pending`, `processing`, `sent` and `failed`, preserving provider errors. |

See [Agent runtime and data flow](wiki/Architecture-and-Development.md#agent-运行时与数据流) for threads, event streams, storage, key functions and design boundaries.

**Use model endpoints or a local Agent.** Configure AI endpoints directly, or connect locally installed and signed-in Agents such as Codex, Cursor and Claude Code through ACP. They can work inside ArgusAI and call workbench capabilities through its tool bridge. An ACP main conversation does not require a separate native ArgusAI model endpoint; native subagents, team members and other AI analysis tasks use their own runtime and model configuration.

**Continue work on PCs and servers from your phone.** Pair QQ, Feishu, Telegram or another supported channel to send tasks, check progress and receive reports and files from a phone, tablet or another computer. The Agent uses SSH, MCP or other connections configured on its work host to operate reachable computers and servers. Desktop control acts on the desktop hosting the tool, and results can return to the paired channel. See [integrations and messaging](wiki/Integrations.md).

For delivery after completion, include “Send the summary and report files back to this paired channel, then verify delivery” in the task's acceptance criteria. Ordinary channel replies can use `channel_reply` for the exact inbound identity; proactive messages use `channel_send_message`, and files use `channel_send_file`, subject to routing and tool confirmation policy. Queue acceptance means delivery was requested; `sent` means the send interface succeeded, not that the recipient read it. A scheduled task's `report_to_origin` reports to the application conversation that created it; agree separately on channel recipients and artifacts for QQ or other platforms.

## In action

<table>
<tr>
<td width="50%" align="center" valign="top">
<strong>Desktop Agent</strong>
<br>
  <a href="website/assets/screens/agent-light.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/agent-dark.webp">
      <img src="website/assets/screens/agent-light.webp" width="100%" alt="ArgusAI desktop Agent">
    </picture>
  </a>
</td>
<td width="50%" align="center" valign="top">
<strong>CLI</strong>
<br>
  <a href="website/assets/screens/cli-dark.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/cli-dark.webp">
      <img src="website/assets/screens/cli-light.webp" width="100%" alt="ArgusAI CLI with both sidebars open">
    </picture>
  </a>
</td>
</tr>
</table>

<details>
<summary><strong>Sandbox preview</strong></summary>

<p align="center">
  <a href="website/assets/screens/sandbox-light.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/sandbox-dark.webp">
      <img src="website/assets/screens/sandbox-light.webp" width="100%" alt="ArgusAI sandbox file preview">
    </picture>
  </a>
</p>

</details>

## One workspace for analysis and execution

<table>
<tr>
<td width="50%" valign="top">
<h3>Conversations, floating entry and teams</h3>
<p>Supports autonomous execution on the scale of tens of hours within one Agent turn, combining tool use, Skills, plans and todos with context compaction, evidence archives and progress checks.</p>
<p>Persistent teams divide work by role, model and tool scope.</p>
<p>The conversation menu and floating button open the same Agent workspace. The button shows running and unread states; expand it to continue a conversation, review tool steps and handle confirmations. Each conversation saves its own model and auto-approval state.</p>
<a href="wiki/Agent-and-Sessions.md">Agent entry points, teams and session settings →</a>
</td>
<td width="50%" valign="top">
<h3>Durable tasks and discovery</h3>
<p>Interval, Cron, signed Webhook and file-change triggers, running in a new conversation, a job-owned conversation or an existing one.</p>
<p>Resume from checkpoints, schedule patrols, compare baselines and report changes.</p>
<a href="wiki/Automation.md">Scheduling, continuation and patrols →</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>Web auditing and traffic</h3>
<p>Built-in web checks, live browser viewing and takeover, raw requests and replay; HTTP/1.1 and HTTP/2 MITM, WebSocket capture, breakpoints and optional rewrites.</p>
<p>Import HAR and Burp logs to bring existing materials into an investigation.</p>
<a href="wiki/Web-Audit-and-Proxy.md">Scans, browser and proxy →</a>
</td>
<td width="50%" valign="top">
<h3>Reverse engineering workspace</h3>
<p>Organize JS, WASM, APK, IPA, PE, ELF and HAR in ReverseJobs, linking triage, extraction, decompilation, dynamic observations and evidence.</p>
<p>Connect external tools such as JADX, APKTool, Ghidra, Frida, capa and yara as needed.</p>
<a href="wiki/Reverse-Workbench.md">Samples and reverse tasks →</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>Evidence, memory and knowledge</h3>
<p>Link Cases, Artifacts, Evidence and findings. Session memory, knowledge and large-output archives support review and continued work.</p>
<p>Export scan reports as HTML, Markdown and JSON for your deliverables.</p>
<a href="wiki/Enterprise-Workflows.md">Enterprise workflows and outputs →</a>
</td>
<td width="50%" valign="top">
<h3>Tools and extensions</h3>
<p>Python Checks, Agent Tools, Markdown Skills, MCP, local ACP agents, paired QQ/Feishu/Telegram and other messaging channels, and desktop automation. QQ supports the official QR binding flow as well as manual App ID / Client Secret setup.</p>
<p>Channels support platform-adapted live stages and multi-bubble <code>channel_reply</code> responses. The same global user-habits profile also governs desktop chats and the floating entry.</p>
<p>Orchestrate external scanners and specialist workflows for cloud, IaC, SBOM, offline traffic and firmware.</p>
<a href="wiki/Integrations.md">Integrations and tool dependencies →</a>
</td>
</tr>
</table>

Describe a goal in the full conversation or through the floating entry, such as “Continue the current application audit, refine the plan, advance the analysis within scope, and preserve evidence.” The Agent organizes tool execution around the task. Open feature pages to inspect progress and results; when more material is needed, drag a sandbox file onto the floating button to reference it in the conversation and continue.

Built-in checks, skill-guided workflows and external integrations handle different parts of the process. The [Wiki manual](wiki/Home.md) describes their inputs, dependencies and usage. The Wiki's primary documentation is in Chinese; this page provides the English project overview and quick start.

## Continuing execution and proactive discovery

### Autonomous turns spanning tens of hours

ArgusAI supports autonomous execution on the scale of tens of hours within a single Agent turn. A turn is one user task containing repeated model and tool calls, not one HTTP or model request. The runtime has no fixed model/tool round-count ceiling. Its default emergency wall-clock limit is 7 days, with progress checks, loop detection, cancellation and deadline handling retained.

This is a runtime configuration and control-flow capability. Actual duration depends on the task, model quota, tools, resources and authorization. Long investigations, sample batches and multi-stage validation can progress within one turn; durable tasks and the scheduler carry work across turns or resume it on a later event.

The native runtime keeps a stable core control surface and discovers domain tools through the catalog. Before model requests it manages history against the context budget; large results can use summaries and artifact references. The sequence below distinguishes continuing the current turn, waking a parent after subtask completion, and querying background work. ACP conversations use the selected external Agent's reasoning loop.

```mermaid
sequenceDiagram
    participant U as User / session entry
    participant A as Main Agent runtime
    participant M as Model
    participant T as Tools / subagents
    participant S as State and artifacts
    U->>A: Goal, materials or continuation request
    loop Work remains actionable and runtime conditions allow
        A->>M: Stable core tools / budgeted context
        M-->>A: Next call, delegation or wait
        alt Invoke workbench capabilities
            A->>T: Discover tools, read Skills, confirm and execute
            T-->>A: Results / evidence references / job receipt
        else Short wait in the current turn
            A->>A: wait_for_duration, returns when due, cancellable
        else Wait for subagents
            A->>T: wait_for_agents
            T-->>A: Status after completion, timeout or user interjection
        else Query a background job
            A->>T: background_job_status(job_ref)
            T-->>A: Current status or results, wait and query as needed
        end
        A->>S: Update Todo / checkpoints, save needed artifacts
        S-->>A: Summaries, references and next-step context
    end
    opt Subtasks end, parent is idle and eligible to continue
        T-->>A: Batched agent_completion notice wakes the parent
        A->>M: Read results, continue review and reporting
    end
    A-->>U: Deliver / wait for input / explain why the turn stopped
```

A user interjection can end `wait_for_agents` while workers continue; `wait_for_duration` is a cancellable short wait. Ordinary background jobs are queried by `job_ref`, while persistent teams coordinate through chat and member queues. The scheduled tasks below can also supervise that work.

### Cross-turn continuation and events

The Agent stores progress in todos, checkpoints, knowledge and artifacts. The scheduler reconnects that work on the next trigger.

<div align="center">

```mermaid
flowchart TB
    Register["Configured persistent task"] --> T
    T["Interval / Cron<br/>Signed Webhook / file changes"] --> Scheduler["Scheduler<br/>Host lease / execution lock"]
    Scheduler --> Ready{"Session available?"}
    Ready -->|Busy| Busy["Defer and check again<br/>May skip after the deferral window"]
    Busy --> Waiting["Wait for a later check, time or event"]
    Ready -->|Available| Destination["Execution destination"]
    Destination --> Existing["session<br/>Wake a specified conversation"]
    Destination --> Dedicated["dedicated_session<br/>Create once, then reuse"]
    Destination --> New["new_session<br/>A separate conversation each run"]
    Existing --> Context["Read the task context, Todo,<br/>checkpoints, knowledge and artifact references"]
    Dedicated --> Context
    New --> Context
    Context --> Run["Execute the next stage<br/>Patrol / analyze / validate<br/>Check team or background work"]
    Run --> Save["Save progress and artifacts<br/>Compare baselines / update checkpoints"]
    Save --> Notify["Notify by policy<br/>Optionally report to the origin conversation"]
    Notify --> Enabled{"Enabled and eligible?"}
    Enabled -->|Continue| Waiting
    Waiting --> T
    Enabled -->|Disabled / run limit reached| Stop["Stop future triggers<br/>Keep records and artifacts"]
    Scheduler -->|Disabled / run limit reached| Stop
```

</div>

New conversations carry work through explicit task context, knowledge and artifacts; existing and job-owned conversations retain their own history. Finishing one run can leave the schedule waiting for its next trigger. The Agent disables it as agreed when the objective is met, or it stops through user action or its run limit. The scheduling host must remain running, and each scheduled execution has its own deadline.

| Trigger | Useful work |
| --- | --- |
| Fixed interval `interval` | Periodic patrols, background scan progress, new traffic review |
| Calendar rule `cron` | Daily asset review, weekly exposure checks, scheduled briefings |
| Signed event `webhook` | Follow-up on test builds, external alerts or other process events |
| File changes `file_change` | Analyze changes in logs, reports or a sample directory |

Choose the execution destination independently of the trigger:

| Destination | How context continues |
| --- | --- |
| New conversation per run `new_session` | Separate transcripts; task context, knowledge and artifacts carry required state |
| Job-owned conversation `dedicated_session` | The task accumulates records across runs in its own conversation |
| Existing conversation `session` | Continue with that conversation's history, todos and checkpoints |

Long tasks can use `max_runs=0` to run without a fixed execution-count limit. Each turn reads checkpoints, performs the next step and saves state. This durable, segmented continuation, together with context compaction and evidence archives, supports long investigations, recurring patrols and multi-stage work. The scheduler, models and tools must remain available; information, resources and authorization still determine whether each turn can proceed.

<details>
<summary><strong>Example tasks: patrols and change notifications</strong></summary>

> Review the authorized test services each day during the agreed window, following the procedure stored in the knowledge base. Compare previous records for new assets and configuration changes, and preserve evidence. Stay quiet when nothing changes. Notify me about new risks, execution failures or required human action. Do not rewrite traffic, remediate systems or expand scope.

> Continue the unfinished analysis in this conversation. Read the todos and latest checkpoint first, avoid repeating completed steps, and save progress each turn. Report what is missing when you need input from me. When the work is complete, stop continuation and organize the conclusions.

</details>

The [automation manual](wiki/Automation.md) covers triggers, destinations, run counts and notification policies.

## Enterprise workflows

| Work area | Inputs and process | Work materials |
| --- | --- | --- |
| Internal-network defense | Define authorized IP/CIDR, host and SSH scope; discover assets and analyze service exposure. On hosts with configured SSH, read listening ports, firewall summaries, failed logins and critical service versions, then compare baselines | Asset coverage, exposure changes, risk priorities, hardening recommendations and raw evidence references |
| Application defense | Combine staging targets, test identities, API materials, HAR/JS and business logs with web checks, browser and proxy analysis; review configuration, authentication, access control and unusual requests, then test remediation | Interface and risk inventories, reproducible requests, request/response evidence, remediation guidance and retest records |
| Attack tracing | Review business logs, host audit, proxy traffic and supplied WAF/EDR/SIEM alerts within a selected time window; correlate timestamps, accounts, IPs, processes and request identifiers to test hypotheses | Event timelines, evidence relationships, observed facts versus inferences, confidence and evidence gaps; attribution follows the available evidence |
| Continuing security exercises | Within authorized assets and agreed labs, combine long Agent turns, periodic jobs and event triggers to connect exposure discovery, separately authorized controlled validation, detection evidence review and remediation testing | Exercise chains, detection coverage and gaps, change notifications, review materials and reusable skills and defensive check procedures |

Alerts and remote state come from materials supplied by the user or configured tools, with checks and validation organized within the authorized scope. The responsible team reviews vulnerability conclusions, risk acceptance, production changes and incident response decisions. See [enterprise workflows](wiki/Enterprise-Workflows.md) for inputs, review points and deliverable organization.

## Quick start

The current version is `0.1.7`. Download the prebuilt package for your platform and its `.sha256` from [Releases](https://github.com/HG-ha/Argusai/releases), then follow [installation](wiki/Installation.md) to verify and install it. The terminal examples use an `argusai` command entry. Without one, use `.\ArgusAI.exe` from the Windows installation directory or `./ArgusAI` from the extracted Linux directory; see the [CLI reference](wiki/CLI-Reference.md) for the macOS entry.

```sh
argusai version --json
argusai --gui
argusai chat
```

On first launch, choose a data directory and configure native AI endpoints and models, or select a locally installed, signed-in and tested ACP Agent. Check browser and external tool paths as needed. Open the full Agent conversation or floating entry and give the Agent the goal, materials and constraints so it can plan and start work. For a first task, provide an approved HAR or JS file and ask the Agent to inventory interfaces, identify leads and archive evidence, then inspect the results on the relevant feature pages.

| Entry point | Start with | Read next |
| --- | --- | --- |
| Full Agent conversation | Open or create a conversation from the sidebar, then give the Agent a goal to execute | [Agent and sessions](wiki/Agent-and-Sessions.md) |
| Agent floating button | Continue current work from a feature page; drag to reposition and check running/unread indicators | [Floating entry and workbench assistance](wiki/Agent-and-Sessions.md) |
| Launch the desktop | Installed desktop entry, or `argusai --gui` | [Quick start](wiki/Quick-Start.md) |
| Interactive terminal | `argusai` or `argusai chat` | [CLI reference](wiki/CLI-Reference.md) |
| Release package | Download the published package and `.sha256` from [Releases](https://github.com/HG-ha/Argusai/releases) | [Installation](wiki/Installation.md) |

Windows needs WebView2 Runtime; Windows ARM64 also needs the native Microsoft .NET Desktop Runtime. macOS desktop automation requires system permissions. Linux needs GTK and WebKitGTK desktop libraries. External models, scanners, reverse CLIs and devices are prepared for the features you use; see [installation](wiki/Installation.md).

<details>
<summary><strong>Source development, build and release</strong></summary>

Source execution needs Python 3.12+, uv, Node.js 18+ and npm. Prepare dependencies and build the frontend from the repository root:

```sh
uv sync --locked
npm --prefix argusai/webui ci
npm --prefix argusai/webui run build
uv run argusai --gui
```

All three platforms use the Python build entry point. Run on the target system with an environment for the target architecture:

```sh
uv run --locked --group build python build.py --help
uv run --locked --group build python build.py --dry-run
uv run --locked --group build python build.py --jobs 2
```

Use `--nsis` on Windows, `--dmg` on macOS or `--onefile` for a single executable. Linux also creates a release archive. [Build and release](wiki/Build-and-Release.md) covers dependencies, signing, checksums and the GitHub Actions workflow.

Read [AGENTS.md](AGENTS.md) for development rules and [architecture and development](wiki/Architecture-and-Development.md) for extension points. Run tests with `uv run pytest`, and use `npm --prefix argusai/webui run dev` for frontend development.

</details>

## Documentation map

The linked Wiki pages below are primarily in Chinese.

<table>
<tr>
<td width="33%" valign="top">
<h3>Get started</h3>
<p><a href="wiki/Quick-Start.md">Quick start</a><br><a href="wiki/GUI-Guide.md">Illustrated desktop guide</a><br><a href="wiki/Installation.md">Installation</a><br><a href="wiki/Agent-and-Sessions.md">Agent, conversations and teams</a><br><a href="wiki/CLI-Reference.md">CLI examples</a><br><a href="wiki/CLI-Options.md">Full CLI parameters</a></p>
</td>
<td width="33%" valign="top">
<h3>Do the work</h3>
<p><a href="wiki/Automation.md">Scheduling, continuation and patrols</a><br><a href="wiki/Web-Audit-and-Proxy.md">Web auditing and proxy</a><br><a href="wiki/Reverse-Workbench.md">Reverse workbench</a><br><a href="wiki/Enterprise-Workflows.md">Enterprise workflows</a></p>
</td>
<td width="33%" valign="top">
<h3>Configure and extend</h3>
<p><a href="wiki/Integrations.md">Models, tools and integrations</a><br><a href="wiki/Data-and-Safety.md">Data and scope of use</a><br><a href="wiki/Troubleshooting.md">Troubleshooting</a><br><a href="wiki/Build-and-Release.md">Build and release</a></p>
</td>
</tr>
</table>

## Data and scope of use

Sessions, tasks, knowledge, reverse artifacts and logs are stored under the configured data directory. AI requests go to user-configured services; search, downloads, messaging and out-of-band verification may also contact external services. Session file and runtime separation organizes execution; actual access remains subject to tool authorization and host permissions.

Use ArgusAI on systems you own, explicitly authorized assessments and isolated labs. Establish authorization for targets, methods, time windows and impact. Review model conclusions and assess active scans, traffic rewrites, dynamic instrumentation and production schedules. See [data and scope of use](wiki/Data-and-Safety.md).

## License

Except for third-party content with separate notices, ArgusAI is licensed under [GNU GPL version 3 only](LICENSE) (`GPL-3.0-only`). Later versions are not automatically permitted. Individuals and businesses may use, modify and commercially use it under this license; business use does not require a separate software license purchase.

When distributing the program or a combined work covered by the GPL, provide the Corresponding Source, preserve copyright and license notices, and meet the other applicable GPLv3 conditions. See [LICENSE](LICENSE) for the complete terms.

Bundled third-party components retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for their scope and notices. DAWA Technology may provide paid deployment, integration, customization and technical support services. These services do not change the rights granted by the GPL.

## Participate

Contribute reproducible issues, tool adapters, checks, Skills or interface improvements. Include the version, operating system, reproduction steps and redacted logs. Preserve third-party tool and vendor license notices.

Contributions use the license applicable to the relevant files: `GPL-3.0-only` for project code and the original licenses for third-party content. Contributors retain their copyright.

[Report an issue](https://github.com/HG-ha/Argusai/issues) · [Development rules](AGENTS.md) · [Architecture](wiki/Architecture-and-Development.md) · [Wiki maintenance](wiki/Wiki-Maintenance.md)

<p align="center">Open-sourced by <a href="https://dawahezhi.com/">DAWA Technology</a> (达瓦科技) · <a href="https://github.com/HG-ha/Argusai">HG-ha/Argusai</a></p>
