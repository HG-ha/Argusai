"""Loom22-aligned tool/skill progressive disclosure tests."""

from __future__ import annotations

import importlib
from pathlib import Path

import argusai.core.assistant.skills as skills_mod
from argusai.core.assistant.skills import (
    MAX_AUTO_SKILL_CHARS,
    MAX_AUTO_SKILLS,
    _build_system_prompt,
    _select_matched_skills,
    _select_skill_text,
    _select_tool_names,
)
from argusai.core.assistant.tools import build_tool_registry


def _reload_skills() -> None:
    importlib.reload(skills_mod)


def test_assistant_default_tools_are_narrow() -> None:
    names = _select_tool_names("assistant", "hello")
    assert "tool_search" in names
    assert "mcp_marketplace_search" in names
    assert "mcp_marketplace_get" in names
    assert "web_search" not in names
    assert "sqli_probe_param" not in names
    assert "desktop_click" not in names
    assert len(names) < 120


def test_web_search_always_injected_when_enabled() -> None:
    names = _select_tool_names("assistant", "hello", allow_web_search=True)
    assert "web_search" in names


def test_system_prompt_mentions_web_search_when_enabled() -> None:
    prompt = _build_system_prompt("- `web_search(query)`: Search internet.", "", allow_web_search=True)
    assert "Internet search is ON" in prompt
    assert "`web_search`" in prompt
    assert "local tool catalogue" in prompt


def test_system_prompt_omits_web_search_contract_when_disabled() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "", allow_web_search=False)
    assert "Internet search is ON" not in prompt


def test_system_prompt_allows_one_refined_tool_search() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "")
    assert "once more" in prompt
    assert "two misses" in prompt
    assert "identical query" in prompt
    assert "invoke_catalogue_tool" in prompt


def test_system_prompt_defines_source_aware_notification_routing() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "")
    assert "User notifications are source-aware" in prompt
    assert "GUI-origin turns" in prompt
    assert "communication-channel-origin turns" in prompt
    assert "normal final reply already returns to that channel" in prompt
    assert "A history entry marked `channel_outbound`" in prompt
    assert "`Origin conversation`" in prompt
    assert "call `read_assistant_session` for that exact origin" in prompt


def test_web_search_keywords_match_internet_capability_question() -> None:
    names = _select_tool_names("assistant", "你能上网吗", allow_web_search=True)
    assert "web_search" in names


def test_web_search_not_injected_when_disabled() -> None:
    names = _select_tool_names("assistant", "hello", allow_web_search=False)
    assert "web_search" not in names
    assert "github_search" not in names
    assert "mcp_github_candidate" not in names


def test_trigger_tools_expand_matched_skill_groups() -> None:
    _reload_skills()
    names = _select_tool_names("assistant", "blind SSRF OOB correlation")
    assert "oob_payload_generate" in names
    assert "evidence_add" in names
    assert "ssrf_probe_url_param" in names


def test_extended_domain_messages_expose_only_relevant_tool_groups() -> None:
    _reload_skills()
    cloud = set(_select_tool_names("assistant", "审计 Kubernetes 集群和云配置"))
    directory = set(_select_tool_names("assistant", "导入 SharpHound 报告分析域安全"))
    assert {"kubectl_readonly_plan", "prowler_cloud_plan", "checkov_scan_plan"} <= cloud
    assert "ad_sharphound_import" not in cloud
    assert {"ad_sharphound_import", "ad_certipy_find_plan"} <= directory
    assert "kubectl_readonly_run" not in directory


def test_attack_tools_expand_on_pentest_message() -> None:
    names = _select_tool_names("assistant", "full pentest https://example.com")
    assert "sqli_probe_param" in names
    assert "pentest_plan_chain" in names


def test_install_wabt_attaches_runtime_tools() -> None:
    _reload_skills()
    names = set(_select_tool_names("assistant", "安装 WABT"))
    assert "runtime_install" in names
    assert "runtime_status" in names


def test_install_apksigner_attaches_reverse_toolchain_install() -> None:
    _reload_skills()
    names = set(_select_tool_names("assistant", "帮我安装 apksigner"))
    assert "reverse_toolchain_install" in names
    assert "reverse_toolchain_scan" in names


def test_code_authoring_attaches_development_tools_and_skill() -> None:
    _reload_skills()
    names = set(_select_tool_names("assistant", "帮我编写代码，实现一个带单元测试的 Python 工具"))
    matched = [sd.name for sd in _select_matched_skills("帮我编写代码，实现一个带单元测试的 Python 工具")]
    assert {"sandbox_write_file", "sandbox_patch_file", "run_python_script"} <= names
    assert "software-development" in matched


def test_daily_file_task_attaches_productivity_tools_and_skill() -> None:
    _reload_skills()
    names = set(_select_tool_names("assistant", "整理文件并批量重命名，完成后提醒我"))
    matched = [sd.name for sd in _select_matched_skills("整理文件并批量重命名，完成后提醒我")]
    assert {"sandbox_write_file", "sandbox_open_file", "desktop_notify"} <= names
    assert "daily-productivity" in matched


def test_notification_request_attaches_desktop_and_channel_delivery() -> None:
    _reload_skills()
    message = "任务完成后通过桌面通知或者飞书提醒我"
    names = set(_select_tool_names("assistant", message))
    matched = [sd.name for sd in _select_matched_skills(message)]
    assert {
        "desktop_notify",
        "channel_list_targets",
        "channel_send_message",
        "channel_delivery_status",
    } <= names
    assert "user-notification" in matched


def test_security_phrasing_does_not_open_desktop_or_shell_via_new_skills() -> None:
    _reload_skills()
    for message in (
        "help me debug this SSRF",
        "organize the findings by severity",
        "二次开发接口鉴权绕过",
    ):
        names = set(_select_tool_names("assistant", message))
        # Weak description overlaps may still surface a skill pointer, but must
        # not expand high-risk desktop input or shell schemas into the turn.
        assert "desktop_click" not in names
        assert "desktop_type" not in names
        assert "desktop_hotkey" not in names
        assert "desktop_launch_app" not in names
        assert "run_shell_script" not in names
        assert "run_shell_snippet" not in names


def test_settings_page_stays_narrow() -> None:
    names = _select_tool_names("settings", "帮我看看 AI 端点设置")
    assert "get_settings" in names
    assert "sqli_probe_param" not in names


def test_cron_tools_are_progressively_disclosed() -> None:
    _reload_skills()
    generic = set(_select_tool_names("assistant", "你好"))
    scheduled = set(_select_tool_names("assistant", "每天九点定时生成安全巡检简报"))
    assert "cron_upsert" not in generic
    assert {"cron_list", "cron_runs", "cron_upsert", "cron_set_enabled", "cron_delete"} <= scheduled
    matched = {sd.name for sd in _select_matched_skills("每天九点定时生成安全巡检简报")}
    assert {"assistant-cron", "proactive-defense"} <= matched


def test_every_n_seconds_and_cron_page_attach_scheduler_tools() -> None:
    _reload_skills()
    message = "每五秒生成一个1-10之间的随机数，小于5时停止并发送邮件"
    names = set(_select_tool_names("assistant", message))
    assert "assistant-cron" in {sd.name for sd in _select_matched_skills(message)}
    assert {"cron_list", "cron_runs", "cron_upsert", "cron_set_enabled", "cron_delete"} <= names
    page_names = set(_select_tool_names("cron", "查看这里的任务"))
    assert {"cron_list", "cron_runs", "cron_upsert", "cron_set_enabled", "cron_delete"} <= page_names


def test_durable_continuation_phrase_routes_to_cron_skill() -> None:
    _reload_skills()
    message = "给当前主 Agent 配置自动续跑，直到完成所有未完成任务"
    matched = {sd.name for sd in _select_matched_skills(message)}
    names = set(_select_tool_names("assistant", message))

    assert "assistant-cron" in matched
    assert {"cron_list", "cron_upsert", "cron_set_enabled"} <= names


def test_external_capability_request_routes_to_provisioning_skill() -> None:
    _reload_skills()
    message = "我想安装外部 CLI 和 MCP 来扩展 ArgusAI，并调用其他软件"
    matched = {sd.name for sd in _select_matched_skills(message)}
    names = set(_select_tool_names("assistant", message))

    assert "mcp-provision-workflow" in matched
    assert {
        "mcp_server_list",
        "mcp_server_list_tools",
        "external_tool_inventory",
        "external_tool_install_plan",
    } <= names


def test_multi_agent_capability_is_always_available_and_routes_to_skill() -> None:
    _reload_skills()
    generic = set(_select_tool_names("assistant", "你好"))
    message = "把代码审计拆给三个独立子 Agent 并行检查"
    matched = {sd.name for sd in _select_matched_skills(message)}
    names = set(_select_tool_names("assistant", message))

    assert {
        "run_subagent",
        "spawn_agent",
        "send_agent_input",
        "pause_agent",
        "resume_agent",
        "wait_for_agents",
        "list_agents",
        "get_agent",
        "close_agent",
        "background_job_status",
        "background_job_list",
        "background_job_cancel",
    } <= generic
    assert "multi-agent-orchestration" in matched
    assert {
        "run_subagent",
        "spawn_agent",
        "send_agent_input",
        "pause_agent",
        "resume_agent",
        "wait_for_agents",
        "list_agents",
        "get_agent",
        "close_agent",
        "background_job_status",
        "background_job_list",
        "background_job_cancel",
    } <= names


def test_complex_multi_component_work_proactively_routes_to_multi_agent_skill() -> None:
    _reload_skills()
    matched = {
        sd.name
        for sd in _select_matched_skills("全面优化所有界面，并检查多个模块的交互一致性")
    }

    assert "multi-agent-orchestration" in matched


def test_skill_no_substring_false_positives() -> None:
    _reload_skills()
    matched = [sd.name for sd in _select_matched_skills("AI endpoint settings")]
    assert "knowledge-base" not in matched
    assert "proxy-guide" not in matched
    assert "browser-attach-workflow" not in matched
    assert "settings-guide" in matched


def test_pentest_loads_workflow_skills_not_browser_attach() -> None:
    _reload_skills()
    matched = [sd.name for sd in _select_matched_skills("full pentest nexusmc.cn")]
    assert "browser-attach-workflow" not in matched
    assert "oob-workflow" not in matched
    assert "session-sandbox-workflow" not in matched
    assert any(
        name in matched
        for name in ("security-workflow", "authorized-pentest", "agent-todo")
    )


def test_sqli_prefers_sqli_workflow_over_browser_attach() -> None:
    _reload_skills()
    matched = [sd.name for sd in _select_matched_skills("SQL injection on login API")]
    assert "sqli-workflow" in matched
    assert "browser-attach-workflow" not in matched


def test_vague_message_does_not_auto_inject_heavy_skills() -> None:
    _reload_skills()
    matched = [sd.name for sd in _select_matched_skills("vague look at this url")]
    assert "js-reverse" not in matched
    assert "scan-setup" not in matched
    assert "browser-attach-workflow" not in matched


def test_auto_skill_caps() -> None:
    _reload_skills()
    matched = _select_matched_skills(
        "pentest sqli xss ssrf cors upload oob blind scan audit methodology"
    )
    assert len(matched) <= MAX_AUTO_SKILLS
    text = _select_skill_text(
        "pentest sqli xss ssrf cors upload oob blind scan audit methodology"
    )
    assert len(text) <= MAX_AUTO_SKILL_CHARS


def test_assistant_prompt_size_reasonable() -> None:
    _reload_skills()
    registry = build_tool_registry(None)
    message = "full pentest https://example.com"
    active = _select_tool_names("assistant", message)
    catalog = registry.catalog(active)
    skills = _select_skill_text(message, active)
    prompt = _build_system_prompt(catalog, skills)
    assert len(active) < 200
    assert len(prompt) < 80_000


def test_settings_prompt_stays_small() -> None:
    _reload_skills()
    registry = build_tool_registry(None)
    message = "帮我看看 AI 端点设置"
    active = _select_tool_names("settings", message)
    prompt = _build_system_prompt(registry.catalog(active), _select_skill_text(message, active))
    assert len(active) < 80
    assert len(prompt) < 25_000


def test_project_workspace_attaches_codebase_tools() -> None:
    from types import SimpleNamespace

    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    _reload_skills()
    names = _select_tool_names("assistant", "hello")
    assert "codebase_tree" not in names
    session = SimpleNamespace(
        runtime_binding=normalize_session_runtime_binding({
            "workspace_mode": "project",
            "project_dir": "H:/work/demo",
        }),
        registry=None,
    )
    expanded = skills_mod.apply_project_workspace_tools(session, names)
    assert "codebase_tree" in expanded
    assert "codebase_run_shell_snippet" in expanded


def test_workspace_note_uses_bound_project_dir(tmp_path: Path) -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    session = AssistantSession(registry=ToolRegistry())
    session.runtime_binding = normalize_session_runtime_binding({
        "workspace_mode": "project",
        "project_dir": str(tmp_path),
    })
    note = session._workspace_capability_note()
    assert "local project" in note
    assert str(tmp_path) in note
    assert "codebase_tree" in note
