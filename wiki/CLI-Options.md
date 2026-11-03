# 命令行参数参考

[文档首页](Home.md) · [命令手册与完整示例](CLI-Reference.md) · [快速开始](Quick-Start.md)

本页从当前版本的实际命令声明生成，逐项列出参数、类型、必填状态、默认值和中文说明。从配置 AI 到渠道、任务与产物投递的可复制示例见[命令手册](CLI-Reference.md)。

覆盖 **95 个末端命令**、**13 个命令分组或全局入口**。`proxy` 分组自身还可前台运行；交互控制台的斜杠指令见命令手册。

参数的“必填”表示解析层要求；服务账号、模型、路径和平台等条件仍由命令执行时检查。默认值不求值，也不读取用户配置；运行时默认与全局设置可影响实际行为。命令、参数、状态值和产品名称保留实际写法，说明统一使用中文。`--help` 本身不在每张表重复。

## 命令导航

- [argusai](#argusai)
- [argusai agent](#argusai-agent)
- [argusai ai](#argusai-ai)
- [argusai channels](#argusai-channels)
- [argusai cron](#argusai-cron)
- [argusai kb](#argusai-kb)
- [argusai mcp](#argusai-mcp)
- [argusai proxy](#argusai-proxy)
- [argusai proxy break](#argusai-proxy-break)
- [argusai reverse](#argusai-reverse)
- [argusai serve](#argusai-serve)
- [argusai team](#argusai-team)
- [argusai todos](#argusai-todos)

## argusai

ArgusAI 安全工作台 · 本地运行 · 可脚本化 · 默认安全。

不带子命令启动：在交互式终端中进入 ops 控制台，否则打开桌面 GUI（可用 --gui / --chat 或 ARGUSAI_HOST=gui|cli 覆盖）。

```text
用法：argusai [选项] 子命令 [参数]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 在支持的命令中优先使用 JSON 输出。 |
| `--verbose` / `-v` | 开关 | 否 | false | — | 将日志同时输出到 stderr（CLI 子命令默认不输出）。 |
| `--gui` | 开关 | 否 | false | — | 强制启动桌面 GUI（覆盖 TTY 自动检测结果）。 |
| `--tray` | 开关 | 否 | false | — | 启动桌面 GUI，并隐藏到系统托盘。 |
| `--chat` | 开关 | 否 | false | — | 强制进入 ops 控制台 / Agent REPL，与交互式终端的默认行为相同。 |
| `--install-completion` | 开关 | 否 | — | — | 为当前命令行环境安装命令补全。 |
| `--show-completion` | 开关 | 否 | — | — | 显示当前命令行环境的补全脚本，便于复制或自定义安装。 |

## argusai agent

管理本地 Agent 运行时（内置 ACP 预设与自定义运行时）。

```text
用法：argusai agent [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai agent add

在 settings.json 中添加或更新自定义 ACP Agent 运行时。

```text
用法：argusai agent add [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | 文本 | 是 | — | — | 显示名称。 |
| `--command` / `-c` | 文本 | 是 | — | — | 可执行程序（通过 PATH 查找或使用绝对路径）。 |
| `--arg` / `-a` | 文本 | 否 | — | 可重复 | 命令参数（可重复），例如 -a acp。 |
| `--id` | 文本 | 否 | — | — | 固定的运行时 ID。 |
| `--env` / `-e` | 文本 | 否 | — | 可重复 | 环境变量，格式为 KEY=VALUE（可重复指定）。 |
| `--cwd` | 文本 | 否 | 空字符串 | — | 工作目录。 |
| `--notes` | 文本 | 否 | 空字符串 | — | 可选备注。 |
| `--enable` / `--disable` | 开关 | 否 | true | — | 是否启用。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent disable

禁用运行时，但不删除。

```text
用法：argusai agent disable [选项] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | 文本 | 是 | — | — | 运行时 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent enable

启用运行时，也可启用内置预设。

```text
用法：argusai agent enable [选项] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | 文本 | 是 | — | — | 运行时 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent list

列出已配置的本地 Agent 及其 PATH 检测状态。

```text
用法：argusai agent list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai agent remove

删除自定义运行时。内置预设只能停用。

```text
用法：argusai agent remove [选项] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | 文本 | 是 | — | — | 自定义运行时 ID（不能是内置运行时）。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 跳过确认。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent test

执行 ACP initialize 握手，或检查原生运行时能力。

```text
用法：argusai agent test [选项] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | 文本 | 是 | — | — | 运行时 ID。 |
| `--json` | 开关 | 否 | false | — | — |

## argusai ai

管理聊天 AI 端点，配置保存在 settings.json 中。

```text
用法：argusai ai [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai ai activate

提高端点优先级，使其成为默认首选端点。

```text
用法：argusai ai activate [选项] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | 文本 | 是 | — | — | 端点索引或名称。 |
| `--model` / `-m` | 文本 | 否 | — | — | 同时切换当前使用的模型。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai add

在 settings.json 中添加或更新聊天 AI 端点。

```text
用法：argusai ai add [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | 文本 | 否 | — | — | 显示名称。 |
| `--provider` / `-p` | 文本 | 否 | — | — | 预设 ID、名称或别名（参见 argusai ai providers）。 |
| `--base-url` / `-u` | 文本 | 否 | — | — | API 基础 URL。 |
| `--api-key` / `-k` | 文本 | 否 | — | — | API 密钥或令牌。 |
| `--model` / `-m` | 文本 | 否 | — | — | 当前使用的模型 ID。 |
| `--protocol` | 文本 | 否 | — | — | openai 或 anthropic（默认采用提供方的设置）。 |
| `--priority` | 整数 | 否 | — | — | 故障转移优先级，数值越高越优先。 |
| `--max-tokens` | 整数 | 否 | — | — | 最大输出词元数。 |
| `--context-window` | 整数 | 否 | — | — | 上下文窗口的词元数。 |
| `--index` | 整数 | 否 | — | — | 按索引更新已有端点。 |
| `--extra-body` | 文本 | 否 | — | — | 合并到 extra_body 的 JSON 对象，例如 '{"explicit_cache":true}'。 |
| `--header` / `-H` | 文本 | 否 | — | 可重复 | 附加请求头 KEY=VALUE（可重复）。 |
| `--enable` / `--disable` | 开关 | 否 | true | — | 是否启用。 |
| `--activate` | 开关 | 否 | false | — | 保存后提高该端点的优先级，使其成为当前使用的端点。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai ai disable

禁用端点，但不删除。

```text
用法：argusai ai disable [选项] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | 文本 | 是 | — | — | 端点索引或名称。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai enable

启用端点，保留已有端点配置。

```text
用法：argusai ai enable [选项] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | 文本 | 是 | — | — | 端点索引或名称。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai list

列出已配置的聊天 AI 端点。

```text
用法：argusai ai list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai ai providers

列出可用于 ``ai add --provider`` 的内置提供商预设。

```text
用法：argusai ai providers [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai ai remove

从 settings.json 中删除 AI 端点。

```text
用法：argusai ai remove [选项] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | 文本 | 是 | — | — | 端点索引或名称。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 跳过确认。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai test

向一个端点发送简短的 ping 提示词。

```text
用法：argusai ai test [选项] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | 文本 | 是 | — | — | 端点索引或名称。 |
| `--json` | 开关 | 否 | false | — | — |

## argusai channels

管理并运行通讯渠道，如 Telegram、Feishu 等。

```text
用法：argusai channels [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai channels add

按名称创建渠道；未指定 --id 时自动生成内部 ID。

```text
用法：argusai channels add [选项] {provider}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `provider` | 文本 | 是 | — | — | 提供方 ID（telegram、feishu 等）。 |
| `--name` / `-n` | 文本 | 否 | — | — | 界面中显示的名称，默认为提供商名称。 |
| `--id` | 文本 | 否 | — | — | 可选的内部 ID。省略时自动生成（建议采用）。 |
| `--secret` / `-s` | 文本 | 否 | — | 可重复 | 凭据，格式为 KEY=VALUE（可重复指定）。 |
| `--setting` | 文本 | 否 | — | 可重复 | 设置项 KEY=VALUE（可重复），例如 proxy=…。 |
| `--host` | 文本 | 否 | — | — | 监听地址（wecom/webhook）。 |
| `--port` | 整数 | 否 | — | — | 监听端口（wecom/webhook）。 |
| `--path` | 文本 | 否 | — | — | 回调路径（wecom/webhook）。 |
| `--enable` | 开关 | 否 | false | — | 保存后立即启用。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai channels bind

将已配对身份绑定到助手会话。

```text
用法：argusai channels bind [选项] {identity_id} {session_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `identity_id` | 文本 | 是 | — | — | 身份 ID。 |
| `session_id` | 文本 | 是 | — | — | 助手会话 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels disable

禁用通讯渠道。

```text
用法：argusai channels disable [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 渠道 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels enable

启用通讯渠道。

```text
用法：argusai channels enable [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 渠道 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels grant

为已配对的渠道身份设置权限。

```text
用法：argusai channels grant [选项] {identity_id} [permissions]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `identity_id` | 文本 | 是 | — | — | 身份 ID，或能唯一匹配该身份的前缀。 |
| `permissions` | 文本 | 否 | — | 可变数量 | 权限名称：chat status cancel confirm configure send。 |
| `--all` | 开关 | 否 | false | — | 授予该身份全部权限。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels identities

列出已配对身份及其绑定的会话。

```text
用法：argusai channels identities [选项] [channel_id]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 否 | 空字符串 | — | 可选的渠道 ID 筛选条件。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels list

列出已配置的通讯渠道。

```text
用法：argusai channels list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai channels login-qq

通过官方扫码绑定流程连接 QQ 机器人。

```text
用法：argusai channels login-qq [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 已有 QQ 渠道的 ID。 |
| `--timeout` | 数值 | 否 | 300.0 | — | 等待扫码的时间，单位为秒。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels login-wechat

为渠道完成微信 iLink 扫码登录。

```text
用法：argusai channels login-wechat [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 已有 wechat 渠道的 ID。 |
| `--timeout` | 数值 | 否 | 180.0 | — | 等待扫码的时间，单位为秒。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels pair

创建配对码，供远程用户绑定此渠道。

```text
用法：argusai channels pair [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 渠道 ID。 |
| `--session` | 文本 | 否 | 空字符串 | — | 配对后要绑定的会话（可选，例如 cli-default）。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels providers

列出支持的渠道提供商及其所需凭据。

```text
用法：argusai channels providers [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai channels remove

删除渠道及其加密保存的凭据。

```text
用法：argusai channels remove [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 渠道 ID。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 跳过确认。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels serve

在前台运行渠道适配器（建议使用 ``argusai serve``）。

```text
用法：argusai channels serve [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 以 JSON 输出启动状态，然后继续运行。 |

### argusai channels test

探测一个渠道适配器。

```text
用法：argusai channels test [选项] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | 文本 | 是 | — | — | 渠道 ID。 |
| `--json` | 开关 | 否 | false | — | — |

### argusai chat

进入交互式 Agent 对话，即支持多轮对话的 ops 控制台。

```text
用法：argusai chat [选项] [message]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `message` | 文本 | 否 | — | 可变数量 | 对话的首条消息。各个词会拼接，因此 `chat /team use <id>` 会进入对应小队。 |
| `--session` / `-s` | 文本 | 否 | cli-default | — | 助手会话 ID（多轮对话复用历史记录）。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 自动批准符合条件的非高风险工具确认；高风险工具仍会在交互终端中询问。 |
| `--once` | 开关 | 否 | false | — | 单轮模式：必须提供消息或文件，运行一轮后退出。 |
| `--timeout` | 数值 | 否 | 600.0 | — | 每轮最长运行时间，单位为秒。 |
| `--json` | 开关 | 否 | false | — | 输出单次运行的 JSON 摘要，同时启用 --once。 |
| `--file` / `-f` | file | 否 | — | 可重复 | 为下一轮 Agent 对话附加本地文件。可重复传入以附加多个文件。 |

## argusai cron

管理助手任务（按时间、签名 webhook 或文件变化触发）。

```text
用法：argusai cron [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai cron add

创建或更新助手定时任务。

```text
用法：argusai cron add [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | 文本 | 是 | — | — | 任务显示名称。 |
| `--prompt` / `-p` | 文本 | 是 | — | — | 发送给助手的消息。 |
| `--every` | 整数 | 否 | — | — | 执行间隔，单位为秒；与 --cron 互斥。 |
| `--cron` | 文本 | 否 | — | — | 5 字段 cron 表达式，例如 '0 9 * * *'。 |
| `--webhook` | 开关 | 否 | false | — | 由内置的签名 webhook 触发。 |
| `--watch-path` | 文本 | 否 | 空字符串 | — | 在此文件或目录发生变化时触发。 |
| `--glob` | 文本 | 否 | * | — | 与 --watch-path 配合使用的文件 glob 匹配模式。 |
| `--recursive` / `--no-recursive` | 开关 | 否 | true | — | 递归监视目录下的内容。 |
| `--poll` | 整数 | 否 | 2 | — | 文件监听的轮询间隔，单位为秒。 |
| `--debounce` | 整数 | 否 | 2 | — | webhook 或文件触发的防抖时间，单位为秒。 |
| `--id` | 文本 | 否 | — | — | 固定的任务 ID。 |
| `--destination` | 文本 | 否 | new_session | — | 执行目的地：新会话（new_session）、任务专属会话（dedicated_session）或固定会话（session）。 |
| `--session` | 文本 | 否 | 空字符串 | — | 使用 --destination=session 时的目标会话 ID。 |
| `--tag` / `-t` | 文本 | 否 | — | 可重复 | 标签（可重复）。 |
| `--enable` / `--disable` | 开关 | 否 | true | — | 是否启用。 |
| `--approve-high-risk` | 开关 | 否 | false | — | 明确授权此任务在无人值守时执行高风险工具。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron authorize

授予或撤销该任务范围内的无人值守高风险执行授权。

```text
用法：argusai cron authorize [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 定时任务 ID。 |
| `--revoke` | 开关 | 否 | false | — | 撤销高风险操作授权。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 确认授权变更。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron disable

停用定时任务。

```text
用法：argusai cron disable [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 定时任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron enable

启用定时任务。

```text
用法：argusai cron enable [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 定时任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron list

列出助手定时任务。

```text
用法：argusai cron list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron remove

删除定时任务。

```text
用法：argusai cron remove [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 定时任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron runs

显示任务的近期运行记录。

```text
用法：argusai cron runs [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 定时任务 ID。 |
| `--limit` / `-n` | 整数 | 否 | 20 | — | 返回的历史记录条数。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai cron serve

在前台运行定时调度器（建议使用 ``argusai serve run --cron-only``）。

```text
用法：argusai cron serve [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 以 JSON 输出启动状态，然后继续运行。 |

### argusai cron webhook-secret

轮换 webhook 签名密钥，并仅显示一次。

```text
用法：argusai cron webhook-secret [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 由 Webhook 触发的任务 ID。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 确认轮换密钥。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai doctor

探测 shell、受管理运行时、浏览器和渠道配置。

```text
用法：argusai doctor [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai kb

在终端中浏览和管理本地知识库。

```text
用法：argusai kb [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai kb add

创建知识条目。

```text
用法：argusai kb add [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--title` / `-T` | 文本 | 是 | — | — | 条目标题。 |
| `--body` / `-b` | 文本 | 否 | 空字符串 | — | Markdown 或纯文本正文。 |
| `--file` / `-f` | 路径 | 否 | — | — | 从 UTF-8 文件读取正文。 |
| `--tag` / `-t` | 文本 | 否 | — | 可重复 | 标签（可重复）。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb delete

将条目移入回收站（软删除）。

```text
用法：argusai kb delete [选项] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | 文本 | 是 | — | — | 知识条目 ID。 |
| `--reason` | 文本 | 否 | cli kb delete | — | 移入回收站的原因。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb list

列出知识条目（最新的在前）。

```text
用法：argusai kb list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--trash` | 开关 | 否 | false | — | 包含回收站中的条目。 |
| `--superseded` | 开关 | 否 | false | — | 包含已被替代的条目。 |
| `--limit` / `-n` | 整数 | 否 | 50 | — | 最多显示行数。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb purge

永久删除回收站中的知识条目。

```text
用法：argusai kb purge [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--id` | 文本 | 否 | — | 可重复 | 永久删除回收站中指定 ID 的条目（可重复）。 |
| `--older-days` | 整数 | 否 | — | — | 永久删除在回收站中保留超过 N 天的条目（使用 --all 时省略）。 |
| `--all` | 开关 | 否 | false | — | 永久删除回收站中的全部内容。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 跳过确认。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb restore

从回收站恢复知识条目。

```text
用法：argusai kb restore [选项] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | 文本 | 是 | — | — | 知识条目 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb search

搜索知识索引，在终端中浏览结果。

```text
用法：argusai kb search [选项] [query]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `query` | 文本 | 否 | 空字符串 | — | 搜索关键词，匹配标题、标签和触发词。 |
| `--tag` / `-t` | 文本 | 否 | — | 可重复 | 按标签筛选。 |
| `--limit` / `-n` | 整数 | 否 | 20 | — | 最多匹配数量。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb show

在终端中显示单个条目及其 Markdown 正文。

```text
用法：argusai kb show [选项] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | 文本 | 是 | — | — | 知识条目 ID。 |
| `--max-chars` | 整数 | 否 | 20000 | — | 截断终端显示的正文（0 表示完整显示）。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai kb trash

列出回收站中的知识条目。

```text
用法：argusai kb trash [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai mcp

管理 MCP 服务（stdio / HTTP / SSE）。

```text
用法：argusai mcp [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai mcp add

在 settings.json 中添加或更新 MCP 服务。

```text
用法：argusai mcp add [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | 文本 | 是 | — | — | 显示名称。 |
| `--command` / `-c` | 文本 | 否 | 空字符串 | — | stdio 可执行程序。 |
| `--arg` / `-a` | 文本 | 否 | — | 可重复 | stdio 参数（可重复指定）。 |
| `--url` | 文本 | 否 | 空字符串 | — | HTTP/SSE 端点 URL。 |
| `--transport` | 文本 | 否 | 空字符串 | — | stdio \| streamable-http \| sse（指定 --command 时默认为 stdio，否则为 sse）。 |
| `--id` | 文本 | 否 | — | — | 固定的服务 ID。 |
| `--env` / `-e` | 文本 | 否 | — | 可重复 | 环境变量，格式为 KEY=VALUE（可重复指定）。 |
| `--header` / `-H` | 文本 | 否 | — | 可重复 | HTTP 请求头 KEY=VALUE（可重复）。 |
| `--cwd` | 文本 | 否 | 空字符串 | — | 工作目录（stdio）。 |
| `--notes` | 文本 | 否 | 空字符串 | — | 可选备注。 |
| `--enable` / `--disable` | 开关 | 否 | true | — | 是否启用。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp disable

停用 MCP 服务，保留其配置。

```text
用法：argusai mcp disable [选项] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | 文本 | 是 | — | — | MCP 服务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp enable

启用 MCP 服务。

```text
用法：argusai mcp enable [选项] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | 文本 | 是 | — | — | MCP 服务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp list

列出已配置的 MCP 服务及其最近一次探测状态。

```text
用法：argusai mcp list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp probe

在后台探测所有已启用的 MCP 服务。

```text
用法：argusai mcp probe [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp remove

从设置中删除 MCP 服务。

```text
用法：argusai mcp remove [选项] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | 文本 | 是 | — | — | MCP 服务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai mcp test

对一个 MCP 服务执行握手或健康检查（阻塞等待结果）。

```text
用法：argusai mcp test [选项] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | 文本 | 是 | — | — | MCP 服务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai proxy

运行本地 MITM 代理，或控制已运行代理的断点。

```text
用法：argusai proxy [选项] 子命令 [参数]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--host` | 文本 | 否 | 127.0.0.1 | — | 监听地址。 |
| `--port` / `-p` | 整数 | 否 | 8866 | — | 监听端口。 |
| `--system` | 开关 | 否 | false | — | 同时将操作系统的系统代理指向此监听地址（谨慎使用）。 |
| `--json` | 开关 | 否 | false | — | 以 JSON 输出启动结果。 |

## argusai proxy break

启用断点，列出、放行或丢弃交互式断点暂停的请求。

```text
用法：argusai proxy break [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai proxy break drop

丢弃一个已暂停的请求。

```text
用法：argusai proxy break drop [选项] {pause_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `pause_id` | 文本 | 是 | — | — | 由 `break list` 列出的已暂停请求 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai proxy break forward

放行一个已暂停的请求，可在放行前修改。

```text
用法：argusai proxy break forward [选项] {pause_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `pause_id` | 文本 | 是 | — | — | 由 `break list` 列出的已暂停请求 ID。 |
| `--method` | 文本 | 否 | 空字符串 | — | 替换后的请求方法。 |
| `--url` | 文本 | 否 | 空字符串 | — | 用于替换的 URL。 |
| `--header` | 文本 | 否 | — | 可重复 | 用于替换的请求头，格式为 'Name: Value'；可重复指定。 |
| `--body` | 文本 | 否 | 空字符串 | — | 替换后的正文文本。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai proxy break list

列出当前在断点处暂停的请求。

```text
用法：argusai proxy break list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai proxy break off

停用断点。已暂停的请求仍需通过 forward 或 drop 处理。

```text
用法：argusai proxy break off [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai proxy break on

在运行中的代理上启用断点。匹配的请求将暂停，等待 forward 或 drop 处理。

```text
用法：argusai proxy break on [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--url` | 文本 | 否 | 空字符串 | — | 仅暂停 URL 中包含此文本的请求。 |
| `--host` | 文本 | 否 | 空字符串 | — | 仅暂停主机名包含此文本的请求。 |
| `--method` | 文本 | 否 | — | 可重复 | 仅匹配这些请求方法。可重复传入。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai report

根据已保存的发现重新生成 HTML / Markdown / JSON 报告。

```text
用法：argusai report [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--task-dir` | 路径 | 否 | — | — | 包含发现结果的任务目录或扫描运行目录路径。 |
| `--task-id` | 文本 | 否 | — | — | 已配置的 tasks_dir 中的任务 ID。 |
| `--format` / `-f` | 文本 | 否 | html,md,json | — | 用逗号分隔的格式：html、md、json。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai reverse

管理逆向任务：list / create / export / toolchain。

```text
用法：argusai reverse [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai reverse create

创建逆向任务。

```text
用法：argusai reverse create [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--title` / `-T` | 文本 | 是 | — | — | 任务标题。 |
| `--url` | 文本 | 否 | 空字符串 | — | 目标 URL。 |
| `--scope` | 文本 | 否 | 空字符串 | — | 范围标签（web/android/…）。 |
| `--intent` | 文本 | 否 | 空字符串 | — | 分析目的。 |
| `--platform` | 文本 | 否 | 空字符串 | — | 平台提示信息。 |
| `--tag` / `-t` | 文本 | 否 | — | 可重复 | 标签（可重复）。 |
| `--task` | 文本 | 否 | 空字符串 | — | 关联的扫描任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse export

将任务导出为 zip 文件或目录（不弹出图形文件管理器）。

```text
用法：argusai reverse export [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 逆向任务 ID。 |
| `--dest` / `-d` | 文本 | 否 | 空字符串 | — | 目标目录（默认使用存储区的导出目录）。 |
| `--decompiled` | 开关 | 否 | false | — | 包含 decompiled/ 目录，其体积可能较大。 |
| `--folder` | 开关 | 否 | false | — | 导出为文件夹，而非 zip 压缩包。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse list

列出逆向任务。

```text
用法：argusai reverse list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--scope` | 文本 | 否 | 空字符串 | — | 按范围筛选。 |
| `--status` | 文本 | 否 | 空字符串 | — | 按状态筛选。 |
| `--trash` | 开关 | 否 | false | — | 包含回收站中的任务。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse purge

永久删除逆向任务，也可删除回收站中的任务。

```text
用法：argusai reverse purge [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 逆向任务 ID。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 跳过确认。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse restore

从回收站恢复逆向任务。

```text
用法：argusai reverse restore [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 逆向任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse show

在终端中显示一个逆向任务的摘要。

```text
用法：argusai reverse show [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 逆向任务 ID。 |
| `--trash` | 开关 | 否 | false | — | 允许查看回收站中的任务。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse toolchain

扫描 PATH 和配置，查找逆向工具链的可执行文件。

```text
用法：argusai reverse toolchain [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--job` | 文本 | 否 | 空字符串 | — | 用于记录扫描结果的任务 ID（可选）。 |
| `--tool` | 文本 | 否 | — | 可重复 | 仅探测指定名称的工具（可重复）。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai reverse trash

将逆向任务移至回收站。

```text
用法：argusai reverse trash [选项] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | 文本 | 是 | — | — | 逆向任务 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai scan

对单个目标运行内置安全检查。

```text
用法：argusai scan [选项] {url}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `url` | 文本 | 是 | — | — | 目标 URL，例如 https://example.com。 |
| `--checks` / `-c` | 文本 | 否 | — | — | 以逗号分隔的检查项 ID（默认执行所有已注册检查项）。 |
| `--out` / `-o` | 路径 | 否 | — | — | 发现结果与报告的输出目录。 |
| `--format` / `-f` | 文本 | 否 | html,md,json | — | 以逗号分隔的报告格式：html、md、json。 |
| `--name` | 文本 | 否 | — | — | 便于识别的目标或运行名称。 |
| `--json` | 开关 | 否 | false | — | 输出机器可读的摘要。 |

## argusai serve

无界面宿主：channels + cron（操作系统服务生命周期管理）。

```text
用法：argusai serve [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai serve boot

修改已安装宿主服务的自动启动策略。

```text
用法：argusai serve boot [选项] {policy}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `policy` | 文本 | 是 | — | — | 启动策略：Windows 使用 logon\|startup；Linux 使用 user\|linger；macOS 使用 logon。 |
| `--start` | 开关 | 否 | false | — | 更新启动触发方式后，同时启动服务。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve install

将宿主注册为用户级操作系统服务（Task Scheduler / systemd --user / LaunchAgent）。

```text
用法：argusai serve install [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--cron-only` | 开关 | 否 | false | — | 安装仅运行定时调度的服务，不安装完整宿主服务。 |
| `--boot` | 文本 | 否 | 空字符串 | — | 启动策略：Windows 使用 logon\|startup；Linux 使用 user\|linger；macOS 使用 logon。 |
| `--no-start` | 开关 | 否 | false | — | 仅注册服务，不立即启动。 |
| `--dry-run` | 开关 | 否 | false | — | 输出服务安装计划，不执行安装。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve restart

重启已安装的宿主服务。

```text
用法：argusai serve restart [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve run

在前台运行无界面宿主（Ctrl+C 停止）。

```text
用法：argusai serve run [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--cron-only` | 开关 | 否 | false | — | 仅运行定时调度器，不启动渠道适配器。 |
| `--json` | 开关 | 否 | false | — | 以 JSON 输出启动状态，然后继续运行。 |

### argusai serve start

启动已安装的宿主服务。

```text
用法：argusai serve start [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve status

显示宿主服务是否已安装、是否正在运行。

```text
用法：argusai serve status [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve stop

停止已安装的宿主服务。

```text
用法：argusai serve stop [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai serve uninstall

移除宿主操作系统服务及旧版拆分服务。

```text
用法：argusai serve uninstall [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai team

管理常设 Agent 小队（list、chat、command、stop）。

```text
用法：argusai team [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai team add-member

添加成员；成员会等待小队收到首条指令后再执行。

```text
用法：argusai team add-member [选项] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--role` / `-r` | 文本 | 是 | — | — | 成员角色，用于 @提及。 |
| `--objective` / `-o` | 文本 | 是 | — | — | 分配给此成员的任务。 |
| `--permission` | 文本 | 否 | readonly | — | 只读（readonly）或限定范围（scoped）。 |
| `--tool` | 文本 | 否 | — | 可重复 | 使用 --permission scoped 时的工具白名单。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team chat

显示小队群聊，或某个成员的私聊记录。

```text
用法：argusai team chat [选项] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--member` / `-m` | 文本 | 否 | 空字符串 | — | 显示与此成员的私聊记录。 |
| `--limit` / `-n` | 整数 | 否 | 40 | — | 输出最近消息的条数。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team create

创建常设小队。预设成员会等待首次指令。

```text
用法：argusai team create [选项] {name}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `name` | 文本 | 是 | — | — | 小队名称。 |
| `--objective` / `-o` | 文本 | 否 | 空字符串 | — | 小队的工作目标。 |
| `--preset` | 文本 | 否 | 空字符串 | — | web_audit、recon_sweep 或 reverse_triage。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team delete

删除小队及其成员。

```text
用法：argusai team delete [选项] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--yes` / `-y` | 开关 | 否 | false | — | 删除时不再询问。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team greenlight

开启或关闭小队授权盾，保留工具确认。

```text
用法：argusai team greenlight [选项] {action} {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `action` | 文本 | 是 | — | — | on、off 或 status。 |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team list

列出常设小队。

```text
用法：argusai team list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team remove-member

从小队中移除一名成员。

```text
用法：argusai team remove-member [选项] {team} {member}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `member` | 文本 | 是 | — | — | 成员 ID 或角色。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team send

发送指令，并持续托管成员直到其完成工作；按 Ctrl+C 停止成员。

```text
用法：argusai team send [选项] {team} {message}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `message` | 文本 | 是 | — | — | 要发送的指令。 |
| `--member` / `-m` | 文本 | 否 | 空字符串 | — | 仅发送给此成员。 |
| `--private` | 开关 | 否 | false | — | 私发给 --member 指定的成员，不在群聊中显示。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team show

显示指定小队及其成员。

```text
用法：argusai team show [选项] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai team stop

停止指定小队成员的当前运行，并丢弃尚未处理的排队消息。

```text
用法：argusai team stop [选项] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | 文本 | 是 | — | — | 小队 ID 或名称。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## argusai todos

管理跨会话的用户级待办；此处不管理评估计划。

```text
用法：argusai todos [选项] 子命令 [参数]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai todos add

创建待办。

```text
用法：argusai todos add [选项] {title}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `title` | 文本 | 是 | — | — | 待办标题。 |
| `--body` / `-b` | 文本 | 否 | 空字符串 | — | 详细说明（可选）。 |
| `--due` | 文本 | 否 | 空字符串 | — | 截止时间，使用时间戳或日期字符串。 |
| `--tag` / `-t` | 文本 | 否 | — | 可重复 | 标签（可重复）。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai todos delete

删除待办。

```text
用法：argusai todos delete [选项] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | 文本 | 是 | — | — | 待办 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai todos done

将待办标记为已完成。

```text
用法：argusai todos done [选项] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | 文本 | 是 | — | — | 待办 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai todos list

列出用户待办。

```text
用法：argusai todos list [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--status` / `-s` | 文本 | 否 | 空字符串 | — | 状态筛选：pending/in_progress/completed/cancelled。 |
| `--tag` / `-t` | 文本 | 否 | 空字符串 | — | 按标签筛选。 |
| `--limit` / `-n` | 整数 | 否 | 100 | — | 最多返回的条数。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai todos show

显示一个待办。

```text
用法：argusai todos show [选项] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | 文本 | 是 | — | — | 待办 ID。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai todos status

设置待办状态。

```text
用法：argusai todos status [选项] {todo_id} {status}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | 文本 | 是 | — | — | 待办 ID。 |
| `status` | 文本 | 是 | — | — | 待办状态：待处理（pending）、进行中（in_progress）、已完成（completed）或已取消（cancelled）。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai update

仅在主动请求时检查更新；可选择下载并验证安装包。

```text
用法：argusai update [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--check` | 开关 | 否 | false | — | 检查更新但不下载（默认行为）。 |
| `--download` | 开关 | 否 | false | — | 下载并验证匹配的发行包。 |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

### argusai version

输出 ArgusAI 版本。

```text
用法：argusai version [选项]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | 输出便于程序读取的结果。 |

## 维护参数参考

新增、删除或修改命令声明后，在仓库根目录重新生成并检查：

```sh
uv run python scripts/cli_docs.py --write
uv run python scripts/cli_docs.py --check
```

生成器只遍历命令声明，不执行登录、网络探测、扫描、服务安装或配置写入。新增或修改帮助文案时，需同步更新 `scripts/cli_docs_zh.json`；缺少译文时生成会报错。新增命令的操作实例仍需同步补入命令手册。
