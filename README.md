<p align="center">
  <img src="assets/brand/readme-banner.svg" width="100%" alt="ArgusAI：连接 Agent、浏览器、代理流量、逆向与证据的本地安全工作台">
</p>

<h1 align="center">ArgusAI</h1>

<p align="center">
  <strong>把目标交给 Agent，让它在 ArgusAI 中完成工作。</strong><br>
  本地工作台 · 长时自主执行 · 主动防护 · 持续攻防
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.1.10-0967a6?style=flat-square" alt="版本 0.1.10">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-555f70?style=flat-square" alt="Windows、macOS 与 Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPLv3-0078a8?style=flat-square" alt="许可证 GPL-3.0-only"></a>
</p>

<p align="center">
  <a href="wiki/Home.md"><strong>使用文档</strong></a> ·
  <a href="wiki/Quick-Start.md">快速开始</a> ·
  <a href="#工作架构">工作架构</a> ·
  <a href="#运行界面">运行界面</a> ·
  <a href="#持续执行与主动发现">持续执行</a> ·
  <a href="https://github.com/HG-ha/Argusai/releases">下载发行包</a> ·
  <a href="README.en.md">English</a> ·
  <a href="https://dawahezhi.com/">官网</a>
</p>

## Agent 贯穿整个工作台

ArgusAI 是由[达瓦科技](https://dawahezhi.com/)开源的 Agent 原生安全工作台。它将 AI 助手、扫描任务、浏览器、代理流量、逆向样本与证据放进同一个本地工作区，面向内网安全防护、应用安全防护、攻击溯源与持续攻防，支持从目标与材料整理，一直推进到受控验证、报告和整改复测。

把目标、材料和授权范围交给 Agent，由它组织扫描、浏览器、代理、逆向、知识管理与定时任务，连续推进分析、验证和交付。Agent 是开展工作的默认入口；各功能页面用于观察进度、查看证据，并在需要时介入。GUI、CLI 与 Python 内核连接同一套工作能力。

侧栏的完整对话与可拖动悬浮球都是任务入口，进入的是同一个 Agent 工作区。Agent 能理解当前应用页面、正在查看的任务或逆向作业，以及相关工作状态；引用具体材料后，它可以接着你正在做的工作继续执行。功能页与对话之间共享任务、样本和证据。

项目面向企业安全团队、应用安全工程师、安全研究人员和明确授权的测试人员。底层 Agent 运行时也支持文件处理、日常信息整理和桌面协作，当前内置工具与工作流重点覆盖安全分析和逆向研究。

## 工作架构

主 Agent 围绕目标建立计划，按需读取 Skills、发现工具，委派子 Agent 或组织持久小队，联动工作台各项能力。工具围绕有状态的工作对象组织，Agent 可以查询当前状态、沿同一对象继续操作，记录步骤与产物，再复核、调整计划，交付报告或进入下一阶段。

```mermaid
flowchart TB
    Input["桌面 / 悬浮球 / CLI<br/>手机等设备的配对通讯渠道"] --> Session["会话执行控制<br/>session_id / 运行锁 / runtime 绑定"]
    Events["间隔 / Cron<br/>Webhook / 文件变化"] --> Session
    Session --> Native["AssistantSession<br/>原生端点 / 流式 Provider<br/>工具循环 / 进展守卫"]
    Session --> ACP["本地 Agent（ACP）<br/>Codex / Cursor<br/>Claude Code"]
    Native --> Main["主 Agent / 任务编排<br/>计划、委派、工具调用<br/>状态与结果复核"]
    ACP --> Main

    Main -->|并行委派 / 汇总| Sub["临时子 Agent<br/>独立历史 / 工作台工具范围<br/>原生或 ACP 子 Agent"]
    Main -->|组织 / 复核| Team["持久 Agent Team<br/>群聊序号 / 阅读游标<br/>成员执行邮箱"]
    Main -->|按需读取| Skills["Skills<br/>任务方法与工作流"]
    Main --> Tools["固定核心 tools[] / 工具目录<br/>按需取 schema / 调用工具<br/>读状态 / 操作工作对象"]
    Sub --> Tools
    Team --> Tools
    Skills -.->|方法指导| Tools

    Tools --> Web["浏览器 / MITM 代理<br/>标签页 / 流量 / 暂停请求<br/>断点 / 重放 / HAR"]
    Tools --> Analysis["扫描 / 全域逆向<br/>Task / ReverseJob<br/>样本 / 派生产物 / 分析记录"]
    Tools --> Execution["沙盒 / 主机 / 桌面<br/>Shell 会话 / 文件 / 环境<br/>脚本、SSH 与设备"]
    Tools --> Knowledge["知识 / 证据 / 报告<br/>Case、Artifact、Evidence"]
    Tools --> Recall["长期与跨会话记忆<br/>检索 → 会话 / 消息定位<br/>按页回读原文与归档引用"]
    Tools -.-> Extensions["可选扩展与工具链<br/>MCP / 外部扫描器<br/>Ghidra / Frida / JADX"]
    Web --> Review["主 Agent 复核<br/>状态 / 步骤 / 证据"]
    Analysis --> Review
    Execution --> Review
    Knowledge --> Review
    Recall -->|查证旧工作与关联任务| Review
    Extensions -.-> Review
    Review --> Delivery["交付报告 / PoC<br/>摘要与会话沙盒产物"]
    Delivery -.->|按任务约定远程交付| Outbox["配对身份 / 持久 Outbox<br/>Adapter 发送<br/>投递状态核验"]
    Review -->|更新计划 / 继续 / 复测| Main
    Sub -.->|完成通知 / 条件唤醒| Main
    Team -->|公开消息入队，成员处理或跳过| Team
    Team -.->|查询群聊与产物| Review
```

内置领域工具与 Skills 按需发现和使用。会话沙盒提供文件与脚本环境，浏览器、代理、扫描服务及外部 Agent 使用各自的运行环境。单轮循环与跨轮唤醒见[持续执行与主动发现](#持续执行与主动发现)，模块入口见[架构与开发](wiki/Architecture-and-Development.md)。

### 运行时设计

会话历史、执行状态、流式事件和领域产物分别管理，再通过运行身份与材料引用连接。长任务与多 Agent 协作依赖以下机制：

| 设计点 | 实际实现 |
| --- | --- |
| [运行身份与执行所有权](argusai/core/assistant/run_jobs.py) | `session_id` 标识会话，持久 `job_id` 标识任务运行，临时 `run_id` 标识增量事件流。恢复运行可以创建新流；真正的 worker 退出后才释放会话运行锁。运行状态以原子快照和单调 JSONL 事件保存。 |
| [稳定控制面与能力目录](argusai/core/assistant/session_chat_loop.py) | 原生会话核心 `tools[]` 和 system 工具目录冻结；`tool_search` 返回领域 schema，`invoke_catalogue_tool` 执行真实工具，发现新能力不反复扩展常驻数组。Skills 先给短名单，再按需读取正文。 |
| [有状态工具与连续操作](wiki/Architecture-and-Development.md#有状态工具与连续操作) | 模型使用对象 ID、步骤和材料引用，宿主管理实际资源与生命周期。浏览器标签、代理流量与暂停请求、交互式 Shell、Task、ReverseJob 各有状态查询及后续操作；Todo 记录阶段、完成条件、阻塞与证据，支持按当前状态推进。 |
| [上下文预算与协议修复](argusai/core/assistant/session_budget.py) | 原生模型请求前压缩旧工具结果、提炼早期对话，并修复 tool-call/result 邻接关系。选定高值输出保留完整 Artifact，模型使用摘要与 `artifact_ref` 续接，按需重新读取原文。 |
| [长期与跨会话记忆召回](argusai/core/assistant/session_memory.py) | 摘要、关键词索引与会话历史分开：索引保留全部可见用户消息摘要及最近 600 条消息中的细节。`search_memory` / `search_all_memory` 定位旧会话和消息，再由 `list_session_messages` / `read_session_message` 或 `read_assistant_session` 分页、按条回读；有归档引用时，沿 `case_id` / `artifact_ref` 读取原始产物。 |
| [按资源分批执行](argusai/core/assistant/session_loop_control.py) | 原生工具循环将连续只读调用按允许的并行度和 resource keys 组成无冲突波次；修改、控制和待确认调用保留各自执行边界。并行完成后仍按原调用顺序写入历史。 |
| [调用授权与能力投影](argusai/core/assistant/confirmation_broker.py) | 先展开真实工具名，再进入策略、确认与执行。Todo 计划批准、具体工具确认、子 Agent 的 readonly/scoped 工具范围分别处理，委派能力与确认模式一起决定子任务权限。 |
| [ACP 会话与工具桥](argusai/core/agent_runtimes/tool_broker.py) | ACP 按 `initialize → session/new 或 load → prompt` 协商与运行。工作台工具经随机会话 token 绑定的回环 MCP broker 接入，每个绑定以执行锁串行确认和调用；外部 Agent 自有工具仍由其运行时管理。 |
| [协作消息与条件唤醒](argusai/core/assistant/agent_threads.py) | 子 Agent 保存独立历史和能力范围；Team Room 的消息序号、阅读游标与成员执行邮箱分开。成员忙时保留输入，可合批、去重和静默跳过；完成通知先记录，再合批尝试唤醒可运行的父会话。 |
| [持久调度控制](argusai/core/assistant/cron.py) | 用数据目录级宿主租约和执行锁协调调度；计划、运行记录、事件队列与文件基线原子落盘。签名 Webhook、文件变化及时间规则可投递到指定、专属或新会话，按检查点与任务材料接续。 |
| [材料引用与派生关系](argusai/core/workspace/cases.py) | Case Artifact 保存原始内容、SHA-256 与大小，Evidence 关联材料、交互和 Finding；ReverseJob 记录样本与派生物。扫描报告、`artifact_ref` 和 `rev://` 各有存储及读取路径，可显式关联。 |
| [流式事件与界面协议](argusai/core/assistant/ui_protocol.py) | 正文增量形成 `text_chunk`，工具结果与确认形成 `block_upsert`；UI 协议规范 block 身份、版本和 action，再投影到各入口。`StreamHub` 按 offset 消费内存事件，与持久 RunJob 日志分开。 |
| [渠道出站与投递回执](argusai/core/channels/store.py) | 文件由执行会话沙盒按配对 identity 路由进入持久 Outbox；发送 worker 领取租约，由 Adapter 投递，记录分段进度并有限重试。`channel_delivery_status` 区分 `pending`、`processing`、`sent`、`failed`，保留平台错误原因。 |

执行线程、事件流和存储之间的关系、关键函数及设计边界，见 [Agent 运行时与数据流](wiki/Architecture-and-Development.md#agent-运行时与数据流)。

**Agent 围绕有状态工具连续工作。** 它可以继续操作同一浏览器标签页，检查代理中的既有流量和暂停请求，向同一个交互式 Shell 发送后续命令，或沿 ReverseJob 中的样本、分析结果和派生产物继续研究。Todo 记录步骤，任务与作业提供状态，证据引用连接阶段产物；Agent 根据这些信息决定下一步。实时连接、运行进程与持久记录分别管理，接续工作时先查询状态，再决定继续操作或重建环境。示例见[有状态工具与连续操作](wiki/Agent-and-Sessions.md#有状态工具与连续操作)。

**旧工作可以主动找回。** Agent 根据目标生成检索词，必要时换词重查，从命中记录定位旧会话、消息与任务线索，回读原文和证据，再核验关联任务的当前状态继续工作。可检索的长期记录能够远超单次模型请求的上下文窗口，本轮只取需要的材料。检索覆盖本机当前数据环境中保存的会话；普通渠道入站消息发起的查询还受配对身份可访问会话的范围限制。用法与承接旧任务的示例见[跨会话长期记忆](wiki/Agent-and-Sessions.md#跨会话长期记忆与材料承接)。

**模型端点与本地 Agent 都能驱动工作。** 可以使用自己配置的 AI 端点，也可以通过 ACP 接入已在本机安装、登录的 Codex、Cursor、Claude Code 等 Agent，让它们在 ArgusAI 内工作，并经工具桥调用工作台能力。ACP 主对话无需重复配置 ArgusAI 原生端点；原生子 Agent、小队成员及其他 AI 分析任务使用各自的运行时与模型配置。

**一部手机，也能接续电脑与服务器上的工作。** 在 QQ、飞书、Telegram 等渠道完成配对后，手机、平板或另一台电脑都能成为任务入口，用于下达任务、查看进度、接收报告和文件。Agent 通过工作宿主上已配置的 SSH、MCP 或其他连接处理可达电脑与服务器上的任务，桌面控制操作运行工具的宿主桌面，结果可回传到当前配对渠道。接入方式见[扩展与通讯渠道](wiki/Integrations.md)。

需要任务完成后远程交付时，把“将摘要与报告文件发回当前配对渠道，并核验投递结果”写入交付条件。普通渠道回答可用 `channel_reply` 回复本次入站身份；主动通知使用 `channel_send_message`，文件使用 `channel_send_file`，按目标路由与工具确认策略执行。入队成功表示已接受投递，`sent` 表示发送接口成功，不代表收件人已读。定时任务的 `report_to_origin` 回报创建任务的应用内会话；发往 QQ 等渠道的目标与产物应在任务中另行约定。

## 运行界面

<table>
<tr>
<td width="50%" align="center" valign="top">
  <strong>桌面 Agent</strong><br>
  <a href="website/assets/screens/agent-light.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/agent-dark.webp">
      <img src="website/assets/screens/agent-light.webp" width="100%" alt="ArgusAI 桌面 Agent 对话">
    </picture>
  </a>
</td>
<td width="50%" align="center" valign="top">
  <strong>命令行</strong><br>
  <a href="website/assets/screens/cli-dark.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/cli-dark.webp">
      <img src="website/assets/screens/cli-light.webp" width="100%" alt="ArgusAI 命令行，左右侧栏展开">
    </picture>
  </a>
</td>
</tr>
</table>

<details>
<summary><strong>沙盒预览</strong></summary>

<p align="center">
  <a href="website/assets/screens/sandbox-light.webp">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="website/assets/screens/sandbox-dark.webp">
      <img src="website/assets/screens/sandbox-light.webp" width="100%" alt="ArgusAI 沙盒文件预览">
    </picture>
  </a>
</p>

</details>

## 一个工作区，连接分析与执行

<table>
<tr>
<td width="50%" valign="top">
<h3>Agent 对话、悬浮球与小队</h3>
<p>支持数十小时级单轮自主执行，持续组织工具调用、Skills、计划与待办，配合上下文压缩、证据归档和进展检查推进长任务。</p>
<p>持久小队可按角色、模型与工具范围分工协作。</p>
<p>通过对话菜单或悬浮球进入同一 Agent 工作区。浮球显示运行与未读状态，展开后可继续对话、查看工具步骤和处理确认；每个对话独立保存模型与自动审批状态。</p>
<a href="wiki/Agent-and-Sessions.md">Agent 入口、小队与会话设置 →</a>
</td>
<td width="50%" valign="top">
<h3>持续任务与主动发现</h3>
<p>固定间隔、Cron、签名 Webhook、文件变化四类触发；支持新建对话、任务专属对话或继续现有对话。</p>
<p>按检查点续跑，安排周期巡检，对比基线、发现变化并报告。</p>
<a href="wiki/Automation.md">调度、续跑与巡检 →</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>Web 审计与流量分析</h3>
<p>内置 Web 检查、浏览器实时观看与接管、原始请求与重放；HTTP/1.1、HTTP/2 MITM、WebSocket、断点及可选改写。</p>
<p>支持 HAR 与 Burp 日志导入，把已有材料接入分析流程。</p>
<a href="wiki/Web-Audit-and-Proxy.md">扫描、浏览器与代理 →</a>
</td>
<td width="50%" valign="top">
<h3>统一逆向工作台</h3>
<p>JS、WASM、APK、IPA、PE、ELF 和 HAR 进入 ReverseJob，关联静态初筛、解包、反编译、动态观察和证据。</p>
<p>按需连接 JADX、APKTool、Ghidra、Frida、capa、yara 等工具。</p>
<a href="wiki/Reverse-Workbench.md">样本与逆向任务 →</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<h3>证据、记忆与知识</h3>
<p>Agent 能主动检索长期与跨会话记忆，找回当前模型窗口之外的旧讨论、任务线索与分析结论，按需回读原文和证据，接续工作。</p>
<p>Case、Artifact、Evidence 与发现相互关联；知识库和大结果归档为复查提供可追溯材料。</p>
<p>扫描报告可导出为 HTML、Markdown 和 JSON，便于整理交付。</p>
<a href="wiki/Enterprise-Workflows.md">企业工作流与交付 →</a>
</td>
<td width="50%" valign="top">
<h3>工具生态与开放扩展</h3>
<p>Python Check、Agent Tool、Markdown Skill、MCP、本地 ACP Agent、QQ/飞书/Telegram 等配对通信渠道，以及桌面自动化。QQ 支持官方扫码绑定，也保留 App ID / Client Secret 手动配置。</p>
<p>渠道回复支持平台适配的流式阶段消息和 <code>channel_reply</code> 多气泡回复；表达遵循全局用户习惯，软件内对话与悬浮球同样生效。</p>
<p>编排外部扫描器，接入云、IaC、SBOM、离线流量、固件等专项工作流。</p>
<a href="wiki/Integrations.md">扩展、渠道与工具依赖 →</a>
</td>
</tr>
</table>

从完整对话或悬浮球描述目标，例如“继续当前应用审计，完善计划，在授权范围内推进分析并保存证据”。Agent 围绕当前任务组织工具执行；你可以打开相关功能页查看进度与结果，需要补充材料时从沙盒把文件拖到浮球，将引用带入对话继续工作。

内置检查、技能工作流和外部工具集成分别承担不同环节。具体功能的输入、依赖与使用步骤见 [Wiki 使用手册](wiki/Home.md)。

## 持续执行与主动发现

### 数十小时级单轮自主执行

ArgusAI 支持数十小时级的单轮自主执行。这里的“单轮”指一次用户任务内连续进行的多次模型与工具调用，不是单次 HTTP 或模型请求。运行时没有固定的模型/工具轮次上限，默认单轮紧急时限为 7 天，并保留进展检查、死循环检测、取消与截止机制。

这是运行时配置与控制流提供的能力，实际时长受任务、模型额度、工具、资源和授权限制。长调查、样本批分析和多阶段验证可以在同一轮内持续推进；需要跨轮或在下一次事件发生时继续的工作，由持久任务与调度器接续。

原生运行时保留稳定的核心控制工具，领域工具通过目录渐进发现和调用；每次模型请求前按上下文预算整理历史，大结果可保留摘要和产物引用。需要旧工作时，Agent 主动检索记忆并回读相关会话、任务与产物，再接回当前循环。下面展示记忆召回、当前轮继续、子任务完成唤醒和后台状态查询的关系。ACP 会话的推理循环由所选外部 Agent 执行。

```mermaid
sequenceDiagram
    participant U as 用户 / 会话入口
    participant A as 主 Agent 运行时
    participant M as 模型
    participant T as 工具 / 子 Agent
    participant S as 状态与产物
    U->>A: 目标、材料或续跑请求
    loop 尚有可推进工作，且运行条件允许
        A->>M: 稳定控制工具 / 预算内上下文
        M-->>A: 下一步调用、委派或等待
        alt 调用领域能力
            A->>T: 工具发现、技能读取，创建或选择工作对象
            T-->>A: 对象 ID / 当前状态 / 已有材料
            A->>T: 按状态选择下一步，经确认后操作同一对象
            T-->>A: 更新后的状态 / 结果 / 证据引用 / 后台回执
        else 主动召回旧工作
            A->>T: search_memory / search_all_memory
            T-->>A: 会话与消息定位、任务与产物线索
            A->>T: 分页回读历史，按 ID 核验任务与归档
            T-->>A: 相关原文与证据引用
        else 当前轮短等待
            A->>A: wait_for_duration，到时返回，可取消
        else 等待子 Agent
            A->>T: wait_for_agents
            T-->>A: 完成、超时或用户插话后返回状态
        else 查询后台作业
            A->>T: background_job_status(job_ref)
            T-->>A: 运行状态或已有结果，按需再等再查
        end
        A->>S: 更新 Todo / 检查点，保存所需产物
        S-->>A: 摘要、引用与下一步依据
    end
    opt 子任务结束，父会话空闲且满足继续条件
        T-->>A: agent_completion 批次通知，唤醒父会话
        A->>M: 读取结果，继续复核与汇报
    end
    A-->>U: 交付结果 / 等待人工 / 报告本轮停止原因
```

`wait_for_agents` 可因用户插话结束等待，子任务仍可继续；`wait_for_duration` 是可取消的短等待。普通后台作业通过 `job_ref` 查询，持久小队通过群聊和成员队列协作；监督这些工作也可交给下面的持续任务。

### 跨轮续跑与事件驱动

Agent 将进度保存为待办、检查点、知识和产物，调度器在后续触发时接上工作。

<div align="center">

```mermaid
flowchart TB
    Register["已配置的持续任务"] --> T
    T["间隔 / Cron<br/>签名 Webhook / 文件变化"] --> Scheduler["调度器<br/>宿主租约 / 执行锁"]
    Scheduler --> Ready{"会话可执行？"}
    Ready -->|正忙| Busy["延期检查<br/>超窗可跳过本次"]
    Busy --> Waiting["等待后续检查、时间或事件"]
    Ready -->|可执行| Destination["执行对话"]
    Destination --> Existing["session<br/>唤醒指定已有会话"]
    Destination --> Dedicated["dedicated_session<br/>首次创建，后续复用"]
    Destination --> New["new_session<br/>每次独立新会话"]
    Existing --> Context["读取对应任务上下文、Todo<br/>检查点、知识与产物引用"]
    Dedicated --> Context
    New --> Context
    Context --> Run["执行下一阶段<br/>巡检 / 分析 / 验证<br/>小队或作业状态检查"]
    Run --> Save["保存进度与产物<br/>对比基线 / 更新检查点"]
    Save --> Notify["按策略通知<br/>可选回报来源对话"]
    Notify --> Enabled{"仍启用且可运行？"}
    Enabled -->|继续| Waiting
    Waiting --> T
    Enabled -->|停用 / 次数耗尽| Stop["停止后续触发<br/>保留记录与产物"]
    Scheduler -->|已停用 / 次数耗尽| Stop
```

</div>

新会话通过任务上下文、知识和产物接续工作；指定会话与任务专属会话可以继续各自历史。一次运行结束后，计划仍可等待下一次触发；目标完成时由 Agent 按约定停用，或由用户停用、达到次数上限后停止。调度宿主需要保持运行，每次调度执行有独立截止。

| 何时触发 | 适合做什么 |
| --- | --- |
| 固定间隔 `interval` | 周期巡检、查询扫描进度、复核新增流量 |
| 日历规则 `cron` | 每日资产复查、每周暴露面审查、定时简报 |
| 签名事件 `webhook` | 对接测试构建、外部告警或其他流程事件 |
| 文件变化 `file_change` | 分析新日志、新报告或样本目录中的变化 |

触发方式与执行对话可以独立组合：

| 在哪里执行 | 上下文如何延续 |
| --- | --- |
| 每次新建对话 `new_session` | 每次运行记录独立，通过任务上下文、知识与产物传递状态 |
| 任务专属对话 `dedicated_session` | 同一任务在自己的对话中积累跨次运行记录 |
| 继续现有对话 `session` | 接上指定对话中的历史、待办和检查点 |

长任务可使用 `max_runs=0`，不按固定执行次数结束，在多轮之间读取检查点、完成下一步、再次保存。这种持久分段续跑配合上下文压缩和证据归档，可以支持长期调查、持续巡检和多阶段任务。调度器、模型和工具需要保持可用；必要信息、资源与授权边界仍决定每一轮能否继续。

<details>
<summary><strong>任务示例：自动巡检与变化通知</strong></summary>

> 每天在约定窗口复核已授权的测试服务，按知识库中保存的检查步骤执行。对照上次记录整理新增资产和配置变化，保存证据。无变化时保持安静；有新增风险、执行失败或需要人工处理时通知我。不进行改写、修复或扩大目标范围。

> 继续当前对话中尚未完成的分析。先读待办和最近检查点，不重复已完成步骤；每轮保存进度。需要我补充信息时报告阻塞原因，全部完成后停止续跑并整理结论。

</details>

查看 [自动化手册](wiki/Automation.md)，选择触发器、对话目的地、运行次数与通知策略。

## 企业中的实际工作流程

| 工作方向 | 组织输入与执行过程 | 形成的工作材料 |
| --- | --- | --- |
| 内网安全防护 | 明确授权的 IP/CIDR、主机与 SSH 范围，发现资产并分析服务暴露；在已配置 SSH 的主机上只读检查监听端口、防火墙摘要、失败登录和关键服务版本，持续对比基线 | 资产覆盖表、暴露面变化、风险优先级、加固建议与原始证据引用 |
| 应用安全防护 | 对接预发布目标、测试身份、接口材料、HAR/JS 与业务日志，联动 Web 检查、浏览器和代理复核配置、认证、访问控制与异常请求，安排整改验证 | 接口与风险清单、可复现请求、请求/响应证据、修复建议与复测记录 |
| 攻击溯源 | 在选定时间窗口内整理业务日志、主机审计、代理流量及已提供的 WAF/EDR/SIEM 告警，关联时间、账号、IP、进程和请求标识，检查调查假设 | 事件时间线、证据关系、已观察事实与推断、置信度和证据缺口；归因以实际证据为准 |
| 持续攻防 | 围绕已授权资产与约定靶场，通过长时单轮执行、周期任务和事件触发，串联暴露面发现、单独授权的受控验证、检测证据复核与整改复测 | 演练链路、检测覆盖与差距、变化通知、复盘材料，以及可复用的技能和防护检查步骤 |

告警与远程状态通过使用者提供的材料或已配置工具获取，按授权范围组织检查与验证。漏洞定性、风险接受、生产变更与事件处置由负责团队审核。查看[企业工作流](wiki/Enterprise-Workflows.md)了解任务输入、人工复核点和交付组织。

## 快速开始

当前版本为 `0.1.10`。从 [Releases](https://github.com/HG-ha/Argusai/releases) 下载对应平台的预编译包和 `.sha256`，按[安装与环境](wiki/Installation.md)校验、安装后即可启动桌面。终端示例使用 `argusai` 命令入口；未设置入口时，Windows 在安装目录中使用 `.\ArgusAI.exe`、Linux 在解压目录中使用 `./ArgusAI`，macOS 入口见[CLI 命令参考](wiki/CLI-Reference.md)。

```sh
argusai version --json
argusai --gui
argusai chat
```

**两个发行版本：** 桌面版包含 GUI 与完整命令行；Shell 版仅提供命令行，适合服务器和 SSH 环境，保留 Agent、通讯渠道、任务、代理与逆向能力。Shell 包名称带 `shell`，无需桌面 WebView 组件；浏览器等外部运行时仍按功能准备。正式包与测试预发布包见 [Releases](https://github.com/HG-ha/Argusai/releases)，使用步骤见[安装说明](wiki/Installation.md)。

首次启动后，确认数据目录，配置原生 AI 端点与模型，或选择已安装、登录并测试通过的本地 ACP Agent；按需要检查浏览器与外部工具路径。打开完整 Agent 对话或点击悬浮球，把目标、材料和约束交给 Agent，让它建立计划并开展工作。第一次可以提供一份允许分析的 HAR 或 JS，让 Agent 整理接口、标注疑点并归档证据，再到相应功能页核对结果。

| 选择入口 | 如何开始 | 继续阅读 |
| --- | --- | --- |
| 完整 Agent 对话 | 从侧栏进入或新建对话，直接描述目标并交给 Agent 执行 | [Agent 与会话](wiki/Agent-and-Sessions.md) |
| Agent 悬浮球 | 在功能页点击浮球接续当前工作；可拖动位置，查看运行和未读提示 | [浮球与工作台协作](wiki/Agent-and-Sessions.md) |
| 启动桌面 | 安装后的桌面入口，或 `argusai --gui` | [快速开始](wiki/Quick-Start.md) |
| 交互终端 | `argusai` 或 `argusai chat` | [CLI 命令参考](wiki/CLI-Reference.md) |
| 发行包 | 从 [Releases](https://github.com/HG-ha/Argusai/releases) 下载发布后对应平台的包与 `.sha256` | [安装与环境](wiki/Installation.md) |

Windows 桌面需要 WebView2 Runtime，Windows ARM64 还需要原生 Microsoft .NET Desktop Runtime；macOS 桌面自动化需要系统权限；Linux 桌面需要 GTK 与 WebKitGTK。外部模型、扫描器、逆向 CLI 和设备按功能准备，详细步骤见[安装说明](wiki/Installation.md)。

<details>
<summary><strong>源码开发、构建与发布</strong></summary>

源码运行需要 Python 3.12+、uv、Node.js 18+ 和 npm。在项目根目录准备依赖并构建前端：

```sh
uv sync --locked
npm --prefix argusai/webui ci
npm --prefix argusai/webui run build
uv run argusai --gui
```

三个平台统一使用 Python 构建入口，在目标系统和目标架构环境中运行：

```sh
uv run --locked --group build python build.py --help
uv run --locked --group build python build.py --dry-run
uv run --locked --group build python build.py --jobs 2
```

Windows 可加 `--nsis`，macOS 可加 `--dmg`，`--onefile` 生成单文件；Linux 同时生成发布压缩包。完整的构建依赖、签名、校验和与 GitHub Actions 发布流程见[构建与发布](wiki/Build-and-Release.md)。

开发规范见 [AGENTS.md](AGENTS.md)，架构与扩展入口见[架构与开发](wiki/Architecture-and-Development.md)。测试使用 `uv run pytest`，前端开发使用 `npm --prefix argusai/webui run dev`。

</details>

## 文档导航

<table>
<tr>
<td width="33%" valign="top">
<h3>开始使用</h3>
<p><a href="wiki/Quick-Start.md">快速开始</a><br><a href="wiki/GUI-Guide.md">桌面图文入门</a><br><a href="wiki/Installation.md">安装与环境</a><br><a href="wiki/Agent-and-Sessions.md">Agent、对话与小队</a><br><a href="wiki/CLI-Reference.md">CLI 完整命令示例</a><br><a href="wiki/CLI-Options.md">CLI 完整参数表</a></p>
</td>
<td width="33%" valign="top">
<h3>开展工作</h3>
<p><a href="wiki/Automation.md">定时、续跑与自动巡检</a><br><a href="wiki/Web-Audit-and-Proxy.md">Web 审计与代理</a><br><a href="wiki/Reverse-Workbench.md">逆向工作台</a><br><a href="wiki/Enterprise-Workflows.md">企业工作流</a></p>
</td>
<td width="33%" valign="top">
<h3>配置与扩展</h3>
<p><a href="wiki/Integrations.md">模型、工具与集成</a><br><a href="wiki/Data-and-Safety.md">数据与使用边界</a><br><a href="wiki/Troubleshooting.md">故障排查</a><br><a href="wiki/Build-and-Release.md">构建与发布</a></p>
</td>
</tr>
</table>

## 数据与使用边界

会话、任务、知识库、逆向产物和日志保存在配置的数据目录中。AI 请求发送到用户配置的服务，搜索、下载、通信渠道和带外验证等功能也可能访问外部服务。会话级文件与运行时隔离用于组织执行，实际访问能力仍受工具授权和宿主机权限约束。

ArgusAI 用于自有系统、明确授权的安全评估和隔离实验环境。请对目标、方法、时间与影响范围取得授权，复核模型结论，并为主动扫描、流量改写、动态注入和生产定时任务评估影响。完整说明见[数据与使用边界](wiki/Data-and-Safety.md)。

## 许可证

除另有说明的第三方内容外，ArgusAI 采用 [GNU GPL 第 3 版](LICENSE)（`GPL-3.0-only`），不自动授予后续版本许可。个人与企业均可按该许可证使用、修改和商用，无需因企业身份另购软件使用授权。

分发程序或受 GPL 约束的整合作品时，须按 GPLv3 提供对应源代码，保留版权与许可声明，并履行其他适用条件。完整条款以 [LICENSE](LICENSE) 为准。

随附第三方组件保留各自许可证，具体范围与声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。达瓦科技可提供部署、集成、定制与技术支持等商业服务；商业服务不改变 GPL 已授予的权利。

## 参与项目

欢迎提交可复现的问题、工具适配、检测规则、Skills 和界面改进。请提供版本、操作系统、复现步骤与脱敏日志，保留第三方工具和 vendor 代码的许可声明。

贡献按对应文件适用的许可证提交，项目代码适用 `GPL-3.0-only`，第三方内容遵循其原有许可证。贡献者保留自己的版权。

[提交问题](https://github.com/HG-ha/Argusai/issues) · [开发规约](AGENTS.md) · [架构与开发](wiki/Architecture-and-Development.md) · [Wiki 维护](wiki/Wiki-Maintenance.md)

<p align="center">由 <a href="https://dawahezhi.com/">达瓦科技</a> 开源 · <a href="https://github.com/HG-ha/Argusai">HG-ha/Argusai</a></p>
