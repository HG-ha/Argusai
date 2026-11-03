# 命令行参考

[文档首页](Home.md) · [快速开始](Quick-Start.md) · [完整参数表](CLI-Options.md) · [集成](Integrations.md) · [自动化](Automation.md)

先配置一个 AI 端点，再用 `argusai chat` 开始工作。CLI 也能管理模型端点、通信渠道、MCP、ACP、小队、定时任务、知识库和逆向任务，或直接运行扫描、代理与报告命令。

本页按当前代码的 95 个叶子命令编写，每个命令都有实例；无子命令的 `argusai proxy` 前台入口另列。逐项选项、类型和默认值见[完整参数表](CLI-Options.md)。以下均使用安装后的 `argusai`；Windows 若未加入 PATH，在安装目录把命令开头换成 `.\ArgusAI.exe`，Linux 在解压目录使用 `./ArgusAI`。源码开发者可按[安装指南](Installation.md)进入自己的环境。

命令中的 `JOB_ID`、`ENTRY_ID`、`TODO_ID`、`IDENTITY_ID`、`PAUSE_ID` 都要替换成前一步实际返回的值；`demo-ai`、`review-team` 等名称由示例创建。操作同一份资料时，保持相同用户和应用数据目录。

## 入口、帮助与诊断

```sh
argusai --help
argusai version --json
argusai doctor
argusai --gui
argusai --tray
argusai --chat
argusai ai add --help
```

交互终端中裸运行 `argusai` 进入交互控制台；`--gui` 启动桌面，`--tray` 启动到托盘。没有 TTY 的脚本应明确写子命令。`doctor` 检查运行环境，输出中的 `warn` 可能表示可选工具未安装，结合自己要用的功能判断。`--verbose` 将日志镜像到 stderr。

需要命令行补全时，`argusai --show-completion` 显示补全脚本，供你核对后手动加入配置。`argusai --install-completion` 会写入当前用户的终端配置；当前 Typer 实现在 PowerShell 下还会尝试把当前用户（`CurrentUser`）的执行策略设为不受限（`Unrestricted`）。自行管理执行策略的用户应使用显示脚本的方式，按自己的策略安装补全。补全属于可选系统配置，不是软件使用前提。

普通单行命令可在 PowerShell 与 bash 中使用；包含 JSON 的两处示例另附 Windows PowerShell 5.1 写法。引号中的路径含空格时保留引号；重复值应重复选项，例如 `--tag review --tag internal`。`--arg=--stdio` 的等号用于把以短横线开头的内容作为参数值。不要把本页每一组示例整段执行：创建、派发、授权和删除各有不同的前提。

## 从零配置端点与模型

### 准备服务地址、模型 ID 和密钥

服务地址和模型 ID 来自你已经开通的服务或本地模型。`argusai ai providers` 展示内置预设；预设可预填地址、协议与默认模型，仍需核对实际账号可用的模型。

```sh
argusai ai providers
argusai ai list --json
```

下面用自定义 OpenAI 兼容端点演示。先用隐藏输入保存当前终端的密钥变量，避免把密钥原文写进命令历史。

bash：

```bash
read -r -p 'API base URL: ' ARGUSAI_BASE_URL
read -r -p 'Model ID: ' ARGUSAI_MODEL
read -r -s -p 'API key: ' ARGUSAI_API_KEY
printf '\n'
export ARGUSAI_BASE_URL ARGUSAI_MODEL ARGUSAI_API_KEY
argusai ai add --name demo-ai --provider custom --protocol openai --base-url "$ARGUSAI_BASE_URL" --api-key "$ARGUSAI_API_KEY" --model "$ARGUSAI_MODEL" --priority 10 --activate
```

PowerShell（含 Windows PowerShell 5.1）：

```powershell
$env:ARGUSAI_BASE_URL = Read-Host 'API base URL'
$env:ARGUSAI_MODEL = Read-Host 'Model ID'
$secret = Read-Host 'API key' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $env:ARGUSAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
argusai ai add --name demo-ai --provider custom --protocol openai --base-url "$env:ARGUSAI_BASE_URL" --api-key "$env:ARGUSAI_API_KEY" --model "$env:ARGUSAI_MODEL" --priority 10 --activate
```

变量由命令行环境展开后传给 `--api-key`，ArgusAI 不会把字符串 `$ARGUSAI_API_KEY` 当成密钥引用。变量写法减少历史记录泄漏，但参数值仍可能被有权限查看进程的人看到；共享主机可在桌面设置里填写密钥。用完后 bash 执行 `unset ARGUSAI_API_KEY`，PowerShell 执行 `Remove-Item Env:ARGUSAI_API_KEY`。

新端点必须有地址、模型和非空 API 密钥；已知预设可以补齐前两项。本地服务若不校验密钥，也要按服务约定填写非空占位值。Anthropic 端点使用 `--protocol anthropic`，并填写该服务要求的地址。

### 测试、启用与切换

```sh
argusai ai list
argusai ai test demo-ai
argusai ai activate demo-ai
argusai chat --session cli-first --once "只回复：连接正常"
argusai chat --session cli-first
```

`ai list` 显示端点索引、启用状态、当前默认、模型和密钥是否已设置。`ai test` 发送很短的 ping 请求，成功后可再测试一次真实对话。默认端点按优先级选择；`ai activate` 会启用端点并把它的优先级提高到现有最高值之上。

在打开的交互对话内输入 `/model` 可列出当前运行时的选择，再用 `/model 1` 按列表序号切换当前会话。这里的显示序号与 `ai list` 的从 0 开始的端点索引不是一回事。

### 更新已有端点与绑定模型

先从 `ai list` 取得真实索引；下例假设目标端点索引为 `0`：

```sh
argusai ai add --index 0 --model MODEL_ID --max-tokens 8192 --context-window 131072
argusai ai add --index 0 --priority 20 --extra-body '{"temperature":0.2}' --header X-Client=argusai
argusai ai activate demo-ai --model MODEL_ID
argusai ai disable demo-ai
argusai ai enable demo-ai
argusai ai remove demo-ai
```

上面 JSON 参数的单引号写法适用于 bash 和使用标准原生参数传递的 PowerShell 7.3+。Windows PowerShell 5.1 会移除传给原生程序的内层双引号，应改用以下写法；同一规则也适用于下文的代理请求正文：

```powershell
argusai ai add --index 0 --priority 20 --extra-body '{\"temperature\":0.2}' --header X-Client=argusai
```

将 `MODEL_ID` 换成服务实际支持的标识。`ai add --index` 是更新入口，省略的密钥会保留；同名也可匹配已有端点，索引更明确。`add` 的 `--enable` 默认开启，更新一个原本停用的端点时，若要继续停用，应同时传 `--disable`。删除后索引可能改变。

`--model` 指定端点的默认模型；`ai activate --model` 可同时切换默认模型。`--max-tokens` 是输出上限，`--context-window` 是上下文容量提示，不能借此扩大服务真实限制。`--priority` 越大越优先，多个启用端点用于回退。`--extra-body` 必须是 JSON 对象，`--header KEY=VALUE` 可重复。

当前 CLI 没有 `ai update`、`ai probe`、`ai models` 子命令。保存与更新使用 `ai add`，连通性测试使用 `ai test`。模型目录枚举、模态能力探测及更完整的模型能力编辑位于桌面设置；CLI 的 `test` 不等于完成这些探测。全局 AI 总开关也应在设置中核对。对话内 `/model` 的选择保存在当前会话，`ai activate` 管理全局默认端点。

## Agent 对话与终端内命令

### 带文件开始、继续并导出结构化结果

先在当前目录准备不含密钥的 `note.txt`。例如写上：“测试应用使用 Cookie 会话，接口为 GET /api/profile，尚未提供响应头。”

```sh
argusai chat --session offline-review --once --file note.txt "只分析附件，不访问目标；列出事实、材料依据和待补充的信息。"
argusai chat --session offline-review "继续上次的材料复核，先检查已有笔记。"
argusai chat --session offline-review --once --file note.txt --file request.har --json "整理材料索引，标注尚未验证的线索。"
```

最后一条需要实际存在的 `request.har`；只准备一个文件时删去第二个 `--file`。`--session` 默认 `cli-default`，显式指定可避免不同工作的历史混在一起；`--timeout` 默认 600 秒。`--once` 完成一轮后退出，`--json` 也启用单轮模式。附件、会话和输出继续使用同一个应用数据目录。

`--yes` 只处理可自动允许的普通工具确认；高风险确认仍遵循运行时规则。不要把它当成任意动作的通行证。材料分析虽然不向被分析目标发请求，模型调用仍会访问已配置端点。

<a id="在-ops-输入区使用斜杠命令"></a>

### 在交互输入区使用斜杠命令

先执行 `argusai chat --session offline-review`，再把下表中的命令输入到对话输入区。斜杠命令由本地交互控制台分派，其中旁路问答、续跑和带目标的授权命令也会调用 Agent。普通自然语言会提交到当前对话或小队房间。

| 功能 | 可直接输入的示例 | 结果或前提 |
| --- | --- | --- |
| 帮助与状态 | `/help` · `/status` · `/session` | 查看命令、工作状态与会话 ID |
| 对话列表 | `/sessions` · `/sessions 2` | 列表与翻页 |
| 切换对话 | `/use SESSION_ID` | 用列表中的 ID 切换 |
| 新建对话 | `/new` | 创建新会话；`/new 名称` 的参数用于选择运行时，先查看 `/agents` |
| 运行时列表 | `/agents` | 查看可用的原生/ACP运行时；`/agent` 是别名 |
| 模型选择 | `/model` · `/model 1` · `/model refresh` | 列表、按显示序号切换、刷新；`/endpoint` 是别名 |
| 联网搜索 | `/search status` · `/search off` · `/search on` | 调整当前对话的搜索开关 |
| 思考配置 | `/think status` · `/think high` · `/thinking off` | 当前对话；还支持 on、auto、none、minimal、low、medium、xhigh、max，实际能力依端点而定 |
| 工具与确认 | `/tools` · `/allow pending` · `/deny pending` | 查看待确认工具，按终端确认提示处理 |
| 单个工具决定 | `/allow CALL_ID` · `/deny CALL_ID` | `CALL_ID` 取自工具确认列表 |
| 自动允许 | `/allow all` · `/allow off` | 调整当前会话自动允许；高风险规则仍生效 |
| 批量拒绝 | `/deny all` | 拒绝当前待处理工具 |
| 授权状态 | `/greenlight status` · `/greenlight on` · `/greenlight off` | 当前会话授权开关；开启前阅读提示 |
| 授权后执行目标 | `/greenlight on 只读整理当前附件` | 授权并立即提交目标，会实际运行 Agent |
| 任务列表 | `/tasks` · `/tasks 2` | 查看扫描任务、翻页 |
| 持久运行 | `/jobs` · `/jobs 2` | 查看运行记录、翻页 |
| 恢复运行 | `/resume JOB_ID 继续整理尚未完成的证据索引` | 选择实际运行记录并提交续跑消息 |
| 旁路提问 | `/btw 这个接口为什么需要进一步核实` | 对当前工作提问；`/side` 是别名 |
| 文件列表 | `/file list` · `/file search report` | 列出或搜索当前会话文件 |
| 文件选择 | `/file 1` | 使用列表编号查看文件元信息与路径，按提示继续 |
| 附件引用 | `@note.txt 请分析这份材料` | 从启动目录模糊查找文件；Tab补全，具体读取范围见提示 |
| 小队列表与房间 | `/team list` · `/team use review-team` · `/team leave` | 进入房间后，后续普通输入发给小队 |
| 小队消息 | `/team chat review-team` · `/team pm review-team 材料整理` | 查看群聊或进入指定成员私聊 |
| 小队控制 | `/team stop review-team` · `/team greenlight on` | 停止成员回复或调整当前小队授权 |
| 提及成员 | `@材料整理 请索引附件` | 在小队房间内使用 |
| 监控 | `/monitor status` · `/monitor both` · `/monitor off` | 还支持 on、sys、tokens、agents |
| 侧边栏 | `/sidebar status` · `/sidebar auto` · `/sidebar off` | 还支持 on |
| 语言 | `/lang zh` · `/lang en` | 修改 CLI 显示语言 |
| 清屏 | `/clear` | 重绘终端，不删除历史 |
| 中止与退出 | `/cancel` · `/quit` | 中止当前执行或离开控制台；`/exit`、`/q` 也能退出 |

`/new` 的运行时选择在首次发言前确定，运行后不要把同一个会话临时改成另一运行时。完整权限和上下文行为见[Agent 与会话](Agent-and-Sessions.md)。

## 小队：创建、派发、查看与结束

```sh
argusai team create review-team --objective "复核已有测试材料，整理证据与待验证问题"
argusai team add-member review-team --role 材料整理 --objective "只读整理附件与证据索引" --permission readonly
argusai team list
argusai team show review-team
argusai team send review-team "先列出需要的材料，不发送目标请求"
argusai team chat review-team --limit 20
argusai team send review-team "整理当前附件列表" --member 材料整理 --private
argusai team chat review-team --member 材料整理
argusai team stop review-team
```

`create --preset web_audit`、`recon_sweep`、`reverse_triage` 可按预设建队；成员等待实际派发。`send` 会执行工作；`chat` 读取消息，`--member` 查看相应成员私聊。成员选择可用列表显示的 ID 或角色名。`--permission` 默认 `readonly`，扩大工具范围前核对授权。

维护与清理示例，按需要分别执行：

```sh
argusai team greenlight on review-team
argusai team greenlight off review-team
argusai team remove-member review-team 材料整理
argusai team delete review-team
```

`greenlight` 是授权变更；`remove-member` 和 `delete` 会改变小队。删除有确认交互，需要脚本确认时再使用 `--yes`。

## 扫描、报告与代理

### 扫描到报告的完整流程

先准备你拥有或已明确获授权的测试服务，例如已经在 `http://127.0.0.1:8000` 运行的本机页面：

```sh
argusai scan http://127.0.0.1:8000 --checks headers --out audit-output --format html,md,json --name "本机测试" --json
argusai report --task-dir audit-output/RUN_DIR --format html,md,json --json
```

把第二条路径换成第一条返回的 `run_dir`，然后打开返回的 HTML 报告。`--out` 是输出基目录，每次运行的目录以结果为准；它不是 GUI 任务 ID。若报告来自 GUI 任务，也可以用 `argusai report --task-id TASK_ID --format html`。

`scan` 不传 `--checks` 时运行默认检查集合；输出有发现不一定代表进程失败。JSON 结果需同时检查 `ok`、`errors`、`findings` 和报告路径；无发现也不等于目标已被证明安全。`report` 根据已有发现重新渲染，不会重新扫描。

### 前台代理与断点

在终端 A 启动代理；Ctrl+C 停止：

```sh
argusai proxy --host 127.0.0.1 --port 8866
```

让测试浏览器使用该代理；HTTPS 解密需先按[代理手册](Web-Audit-and-Proxy.md)管理 CA。`--system` 会修改系统代理，仅在需要时显式添加。保持终端 A 运行，在相同用户和数据目录下的终端 B 控制断点：

```sh
argusai proxy break on --host localhost --method GET
argusai proxy break list --json
argusai proxy break forward PAUSE_ID
argusai proxy break drop PAUSE_ID
argusai proxy break off
```

`PAUSE_ID` 来自实际断点列表，`forward` 与 `drop` 是对一个在途请求的不同处理选择，不能对同一条记录先后照抄。改写后放行的例子：`argusai proxy break forward PAUSE_ID --method POST --header Content-Type=application/json --body '{"review":true}'`。断点控制要求同一数据目录中的代理已经运行。

Windows PowerShell 5.1 的 JSON 正文写法：

```powershell
argusai proxy break forward PAUSE_ID --method POST --header Content-Type=application/json --body '{\"review\":true}'
```

## 知识库与个人待办

### 将复核笔记加入知识库

```sh
argusai kb add --title "认证材料复核步骤" --body "记录会话依据，区分事实与待验证线索。" --tag review
argusai kb list --limit 20
argusai kb search "认证" --tag review
argusai kb show ENTRY_ID --max-chars 8000
argusai kb delete ENTRY_ID --reason "示例归档清理"
argusai kb trash
argusai kb restore ENTRY_ID
```

`add --file note.txt` 可从已有 UTF-8 文件加入正文。`delete` 把条目移到回收站，`restore` 恢复。永久清理只在核对对象后执行：

```sh
argusai kb purge --id ENTRY_ID --yes
```

也可用 `--older-days 30` 筛选旧条目或 `--all` 清空回收站；它们不是上面单条清理的必需参数。

### 记录个人待办

```sh
argusai todos add "补齐响应头材料" --body "由目标负责人提供脱敏请求与响应" --tag review
argusai todos list --status pending
argusai todos show TODO_ID
argusai todos status TODO_ID in_progress
argusai todos done TODO_ID
argusai todos delete TODO_ID
```

`--due` 可附截止时间文本。状态使用 `pending`、`in_progress`、`completed`、`cancelled`；`done` 设为 `completed`，`delete` 删除该待办。

## 逆向工作台

```sh
argusai reverse create --title "本地样本复核" --platform android --intent "整理静态材料与待验证线索" --tag review
argusai reverse list --json
argusai reverse show JOB_ID
argusai reverse toolchain --job JOB_ID
argusai reverse export JOB_ID --dest reverse-export --folder
argusai reverse trash JOB_ID
argusai reverse list --trash
argusai reverse restore JOB_ID
```

`create` 只建立任务元数据，不会自动导入本机 APK。提供样本、解包和分析操作按[逆向工作台](Reverse-Workbench.md)继续；`toolchain --tool jadx` 可只看所需工具。`export` 默认 ZIP，`--folder` 改为目录，`--decompiled` 把可能较大的反编译结果也包含进去。

核对样本与证据后，永久删除示例是 `argusai reverse purge JOB_ID --yes`；它会删除任务内容，包含回收站中的任务。

## 定时与事件任务

先配置可用模型。下面创建停用任务、检查配置、启用并查看运行记录：

```sh
argusai cron add --id hourly-review --name "材料复查" --every 3600 --destination dedicated_session --prompt "复核已有材料的变化，保存证据索引，不向目标主动请求。" --disable
argusai cron list --json
argusai cron enable hourly-review
argusai cron runs hourly-review --limit 10
argusai cron disable hourly-review
```

任务需要在线宿主才会执行，可用 `argusai serve run --cron-only`。`cron list` 本身不会持续调度。任务默认启用，示例显式 `--disable` 便于先核对；`--id` 相同会更新对应任务。

四种触发各有创建实例，分别使用，不能混用触发选项：

```sh
argusai cron add --id morning-review --name "每日复核" --cron "0 9 * * *" --destination dedicated_session --prompt "复核本地已有材料并报告变化。" --disable
argusai cron add --id webhook-review --name "事件复核" --webhook --destination new_session --prompt "复核本次事件提供的材料。" --disable
argusai cron add --id file-review --name "目录变化复核" --watch-path ./incoming --glob "*.har" --recursive --poll 5 --debounce 3 --destination dedicated_session --prompt "整理新增HAR材料，先检查授权范围。" --disable
argusai cron add --id existing-review --name "继续指定对话" --every 3600 --destination session --session offline-review --prompt "继续上次未完成的材料整理。" --disable
```

文件例子要求 `incoming` 路径实际存在。`new_session` 每次新建对话，`dedicated_session` 使用任务专属对话，`session` 必须传真实会话 ID。高级续跑字段、通知和事件签名见[自动化](Automation.md)；CLI `cron add` 未暴露该页全部 GUI 字段。

授权与清理示例需要单独理解其影响：

```sh
argusai cron authorize hourly-review --yes
argusai cron authorize hourly-review --revoke --yes
argusai cron webhook-secret webhook-review --yes
argusai cron remove hourly-review
```

`authorize` 授予或撤销任务级无人值守高风险执行，`--yes` 是显式确认。`webhook-secret` 轮换并只显示一次签名密钥，不要把其输出放入共享日志。旧调用方要同步新密钥。`remove` 删除任务。

## 常驻服务与启停策略

前台入口分别是：

```sh
argusai serve run
argusai channels serve
argusai cron serve
argusai serve run --cron-only
```

`serve run` 托管渠道与定时任务，`channels serve` 是全量宿主入口，`cron serve` 和 `--cron-only` 仅托管调度。它们持续运行，Ctrl+C 停止；即使加 `--json` 也不会变成一次性查询。

先查看服务计划，再决定是否注册：

```sh
argusai serve status
argusai serve install --dry-run
argusai serve install --no-start
argusai serve start
argusai serve restart
argusai serve stop
argusai serve uninstall
```

`install --no-start` 注册但不立即启动，默认 `install` 则会启动。服务绑定安装时的可执行文件、参数及用户环境；移动安装路径后应核对服务。相同数据目录不要同时运行争用宿主锁的多个实例。

已安装服务的启停策略按系统选择，下面三条是不同平台的例子：

| 平台 | 安装或修改示例 | 含义 |
| --- | --- | --- |
| Windows | `argusai serve install --boot logon`；`argusai serve boot startup --start` | 用户登录触发，或改为系统启动触发并启动；startup 需要相应权限 |
| Linux | `argusai serve install --boot user`；`argusai serve boot linger --start` | 用户服务；linger 允许退出登录后继续，按系统提示完成权限操作 |
| macOS | `argusai serve install --boot logon`；`argusai serve boot logon --start` | 登录时运行 LaunchAgent |

## 通信渠道：创建、登录、配对与权限

### 用 QQ 建立一条渠道

```sh
argusai channels providers
argusai channels add qq --id qq-demo --name "QQ测试渠道"
argusai channels login-qq qq-demo
argusai channels list
argusai channels enable qq-demo
argusai channels test qq-demo
argusai channels pair qq-demo --session offline-review
```

`add` 默认停用，避免保存不完整配置后就开始接入；`login-qq` 显示登录流程，由机器人所有者扫码授权。配对码有时效，保持渠道宿主运行后，由实际使用者在该渠道的私聊发送 `/pair 配对码`，完成后检查身份与绑定。

```sh
argusai serve run
```

宿主前台运行时，在另一个相同用户环境的终端执行管理命令：

```sh
argusai channels identities qq-demo
argusai channels bind IDENTITY_ID offline-review
argusai channels grant IDENTITY_ID chat status cancel
```

`grant` 设置该身份的权限集合。可选项为 `chat`、`status`、`cancel`、`confirm`、`configure`、`send`；上例仅保留前三项。`argusai channels grant IDENTITY_ID --all` 授予全套权限，应按实际使用者职责决定。

微信登录和已有令牌配置分别示例：

```sh
argusai channels add wechat --id wechat-demo --name "微信测试渠道"
argusai channels login-wechat wechat-demo
```

```bash
argusai channels add telegram --id telegram-demo --name "Telegram测试渠道" --secret "bot_token=$TELEGRAM_BOT_TOKEN"
```

Telegram 的令牌先按前面的隐藏输入方式放进环境变量；PowerShell 把值改为 `"bot_token=$env:TELEGRAM_BOT_TOKEN"`。其它平台先看 `channels providers --json` 返回的凭据字段，再按重复 `--secret KEY=VALUE` 和 `--setting KEY=VALUE` 配置；回调型平台还可用 `--host`、`--port`、`--path`。

停用和删除例子：

```sh
argusai channels disable qq-demo
argusai channels remove qq-demo
```

`remove` 删除渠道及其保存的凭据，有交互确认。平台权限、流式/卡片能力与附件边界见[集成](Integrations.md)。

## MCP：接入工具服务

以下例子以已在本机 `http://127.0.0.1:9000/mcp` 运行的 MCP HTTP 服务为前提，地址应换成你的实际服务：

```sh
argusai mcp add --id local-tools --name "本地工具" --transport streamable-http --url http://127.0.0.1:9000/mcp --disable
argusai mcp list
argusai mcp enable local-tools
argusai mcp test local-tools
argusai mcp probe
argusai mcp disable local-tools
argusai mcp remove local-tools
```

通过标准输入输出（stdio）通信的服务配置例子是 `argusai mcp add --id stdio-tools --name "stdio工具" --command MCP_EXECUTABLE --arg=--stdio --cwd WORK_DIR --disable`。把 `MCP_EXECUTABLE` 与 `WORK_DIR` 换成已经安装的 MCP 服务程序和工作目录。`--env KEY=VALUE` 与 `--header KEY=VALUE` 可重复。

`add` 带同一 `--id` 可更新配置，默认启用；示例先停用。`test` 会连接或启动一个服务，`probe` 批量探测，不只是读取设置。`remove` 删除配置。MCP 工具权限仍受当前会话规则控制。

## ACP：接入本地 Agent

先按所选本地 Agent 的文档准备 ACP 兼容可执行程序，再把 `ACP_EXECUTABLE` 与 `WORK_DIR` 替换为实际路径：

```sh
argusai agent add --id local-acp --name "本地ACP Agent" --command ACP_EXECUTABLE --arg=--stdio --cwd WORK_DIR --disable
argusai agent list
argusai agent enable local-acp
argusai agent test local-acp
argusai agent disable local-acp
argusai agent remove local-acp
```

`--arg` 重复传多个参数，`--env KEY=VALUE` 提供该子进程环境；不同 Agent 的 ACP 启动参数不同，不能默认都接受 `--stdio`。`add` 带同一 ID 更新配置，`test` 会启动进程并测试 ACP 握手。凭据、模型和外部 Agent 自有工具权限按相应运行时管理，不能把本工作台的审批状态当成外部 Agent 的全部权限设置。

在交互控制台中用 `/agents` 查看，再 `/new local-acp` 创建对应运行时的新会话。不要复用已绑定其它运行时的执行会话。

## 更新、JSON 与脚本处理

```sh
argusai update
argusai update --check --json
argusai update --download
argusai version --json
```

普通 CLI 命令不会主动检查软件更新。`update` 默认只检查官网版本信息，发行包用户加 `--download` 后下载匹配平台的包并验证大小和 SHA-256，返回实际路径；不会自动打开 GUI 或执行安装器。源码或包管理器安装按原安装方式更新。

多数一次性命令支持 `--json`，脚本应读取 `ok`、错误字段和退出状态。根命令的 `--json` 是偏好，不能代替命令自身输出契约。`chat --json` 启用单轮模式，常驻宿主仍然持续运行。

遇到问题先核对这些信息：

| 现象 | 排查方向 |
| --- | --- |
| `No such command` / `No such option` | `argusai version --json`、当前命令 `--help`；不要使用本页明确不存在的 `ai update` 等名称 |
| 新端点保存失败 | 地址、模型、非空密钥、`--protocol` 是否为 openai 或 anthropic；JSON 参数是否有效对象 |
| `ai test` 超时或鉴权失败 | 服务地址、API 密钥、模型 ID、账号模型权限、网络；端点可响应不代表每种工具/模态能力均可用 |
| 对话看不到预期模型 | 先查 `ai list`，再查会话内 `/model`；检查默认端点和当前会话是否固定了不同选择 |
| 找不到 ID 或历史 | 从当前用户、当前数据目录的 list 命令重新取 ID；不要把示例占位名当作真实 ID |
| MCP/ACP 测试失败 | 可执行程序、工作目录、启动参数和依赖；HTTP 服务是否运行；标准输入输出程序是否按相应协议通信 |
| 渠道显示 `ext` | 可能由另一个服务宿主托管，先查 `serve status` 与宿主 PID，不必重复启动 |
| 定时任务不运行 | 启用状态、下次运行时间、在线宿主、授权、目的会话；`cron runs` 查看实际记录 |
| 断点命令连接失败 | 同一用户/数据根的代理必须先运行；`PAUSE_ID` 需来自当前断点列表 |
| 扫描无发现 | 检查 `errors`、实际选择的检查、返回报告与证据；不要仅看发现数量 |

专题步骤继续见[集成](Integrations.md)、[自动化](Automation.md)、[数据与安全](Data-and-Safety.md)与[故障排查](Troubleshooting.md)。
