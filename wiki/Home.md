# ArgusAI 使用文档

[项目首页](../README.md) · [English introduction](../README.en.md) · [项目仓库](https://github.com/HG-ha/Argusai) · [达瓦科技](https://dawahezhi.com/)

这里是 ArgusAI `0.1.3` 的用户指南。**把目标交给 Agent，让它在 ArgusAI 中完成工作。** 扫描、代理、浏览器、逆向、知识与定时任务围绕同一个 Agent 工作流组织：你说明目标和范围，Agent 选择工具、推进步骤、保存证据并交付结果。

从应用内的悬浮球唤出助手，或进入 **Agent 对话** 管理完整会话。Agent 能结合当前页面、任务和工作状态继续协助；各功能页面用于观察执行、核对证据和需要时介入。两种入口的用法见 [Agent 与会话](Agent-and-Sessions.md)。

> **先完成一个小任务**
> 配置模型后，从悬浮球或完整对话发出第一项任务：“分析我提供的 HAR、JS 文件或脱敏报告，整理关键线索、证据来源与下一步待办。”让 Agent 组织工具和产物，再到相应页面检查结果。

## 入门路线

| 你现在的情况 | 从这里开始 | 完成后应得到什么 |
| --- | --- | --- |
| 第一次使用 | [快速开始](Quick-Start.md) | 能启动、能对话、能找到第一份任务结果 |
| 需要安装或配置环境 | [安装与环境](Installation.md) | 明确桌面依赖、模型端点、浏览器和工具路径 |
| 想从终端工作 | [命令行参考](CLI-Reference.md) | 能选择合适的命令，运行扫描、对话或后台服务 |
| 一个复杂任务需要连续运行数十小时 | [单轮长时间执行](Agent-and-Sessions.md#单轮长时间执行与三种持续方式) | 能配置时长、组织待办与检查点，并跟踪阶段证据 |
| 想持续巡检或续跑 | [自动化与持续执行](Automation.md) | 能选择触发方式、执行对话、授权和通知策略 |

## 按工作场景阅读

### 对话、计划与协作

[Agent 与会话](Agent-and-Sessions.md) 介绍模型选择、当前对话的自动审批、计划、上下文、会话沙盒、小队与证据，也说明如何组织数十小时级的单轮自主任务。单轮持续执行、后台工具任务和 Cron 跨轮续跑是三种可组合的方式。设置页的默认值用于新对话；已有对话保留各自的模型和审批状态。

### 应用审计与流量排查

[Web 审计与代理](Web-Audit-and-Proxy.md) 说明如何建立扫描任务、使用浏览器、导入已有流量、设置断点和导出结果。先区分离线材料分析、在线探针和主动流量改写，再决定本次工作允许哪些操作。

### 样本分析与外部工具

[逆向工作台](Reverse-Workbench.md) 按 JS、移动应用和原生二进制说明输入、ReverseJob、工具链依赖与结果位置。[集成与远程访问](Integrations.md) 介绍 MCP、ACP 和通信渠道。

### 内网防护、应用防护与持续攻防

[企业工作流](Enterprise-Workflows.md) 按内网安全防护、应用安全防护、攻击溯源和持续攻防组织操作路线：从资产、流量、日志或样本出发，经过检查与验证，产出基线差异、事件时间线、证据和整改复测结果。[自动化](Automation.md) 进一步说明如何安排主动巡检、变化触发、长期调查与持续续跑。有关数据位置、模型请求和授权，请阅读[数据与执行边界](Data-and-Safety.md)。

## 文档地图

| 类别 | 页面 |
| --- | --- |
| 开始使用 | [快速开始](Quick-Start.md) · [安装与环境](Installation.md) |
| 日常工作 | [Agent 与会话](Agent-and-Sessions.md) · [Web 审计与代理](Web-Audit-and-Proxy.md) · [逆向工作台](Reverse-Workbench.md) |
| 持续运行 | [自动化](Automation.md) · [集成](Integrations.md) · [企业工作流](Enterprise-Workflows.md) |
| 终端与维护 | [CLI 参考](CLI-Reference.md) · [故障排查](Troubleshooting.md) |
| 开发与分发 | [架构与开发](Architecture-and-Development.md) · [构建与发布](Build-and-Release.md) · [Wiki 维护](Wiki-Maintenance.md) |
| 数据与权限 | [数据与执行边界](Data-and-Safety.md) |

本文档当前以中文为主。英文产品介绍与运行入口见 [README.en.md](../README.en.md)。源码版本、运行环境和外部工具不同，具体命令以对应子命令的 `--help` 为准。

## 文档与 GitHub Wiki

`wiki/` 是随源码评审和维护的文档源稿，单独存在是因为仓库的 `docs/` 留给被忽略的本地工程材料。当前文件可直接在仓库中阅读；同步到 GitHub 的 Wiki 页签需要另外处理独立的 Wiki 仓库，步骤见 [Wiki 维护](Wiki-Maintenance.md)。
