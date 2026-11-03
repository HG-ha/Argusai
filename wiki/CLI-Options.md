# CLI 参数参考

[Wiki 首页](Home.md) · [命令手册与完整示例](CLI-Reference.md) · [快速开始](Quick-Start.md)

本页从当前版本的实际 Typer 命令树生成，逐项列出参数、类型、必填状态、默认值和帮助原文。从配置 AI 到渠道、任务与产物投递的可复制示例见[命令手册](CLI-Reference.md)。

覆盖 **95 个末端命令**、**13 个命令分组或全局入口**。`proxy` 分组自身还可前台运行；交互控制台的斜杠指令见命令手册。

参数的“必填”表示解析层要求；服务账号、模型、路径和平台等条件仍由命令执行时检查。默认值不求值，也不读取用户配置；运行时默认与全局设置可影响实际行为。选项说明沿用 `--help` 原文，`--help` 本身不在每张表重复。

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

ArgusAI Security Workbench — local · scriptable · safe-by-default.

Bare invoke: interactive console → ops console; otherwise desktop GUI (override with --gui / --chat, or ARGUSAI_HOST=gui|cli).

```text
Usage: argusai [OPTIONS] COMMAND [ARGS]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Prefer JSON output where supported. |
| `--verbose` / `-v` | 开关 | 否 | false | — | Mirror logs to stderr (default: quiet for CLI subcommands). |
| `--gui` | 开关 | 否 | false | — | Force desktop GUI (overrides TTY auto-detect). |
| `--tray` | 开关 | 否 | false | — | Start the desktop GUI hidden in the system tray. |
| `--chat` | 开关 | 否 | false | — | Force ops console / Agent REPL (same as interactive default). |
| `--install-completion` | 开关 | 否 | — | — | Install completion for the current shell. |
| `--show-completion` | 开关 | 否 | — | — | Show completion for the current shell, to copy it or customize the installation. |

## argusai agent

Manage local Agent runtimes (built-in ACP presets + custom).

```text
Usage: argusai agent [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai agent add

Add or update a custom ACP agent runtime in settings.json.

```text
Usage: argusai agent add [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | str | 是 | — | — | Display name. [required] |
| `--command` / `-c` | str | 是 | — | — | Executable (PATH or absolute). [required] |
| `--arg` / `-a` | str | 否 | — | 可重复 | Command argument (repeatable), e.g. -a acp |
| `--id` | str | 否 | — | — | Stable runtime id. |
| `--env` / `-e` | str | 否 | — | 可重复 | Environment KEY=VALUE (repeatable). |
| `--cwd` | str | 否 | 空字符串 | — | Working directory. |
| `--notes` | str | 否 | 空字符串 | — | Optional notes. |
| `--enable` / `--disable` | 开关 | 否 | true | — | Enabled flag. [default: enable] |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent disable

Disable a runtime without deleting it.

```text
Usage: argusai agent disable [OPTIONS] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | str | 是 | — | — | Runtime id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent enable

Enable a runtime (including built-in presets).

```text
Usage: argusai agent enable [OPTIONS] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | str | 是 | — | — | Runtime id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent list

List configured local agents and PATH detection status.

```text
Usage: argusai agent list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai agent remove

Delete a custom runtime. Built-in presets can only be disabled.

```text
Usage: argusai agent remove [OPTIONS] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | str | 是 | — | — | Custom runtime id (not built-in). [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Skip confirmation. |
| `--json` | 开关 | 否 | false | — | — |

### argusai agent test

ACP initialize handshake (or native capability check).

```text
Usage: argusai agent test [OPTIONS] {runtime_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `runtime_id` | str | 是 | — | — | Runtime id. [required] |
| `--json` | 开关 | 否 | false | — | — |

## argusai ai

Manage chat AI endpoints (settings.json).

```text
Usage: argusai ai [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai ai activate

Raise an endpoint's priority so new turns use it first.

```text
Usage: argusai ai activate [OPTIONS] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | str | 是 | — | — | Endpoint index or name. [required] |
| `--model` / `-m` | str | 否 | — | — | Also switch active model. |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai add

Add or update a chat AI endpoint in settings.json.

```text
Usage: argusai ai add [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | str | 否 | — | — | Display name. |
| `--provider` / `-p` | str | 否 | — | — | Preset id/name/alias (see: argusai ai providers). |
| `--base-url` / `-u` | str | 否 | — | — | API base URL. |
| `--api-key` / `-k` | str | 否 | — | — | API key / token. |
| `--model` / `-m` | str | 否 | — | — | Active model id. |
| `--protocol` | str | 否 | — | — | openai or anthropic (default from provider). |
| `--priority` | int | 否 | — | — | Failover priority (higher first). |
| `--max-tokens` | int | 否 | — | — | Max output tokens. |
| `--context-window` | int | 否 | — | — | Context window tokens. |
| `--index` | int | 否 | — | — | Update existing endpoint by index. |
| `--extra-body` | str | 否 | — | — | JSON object merged into extra_body, e.g. '{"explicit_cache":true}'. |
| `--header` / `-H` | str | 否 | — | 可重复 | Extra header KEY=VALUE (repeatable). |
| `--enable` / `--disable` | 开关 | 否 | true | — | Enabled flag. [default: enable] |
| `--activate` | 开关 | 否 | false | — | After save, raise priority so this endpoint is active. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai ai disable

Disable an endpoint without deleting it.

```text
Usage: argusai ai disable [OPTIONS] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | str | 是 | — | — | Endpoint index or name. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai enable

Enable an endpoint without deleting it.

```text
Usage: argusai ai enable [OPTIONS] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | str | 是 | — | — | Endpoint index or name. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai list

List configured chat AI endpoints.

```text
Usage: argusai ai list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai ai providers

List built-in provider presets usable with ``ai add --provider``.

```text
Usage: argusai ai providers [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai ai remove

Delete an AI endpoint from settings.json.

```text
Usage: argusai ai remove [OPTIONS] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | str | 是 | — | — | Endpoint index or name. [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Skip confirmation. |
| `--json` | 开关 | 否 | false | — | — |

### argusai ai test

Send a lightweight ping prompt to one endpoint.

```text
Usage: argusai ai test [OPTIONS] {selector}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `selector` | str | 是 | — | — | Endpoint index or name. [required] |
| `--json` | 开关 | 否 | false | — | — |

## argusai channels

Manage and run communication channels (Telegram, Feishu, …).

```text
Usage: argusai channels [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai channels add

Create a channel by name; internal id is auto-generated unless --id is set.

```text
Usage: argusai channels add [OPTIONS] {provider}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `provider` | str | 是 | — | — | Provider id (telegram, feishu, …). [required] |
| `--name` / `-n` | str | 否 | — | — | Display name (what you see in the UI). Defaults to the provider label. |
| `--id` | str | 否 | — | — | Optional internal id. Omit to auto-generate (recommended). |
| `--secret` / `-s` | str | 否 | — | 可重复 | Credential KEY=VALUE (repeatable). |
| `--setting` | str | 否 | — | 可重复 | Settings KEY=VALUE (repeatable), e.g. proxy=… |
| `--host` | str | 否 | — | — | Listen host (wecom/webhook). |
| `--port` | int | 否 | — | — | Listen port (wecom/webhook). |
| `--path` | str | 否 | — | — | Callback path (wecom/webhook). |
| `--enable` | 开关 | 否 | false | — | Enable immediately after save. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai channels bind

Bind a paired identity to an assistant session.

```text
Usage: argusai channels bind [OPTIONS] {identity_id} {session_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `identity_id` | str | 是 | — | — | Identity id. [required] |
| `session_id` | str | 是 | — | — | Assistant session id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels disable

Disable a channel.

```text
Usage: argusai channels disable [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Channel id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels enable

Enable a channel.

```text
Usage: argusai channels enable [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Channel id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels grant

Set permissions for a paired channel identity.

```text
Usage: argusai channels grant [OPTIONS] {identity_id} [permissions]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `identity_id` | str | 是 | — | — | Identity id (or unique prefix). [required] |
| `permissions` | str | 否 | — | 可变数量 | Permission names: chat status cancel confirm configure send. |
| `--all` | 开关 | 否 | false | — | Grant every identity permission. |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels identities

List paired identities (and bound sessions).

```text
Usage: argusai channels identities [OPTIONS] [channel_id]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 否 | 空字符串 | — | Optional channel id filter. |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels list

List configured channels.

```text
Usage: argusai channels list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai channels login-qq

Bind a QQ bot through the official QR Onboard flow.

```text
Usage: argusai channels login-qq [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Existing QQ channel id. [required] |
| `--timeout` | float | 否 | 300.0 | — | Seconds to wait for scan. [default: 300.0] |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels login-wechat

Complete WeChat iLink QR sign-in for a channel.

```text
Usage: argusai channels login-wechat [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Existing wechat channel id. [required] |
| `--timeout` | float | 否 | 180.0 | — | Seconds to wait for scan. [default: 180.0] |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels pair

Create a pairing code for remote users to bind this channel.

```text
Usage: argusai channels pair [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Channel id. [required] |
| `--session` | str | 否 | 空字符串 | — | Optional session to bind after pairing (e.g. cli-default). |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels providers

List supported channel providers and required secrets.

```text
Usage: argusai channels providers [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai channels remove

Delete a channel and its encrypted secrets.

```text
Usage: argusai channels remove [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Channel id. [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Skip confirmation. |
| `--json` | 开关 | 否 | false | — | — |

### argusai channels serve

Run channel adapters in the foreground (prefer ``argusai serve``).

```text
Usage: argusai channels serve [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Print startup status as JSON then keep running. |

### argusai channels test

Probe one channel adapter.

```text
Usage: argusai channels test [OPTIONS] {channel_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `channel_id` | str | 是 | — | — | Channel id. [required] |
| `--json` | 开关 | 否 | false | — | — |

### argusai chat

Enter an interactive Agent conversation (multi-turn ops console).

```text
Usage: argusai chat [OPTIONS] [message]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `message` | str | 否 | — | 可变数量 | Opening line. Words are joined, so `chat /team use <id>` enters that team. |
| `--session` / `-s` | str | 否 | cli-default | — | Assistant session id (history is reused across turns). [default: cli-default] |
| `--yes` / `-y` | 开关 | 否 | false | — | Auto-approve eligible non-high-risk tool confirms; high-risk still asks on a TTY. |
| `--once` | 开关 | 否 | false | — | One-shot mode: require a message or file, run one turn, then exit. |
| `--timeout` | float | 否 | 600.0 | — | Max seconds per turn. [default: 600.0] |
| `--json` | 开关 | 否 | false | — | One-shot JSON summary (implies --once). |
| `--file` / `-f` | file | 否 | — | 可重复 | Attach a local file to the next Agent turn. Repeat for multiple files. |

## argusai cron

Manage assistant jobs (time, signed webhook, or file change).

```text
Usage: argusai cron [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai cron add

Create or update a scheduled assistant job.

```text
Usage: argusai cron add [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | str | 是 | — | — | Job display name. [required] |
| `--prompt` / `-p` | str | 是 | — | — | Message sent to the assistant. [required] |
| `--every` | int | 否 | — | — | Interval seconds (mutually exclusive with --cron). |
| `--cron` | str | 否 | — | — | 5-field cron expr, e.g. '0 9 * * *'. |
| `--webhook` | 开关 | 否 | false | — | Trigger from the built-in signed webhook. |
| `--watch-path` | str | 否 | 空字符串 | — | Trigger when this file or directory changes. |
| `--glob` | str | 否 | * | — | File glob used with --watch-path. [default: *] |
| `--recursive` / `--no-recursive` | 开关 | 否 | true | — | Recurse under watched directories. [default: recursive] |
| `--poll` | int | 否 | 2 | — | File watcher poll interval in seconds. [default: 2] |
| `--debounce` | int | 否 | 2 | — | Webhook/file trigger debounce seconds. [default: 2] |
| `--id` | str | 否 | — | — | Stable job id. |
| `--destination` | str | 否 | new_session | — | new_session \| dedicated_session \| session [default: new_session] |
| `--session` | str | 否 | 空字符串 | — | Target session id when --destination=session. |
| `--tag` / `-t` | str | 否 | — | 可重复 | Tag (repeatable). |
| `--enable` / `--disable` | 开关 | 否 | true | — | Enabled flag. [default: enable] |
| `--approve-high-risk` | 开关 | 否 | false | — | Explicitly authorize unattended high-risk tools for this job. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron authorize

Grant or revoke job-scoped unattended high-risk execution.

```text
Usage: argusai cron authorize [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Cron job id. [required] |
| `--revoke` | 开关 | 否 | false | — | Revoke high-risk approval. |
| `--yes` / `-y` | 开关 | 否 | false | — | Confirm the authorization change. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron disable

Disable a cron job.

```text
Usage: argusai cron disable [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Cron job id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron enable

Enable a cron job.

```text
Usage: argusai cron enable [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Cron job id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron list

List assistant cron jobs.

```text
Usage: argusai cron list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron remove

Delete a cron job.

```text
Usage: argusai cron remove [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Cron job id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron runs

Show recent run history for a job.

```text
Usage: argusai cron runs [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Cron job id. [required] |
| `--limit` / `-n` | int | 否 | 20 | — | History rows. [default: 20] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai cron serve

Run the cron scheduler in the foreground (prefer ``argusai serve --cron-only``).

```text
Usage: argusai cron serve [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Print startup status as JSON then keep running. |

### argusai cron webhook-secret

Rotate and show a webhook signing secret once.

```text
Usage: argusai cron webhook-secret [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Webhook-triggered job id. [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Confirm secret rotation. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai doctor

Probe shells, managed runtimes, browser, and channel config.

```text
Usage: argusai doctor [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai kb

Browse and manage the local knowledge base (terminal-friendly).

```text
Usage: argusai kb [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai kb add

Create a knowledge entry.

```text
Usage: argusai kb add [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--title` / `-T` | str | 是 | — | — | Entry title. [required] |
| `--body` / `-b` | str | 否 | 空字符串 | — | Markdown / plain body. |
| `--file` / `-f` | 路径 | 否 | — | — | Read body from a UTF-8 file. |
| `--tag` / `-t` | str | 否 | — | 可重复 | Tag (repeatable). |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb delete

Move an entry to trash (soft delete).

```text
Usage: argusai kb delete [OPTIONS] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | str | 是 | — | — | Knowledge entry id. [required] |
| `--reason` | str | 否 | cli kb delete | — | Trash reason. [default: cli kb delete] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb list

List knowledge entries (newest first).

```text
Usage: argusai kb list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--trash` | 开关 | 否 | false | — | Include trashed entries. |
| `--superseded` | 开关 | 否 | false | — | Include superseded entries. |
| `--limit` / `-n` | int | 否 | 50 | — | Max rows to show. [default: 50] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb purge

Permanently delete trashed knowledge entries.

```text
Usage: argusai kb purge [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--id` | str | 否 | — | 可重复 | Hard-delete specific trashed id(s) (repeatable). |
| `--older-days` | int | 否 | — | — | Hard-delete trashed entries older than N days (omit with --all). |
| `--all` | 开关 | 否 | false | — | Hard-delete everything currently in trash. |
| `--yes` / `-y` | 开关 | 否 | false | — | Skip confirmation. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb restore

Restore a knowledge entry from trash.

```text
Usage: argusai kb restore [OPTIONS] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | str | 是 | — | — | Knowledge entry id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb search

Search the knowledge index (terminal browse).

```text
Usage: argusai kb search [OPTIONS] [query]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `query` | str | 否 | 空字符串 | — | Keywords (title / tags / triggers). |
| `--tag` / `-t` | str | 否 | — | 可重复 | Filter by tag. |
| `--limit` / `-n` | int | 否 | 20 | — | Max matches. [default: 20] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb show

Print one entry with markdown body in the terminal.

```text
Usage: argusai kb show [OPTIONS] {entry_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `entry_id` | str | 是 | — | — | Knowledge entry id. [required] |
| `--max-chars` | int | 否 | 20000 | — | Truncate body for the terminal (0 = full). [default: 20000] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai kb trash

List trashed knowledge entries.

```text
Usage: argusai kb trash [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai mcp

Manage MCP servers (stdio / HTTP / SSE).

```text
Usage: argusai mcp [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai mcp add

Add or update an MCP server in settings.json.

```text
Usage: argusai mcp add [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--name` / `-n` | str | 是 | — | — | Display name. [required] |
| `--command` / `-c` | str | 否 | 空字符串 | — | stdio executable. |
| `--arg` / `-a` | str | 否 | — | 可重复 | stdio argument (repeatable). |
| `--url` | str | 否 | 空字符串 | — | HTTP/SSE endpoint URL. |
| `--transport` | str | 否 | 空字符串 | — | stdio \| streamable-http \| sse (default: stdio if --command, else sse). |
| `--id` | str | 否 | — | — | Stable server id. |
| `--env` / `-e` | str | 否 | — | 可重复 | Environment KEY=VALUE (repeatable). |
| `--header` / `-H` | str | 否 | — | 可重复 | HTTP header KEY=VALUE (repeatable). |
| `--cwd` | str | 否 | 空字符串 | — | Working directory (stdio). |
| `--notes` | str | 否 | 空字符串 | — | Optional notes. |
| `--enable` / `--disable` | 开关 | 否 | true | — | Enabled flag. [default: enable] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp disable

Disable an MCP server without deleting it.

```text
Usage: argusai mcp disable [OPTIONS] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | str | 是 | — | — | MCP server id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp enable

Enable an MCP server.

```text
Usage: argusai mcp enable [OPTIONS] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | str | 是 | — | — | MCP server id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp list

List configured MCP servers and last probe status.

```text
Usage: argusai mcp list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp probe

Kick a background probe of every enabled MCP server.

```text
Usage: argusai mcp probe [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp remove

Delete an MCP server from settings.

```text
Usage: argusai mcp remove [OPTIONS] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | str | 是 | — | — | MCP server id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai mcp test

Handshake / health-check one MCP server (blocking).

```text
Usage: argusai mcp test [OPTIONS] {server_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `server_id` | str | 是 | — | — | MCP server id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai proxy

Run the local MITM proxy, or control breakpoints on the one already running.

```text
Usage: argusai proxy [OPTIONS] COMMAND [ARGS]...
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--host` | str | 否 | 127.0.0.1 | — | Listen host. [default: 127.0.0.1] |
| `--port` / `-p` | int | 否 | 8866 | — | Listen port. [default: 8866] |
| `--system` | 开关 | 否 | false | — | Also set the OS system proxy to this listener (use carefully). |
| `--json` | 开关 | 否 | false | — | Print start result as JSON. |

## argusai proxy break

Arm, list, forward, or drop interactive request breakpoints.

```text
Usage: argusai proxy break [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai proxy break drop

Drop one parked request.

```text
Usage: argusai proxy break drop [OPTIONS] {pause_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `pause_id` | str | 是 | — | — | Parked request id from `break list`. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai proxy break forward

Forward one parked request, optionally edited.

```text
Usage: argusai proxy break forward [OPTIONS] {pause_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `pause_id` | str | 是 | — | — | Parked request id from `break list`. [required] |
| `--method` | str | 否 | 空字符串 | — | Replacement method. |
| `--url` | str | 否 | 空字符串 | — | Replacement URL. |
| `--header` | str | 否 | — | 可重复 | Replacement header, 'Name: Value'. Repeatable. |
| `--body` | str | 否 | 空字符串 | — | Replacement body text. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai proxy break list

List requests currently parked at a breakpoint.

```text
Usage: argusai proxy break list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai proxy break off

Disarm breakpoints. Parked requests are left for forward or drop.

```text
Usage: argusai proxy break off [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai proxy break on

Arm breakpoints on the running proxy. Matching requests wait for forward or drop.

```text
Usage: argusai proxy break on [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--url` | str | 否 | 空字符串 | — | Only park URLs containing this text. |
| `--host` | str | 否 | 空字符串 | — | Only park hosts containing this text. |
| `--method` | str | 否 | — | 可重复 | Only these methods. Repeatable. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai report

Rebuild HTML / Markdown / JSON reports from saved findings.

```text
Usage: argusai report [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--task-dir` | 路径 | 否 | — | — | Path to a task or scan run directory containing findings. |
| `--task-id` | str | 否 | — | — | Task id under the configured tasks_dir. |
| `--format` / `-f` | str | 否 | html,md,json | — | Comma-separated formats: html, md, json. [default: html,md,json] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai reverse

Manage reverse-engineering jobs (list / create / export / toolchain).

```text
Usage: argusai reverse [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai reverse create

Create a reverse job.

```text
Usage: argusai reverse create [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--title` / `-T` | str | 是 | — | — | Job title. [required] |
| `--url` | str | 否 | 空字符串 | — | Target URL. |
| `--scope` | str | 否 | 空字符串 | — | Scope label (web/android/…). |
| `--intent` | str | 否 | 空字符串 | — | Analysis intent. |
| `--platform` | str | 否 | 空字符串 | — | Platform hint. |
| `--tag` / `-t` | str | 否 | — | 可重复 | Tag (repeatable). |
| `--task` | str | 否 | 空字符串 | — | Linked scan task id. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse export

Export a job to zip/folder (no GUI file-manager popup).

```text
Usage: argusai reverse export [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Reverse job id. [required] |
| `--dest` / `-d` | str | 否 | 空字符串 | — | Destination dir (default: store exports). |
| `--decompiled` | 开关 | 否 | false | — | Include decompiled/ (can be large). |
| `--folder` | 开关 | 否 | false | — | Export as a folder instead of zip. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse list

List reverse jobs.

```text
Usage: argusai reverse list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--scope` | str | 否 | 空字符串 | — | Filter by scope. |
| `--status` | str | 否 | 空字符串 | — | Filter by status. |
| `--trash` | 开关 | 否 | false | — | Include trashed jobs. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse purge

Permanently delete a reverse job (including from trash).

```text
Usage: argusai reverse purge [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Reverse job id. [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Skip confirmation. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse restore

Restore a reverse job from trash.

```text
Usage: argusai reverse restore [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Reverse job id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse show

Show one reverse job summary in the terminal.

```text
Usage: argusai reverse show [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Reverse job id. [required] |
| `--trash` | 开关 | 否 | false | — | Allow trashed jobs. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse toolchain

Scan PATH / settings for reverse toolchain binaries.

```text
Usage: argusai reverse toolchain [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--job` | str | 否 | 空字符串 | — | Optional job id to record scan on. |
| `--tool` | str | 否 | — | 可重复 | Limit scan to named tools (repeatable). |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai reverse trash

Move a reverse job to trash.

```text
Usage: argusai reverse trash [OPTIONS] {job_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `job_id` | str | 是 | — | — | Reverse job id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai scan

Run built-in security checks against one target.

```text
Usage: argusai scan [OPTIONS] {url}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `url` | str | 是 | — | — | Target URL (e.g. https://example.com). [required] |
| `--checks` / `-c` | str | 否 | — | — | Comma-separated check ids (default: all registered). |
| `--out` / `-o` | 路径 | 否 | — | — | Output directory for findings / reports. |
| `--format` / `-f` | str | 否 | html,md,json | — | Comma-separated report formats: html, md, json. [default: html,md,json] |
| `--name` | str | 否 | — | — | Friendly target / run name. |
| `--json` | 开关 | 否 | false | — | Emit machine-readable summary. |

## argusai serve

Headless host: channels + cron (OS service lifecycle).

```text
Usage: argusai serve [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai serve boot

Change autostart policy for an already-installed host service.

```text
Usage: argusai serve boot [OPTIONS] {policy}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `policy` | str | 是 | — | — | Boot policy: Windows logon\|startup; Linux user\|linger; macOS logon. [required] |
| `--start` | 开关 | 否 | false | — | Also start the service after updating the trigger. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve install

Register the host as a user OS service (Task Scheduler / systemd --user / LaunchAgent).

```text
Usage: argusai serve install [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--cron-only` | 开关 | 否 | false | — | Install a cron-only service instead of full host. |
| `--boot` | str | 否 | 空字符串 | — | Boot policy: Windows logon\|startup; Linux user\|linger; macOS logon. |
| `--no-start` | 开关 | 否 | false | — | Register only; do not start immediately. |
| `--dry-run` | 开关 | 否 | false | — | Print the service plan without installing. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve restart

Restart the installed host service.

```text
Usage: argusai serve restart [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve run

Run the headless host in the foreground (Ctrl+C to stop).

```text
Usage: argusai serve run [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--cron-only` | 开关 | 否 | false | — | Only run the cron scheduler (skip channel adapters). |
| `--json` | 开关 | 否 | false | — | Print startup status as JSON then keep running. |

### argusai serve start

Start the installed host service.

```text
Usage: argusai serve start [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve status

Show whether the host service is installed / running.

```text
Usage: argusai serve status [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve stop

Stop the installed host service.

```text
Usage: argusai serve stop [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai serve uninstall

Remove the host OS service (and any legacy split services).

```text
Usage: argusai serve uninstall [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai team

Manage standing Agent teams (list, chat, command, stop).

```text
Usage: argusai team [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai team add-member

Add a member. They wait until the first team command.

```text
Usage: argusai team add-member [OPTIONS] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--role` / `-r` | str | 是 | — | — | Member role, used for @mentions. [required] |
| `--objective` / `-o` | str | 是 | — | — | This member's assignment. [required] |
| `--permission` | str | 否 | readonly | — | readonly or scoped. [default: readonly] |
| `--tool` | str | 否 | — | 可重复 | Tool whitelist when --permission scoped. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team chat

Print the group room, or one member's private thread.

```text
Usage: argusai team chat [OPTIONS] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--member` / `-m` | str | 否 | 空字符串 | — | Show the private thread with this member. |
| `--limit` / `-n` | int | 否 | 40 | — | How many recent messages to print. [default: 40] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team create

Create a standing team. Preset members wait for the first command.

```text
Usage: argusai team create [OPTIONS] {name}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `name` | str | 是 | — | — | Team name. [required] |
| `--objective` / `-o` | str | 否 | 空字符串 | — | What this team is for. |
| `--preset` | str | 否 | 空字符串 | — | web_audit, recon_sweep, or reverse_triage. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team delete

Delete a team and its members.

```text
Usage: argusai team delete [OPTIONS] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--yes` / `-y` | 开关 | 否 | false | — | Delete without prompting. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team greenlight

Turn the team authorization shield on or off. Tool confirms stay.

```text
Usage: argusai team greenlight [OPTIONS] {action} {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `action` | str | 是 | — | — | on, off, or status. [required] |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team list

List standing teams.

```text
Usage: argusai team list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team remove-member

Remove one member from the team.

```text
Usage: argusai team remove-member [OPTIONS] {team} {member}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `member` | str | 是 | — | — | Member id or role. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team send

Send a command and host the members until they finish. Ctrl+C stops them.

```text
Usage: argusai team send [OPTIONS] {team} {message}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `message` | str | 是 | — | — | Command to send. [required] |
| `--member` / `-m` | str | 否 | 空字符串 | — | Send only to this member. |
| `--private` | 开关 | 否 | false | — | Private to --member. Hidden from the group. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team show

Show one team and its members.

```text
Usage: argusai team show [OPTIONS] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai team stop

Stop every member hosted by this process and drop queued replies.

```text
Usage: argusai team stop [OPTIONS] {team}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `team` | str | 是 | — | — | Team id or name. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## argusai todos

Manage user-level todos (cross-session, not assessment plan).

```text
Usage: argusai todos [OPTIONS] COMMAND [ARGS]...
```

无额外参数。使用 `--help` 查看命令帮助。

### argusai todos add

Create a todo.

```text
Usage: argusai todos add [OPTIONS] {title}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `title` | str | 是 | — | — | Todo title. [required] |
| `--body` / `-b` | str | 否 | 空字符串 | — | Optional details. |
| `--due` | str | 否 | 空字符串 | — | Due timestamp / date string. |
| `--tag` / `-t` | str | 否 | — | 可重复 | Tag (repeatable). |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai todos delete

Delete a todo.

```text
Usage: argusai todos delete [OPTIONS] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | str | 是 | — | — | Todo id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai todos done

Mark a todo completed.

```text
Usage: argusai todos done [OPTIONS] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | str | 是 | — | — | Todo id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai todos list

List user todos.

```text
Usage: argusai todos list [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--status` / `-s` | str | 否 | 空字符串 | — | Filter: pending/in_progress/completed/cancelled. |
| `--tag` / `-t` | str | 否 | 空字符串 | — | Filter by tag. |
| `--limit` / `-n` | int | 否 | 100 | — | Max rows. [default: 100] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai todos show

Show one todo.

```text
Usage: argusai todos show [OPTIONS] {todo_id}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | str | 是 | — | — | Todo id. [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai todos status

Set todo status.

```text
Usage: argusai todos status [OPTIONS] {todo_id} {status}
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `todo_id` | str | 是 | — | — | Todo id. [required] |
| `status` | str | 是 | — | — | pending\|in_progress\|completed\|cancelled [required] |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai update

Check for updates only when requested; optionally download a verified package.

```text
Usage: argusai update [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--check` | 开关 | 否 | false | — | Check for updates without downloading (default). |
| `--download` | 开关 | 否 | false | — | Download and verify the matching release package. |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

### argusai version

Print the ArgusAI version.

```text
Usage: argusai version [OPTIONS]
```

| 参数 | 类型 | 必填 | 默认值 | 重复 / 多值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `--json` | 开关 | 否 | false | — | Machine-readable output. |

## 维护参数参考

新增、删除或修改命令声明后，在仓库根目录重新生成并检查：

```sh
uv run python scripts/cli_docs.py --write
uv run python scripts/cli_docs.py --check
```

生成器只遍历命令声明，不执行登录、网络探测、扫描、服务安装或配置写入。新增命令的操作实例仍需同步补入命令手册。
