# Wiki 维护与同步

[文档首页](Home.md) · [构建与发布](Build-and-Release.md)

仓库的 `wiki/` 保存公开用户文档源稿，与代码一同评审。`docs/` 被 `.gitignore` 忽略，留给本地工程补充。新增 `wiki/` 是为了让用户手册可以随版本跟踪，同时支持之后同步到 GitHub Wiki。

## 本地阅读与检查

入口是 [Home.md](Home.md)。正文页面使用 Markdown 相对链接，例如 `[快速开始](Quick-Start.md)`；引用源码使用 `../argusai/...`。保持链接实际存在，不使用还没有发布的页面作为唯一入口。

```sh
python scripts/wiki_docs.py check
```

检查范围包括中英文 README、品牌资源说明和 `wiki/` 下的 Markdown：本地文件、页面锚点、代码围栏和 HTML 图片引用。它不访问外部网站，也不替代命令签名与内容核对。

## 准备 GitHub Wiki 文件

GitHub Wiki 是独立的 Git 仓库，直接推送应用源码不会更新 Wiki 页签。可以先在本地导出：

```sh
python scripts/wiki_docs.py export --output-dir build/wiki-export
```

导出器把源稿的 `.md` 页面链接转换为 Wiki 页面地址，把指向应用源码的链接转换为主仓库链接，并保留 `_Sidebar.md`、`_Footer.md`。导出目录默认为新的独立目录；如果已存在，工具会停止，避免覆盖手动编辑的内容。

默认源码链接对应 `https://github.com/HG-ha/Argusai` 的 `main` 分支。需要对齐一个发布标签时：

```sh
python scripts/wiki_docs.py export --output-dir build/wiki-v0.1.4 --ref v0.1.4
```

先检查导出结果，尤其是导航、源码链接和图片路径。导出操作不创建远端页面、不执行 Git 提交，也不推送。

## 经确认后同步到远端

准备发布 Wiki 时，先在 GitHub 仓库启用 Wiki 并创建初始页面，再按 [GitHub 官方说明](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages) 克隆：

```sh
git clone https://github.com/HG-ha/Argusai.wiki.git
```

把已核对的导出 Markdown 复制到这个独立 Wiki 工作区，检查差异，再在该目录中提交和推送。不要把应用仓库的 `.git` 或运行时数据复制进去。

本轮文档工作只准备源稿和本地导出，不执行上述远端同步。以后更新命令、行为或页面目录时，先修改主仓库源稿、运行检查，再重新导出，避免在两个位置维护出不同版本。

## 页面组织

- `Home.md`：阅读入口与文档地图。
- 功能页面：先写使用目标和前提，再给步骤、预期结果、问题处理和相关页面。
- `_Sidebar.md`：导航目录，与文档地图保持一致。
- `_Footer.md`：项目和公司入口。

文档中的示例应能在当前版本的 `--help` 或源码中找到依据。描述企业场景时，区分操作示例与真实客户案例；描述持续运行时，说明触发、状态保存、停止条件和宿主在线要求。
