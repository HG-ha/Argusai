"""CLI locale for ops-console slash commands and Typer help.

Reuses the channel i18n catalog so ``/lang zh|en|both`` actually changes
``/status``, ``/help``, ``/model``, and other local command copy.

Default locale follows the OS (``ARGUSAI_LANG``, Windows UI language,
macOS ``AppleLanguages``, then POSIX ``LANG``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from argusai.core.channels.i18n import (
    BILINGUAL,
    BILINGUAL_PRIMARY,
    DEFAULT_LOCALE,
    channel_text,
    detect_system_locale,
    normalize_locale,
)

_CLI_LOCALE = "en"
_LOCALE_BOOTSTRAPPED = False

# Labels that only exist in the CLI ops console (not channel cards).
_CLI_LABELS: dict[str, dict[str, str]] = {
    "system_section": {"en": "── system ──", "zh": "── 系统 ──"},
    "knowledge_section": {"en": "── knowledge ──", "zh": "── 知识库 ──"},
    "label_ai": {"en": "AI", "zh": "AI"},
    "label_proxy": {"en": "Proxy", "zh": "代理"},
    "label_tasks": {"en": "Tasks", "zh": "任务"},
    "label_checks": {"en": "Checks", "zh": "检查"},
    "tasks_line": {
        "en": "running={running}  total={total}",
        "zh": "进行中={running}  总计={total}",
    },
    "proxy_on": {"en": "on", "zh": "运行中"},
    "proxy_off": {"en": "off", "zh": "未运行"},
    "session_closed": {"en": "session closed", "zh": "会话已关闭"},
    "send_help": {
        "en": "send /help for the command map",
        "zh": "发送 /help 查看命令",
    },
    "unknown_cmd": {"en": "unknown command  {cmd}", "zh": "未知命令  {cmd}"},
    "cli_extra_clear": {"en": "redraw ops HUD", "zh": "重绘控制台抬头"},
    "cli_extra_session": {"en": "show current session id", "zh": "显示当前会话 ID"},
    "cli_extra_mention": {
        "en": "mention a team member while a room is open",
        "zh": "小队房间里提及成员",
    },
    "cli_extra_attachment": {
        "en": "fuzzy-find files under the launch directory; Tab completes (startup: --file)",
        "zh": "从启动目录模糊查找文件；Tab 补全（启动参数：--file）",
    },
    "cli_extra_greenlight_target": {
        "en": "enable Greenlight, then run the objective (GUI review-bypass equivalent)",
        "zh": "开启绿灯模式并立即执行目标（等同 GUI「审查绕过」）",
    },
    "cli_extra_tasks": {"en": "list scan tasks", "zh": "列出扫描任务"},
    "cli_extra_quit": {"en": "leave console", "zh": "退出控制台"},
    "cli_extra_team": {"en": "list standing teams", "zh": "列出常驻小队"},
    "cli_extra_team_use": {"en": "enter a team room; later lines go there", "zh": "进入小队房间，之后的输入发给小队"},
    "cli_extra_team_leave": {"en": "leave the team room", "zh": "离开小队房间，回到主 Agent"},
    "cli_extra_team_greenlight": {
        "en": "team shield: stop asking whether the work is allowed",
        "zh": "小队授权：不再询问能不能做，工具确认仍保留",
    },
    "team_room_empty": {
        "en": "No teams yet. Create one with argusai team create, then /team use <name>.",
        "zh": "还没有小队。先用 argusai team create 建队，再 /team use <名称>。",
    },
    "team_room_list_title": {"en": "Teams", "zh": "小队"},
    "team_room_list_hint": {
        "en": "/team use <number|name> enters the room; later lines go there. /team leave returns to the main Agent.",
        "zh": "/team use <编号|名称> 进入群聊，之后直接打字。/team leave 回到主 Agent。",
    },
    "team_room_left": {"en": "Back to the main Agent", "zh": "已回到主 Agent"},
    "team_room_usage": {
        "en": "Usage: /team use <number|name>   or   /team pm <number|name> <member>",
        "zh": "用法：/team use <编号|名称>  或  /team pm <编号|名称> <成员>",
    },
    "team_room_pm_usage": {
        "en": "Usage: /team pm <team> <member>",
        "zh": "用法：/team pm <小队> <成员>",
    },
    "team_room_where_group": {"en": "group", "zh": "群聊"},
    "team_room_where_private": {"en": "private @{role}", "zh": "私聊 @{role}"},
    "team_room_entered": {
        "en": "Entered team {where}",
        "zh": "进入小队{where}",
    },
    "team_room_no_messages": {
        "en": "No messages yet. Type to send one here.",
        "zh": "还没有消息。直接输入就会发给这里。",
    },
    "team_room_footer": {
        "en": "/team leave returns to the main Agent · /greenlight on authorizes the team · /team stop halts replies",
        "zh": "/team leave 回到主 Agent · /greenlight on 打开小队授权 · /team stop 终止回复",
    },
    "team_room_need_use": {
        "en": "Use /team use <team> first. Later lines refresh this room.",
        "zh": "先 /team use <小队>，之后的输入会在这个房间里刷新。",
    },
    "team_room_stopped": {"en": "Stopped {count} member(s)", "zh": "已终止 {count} 名成员"},
    "team_room_stop_usage": {
        "en": "Usage: /team stop <team>, or /team use first and then /team stop",
        "zh": "用法：/team stop <小队>，或先 /team use 再 /team stop",
    },
    "team_room_help": {
        "en": "Usage: /team · /team use <team> · /team pm <team> <member> · /team leave · /team stop",
        "zh": "用法：/team  ·  /team use <小队>  ·  /team pm <小队> <成员>  ·  /team leave  ·  /team stop",
    },
    "team_room_you": {"en": "you", "zh": "你"},
    "team_room_sent": {
        "en": "Sent to {count} member(s). Waiting for replies…",
        "zh": "已发给 {count} 名成员，等待回复…",
    },
    "team_room_greenlight_on": {
        "en": "Team authorization is on. Members stop asking whether the work is allowed; tool confirms stay.",
        "zh": "小队授权已打开。成员不再询问能不能做，工具执行仍会确认。",
    },
    "team_room_greenlight_off": {
        "en": "Team authorization is off.",
        "zh": "小队授权已关闭。",
    },
    "team_room_greenlight_mark": {"en": "auth", "zh": "授权"},
    "team_rail_title": {"en": "TEAM", "zh": "小队"},
    "team_rail_group": {"en": "group", "zh": "群聊"},
    "team_rail_private": {"en": "private @{role}", "zh": "私聊 @{role}"},
    "team_rail_leave": {"en": "/team leave  main Agent", "zh": "/team leave  回主 Agent"},
    "team_rail_quit": {"en": "/quit  leave console", "zh": "/quit  退出控制台"},
    "team_rail_stop": {"en": "/team stop  halt replies", "zh": "/team stop  终止回复"},
    "team_rail_pm": {"en": "/team pm <role>", "zh": "/team pm <角色>"},
    "team_rail_at": {"en": "@role  mention a member", "zh": "@角色  提及成员"},
    "team_mention_meta": {"en": "member", "zh": "成员"},
    "team_room_greenlight_usage": {
        "en": "Usage: /greenlight on|off|status inside a room, or /team greenlight on|off <team>",
        "zh": "用法：房间里 /greenlight on|off|status，或 /team greenlight on|off <小队>",
    },
    "tasks_title": {
        "en": "Tasks · page {page}/{page_count} · {total} total",
        "zh": "任务列表 · 第 {page}/{page_count} 页 · 共 {total} 个",
    },
    "tasks_empty": {"en": "(no tasks)", "zh": "（暂无任务）"},
    "tasks_usage": {"en": "Usage: /tasks [page]", "zh": "用法：/tasks [页码]"},
    "tasks_hint": {
        "en": "Send /tasks <page> to flip pages.",
        "zh": "发送 /tasks <页码> 翻页。",
    },
    "tasks_row": {
        "en": "{ordinal}. [{status}] findings={findings}  {url}",
        "zh": "{ordinal}. [{status}] 发现={findings}  {url}",
    },
    "history_hint": {
        "en": "↑↓ recall input history",
        "zh": "↑↓ 翻看输入历史",
    },
    "cli_group": {"en": "CLI", "zh": "本机控制台"},
    "tools_registered": {
        "en": "Tools registered: {count}",
        "zh": "已注册工具：{count}",
    },
    "tools_pending_count": {
        "en": "Pending confirms: {count}",
        "zh": "待确认：{count}",
    },
    "tools_pending_hint": {
        "en": "Use /allow or answer the next AUTH prompt.",
        "zh": "发送 /allow，或在下次 AUTH 提示中确认。",
    },
    "tools_risk": {"en": "risk={risk}", "zh": "风险={risk}"},
    "cancel_ok": {"en": "Cancelled the active run.", "zh": "已取消当前运行。"},
    "cancel_fail": {"en": "Cancel failed.", "zh": "取消失败。"},
    "sessions_empty": {"en": "(empty)", "zh": "（空）"},
    "already_on_session": {
        "en": "Already on {session_id}.",
        "zh": "当前已是 {session_id}。",
    },
    "switch_failed": {"en": "Switch failed.", "zh": "切换失败。"},
    "model_switch_ask": {
        "en": "▸ AUTH  switch model to {name}?",
        "zh": "▸ AUTH  切换模型到 {name}？",
    },
    "model_switch_aborted": {
        "en": "Switch aborted.",
        "zh": "已取消切换。",
    },
    "option_on": {"en": "on", "zh": "开"},
    "option_off": {"en": "off", "zh": "关"},
    "file_title": {
        "en": "Files  {total}  ·  page {page}/{pages}",
        "zh": "文件  {total}  ·  第 {page}/{pages} 页",
    },
    "file_title_query": {
        "en": "Files  {total}  ·  page {page}/{pages}  ·  search {query}",
        "zh": "文件  {total}  ·  第 {page}/{pages} 页  ·  搜索 {query}",
    },
    "file_hint": {
        "en": "/file <number|path> to resolve a sandbox file",
        "zh": "发送 /file <编号|路径> 查看沙盒文件",
    },
    "file_meta_name": {"en": "file", "zh": "文件"},
    "file_meta_path": {"en": "path", "zh": "相对路径"},
    "file_meta_abs": {"en": "abs", "zh": "绝对路径"},
    "file_meta_size": {"en": "size", "zh": "大小"},
    "file_meta_sha": {"en": "sha256", "zh": "sha256"},
    "file_meta_mime": {"en": "mime", "zh": "类型"},
    "file_bytes": {"en": "{size} B", "zh": "{size} 字节"},
    "auth_auto_allow": {
        "en": "▸ AUTH  enable session auto-allow for all tools (incl. high-risk)?",
        "zh": "▸ AUTH  开启会话级全部工具自动允许（含高危）？",
    },
    "auto_allow_aborted": {
        "en": "Auto-allow aborted.",
        "zh": "已取消自动允许。",
    },
    "decision_forwarded": {
        "en": "Decision forwarded: {detail}",
        "zh": "已转发决定：{detail}",
    },
    "decision_allow_all": {
        "en": "allow all pending ({count})",
        "zh": "允许全部待确认（{count}）",
    },
    "decision_deny_all": {
        "en": "deny all pending ({count})",
        "zh": "拒绝全部待确认（{count}）",
    },
    "no_pending_match": {
        "en": "No pending call matches {selector}.",
        "zh": "没有匹配的待确认调用：{selector}",
    },
    "try_tools": {
        "en": "Try /tools to list call ids.",
        "zh": "发送 /tools 查看调用 ID。",
    },
    "agents_title": {"en": "Agents ({count})", "zh": "Agent 列表（{count}）"},
    "agent_unavailable_tag": {"en": "unavailable", "zh": "不可用"},
    "agents_hint": {
        "en": "/new <number|name> to start a session on that agent",
        "zh": "发送 /new <编号|名称> 用该 Agent 新建会话",
    },
    "new_session_line": {
        "en": "New session  {session_id}",
        "zh": "新会话  {session_id}",
    },
    "new_agent_line": {
        "en": "Agent        {name}",
        "zh": "Agent        {name}",
    },
    "empty_side": {"en": "(empty side reply)", "zh": "（旁问无回复）"},
    "btw_usage": {"en": "usage  /btw <question>", "zh": "用法  /btw <问题>"},
    "tag_cmd": {"en": "CMD", "zh": "命令"},
    "tag_user": {"en": "YOU", "zh": "你"},
    "cli_attachment_summary": {
        "en": "Attachments · {names}",
        "zh": "附件 · {names}",
    },
    "cli_attachment_error": {
        "en": "Attachment failed: {error}",
        "zh": "附件处理失败：{error}",
    },
    "tag_choice": {"en": "CHOICE", "zh": "选择"},
    "tag_auth": {"en": "AUTH", "zh": "确认"},
    "tag_reply": {"en": "REPLY", "zh": "回复"},
    "reply_streaming": {"en": "composing reply", "zh": "正在生成回复"},
    "tag_think": {"en": "THINK", "zh": "思考"},
    "tag_exec": {"en": "EXEC", "zh": "执行"},
    "decision_pick_hint": {
        "en": "Type a number/custom answer, or Tab to MAIN for keyboard selection:",
        "zh": "输入编号/自定义回答，或按 Tab 到 MAIN 键盘选择：",
    },
    "decision_keyboard_hint": {
        "en": "↑↓ select · Enter confirm · Tab custom input",
        "zh": "↑↓ 选择 · Enter 确认 · Tab 返回输入",
    },
    "decision_continue": {"en": "Continue", "zh": "继续"},
    "decision_cancel": {"en": "Cancel", "zh": "取消"},
    "decision_ask": {
        "en": "Your choice",
        "zh": "你的选择",
    },
    "decision_batch_ask": {
        "en": "Question {n} — pick option",
        "zh": "问题 {n} — 请选择",
    },
    "auth_allow_ask": {
        "en": "Allow {name}?",
        "zh": "允许 {name}？",
    },
    "auth_high_risk_ask": {
        "en": "Allow high-risk {name}?",
        "zh": "允许高危操作 {name}？",
    },
    "auth_high_risk_note": {
        "en": "High-risk tool — double-check before allowing.",
        "zh": "高危工具 — 允许前请再次确认。",
    },
    "auth_abort": {"en": "Turn aborted (confirmation denied).", "zh": "已中止（确认被拒绝）。"},
    "toolbar": {
        "en": " {session}  ·  {lang}  ·  PgUp/PgDn  ·  /quit ",
        "zh": " {session}  ·  {lang}  ·  PgUp/PgDn  ·  /quit ",
    },
    "ops_idle_title": {
        "en": "standing by",
        "zh": "待命中",
    },
    "ops_idle_hint": {
        "en": "type a message, or /help for the command map",
        "zh": "输入消息开始，或发送 /help 查看命令",
    },
    "ops_busy_toolbar": {
        "en": "running · /btw /cancel ok",
        "zh": "运行中 · 可用 /btw /cancel",
    },
    "ops_busy_side": {"en": "/btw /cancel", "zh": "/btw /cancel"},
    "ops_activity_model": {"en": "waiting on model", "zh": "请求模型"},
    "ops_activity_think": {"en": "thinking", "zh": "思考中"},
    "ops_activity_reply": {"en": "writing", "zh": "生成中"},
    "ops_activity_context": {"en": "linking context", "zh": "整理上下文"},
    "ops_activity_tool": {"en": "tool {name}", "zh": "工具 {name}"},
    "ops_activity_tool_generic": {"en": "running tool", "zh": "执行工具"},
    "ops_activity_round": {"en": "round {n}", "zh": "第 {n} 轮"},
    "ops_activity_resume": {"en": "resumed", "zh": "已续上"},
    "ops_activity_working": {"en": "working", "zh": "处理中"},
    "ops_busy_hint": {
        "en": "Agent is busy. Use /btw <question> for a side ask, or /cancel to stop.",
        "zh": "Agent 正在运行。侧问用 /btw <问题>，停止用 /cancel。",
    },
    "ops_rail_todo": {"en": "TODO", "zh": "TODO"},
    "ops_rail_session": {"en": "SESSION", "zh": "会话"},
    "ops_rail_sys": {"en": "SYS", "zh": "系统"},
    "ops_rail_tokens": {"en": "TOKENS", "zh": "Token"},
    "ops_rail_agents": {"en": "AGENTS", "zh": "Agent"},
    "ops_todo_empty": {"en": "(no todos)", "zh": "（无计划）"},
    "ops_tokens_empty": {"en": "(no usage yet)", "zh": "（暂无消耗）"},
    "ops_agents_empty": {"en": "(no agents)", "zh": "（无子任务）"},
    "ops_session_running": {"en": "running", "zh": "运行中"},
    "ops_session_idle": {"en": "idle", "zh": "空闲"},
    "ops_tab_hint": {
        "en": "Tab panes · MAIN ↑↓ · PgUp/PgDn · Enter allow",
        "zh": "Tab切区 · MAIN↑↓滚屏 · PgUp/PgDn · Enter授权",
    },
    "ops_choice_tab_hint": {
        "en": "Tab input/MAIN · ↑↓ select · Enter confirm",
        "zh": "Tab切换输入/MAIN · ↑↓选择 · Enter确认",
    },
    "ops_choice_active": {
        "en": "CHOICE {current}/{total}",
        "zh": "选择 {current}/{total}",
    },
    "ops_auth_hint": {
        "en": "Tab→right→Enter or /allow",
        "zh": "Tab→右栏→Enter 或 /allow",
    },
    "ops_auth_pending": {
        "en": "AUTH pending×{count}",
        "zh": "待授权×{count}",
    },
    "ops_monitor_collapsed": {
        "en": "monitor on — widen terminal or /monitor off",
        "zh": "监控已开 — 加宽终端或 /monitor off",
    },
    "cli_extra_monitor": {
        "en": "toggle right monitor rail",
        "zh": "开关右侧监控栏",
    },
    "cli_extra_sidebar": {
        "en": "show, hide, or responsively manage the left rail",
        "zh": "显示、隐藏或自动管理左侧栏",
    },
    "cli_extra_jobs": {"en": "list run jobs / sub-agents", "zh": "列出运行任务 / 子 Agent"},
    "cli_extra_resume": {
        "en": "resume a paused job with a message",
        "zh": "用消息续跑暂停的任务",
    },
    "monitor_usage": {
        "en": "Usage: /monitor [on|off|sys|tokens|agents|both|status]",
        "zh": "用法：/monitor [on|off|sys|tokens|agents|both|status]",
    },
    "monitor_set": {
        "en": "Monitor rail: {mode}",
        "zh": "监控栏：{mode}",
    },
    "monitor_status_ask": {
        "en": "Send /monitor on|off|sys|tokens|agents|both (default off).",
        "zh": "发送 /monitor on|off|sys|tokens|agents|both（默认 off）。",
    },
    "sidebar_usage": {
        "en": "Usage: /sidebar [on|off|auto|status]",
        "zh": "用法：/sidebar [on|off|auto|status]",
    },
    "sidebar_set": {
        "en": "Left rail: {mode}",
        "zh": "左侧栏：{mode}",
    },
    "sidebar_status_ask": {
        "en": "Send /sidebar on|off|auto (default off).",
        "zh": "发送 /sidebar on|off|auto（默认 off）。",
    },
    "jobs_title": {
        "en": "Jobs · page {page}/{page_count} · {total}",
        "zh": "任务 · 第 {page}/{page_count} 页 · 共 {total}",
    },
    "jobs_row": {
        "en": "{ordinal}. {job_id}  [{state}]{flag}  {status}",
        "zh": "{ordinal}. {job_id}  [{state}]{flag}  {status}",
    },
    "jobs_unavailable": {"en": "Job list unavailable.", "zh": "任务列表不可用。"},
    "resume_usage": {
        "en": "Usage: /resume <job-id> <message>",
        "zh": "用法：/resume <job-id> <消息>",
    },
    "resume_need_allow": {
        "en": "Resolve pending tool confirm with /allow or /deny first.",
        "zh": "请先用 /allow 或 /deny 处理待确认工具。",
    },
    "resume_unavailable": {"en": "Resume unavailable.", "zh": "无法续跑。"},
    "resume_ok": {"en": "Resumed job {job_id}.", "zh": "已续跑 {job_id}。"},
    "empty_reply": {"en": "(empty reply)", "zh": "（空回复）"},
    "timeout": {"en": "Turn timed out.", "zh": "本轮已超时。"},
    "resumed": {"en": "resumed…", "zh": "已恢复…"},
    "banner_subtitle": {
        "en": "security workbench · local · scriptable",
        "zh": "本地安全工作台 · 可脚本化",
    },
    "setup_title": {"en": "setup", "zh": "初始配置"},
    "setup_no_endpoint": {
        "en": "No usable AI endpoint is configured for the built-in ArgusAI Agent.",
        "zh": "尚未配置可用的 AI 端点，内置 ArgusAI Agent 无法对话。",
    },
    "setup_configure_endpoint": {
        "en": "Configure: argusai ai add --provider … --api-key …  (or desktop Settings → AI).",
        "zh": "配置：argusai ai add --provider … --api-key …（或桌面版「设置 → AI 端点」）。",
    },
    "help_ai": {
        "en": "Manage chat AI endpoints (settings.json).",
        "zh": "管理对话 AI 端点（settings.json）。",
    },
    "help_ai_list": {
        "en": "List configured chat AI endpoints.",
        "zh": "列出已配置的对话 AI 端点。",
    },
    "help_ai_providers": {
        "en": "List built-in provider presets usable with ai add --provider.",
        "zh": "列出可用的厂商预设（配合 ai add --provider）。",
    },
    "help_ai_add": {
        "en": "Add or update a chat AI endpoint in settings.json.",
        "zh": "在 settings.json 中添加或更新对话 AI 端点。",
    },
    "help_ai_enable": {
        "en": "Enable an endpoint without deleting it.",
        "zh": "启用端点（不删除配置）。",
    },
    "help_ai_disable": {
        "en": "Disable an endpoint without deleting it.",
        "zh": "禁用端点（不删除配置）。",
    },
    "help_ai_remove": {
        "en": "Delete an AI endpoint from settings.json.",
        "zh": "从 settings.json 删除 AI 端点。",
    },
    "help_ai_activate": {
        "en": "Raise an endpoint's priority so new turns use it first.",
        "zh": "提高端点优先级，使新对话优先使用它。",
    },
    "help_ai_test": {
        "en": "Send a lightweight ping prompt to one endpoint.",
        "zh": "向指定端点发送轻量 ping 探测。",
    },
    "help_opt_base_url": {"en": "API base URL.", "zh": "API Base URL。"},
    "help_opt_api_key": {"en": "API key / token.", "zh": "API Key / Token。"},
    "help_opt_provider": {
        "en": "Preset id/name/alias (see: argusai ai providers).",
        "zh": "厂商预设 id/名称/别名（见 argusai ai providers）。",
    },
    "help_opt_model": {"en": "Active model id.", "zh": "当前模型 id。"},
    "help_opt_protocol": {
        "en": "openai or anthropic (default from provider).",
        "zh": "openai 或 anthropic（默认取预设）。",
    },
    "help_opt_priority": {
        "en": "Failover priority (higher first).",
        "zh": "故障转移优先级（越大越优先）。",
    },
    "help_opt_max_tokens": {"en": "Max output tokens.", "zh": "最大输出 token 数。"},
    "help_opt_context_window": {"en": "Context window tokens.", "zh": "上下文窗口 token 数。"},
    "help_opt_extra_body": {
        "en": "JSON object merged into extra_body.",
        "zh": "合并进 extra_body 的 JSON 对象。",
    },
    "help_opt_activate": {
        "en": "After save, raise priority so this endpoint is active.",
        "zh": "保存后提升优先级，立即作为当前端点。",
    },
    "help_opt_index": {
        "en": "Update existing endpoint by index.",
        "zh": "按序号更新已有端点。",
    },
    "setup_use_local_agent": {
        "en": "Local Agent available: {names}",
        "zh": "检测到可用本地 Agent：{names}",
    },
    "setup_hint_new": {
        "en": "In ops: /agent · /new <id|name|#>   ·   CLI: argusai agent list",
        "zh": "ops 内：/agent · /new <id|名|编号>   ·   命令行：argusai agent list",
    },
    "setup_hint_agent_list": {
        "en": "Install an ACP CLI (Cursor/Codex/…) then: argusai agent list · /new <id>",
        "zh": "安装 ACP 客户端（Cursor/Codex/…）后执行：argusai agent list · /new <id>",
    },
    "setup_hint_doctor": {
        "en": "Health check: argusai doctor",
        "zh": "健康检查：argusai doctor",
    },
    "help_root": {
        "en": (
            "ArgusAI Security Workbench — local · scriptable · safe-by-default.\n\n"
            "Bare invoke: interactive TTY → ops console; otherwise desktop GUI "
            "(override with --gui / --chat, or ARGUSAI_HOST=gui|cli)."
        ),
        "zh": (
            "ArgusAI 安全工作台 — 本地 · 可脚本化 · 默认安全。\n\n"
            "直接运行：交互终端 → ops 控制台；否则桌面 GUI "
            "（可用 --gui / --chat，或 ARGUSAI_HOST=gui|cli 强制）。"
        ),
    },
    "help_channels": {
        "en": "Manage and run communication channels (Telegram, Feishu, …).",
        "zh": "管理并运行通信渠道（Telegram、飞书等）。",
    },
    "help_channels_providers": {
        "en": "List supported channel providers and required secrets.",
        "zh": "列出支持的渠道类型及所需密钥字段。",
    },
    "help_channels_list": {"en": "List configured channels.", "zh": "列出已配置的渠道。"},
    "help_channels_add": {
        "en": "Add a channel by display name; id is auto-generated unless --id is set.",
        "zh": "按显示名称添加渠道；不写 --id 时自动生成内部 ID。",
    },
    "help_channels_enable": {"en": "Enable a channel.", "zh": "启用渠道。"},
    "help_channels_disable": {"en": "Disable a channel.", "zh": "停用渠道。"},
    "help_channels_remove": {
        "en": "Delete a channel and its encrypted secrets.",
        "zh": "删除渠道及其加密密钥。",
    },
    "help_channels_test": {"en": "Probe one channel adapter.", "zh": "探测单个渠道适配器。"},
    "help_channels_pair": {
        "en": "Create a pairing code for remote users to bind this channel.",
        "zh": "生成配对码，供远端用户绑定此渠道。",
    },
    "help_channels_identities": {
        "en": "List paired identities (and bound sessions).",
        "zh": "列出已配对身份及其绑定会话。",
    },
    "help_channels_bind": {
        "en": "Bind a paired identity to an assistant session.",
        "zh": "将已配对身份绑定到助手会话。",
    },
    "help_channels_grant": {
        "en": "Set permissions for a paired channel identity.",
        "zh": "设置已配对渠道身份的权限。",
    },
    "help_opt_channels_grant_all": {
        "en": "Grant every identity permission.",
        "zh": "授予该身份全部权限。",
    },
    "help_channels_login_qq": {
        "en": "Bind a QQ bot through the official QR Onboard flow.",
        "zh": "通过官方二维码 Onboard 流程绑定 QQ 机器人。",
    },
    "help_channels_login_wechat": {
        "en": "Complete WeChat iLink QR sign-in for a channel.",
        "zh": "完成微信 iLink 扫码登录。",
    },
    "help_channels_serve": {
        "en": "Run channel adapters without opening the desktop GUI.",
        "zh": "无桌面 GUI 时运行渠道适配器。",
    },
    "help_agent": {
        "en": "Manage local Agent runtimes (built-in ACP presets + custom).",
        "zh": "管理本地 Agent 运行时（内置 ACP 预设 + 自定义）。",
    },
    "help_agent_list": {
        "en": "List configured local agents and PATH detection status.",
        "zh": "列出已配置的本地 Agent 及 PATH 检测状态。",
    },
    "help_agent_add": {
        "en": "Add or update a custom ACP agent runtime in settings.json.",
        "zh": "在 settings.json 中添加或更新自定义 ACP Agent。",
    },
    "help_agent_enable": {
        "en": "Enable a runtime (including built-in presets).",
        "zh": "启用运行时（含内置预设）。",
    },
    "help_agent_disable": {
        "en": "Disable a runtime without deleting it.",
        "zh": "禁用运行时（不删除配置）。",
    },
    "help_agent_remove": {
        "en": "Delete a custom runtime. Built-in presets can only be disabled.",
        "zh": "删除自定义运行时。内置预设只能禁用不能删。",
    },
    "help_agent_test": {
        "en": "ACP initialize handshake (or native capability check).",
        "zh": "执行 ACP 握手探测（或内置 Agent 能力检查）。",
    },
    "help_chat": {
        "en": "Interactive ops console. `argusai chat /team use <id>` opens that team room.",
        "zh": "交互式控制台。`argusai chat /team use <id>` 直接进入该小队。",
    },
    "help_doctor": {
        "en": "Environment / runtime health checks.",
        "zh": "环境与运行时健康检查。",
    },
    "help_proxy": {
        "en": "Run the local MITM proxy, or control breakpoints on the one already running.",
        "zh": "运行本地 MITM 代理，或控制已经在跑的代理上的请求断点。",
    },
    "help_proxy_break": {"en": "Arm, list, forward, or drop breakpoints.", "zh": "武装、列出、放行或丢弃请求断点。"},
    "help_proxy_break_on": {"en": "Arm breakpoints on the running proxy.", "zh": "在正在运行的代理上打开断点。"},
    "help_proxy_break_off": {"en": "Disarm breakpoints.", "zh": "关闭断点。"},
    "help_proxy_break_list": {"en": "List parked requests.", "zh": "列出挂起的请求。"},
    "help_proxy_break_forward": {"en": "Forward one parked request.", "zh": "放行一条挂起的请求。"},
    "help_proxy_break_drop": {"en": "Drop one parked request.", "zh": "丢弃一条挂起的请求。"},
    "help_team": {"en": "Manage standing Agent teams.", "zh": "管理常驻 Agent 小队。"},
    "help_team_list": {"en": "List standing teams.", "zh": "列出常驻小队。"},
    "help_team_create": {"en": "Create a standing team.", "zh": "创建常驻小队。"},
    "help_team_show": {"en": "Show one team and its members.", "zh": "查看小队和成员。"},
    "help_team_delete": {"en": "Delete a team.", "zh": "删除小队。"},
    "help_team_add_member": {"en": "Add a member that waits for the first command.", "zh": "添加一名待命成员。"},
    "help_team_remove_member": {"en": "Remove one member.", "zh": "移除一名成员。"},
    "help_team_chat": {"en": "Print the group room or one private thread.", "zh": "查看群聊或某个成员的私聊。"},
    "help_team_send": {"en": "Send a command and host members until they finish.", "zh": "发送命令，并在本进程里等到成员结束。"},
    "help_team_stop": {"en": "Stop members hosted by this process.", "zh": "终止本进程里正在回复的成员。"},
    "help_team_greenlight": {
        "en": "Turn the team authorization shield on or off.",
        "zh": "打开或关闭小队授权（不再询问能不能做）。",
    },
    "help_report": {"en": "Render reports for a task.", "zh": "为任务生成报告。"},
    "help_scan": {
        "en": "Run security checks against a target URL.",
        "zh": "对目标 URL 运行安全检查。",
    },
    "help_version": {"en": "Print the ArgusAI version.", "zh": "打印 ArgusAI 版本。"},
    "help_kb": {
        "en": "Browse and manage the local knowledge base (terminal-friendly).",
        "zh": "在终端浏览与管理本地知识库。",
    },
    "help_kb_list": {"en": "List knowledge entries (newest first).", "zh": "列出知识条目（最新优先）。"},
    "help_kb_search": {
        "en": "Search the knowledge index (terminal browse).",
        "zh": "搜索知识库索引（终端浏览）。",
    },
    "help_kb_show": {
        "en": "Print one entry with markdown body in the terminal.",
        "zh": "在终端打印单条知识（含 Markdown 正文）。",
    },
    "help_kb_add": {"en": "Create a knowledge entry.", "zh": "新建知识条目。"},
    "help_kb_delete": {
        "en": "Move an entry to trash (soft delete).",
        "zh": "将条目移入回收站（软删除）。",
    },
    "help_kb_trash": {"en": "List trashed knowledge entries.", "zh": "列出回收站中的知识条目。"},
    "help_kb_restore": {
        "en": "Restore a knowledge entry from trash.",
        "zh": "从回收站恢复知识条目。",
    },
    "help_kb_purge": {
        "en": "Permanently delete trashed knowledge entries.",
        "zh": "彻底删除回收站中的知识条目。",
    },
    "help_mcp": {
        "en": "Manage MCP servers (stdio / HTTP / SSE).",
        "zh": "管理 MCP 服务（stdio / HTTP / SSE）。",
    },
    "help_mcp_list": {
        "en": "List configured MCP servers and last probe status.",
        "zh": "列出已配置的 MCP 服务及最近探测状态。",
    },
    "help_mcp_add": {
        "en": "Add or update an MCP server in settings.json.",
        "zh": "在 settings.json 中添加或更新 MCP 服务。",
    },
    "help_mcp_remove": {
        "en": "Delete an MCP server from settings.",
        "zh": "从设置中删除 MCP 服务。",
    },
    "help_mcp_enable": {"en": "Enable an MCP server.", "zh": "启用 MCP 服务。"},
    "help_mcp_disable": {
        "en": "Disable an MCP server without deleting it.",
        "zh": "禁用 MCP 服务（不删除配置）。",
    },
    "help_mcp_test": {
        "en": "Handshake / health-check one MCP server (blocking).",
        "zh": "探测单个 MCP 服务（阻塞握手）。",
    },
    "help_mcp_probe": {
        "en": "Kick a background probe of every enabled MCP server.",
        "zh": "后台探测所有已启用的 MCP 服务。",
    },
    "help_reverse": {
        "en": "Manage reverse-engineering jobs (list / create / export / toolchain).",
        "zh": "管理逆向工作台任务（列表 / 创建 / 导出 / 工具链）。",
    },
    "help_reverse_list": {"en": "List reverse jobs.", "zh": "列出逆向任务。"},
    "help_reverse_create": {"en": "Create a reverse job.", "zh": "创建逆向任务。"},
    "help_reverse_show": {
        "en": "Show one reverse job summary in the terminal.",
        "zh": "在终端显示单个逆向任务摘要。",
    },
    "help_reverse_trash": {"en": "Move a reverse job to trash.", "zh": "将逆向任务移入回收站。"},
    "help_reverse_restore": {
        "en": "Restore a reverse job from trash.",
        "zh": "从回收站恢复逆向任务。",
    },
    "help_reverse_purge": {
        "en": "Permanently delete a reverse job (including from trash).",
        "zh": "彻底删除逆向任务（含回收站中的）。",
    },
    "help_reverse_export": {
        "en": "Export a job to zip/folder (no GUI file-manager popup).",
        "zh": "导出任务为 zip/目录（不弹系统文件管理器）。",
    },
    "help_reverse_toolchain": {
        "en": "Scan PATH / settings for reverse toolchain binaries.",
        "zh": "扫描 PATH / 设置中的逆向工具链。",
    },
    "help_todos": {
        "en": "Manage user-level todos (cross-session, not assessment plan).",
        "zh": "管理用户级待办（跨会话，非评估计划）。",
    },
    "help_todos_list": {"en": "List user todos.", "zh": "列出用户待办。"},
    "help_todos_add": {"en": "Create a todo.", "zh": "新建待办。"},
    "help_todos_show": {"en": "Show one todo.", "zh": "显示单条待办。"},
    "help_todos_done": {"en": "Mark a todo completed.", "zh": "将待办标记为完成。"},
    "help_todos_status": {"en": "Set todo status.", "zh": "设置待办状态。"},
    "help_todos_delete": {"en": "Delete a todo.", "zh": "删除待办。"},
    "help_cron": {
        "en": "Manage assistant scheduled jobs (interval or cron expr).",
        "zh": "管理助手定时任务（间隔或 cron 表达式）。",
    },
    "help_cron_list": {"en": "List assistant cron jobs.", "zh": "列出助手定时任务。"},
    "help_cron_add": {
        "en": "Create or update a scheduled assistant job.",
        "zh": "创建或更新助手定时任务。",
    },
    "help_cron_enable": {"en": "Enable a cron job.", "zh": "启用定时任务。"},
    "help_cron_disable": {"en": "Disable a cron job.", "zh": "禁用定时任务。"},
    "help_cron_remove": {"en": "Delete a cron job.", "zh": "删除定时任务。"},
    "help_cron_runs": {
        "en": "Show recent run history for a job.",
        "zh": "查看定时任务最近运行记录。",
    },
    "help_cron_serve": {
        "en": "Run the cron scheduler in the foreground (prefer argusai serve --cron-only).",
        "zh": "前台仅跑定时调度（推荐用 argusai serve --cron-only）。",
    },
    "help_serve": {
        "en": "Headless host: channels + cron (OS service lifecycle).",
        "zh": "无头宿主：渠道 + 定时（系统服务生命周期）。",
    },
    "help_serve_run": {
        "en": "Run the headless host in the foreground (Ctrl+C to stop).",
        "zh": "前台运行无头宿主（Ctrl+C 停止）。",
    },
    "help_serve_install": {
        "en": "Register the host as a user OS service (Task Scheduler / systemd --user / LaunchAgent).",
        "zh": "将宿主注册为用户级系统服务（任务计划 / systemd --user / LaunchAgent）。",
    },
    "help_serve_boot": {
        "en": "Change autostart policy for an already-installed host service.",
        "zh": "修改已安装宿主服务的开机/登录自启策略。",
    },
    "help_serve_uninstall": {
        "en": "Remove the host OS service (and any legacy split services).",
        "zh": "卸载宿主系统服务（并清理旧的拆分服务）。",
    },
    "help_serve_start": {"en": "Start the installed host service.", "zh": "启动已安装的宿主服务。"},
    "help_serve_stop": {"en": "Stop the installed host service.", "zh": "停止已安装的宿主服务。"},
    "help_serve_restart": {
        "en": "Restart the installed host service.",
        "zh": "重启已安装的宿主服务。",
    },
    "help_serve_status": {
        "en": "Show whether the host service is installed / running.",
        "zh": "查看宿主服务是否已安装 / 是否在运行。",
    },
    "help_opt_help": {"en": "Show this message and exit.", "zh": "显示帮助并退出。"},
    "help_opt_json": {"en": "Machine-readable JSON output.", "zh": "以 JSON 输出。"},
    "help_opt_verbose": {
        "en": "Mirror logs to stderr (default: quiet for CLI subcommands).",
        "zh": "将日志输出到 stderr（CLI 子命令默认安静）。",
    },
    "help_opt_gui": {
        "en": "Force desktop GUI (overrides TTY auto-detect).",
        "zh": "强制启动桌面 GUI（覆盖 TTY 自动检测）。",
    },
    "help_opt_tray": {
        "en": "Start the desktop GUI hidden in the system tray.",
        "zh": "启动桌面 GUI 并隐藏到托盘。",
    },
    "help_opt_chat": {
        "en": "Force ops console / Agent REPL (same as interactive default).",
        "zh": "强制进入 ops 控制台 / Agent 对话。",
    },
    "help_opt_yes": {"en": "Skip confirmation prompts.", "zh": "跳过确认提示。"},
    "help_opt_enable": {"en": "Enable (or enable after save).", "zh": "启用（或保存后启用）。"},
    "help_opt_disable": {"en": "Disable (or keep disabled).", "zh": "禁用（或保持禁用）。"},
    "help_opt_id": {
        "en": "Optional internal id (auto-generated if omitted).",
        "zh": "可选内部 ID（省略则自动生成）。",
    },
    "help_opt_name": {
        "en": "Display name (recommended).",
        "zh": "显示名称（推荐填写）。",
    },
    "help_opt_secret": {
        "en": "Credential KEY=VALUE (repeatable).",
        "zh": "凭证 KEY=VALUE（可重复）。",
    },
    "help_opt_setting": {
        "en": "Settings KEY=VALUE (repeatable).",
        "zh": "设置 KEY=VALUE（可重复）。",
    },
    "help_opt_host": {
        "en": "Listen host.",
        "zh": "监听主机。",
    },
    "help_opt_port": {
        "en": "Listen port.",
        "zh": "监听端口。",
    },
    "help_opt_path": {
        "en": "Path (callback / filesystem, depending on command).",
        "zh": "路径（回调或文件系统，视命令而定）。",
    },
    "help_opt_session": {"en": "Assistant session id.", "zh": "助手会话 ID。"},
    "help_opt_timeout": {"en": "Timeout in seconds.", "zh": "超时秒数。"},
    "help_opt_command": {
        "en": "Executable (PATH or absolute).",
        "zh": "可执行文件（PATH 或绝对路径）。",
    },
    "help_opt_arg": {"en": "Command argument (repeatable).", "zh": "命令参数（可重复）。"},
    "help_opt_env": {
        "en": "Environment KEY=VALUE (repeatable).",
        "zh": "环境变量 KEY=VALUE（可重复）。",
    },
    "help_opt_cwd": {"en": "Working directory.", "zh": "工作目录。"},
    "help_opt_notes": {"en": "Optional notes.", "zh": "备注。"},
    "help_opt_cron_only": {
        "en": "Only run / install the cron scheduler (skip channel adapters).",
        "zh": "仅运行/安装定时调度（跳过渠道适配器）。",
    },
    "help_opt_boot": {
        "en": "Boot policy: Windows logon|startup; Linux user|linger; macOS logon.",
        "zh": "启动策略：Windows 为 logon|startup；Linux 为 user|linger；macOS 为 logon。",
    },
    "help_opt_no_start": {
        "en": "Register only; do not start immediately.",
        "zh": "仅注册，不立即启动。",
    },
    "help_opt_dry_run": {
        "en": "Print the plan without applying changes.",
        "zh": "只打印计划，不实际执行。",
    },
    "help_opt_start": {
        "en": "Also start after updating the service.",
        "zh": "更新后同时启动服务。",
    },
    "help_opt_once": {
        "en": "One-shot mode: run one turn then exit (for scripts).",
        "zh": "单轮模式：跑完一轮后退出（适合脚本）。",
    },
    "help_opt_system": {
        "en": "Also set the OS system proxy to this listener (use carefully).",
        "zh": "同时把系统代理指向此监听（请谨慎）。",
    },
    "help_opt_url": {"en": "URL.", "zh": "URL。"},
    "help_opt_transport": {
        "en": "stdio | streamable-http | sse.",
        "zh": "传输：stdio | streamable-http | sse。",
    },
    "help_opt_header": {
        "en": "HTTP header KEY=VALUE (repeatable).",
        "zh": "HTTP 头 KEY=VALUE（可重复）。",
    },
    "help_opt_tag": {"en": "Tag (repeatable / filter).", "zh": "标签（可重复 / 筛选）。"},
    "help_opt_limit": {"en": "Max rows / matches.", "zh": "最大条数。"},
    "help_opt_title": {"en": "Title.", "zh": "标题。"},
    "help_opt_body": {"en": "Body text.", "zh": "正文。"},
    "help_opt_file": {"en": "Read content from a UTF-8 file.", "zh": "从 UTF-8 文件读取内容。"},
    "help_opt_chat_file": {
        "en": "Attach a local file to the next Agent turn (repeatable).",
        "zh": "附加本地文件到下一轮 Agent 对话（可重复指定）。",
    },
    "help_opt_reason": {"en": "Reason note.", "zh": "原因备注。"},
    "help_opt_trash": {"en": "Include / allow trashed items.", "zh": "包含/允许回收站条目。"},
    "help_opt_superseded": {
        "en": "Include superseded knowledge entries.",
        "zh": "包含已被取代的知识条目。",
    },
    "help_opt_max_chars": {
        "en": "Truncate body for the terminal (0 = full).",
        "zh": "终端正文截断长度（0 = 全文）。",
    },
    "help_opt_scope": {"en": "Scope filter / label.", "zh": "范围筛选/标签。"},
    "help_opt_status": {"en": "Status filter.", "zh": "状态筛选。"},
    "help_opt_intent": {"en": "Analysis intent.", "zh": "分析意图。"},
    "help_opt_platform": {"en": "Platform hint.", "zh": "平台提示。"},
    "help_opt_task": {"en": "Linked scan task id.", "zh": "关联扫描任务 ID。"},
    "help_opt_dest": {"en": "Destination directory.", "zh": "目标目录。"},
    "help_opt_decompiled": {
        "en": "Include decompiled/ (can be large).",
        "zh": "包含 decompiled/（可能很大）。",
    },
    "help_opt_folder": {
        "en": "Export as a folder instead of zip.",
        "zh": "导出为目录而非 zip。",
    },
    "help_opt_job": {"en": "Job id.", "zh": "任务 ID。"},
    "help_opt_tool": {
        "en": "Limit to named tool(s) (repeatable).",
        "zh": "限定工具名（可重复）。",
    },
    "help_opt_due": {"en": "Due timestamp / date string.", "zh": "截止时间。"},
    "help_opt_prompt": {
        "en": "Message sent to the assistant.",
        "zh": "发给助手的提示内容。",
    },
    "help_opt_every": {
        "en": "Interval seconds (mutually exclusive with --cron).",
        "zh": "间隔秒数（与 --cron 互斥）。",
    },
    "help_opt_cron_expr": {
        "en": "5-field cron expr, e.g. '0 9 * * *'.",
        "zh": "5 段 cron 表达式，如 '0 9 * * *'。",
    },
    "help_opt_destination": {
        "en": "new_session | session.",
        "zh": "目标：new_session | session。",
    },
    "help_opt_checks": {
        "en": "Comma-separated check ids (default: all registered).",
        "zh": "逗号分隔的检查 id（默认全部）。",
    },
    "help_opt_out": {
        "en": "Output directory for findings / reports.",
        "zh": "发现/报告输出目录。",
    },
    "help_opt_format": {
        "en": "Comma-separated formats: html, md, json.",
        "zh": "逗号分隔格式：html、md、json。",
    },
    "help_opt_task_dir": {
        "en": "Path to a task or scan run directory.",
        "zh": "任务或扫描运行目录路径。",
    },
    "help_opt_task_id": {
        "en": "Task id under the configured tasks_dir.",
        "zh": "配置的 tasks_dir 下的任务 id。",
    },
    "help_opt_install_completion": {
        "en": "Install completion for the current shell.",
        "zh": "为当前 shell 安装补全脚本。",
    },
    "help_opt_show_completion": {
        "en": "Show completion for the current shell (to copy or customize).",
        "zh": "显示当前 shell 的补全脚本（便于复制或自定义）。",
    },
    "help_opt_older_days": {
        "en": "Hard-delete trashed entries older than N days.",
        "zh": "彻底删除回收站中超过 N 天的条目。",
    },
    "help_opt_all": {
        "en": "Apply to everything currently in trash.",
        "zh": "作用于回收站中的全部条目。",
    },
    "help_opt_kb_purge_id": {
        "en": "Hard-delete specific trashed id(s) (repeatable).",
        "zh": "彻底删除指定回收站条目 ID（可重复）。",
    },
    "help_arg_provider": {
        "en": "Provider id (telegram, feishu, …).",
        "zh": "渠道类型（telegram、feishu…）。",
    },
    "help_arg_channel_id": {"en": "Channel id.", "zh": "渠道 ID。"},
    "help_arg_identity_id": {"en": "Paired identity id.", "zh": "已配对身份 ID。"},
    "help_arg_session_id": {"en": "Assistant session id.", "zh": "助手会话 ID。"},
    "help_arg_runtime_id": {"en": "Agent runtime id.", "zh": "Agent 运行时 ID。"},
    "help_arg_mcp_server_id": {"en": "MCP server id.", "zh": "MCP 服务 ID。"},
    "help_arg_kb_query": {
        "en": "Keywords (title / tags / triggers).",
        "zh": "关键词（标题 / 标签 / 触发词）。",
    },
    "help_arg_kb_entry_id": {"en": "Knowledge entry id.", "zh": "知识条目 ID。"},
    "help_arg_reverse_job_id": {"en": "Reverse job id.", "zh": "逆向任务 ID。"},
    "help_arg_todo_title": {"en": "Todo title.", "zh": "待办标题。"},
    "help_arg_todo_id": {"en": "Todo id.", "zh": "待办 ID。"},
    "help_arg_todo_status": {
        "en": "pending|in_progress|completed|cancelled",
        "zh": "pending|in_progress|completed|cancelled",
    },
    "help_arg_cron_job_id": {"en": "Cron job id.", "zh": "定时任务 ID。"},
    "help_arg_boot_policy": {
        "en": "Boot policy: Windows logon|startup; Linux user|linger; macOS logon.",
        "zh": "启动策略：Windows 为 logon|startup；Linux 为 user|linger；macOS 为 logon。",
    },
    "help_arg_scan_url": {
        "en": "Target URL (e.g. https://example.com).",
        "zh": "目标 URL（如 https://example.com）。",
    },
    "help_arg_chat_message": {
        "en": "Opening line. Words are joined, so `/team use <id>` enters that team room.",
        "zh": "启动后的第一句。多个词会拼在一起，`/team use <id>` 直接进入该小队。",
    },
    "boot_policy_logon": {"en": "At user logon (tray)", "zh": "登录后托盘启动"},
    "boot_policy_logon_hint": {
        "en": "Per-user Startup entry (Settings → Apps → Startup). Tray, no admin.",
        "zh": "写入当前用户启动项（系统设置 → 应用 → 启动），托盘打开，不需要管理员。",
    },
    "boot_policy_startup": {"en": "At system startup (no tray)", "zh": "未登录也运行（无托盘）"},
    "boot_policy_startup_hint": {
        "en": "Headless host before anyone signs in. Needs Administrator; no tray icon.",
        "zh": "没人登录也跑无头进程，没有托盘。需要管理员权限。",
    },
    "boot_policy_user": {"en": "With user session", "zh": "用户会话启动时"},
    "boot_policy_user_hint": {
        "en": "Starts when your user session is up.",
        "zh": "用户会话起来后启动。",
    },
    "boot_policy_linger": {"en": "System boot (linger)", "zh": "系统启动（linger）"},
    "boot_policy_linger_hint": {
        "en": "Enables systemd lingering so it survives without login.",
        "zh": "开启 systemd linger，无需保持登录也可随系统启动。",
    },
    "boot_policy_macos_logon_hint": {
        "en": "Packaged .app: Login Item. Dev install: LaunchAgent. After log in, no admin.",
        "zh": "打包的 .app 走登录项；开发安装走 LaunchAgent。登录后启动，不需要管理员。",
    },
    "serve_err_macos_login_item": {
        "en": "Could not register the macOS Login Item. Allow ArgusAI in System Settings → General → Login Items, or use a packaged .app.",
        "zh": "无法注册 macOS 登录项。请在系统设置 → 通用 → 登录项与扩展 里允许 ArgusAI，或使用打包的 .app。",
    },
    "serve_err_startup_needs_admin": {
        "en": "“At system startup” registers a boot task and needs Administrator. Right-click ArgusAI → Run as administrator, or switch to “At user logon” (tray, no admin).",
        "zh": "「系统开机时」要在登录前注册计划任务，需要管理员权限。请右键 ArgusAI 选「以管理员身份运行」后再打开这项，或改用「用户登录后」（托盘启动，不用管理员）。",
    },
    "serve_err_access_denied_logon": {
        "en": "Windows refused this change. Run ArgusAI as administrator and try again, or use “At user logon”.",
        "zh": "Windows 拒绝了这项更改。请以管理员身份运行 ArgusAI 后再试，或改用「用户登录后」。",
    },
    "serve_err_access_denied_delete": {
        "en": "Could not remove the scheduled task (it was probably created as administrator). Run ArgusAI as administrator, then turn this off.",
        "zh": "删不掉已有的计划任务（多半是以前用管理员创建的）。请以管理员身份运行 ArgusAI，再关闭这项。",
    },
    "serve_err_unknown": {
        "en": "Could not update the Windows scheduled task.",
        "zh": "无法更新 Windows 计划任务。",
    },
    "serve_note_s4u_fallback": {
        "en": "Windows refused a pre-logon (S4U) task. Registered at user logon instead.",
        "zh": "系统拒绝了登录前（S4U）任务，已改为用户登录后启动。",
    },
    "serve_status_not_installed": {"en": "not installed", "zh": "未安装"},
    "serve_status_hint_install": {
        "en": "argusai serve install",
        "zh": "argusai serve install",
    },
    "serve_status_active": {"en": "active", "zh": "运行中"},
    "serve_mode_cron_only": {"en": "cron-only", "zh": "仅定时"},
    "serve_mode_full": {"en": "channels+cron", "zh": "渠道+定时"},
    "serve_msg_installed": {"en": "installed", "zh": "已安装"},
    "serve_msg_removed": {"en": "removed", "zh": "已移除"},
    "serve_msg_started": {"en": "started", "zh": "已启动"},
    "serve_msg_stopped": {"en": "stopped", "zh": "已停止"},
    "serve_msg_restarted": {"en": "restarted", "zh": "已重启"},
    "serve_msg_boot": {"en": "boot", "zh": "启动策略"},
    "serve_msg_none_registered": {
        "en": "No ArgusAI host service was registered.",
        "zh": "尚未注册 ArgusAI 宿主服务。",
    },
    "serve_msg_not_installed": {
        "en": "host service is not installed; run: argusai serve install",
        "zh": "宿主服务未安装；请先执行：argusai serve install",
    },
}


def configure_cli_stdio() -> None:
    """Align stdio for consoles, ``pythonw``, and headless Task Scheduler hosts.

    Under ``pythonw.exe`` (no console) ``sys.stdout`` / ``sys.stderr`` /
    ``sys.stdin`` are ``None``; Rich/Typer then crash on first print. Bind
    those to ``os.devnull`` so ``argusai serve run`` can stay alive.

    For an interactive TTY we only adjust **output** encoding. Forcing
    ``SetConsoleCP(65001)`` / ``stdin.reconfigure(utf-8)`` on Chinese Windows
    consoles commonly breaks prompt_toolkit key input in the ops console.
    """
    import io

    interactive_stdin = False
    try:
        interactive_stdin = bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:
        interactive_stdin = False

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(
                    sys,
                    name,
                    open(os.devnull, "w", encoding="utf-8", errors="replace"),  # noqa: SIM115
                )
            except Exception:
                setattr(sys, name, io.StringIO())
    if getattr(sys, "stdin", None) is None:
        # Headless / pythonw only — never replace a live interactive stdin.
        try:
            sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
        except Exception:
            sys.stdin = io.StringIO()

    if sys.platform == "win32":
        try:
            import ctypes

            # Output CP only. Changing the *input* CP (SetConsoleCP) breaks
            # keyboard handling in cmd.exe / ConHost for prompt_toolkit apps.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # Leave interactive stdin on the console's native encoding so typed keys
    # reach prompt_toolkit. Headless null stdin can stay UTF-8.
    if not interactive_stdin:
        stream = getattr(sys, "stdin", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def bootstrap_locale(*, force: bool = False) -> str:
    """Initialize CLI locale from the environment / OS once."""
    global _LOCALE_BOOTSTRAPPED
    if _LOCALE_BOOTSTRAPPED and not force:
        return get_locale()
    set_locale(detect_system_locale())
    _LOCALE_BOOTSTRAPPED = True
    return get_locale()


def get_locale() -> str:
    return normalize_locale(_CLI_LOCALE)


def set_locale(value: str) -> str:
    global _CLI_LOCALE
    _CLI_LOCALE = normalize_locale(value)
    return _CLI_LOCALE


def sync_from_api(api: Any) -> str:
    """Prefer Api locale when present (set by ``/lang`` or GUI)."""
    raw = getattr(api, "_locale", None)
    if raw:
        return set_locale(str(raw))
    return get_locale()


def t(key: str, **params: Any) -> str:
    """Channel catalog string in the active CLI locale."""
    return channel_text(key, get_locale(), inline=True, **params)


def label(key: str, **params: Any) -> str:
    """CLI-only label (falls back to English)."""
    mode = get_locale()
    if mode == BILINGUAL:
        mode = BILINGUAL_PRIMARY
    if mode not in {"en", "zh"}:
        mode = DEFAULT_LOCALE
    template = (
        _CLI_LABELS.get(key, {}).get(mode)
        or _CLI_LABELS.get(key, {}).get("en")
        or key
    )
    try:
        return template.format(**params) if params else template
    except Exception:
        return template
