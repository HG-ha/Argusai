# 安装与环境准备

[Wiki 首页](Home.md) · [快速开始](Quick-Start.md) · [构建发行包](Build-and-Release.md)

发行包适合直接使用桌面工作台；源码环境适合开发、调试和脚本化使用。当前版本为 `0.1.2`。不同功能依赖可以分开准备，不必在第一次启动前装齐全部逆向工具。

## 发行包

公开版本发布后，从 [GitHub Releases](https://github.com/HG-ha/Argusai/releases) 下载与你的系统和架构匹配的产物及 `.sha256` 文件。

| 平台 | 自动发布产物 | 额外运行环境 |
| --- | --- | --- |
| Windows x64 | `ArgusAI-0.1.2-win-x64-setup.exe` | pywebview 使用的 WebView2 Runtime |
| Windows ARM64 | `ArgusAI-0.1.2-win-arm64-setup.exe` | 原生 WebView2 Runtime 与 Microsoft .NET Desktop Runtime |
| macOS Apple Silicon | `ArgusAI-0.1.2-macos-arm64.dmg` | 运行于对应 macOS 环境；桌面自动化另需权限 |
| Linux x64 | `ArgusAI-0.1.2-linux-x64.tar.gz` | GTK、WebKitGTK 等桌面运行库 |
| Linux ARM64 | `ArgusAI-0.1.2-linux-arm64.tar.gz` | 对应 ARM64 发行版的 GTK、WebKitGTK 等桌面运行库 |

这些是自动工作流目前配置的发布平台。其他架构应在对应环境中从源码运行或自行构建，不能仅修改文件名来转换架构。

### 校验下载文件

将产物与对应 `.sha256` 放在同一目录。校验用于确认文件与公布摘要一致。

Windows PowerShell：

```powershell
Get-FileHash .\ArgusAI-0.1.2-win-x64-setup.exe -Algorithm SHA256
Get-Content .\ArgusAI-0.1.2-win-x64-setup.exe.sha256
```

比较两者的哈希，不要把摘要文件中的文件名也纳入比较。

Linux：

```sh
sha256sum -c ArgusAI-0.1.2-linux-x64.tar.gz.sha256
```

macOS：

```sh
shasum -a 256 -c ArgusAI-0.1.2-macos-arm64.dmg.sha256
```

### Windows

运行与系统架构匹配的 NSIS 安装包，按安装器完成安装，然后从开始菜单或桌面入口打开。缺少 WebView2 Runtime 时，先安装对应架构的运行时再启动。Windows ARM64 还需要原生 Microsoft .NET Desktop Runtime；缺失时 WinForms 桥接无法初始化。

企业设备可能对未知程序、脚本或浏览器运行时有限制。按组织的软件准入流程处理，保留被拦截的错误和版本信息用于排查。

### macOS

打开 DMG，将应用拖到 Applications。当前 CI 的 macOS 产物使用临时签名，没有 Developer ID 公证；Gatekeeper 可能限制首次运行，应按系统提示和组织要求核实来源。自行分发的签名与公证流程见[构建与发布](Build-and-Release.md)。

需要桌面观察或输入自动化时，在系统设置中授予对应的辅助功能、屏幕录制权限。只做离线文件分析时，可以先不启用这些能力。

### Linux

解压 tar.gz 后进入版本目录，启动桌面：

```sh
tar -xzf ArgusAI-0.1.2-linux-x64.tar.gz
cd ArgusAI-0.1.2-linux-x64
./ArgusAI --gui
```

ARM64 系统将文件名和目录名中的 `linux-x64` 替换为 `linux-arm64`。

交互终端中直接执行 `./ArgusAI` 会进入 ops 控制台。Linux 桌面依赖按发行版准备；Ubuntu 示例：

```sh
sudo apt-get install libgtk-3-dev libwebkit2gtk-4.1-dev
```

应在实际目标发行版上验证兼容性；缺少共享库时记录错误，不要用不同架构或其他发行版的二进制文件替代缺失库。 GTK 后端还涉及 GObject introspection；源码虚拟环境的 Python 依赖见下节。

## 从源码运行

### 基础依赖

| 依赖 | 用途 |
| --- | --- |
| Python 3.12+ | 应用与 Python 内核；发布 CI 使用 Python 3.12 |
| uv | 安装锁定依赖、运行项目命令 |
| Node.js 18+ 和 npm | 安装与构建 Vue 前端；发布 CI 使用 Node.js 22 |
| 系统桌面运行库 | WebView2、macOS 系统组件或 Linux GTK/WebKitGTK |

从仓库根目录执行以下命令。命令在 PowerShell 和常见 Unix shell 中均可使用：

```sh
git clone https://github.com/HG-ha/Argusai.git
cd Argusai
uv sync --locked
npm --prefix argusai/webui ci
npm --prefix argusai/webui run build
uv run argusai --gui
```

`uv sync --locked` 使用现有锁文件，不会静默替你重新选择依赖版本。前端必须先构建，pywebview 才能加载 `argusai/webui/dist`。

要修改前端代码，可以另开终端启动 Vite：

```sh
npm --prefix argusai/webui run dev
```

Vite 页面用于前端开发；需要真实 Python 桥接的功能仍依赖桌面宿主。发布构建使用 `npm run build`。

### Linux 的 Python 图形后端

pywebview 在 Linux 需要可用的 GTK 或 Qt 后端。GTK 使用 PyGObject；系统安装 GTK/WebKitGTK 库，与项目 Python 能否导入 `gi` 是两项检查。使用独立 Python 或 uv 虚拟环境时尤其要核对。

如果选择 GTK，可在运行应用的同一环境进行自检：

```sh
uv run python -c "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('WebKit2', '4.1'); from gi.repository import Gtk, WebKit2; print('GTK/WebKitGTK available')"
```

若报 `No module named gi` 或缺少 typelib，按 [pywebview 官方 Linux 安装说明](https://pywebview.flowrl.com/guide/installation.html)与发行版的 PyGObject 配置补齐 Python 后端、introspection 包和原生依赖，并再次自检。安装系统 `python3-gi` 后，也需确认当前应用解释器能够访问它。CLI 和纯文件分析的依赖范围与桌面后端不同。

### 验证安装

```sh
uv run argusai version --json
uv run argusai --help
uv run argusai doctor
```

预期版本为当前源码版本，帮助列出 CLI 命令，诊断输出指出已配置与缺失的环境。诊断发现可选工具缺失时，先判断是否需要该功能。

## 首次配置

### AI 端点

在设置页填写 AI 服务地址、协议、模型与凭据。先测试连接，再用一个非敏感短消息测试对话。凭据只保存在用户级配置中，不要放到仓库、截图或问题报告。

CLI 提供 `ai providers`、`ai list`、`ai add` 等命令；涉及密钥时建议通过设置页输入，避免出现在 shell 历史和进程参数中。用法见[CLI 参考](CLI-Reference.md)。

### 数据目录

选择用于任务、会话、证据、样本和知识库的本地数据目录。历史记录与目录绑定，换用户环境、改变目录或用不同配置启动，都可能看到不同的记录。

需要迁移时使用软件提供的数据目录迁移流程；不要在程序运行时手动搬运数据或日志。目录、凭据和留存边界见[数据与安全](Data-and-Safety.md)。

### 浏览器与外部工具

浏览器功能默认探测系统 Chrome。Playwright 的驱动和浏览器环境应按诊断与设置提示准备，不需要为了启动界面就安装所有浏览器。

按计划使用的功能准备外部工具：

| 功能 | 常用环境 |
| --- | --- |
| JS/WASM 分析 | 按需安装的 Node.js、JS 工具或 WABT |
| Android 分析 | JADX、APKTool；动态操作需要 adb 可见的已连接设备 |
| 原生二进制分析 | Ghidra、capa、yara；动态操作按需使用 Frida |
| 外部扫描 | 所需的 nuclei、ffuf、sqlmap 等工具及其授权范围 |
| 桌面自动化 | 对应 OS 能力与权限 |

工具可以通过 PATH 或设置中的路径发现。相关操作见[逆向工作台](Reverse-Workbench.md)与[集成](Integrations.md)。

## 离线与企业内网

应用本地保存工作资料，但模型请求会发往配置的端点，搜索、下载、通信渠道和带外验证也可能联网。

内网部署前准备依赖与模型服务，明确允许访问的域名和工具来源。按需运行时不能在完全离线时首次下载；已有运行时也应先检查版本和健康状态。企业使用建议先用脱敏材料验证，参见[企业工作流](Enterprise-Workflows.md)。

## 常见安装问题

| 现象 | 排查方向 |
| --- | --- |
| 找不到 `argusai` | 源码环境使用 `uv run argusai`；确认工作目录含 `pyproject.toml` |
| 找不到 `webui/dist/index.html` | 安装 npm 依赖并执行前端 build |
| `uv sync --locked` 报锁文件不一致 | 核对分支与文件是否完整；开发者更新依赖后应同步锁文件 |
| GUI 空白或 WebView 初始化失败 | WebView2 / WebKitGTK、系统 WebView、图形环境与错误日志 |
| 模型测试失败 | 地址、协议、模型、凭据、网络与服务端错误 |
| 设备列表为空 | 在外部先验证 adb 连接；软件不会自动启动模拟器 |

更多问题见[故障排查](Troubleshooting.md)。
