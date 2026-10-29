# AGENTS.md — argusai 工程总规约

> 这份文件是**仓库根部的"工作合同"**：任何人（或 AI 代理）来这个仓库写代码，第一件事就是读它。本地若存在 `docs/`（默认不随仓库发布），可继续作为补充规范输入。

## 1. 项目是什么

`argusai` 是一个**可脚本化的 Web 安全审计工具**，同时提供：

- **CLI** —— `python -m argusai scan ...`、`argusai-gui`、`argusai report ...`、`argusai proxy`
- **桌面 GUI** —— pywebview 包裹的 Vue 3 SPA
- **被动 MITM 代理** —— 透明拦截 + 规则驱动的被动分析
- **AI 安全助手** —— 工具 + 技能（Skills）两层 + 多端点 fallback + 流式
- **JS 逆向工作台** —— 静态/动态分析、反混淆、JSVMP、WASM、加密复现

工程上按 Agent 原生 runtime 约束实现（确认分层、上下文预算、证据归档、UI blocks 等）。本地若有 Loom22 / 架构文档，改 Agent runtime 前优先对照；没有则以此文件 + `README` + 源码契约为准。

详见 `README.md`（中文）/ `README.en.md`。

## 2. 顶层目录约定

```
argusai/                ← Python 包根
├─ __main__.py           ← 入口，按终端环境分流 CLI / GUI
├─ gui/                  ← pywebview 桥接，多 mixin 拼装 Api 类
├─ core/                 ← 与 UI 无关的内核 (Check/Task/Context/AI/Proxy/Reverse...)
├─ checks/               ← 内置安全检查（每个文件一个 Check 子类）
├─ skills/               ← AI 助手的 Markdown 技能（progressive disclosure）
├─ report/               ← html/md/json 报告渲染
├─ webui/                ← Vue 3 前端 (src/, dist/, package.json)
└─ vendor/               ← 保留的小型第三方资源 (runners, innoextract, sqlmap)

docs/                    ← 本地工程文档（默认 gitignore，不发布）
wiki/                    ← 随版本维护的公开用户手册与 GitHub Wiki 源稿
scripts/                 ← 一次性/维护类脚本
tests/                   ← pytest 测试
pyproject.toml
README.md
AGENTS.md                ← 本文件
```

**新建顶层目录前**：先在 PR 描述里说明理由，避免"垃圾抽屉"目录滋生。

## 3. 不可变契约（破坏即视为 P0 bug）

以下契约**必须保持稳定**；变更需要写清理由（本地若维护 ADR，可写在 `docs/adr/`）：

1. **`argusai/__init__.py` 导出** —— `Finding, Severity, Target, AuthConfig, Context, Runner`。  
   下游用户已在脚本里 `from argusai import Finding` 这么用。

2. **`argusai.gui:Api` 类的对外方法名** —— `webui/src/api.js` 里 `call("xxx")` 的所有 `xxx`。  
   Python 端方法名 == JS 端 `call()` 字面量。改 Python 必须同时改 JS，反之亦然。

3. **`argusai.gui:Api.settings_mgr` 属性** —— `argusai/core/_tools.py` 直接用 `api.settings_mgr.get().sessions_dir`。

4. **`argusai-gui = "argusai.gui:main"` 入口** —— `pyproject.toml` 里登记了。`argusai.gui` 现在是包，`main` 在 `__init__.py` 重导出。

5. **`Check.id`** —— 任务参数里的 `checks` 字段持久化在 `tasks/<id>/task.json`；改 `Check.id` 等于让旧任务无法 rescan。  
   只能加新 check，**不能改名旧 check 的 id**。除非同时写迁移逻辑。

6. **`Finding.fingerprint()` 算法** —— 用于去重，改算法 = 历史数据全部"变成新发现"。

7. **`data_root` 子目录名** —— `SUBDIR_TASKS / SUBDIR_SESSIONS / SUBDIR_POC_PAGES / SUBDIR_KNOWLEDGE / SUBDIR_REVERSE_JOBS / SUBDIR_LOGS`。改名要附带迁移代码。
 `SUBDIR_LOGS` 刻意**不在** `PERSISTENT_DATA_SUBDIRS` 里：Windows 上搬运正在写入的 rotating log 不安全，所以换 `data_root` 时只重新绑定 handler，旧日志留在原地（迁移预览会给 `log_dir_not_transferred` 警告）。 旧版 `<app_root>/logs` 由 `adopt_legacy_log_files` 首次启动时一次性收编。

8. **AI Tool 的 `name` 与 `parameters schema`** —— 老会话 (`*.json`) 里残留的 `tool_use` 会用这些 schema 重放。改 schema 等于让历史会话无法续聊。

## 4. 常用命令

| 任务 | 命令 |
|---|---|
| 启动 GUI | `argusai-gui` / `argusai --gui`（交互 TTY 下裸 `argusai` 默认进 ops 控制台） |
| CLI | `argusai --help` · `scan` / `report` / `proxy` / `channels serve` / `chat`（多轮 Agent REPL） / `doctor` / `version` |
| 前端开发 | `cd argusai/webui && npm install && npm run dev` |
| 前端构建 | `cd argusai/webui && npm run build` |
| 保留的 Vendor 工具（innoextract） | `python scripts/vendor_runtime.py` |
| 烟测 | `python scripts/smoke_tasks.py` / `python scripts/smoke_browser_hub.py` |
| 文档检查 | `python scripts/wiki_docs.py check` |
| Nuitka 打包 | `uv run --group build python build.py --help` |

## 5. 提交与 PR 规范

### Commit message

格式：`<area>: <imperative summary>` —— **不超过 72 字符**。`area` 用以下小写词之一：

- `core` —— 内核（Check/Runner/Context/AI/Proxy/...）
- `checks` —— 单个安全检查
- `gui` —— pywebview 后端或 webui 前端
- `assistant` —— AI 助手相关
- `reverse` —— JS 逆向子系统
- `proxy` —— 被动代理
- `vendor` —— vendor 工具链
- `docs` —— 文档
- `build` —— 打包/CI/依赖
- `refactor` —— 纯结构调整无行为变化
- `fix` —— bug 修复

正文（可选）补充"为什么"（不是"做了什么"，diff 已经说了）。示例：

```
gui: split Api class into per-domain mixins

The 2000-line Api class in gui.py made it impossible to find where a
given window.pywebview.api method was implemented. Mixins keep the
external contract identical (verified against api.js call sites) while
making each domain ~200 LOC.
```

### PR 规范

任何**非微小修改**都需要：

1. 改了不可变契约（§3）？→ PR 描述里必须有 `## Contract Change` 章节。
2. 新增了 `Check`？→ 列出 `id, name, order, needs_browser`，并在 `argusai/checks/__init__.py` import。
3. 加了 `_tools.py` 工具？→ 必须明示风险级别（见源码 `tools.py` / streaming 确认集合）。
4. 加了 `skills/<name>/SKILL.md`？→ 必须有 `description:` 前置字段、触发词，单文件 < 300 行。
5. 改了 `webui/src/api.js`？→ 同 PR 必须有对应的 Python 端方法。

### 不要做的事

- ❌ **不要** `git push --force` 到 `main`/`master`。
- ❌ **不要** 直接在 `vendor/` 下改第三方代码。要改就 fork、写适配层、在 `vendor_runtime.py` 里说明。
- ❌ **不要** 把 `tasks/`、`sessions/`、`config/`、`data/` 等 **运行时产物** 提交进 git。这些已经在 `.gitignore` 里。
- ❌ **不要** 在 `Check` 里写默认 5 秒 sleep 之类的破坏性 payload。所有探测必须**默认安全**。

## 6. 安全总原则（**所有 Check / Tool 必须遵守**）

源头是 `README.md` 的 "Safety Notes" 章节，这里复述并扩展：

1. **默认非破坏**：所有 payload 必须是探针式（错误回显、`SLEEP(3)`、`uid=`），不允许 `DROP TABLE`、`xp_cmdshell`、`rm -rf` 等。
2. **不越 scope**：每次 HTTP 请求前用 `Target.in_scope(url)` 校验。
3. **限速**：`Context` 内置 `semaphore` 控制并发，新 Check 直接用 `await ctx.client.get(...)`，禁止自己开 raw `socket`。
4. **AI 工具的二次确认**：`NO_AUTO_APPROVE_TOOLS` 里的工具，**即使**用户开了"全部自动允许"也必须再次弹窗。新加高危工具，必须加入这个白名单。
5. **浏览器隔离**：除非用户显式 "attach"，否则 Check 用 Playwright 必须走 `ctx.browser_context()` 的**独立 context**，禁止共享 cookie。
6. **Secret 信息**：API key / 私钥 / OAuth secret 只能写在用户级 `settings.json`，绝不允许 hardcoded。

## 7. AI 代理使用规则

当你（AI 代理）在这个仓库工作时：

- **先读** `AGENTS.md` 与 `README`；本地若有 `docs/`，再读其中的架构/规范文档。
- **改 Agent runtime（assistant、工具、确认、上下文、UI blocks…）前**：对照已有源码契约与本地手册（若有）；不要凭空破坏确认分层与证据链。
- **不要**在没读过相关 mixin / Check / Tool 源码之前修改它们。`gui/` 现在是多文件拼装，必须知道每个文件的边界。
- **每改一个公开方法名**前，先 grep 它在 `webui/src/api.js`、工具注册与 assistant 中是否被引用。
- **每加一个新文件**前，确认有没有现成模块可以放，避免目录爆炸。
- **生成测试**时优先放在 `tests/<对应模块>/test_<功能>.py`，使用 `pytest`，禁用网络（用 `respx` 或本地 HTTP fixture）。

## 8. 相关文档索引

- 项目协议与第三方声明 → `LICENSE`（GPL-3.0-only）/ `THIRD_PARTY_NOTICES.md`
- 产品能力与安装 → `README.md`（中文）/ `README.en.md`
- 公开使用手册与 Wiki → `wiki/Home.md`；维护和导出 → `wiki/Wiki-Maintenance.md`
- 安全工作流（运行时技能） → `argusai/skills/security-workflow/SKILL.md`
- 报告模板 → `argusai/report/templates/report.html.j2`
- 本地补充（不发布）→ `docs/`（Loom22、架构、ADR、CODE-STYLE 等，若存在）
