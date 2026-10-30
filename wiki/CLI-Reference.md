# 命令行参考

[Wiki 首页](Home.md) · [快速开始](Quick-Start.md) · [自动化](Automation.md)

ArgusAI 的 CLI 与桌面共用应用内核，可以进入交互式 Agent 对话，也可以用子命令运行扫描、管理任务和集成。本页以源码启动形式 `uv run argusai` 展示；已安装环境可使用 `argusai`，发行包使用相应可执行文件。

本页命令与当前 CLI 源码及 `--help` 对照。具体选项仍以你正在运行版本的帮助为准。

## 入口与帮助

```sh
uv run argusai --help
uv run argusai version --json
uv run argusai doctor
uv run argusai --gui
uv run argusai --chat
```

| 入口 | 行为 |
| --- | --- |
| 交互终端中裸运行 `argusai` | 进入 ops 控制台 |
| `argusai --chat` | 显式进入交互控制台 |
| `argusai --gui` | 显式启动桌面 GUI |
| `argusai --tray` | 以托盘方式启动 GUI |
| `argusai <子命令> --help` | 查看该命令的参数，不执行业务操作 |

无 TTY 环境不要依赖裸命令进入交互；脚本应选择明确的子命令。`--verbose` 可用于将日志镜像到 stderr，收集日志前先考虑脱敏。

## 检查与下载更新

CLI 只在主动执行更新命令时访问官网版本清单。普通命令、帮助、版本输出和交互式 Agent 启动都不会自动检查或插入更新提示。

```sh
uv run argusai update
uv run argusai update --check --json
argusai update --download
```

`update` 默认只检查并显示当前版本、最新版本、更新内容和发行页面；`--check` 显式执行相同行为。发行包用户加 `--download` 后下载当前系统与架构的包，并校验大小及 SHA-256，完成后显示包路径。此命令不会自行启动安装器或打开 GUI；可手动安装已校验的包，或进入桌面端「关于 → 安装并重启」。下载中按 Ctrl+C 取消。

源码或包管理器安装按原有 Git、uv、pip 流程升级，更新命令不会覆盖仓库或 Python 环境。`--json` 提供机器可读结果；检查失败或下载失败返回非零退出码。

## Agent 对话

### 多轮对话与固定会话

```sh
uv run argusai chat
uv run argusai chat --session release-review
uv run argusai chat --session release-review "继续整理上次的待办，先报告当前进度。"
```

`--session` / `-s` 的值是会话 ID，默认 `cli-default`。同一 ID 复用历史，数据目录不同则不共享同一份历史。

### 单轮与附件

```sh
uv run argusai chat --session offline-review --once --file build/quickstart/note.txt "仅分析附件，不发送网络请求。"
uv run argusai chat --session offline-review --json "只总结该会话已有材料和待办，不调用外部工具。"
```

附件路径应已存在，多个附件重复使用 `--file` / `-f`。交互中也可以用 `@path` 或 `@"含空格的路径"` 引用文件。路径只解决读取位置，不代表材料已经允许发送给模型。

| 参数 | 作用 |
| --- | --- |
| `--once` | 运行一轮后退出，需要消息或附件 |
| `--json` | 输出单轮 JSON 摘要，同时启用单轮模式 |
| `--timeout` | 单轮最大等待秒数，CLI 默认 600 秒 |
| `--yes` / `-y` | 自动通过非高风险工具确认；高风险操作在 TTY 中仍询问 |

CLI 的 `--yes` 参数本身与 GUI 当前对话的完整“自动审批”不是同一个范围。它处理仍然弹出的普通确认；高风险确认在非交互/JSON 环境无法获批时会停止等待流程，不能仅靠 `--yes` 越过。已有会话还可能保存单独的授权状态，执行前应一起核对。

### 控制台斜杠命令

在 ops 的输入区输入以下命令，而不是在外部 shell 中输入。`/help` 显示当前版本支持的完整列表。

| 命令 | 用途 |
| --- | --- |
| `/status`、`/session` | 查看当前工作与会话状态 |
| `/sessions`、`/use 会话ID` | 查看并切换会话 |
| `/new 新名称` | 新建对话 |
| `/model` | 查看和选择当前会话的模型 |
| `/search on`、`/search off` | 调整当前会话的联网搜索 |
| `/think high`、`/thinking status` | 调整或查看思考设置，实际支持取决于模型 |
| `/tools`、`/tasks`、`/jobs` | 查看工具、任务或运行工作 |
| `/resume` | 查看或恢复可续跑工作，具体选择按交互提示 |
| `/cancel` | 请求取消当前执行 |
| `/btw 一个问题` | 旁路询问当前会话相关问题，不加入主对话历史 |
| `/team list`、`/team use 小队名称` | 进入小队房间 |
| `/team leave` | 退出房间，回到主对话 |
| `/monitor status`、`/monitor tokens` | 查看监控显示配置或切换用量显示 |
| `/lang zh`、`/lang en` | 切换 CLI 语言 |
| `/clear` | 清理控制台显示，不删除会话历史 |
| `/quit`、`/exit` | 退出控制台 |

`/greenlight`、`/allow`、`/deny` 涉及授权范围和确认状态，使用前阅读交互提示及[数据与安全](Data-and-Safety.md)。

## Web 扫描与报告

扫描会向目标发起请求。示例仅对你自己启动的本地测试服务使用，准备方法见[快速开始](Quick-Start.md)。

```sh
uv run argusai scan http://127.0.0.1:8000 --checks headers --out build/local-audit --format html,md,json --json
```

| 参数 | 作用 |
| --- | --- |
| `URL` | 必填目标 URL |
| `--checks` / `-c` | 逗号分隔 Check ID；不传时运行全部已注册检查 |
| `--out` / `-o` | 运行结果的基目录，具体路径读取输出的 `run_dir` |
| `--format` / `-f` | 报告格式，支持 html、md、json |
| `--name` | 目标或运行显示名称 |
| `--json` | 机器可读摘要，含报告位置和检查错误 |

不要把“未出现发现”直接解释为“没有漏洞”。核对 `errors`、实际执行检查、证据和业务上下文。

对已有结果重新生成报告：

```sh
uv run argusai report --task-dir path/to/run --format html,md,json --json
uv run argusai report --task-id TASK_ID --format html
```

将 `path/to/run` 替换为实际返回的运行目录，`TASK_ID` 替换为 GUI/任务管理中的任务 ID。目录需包含 `findings.jsonl` 或 `report.json`。重新生成报告不会重新扫描目标。

## 代理与断点

前台启动代理：

```sh
uv run argusai proxy --host 127.0.0.1 --port 8866
```

在另一个终端管理该数据目录中已运行代理的断点：

```sh
uv run argusai proxy break on --host localhost --method GET
uv run argusai proxy break list --json
uv run argusai proxy break forward PAUSE_ID
uv run argusai proxy break off
```

`PAUSE_ID` 来自断点列表。`forward` 还支持 `--method`、`--url`、重复的 `--header` 与 `--body`；`break drop PAUSE_ID` 丢弃请求。它们会改变在途请求的处理，应只用于约定的测试环境。

`proxy --system` 会尝试切换系统代理；需要系统代理时再显式使用。HTTPS 解密需要按平台管理代理 CA，见[Web 审计与代理](Web-Audit-and-Proxy.md)。

## 小队

```sh
uv run argusai team list
uv run argusai team create "接口复核" --objective "整理测试材料并记录待验证问题"
uv run argusai team add-member "接口复核" --role "材料整理" --objective "只读分析已有材料并索引证据" --permission readonly
uv run argusai team show "接口复核"
uv run argusai chat /team use "接口复核"
```

进入房间后，普通输入发给当前小队。管理命令还包括 `team chat`、`team send`、`team stop`、`team remove-member` 和 `team delete`。`team send` 会实际派发工作，使用前核对接收小队和目标成员。

`team greenlight on 小队名称` 属于授权变更，不应作为不看提示的默认初始化步骤。

## 逆向工作台

```sh
uv run argusai reverse list --json
uv run argusai reverse create --title "本地样本复核" --platform android --intent "整理静态材料与待验证线索"
uv run argusai reverse show JOB_ID
uv run argusai reverse toolchain --job JOB_ID
uv run argusai reverse export JOB_ID --dest build/reverse-export --folder
```

`JOB_ID` 来自创建或列表输出。`create` 建立任务元数据，不会自动找到并导入本机的 APK；样本导入、产物分析和动态操作按[逆向工作台](Reverse-Workbench.md)继续。

`trash` 放入回收站，`restore` 恢复，`purge` 永久清理。导出内容可能包含样本和反编译结果，向外发送前核对材料权限。

## 知识库与个人待办

```sh
uv run argusai kb list
uv run argusai kb search "认证"
uv run argusai kb add --title "测试材料复核步骤" --file build/quickstart/note.txt --tag review
uv run argusai kb show ENTRY_ID
uv run argusai todos add "复核测试材料" --tag review
uv run argusai todos list --status pending
uv run argusai todos done TODO_ID
```

`ENTRY_ID`、`TODO_ID` 来自相应列表。知识库还支持 `delete`、`trash`、`restore`、`purge`；个人待办支持 `show`、`status`、`delete`。回收站与永久删除含义不同，执行清理前核对对象。

## 定时与事件任务

CLI 支持四类触发：间隔、Cron、Webhook 和文件变化。下例先建立停用状态的任务，检查配置后再启用：

```sh
uv run argusai cron add --id demo-review --name "测试材料复查" --every 3600 --destination dedicated_session --prompt "只复核已提供材料，整理新增证据和待办，不发送主动请求。" --disable
uv run argusai cron list --json
uv run argusai cron enable demo-review
uv run argusai cron runs demo-review
uv run argusai cron disable demo-review
```

创建任务时，在 `--every 秒数`、`--cron "五字段表达式"`、`--webhook` 和 `--watch-path 路径` 中选择一种。运行目的地为 `new_session`、`dedicated_session` 或 `session`；最后一种需指定 `--session`。

任务需要在线调度宿主才能运行。单次 `cron list` 退出后不会替你保持调度。前台托管：

```sh
uv run argusai serve run --cron-only
```

`cron authorize` 与 `--approve-high-risk` 是独立的高风险授权入口；`cron webhook-secret` 涉及签名密钥。先了解调用影响，避免把密钥输出记录进共享日志。运行次数、续跑和通知策略详见[自动化](Automation.md)；CLI 的 `cron add` 不暴露全部 GUI/工具字段。

## 常驻服务

```sh
uv run argusai serve status
uv run argusai serve install --dry-run
uv run argusai serve run
```

`serve run` 在前台托管渠道与定时任务，`Ctrl+C` 停止。`serve install --dry-run` 只显示系统服务计划。

确认环境稳定后，可使用 `serve install` 注册服务，或用 `--no-start` 仅注册。`start`、`stop`、`restart`、`uninstall` 管理服务。启动策略见 `serve install --help`：Windows 支持 logon/startup，Linux 支持 user/linger，macOS 支持 logon；部分策略涉及额外系统权限。

服务绑定安装时的解释器和路径。移动源码目录、删除虚拟环境或更换用户后，应重新核对服务配置。相同数据根目录的宿主应按软件的单实例约束运行，避免多个进程争用。

## 通信渠道、MCP 与 ACP

先查看库存与状态：

```sh
uv run argusai channels providers
uv run argusai channels list
uv run argusai channels identities
uv run argusai mcp list
uv run argusai agent list
uv run argusai ai providers
uv run argusai ai list
```

| 命令组 | 支持的管理操作 |
| --- | --- |
| `channels` | add、enable、disable、remove、test、pair、identities、bind、grant、login-qq、login-wechat、serve |
| `mcp` | add、enable、disable、remove、test、probe |
| `agent` | add、enable、disable、remove、test |
| `ai` | providers、add、enable、disable、remove、activate、test |

渠道与模型密钥优先通过设置页填写，避免出现在 shell 历史或进程参数中。配置完渠道后，可用 `channels pair CHANNEL_ID --session SESSION_ID` 生成配对流程，再由实际使用者在规定的私聊中完成配对。

`mcp test`、`mcp probe` 和 `agent test` 会连接或启动配置的服务；`ai test` 会向模型端点发测试请求。它们不只是读取配置，执行前核对命令、地址与凭据来源。详细操作见[集成](Integrations.md)。

`ai activate` 更改全局默认端点；Agent 对话中的模型选择属于当前会话，两者不要混淆。

## JSON 与退出状态

自动化脚本使用具体子命令支持的 `--json`：

```sh
uv run argusai version --json
uv run argusai reverse list --json
uv run argusai cron list --json
```

根命令的 `--json` 表示偏好，不能替代每个子命令自身的输出契约。检查 `ok`、错误字段与进程退出状态，不要只判断 stdout 是否非空。

`chat --json` 隐含单轮模式。长驻命令即使带 `--json` 仍可能持续运行，例如 `channels serve` 输出启动状态后继续托管。不同命令没有统一“发现漏洞即返回非零”的约定；脚本应按该命令的实际结果处理。

## 查找实现与排查问题

命令实现位于 [CLI commands](../argusai/cli/commands/)，交互命令在 [shell.py](../argusai/cli/shell.py)。遇到参数不识别，先核对版本与对应 `--help`；无模型响应或数据目录不同则参见[故障排查](Troubleshooting.md)。
