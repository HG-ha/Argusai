# 扩展、外部 Agent 与通信渠道

[Wiki 首页](Home.md) · [Agent 对话](Agent-and-Sessions.md) · [自动化](Automation.md)

扩展的目的，是让 Agent 完成工作所需的工具和连接就位。ArgusAI 通过 MCP 接入工具与资源，通过 ACP 使用本地 Agent，也可以把授权对话接到通信渠道。

从悬浮球或完整对话提出实际目标，例如“检查现有工具能否完成这项任务，定位缺失的连接，给出接入和验证步骤”。Agent 可以检索已有能力、检查连接并组织后续操作；本页说明各类入口的配置与验收方式。

## 先选择扩展方式

| 需要 | 使用入口 | 主要检查点 |
| --- | --- | --- |
| 给现有 Agent 增加外部工具或资源 | **扩展**，即 MCP 服务 | 来源、执行命令或远端地址、凭据、工具清单与授权策略 |
| 使用本机已经配置的编码或分析 Agent | **本地 Agent**，即 ACP runtime | 命令是否可用、握手是否通过、工作目录与会话绑定 |
| 从聊天软件接续授权对话 | **通讯接入** | 渠道配置、私聊配对、身份与会话授权、消息路由 |
| 长期在后台运行渠道和计划 | `argusai serve` | 数据目录、宿主单实例、服务状态与凭据可用性 |
| 增加固定分析方法或检测 | Markdown Skill、Python Check、工具适配 | 契约、作用域、风险与证据输出 |

选择扩展前，可以先让 Agent 搜索已有工具和 Skills，避免为现成功能重复安装软件。

## 接入 MCP 工具

1. 在 **扩展** 面板检查已有服务。确认需要本地 stdio 进程，还是远端 HTTP/SSE 端点。
2. 核对来源与版本、启动命令、环境变量、网络地址和需要的权限。第三方服务运行的是外部代码。
3. 在界面添加或编辑服务，并按界面支持的测试、连接和工具发现操作检查状态。
4. 读取工具与资源清单，确认名称、参数、风险分级和自动允许策略符合预期。
5. 先使用一个低影响的读取任务验证输出，再纳入正式工作流。
6. 不再需要时停用或移除服务，并撤销相关凭据与访问权。

让 Agent 规划接入时，可使用：

```text
先检索现有工具和已连接 MCP 服务，判断能否完成这个任务。
若需要新服务，只整理来源、固定版本、许可、依赖、启动方式、所需权限和验证步骤。
先给出可复核方案，不下载、不安装、不启动第三方代码。
```

凭据通过设置或服务的专用配置管理，不要粘贴进可公开的来源说明、提示词或示例。远端服务能够接触哪些数据，应和模型端点一样纳入数据处理评估。

## 使用本地 ACP Agent

**本地 Agent** 面板列出已配置的运行时与可执行命令。配置被发现或显示在列表中，不代表已经成功握手。

1. 先在主机准备对应 Agent 与它需要的账号、依赖或凭据。
2. 在面板中核对可执行命令、参数、环境和启用状态。
3. 点击测试执行 ACP 握手，检查返回的能力与错误。
4. 新建对话时选择该运行时，并指定会话沙盒或授权的本地项目目录。
5. 首次发言后运行时绑定锁定。以后在该对话中切换其暴露的模型选项；需要更换运行时或项目边界时新建对话。

ACP 模型清单取决于运行时提供的配置能力，可能需要刷新协商。不能因为另一个会话支持图片，就推定当前运行时也支持；图片、文件与工作目录以本次绑定和协商结果为准。

ArgusAI 工具可通过绑定会话的回环 MCP broker 提供给外部 Agent，仍按宿主的工具与确认策略执行。项目目录的选择和外部 Agent 的权限需要一起核对。

## 配对通信渠道

当前提供商包括 QQ、Telegram、Discord、Slack、飞书、钉钉、企业微信、微信和签名 Webhook。各提供商有各自的机器人、应用或登录配置，详细字段以 **通讯接入** 面板和 `argusai channels providers` 为准。

QQ 使用腾讯官方 [`qqbot-agent-sdk`](https://github.com/tencent-connect/qqbot-agent-sdk) 建立 WebSocket 长连接，不需要公网回调地址。新建并保存 QQ 渠道后，可在详情页点击 **QQ 扫码绑定**，用手机 QQ 完成官方 Onboard；返回的 App ID 与 Client Secret 会直接写入本机加密凭据存储，不经过 Web 界面。已有机器人也可以手动填写这两个字段。命令行使用 `argusai channels login-qq CHANNEL_ID`，将 `CHANNEL_ID` 替换为实际渠道 ID，终端会显示二维码和可打开的扫码链接。

QQ 渠道只接受 C2C 私聊事件；群、频道和 guild 消息在适配器与运行时两层都会被忽略。文字按 QQ 限制分段，图片、语音、视频和文件通过官方媒体接口发送。扫码完成的是机器人凭据绑定，远端账号仍需发送一次性 `/pair` 口令，才能访问指定的 Agent 对话。

### 一次完整配对

1. 在 **通讯接入** 新建渠道，填写提供商需要的设置。密钥在专用凭据输入中配置。
2. 测试或启动渠道，检查在线状态和错误。
3. 在本机创建一次性配对码，并决定该身份可访问的会话。
4. 在与机器人的一对一私聊中提交配对码。群聊、频道和房间消息不会作为配对入口。
5. 在本机确认配对结果、身份与聊天标识，再从远端发送一个低影响的测试问题。
6. 核对回复落在正确身份与会话中。需要回传报告时，检查实际文件投递状态。

每个配对只应看到授予它的会话。把同一机器人配置给多人时，也不能把他们当成共享全部历史的同一个用户。事件重试有去重与限流，消息未回复时先看运行状态和投递记录。

远端普通确认可按渠道能力转发；敏感操作和授权策略修改可能要求回到本机处理。不要假设聊天软件中的任意一句“同意”都能替代本机确认。

### 用 CLI 接通 QQ 并绑定一个工作对话

以下命令使用发行包中的 `argusai`，不需要检出源码。渠道接入和模型配置是两件事：QQ 负责收发消息，绑定的 Agent 对话负责选模型、执行任务和保存产物。配置命令、桌面程序和常驻宿主要使用同一系统用户及数据目录。

1. 准备可用的模型端点。已有配置可先执行 `argusai ai list`；新配置可以使用下面的命令。环境变量应由你的终端或凭据管理器预先提供，示例不包含真实密钥。

   ```sh
   argusai ai add --name review --base-url "$ARGUSAI_AI_BASE_URL" --api-key "$ARGUSAI_AI_KEY" --model "$ARGUSAI_AI_MODEL" --protocol openai --activate
   ```

   这里的变量是 shell 展开，不是 ArgusAI 自动读取的配置项。PowerShell 中对应写为 `$env:ARGUSAI_AI_BASE_URL`、`$env:ARGUSAI_AI_KEY`、`$env:ARGUSAI_AI_MODEL`。API Key 会作为进程参数传入；不要把填入真实值的命令、终端录像或进程诊断公开。

2. 建立一个命名对话，再在对话中选择它使用的模型：

   ```sh
   argusai chat --session audit-delivery
   ```

   以下内容输入 Agent 对话，不是另开终端执行的命令：

   ```text
   /model
   /model review
   /quit
   ```

   按交互提示确认模型选择。`/model` 只修改该对话；`argusai ai activate review` 调整的是全局默认端点优先级。之后 QQ 绑定 `audit-delivery`，便能接着同一份历史工作。

3. 创建 QQ 渠道，完成官方扫码，再启用和检查连接：

   ```sh
   argusai channels providers
   argusai channels add qq --id qq-work --name "我的 QQ 工作入口"
   argusai channels login-qq qq-work --timeout 300
   argusai channels enable qq-work
   argusai channels test qq-work
   ```

   扫码应由账号本人在手机 QQ 完成。已有官方机器人可改用 `channels add qq --secret app_id=APP_ID --secret client_secret=CLIENT_SECRET`；这两个值是占位符。不要把机器人凭据发给 Agent 或写进任务提示词。

4. 生成绑定到工作对话的一次性配对码，然后让宿主保持运行：

   ```sh
   argusai channels pair qq-work --session audit-delivery
   argusai serve run
   ```

   `serve run` 占用当前终端；后续管理命令在另一终端执行。如果桌面程序已经托管同一数据目录，继续使用该宿主，避免再启动一个竞争连接的进程。`argusai channels serve` 也能前台托管渠道并启动调度器；同时需要渠道和计划时统一使用 `serve run`。

5. 在手机 QQ 与机器人的私聊中发送输出的配对码，再检查会话：

   ```text
   /pair 实际配对码
   /status
   /model
   /live on
   ```

   未指定 `--session` 的配对，可以在私聊中发送 `/new` 创建原生 Agent 对话；`/agents` 列出可用本地 Agent，`/new 编号或名称` 可建立相应 ACP 对话。远端创建的 ACP 对话使用会话沙盒，不会自动进入本机项目目录。

6. 在本机核对身份和授权。`--json` 可以取得完整的身份 ID；下面的 `IDENTITY_ID` 必须替换为实际值。

   ```sh
   argusai channels identities qq-work --json
   argusai channels bind IDENTITY_ID audit-delivery
   argusai channels grant IDENTITY_ID chat status cancel confirm configure send
   ```

   已经由第 4 步绑定时无需重复 `bind`。`grant` 设置的是完整权限集合，会替换原集合；可以只保留任务需要的权限。远端切换模型或会话需要 `configure`，主动消息和文件投递需要 `send`。新配对默认拥有上述六项权限，应按使用者实际职责检查。

其他提供商使用相同的配对、绑定和身份权限流程，创建字段以 `channels providers` 为准。例如，已准备 Telegram Bot Token 后可执行：

```sh
argusai channels add telegram --name "Telegram 工作入口" --secret "bot_token=$ARGUSAI_TELEGRAM_BOT_TOKEN" --enable
```

返回的渠道 ID 用于后续 `pair`，不能用显示名称代替。微信 iLink 使用 `argusai channels login-wechat 渠道ID` 完成自己的扫码流程。

### 任务完成后回传结果和文件

在桌面、CLI 或已配对私聊里，都可以把“完成后发送给谁、发送什么”写进任务。先核对目标身份，再让 Agent 执行。例如，在 `audit-delivery` 对话中输入：

```text
基于本对话已经授权的材料完成分析，把报告保存为 reports/review.md。
先用 channel_list_targets 核对收件人，文件只发给 identity_id=IDENTITY_ID。
完成并复核报告后，用 channel_send_file 发送 reports/review.md，附上简短说明。
记录返回的 delivery_id，用 channel_delivery_status 检查该投递。
如果仍在排队，如实说明排队状态，后续查询同一 delivery_id，不重复创建投递。
如果失败，保留本地报告，报告 last_error，不把文件名或已排队说成送达成功。
```

`IDENTITY_ID` 从前面的身份清单取得。当前 CLI 没有独立的 `channels send`、`send-file` 或 `delivery` 子命令；这里通过 Agent 工具发送和查询，工具名也不是 shell 命令。

| Agent 工具 | 用途与边界 |
| --- | --- |
| `channel_list_targets` | 查看本对话的配对收件人、权限及可能的自动路由；多个收件人时先确认身份 |
| `channel_send_message(text, identity_id)` | 主动发送摘要或完成通知；省略身份可能向同会话多个渠道投递 |
| `channel_send_file(path, identity_id, caption, filename)` | 发送当前执行对话沙盒中的一个已有文件，保留文件大小与 SHA-256；需要发送授权 |
| `channel_delivery_status(delivery_id)` | 查询当前发送对话的投递记录、尝试次数、分段进度和错误 |
| `channel_reply(messages)` | 仅用于渠道发起的当前轮次，固定回复来源身份；接受后结束该轮 Agent 执行 |

因此，同一轮既要发文件又要回复时，应先生成文件、提交文件投递和记录回执，最后再调用 `channel_reply`。桌面或 Cron 发起的主动交付使用 `channel_send_message`，不使用仅限渠道当前轮次的 `channel_reply`。后台扫描返回 `job_ref` 时，先取得最终结果并核对实际产物，再发送报告。

文件路径相对于当前执行对话的沙盒，不能借绝对路径发送其他会话或宿主任意文件。发送已有扫描报告、ReverseJob 产物时，先通过相应读取或导出工具把待交付文件放入当前沙盒。单文件上限为 32 MiB，平台还可能有更低的限制；超限时先生成精简报告或拆分产物。

入队回执中的 `queued=true` 表示 accepted；持久投递记录使用以下状态，不另有 `delivered` 状态：

| `state` | 如何理解 |
| --- | --- |
| `pending` | 已排队，或等待下一次重试 |
| `processing` | 宿主正在向平台投递 |
| `sent` | 各部分已通过平台适配器发送；不代表收件人已经阅读或下载 |
| `failed` | 已停止投递，查看 `last_error`、`attempts` 和 `delivered_parts`，部分内容可能已经发出 |

宿主需要保持在线，暂时性错误会按投递队列规则重试。平台权限不足、身份撤销或绑定变化可能导致失败；QQ 等平台对主动消息窗口、媒体上传和卡片另有规则，ArgusAI 的 `send` 权限不能代替平台权限。文件仍是本机产物，投递前后不要立即删除或覆盖它。

用户也可以在已绑定私聊里自行取回文件：

```text
/file list
/file search review
/file reports/review.md
```

这些是聊天软件中的斜杠命令。跨轮自动交付见[定时任务与渠道回传示例](Automation.md#用-cli-安排检查并回传产物)。

### 渠道交互和文件

渠道可展示阶段消息与可投影的决策卡，最终文本不一定包含全部中间细节。收到“执行完成”时，应查看关联的报告与运行记录，而不只依赖一条摘要。

QQ、飞书合并转发中的长记录会作为文本附件保存到会话沙盒，Agent 可检索或分段读取。图片、音视频和文件保存在媒体 ZIP 中，清单 `manifest.json` 记录文件名、类型、大小与 SHA-256；Agent 可用 Python 工具按需提取，再通过图片或文件工具读取。文件留存在本机会话中，后续查看不依赖平台下载链接继续有效。

单次转发的文本附件上限为 4 MiB；媒体最多尝试下载 64 个文件，总内容约 32 MiB，另受单文件大小、平台权限和下载时限约束。超过限额或下载失败会标明记录不完整，不能将已读取部分视为全部材料。

Discord 可读取事件自带的转发快照，Slack 可读取事件内携带的共享正文和文件；仅有分享链接时，不代表已经获取了原消息。Telegram 普通转发沿用单条消息接收，相册中的各条消息分别进入会话。飞书普通富文本、钉钉、微信等渠道的多文件输入超过 8 个时也会打包留存；普通图片仍可直接查看，文件可从会话附件中读取。Webhook 按标准附件协议接收，单次超过 8 个附件或校验失败会返回 HTTP 400，发送端应拆分请求。

在配对的私聊中发送 `/live on`，开启当前绑定对话的过程输出；`/live off` 关闭。桌面宿主和 `argusai serve run` 使用相同的发送路径。过程文字按渠道能力展示：

| 渠道 | 开启 `/live` 后的文字展示 | 配置要求 |
| --- | --- | --- |
| QQ | C2C 原生 Markdown 流，结束时发送完成帧 | 机器人需有对应消息权限；权限不足时使用普通消息 |
| Telegram | `sendMessageDraft` 临时预览，结束后用正式消息保存完整内容 | 支持草稿流的 Bot API，私聊；预览只展示当前长度窗口，完整内容在结束时发送 |
| Slack | `chat.startStream`、`chat.appendStream`、`chat.stopStream` | `chat:write`；在当前消息的话题中回复，达到单流时限或长度上限时接续新流 |
| 飞书 | CardKit 2.0 卡片正文增量更新，结束时关闭流式模式 | 应用需有消息发送和 CardKit 创建、更新权限；无需额外安装新版 SDK |
| 钉钉 | AI 卡片全量更新正文，结束时设置 `isFinalize` | 在渠道编辑窗口填写已发布的流式 AI 卡片模板 ID、正文变量名，并开启卡片创建、投放与更新权限 |
| Discord | 反复编辑机器人发出的同一条消息 | 机器人可发送、编辑自己的消息；超过消息长度上限时接续下一条 |
| 企业微信应用消息、微信 iLink | 阶段消息与最终消息 | 当前接入方式不提供通用文字流接口；阶段消息保留正常投递机制 |
| 通用 Webhook | 默认发送阶段消息；可选择 ArgusAI stream v1 实现原位更新 | 接收方需实现下述流式协议后，再在渠道编辑窗口启用 |

流式更新会合并短时间内的变化，结束时强制提交最后一段。接口返回错误、权限不足或流式收尾失败时，完整文字仍通过普通投递路径发送；已经出现的部分预览可能保留。对话中的确认卡和文件继续使用各渠道现有的卡片、附件接口。

钉钉的正文变量名默认是 `content`，应与自己的模板一致。CLI 创建渠道时也可传入 `--setting card_template_id=<模板ID> --setting card_content_key=content`。未填写模板 ID 时使用阶段消息。这里的流式 AI 卡片与用于接收事件的 DingTalk Stream 长连接分别配置。

通用 Webhook 在界面选择 **ArgusAI stream v1**，或配置 `--setting stream_protocol=v1`。发送地址接收 JSON POST：

```json
{
  "type": "stream_delta",
  "protocol": "argusai.stream.v1",
  "stream_id": "同一回复中保持不变的标识",
  "sequence": 2,
  "chat_id": "目标聊天标识",
  "thread_id": "目标话题标识",
  "reply_to_message_id": "原始消息标识",
  "text": "截至本次更新的完整文字",
  "mode": "replace",
  "final": false
}
```

事件依次为 `stream_start`、零次或多次 `stream_delta`、`stream_end`。接收方按聊天、话题和 `stream_id` 定位显示位置，按递增的 `sequence` 忽略旧帧，并用 `text` 替换正文。一次完成的回复可能直接收到 `stream_end`；此时创建并保存该条消息。结束事件的 `final` 为 `true`。接收成功返回 HTTP 2xx；流式接口返回错误时，发送端会使用原有普通消息协议补发完整答案，因此接收方应同时兼容普通消息。

平台协议依据：[Telegram 草稿流](https://core.telegram.org/bots/api#sendmessagedraft)、[Slack 流式消息](https://docs.slack.dev/messaging/sending-and-scheduling-messages/#text-streaming-in-messages)、[飞书流式更新文本](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/cardkit-v1/card-element/content)、[钉钉官方卡片 SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python/blob/main/dingtalk_stream/card_replier.py)、[Discord 编辑消息](https://docs.discord.com/developers/resources/message#edit-message)。

渠道发来的普通问题由 Agent 使用 `channel_reply` 回复。一次工具调用可按顺序发送多句话，形成少量独立气泡，并在气泡之间保留短暂停顿；目标固定为发起当前轮次的那个配对身份，不会广播到同会话的其他渠道。各渠道仍按自身能力渲染 Markdown、卡片、分段和附件。回复被投递队列接受后，Agent 本轮立即结束，不会再调用模型生成发送回执或重复总结。

气泡边界由 Agent 决定：`channel_reply` 的 `messages` 每项就是一个完整气泡，工具不会按标点拆句。工具参数完整生成后，文字进入可靠投递队列；开启过程输出时，各气泡可通过平台流式接口逐步显示。这是已完成文字的流式投递，不是工具参数生成时的逐 token 推送。

表达方式先读取设置中的全局 **用户习惯**。说话风格、输出偏好、避免事项和备注同时作用于完整 Agent 对话、悬浮球和通讯渠道；渠道拟人回复 Skill 只负责把同一份习惯落实为自然措辞和合理气泡边界，不会另建一套渠道人格，也不会改写技术事实。思考开关、思考强度、模型选择和自动审批仍是当前对话的会话级状态，一个对话中的切换不会改变其他对话。

文件回传使用本机已有的会话产物和渠道投递机制。发送前核对目标身份、文件内容与脱敏要求；“已排队”不等于“已成功送达”。

## 不开桌面窗口继续托管

```sh
argusai serve run
```

该前台宿主同时托管已启用渠道和计划。只运行计划可使用 `--cron-only`。查看状态与系统服务计划：

```sh
argusai serve status
argusai serve install --dry-run
```

先检查安装计划，再决定注册系统服务。源码仓库路径、虚拟环境或解释器变化可能使旧服务失效。GUI 与守护宿主不要竞争同一数据根目录的后台服务。

企业微信、通用 Webhook 等回调默认使用回环监听。若需要让外部平台访问，必须设计可达的地址与认证 HTTPS 反向代理，并核对平台回调校验；仅修改监听为所有网卡不足以完成接入。

## 常见检查顺序

- 服务无法启动：先读解析后的可执行路径和错误，再核对环境变量与依赖。
- 能连接但工具不可见：检查工具发现、允许列表和服务返回的 schema。
- 外部 Agent 没响应：先测试 ACP 握手，再检查所选运行时、项目目录和对话绑定。
- 远端没有回复：确认私聊身份已配对、会话已授权、适配器在线、调度宿主没有冲突。
- 确认卡停住：查看是否要求本机处理，不要通过重复远端消息绕过。

## 本页源码依据

- [MCP 接入工作流](../argusai/skills/mcp-provision-workflow/SKILL.md)
- [本地 Agent 桥接](../argusai/gui/agent_runtimes.py)
- [通信渠道模型](../argusai/core/channels/models.py)
- [渠道 CLI](../argusai/cli/commands/channels.py)
- [渠道发送与投递状态工具](../argusai/core/tools/channel_tools.py)
- [渠道交互工作流](../argusai/skills/channel-interaction/SKILL.md)
- [渠道拟人回复工作流](../argusai/skills/channel-human-reply/SKILL.md)
- [无界面宿主 CLI](../argusai/cli/commands/serve.py)

继续阅读：[CLI 参考](CLI-Reference.md) · [数据与安全](Data-and-Safety.md) · [故障排查](Troubleshooting.md)
