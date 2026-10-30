# ArgusAI website

`website/` 是 `argusai.sh` 的静态官网源码，与桌面端 `argusai/webui` 独立。

## 本地预览

```sh
python -m http.server 4173 --directory website
```

然后访问 `http://127.0.0.1:4173`。

## Cloudflare Pages

- Framework preset: `None`
- Build command: 留空
- Build output directory: `website`
- Root directory: `/`

Pages 会直接发布 `website/`。自定义域名使用 `argusai.sh`，可将 `www.argusai.sh` 重定向到根域名。

## 页面与主题

官网支持简体中文与英文。导航栏的语言按钮可直接切换，偏好保存在浏览器中，跨页面保留；首次访问按浏览器语言选择。也可用 `?lang=zh` 或 `?lang=en` 指定语言。翻译集中在 `i18n.js`，覆盖页面正文、元信息、图集、无障碍标签及动态工作流；截图保留真实软件界面的语言。

- `index.html`：首页，Agent 任务入口、真实工具执行、下载与能力导航。
- `product.html`：Agent 流量分析、沙盒、逆向、协作与持续任务；可切换工作流与真实产物预览。
- `scenarios.html`：使用场景与本地部署结构图。
- `changelog.html`：版本更新日志，内容依据 GitHub Releases 维护；日期使用北京时间。

页面共享 `styles.css`、`theme.js` 和 `app.js`，无需构建。默认浅色，可选择深色或跟随系统，偏好跨页面保留。截图使用浅色、深色两组真实素材，跟随网站主题切换；说明图使用同一套颜色变量。首页、工作台和使用场景页以四图画廊展示 Agent、沙盒、扫描与模型配置；点击打开原生 dialog 大图预览，支持左右方向键、前后切换、Esc 与背景关闭，并恢复焦点和页面滚动。

## 截图来源与隐私

`assets/screens/scan-{light,dark}.webp` 和 `models-{light,dark}.webp` 来自本机 ArgusAI v0.1.2 实际运行界面，通过 macOS `screencapture` 截取完整窗口，保留原生红黄绿按钮；未拼接或绘制标题栏。使用已有 DeepSeek 官方端点，使用独立演示数据目录，拍摄扫描配置和模型设置；未运行扫描或读取业务文件。拍摄前备份配置，拍摄后完整恢复。

新增的 agent-{light,dark}.webp、sandbox-{light,dark}.webp 来自本机开发版本的独立演示会话。Agent 实际调用沙盒写入与读取工具，生成并核验 demo-report.md；没有执行扫描。桌面权限提示已改为当前轮涉及桌面工具时才出现。

`assets/screens/cli-{light,dark}.webp` 来自当前 v0.1.3 源码在 macOS 上运行的真实 CLI 会话：使用 168 列 × 46 行的 PTY，实际输入 `/sidebar on`、`/monitor both`、`/status`、`/file list`，捕获 ANSI 输出，由终端单元缓冲区忠实渲染为图片。图片是终端会话渲染，不是 macOS 窗口截屏；未拼造 Agent 回复、工具执行或业务结果。左右栏与主界面均是应用真实输出。演示使用独立临时数据目录和 `demo-cli` 会话，没有配置模型端点、读取用户会话或执行业务任务。截图中的三个文件由拍摄脚本人工准备，内容明确注明用于演示；`/file list` 读取的是这些真实存在的文件，不是伪造的工具执行结果。CLI 当前使用固定深色 night-ops 配色，官网明暗主题引用同一真实终端画面，不代表 CLI 提供浅色主题。大图 3020 × 1904，缩略图宽 960；录制脚本与原始 ANSI 仅保留在本地忽略目录中。

本轮视觉采用 Agent 工作过程与真实界面主导，保留品牌图标、分页面和双主题；设计参考 Impeccable 的 hierarchy、craft-floor 与 redesign 指导。

官网产品能力描述来自仓库 `README.md`。不要将包含私有端点、账号、路径、访问记录或凭据的截图加入公开静态资源。配置备份和演示数据必须保存在官网及 Git 仓库之外。

v0.1.3 更新日志已补录；从 v0.1.4 开始，更新日志中的生成区域由发布流程维护。较早版本的历史内容仍保留；不要根据未发布的提交提前修改稳定版清单。

## 版本清单与发布同步

桌面客户端从 `https://argusai.sh/updates/stable.json` 检查更新，安装包直接从对应 GitHub Release 下载。每份清单包含版本、实际发布时间、发布说明、五个平台的固定版本下载地址、文件大小和 SHA-256；源码包仍在 Release 提供。`updates/releases/<版本>.json` 保存发布后的不可变记录。

`Release packages` 工作流在五个平台打包、源码归档和 Release 上传均成功后，才运行官网同步任务。它重新核验正式 Release、Git tag 的源提交、六个包与各自的校验文件，以及发布说明是否与源提交一致；随后在最新 `main` 上生成清单和更新日志，采用普通快进推送。主分支同期有新提交时会重新生成，稳定版不能回退，已发布版本的清单不能原地替换。

官网继续使用现有 Pages Git 集成。也可在 Pages 控制台为 `main` 创建 Deploy Hook，把完整 URL 保存到仓库 Actions Secret `CLOUDFLARE_PAGES_DEPLOY_HOOK`；发布流程会在提交官网变更后主动触发它。Hook URL 是凭据，不要写入源码。该方式不需要向客户端提供 GitHub Token 或 CF 凭据。

最后一步会轮询公网清单，确认部署内容与已验证 Release 一致。若 Git 集成未触发或部署失败，该步骤会失败，提示检查 Pages，而不会把“已提交源码”视为“已上线”。GitHub Token 产生的事件不会递归触发普通 Actions 工作流；Pages 是独立的 Git 集成，仍需以上线核验为准。

本地维护或补同步一个已经完整发布的版本：

```sh
python scripts/release_feed.py --tag v0.1.3 --commit 2d8acd26bbd37afbb52e2d5150e12e3d3fb9aab9
python scripts/release_feed.py --tag v0.1.3 --commit 2d8acd26bbd37afbb52e2d5150e12e3d3fb9aab9 --verify-live
```

第一条只生成本地文件，不会推送、部署或修改 Release。CI 通过环境变量 `GH_TOKEN` 读取仓库 API；本地公开仓库也可不提供 Token。更新说明沿用 Release 原文，英文翻译集中维护在 `i18n.js`，没有翻译的条目保留原文。稳定版清单缓存 60 秒，每个历史版本的清单长期缓存；客户端不访问 GitHub Release API。

## 图片交付

手机端使用折叠导航、单按钮主题轮换、横向吸附图集与全屏预览。预览支持左右滑动切图、放大后平移阅读；工作流标签在窄屏下完整排列为两列。资源引用包含版本号，发布后避免旧脚本与新页面混用。

截图大图使用 WebP（质量 90，保留原始尺寸）；列表使用宽度不超过 960 像素的独立缩略图。只有打开预览时才载入对应大图。JPEG 原始文件保留在本机官网目录以外，不参与发布。
