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

本轮视觉采用 Agent 工作过程与真实界面主导，保留品牌图标、分页面和双主题；设计参考 Impeccable 的 hierarchy、craft-floor 与 redesign 指导。

官网产品能力描述来自仓库 `README.md`。不要将包含私有端点、账号、路径、访问记录或凭据的截图加入公开静态资源。配置备份和演示数据必须保存在官网及 Git 仓库之外。

更新版本时编辑 `changelog.html`，填写实际发布日期、摘要和对应 Release 链接，不根据未发布的提交推测版本内容。

## 图片交付

截图大图使用 WebP（质量 90，保留原始尺寸）；列表使用宽度不超过 960 像素的独立缩略图。只有打开预览时才载入对应大图。JPEG 原始文件保留在本机官网目录以外，不参与发布。
