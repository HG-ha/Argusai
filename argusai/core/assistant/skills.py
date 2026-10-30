"""Assistant runtime helpers split from the former monolithic module."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

log = logging.getLogger("argusai.assistant")

__all__ = [
    'SKILLS_DIR', 'SkillDoc', '_parse_frontmatter', '_load_skill_docs', '_SKILL_DOCS',
    'TOOL_GROUPS', 'ALWAYS_LOADED_TOOLS', 'ALWAYS_LOADED_WEB_TOOLS',
    'ALWAYS_LOADED_PROJECT_TOOLS', 'ALWAYS_LOADED_AGENT_TOOLS',
    'ALWAYS_LOADED_CRON_TOOLS', 'TOOL_KEYWORDS', 'EXPLICIT_SCHEMAS',
    '_select_tool_names', 'apply_project_workspace_tools', '_select_skill_text',
    '_build_system_prompt',
]

# ---------------------------------------------------------------------------
# Skill document loader — skills/*/SKILL.md (Agent Skills standard)
# ---------------------------------------------------------------------------

SKILLS_DIR = Path(__file__).parents[2] / "skills"


@dataclass
class SkillDoc:
    name: str
    description: str
    body: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    trigger_tools: set[str] = field(default_factory=set)
    trigger_keywords: list[str] = field(default_factory=list)
    inject_priority: int = 0


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, m.group(2).strip()


def _load_skill_docs() -> list[SkillDoc]:
    skills: list[SkillDoc] = []
    if not SKILLS_DIR.exists():
        return skills
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue
        meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        name = meta.get("name") or d.name
        description = meta.get("description") or ""
        if not description:
            description = body.strip().split("\n")[0] if body else name
        raw_trigger_tools = meta.get("trigger-tools") or meta.get("trigger_tools") or []
        if isinstance(raw_trigger_tools, str):
            trigger_tools = {x.strip() for x in raw_trigger_tools.split(",") if x.strip()}
        elif isinstance(raw_trigger_tools, list):
            trigger_tools = {str(x).strip() for x in raw_trigger_tools if str(x).strip()}
        else:
            trigger_tools = set()
        raw_trigger_keywords = meta.get("trigger-keywords") or meta.get("trigger_keywords") or []
        if isinstance(raw_trigger_keywords, str):
            trigger_keywords = [x.strip() for x in raw_trigger_keywords.split(",") if x.strip()]
        elif isinstance(raw_trigger_keywords, list):
            trigger_keywords = [str(x).strip() for x in raw_trigger_keywords if str(x).strip()]
        else:
            trigger_keywords = []
        inject_priority = int(meta.get("inject-priority") or meta.get("inject_priority") or 0)
        skills.append(SkillDoc(
            name=name,
            description=description,
            body=body,
            disable_model_invocation=bool(meta.get("disable-model-invocation")),
            user_invocable=meta.get("user-invocable", True) is not False,
            trigger_tools=trigger_tools,
            trigger_keywords=trigger_keywords,
            inject_priority=inject_priority,
        ))
    return skills


_SKILL_DOCS = _load_skill_docs()

# ---------------------------------------------------------------------------
# Tool groups — for context-aware progressive disclosure of tool schemas
# ---------------------------------------------------------------------------

TOOL_GROUPS: dict[str, list[str]] = {
    "evidence": [
        "case_create", "case_list", "case_get", "case_events", "case_link_add", "case_graph",
        "artifact_add", "artifact_read", "evidence_add",
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory", "external_tool_inventory",
    ],
    "workflow": [
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory",
        "external_tool_inventory", "external_tool_install_plan", "plan_pentest", "tool_search",
        "skill_list", "skill_search", "skill_read",
        "set_todos", "add_todos", "update_todo", "complete_todo_with_refs", "create_validation_todo", "assessment_plan_snapshot", "list_todos", "clear_todos",
        "user_todo_create", "user_todo_list", "user_todo_update", "user_todo_complete", "user_todo_delete",
        "user_profile_read", "user_profile_suggest_update", "user_profile_apply_update",
        "business_surface_extract", "business_test_plan", "business_risk_select",
        "configure_auto_archive_tools", "show_data_table", "show_structured_card", "show_file_resource_view",
    ],
    "mcp": [
        "mcp_marketplace_search", "mcp_marketplace_get",
        "mcp_github_candidate", "mcp_provision_plan", "mcp_revoke_plan",
        "mcp_server_list", "mcp_server_add", "mcp_server_delete", "mcp_server_health",
        "mcp_server_list_tools", "mcp_server_list_resources",
        "mcp_server_call_tool", "mcp_server_read_resource",
        "mcp_stdio_health", "mcp_stdio_list_tools", "mcp_stdio_call_tool",
        "mcp_stdio_list_resources", "mcp_stdio_read_resource",
    ],
    "codebase": [
        "case_create", "case_get", "case_link_add", "case_graph", "artifact_add", "artifact_read", "evidence_add",
        "codebase_scope_set", "codebase_scope_get", "codebase_tree",
        "codebase_search", "codebase_read", "codebase_entrypoints",
        "codebase_evidence_add",
        "codebase_write_file", "codebase_patch_file", "codebase_delete_file",
        "codebase_stat_file", "codebase_grep_file", "codebase_list_files",
        "codebase_open_file", "codebase_reveal_in_folder",
        "codebase_run_python_script", "codebase_run_python_snippet",
        "codebase_run_shell_script", "codebase_run_shell_snippet",
        "business_surface_extract", "business_test_plan", "business_risk_select",
    ],
    "development": [
        "codebase_scope_set", "codebase_scope_get", "codebase_tree", "codebase_search", "codebase_read",
        "codebase_write_file", "codebase_patch_file", "codebase_delete_file",
        "codebase_stat_file", "codebase_grep_file", "codebase_list_files",
        "codebase_open_file", "codebase_reveal_in_folder",
        "codebase_run_python_script", "codebase_run_python_snippet",
        "codebase_run_shell_script", "codebase_run_shell_snippet",
        "session_sandbox_status", "sandbox_write_file", "sandbox_read_file", "sandbox_patch_file",
        "sandbox_stat_file", "sandbox_grep_file", "sandbox_list_files", "sandbox_pip_install",
        "run_python_script", "run_python_snippet", "run_shell_script", "run_shell_snippet",
        "shell_session_start", "shell_session_exec", "shell_session_write",
        "shell_session_list", "shell_session_read", "shell_session_stop",
        "sandbox_open_file", "sandbox_reveal_in_folder", "export_workspace_bundle",
        "parse_document",
    ],
    "productivity": [
        "user_todo_create", "user_todo_list", "user_todo_update", "user_todo_complete", "user_todo_delete",
        "session_sandbox_status", "sandbox_write_file", "sandbox_read_file", "sandbox_patch_file",
        "sandbox_stat_file", "sandbox_grep_file", "sandbox_list_files", "sandbox_open_file",
        "sandbox_reveal_in_folder", "export_workspace_bundle", "desktop_notify",
        "parse_document",
    ],
    "cron": [
        "cron_list", "cron_runs", "cron_upsert", "cron_set_enabled",
        "cron_delete", "cron_grant_high_risk",
    ],
    "channels": [
        "channel_list_targets", "channel_reply", "channel_send_message", "channel_send_file",
        "channel_delivery_status",
    ],
    "web": [
        "web_request_parse", "web_request_mutate", "web_request_replay",
        "web_response_diff", "web_request_to_curl",
        "api_inventory_build", "api_inventory_diff", "api_request_select", "api_spec_import",
        "business_surface_extract", "business_test_plan", "business_risk_select",
        "graphql_probe", "graphql_batch_probe",
        "websocket_handshake_probe", "websocket_connect_probe", "websocket_frame_analyze",
        "websocket_replay_plan", "websocket_send_receive", "websocket_authz_probe",
        "grpc_web_analyze", "protobuf_field_preview",
        "session_vault_save", "session_vault_list", "session_vault_get",
        "auth_matrix_from_vault", "js_signature_oracle_plan",
    ],
    "mobile": [
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory", "case_link_add", "case_graph", "artifact_add", "artifact_read", "evidence_add",
        "mobile_dast_tool_inventory", "appium_status", "mobile_dast_plan",
        "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import",
        "android_apk_analyze", "ios_ipa_analyze", "ios_class_dump",
        "android_jadx_decompile", "android_apktool_decode",
        "android_apktool_build", "android_apk_sign", "android_apk_validate",
        "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
        "android_objection_run",
        "mobile_frida_recipe_list", "mobile_frida_recipe_apply",
        "frida_list_devices", "frida_list_processes",
        "frida_attach", "frida_spawn", "frida_run_script", "frida_session_list", "frida_session_stop",
        "reverse_job_list", "reverse_job_create", "reverse_job_get", "reverse_job_update",
        "reverse_artifact_add", "reverse_artifact_read", "reverse_finding_add",
        "reverse_job_open_dir", "reverse_artifact_reveal",
        "reverse_artifact_copy_to", "reverse_artifact_extract_strings", "reverse_artifact_grep",
        "reverse_job_link_proxy_flow", "reverse_job_export",
    ],
    "cloud": [
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory",
        "external_tool_inventory", "case_create", "artifact_add", "artifact_read", "evidence_add",
        "cloud_tool_inventory", "trivy_cloud_plan", "trivy_cloud_scan",
        "prowler_cloud_plan", "prowler_cloud_scan",
        "kubectl_readonly_plan", "kubectl_readonly_run",
        "iac_tool_inventory", "trivy_iac_plan", "trivy_iac_scan",
        "checkov_scan_plan", "checkov_run_scan",
    ],
    "directory": [
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory",
        "external_tool_inventory", "case_create", "artifact_add", "artifact_read", "evidence_add",
        "ad_tool_inventory", "ad_sharphound_import", "ad_pingcastle_import",
        "ad_certipy_find_plan", "ad_certipy_find_run",
    ],
    "binary": [
        "domain_inventory", "asset_ref_normalize", "tool_adapter_inventory", "case_link_add", "case_graph", "artifact_add", "artifact_read", "evidence_add",
        "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import", "windows_pe_analyze", "elf_analyze",
        "installer_unpack",
        "binary_capa_analyze", "binary_yara_scan",
        "binary_list_functions", "binary_disassemble", "binary_decompile", "binary_patch",
        "frida_list_devices", "frida_list_processes",
        "frida_attach", "frida_spawn", "frida_run_script", "frida_session_list", "frida_session_stop",
        "reverse_job_list", "reverse_job_create", "reverse_job_get", "reverse_job_update",
        "reverse_artifact_add", "reverse_artifact_read", "reverse_finding_add",
        "reverse_job_open_dir", "reverse_artifact_reveal",
        "reverse_artifact_copy_to", "reverse_artifact_extract_strings", "reverse_artifact_grep",
        "reverse_job_link_proxy_flow", "reverse_job_export",
    ],
    "tasks": [
        "list_tasks", "get_task_detail", "get_task_findings",
        "get_finding_detail", "get_task_events", "wait_for_task",
        "create_scan", "cancel_task", "delete_task", "rescan_task",
        "analyze_findings", "export_task_report", "export_workspace_bundle",
    ],
    "proxy": [
        "get_proxy_status", "get_proxy_traffic", "get_proxy_findings", "get_proxy_flow_detail",
        "get_proxy_ca_info", "install_proxy_ca", "uninstall_proxy_ca",
        "export_proxy_har", "proxy_import_har", "proxy_import_burp_log",
        "save_proxy_flow_body", "open_proxy_exports_dir", "open_proxy_captures_dir",
        "start_proxy", "stop_proxy",
        "proxy_search_traffic", "proxy_decode_flow",
        "proxy_decrypt_rules_list", "proxy_decrypt_rule_test",
        "proxy_decrypt_rule_save", "proxy_decrypt_script_rule_save", "proxy_decrypt_rule_delete",
        "proxy_rewrite_rules_list", "proxy_rewrite_rule_test",
        "proxy_rewrite_rule_save", "proxy_rewrite_rule_delete",
        "proxy_breakpoint_config", "proxy_breakpoints_list", "proxy_breakpoint_resolve",
    ],
    "settings": [
        "get_settings", "update_settings", "test_ai_connection",
    ],
    "team": [
        "team_list", "team_get", "team_chat_get", "team_message_send",
        "team_create", "team_update", "team_delete",
        "team_add_member", "team_member_get", "team_member_update",
        "team_member_send_input", "team_member_control",
    ],
    "network": [
        "domain_inventory", "artifact_add", "artifact_read", "evidence_add",
        "network_tool_inventory", "ping_host", "dns_lookup", "trace_route",
        "http_curl", "http_request_impersonate", "http_impersonate_status",
        "whois_lookup", "nmap_scan", "network_discover_hosts", "network_scan_ports",
        "web_scan_tool_inventory",
        "network_fingerprint_services", "network_profile_host", "network_assess_exposure",
        "network_protocol_assess", "vuln_intel_match", "weak_credential_audit_plan",
        "pcap_tool_inventory", "tshark_status",
        "pcap_summary_plan", "pcap_summary",
        "pcap_filter_plan", "pcap_filter",
        "pcap_http_extract_plan", "pcap_http_extract",
        "fetch_page_source", "extract_page_text",
        "codec_transform", "hex_view", "hash_text", "hmac_text", "jwt_decode",
    ],
    "supply_chain": [
        "domain_inventory", "tool_adapter_inventory", "external_tool_inventory",
        "case_create", "artifact_add", "artifact_read", "evidence_add",
        "sca_tool_inventory", "syft_status", "trivy_status", "osv_status", "osv_scanner_status",
        "syft_plan_inventory", "syft_run_inventory",
        "trivy_plan_scan", "trivy_run_scan",
        "osv_plan_scan", "osv_run_scan", "sbom_diff",
    ],
    "wireless": [
        "domain_inventory", "tool_adapter_inventory", "external_tool_inventory",
        "case_create", "artifact_add", "artifact_read", "evidence_add",
        "wireless_preflight", "wireless_wlan_inventory",
        "wireless_parse_netsh_interfaces", "wireless_parse_netsh_networks",
        "wireless_parse_nmcli", "wireless_pcap_plan", "wireless_action_check",
    ],
    "iot": [
        "domain_inventory", "tool_adapter_inventory", "external_tool_inventory",
        "case_create", "artifact_add", "artifact_read", "evidence_add",
        "iot_firmware_preflight", "firmware_path_validate",
        "firmware_rootfs_grep", "firmware_service_map", "firmware_analysis_plan",
        "binary_yara_scan", "binary_capa_analyze", "binary_decompile",
    ],
    "hardware": [
        "domain_inventory", "tool_adapter_inventory", "external_tool_inventory",
        "case_create", "artifact_add", "artifact_read", "evidence_add",
        "hardware_preflight", "hardware_uart_parse", "hardware_flash_diff",
        "hardware_sigrok_plan", "hardware_readonly_plan", "hardware_action_check",
    ],
    "web_lookup": [
        "web_search", "github_search",
    ],
    "probes": [
        "http_probe", "payload_probe", "browser_probe", "browser_evaluate",
        "browser_status", "browser_reset", "browser_profile_list",
        "browser_system_inventory", "browser_install_engine",
        # CDP attach: drive the user's real browser session
        "browser_attach_status", "browser_launch_for_attach",
        "browser_attach_existing", "browser_detach", "browser_resume_control",
        "browser_list_tabs", "browser_open_tab", "browser_use_tab", "browser_screenshot",
        "browser_close_owned_tabs", "browser_mark_tabs_owned",
    ],
    "reverse": [
        "js_fetch_asset", "js_asset_inventory", "js_bundle_analyze", "js_beautify",
        "js_extract_strings", "js_search", "js_slice_context", "source_map_discover",
        "source_map_recover", "webpack_modules_extract", "webpack_module_read",
        "js_deobfuscate", "js_decode_literal", "js_string_array_map",
        "js_constant_fold_preview", "js_control_flow_hints", "js_eval_unpack",
        "js_api_extract", "js_signature_candidates", "js_dependency_trace",
        "js_crypto_detect", "js_request_rebuild_hint", "browser_trace",
        "browser_trace_crypto", "browser_trace_form", "browser_snapshot_state",
        "browser_instrument_click", "browser_network_replay_plan",
        "browser_hook_function", "browser_hook_property",
        "browser_hook_list", "browser_hook_remove",
        "browser_hook_drain", "browser_hook_reattach",
        "reverse_job_list", "reverse_job_create", "reverse_job_get",
        "reverse_job_update", "reverse_job_trash", "reverse_job_events",
        "reverse_artifact_add", "reverse_artifact_read", "reverse_artifact_remove",
        "reverse_finding_add", "reverse_finding_read", "reverse_finding_remove",
        "reverse_hook_hits",
        "reverse_oracle_add", "reverse_oracle_list", "reverse_oracle_read",
        "reverse_oracle_compare",
        "reverse_reproduce_add", "reverse_reproduce_read",
        "reverse_recipe_list", "reverse_recipe_apply",
        "reverse_job_open_dir", "reverse_artifact_reveal",
        "reverse_artifact_copy_to", "reverse_artifact_extract_strings", "reverse_artifact_grep",
        "reverse_job_link_proxy_flow", "reverse_job_export",
        "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import",
        "android_apk_analyze", "ios_ipa_analyze", "ios_class_dump", "windows_pe_analyze", "elf_analyze",
        "installer_unpack",
        "android_jadx_decompile", "android_apktool_decode",
        "android_apktool_build", "android_apk_sign", "android_apk_validate",
        "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
        "binary_capa_analyze", "binary_yara_scan",
        "binary_list_functions", "binary_disassemble", "binary_decompile", "binary_patch",
        "android_objection_run",
        "mobile_frida_recipe_list", "mobile_frida_recipe_apply",
        "frida_list_devices", "frida_list_processes",
        "frida_attach", "frida_spawn", "frida_run_script", "frida_session_list", "frida_session_stop",
        "js_deobfuscate_heavy", "js_unpack_bundle", "js_detect_bundler",
        "source_map_recover_indexed", "source_map_decode_data_uri",
        # Managed WASM toolchain lives under data_root/tools/runtimes (not
        # reverse_toolchain_install). Keep status + install together so
        # "install WABT" turns can call runtime_install(runtime='wabt').
        "runtime_status", "runtime_install",
        # Python-first reverse engineering: run JS without a browser,
        # reproduce crypto in Python, ship one-click PoCs. Prefer these
        # over sandbox_eval_js + create_poc_page when the operation is
        # pure computation (decoders, signature builders, decryptors).
        "run_node_script", "run_js_function",
        # Hosted runtime: like a CDP browser hub for local JS. Load large
        # bundles once, install local hooks, drain oracle records, then
        # reproduce in Python.
        "js_runtime_status", "js_runtime_create", "js_runtime_close",
        "js_runtime_stop", "js_runtime_load", "js_runtime_eval",
        "js_runtime_call", "js_runtime_hook", "js_runtime_drain",
        "js_runtime_logs", "js_runtime_sources", "js_runtime_patches",
        "js_runtime_hooks", "js_runtime_patch", "js_runtime_instrument",
        "js_runtime_snapshot", "js_runtime_restore", "js_runtime_sample_profile",
        "js_runtime_to_reproducer_hint", "python_reproducer_verify",
        "crypto_reproduce", "create_python_poc",
        "captcha_detect", "captcha_script_analyze", "captcha_runtime_trace",
        "anti_bot_fingerprint_detect", "captcha_report", "sandbox_eval_js",
        "sandbox_test_function", "sandbox_load_library", "sandbox_compare_outputs",
        "sandbox_status", "sandbox_reset", "json_path_extract", "querystring_parse_build",
        "header_cookie_parse", "wasm_inventory", "wasm_decompile_hint", "har_import_analyze",
        "proxy_import_har", "proxy_import_burp_log",
        "api_inventory_build", "api_inventory_diff", "api_request_select", "api_spec_import",
        "business_surface_extract", "business_test_plan", "business_risk_select",
        "graphql_probe", "graphql_batch_probe",
        "websocket_handshake_probe", "websocket_connect_probe", "websocket_frame_analyze",
        "websocket_replay_plan", "websocket_send_receive", "websocket_authz_probe",
        "grpc_web_analyze", "protobuf_field_preview",
        "js_ast_inventory", "js_ast_string_array_decode", "js_function_dependency_graph",
        "js_hook_template_generate",
        # P5 WASM toolchain — path-based, artifacts to disk, never push
        # raw binary through the chat context.
        "browser_save_wasm",
        "wasm_inspect", "wasm_disassemble", "wasm_decompile",
        "wasm_strings", "wasm_call_graph",
        "js_signature_oracle_plan", "create_poc_page", "open_poc_page",
    ],
    "session_sandbox": [
        "session_sandbox_status", "runtime_status", "runtime_install",
        "discover_shell_environments", "discover_python_environments",
        "sandbox_write_file", "sandbox_read_file", "sandbox_patch_file",
        "sandbox_stat_file", "sandbox_grep_file", "sandbox_list_files",
        "sandbox_delete_file", "sandbox_reset_files", "sandbox_pip_install",
        "run_python_script",
        "run_shell_script", "run_python_snippet", "run_shell_snippet",
        "run_host_python_script", "run_host_python_snippet",
        "shell_session_start", "shell_session_exec", "shell_session_write",
        "shell_session_list", "shell_session_read", "shell_session_stop",
        "sandbox_open_file", "sandbox_reveal_in_folder", "export_workspace_bundle",
    ],
    "desktop": [
        "desktop_observe",
        "desktop_launch_app",
        "desktop_notify",
        "desktop_action_session_start", "desktop_action_session_stop", "desktop_action_session_status",
        "desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey", "desktop_focus_window",
        "desktop_uia_find", "desktop_uia_action",
    ],
    "user_todos": [
        "user_todo_create", "user_todo_list", "user_todo_update", "user_todo_complete", "user_todo_delete",
    ],
    "attack": [
        "web_request_parse", "web_request_mutate", "web_request_replay",
        "web_response_diff", "web_request_to_curl",
        "api_inventory_build", "api_inventory_diff", "api_request_select", "api_spec_import",
        "business_surface_extract", "business_test_plan", "business_risk_select",
        "graphql_probe", "graphql_batch_probe",
        "websocket_handshake_probe", "websocket_connect_probe", "websocket_frame_analyze",
        "websocket_replay_plan", "websocket_send_receive", "websocket_authz_probe",
        "grpc_web_analyze", "protobuf_field_preview",
        "sqli_probe_param", "sqli_boolean_diff", "sqli_time_probe",
        "sqlmap_status", "sqlmap_install_update", "sqlmap_plan_scan", "sqlmap_run_scan",
        "nuclei_status", "nuclei_plan_scan", "nuclei_run_scan",
        "ffuf_status", "ffuf_plan_scan", "ffuf_run_scan",
        "web_scan_tool_inventory",
        "sqli_union_plan",
        "nosql_injection_probe", "oob_payload_generate", "oob_check_events", "oob_status", "oob_start", "oob_stop",
        "session_vault_save", "session_vault_list", "session_vault_get",
        "auth_session_inventory", "idor_compare_request", "role_matrix_probe", "auth_matrix_from_vault", "business_state_probe",
        "upload_surface_discover", "upload_payload_matrix", "upload_verify_access",
        "ssrf_probe_url_param", "ssrf_redirect_chain_test", "ssrf_metadata_template",
        "cors_matrix_probe", "cors_exploit_verify", "csrf_poc_generate", "oauth_flow_analyze", "jwt_attack_surface",
        "saml_surface_analyze", "http_desync_probe", "cache_poison_probe", "raw_http_send",
        "pentest_plan_chain", "pentest_verify_finding", "post_exploit_map_impact",
        "post_exploit_plan_actions", "post_exploit_lab_plan", "post_exploit_lab_generate",
        "weak_credential_audit_plan", "weak_credential_check",
    ],
    "always": [
        "get_app_snapshot", "list_checks", "launch_chrome", "plan_pentest",
        "tool_search", "invoke_catalogue_tool", "skill_list", "skill_search", "skill_read", "domain_inventory", "asset_ref_normalize",
        "tool_adapter_inventory", "external_tool_inventory",
        "search_memory", "search_all_memory", "read_assistant_session",
        "list_session_messages", "read_session_message",
        "vision_inspect_image",
        # Document parse — local pdf/docx always; MinerU when enabled in settings.
        "parse_document",
        "set_todos", "add_todos", "update_todo", "complete_todo_with_refs", "create_validation_todo", "assessment_plan_snapshot", "list_todos", "clear_todos",
        "configure_auto_archive_tools", "show_data_table", "show_structured_card", "show_file_resource_view",
        "codebase_scope_get",
        "kb_search", "kb_read", "kb_save", "kb_update", "kb_supersede", "kb_delete",
        "kb_suggest_cleanup",
        "user_profile_read", "user_profile_suggest_update", "user_profile_apply_update",
        "stop_for_user_decision", "stop_for_user_decision_batch", "wait_for_duration",
        "run_subagent", "spawn_agent", "spawn_team", "send_agent_input", "pause_agent", "resume_agent",
        "wait_for_agents", "list_agents", "get_agent", "close_agent",
        "background_job_status", "background_job_list", "background_job_cancel",
        # Generic file I/O. These stay in the always keyword bucket so
        # skill matching still sees them. They are not on the frozen
        # tools[] seed — discover them with tool_search.
        "read_text_file", "stat_text_file", "grep_text", "grep_text_dir",
        "read_text_slice", "extract_strings", "patch_text_file", "write_text_file",
        # MCP discovery — risk=low, no confirmation. MCP child tools are
        # NEVER registered as top-level ToolRegistry names (see plan §不做
        # 的事), so the only way the model can find out what external
        # capabilities the user has wired up is to actively enumerate.
        # Keeping the list call always-available means the model can
        # answer "do you have X?" questions correctly instead of denying
        # capabilities the user just configured.
        "mcp_marketplace_search", "mcp_marketplace_get", "mcp_server_list",
        # Paired communication targets are first-class Agent capabilities.
        # Sending remains confirmation-gated; discovery is read-only.
        "channel_list_targets", "channel_reply", "channel_send_message", "channel_send_file",
        "channel_delivery_status",
    ],
}

# Byte-stable OpenAI/Codex tools[] seed. Distinct from TOOL_GROUPS["always"],
# which remains the keyword-selection bucket and must not be thinned — models
# pick the wrong function once 30–50 schemas are visible at once.
ALWAYS_LOADED_TOOLS: tuple[str, ...] = (
    "tool_search",
    "invoke_catalogue_tool",
    "skill_search",
    "skill_read",
    "get_app_snapshot",
    "search_memory",
    "set_todos",
    "list_todos",
    "stop_for_user_decision",
    "team_room_pass",
    "mcp_server_list",
    "vision_inspect_image",
    "add_todos",
    "update_todo",
    "complete_todo_with_refs",
    "clear_todos",
)
ALWAYS_LOADED_AGENT_TOOLS: tuple[str, ...] = (
    "run_subagent",
    "spawn_agent",
    "send_agent_input",
    "pause_agent",
    "resume_agent",
    "wait_for_agents",
    "list_agents",
    "get_agent",
    "close_agent",
)
ALWAYS_LOADED_CRON_TOOLS: tuple[str, ...] = (
    "cron_list",
    "cron_runs",
    "cron_upsert",
    "cron_set_enabled",
    "cron_delete",
)
ALWAYS_LOADED_WEB_TOOLS: tuple[str, ...] = (
    "web_search",
    "github_search",
)
ALWAYS_LOADED_PROJECT_TOOLS: tuple[str, ...] = (
    "codebase_search",
    "codebase_read",
    "codebase_tree",
    "codebase_run_shell_snippet",
)

TOOL_KEYWORDS: dict[str, list[str]] = {
    "tasks": [
        r"\bscan\b", r"\btask\b", r"\bfinding\b", r"\bseverity\b",
        r"\breport\b", r"\brescan\b", r"\bexport\b", r"\bdeliverable\b",
        r"\b扫描\b", r"\b任务\b", r"\b漏洞\b", r"\b发现\b", r"\b报告\b",
        r"\b导出\b", r"\b交付\b", r"\b复现流程\b", r"\b重跑\b",
    ],
    "proxy": [
        r"\bproxy\b", r"\b代理\b", r"\btraffic\b", r"\b流量\b",
        r"\bpassive\b", r"\b被动\b",
    ],
    "settings": [
        r"\bsetting\b", r"\bconfig\b", r"\bendpoint\b", r"\bmodel\b", r"\bprofile\b",
        r"\bpreference\b", r"\bhabit\b", r"\bstyle\b",
        r"\b设置\b", r"\b配置\b", r"\b端点\b", r"\b模型\b", r"\b用户习惯\b",
        r"\b偏好\b", r"\b习惯\b", r"\b风格\b",
    ],
    "team": [
        r"(?<![a-z0-9])teams?(?![a-z0-9])", r"(?<![a-z0-9])squads?(?![a-z0-9])",
        r"小队|团队|组队|战队|分队|攻坚队|审计队|成员",
    ],
    "network": [
        r"\bnmap\b", r"\bmtr\b", r"\btraceroute\b", r"\btracert\b",
        r"\bping\b", r"\bdns\b", r"\bdig\b", r"\bnslookup\b", r"\bcurl\b",
        r"\bwhois\b", r"\bhtml\b",
        r"\bpcap\b", r"\bcidr\b", r"\bservice\b", r"\bnetwork\b", r"\bport\b",
        r"\bfingerprint\b", r"\bbanner\b", r"\bos\b", r"\bhost profile\b",
        r"\bcve\b", r"\bcvss\b", r"\bepss\b", r"\bcpe\b", r"\bprotocol\b",
        r"\bweak password\b", r"\bdefault password\b", r"\bcredential audit\b",
        r"\bhash\b", r"\bhmac\b", r"\bjwt\b", r"\bbase64\b", r"\bdecode\b", r"\bencode\b",
        r"\b网络工具\b", r"\b端口\b", r"\b路由跟踪\b", r"\b服务探测\b",
        r"\b漏洞情报\b", r"\b协议\b", r"\b版本漏洞\b", r"\b弱口令\b", r"\b默认口令\b",
        r"\b网页\b", r"\b加密\b", r"\b解密\b", r"\b编码\b", r"\b解码\b", r"\b哈希\b",
        r"\b系统\b", r"\b网络\b", r"\b服务\b", r"\b抓包\b", r"\b指纹\b",
    ],
    "evidence": [
        r"\bcase\b", r"\bevidence\b", r"\bartifact\b", r"\btimeline\b",
        r"\b证据\b", r"\b案例\b", r"\b工件\b", r"\b时间线\b", r"\b复盘\b",
    ],
    "workflow": [
        r"\bskill\b", r"\bworkflow\b", r"\bplaybook\b", r"\bmethodology\b",
        r"\bcron\b", r"\bschedule\b", r"\bloop\b", r"\b定时\b", r"\b周期\b",
        r"\bdefense\b", r"\bdefense mode\b", r"\battack surface\b",
        r"\b防御\b", r"\b主动防御\b", r"\b攻击面\b", r"\b巡检\b",
        r"\b流程\b", r"\b工作流\b", r"\b方法论\b", r"\b能力文档\b",
    ],
    "user_todos": [
        r"\buser todo\b", r"\bpersonal todo\b", r"\bremind me\b",
        r"\b待办\b", r"\b个人待办\b", r"\b提醒我\b", r"\b记一下\b",
    ],
    "desktop": [
        r"\bdesktop\b", r"\bscreenshot\b", r"\bscreen\b", r"\bwindow list\b",
        r"\bclick\b", r"\bkeyboard\b", r"\bhotkey\b",
        r"\b桌面\b", r"\b截图\b", r"\b窗口\b", r"\b前台窗口\b", r"\b鼠标\b", r"\b键盘\b",
        r"\b打开\b", r"\b发送\b", r"\b发消息\b", r"\b聊天\b", r"\bqq\b", r"\bwechat\b", r"\b微信\b",
        r"\b本地应用\b", r"\b客户端\b", r"\b模拟点击\b", r"\b自动化\b",
        r"\bopen\s+(qq|wechat|slack|telegram)\b",
    ],
    "mcp": [
        r"\bmcp\b", r"\bmodel context protocol\b", r"\bexternal tool\b",
        r"\b外部工具\b", r"\b外部服务\b", r"\b外部 mcp\b", r"\b接入 mcp\b",
    ],
    "channels": [
        r"\bchannel\b", r"\bqq\b", r"\btelegram\b", r"\bdiscord\b", r"\bslack\b",
        r"\bfeishu\b", r"\bdingtalk\b", r"\bwecom\b", r"\bwebhook\b",
        r"\b通讯渠道\b", r"\b通讯接入\b", r"\b飞书\b", r"\b钉钉\b",
        r"\b企业微信\b", r"\b发给我\b", r"\b发送文件\b", r"\b回传文件\b",
    ],
    "codebase": [
        r"\bcodebase\b", r"\brepository\b", r"\brepo\b", r"\bsource code\b", r"\bsast\b",
        r"\bwhite[- ]?box\b", r"\bcode review\b", r"\bstatic audit\b", r"\bsource audit\b",
        r"\b源码\b", r"\b代码审计\b", r"\b白盒\b", r"\b白盒测试\b", r"\b源码审计\b",
        r"\b仓库\b", r"\b项目目录\b", r"\b代码库\b", r"\b静态审计\b",
    ],
    "development": [
        r"\bunit test\b", r"\bwrite code\b", r"\bcreate (a )?(script|app|tool)\b",
        r"\bbuild (a |an |the )?(script|app|tool|program|project)\b",
        r"\bsoftware development\b", r"\brefactor (the )?(code|script|function)\b",
        r"\bAGENTS\.md\b", r"\bCLAUDE\.md\b", r"\brepo (rules|conventions)\b",
        r"\b编写代码\b", r"\b写代码\b", r"\b写个(脚本|程序|工具)\b", r"\b实现功能\b",
        r"\b修复 bug\b", r"\b单元测试\b", r"\b自动化脚本\b", r"\b生成程序\b",
        r"\b仓库规范\b", r"\b开发规范\b", r"\bAgent 规范\b",
    ],
    "productivity": [
        r"\bdaily task\b", r"\bproductivity\b", r"\borganize files?\b", r"\brename files?\b",
        r"\bconvert files?\b", r"\bsummarize (a )?(document|file)\b",
        r"\bparse (a )?(pdf|docx|document)\b", r"\bpdf\b", r"\bdocx\b", r"\bmineru\b",
        r"\b日常任务\b", r"\b整理文件\b", r"\b批量重命名\b", r"\b文件转换\b",
        r"\b处理文件\b", r"\b生成表格\b", r"\b整理资料\b", r"\b待办事项\b",
        r"\b解析(文档|pdf|docx)\b", r"\b文档解析\b", r"\b提取(文本|正文)\b",
    ],
    "web": [
        r"\breplay\b", r"\bmutation\b", r"\bmutate\b", r"\brequest diff\b",
        r"\bapi inventory\b", r"\bgraphql\b", r"\bgql\b", r"\bwebsocket\b",
        r"\bgrpc[- ]?web\b", r"\bprotobuf\b", r"\bproto\b",
        r"\bsession vault\b", r"\bauth matrix\b", r"\bbusiness surface\b", r"\bbusiness logic\b", r"\b签名重放\b",
        r"\b接口盘点\b", r"\b接口清单\b", r"\b请求重放\b", r"\b请求变异\b",
        r"\b响应对比\b", r"\b身份矩阵\b", r"\b业务点\b", r"\b业务画像\b", r"\b业务逻辑\b",
    ],
    "mobile": [
        r"\bapk\b", r"\bipa\b", r"\bandroid\b", r"\bios\b", r"\bfrida\b",
        r"\bjadx\b", r"\bapktool\b", r"\bapksigner\b", r"\bzipalign\b",
        r"\bobjection\b", r"\bappium\b", r"\bmobile dast\b", r"\bapp\b",
        r"\b移动端\b", r"\b安卓\b", r"\b苹果\b", r"\b应用逆向\b", r"\b移动动态测试\b",
    ],
    "cloud": [
        r"\bcloud\b", r"\baws\b", r"\bazure\b", r"\bgcp\b", r"\bprowler\b",
        r"\bkubernetes\b", r"\bk8s\b", r"\bkubectl\b", r"\biac\b", r"\bterraform\b",
        r"\bcheckov\b", r"\b云安全\b", r"\b云账户\b", r"\b容器镜像\b",
        r"\b集群\b", r"\b基础设施即代码\b",
    ],
    "directory": [
        r"\bactive directory\b", r"\bad cs\b", r"\bcertipy\b", r"\bsharphound\b",
        r"\bbloodhound\b", r"\bpingcastle\b", r"\bldap\b", r"\bdomain controller\b",
        r"\b域控\b", r"\b活动目录\b", r"\b域安全\b", r"\b证书服务\b",
    ],
    "binary": [
        r"\bexe\b", r"\bdll\b", r"\belf\b", r"\bpe\b", r"\byara\b",
        r"\bcapa\b", r"\bghidra\b", r"\bbinary\b",
        r"\b二进制\b", r"\b程序逆向\b", r"\b反汇编\b", r"\b反编译\b",
    ],
    "web_lookup": [
        r"\bweb search\b", r"\bsearch web\b", r"\bgoogle\b", r"\bddg\b", r"\bddgs\b",
        r"\bwriteup\b", r"\bwalkthrough\b", r"\b公开资料\b", r"\b外部搜索\b",
        r"\b网上搜索\b", r"\b搜索引擎\b", r"\b搜一下网上\b",
        r"\bnews\b", r"\bheadline\b", r"\bcurrent events\b",
        r"\b新闻\b", r"\b头条\b", r"\b今日\b", r"\b热点\b",
        r"\b上网\b", r"\b互联网\b", r"\b联网\b", r"\b能上网\b", r"\bonline\b",
        r"\bgithub\b", r"\brepo\b", r"\brepository\b", r"\bpoc\b", r"\bexploit\b",
    ],
    "probes": [
        r"\bprobe\b", r"\bpayload\b", r"\bhttp\b", r"\bbrowser\b",
        r"\bfingerprint\b", r"\bua\b", r"\buser[- ]?agent\b", r"\bstealth\b",
        r"\b临时\b", r"\b探测\b", r"\b请求\b", r"\b载荷\b",
        r"\b指纹\b", r"\b伪装\b", r"\b伪造\b", r"\bUA\b", r"\b浏览器伪装\b",
    ],
    "reverse": [
        r"\breverse\b", r"\bdeobfuscat", r"\bwebpack\b", r"\bsourcemap\b", r"\bsource map\b",
        r"\bbundle\b", r"\bchunk\b", r"\bcaptcha\b", r"\bwasm\b", r"\bwabt\b",
        r"\bwasm2wat\b", r"\bwasm-decompile\b", r"\bhar\b",
        r"\bwebcrypto\b", r"\bsignature\b", r"\bsandbox\b", r"\binstrument\b",
        r"\bpoc\b", r"\bexploit html\b", r"\bone-click\b",
        r"\bjadx\b", r"\bapktool\b", r"\bapksigner\b", r"\bzipalign\b",
        r"\breverse.?toolchain\b", r"\btoolchain.?install\b",
        r"\b逆向\b", r"\b反混淆\b", r"\b源码还原\b", r"\b验证码\b", r"\b签名\b",
        r"\b加密参数\b", r"\b打包\b", r"\b沙盒\b", r"\b运行时\b", r"\b一键攻击\b",
        r"\b攻击页面\b", r"\b写入网页\b", r"\b打开网页\b",
        r"\b逆向工具\b", r"\b工具链\b",
    ],
    "session_sandbox": [
        r"\bscript\b", r"\bsnippet\b", r"\bpython\b", r"\bpowershell\b", r"\bcmd\b",
        r"\bbash\b", r"\bshell\b", r"\bfile\b", r"\bwrite file\b", r"\bsave\b",
        r"\brun\b", r"\bexecute\b", r"\b脚本\b", r"\b片段\b", r"\b文件\b",
        r"\b写入\b", r"\b保存\b", r"\b执行\b", r"\b运行\b", r"\b沙盒\b",
        r"\b工作区\b", r"\bpoc代码\b", r"\bpoc 文件\b",
        r"\b解释器\b", r"\b环境\b",
        r"\bwabt\b", r"\bwasm\b", r"\bmanaged runtime\b", r"\bruntime_install\b",
        r"\b沙盒运行时\b", r"\b受管运行时\b",
    ],
    "supply_chain": [
        r"\bsbom\b", r"\bsca\b", r"\bsyft\b", r"\btrivy\b", r"\bosv[- ]?scanner\b",
        r"\bdependency audit\b", r"\bsupply chain\b", r"\blockfile\b",
        r"\b软件成分\b", r"\b依赖审计\b", r"\b供应链\b", r"\b物料清单\b",
    ],
    "wireless": [
        r"\bwi-?fi\b", r"\bwlan\b", r"\b802\.11\b", r"\bradiotap\b",
        r"\baircrack\b", r"\bkismet\b", r"\bwireless\b",
        r"\b无线\b", r"\b无线网卡\b", r"\b无线流量\b",
    ],
    "iot": [
        r"\biot\b", r"\bfirmware\b", r"\bbinwalk\b", r"\brootfs\b", r"\bemba\b",
        r"\b固件\b", r"\b物联网\b", r"\b文件系统提取\b",
    ],
    "hardware": [
        r"\bhardware\b", r"\buart\b", r"\bjtag\b", r"\bswd\b", r"\bchipsec\b",
        r"\bopenocd\b", r"\bsigrok\b", r"\bflash dump\b",
        r"\b硬件\b", r"\b串口\b", r"\b闪存\b", r"\b逻辑分析仪\b",
    ],
    "attack": [
        r"\bsqli\b", r"\bsql injection\b", r"\bblind\b", r"\bunion\b", r"\bnosql\b",
        r"\bxss\b", r"\bssti\b", r"\bxxe\b", r"\blfi\b", r"\brfi\b",
        r"\btemplate injection\b", r"\bpath traversal\b", r"\bfile inclusion\b",
        r"\bcommand injection\b", r"\bprompt injection\b", r"\bjailbreak\b",
        r"\b(rag|chatbot|llm|agentic)\b.*\b(attack|audit|security|test|vulnerability)\b",
        r"\bpentest\b", r"\bpenetration\b", r"\bexploit\b", r"\broe\b",
        r"\bweak password\b", r"\bcredential audit\b", r"\bdefault password\b",
        r"\bpost[- ]?exploit\b", r"\bpost exploitation\b", r"\bimpact\b", r"\blab harness\b",
        r"\bsqlmap\b",
        r"\bidor\b", r"\bauth\b", r"\baccess control\b", r"\bcsrf\b", r"\bcors\b",
        r"\boauth\b", r"\boidc\b", r"\bsaml\b", r"\bjwt\b", r"\bssrf\b", r"\bupload\b",
        r"\bdesync\b", r"\brequest smuggling\b", r"\bcache poison", r"\boob\b",
        r"\b注入\b", r"\b越权\b", r"\b未授权\b", r"\b上传\b", r"\b回连\b",
        r"\b大模型\b.*\b(攻击|审计|安全|测试|漏洞)\b", r"\b提示注入\b", r"\b越狱\b",
        r"\b请求走私\b", r"\b缓存投毒\b", r"\b身份\b", r"\b认证\b", r"\b令牌\b",
        r"\b渗透\b", r"\b利用链\b", r"\b授权\b", r"\b后渗透\b", r"\b影响面\b", r"\b靶场\b",
        r"\b弱口令\b", r"\b默认口令\b", r"\b口令审计\b", r"\b业务漏洞\b", r"\b业务测试\b",
    ],
}

_PROXY_QUERY_DESCRIPTION = (
    "ArgusAI traffic filter (skill proxy-traffic-query). Whitespace = AND; OR or | alternates; "
    "-term / !term / NOT negates; parentheses group. Fields: url host path method "
    "status(4xx, >=500, 200-299, failed) type(api,doc,js,css,img,media,other) ct size(>100k) "
    "reqsize duration(>2s) id header|req.header|resp.header(name, name=value, name=/re/) "
    "body|req.body|resp.body (raw + decrypted) dec|dec.req|dec.resp (decrypted only) "
    "finding has(finding,error,body,reqbody,decrypted,cookie,auth) tls(yes/no). Values: "
    "case-insensitive substring, a,b lists, * globs, /regex/flags (i m s x). Bare word or "
    "/regex/ matches the URL. Example: host:/^api\\./ method:POST status:4xx "
    "resp.body:/\"code\":\\s*-1/ -host:google.com"
)
_PROXY_DECRYPT_RULE_DESCRIPTION = (
    "Decryption rule object: {id? (update), name, enabled, match (traffic filter selecting flows), "
    "apply_to: response|request|both, field: '' whole body | JSON path data.payload / list[*].enc | "
    "form.NAME | query.NAME | header.NAME, steps: [...], note}. Steps run in order over bytes: "
    "{op:decode, codec:base64|base64url|hex|url} {op:sym, algo:aes-cbc|aes-ecb|aes-ctr|aes-gcm|"
    "des-cbc|des-ecb|3des-cbc|3des-ecb|sm4-cbc|sm4-ecb|rc4|xor, key, key_format:auto|utf8|hex|base64|"
    "base64url, iv ('@prefix' = IV at ciphertext head), iv_format, padding:pkcs7|none} "
    "{op:rsa, private_key:PEM, padding:pkcs1|oaep|oaep-sha256} {op:decompress, algo:gzip|zlib|zstd|br} "
    "{op:slice, start, end} {op:script, code:'function decrypt(input /*Buffer*/, ctx) { return string|Buffer }'}."
)

_PROXY_REWRITE_RULE_DESCRIPTION = (
    "Active rewrite rule object (MODIFIES LIVE TRAFFIC; skill proxy-rewrite-workflow): "
    "{id? (update), name, enabled, match (traffic filter selecting flows), apply_to: "
    "request|response|both, actions: [...], note}. Request actions: {op:set_header,name,value} "
    "{op:remove_header,name} {op:set_query,name,value} {op:remove_query,name} {op:set_method,method} "
    "{op:replace_body,find,replace,regex?,count?} {op:short_circuit,status,headers?,body?} (answer "
    "the client without contacting origin). Response actions: set_header, remove_header, "
    "{op:set_status,status}, replace_body. Body rewrite only runs on buffered, uncompressed bodies "
    "(<=2 MiB); larger/compressed bodies keep streaming and only non-body actions apply."
)

EXPLICIT_SCHEMAS: dict[str, dict[str, Any]] = {
    "proxy_search_traffic": {
        "query": {"type": "string", "description": _PROXY_QUERY_DESCRIPTION},
        "limit": {"type": "integer", "description": "Max matching flows (newest) returned as summaries.", "default": 30},
    },
    "proxy_decode_flow": {
        "flow_id": {"type": "integer", "description": "Proxy flow id from get_proxy_traffic / proxy_search_traffic."},
    },
    "proxy_decrypt_rules_list": {},
    "proxy_decrypt_rule_test": {
        "rule": {"type": "object", "description": _PROXY_DECRYPT_RULE_DESCRIPTION + " Script steps are rejected here."},
        "flow_id": {"type": "integer", "description": "Captured flow to dry-run the rule on."},
    },
    "proxy_decrypt_rule_save": {
        "rule": {"type": "object", "description": _PROXY_DECRYPT_RULE_DESCRIPTION + " Script steps are rejected; use proxy_decrypt_script_rule_save."},
        "test_flow_id": {"type": "integer", "description": "Optional flow id to test the saved rule on immediately.", "default": 0},
    },
    "proxy_decrypt_script_rule_save": {
        "rule": {"type": "object", "description": _PROXY_DECRYPT_RULE_DESCRIPTION},
        "test_flow_id": {"type": "integer", "description": "Optional flow id to test the saved rule on immediately.", "default": 0},
    },
    "proxy_decrypt_rule_delete": {
        "rule_id": {"type": "string", "description": "Rule id from proxy_decrypt_rules_list."},
    },
    "proxy_rewrite_rules_list": {},
    "proxy_rewrite_rule_test": {
        "rule": {"type": "object", "description": _PROXY_REWRITE_RULE_DESCRIPTION},
        "flow_id": {"type": "integer", "description": "Captured flow to dry-run the rewrite on (no live traffic touched)."},
    },
    "proxy_rewrite_rule_save": {
        "rule": {"type": "object", "description": _PROXY_REWRITE_RULE_DESCRIPTION},
        "test_flow_id": {"type": "integer", "description": "Optional captured flow id to dry-run the saved rule on.", "default": 0},
    },
    "proxy_rewrite_rule_delete": {
        "rule_id": {"type": "string", "description": "Rule id from proxy_rewrite_rules_list."},
    },
    "proxy_breakpoint_config": {
        "enabled": {"type": "boolean", "description": "Turn interactive request breakpoints on/off. Matching requests are PARKED until resolved.", "default": True},
        "url_contains": {"type": "string", "description": "Only park requests whose URL contains this substring.", "default": ""},
        "host_contains": {"type": "string", "description": "Only park requests whose host contains this substring.", "default": ""},
        "methods": {"type": "array", "items": {"type": "string"}, "description": "Only park these HTTP methods (empty = any).", "default": []},
    },
    "proxy_breakpoints_list": {},
    "proxy_breakpoint_resolve": {
        "pause_id": {"type": "string", "description": "Parked request id from proxy_breakpoints_list."},
        "action": {"type": "string", "enum": ["forward", "drop"], "description": "Forward (optionally edited) or drop the request.", "default": "forward"},
        "edits": {"type": "object", "description": "Forward-only overrides: method, url, headers ([[name,value],…]), body_text. Absent fields keep the original."},
    },
    "team_list": {},
    "team_get": {
        "team_id": {"type": "string", "description": "Standing team id from team_list / team_create."},
    },
    "team_chat_get": {
        "team_id": {"type": "string", "description": "Standing team id; returns one merged chronological group chat."},
    },
    "team_message_send": {
        "team_id": {"type": "string", "description": "Standing team id."},
        "message": {"type": "string", "description": "Command or context to send into the team group chat."},
        "agent_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional recipient member ids; empty sends to every member.", "default": []},
        "visibility": {"type": "string", "enum": ["group", "private"], "description": "group is visible to the room; private requires exactly one agent_id and is visible only to leader + that member.", "default": "group"},
    },
    "team_create": {
        "name": {"type": "string", "description": "Persistent Team-sidebar name. Not spawn_team (that squad lives only in the current chat)."},
        "objective": {"type": "string", "description": "What the team is for; becomes default context for members.", "default": ""},
        "preset": {"type": "string", "description": "Optional squad preset ('web_audit' | 'recon_sweep' | 'reverse_triage') to auto-spawn roles when objective is provided.", "default": ""},
    },
    "team_update": {
        "team_id": {"type": "string", "description": "Target team id."},
        "name": {"type": "string", "description": "New team name (leave empty to keep current).", "default": ""},
        "objective": {"type": "string", "description": "New team objective (leave empty to keep current).", "default": ""},
    },
    "team_delete": {
        "team_id": {"type": "string", "description": "Target team id to permanently delete."},
    },
    "team_add_member": {
        "team_id": {"type": "string", "description": "Target team id from team_list / team_create."},
        "role": {"type": "string", "description": "Specialist role, e.g. recon / vuln-analyst / reporter."},
        "objective": {"type": "string", "description": "The member's bounded objective."},
        "context": {"type": "string", "description": "Optional extra context for the member.", "default": ""},
        "expected_output": {"type": "string", "description": "What the member should return.", "default": ""},
        "permission": {"type": "string", "enum": ["readonly", "scoped"], "description": "readonly (safe default) or scoped (then requested_tools is the exact whitelist).", "default": "readonly"},
        "requested_tools": {"type": "array", "items": {"type": "string"}, "description": "Exact tool whitelist for a scoped member.", "default": []},
        "model": {"type": "string", "description": "Optional model override.", "default": ""},
        "approval_mode": {"type": "string", "enum": ["leader_auto", "manual"], "description": "leader_auto silently authorizes the approved whitelist; manual surfaces confirmations in Team chat.", "default": "leader_auto"},
        "allow_web_search": {"type": "boolean", "description": "Allow public web search for this member.", "default": True},
    },
    "team_member_get": {
        "team_id": {"type": "string", "description": "Standing team id."},
        "agent_id": {"type": "string", "description": "Member agent_id from team_get."},
    },
    "team_member_update": {
        "team_id": {"type": "string", "description": "Standing team id."},
        "agent_id": {"type": "string", "description": "Member agent_id from team_get."},
        "role": {"type": "string", "description": "Optional new role name.", "default": ""},
        "objective": {"type": "string", "description": "Optional new assignment objective.", "default": ""},
        "permission": {"type": "string", "enum": ["", "readonly", "scoped"], "description": "Optional new permission projection.", "default": ""},
        "requested_tools": {"type": "array", "items": {"type": "string"}, "description": "Exact whitelist when permission='scoped'.", "default": []},
        "model": {"type": "string", "description": "Optional configured endpoint name/model for this member.", "default": ""},
        "approval_mode": {"type": "string", "enum": ["", "leader_auto", "manual"], "description": "Optional confirmation mode.", "default": ""},
        "allow_web_search": {"type": "boolean", "description": "Optional web-search toggle; omit to keep current."},
    },
    "team_member_send_input": {
        "team_id": {"type": "string", "description": "Standing team id."},
        "agent_id": {"type": "string", "description": "Member agent_id from team_get."},
        "message": {"type": "string", "description": "Instruction or steering message to inject / wake the member with."},
        "resume": {"type": "boolean", "description": "When true (default), wakes an idle/paused/completed member with this message.", "default": True},
    },
    "team_member_control": {
        "team_id": {"type": "string", "description": "Standing team id."},
        "agent_id": {"type": "string", "description": "Member agent_id from team_get."},
        "action": {"type": "string", "enum": ["pause", "resume", "close", "delete"], "description": "Lifecycle action on the member.", "default": "pause"},
        "message": {"type": "string", "description": "Optional message when action='resume'.", "default": ""},
    },
    "parse_document": {
        "path": {
            "type": "string",
            "description": "Sandbox-relative path to the document (e.g. _attachments/report.pdf).",
        },
        "engine": {
            "type": "string",
            "enum": ["auto", "local", "mineru"],
            "description": (
                "auto: local pypdf/python-docx for .pdf/.docx, else MinerU when enabled; "
                "local: never upload; mineru: always use MinerU cloud (requires Settings)."
            ),
            "default": "auto",
        },
        "pages": {
            "type": "string",
            "description": "Optional page selector for PDF: '5' or '1-10'. Empty = all pages.",
            "default": "",
        },
        "output_path": {
            "type": "string",
            "description": "Optional sandbox path to write markdown (e.g. _attachments/report.parsed.md).",
            "default": "",
        },
        "language": {
            "type": "string",
            "description": "MinerU language hint (e.g. ch, en). Ignored by local engine.",
            "default": "ch",
        },
        "ocr": {
            "type": "boolean",
            "description": "Force OCR path (uses MinerU when enabled). Local digital PDF/DOCX keep ocr=false.",
            "default": False,
        },
        "max_chars": {
            "type": "integer",
            "description": "Soft cap on returned markdown length. 0 = settings default.",
            "default": 0,
        },
    },
    "channel_list_targets": {
        "identity_id": {"type": "string", "description": "Optional paired identity id filter.", "default": ""},
        "channel_id": {"type": "string", "description": "Optional configured channel id filter.", "default": ""},
    },
    "channel_delivery_status": {
        "delivery_id": {"type": "string", "description": "Optional delivery id returned by channel_send_message or channel_send_file.", "default": ""},
        "limit": {"type": "integer", "description": "How many recent deliveries of this conversation to report.", "default": 10},
    },
    "channel_reply": {
        "messages": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 8,
            "description": "Ordered message bubbles sent to the exact paired identity that authored this channel turn. Apply the global user-habits profile and usually prefer 2–4 natural bubbles when the reply contains distinct conversational thoughts. Call only as the final action: once accepted, channel_reply terminates the Agent turn.",
        },
        "pause_ms": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2000,
            "description": "Pause before each bubble after the first, in milliseconds. Keep it short and natural.",
            "default": 350,
        },
    },
    "channel_send_message": {
        "text": {"type": "string", "description": "Message to deliver to the paired channel target."},
        "identity_id": {"type": "string", "description": "Optional target identity id. Omit to reply to the channel identity that sent this turn; from the GUI, fan out to bound apps or auto-route to a recently active chat.", "default": ""},
    },
    "channel_send_file": {
        "path": {"type": "string", "description": "File path inside the current conversation sandbox. Absolute paths are accepted only when they still resolve inside that sandbox."},
        "identity_id": {"type": "string", "description": "Optional target identity id. Omit to reply to the channel identity that sent this turn; from the GUI, fan out to bound apps or auto-route to a recently active chat.", "default": ""},
        "caption": {"type": "string", "description": "Optional message sent with the file.", "default": ""},
        "filename": {"type": "string", "description": "Optional safe destination filename; defaults to the sandbox filename.", "default": ""},
    },
    "artifact_add": {
        "case_id": {"type": "string", "description": "Existing Case id that will own the artifact.", "default": ""},
        "kind": {"type": "string", "description": "Artifact kind such as request, response, trace, pcap, source, ai_prompt, ai_response, tool_call_log, config, manifest, policy, sbom, vuln_report, wireless, firmware, filesystem, ldap, graph, uart, flash, log, note, or other.", "default": "other"},
        "title": {"type": "string", "description": "Short human-readable artifact title.", "default": ""},
        "content": {"type": "string", "description": "Text content to archive. Redact secrets and prefer bounded summaries for large output.", "default": ""},
        "filename": {"type": "string", "description": "Optional safe filename under the Case artifact directory.", "default": ""},
        "domain": {"type": "string", "description": "Optional domain label such as web, ai, cloud, directory, supply_chain, wireless, iot, or hardware.", "default": ""},
        "media_type": {"type": "string", "description": "Optional MIME type.", "default": ""},
        "metadata_json": {"type": "string", "description": "Optional JSON object with source tool, version, scope, and redacted provenance.", "default": "{}"},
    },
    "evidence_add": {
        "case_id": {"type": "string", "description": "Existing Case id that will own the evidence.", "default": ""},
        "kind": {"type": "string", "description": "Evidence kind such as finding, http, oob, oracle, diff, trace, ai, dom, tool_call, source, static_analysis, dynamic_analysis, network, mobile, binary, cloud, config, directory, kerberos, sca, dependency, wireless, firmware, hardware, note, or other.", "default": "other"},
        "title": {"type": "string", "description": "Short evidence title.", "default": ""},
        "confidence": {"type": "string", "description": "Confidence: suspected, probable, or confirmed.", "default": "suspected"},
        "artifact_refs": {"type": "array", "items": {"type": "string"}, "description": "Artifact ids supporting this evidence.", "default": []},
        "interaction_refs": {"type": "array", "items": {"type": "string"}, "description": "Interaction ids supporting this evidence.", "default": []},
        "finding_uid": {"type": "string", "description": "Optional finding uid linked to this evidence.", "default": ""},
        "summary": {"type": "string", "description": "Concise claim, observed result, and verification basis.", "default": ""},
        "metadata_json": {"type": "string", "description": "Optional JSON object with redacted structured details.", "default": "{}"},
    },
    "mcp_marketplace_search": {
        "query": {"type": "string", "description": "Keywords for MCP capability, server name, publisher, category, or tag.", "default": ""},
        "source": {"type": "string", "enum": ["modelscope", "official"], "description": "Marketplace source. ModelScope provides Chinese metadata; official queries the MCP Registry.", "default": "modelscope"},
        "cursor": {"type": "string", "description": "Opaque next_cursor from a previous result.", "default": ""},
        "limit": {"type": "integer", "description": "Maximum concise entries to return, clamped to 1..20.", "default": 10},
        "category": {"type": "string", "description": "Optional ModelScope category filter.", "default": ""},
        "hosted": {"type": "string", "enum": ["all", "hosted", "local"], "description": "Optional ModelScope runtime filter.", "default": "all"},
    },
    "mcp_marketplace_get": {
        "server_id": {"type": "string", "description": "Exact marketplace server id returned by mcp_marketplace_search."},
        "source": {"type": "string", "enum": ["modelscope", "official"], "description": "Marketplace source that returned the entry.", "default": "modelscope"},
    },
    "mcp_server_list": {
        "include_disabled": {"type": "boolean", "description": "Include disabled configured MCP servers.", "default": True},
    },
    "mcp_server_add": {
        "name": {"type": "string", "description": "Display name for this external MCP server."},
        "transport": {"type": "string", "description": "Transport: stdio | streamable_http | sse.", "default": "stdio"},
        "command": {"type": "string", "description": "Stdio transport only: command that launches the MCP server.", "default": ""},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Stdio transport only: arguments for the MCP server.", "default": []},
        "url": {"type": "string", "description": "HTTP/SSE transport only: remote MCP server URL.", "default": ""},
        "headers_json": {"type": "string", "description": "HTTP/SSE transport only: JSON object of request headers (auth tokens kept off the wire).", "default": "{}"},
        "server_id": {"type": "string", "description": "Optional stable id. Leave empty to generate one.", "default": ""},
        "enabled": {"type": "boolean", "description": "Whether this MCP server is enabled.", "default": True},
        "env_json": {"type": "string", "description": "Stdio transport only: JSON object of env vars. Avoid hard-coding secrets here.", "default": "{}"},
        "cwd": {"type": "string", "description": "Stdio transport only: working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Per-request timeout, 1-120 seconds.", "default": 20},
        "notes": {"type": "string", "description": "Optional local notes about purpose, scope or risk.", "default": ""},
        "policy_json": {"type": "string", "description": "Optional JSON ExternalToolPolicy: {default_risk, allow_tools, deny_tools, tool_risks, allow_auto_approve, max_timeout_seconds, max_result_bytes, allow_insecure_http}.", "default": "{}"},
    },
    "mcp_server_delete": {
        "server_id": {"type": "string", "description": "Configured MCP server id to delete."},
    },
    "mcp_server_health": {
        "server_id": {"type": "string", "description": "Configured MCP server id to health-check (any transport)."},
    },
    "mcp_server_list_tools": {
        "server_id": {"type": "string", "description": "Configured MCP server id whose tool catalogue you want."},
        "query": {"type": "string", "description": "Optional case-insensitive filter over MCP tool name and description. Use it when a server exposes many tools.", "default": ""},
        "limit": {"type": "integer", "description": "Maximum tools to return after filtering. Clamped to 1..100.", "default": 50},
        "include_schema": {"type": "boolean", "description": "Whether to include sanitized input schemas. Use false for discovery/search, true once selecting a candidate tool.", "default": True},
    },
    "mcp_server_list_resources": {
        "server_id": {"type": "string", "description": "Configured MCP server id whose resources you want to enumerate."},
    },
    "mcp_server_call_tool": {
        "server_id": {"type": "string", "description": "Configured MCP server id (preferred over command-based mcp_stdio_call_tool)."},
        "tool_name": {"type": "string", "description": "MCP tool name to call on that server. Use mcp_server_list_tools first to discover it."},
        "arguments_json": {"type": "string", "description": "JSON object passed as MCP tool arguments.", "default": "{}"},
    },
    "mcp_server_read_resource": {
        "server_id": {"type": "string", "description": "Configured MCP server id."},
        "uri": {"type": "string", "description": "MCP resource URI (e.g. file:///path or argus://...) to read."},
        "offset": {"type": "integer", "description": "Byte offset into the resource's summary.", "default": 0},
        "limit": {"type": "integer", "description": "Maximum summary bytes to return (clamped by the server policy's max_result_bytes).", "default": 20000},
    },
    "mcp_stdio_health": {
        "command": {"type": "string", "description": "Executable command that starts the external stdio MCP server."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments for the MCP server.", "default": []},
        "server_name": {"type": "string", "description": "Short local name for this external MCP server.", "default": "external"},
        "env_json": {"type": "string", "description": "Optional JSON object of environment variables for the MCP process.", "default": "{}"},
        "cwd": {"type": "string", "description": "Optional working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Initialize timeout, 1-120 seconds.", "default": 10},
    },
    "mcp_stdio_list_tools": {
        "command": {"type": "string", "description": "Executable command that starts the external stdio MCP server."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments for the MCP server.", "default": []},
        "server_name": {"type": "string", "description": "Short local name for this external MCP server.", "default": "external"},
        "env_json": {"type": "string", "description": "Optional JSON object of environment variables for the MCP process.", "default": "{}"},
        "cwd": {"type": "string", "description": "Optional working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Per-request timeout, 1-120 seconds.", "default": 20},
    },
    "mcp_stdio_call_tool": {
        "command": {"type": "string", "description": "Executable command that starts the external stdio MCP server."},
        "tool_name": {"type": "string", "description": "MCP tool name to call on the external server."},
        "arguments_json": {"type": "string", "description": "JSON object passed as MCP tool arguments.", "default": "{}"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments for the MCP server.", "default": []},
        "server_name": {"type": "string", "description": "Short local name for this external MCP server.", "default": "external"},
        "env_json": {"type": "string", "description": "Optional JSON object of environment variables for the MCP process.", "default": "{}"},
        "cwd": {"type": "string", "description": "Optional working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Per-request timeout, 1-120 seconds.", "default": 20},
    },
    "mcp_stdio_list_resources": {
        "command": {"type": "string", "description": "Executable command that starts the external stdio MCP server."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments for the MCP server.", "default": []},
        "server_name": {"type": "string", "description": "Short local name for this external MCP server.", "default": "external"},
        "env_json": {"type": "string", "description": "Optional JSON object of environment variables for the MCP process.", "default": "{}"},
        "cwd": {"type": "string", "description": "Optional working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Per-request timeout, 1-120 seconds.", "default": 20},
    },
    "mcp_stdio_read_resource": {
        "command": {"type": "string", "description": "Executable command that starts the external stdio MCP server."},
        "uri": {"type": "string", "description": "MCP resource URI returned by mcp_stdio_list_resources."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments for the MCP server.", "default": []},
        "server_name": {"type": "string", "description": "Short local name for this external MCP server.", "default": "external"},
        "env_json": {"type": "string", "description": "Optional JSON object of environment variables for the MCP process.", "default": "{}"},
        "cwd": {"type": "string", "description": "Optional working directory for the MCP process.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Per-request timeout, 1-120 seconds.", "default": 20},
    },
    "plan_pentest": {
        "target_url": {"type": "string", "description": "Authorized target URL, e.g. https://example.com"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "intensity": {"type": "string", "enum": ["safe", "balanced", "active"], "description": "Testing intensity"},
    },
    "codebase_scope_set": {
        "root": {"type": "string", "description": "Authorized local repository/source root for this assistant session."},
        "audit_goal": {"type": "string", "description": "What to audit for, e.g. authz bypass, payment flow, secrets, business logic.", "default": ""},
        "case_id": {"type": "string", "description": "Optional repository case id to attach later evidence to.", "default": ""},
        "excludes": {"type": "array", "items": {"type": "string"}, "description": "Extra directory names or glob patterns to exclude. Defaults already skip .git, node_modules, dist, build, .venv, vendor.", "default": []},
    },
    "codebase_scope_get": {},
    "codebase_tree": {
        "path": {"type": "string", "description": "Directory inside the current codebase scope. Relative to the scoped root.", "default": "."},
        "max_depth": {"type": "integer", "description": "Maximum directory depth to return.", "default": 3},
        "offset": {"type": "integer", "description": "Pagination offset in the flattened tree.", "default": 0},
        "limit": {"type": "integer", "description": "Maximum entries to return.", "default": 200},
        "include_files": {"type": "boolean", "description": "Include files as well as directories.", "default": True},
    },
    "codebase_search": {
        "pattern": {"type": "string", "description": "Regex or literal pattern to search inside the scoped codebase."},
        "path": {"type": "string", "description": "File or directory inside scope. Relative to codebase root.", "default": "."},
        "glob": {"type": "string", "description": "Optional glob such as '*.py' or '**/*.js' when searching a directory.", "default": ""},
        "mode": {"type": "string", "enum": ["content", "files_with_matches", "count", "context"], "description": "Search output mode. Use context for minified/single-line files.", "default": "content"},
        "max_matches": {"type": "integer", "description": "Maximum matches returned.", "default": 50},
        "max_files": {"type": "integer", "description": "Maximum files scanned.", "default": 5000},
        "case_insensitive": {"type": "boolean", "description": "Case-insensitive search.", "default": True},
        "regex": {"type": "boolean", "description": "Interpret pattern as regex. Set false for literal text.", "default": True},
    },
    "codebase_read": {
        "path": {"type": "string", "description": "File inside current codebase scope, relative to scoped root. Raster images (png/jpg/webp/…) are attached for vision instead of being decoded as text."},
        "offset": {"type": "integer", "description": "1-indexed line offset. Negative values count from EOF.", "default": 1},
        "limit": {"type": "integer", "description": "Maximum lines to return.", "default": 200},
        "line_trunc": {"type": "integer", "description": "Maximum chars per displayed line.", "default": 400},
    },
    "codebase_entrypoints": {
        "path": {"type": "string", "description": "Directory inside current codebase scope to scan.", "default": "."},
        "max_per_kind": {"type": "integer", "description": "Maximum candidate signals returned per kind.", "default": 25},
        "max_files": {"type": "integer", "description": "Maximum files scanned per signal family.", "default": 3000},
    },
    "codebase_evidence_add": {
        "case_id": {"type": "string", "description": "Case id. If omitted, uses case_id from codebase_scope_set.", "default": ""},
        "path": {"type": "string", "description": "File inside current codebase scope, relative to scoped root."},
        "start_line": {"type": "integer", "description": "First source line to attach.", "default": 1},
        "end_line": {"type": "integer", "description": "Last source line to attach.", "default": 80},
        "title": {"type": "string", "description": "Evidence title.", "default": ""},
        "summary": {"type": "string", "description": "Model-written reason this snippet matters.", "default": ""},
        "confidence": {"type": "string", "enum": ["suspected", "probable", "confirmed"], "description": "Confidence of the source evidence.", "default": "suspected"},
        "kind": {"type": "string", "description": "Evidence kind, e.g. source, authz, injection, business_logic.", "default": "source"},
    },
    "codebase_write_file": {
        "path": {"type": "string", "description": "File path relative to codebase_scope_set root (or absolute still inside scope)."},
        "content": {"type": "string", "description": "Full file content to create or overwrite. Prefer codebase_patch_file for small edits."},
    },
    "codebase_patch_file": {
        "path": {"type": "string", "description": "Existing file inside codebase scope."},
        "old_string": {"type": "string", "description": "Exact substring to replace; copy from codebase_read content."},
        "new_string": {"type": "string", "description": "Replacement text.", "default": ""},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence when true.", "default": False},
    },
    "codebase_delete_file": {
        "path": {"type": "string", "description": "File inside codebase scope to delete."},
    },
    "codebase_stat_file": {
        "path": {"type": "string", "description": "File inside codebase scope."},
    },
    "codebase_grep_file": {
        "path": {"type": "string", "description": "Single file inside codebase scope."},
        "pattern": {"type": "string", "description": "Regex or literal pattern."},
        "mode": {"type": "string", "enum": ["content", "files_with_matches", "count", "context"], "default": "content"},
        "max_matches": {"type": "integer", "default": 50},
        "case_insensitive": {"type": "boolean", "default": False},
        "regex": {"type": "boolean", "default": True},
    },
    "codebase_list_files": {
        "path": {"type": "string", "description": "Directory inside codebase scope.", "default": "."},
        "max_depth": {"type": "integer", "default": 2},
        "limit": {"type": "integer", "default": 200},
    },
    "codebase_open_file": {
        "path": {"type": "string", "description": "Previewable file inside codebase scope."},
    },
    "codebase_reveal_in_folder": {
        "path": {"type": "string", "description": "File or directory inside scope; empty opens the scoped root.", "default": ""},
    },
    "codebase_run_python_script": {
        "path": {"type": "string", "description": ".py file inside codebase scope. cwd is the scoped root."},
        "python_env": {"type": "string", "description": "Optional interpreter; default prefers scope/.venv then host/managed Python.", "default": ""},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Script argv.", "default": []},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "codebase_run_python_snippet": {
        "code": {"type": "string", "description": "Inline Python; runs with cwd = codebase scope root."},
        "python_env": {"type": "string", "default": ""},
        "args": {"type": "array", "items": {"type": "string"}, "default": []},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "codebase_run_shell_script": {
        "path": {"type": "string", "description": ".ps1/.bat/.cmd/.sh inside codebase scope. cwd = scoped root."},
        "shell": {"type": "string", "description": "Shell override (auto/powershell/cmd/bash).", "default": ""},
        "args": {"type": "array", "items": {"type": "string"}, "default": []},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "codebase_run_shell_snippet": {
        "script": {"type": "string", "description": "Inline shell (npm test / go test / cargo test / pytest …). cwd = codebase scope root. Primary polyglot runner."},
        "shell": {"type": "string", "default": ""},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    "create_scan": {
        "url": {"type": "string", "description": "Authorized target URL"},
        "name": {"type": "string", "description": "Optional task name"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "paths": {"type": "string", "description": "Optional newline-separated paths"},
        "checks": {"type": "array", "items": {"type": "string"}, "description": "Check ids"},
    },
    "wait_for_task": {
        "task_id": {"type": "string", "description": "Task id to observe until it reaches done/cancelled/error"},
        "timeout_seconds": {"type": "integer", "description": "Maximum seconds to wait; browser/XSS scans may need 300-600 seconds", "default": 600},
    },
    "get_task_findings": {
        "task_id": {"type": "string", "description": "Task id"},
        "severity": {"type": "string", "description": "Optional severity filter: critical/high/medium/low/info"},
        "limit": {"type": "integer", "description": "Maximum findings to return", "default": 50},
    },
    "get_task_events": {
        "task_id": {"type": "string", "description": "Task id"},
        "limit": {"type": "integer", "description": "Maximum events to return", "default": 20},
    },
    "export_task_report": {
        "task_id": {"type": "string", "description": "Task id whose report files should be copied."},
        "out_dir": {"type": "string", "description": "Destination directory. Relative values are created inside the current session sandbox; absolute paths are treated as explicit user-selected locations."},
        "formats": {"type": "array", "items": {"type": "string"}, "description": "Report formats to export: html, md, json, findings. Empty = customer handoff (html, md). Explicitly include json/findings only for internal archive or automation."},
        "overwrite": {"type": "boolean", "description": "Overwrite existing files in the destination.", "default": False},
        "create_zip": {"type": "boolean", "description": "Also create a .zip archive next to the exported report directory.", "default": False},
    },
    "export_workspace_bundle": {
        "out_dir": {"type": "string", "description": "Destination directory for the deliverable workspace. Relative values are created inside the current session sandbox; absolute paths are explicit user-selected locations. Empty creates exports/<title>-timestamp in the sandbox."},
        "task_id": {"type": "string", "description": "Optional task id; if provided, reports are copied into reports/."},
        "title": {"type": "string", "description": "Bundle name used when out_dir is empty.", "default": "security-deliverable"},
        "workflow_md": {"type": "string", "description": "Reproduction workflow markdown content."},
        "analysis_md": {"type": "string", "description": "Analysis/thought process markdown content."},
        "notes_md": {"type": "string", "description": "Additional notes markdown content."},
        "include_reports": {"type": "boolean", "description": "Copy customer-facing task report files (report.html/report.md) when task_id is provided. Raw report.json/findings.jsonl are internal and require export_task_report formats=['json','findings'] if needed.", "default": True},
        "include_sandbox": {"type": "boolean", "description": "Copy current session sandbox files such as PoC code, lab harnesses, verification snippets, scripts, and notes.", "default": True},
        "include_readme": {"type": "boolean", "description": "Write a directory-index README.md. Leave unset (auto) and the README is only added when there is a curated document (REPRODUCTION/ANALYSIS/NOTES/reports) worth pointing at; an export that only contains workspace/ skips the README entirely. Set true to force, false to forbid."},
        "include_manifest": {"type": "boolean", "description": "Write a machine-readable manifest.json of which files were copied. Off by default because no human reviewer reads it and no part of the assistant reads it back; enable only when piping the export into downstream automation.", "default": False},
        "overwrite": {"type": "boolean", "description": "Overwrite existing files in the destination.", "default": False},
        "max_sandbox_files": {"type": "integer", "description": "Maximum sandbox files to copy, 1-1000.", "default": 200},
        "selection_mode": {"type": "string", "enum": ["auto", "all"], "description": "auto selects deliverable-looking files (PoC, reports, evidence, scripts, HAR/JSON/Markdown) and exports .tmp/snippet-* verification scripts under workspace/verification-scripts/ while excluding internal audit.jsonl; all copies every non-hidden sandbox file except runtime internals and audit logs.", "default": "auto"},
        "sandbox_paths": {"type": "array", "items": {"type": "string"}, "description": "Optional explicit list of sandbox files or directories chosen by the AI for export. Relative paths resolve inside the current assistant session sandbox. When provided, these paths override selection_mode for sandbox copying; runtime internals such as audit.jsonl and .venv are still excluded.", "default": []},
        "create_zip": {"type": "boolean", "description": "Also create a .zip archive next to the exported workspace directory.", "default": False},
    },
    "search_all_memory": {
        "query": {"type": "string", "description": "Model-generated query for searching across all assistant sessions"},
        "limit": {"type": "integer", "description": "Maximum number of hits", "default": 10},
    },
    "read_assistant_session": {
        "session_id": {"type": "string", "description": "Exact assistant session id, such as run_session_id from a cron_result or cron_runs row."},
        "role": {"type": "string", "enum": ["user", "assistant", "all"], "description": "Visible message roles to return.", "default": "assistant"},
        "order": {"type": "string", "enum": ["asc", "desc"], "description": "Newest-first or oldest-first pagination.", "default": "desc"},
        "offset": {"type": "integer", "description": "Skip this many matching visible messages after ordering.", "default": 0},
        "limit": {"type": "integer", "description": "Maximum messages to return, clamped to 1..50.", "default": 10},
        "max_chars": {"type": "integer", "description": "Total content budget for this page, clamped to 1000..64000.", "default": 24000},
    },
    "skill_list": {
        "include_disabled": {"type": "boolean", "description": "Include skills disabled for model invocation/user invocation.", "default": False},
    },
    "skill_search": {
        "query": {"type": "string", "description": "Search terms for project skills, e.g. 'oob sqli', 'case evidence', 'mobile reverse'."},
        "limit": {"type": "integer", "description": "Maximum number of matching skills to return.", "default": 8},
        "include_disabled": {"type": "boolean", "description": "Include disabled skills.", "default": False},
    },
    "skill_read": {
        "name": {"type": "string", "description": "Skill name or directory name, e.g. 'case-evidence-workflow' or 'js-reverse'."},
        "max_chars": {"type": "integer", "description": "Maximum body chars to return when using character paging. Use offset to page.", "default": 24000},
        "offset": {"type": "integer", "description": "0-based character offset into the skill body. Used when heading and line_limit are omitted.", "default": 0},
        "line_offset": {"type": "integer", "description": "0-based line offset into the skill body. Used with line_limit.", "default": 0},
        "line_limit": {"type": "integer", "description": "Maximum lines to return. When > 0, line paging takes precedence over offset/max_chars.", "default": 0},
        "heading": {"type": "string", "description": "Optional exact Markdown heading title to read, e.g. 'GraphQL'. Takes precedence over line and character paging.", "default": ""},
    },
    "kb_search": {
        "query": {"type": "string", "description": "Keyword query (title/tags/applies_to/trigger_terms are searched)."},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Hard tag filter; only entries with at least one of these tags are returned."},
        "limit": {"type": "integer", "description": "Max number of hits", "default": 8},
        "include_superseded": {"type": "boolean", "description": "Include entries that were superseded by a newer rewrite", "default": False},
    },
    "kb_read": {
        "id": {"type": "string", "description": "Knowledge entry id, e.g. kb-20260513-A7Xq"},
        "max_chars": {"type": "integer", "description": "Max chars of the body to return (default 32768). Use a smaller value for a peek; the response carries body_total_chars / body_more so you can paginate via offset.", "default": 32768},
        "offset": {"type": "integer", "description": "0-based char offset into the body. Use this to page through a large entry when body_more is true.", "default": 0},
    },
    "kb_save": {
        "title": {"type": "string", "description": "Short, concrete title (e.g. 'Oracle blind SQLi via EXTRACTVALUE + OOB'). Required."},
        "body": {"type": "string", "description": "Full markdown body. Should include Context, Solution (copy-paste-ready payload/command/snippet), Why it works, optional Known limitations. Required, must be at least 24 chars of real content."},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Lowercase tag list, e.g. ['sqli','oracle','xxe','oob','portswigger']. Used for tag-filtered search."},
        "trigger_terms": {"type": "array", "items": {"type": "string"}, "description": "Words/phrases that should auto-surface this entry in search."},
        "applies_to": {"type": "array", "items": {"type": "string"}, "description": "Short list of scenarios this knowledge applies to, e.g. ['Oracle 盲注，需外带','UTL_HTTP 被禁用']."},
        "confidence": {"type": "string", "enum": ["confirmed", "probable", "hypothesis", "experimental"], "description": "How confident you are this works. confirmed = you saw it work end-to-end."},
        "related": {"type": "array", "items": {"type": "string"}, "description": "Other kb ids or 'task:<task_id>' references this entry relates to."},
        "task_id": {"type": "string", "description": "Originating scan task id, if any."},
        "supersedes": {"type": "string", "description": "Old kb id this new entry replaces (structural rewrite)."},
        "reason": {"type": "string", "description": "One line: why are you saving this? (e.g. '解出 PortSwigger Lab 7')"},
    },
    "kb_update": {
        "id": {"type": "string", "description": "Knowledge entry id to update"},
        "title": {"type": "string", "description": "(Optional) New title."},
        "body": {"type": "string", "description": "(Optional) Full new markdown body — REPLACES the old body."},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "(Optional) New tag list — REPLACES the old list."},
        "trigger_terms": {"type": "array", "items": {"type": "string"}, "description": "(Optional) New trigger terms list."},
        "applies_to": {"type": "array", "items": {"type": "string"}, "description": "(Optional) New applies_to list."},
        "confidence": {"type": "string", "enum": ["confirmed", "probable", "hypothesis", "experimental"], "description": "(Optional) New confidence level."},
        "related": {"type": "array", "items": {"type": "string"}, "description": "(Optional) New related list."},
        "task_id": {"type": "string", "description": "(Optional) New originating task id."},
        "reason": {"type": "string", "description": "Required. Briefly: why are you updating this entry?"},
    },
    "kb_supersede": {
        "old_id": {"type": "string", "description": "Existing kb id that is being replaced"},
        "new_id": {"type": "string", "description": "Newer kb id that replaces it"},
        "reason": {"type": "string", "description": "Optional: why is new better?"},
    },
    "kb_delete": {
        "id": {"type": "string", "description": "Knowledge entry id to soft-delete (moves to _trash, recoverable via UI)"},
        "reason": {"type": "string", "description": "Required. Briefly: why are you deleting this entry?"},
    },
    "kb_suggest_cleanup": {
        "min_age_days": {"type": "integer", "description": "Only suggest trashed entries older than N days. Default 30.", "default": 30},
        "max_results": {"type": "integer", "description": "Cap the candidate list. Default 20.", "default": 20},
    },
    "invoke_catalogue_tool": {
        "name": {
            "type": "string",
            "description": "Exact catalogue tool name from tool_search.",
        },
        "arguments": {
            "type": "object",
            "description": "Arguments for that tool (JSON object).",
        },
    },
    "web_search": {
        "query": {
            "type": "string",
            "description": "Internet search query (news, CVE writeups, docs, GitHub issues, etc.). Not for searching the assistant tool catalogue — use tool_search for that.",
        },
        "max_results": {"type": "integer", "description": "Maximum results, 1-10", "default": 5},
        "region": {
            "type": "string",
            "description": "Search region/locale, e.g. cn-zh (China), wt-wt (worldwide), us-en. Chinese hosts default to cn-zh.",
            "default": "wt-wt",
        },
    },
    "github_search": {
        "query": {
            "type": "string",
            "description": "GitHub search query. Supports qualifiers, e.g. 'React2Shell CVE-2025-55182', 'org:foo path:*.env', 'topic:rce stars:>50'.",
        },
        "search_type": {
            "type": "string",
            "enum": ["repositories", "code", "issues"],
            "description": "What to search. 'repositories' and 'issues' work without a token; 'code' requires GITHUB_TOKEN.",
            "default": "repositories",
        },
        "max_results": {"type": "integer", "description": "Maximum results, 1-30", "default": 10},
    },
    "http_curl": {
        "target": {"type": "string", "description": "Authorized URL"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "method": {"type": "string", "description": "HTTP method", "default": "GET"},
        "data": {"type": "string", "description": "Raw request body (sent as --data-binary)"},
        "headers": {"type": "string", "description": "Headers as JSON object string or 'Name: value' lines. You may include Cookie here, but prefer the dedicated cookie parameter for browser session cookies."},
        "cookie": {"type": "string", "description": "Cookie header value sent with curl -b, e.g. 'session=abc; csrf=xyz'. For authenticated/CSRF flows, first read the real browser cookie with browser_use_tab(expression='document.cookie') or browser_evaluate(expression='document.cookie'), then pass that exact string here so CSRF token and session match."},
        "follow_redirects": {"type": "boolean", "description": "Follow HTTP redirects", "default": True},
        "insecure": {"type": "boolean", "description": "Skip TLS verification (-k)", "default": False},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-120", "default": 30},
        "user_agent": {"type": "string", "description": "Override User-Agent"},
    },
    "http_request_impersonate": {
        "target": {"type": "string", "description": "Authorized URL"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "method": {"type": "string", "description": "HTTP method", "default": "GET"},
        "impersonate": {"type": "string", "description": "Browser fingerprint profile (TLS ClientHello + HTTP/2 settings), e.g. chrome124, chrome131, firefox135, safari17_0, edge101. Use http_impersonate_status to list available profiles.", "default": "chrome124"},
        "headers": {"type": "string", "description": "Headers as JSON object string or 'Name: value' lines"},
        "cookies": {"type": "string", "description": "Cookies as JSON object string or raw 'k=v; k2=v2'"},
        "data": {"type": "string", "description": "Request body (string -> raw, JSON string -> sent as json)"},
        "params": {"type": "string", "description": "Optional query params as JSON object"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-120", "default": 30},
        "follow_redirects": {"type": "boolean", "description": "Follow HTTP redirects", "default": True},
        "verify": {"type": "boolean", "description": "Verify TLS certificate", "default": True},
        "proxy": {"type": "string", "description": "Optional proxy URL (http://user:pass@host:port)"},
        "user_agent": {"type": "string", "description": "Override User-Agent (sent on top of the impersonation profile)"},
    },
    "http_impersonate_status": {},
    "fetch_page_source": {
        "url": {"type": "string", "description": "Authorized URL to fetch"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
    },
    "extract_page_text": {
        "url": {"type": "string", "description": "Authorized URL to extract"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
    },
    "codec_transform": {
        "text": {"type": "string", "description": "Input text (for hex_decode, tolerant of 0x / \\x prefixes, spaces, commas, line breaks)"},
        "operation": {"type": "string", "enum": ["base64_encode", "base64_decode", "url_encode", "url_decode", "html_escape", "html_unescape", "hex_encode", "hex_decode"], "description": "Transform operation"},
    },
    "hex_view": {
        "text": {"type": "string", "description": "Payload to render as a hexdump. Interpretation depends on input_format."},
        "input_format": {"type": "string", "enum": ["text", "hex", "base64"], "description": "How to interpret 'text' before dumping: 'text' encodes as UTF-8 bytes, 'hex' parses a tolerant hex string, 'base64' decodes base64.", "default": "text"},
        "width": {"type": "integer", "description": "Bytes per row (1-64).", "default": 16},
        "max_bytes": {"type": "integer", "description": "Maximum bytes to render (16-65536). Excess is truncated.", "default": 4096},
    },
    "hash_text": {
        "text": {"type": "string", "description": "Input text"},
        "algorithm": {"type": "string", "description": "Hash algorithm, e.g. md5, sha1, sha256, sha512", "default": "sha256"},
    },
    "hmac_text": {
        "text": {"type": "string", "description": "Input text"},
        "key": {"type": "string", "description": "HMAC key"},
        "algorithm": {"type": "string", "description": "Hash algorithm", "default": "sha256"},
    },
    "jwt_decode": {
        "token": {"type": "string", "description": "JWT token to decode without signature verification"},
    },
    "http_probe": {
        "url": {"type": "string", "description": "Authorized URL to request"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "HEAD"], "description": "HTTP method"},
        "body": {"type": "string", "description": "Optional request body"},
    },
    "payload_probe": {
        "url": {"type": "string", "description": "Authorized URL with query string"},
        "payload": {"type": "string", "description": "Payload to place in one parameter"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "parameter": {"type": "string", "description": "Optional parameter name"},
    },
    "browser_probe": {
        "url": {"type": "string", "description": "Authorized URL to render in a real browser"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine. Default chromium (uses system Chrome when available). Switch to firefox/webkit when the target gates on Chromium fingerprints or only renders correctly in another engine. Each engine keeps its own warm process.", "default": "chromium"},
        "profile": {"type": "string", "description": "Fingerprint profile name: default | desktop_chrome_win | desktop_chrome_mac | desktop_chrome_zh | desktop_firefox_linux | desktop_safari_mac | mobile_iphone_safari | mobile_android_chrome | random. Sets UA, locale, timezone, viewport, navigator.platform/vendor and applies a stealth init script. Each unique profile gets its own warm hub. Call browser_profile_list for the catalogue.", "default": ""},
        "channel": {"type": "string", "description": "Optional Playwright channel for Chromium: chrome | chrome-beta | chrome-canary | msedge | msedge-beta | msedge-canary | msedge-dev. Empty = auto-detect best system Chrome/Edge then fall back to Playwright bundle. Ignored for firefox/webkit. Call browser_system_inventory to see what is installed.", "default": ""},
        "executable_path": {"type": "string", "description": "Optional absolute path to a Chromium-family binary (Chrome / Edge / Brave / Vivaldi / arbitrary Chromium). Takes precedence over channel. Empty = auto.", "default": ""},
        "headless": {"type": "boolean", "description": "Run the browser headlessly (default true). Only honored on first launch."},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "description": "When to consider navigation complete. Use 'networkidle' for SPAs/CTFs.", "default": "networkidle"},
        "wait_for_selector": {"type": "string", "description": "Optional CSS selector to wait for after navigation (e.g. '#app')"},
        "extra_wait_ms": {"type": "integer", "description": "Optional extra wait in milliseconds after the page settles (0-10000) for slow hydration", "default": 0},
        "fresh_context": {"type": "boolean", "description": "Drop the shared browser context (cookies/storage) before this call. Use when switching authorization or targets that must not share state.", "default": False},
    },
    "browser_evaluate": {
        "url": {"type": "string", "description": "Authorized URL to render"},
        "expression": {"type": "string", "description": "JS expression or arrow function to evaluate in the page context, e.g. '() => xss.get(\"xss\")', 'document.title', 'JSON.stringify(window.__INITIAL_STATE__)'. For navigation-triggering expressions (form.submit, location.href=..., link.click()), also set wait_for_navigation=true so the function does not return before the new page loads."},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine", "default": "chromium"},
        "profile": {"type": "string", "description": "Fingerprint profile name (see browser_probe.profile). Empty = default Playwright fingerprint.", "default": ""},
        "channel": {"type": "string", "description": "System browser channel for Chromium (chrome | msedge | ...). See browser_probe.channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium-family browser binary. See browser_probe.executable_path.", "default": ""},
        "headless": {"type": "boolean", "description": "Run the browser headlessly (default true). Only honored on first launch."},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "description": "Navigation wait state", "default": "networkidle"},
        "wait_for_selector": {"type": "string", "description": "Optional CSS selector to wait for before evaluating"},
        "extra_wait_ms": {"type": "integer", "description": "Extra wait (ms) before evaluating for late hydration (0-60000)", "default": 0},
        "fresh_context": {"type": "boolean", "description": "Drop the shared browser context (cookies/storage) before this call.", "default": False},
        "wait_for_navigation": {"type": "boolean", "description": "Wait for a page navigation to complete after the expression runs. Use when the expression submits a form, sets location.href, or triggers a redirect.", "default": False},
        "navigation_timeout_ms": {"type": "integer", "description": "Timeout (ms) for the post-evaluate navigation and post_eval_selector (1000-60000)", "default": 15000},
        "post_eval_selector": {"type": "string", "description": "Optional CSS selector to wait for AFTER the expression runs (e.g. wait for response page element)"},
        "post_eval_wait_ms": {"type": "integer", "description": "Extra wait (ms) after the expression and any navigation/selector wait (0-60000)", "default": 0},
    },
    "browser_system_inventory": {},
    "browser_install_engine": {
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Which Playwright engine to download (~120-300 MB). Defaults to chromium.", "default": "chromium"},
    },
    "browser_status": {
        "engine": {"type": "string", "description": "Optional engine filter (chromium|firefox|webkit). Omit to list every warm hub.", "default": ""},
    },
    "browser_reset": {
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Engine whose context should be dropped.", "default": "chromium"},
        "profile": {"type": "string", "description": "Reset only the hub for this fingerprint profile. Empty = default profile hub.", "default": ""},
    },
    "browser_hook_function": {
        "job_id": {"type": "string", "description": "ReverseJob id (rev-YYYYMMDD-xxxx) this hook attaches to. Persisted hits land in <job>/hooks/<hook_id>.hits.jsonl."},
        "target": {"type": "string", "description": "Dotted path of the function to hook on the live page: e.g. 'window.foo', 'JSON.stringify', 'XMLHttpRequest.prototype.setRequestHeader', 'WebSocket.prototype.send'. Resolved relative to window."},
        "replace_return_src": {"type": "string", "description": "Optional JS function body. Receives (args, thisArg, origReturn) and must `return` the value you want the page to see. Use for anti-fingerprint spoofing — e.g. `return {data: new Uint8Array(0)};` for canvas getImageData. Empty = pure observation (no return tampering).", "default": ""},
        "filter_src": {"type": "string", "description": "Optional JS function body taking (args, thisArg) returning truthy to record. Use to cut noise (e.g. only record calls with a specific URL substring).", "default": ""},
        "tag": {"type": "string", "description": "Free-form label attached to each hit record so later log analysis can group/filter (e.g. 'sign-header').", "default": ""},
        "recipe": {"type": "string", "description": "Name of the recipe that produced this hook (free-form, used for UI display). Empty when called directly.", "default": ""},
        "capture_stack": {"type": "boolean", "description": "Record the JS call stack per hit (truncated to 30 frames). Disable for ultra-high-frequency hooks.", "default": True},
        "note": {"type": "string", "description": "Free-form note saved alongside the hook definition.", "default": ""},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine hosting the hook target.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel for Chromium (chrome | msedge | ...). See browser_probe.channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary. See browser_probe.executable_path.", "default": ""},
        "headless": {"type": "boolean", "description": "Run the browser headlessly on first launch.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name (see browser_probe.profile).", "default": ""},
    },
    "browser_hook_property": {
        "job_id": {"type": "string", "description": "ReverseJob id this hook attaches to."},
        "target": {"type": "string", "description": "Dotted path to the property getter/setter on the live page: e.g. 'navigator.webdriver', 'document.cookie', 'location.host', 'screen.colorDepth'."},
        "replace_get_src": {"type": "string", "description": "Optional JS function body taking (orig) returning the replacement value. Use for spoofing (`return false;` for navigator.webdriver). Empty = pure observation.", "default": ""},
        "tag": {"type": "string", "description": "Free-form label.", "default": ""},
        "recipe": {"type": "string", "description": "Recipe name that produced this hook.", "default": ""},
        "capture_stack": {"type": "boolean", "description": "Record the JS call stack per get/set.", "default": True},
        "note": {"type": "string", "description": "Free-form note.", "default": ""},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary.", "default": ""},
        "headless": {"type": "boolean", "description": "Run the browser headlessly on first launch.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name.", "default": ""},
    },
    "browser_hook_list": {
        "job_id": {"type": "string", "description": "ReverseJob id whose hooks to enumerate."},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary.", "default": ""},
        "headless": {"type": "boolean", "description": "Whether to use a headless browser.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name.", "default": ""},
    },
    "browser_hook_remove": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "hook_id": {"type": "string", "description": "Hook id (h-xxxxxx) from browser_hook_list output."},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary.", "default": ""},
        "headless": {"type": "boolean", "description": "Headless on first launch.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name.", "default": ""},
    },
    "browser_hook_drain": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "hook_id": {"type": "string", "description": "Optional hook id. Empty drains every hook for this job.", "default": ""},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary.", "default": ""},
        "headless": {"type": "boolean", "description": "Headless on first launch.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name.", "default": ""},
    },
    "browser_hook_reattach": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium binary.", "default": ""},
        "headless": {"type": "boolean", "description": "Headless on first launch.", "default": True},
        "profile": {"type": "string", "description": "Fingerprint profile name.", "default": ""},
    },
    "reverse_job_list": {
        "scope": {"type": "string", "description": "Substring filter on the job's scope field (usually eTLD+1). Empty = all.", "default": ""},
        "status": {"type": "string", "enum": ["", "active", "paused", "done", "abandoned"], "description": "Filter to one status. Empty = all (non-trashed).", "default": ""},
        "include_trashed": {"type": "boolean", "description": "Also list jobs that the user soft-deleted. They appear with trashed:true.", "default": False},
    },
    "reverse_job_create": {
        "title": {"type": "string", "description": "Short concrete title for this reverse-engineering effort. Required. e.g. 'douyin.com sign request analysis'."},
        "target_url": {"type": "string", "description": "Entry URL you'll probe (the page where the interesting JS lives).", "default": ""},
        "scope": {"type": "string", "description": "eTLD+1 grouping key, e.g. 'douyin.com'. Used to find prior jobs for the same target.", "default": ""},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Free-form labels (e.g. ['sign', 'antibot']).", "default": []},
        "intent": {"type": "string", "description": "One-paragraph goal. e.g. 'Recover the X-Sign HMAC algorithm and reproduce it in Python.'", "default": ""},
        "framework": {"type": "string", "description": "If known, the JS framework the site uses ('react' | 'vue' | 'svelte' | 'angular' | …).", "default": ""},
        "bundler": {"type": "string", "description": "If known, the bundler ('webpack4' | 'webpack5' | 'vite' | 'rollup' | 'parcel' | 'systemjs' | 'browserify').", "default": ""},
        "obfuscation": {"type": "string", "description": "If known, the obfuscator chain ('obfuscator.io' | 'jsvmp' | 'packed' | 'closure-adv' | 'none').", "default": ""},
        "antibot": {"type": "array", "items": {"type": "string"}, "description": "If known, the anti-bot stack (['cloudflare', 'akamai-bmp', 'datadome', 'tencent-captcha']).", "default": []},
        "linked_task_id": {"type": "string", "description": "If this reverse job grew out of an existing scan/task, the task id.", "default": ""},
        "target_type": {"type": "string", "enum": ["", "web_js", "android", "ios", "windows", "native", "mixed", "unknown"], "description": "Primary target domain.", "default": ""},
        "platform": {"type": "string", "enum": ["", "web", "android", "ios", "windows", "linux", "macos", "unknown"], "description": "Runtime platform.", "default": ""},
    },
    "reverse_job_get": {
        "job_id": {"type": "string", "description": "ReverseJob id, e.g. 'rev-20260513-A7Xq'. Required."},
        "include_trashed": {"type": "boolean", "description": "Look in _trash/ too. Default false.", "default": False},
    },
    "reverse_job_update": {
        "job_id": {"type": "string", "description": "ReverseJob id. Required."},
        "title": {"type": ["string", "null"], "description": "New title.", "default": None},
        "scope": {"type": ["string", "null"], "description": "New scope.", "default": None},
        "status": {"type": ["string", "null"], "enum": [None, "active", "paused", "done", "abandoned"], "description": "Lifecycle state. Set to 'done' once findings are saved + reproducer verified.", "default": None},
        "tags": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Replace tag list.", "default": None},
        "intent": {"type": ["string", "null"], "description": "Update the intent paragraph.", "default": None},
        "framework": {"type": ["string", "null"], "description": "Update framework field of stack.", "default": None},
        "bundler": {"type": ["string", "null"], "description": "Update bundler field of stack.", "default": None},
        "obfuscation": {"type": ["string", "null"], "description": "Update obfuscation field of stack.", "default": None},
        "antibot": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Replace the antibot list.", "default": None},
        "linked_task_id": {"type": ["string", "null"], "description": "Update the linked scan task.", "default": None},
        "linked_kb_ids": {"type": ["array", "null"], "items": {"type": "string"}, "description": "Replace linked KB entry ids.", "default": None},
    },
    "reverse_job_trash": {
        "job_id": {"type": "string", "description": "ReverseJob id to soft-delete."},
    },
    "reverse_job_events": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "tail": {"type": "integer", "description": "Last N events.", "default": 100, "minimum": 1, "maximum": 2000},
    },
    "reverse_artifact_add": {
        "job_id": {"type": "string", "description": "ReverseJob id this artifact attaches to."},
        "content": {"type": "string", "description": "Raw artifact content as a utf-8 string (e.g. the JS source you fetched, the recovered original, the .wat output). Up to ~50 MB."},
        "kind": {"type": "string", "enum": ["entry", "chunk", "inline", "worker", "wasm", "sourcemap", "html", "trace", "deob", "webpack_module", "recovered", "js"], "description": "What kind of artifact this is. Drives where it's stored and how the UI labels it.", "default": "js"},
        "filename": {"type": "string", "description": "Original filename or a label (used to derive the file extension).", "default": ""},
        "url": {"type": "string", "description": "If fetched from the web, the source URL.", "default": ""},
        "from_artifact": {"type": "string", "description": "If this is a derivative (deobfuscation output, recovered original), the parent artifact id.", "default": ""},
        "tool": {"type": "string", "description": "Name of the tool that produced this artifact ('js_deobfuscate', 'source_map_recover', 'webpack_modules_extract', 'http_curl', …). Free-form.", "default": ""},
        "note": {"type": "string", "description": "Free-form note.", "default": ""},
    },
    "reverse_artifact_read": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "artifact_id": {"type": "string", "description": "Artifact id (art-xxxxxx) from reverse_job_get.artifacts."},
        "max_bytes": {"type": "integer", "description": "Truncate at this byte count.", "default": 2000000, "minimum": 256, "maximum": 50000000},
    },
    "reverse_artifact_remove": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "artifact_id": {"type": "string", "description": "Artifact id."},
    },
    "reverse_finding_add": {
        "job_id": {"type": "string", "description": "ReverseJob id. Required."},
        "title": {"type": "string", "description": "One-line summary of the finding. Required. e.g. 'Site signs requests with HMAC-SHA256(secret, ts||nonce||url||body)'."},
        "body": {"type": "string", "description": "Full markdown body — include payload examples, source snippets, reproducer pointers.", "default": ""},
        "kind": {"type": "string", "enum": ["algorithm", "flow", "vuln", "fingerprint", "note"], "description": "What kind of conclusion this is. ", "default": "note"},
        "confidence": {"type": "string", "enum": ["unverified", "probable", "verified"], "description": "How confident you are. 'verified' = you re-produced it in the sandbox and it matched a captured sample.", "default": "probable"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags (e.g. ['sign', 'hmac']).", "default": []},
        "coverage_id": {"type": "string", "description": "Row id from §3.5 coverage matrix in JS-REVERSE-PLAN, e.g. 'C9'. Empty if not applicable.", "default": ""},
    },
    "reverse_finding_read": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "finding_id": {"type": "string", "description": "Finding id (f-xxxxxx)."},
    },
    "reverse_finding_remove": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "finding_id": {"type": "string", "description": "Finding id."},
    },
    "reverse_hook_hits": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "hook_id": {"type": "string", "description": "Hook id (h-xxxxxx) from reverse_job_get.hooks."},
        "tail": {"type": "integer", "description": "Last N hits.", "default": 200, "minimum": 1, "maximum": 5000},
    },
    "reverse_oracle_add": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "records_json": {"type": "string", "description": "JSON object or array of oracle records, typically from js_runtime_drain.records."},
        "stage": {"type": "string", "description": "Pipeline stage label, e.g. 'stream-key', 'block-decrypt', 'outer-encrypt'.", "default": ""},
        "source": {"type": "string", "description": "Source label/path/function for these records.", "default": ""},
        "runtime": {"type": "string", "description": "Runtime label, e.g. 'hosted-js', 'cdp-browser'.", "default": "hosted-js"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Searchable tags.", "default": []},
        "note": {"type": "string", "description": "Short note.", "default": ""},
    },
    "reverse_oracle_list": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
    },
    "reverse_oracle_read": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "oracle_id": {"type": "string", "description": "Optional oracle id.", "default": ""},
        "stage": {"type": "string", "description": "Optional stage filter.", "default": ""},
        "kind": {"type": "string", "description": "Optional kind filter.", "default": ""},
        "tail": {"type": "integer", "description": "Last N records.", "default": 200, "minimum": 1, "maximum": 5000},
    },
    "reverse_oracle_compare": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "oracle_id": {"type": "string", "description": "Optional oracle id.", "default": ""},
        "actual_field": {"type": "string", "description": "Field holding produced value.", "default": "actual"},
        "expected_field": {"type": "string", "description": "Field holding captured expected value.", "default": "expected"},
        "tail": {"type": "integer", "description": "Last N records to compare.", "default": 200},
    },
    "reverse_reproduce_add": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "name": {"type": "string", "description": "Short slug for the filename, e.g. 'sign_request'."},
        "content": {"type": "string", "description": "Full reproducer script source (utf-8)."},
        "lang": {"type": "string", "description": "Language identifier, used for the extension. 'python' | 'js' | 'shell' | 'go' …", "default": "python"},
        "from_finding": {"type": "string", "description": "Id of the finding this reproducer implements.", "default": ""},
        "verified": {"type": "boolean", "description": "Set true ONLY after you ran the script in the sandbox and it matched a captured sample.", "default": False},
    },
    "reverse_reproduce_read": {
        "job_id": {"type": "string", "description": "ReverseJob id."},
        "rep_id": {"type": "string", "description": "Reproduce-script id (r-xxxxxx)."},
    },
    "reverse_recipe_list": {},
    "reverse_recipe_apply": {
        "job_id": {"type": "string", "description": "ReverseJob id to install hooks into. Required."},
        "recipe_id": {"type": "string", "description": "Recipe id from reverse_recipe_list. 'canvas_fingerprint' | 'canvas_fingerprint_spoof' | 'audio_fingerprint' | 'webdriver' | 'mouse_trajectory' | 'signed_header' | 'signed_cookie' | 'eval_harvest' | 'location_host_getter' | 'websocket' | 'fetch' | 'eventsource' | 'webassembly_compile' | 'vm_dispatcher_trace'. Required."},
    },
    "reverse_job_open_dir": {
        "job_id": {"type": "string", "description": "Reverse job id. The OS file manager opens the corresponding reverse_jobs/<id>/ folder."},
    },
    "reverse_artifact_reveal": {
        "job_id": {"type": "string", "description": "Reverse job id."},
        "artifact_id": {"type": "string", "description": "Artifact id (art-xxxxxx). The OS file manager opens its parent directory with the file selected."},
    },
    "reverse_artifact_copy_to": {
        "job_id": {"type": "string", "description": "Reverse job id."},
        "artifact_id": {"type": "string", "description": "Artifact id (art-xxxxxx)."},
        "dest_path": {"type": "string", "description": "Destination path. Empty = copy into current assistant session sandbox under imports/. Existing directory = append the artifact's filename. Otherwise used verbatim.", "default": ""},
    },
    "reverse_artifact_extract_strings": {
        "job_id": {"type": "string", "description": "Reverse job id."},
        "artifact_id": {"type": "string", "description": "Artifact id (art-xxxxxx). Decompile index JSON (decompile-*.json) scans the whole indexed output_dir; single-file artifacts scan that file."},
        "kind": {"type": "string", "enum": ["any", "literal", "url", "data_uri", "jwt", "base64", "hex", "high_entropy"], "description": "String filter — same semantics as extract_strings.", "default": "any"},
        "min_len": {"type": "integer", "description": "Minimum literal length (default 16).", "default": 16},
        "max_results": {"type": "integer", "description": "Cap on returned items (default 50, max 500).", "default": 50},
        "min_entropy": {"type": "number", "description": "Entropy floor for kind=high_entropy.", "default": 0.0},
        "relative_path": {"type": "string", "description": "Optional path inside the decompile output_dir (when artifact is a decompile index) or job directory.", "default": ""},
        "glob": {"type": "string", "description": "Optional filename glob when scanning a decompile tree, e.g. '**/*.smali' or 'AndroidManifest.xml'.", "default": ""},
    },
    "reverse_artifact_grep": {
        "job_id": {"type": "string", "description": "Reverse job id. Required."},
        "pattern": {"type": "string", "description": "Regex or literal to search for (same as grep_text_dir)."},
        "artifact_id": {"type": "string", "description": "Optional decompile index artifact id. Empty = grep entire job decompiled/ tree.", "default": ""},
        "relative_path": {"type": "string", "description": "Optional path inside decompile output or job dir.", "default": ""},
        "glob": {"type": "string", "description": "Optional filename glob, e.g. '**/*.smali' or '*.xml'.", "default": ""},
        "mode": {"type": "string", "enum": ["content", "files_with_matches", "count", "context"], "description": "Grep mode.", "default": "content"},
        "max_matches": {"type": "integer", "description": "Global match cap.", "default": 50},
        "max_files": {"type": "integer", "description": "Max files scanned in directory mode.", "default": 2000},
        "line_trunc": {"type": "integer", "description": "Per-line truncation.", "default": 400},
        "case_insensitive": {"type": "boolean", "description": "Case-insensitive search.", "default": False},
        "regex": {"type": "boolean", "description": "Treat pattern as regex.", "default": True},
    },
    "reverse_job_link_proxy_flow": {
        "job_id": {"type": "string", "description": "ReverseJob id to attach the proxy flow to."},
        "flow_id": {"type": "integer", "description": "Proxy flow id from get_proxy_traffic / get_proxy_flow_detail."},
        "note": {"type": "string", "description": "Optional note stored on the evidence ref and artifact.", "default": ""},
        "attach_har": {"type": "boolean", "description": "Store a single-entry HAR JSON artifact in the job.", "default": True},
    },
    "reverse_job_export": {
        "job_id": {"type": "string", "description": "Reverse job id to export."},
        "dest_dir": {"type": "string", "description": "Destination folder. Empty = <reverse_jobs_root>/_exports/.", "default": ""},
        "include_decompiled": {"type": "boolean", "description": "Include the decompiled/ subtree (JADX/APKTool output). Off by default because it can be very large.", "default": False},
        "as_zip": {"type": "boolean", "description": "True = produce a .zip archive (default). False = copy as a directory.", "default": True},
    },
    "reverse_toolchain_scan": {
        "job_id": {"type": "string", "description": "Optional ReverseJob id to persist toolchain snapshot.", "default": ""},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "Subset of tools to probe (frida, jadx, adb, capa, …). Empty = full list.", "default": []},
    },
    "reverse_toolchain_install": {
        "tool": {
            "type": "string",
            "enum": ["adb", "apksigner", "apktool", "capa", "frida", "ghidra", "innoextract", "jadx", "objection", "yara", "zipalign"],
            "description": (
                "Reverse CLI to install into data_root/tools/reverse/ (persistent across restarts). "
                "Use ONLY when reverse_toolchain_scan shows the tool missing AND the user has confirmed install. "
                "Updates settings.toolchain.paths.<tool>. ghidra is the function discovery, disassembly and "
                "decompile backend for binary_list_functions / binary_disassemble / binary_decompile "
                "(large download, needs a JDK 21+ on PATH). "
                "innoextract is the Inno Setup installer unpacker used by installer_unpack (Windows/Linux auto-install; macOS use brew). "
                "Do not use for jarsigner/class-dump — those are manual; point the user at the docs_url instead. "
                "This installs UNDER data_root (not your host system) — do NOT use run_shell_snippet / winget / choco / sandbox_pip_install / brew install as a substitute."
            ),
        },
        "force": {
            "type": "boolean",
            "description": "Force re-download even if already installed/cached. Set true ONLY for genuine retry after a failed install — repeating with force=false in the same loop will be deduped.",
            "default": False,
        },
    },
    "reverse_sample_import": {
        "path": {"type": "string", "description": "Absolute path to APK/IPA/PE sample (or sandbox-relative path). Required."},
        "job_id": {"type": "string", "description": "Existing ReverseJob id. Empty = create a new job.", "default": ""},
        "title": {"type": "string", "description": "Title when creating a new job.", "default": ""},
        "target_type": {"type": "string", "enum": ["", "android", "ios", "windows", "native", "mixed", "unknown"], "default": ""},
        "platform": {"type": "string", "enum": ["", "android", "ios", "windows", "linux", "macos", "web", "unknown"], "default": ""},
        "analyze": {"type": "boolean", "description": "Run static triage after import.", "default": True},
    },
    "android_apk_analyze": {
        "path": {"type": "string", "description": "Path to APK/AAB/XAPK. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Save analysis JSON as job artifact.", "default": True},
    },
    "android_jadx_decompile": {
        "path": {"type": "string", "description": "Path to APK/AAB/XAPK. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id to store output under decompiled/ and index artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "APK artifact id inside the job.", "default": ""},
        "output_dir": {"type": "string", "description": "Optional explicit output directory. Default: job decompiled/jadx-<hash>/ or beside the APK.", "default": ""},
        "no_res": {"type": "boolean", "description": "JADX --no-res (skip resource decoding).", "default": False},
        "deobf": {"type": "boolean", "description": "JADX --deobf.", "default": False},
        "threads": {"type": "integer", "description": "JADX --threads-count.", "default": 4, "minimum": 1, "maximum": 16},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 600, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Write decompile index + manifest artifacts into the job.", "default": True},
    },
    "android_apktool_decode": {
        "path": {"type": "string", "description": "Path to APK. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id to store output under decompiled/ and index artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "APK artifact id inside the job.", "default": ""},
        "output_dir": {"type": "string", "description": "Optional explicit output directory.", "default": ""},
        "force": {"type": "boolean", "description": "APKTool -f overwrite existing output.", "default": True},
        "no_src": {"type": "boolean", "description": "APKTool -s (skip smali).", "default": False},
        "no_res": {"type": "boolean", "description": "APKTool -r (skip resources).", "default": False},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 300, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Write decompile index + manifest artifacts into the job.", "default": True},
    },
    "android_apktool_build": {
        "job_id": {"type": "string", "description": "ReverseJob id with an apktool decode project. Required."},
        "project_dir": {"type": "string", "description": "Optional explicit apktool project dir. Default: job decompiled/apktool-* from android_apktool_decode.", "default": ""},
        "output_apk": {"type": "string", "description": "Optional output APK path. Default: reverse_jobs/<id>/built/apktool-build-*.apk", "default": ""},
        "force": {"type": "boolean", "description": "APKTool -f overwrite existing output.", "default": True},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 600, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Register built APK + index artifact in the job.", "default": True},
    },
    "android_apk_sign": {
        "job_id": {"type": "string", "description": "ReverseJob id (required unless path= only).", "default": ""},
        "artifact_id": {"type": "string", "description": "Unsigned APK artifact id in the job.", "default": ""},
        "path": {"type": "string", "description": "Path to unsigned APK when not using job artifact.", "default": ""},
        "output_apk": {"type": "string", "description": "Optional signed output path. Default: reverse_jobs/<id>/built/signed-*.apk", "default": ""},
        "keystore_path": {"type": "string", "description": "Keystore file path.", "default": ""},
        "keystore_pass": {"type": "string", "description": "Keystore password.", "default": ""},
        "key_alias": {"type": "string", "description": "Key alias inside the keystore.", "default": ""},
        "key_pass": {"type": "string", "description": "Private key password (defaults to keystore_pass).", "default": ""},
        "use_debug_keystore": {"type": "boolean", "description": "Use ~/.android/debug.keystore (android/androiddebugkey).", "default": False},
        "skip_zipalign": {"type": "boolean", "description": "Skip zipalign even when available.", "default": False},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 180, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Register signed APK + index artifact in the job.", "default": True},
    },
    "android_apk_validate": {
        "job_id": {"type": "string", "description": "ReverseJob id (required unless path= only).", "default": ""},
        "artifact_id": {"type": "string", "description": "APK artifact id in the job.", "default": ""},
        "path": {"type": "string", "description": "Path to APK when not using job artifact.", "default": ""},
        "require_signature": {
            "type": "boolean",
            "description": "Run apksigner verify (set false for unsigned apktool build output).",
            "default": True,
        },
        "timeout_seconds": {"type": "integer", "description": "CLI timeout for zipalign/apksigner/aapt checks.", "default": 90, "minimum": 15, "maximum": 600},
    },
    "adb_devices": {
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 15, "minimum": 5, "maximum": 120},
    },
    "adb_install": {
        "path": {"type": "string", "description": "Path to APK. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id with a signed/built APK artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "APK artifact id inside the job.", "default": ""},
        "serial": {"type": "string", "description": "Optional adb -s device serial.", "default": ""},
        "replace": {"type": "boolean", "description": "Pass adb install -r.", "default": True},
        "grant_permissions": {"type": "boolean", "description": "Pass adb install -g.", "default": False},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 120, "minimum": 30, "maximum": 600},
    },
    "adb_pull": {
        "remote_path": {"type": "string", "description": "Device path to pull, e.g. /sdcard/Download/app.db."},
        "local_path": {"type": "string", "description": "Optional host output path. Default: reverse_jobs/<id>/adb/ or session sandbox.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id to store pulled file under adb/.", "default": ""},
        "serial": {"type": "string", "description": "Optional adb -s device serial.", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Register pulled file as job artifact when <=10MB.", "default": True},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 120, "minimum": 10, "maximum": 600},
    },
    "adb_push": {
        "remote_path": {"type": "string", "description": "Destination path on device, e.g. /sdcard/Download/payload.apk."},
        "path": {"type": "string", "description": "Local file path. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id when pushing a job artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job.", "default": ""},
        "serial": {"type": "string", "description": "Optional adb -s device serial.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 120, "minimum": 10, "maximum": 600},
    },
    "adb_logcat": {
        "serial": {"type": "string", "description": "Optional adb -s device serial.", "default": ""},
        "duration_seconds": {"type": "integer", "description": "Capture duration (0 = one-shot dump via logcat -d).", "default": 10, "minimum": 0, "maximum": 300},
        "filter_spec": {"type": "string", "description": "Optional logcat filter tokens, e.g. ActivityManager:I *:S.", "default": ""},
        "max_lines": {"type": "integer", "description": "Maximum lines returned.", "default": 500, "minimum": 10, "maximum": 5000},
        "job_id": {"type": "string", "description": "ReverseJob id to store logcat JSON artifact.", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Save logcat JSON as job artifact.", "default": True},
    },
    "ios_ipa_analyze": {
        "path": {"type": "string", "description": "Path to IPA. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Save analysis JSON as job artifact.", "default": True},
    },
    "ios_class_dump": {
        "path": {"type": "string", "description": "Path to IPA. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "output_dir": {"type": "string", "description": "Optional output directory for .h headers.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 180, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Save class-dump index JSON as job artifact.", "default": True},
    },
    "windows_pe_analyze": {
        "path": {"type": "string", "description": "Path to EXE/DLL. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Save analysis JSON as job artifact.", "default": True},
    },
    "elf_analyze": {
        "path": {"type": "string", "description": "Path to ELF/SO binary. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Save analysis JSON as job artifact.", "default": True},
    },
    "binary_capa_analyze": {
        "path": {"type": "string", "description": "Path to PE/ELF/APK sample. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "format_hint": {"type": "string", "description": "Optional capa -f format hint (pe, elf, apk, …).", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 180, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Save capa_report JSON as job artifact.", "default": True},
    },
    "binary_yara_scan": {
        "path": {"type": "string", "description": "Path to sample. Omit when using job_id+artifact_id.", "default": ""},
        "rules_path": {"type": "string", "description": "Absolute path to .yar / .yara rules file. Required."},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 120, "minimum": 10, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Save yara_match JSON as job artifact.", "default": True},
    },
    "installer_unpack": {
        "path": {"type": "string", "description": "Path to the installer (Inno Setup setup.exe). Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "output_dir": {"type": "string", "description": "Where to extract. Empty = session sandbox under installer-unpack/<name>/.", "default": ""},
        "password": {"type": "string", "description": "Decryption password for ENCRYPTED Inno Setup installers. Without it encrypted installers cannot be extracted — pivot to dynamic analysis instead.", "default": ""},
        "list_only": {"type": "boolean", "description": "List the bundled files without extracting (fast triage).", "default": False},
        "timeout_seconds": {"type": "integer", "description": "innoextract timeout.", "default": 180, "minimum": 10, "maximum": 3600},
    },
    "binary_list_functions": {
        "path": {"type": "string", "description": "Path to PE/ELF/Mach-O sample. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "limit": {"type": "integer", "description": "Max functions to return (largest first).", "default": 400, "minimum": 1, "maximum": 5000},
        "timeout_seconds": {"type": "integer", "description": "Ghidra analysis timeout; first import can be slow on large binaries.", "default": 300, "minimum": 30, "maximum": 3600},
    },
    "binary_disassemble": {
        "path": {"type": "string", "description": "Path to sample. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "function": {"type": "string", "description": "Function name to disassemble (e.g. main, FUN_00401000). Use binary_list_functions first.", "default": ""},
        "address": {"type": "string", "description": "Address to disassemble (e.g. 0x401000). Takes precedence over function. Empty function+address = entry point.", "default": ""},
        "count": {"type": "integer", "description": "Number of instructions. 0 = disassemble the whole analyzed function.", "default": 0, "minimum": 0, "maximum": 4000},
        "timeout_seconds": {"type": "integer", "description": "Ghidra analysis timeout.", "default": 300, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Save disassembly as a 'disasm' job artifact.", "default": True},
    },
    "binary_decompile": {
        "path": {"type": "string", "description": "Path to sample. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional).", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "function": {"type": "string", "description": "Function name to decompile (e.g. main, FUN_00401000). Use binary_list_functions first.", "default": ""},
        "address": {"type": "string", "description": "Address to decompile (e.g. 0x401000). Takes precedence over function.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Timeout. Ghidra's first run analyzes the whole binary (can be slow); subsequent functions reuse the cached project.", "default": 300, "minimum": 30, "maximum": 3600},
        "attach_to_job": {"type": "boolean", "description": "Save pseudocode as a 'decompiled' job artifact.", "default": True},
    },
    "binary_patch": {
        "path": {"type": "string", "description": "Path to the binary to patch. Omit when using job_id+artifact_id.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id (optional). When set, the patched copy is stored as a 'patched' artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "Artifact id inside the job (optional).", "default": ""},
        "patches": {
            "type": "array",
            "description": (
                "List of patch objects. Each has a target — one of offset (file offset), rva, or va (PE) — "
                "and a replacement — one of bytes (hex like '9090'), nop+count, or assemble (e.g. 'xor eax, eax', needs keystone). "
                "Optionally include expect (hex of original bytes) to assert before writing; the whole patch aborts on mismatch."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "offset": {"type": "string", "description": "File offset (int or 0x-hex)."},
                    "rva": {"type": "string", "description": "PE relative virtual address (0x-hex)."},
                    "va": {"type": "string", "description": "PE virtual address incl. image base (0x-hex)."},
                    "bytes": {"type": "string", "description": "Replacement bytes as hex, e.g. '9090' or 'EB'."},
                    "nop": {"type": "boolean", "description": "Fill with NOP (0x90)."},
                    "count": {"type": "integer", "description": "Byte count for nop fill."},
                    "assemble": {"type": "string", "description": "Assembly to encode (needs keystone), e.g. 'jmp 0x401000'."},
                    "arch": {"type": "string", "description": "Arch for assemble: x86/x64/arm/arm64.", "default": "x64"},
                    "expect": {"type": "string", "description": "Hex of original bytes to assert before patching."},
                },
            },
        },
        "expect_original": {"type": "boolean", "description": "Enforce per-patch 'expect' assertions (strongly recommended).", "default": True},
        "recompute_checksum": {"type": "boolean", "description": "Recompute the PE OptionalHeader checksum after patching.", "default": True},
        "output_name": {"type": "string", "description": "Filename for the patched copy. Empty = <name>.patched<ext>. Never overwrites the input.", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "Store the patched binary as a 'patched' job artifact.", "default": True},
        "confirm_label": {"type": "string", "description": "Must be PATCH_BINARY to proceed (destructive). Set only after the user approves.", "default": ""},
    },
    "android_objection_run": {
        "package": {"type": "string", "description": "Android package name for objection -g."},
        "command": {"type": "string", "description": "Objection REPL command, e.g. android sslpinning disable."},
        "gadget": {"type": "string", "description": "Optional objection --gadget name.", "default": ""},
        "network": {"type": "boolean", "description": "Pass --network to objection.", "default": False},
        "job_id": {"type": "string", "description": "ReverseJob id to store output artifact.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "CLI timeout.", "default": 90, "minimum": 10, "maximum": 600},
        "attach_to_job": {"type": "boolean", "description": "Save objection output as trace artifact.", "default": True},
    },
    "mobile_frida_recipe_list": {
        "platform": {"type": "string", "enum": ["", "android", "ios"], "description": "Filter recipes by platform.", "default": ""},
    },
    "mobile_frida_recipe_apply": {
        "job_id": {"type": "string", "description": "ReverseJob id to install the script into. Required."},
        "recipe_id": {"type": "string", "description": "Recipe id from mobile_frida_recipe_list. Required."},
    },
    "frida_list_devices": {},
    "frida_list_processes": {
        "device": {"type": "string", "description": "Frida device id from frida_list_devices. Empty = local USB/default.", "default": ""},
    },
    "frida_attach": {
        "process_name": {"type": "string", "description": "Process name for ``frida -n``.", "default": ""},
        "pid": {"type": "integer", "description": "PID for ``frida -p``.", "default": 0},
        "device": {"type": "string", "description": "Device id, or 'usb' / 'remote'.", "default": ""},
        "script": {"type": "string", "description": "Inline Frida JS script body.", "default": ""},
        "script_path": {"type": "string", "description": "Path to .js script (absolute or sandbox-relative).", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id to attach logs/sessions.", "default": ""},
        "artifact_id": {"type": "string", "description": "Frida script artifact id in the job.", "default": ""},
        "timeout_seconds": {"type": "integer", "description": "Foreground timeout.", "default": 30, "minimum": 1, "maximum": 600},
        "background": {"type": "boolean", "description": "Keep Frida running; stop with frida_session_stop.", "default": False},
        "save_log": {"type": "boolean", "description": "Save stdout log as frida_log artifact.", "default": True},
    },
    "frida_spawn": {
        "package": {"type": "string", "description": "Android/iOS package or bundle id for ``frida -f``."},
        "device": {"type": "string", "description": "Device id, or 'usb' / 'remote'.", "default": ""},
        "script": {"type": "string", "description": "Inline Frida JS script body.", "default": ""},
        "script_path": {"type": "string", "description": "Path to .js script.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id.", "default": ""},
        "artifact_id": {"type": "string", "description": "Script artifact id in the job.", "default": ""},
        "timeout_seconds": {"type": "integer", "default": 45, "minimum": 1, "maximum": 600},
        "background": {"type": "boolean", "default": False},
        "save_log": {"type": "boolean", "default": True},
    },
    "frida_run_script": {
        "mode": {"type": "string", "enum": ["spawn", "attach_name", "attach_pid"], "description": "Frida attach mode.", "default": "attach_name"},
        "target": {"type": "string", "description": "Package name, process name, or pid string."},
        "device": {"type": "string", "default": ""},
        "script": {"type": "string", "default": ""},
        "script_path": {"type": "string", "default": ""},
        "job_id": {"type": "string", "default": ""},
        "artifact_id": {"type": "string", "default": ""},
        "timeout_seconds": {"type": "integer", "default": 60, "minimum": 1, "maximum": 600},
        "background": {"type": "boolean", "default": False},
        "save_log": {"type": "boolean", "default": True},
    },
    "frida_session_list": {},
    "frida_session_stop": {
        "session_id": {"type": "string", "description": "Session id from frida_attach/spawn/run_script (background=true)."},
        "job_id": {"type": "string", "description": "Optional ReverseJob id to close runtime_sessions entry.", "default": ""},
    },
    "js_deobfuscate_heavy": {
        "source": {"type": "string", "description": "Full JS bundle source (utf-8). Up to ~10 MB practically; webcrack will get slow above that."},
        "use_restringer": {"type": "boolean", "description": "Run restringer as a second pass when webcrack didn't change anything and the input looks like obfuscator.io output.", "default": True},
        "timeout": {"type": "integer", "description": "Subprocess timeout for the Node side (seconds). Increase for very large bundles.", "default": 120, "minimum": 10, "maximum": 600},
    },
    "js_unpack_bundle": {
        "source": {"type": "string", "description": "Full bundle source (utf-8). Auto-detects Webpack 4/5, Vite, Rollup, Parcel, SystemJS, UMD, Module Federation, Browserify, Closure ADV."},
        "timeout": {"type": "integer", "description": "Subprocess timeout for the Node side (seconds). Only used when delegating to webcrack (webpack/browserify).", "default": 120, "minimum": 10, "maximum": 600},
    },
    "js_detect_bundler": {
        "source": {"type": "string", "description": "JS source (or just the first 100 KB if speed matters)."},
    },
    "source_map_recover_indexed": {
        "map_text": {"type": "string", "description": "Full .map content as JSON text. Supports both flat V3 maps (top-level ``sources``) and indexed/sectioned maps (``sections`` array)."},
    },
    "source_map_decode_data_uri": {
        "uri": {"type": "string", "description": "A ``data:application/json;base64,...`` URI from a ``//# sourceMappingURL=data:...`` comment."},
    },
    "runtime_status": {
        "force_refresh": {"type": "boolean", "description": "Bypass the WABT probe cache and refresh managed Python/Node runtime health.", "default": False},
    },
    "js_runtime_status": {
        "runtime_name": {"type": "string", "description": "Optional hosted runtime process name. Empty lists every known runtime.", "default": ""},
    },
    "js_runtime_create": {
        "runtime_name": {"type": "string", "description": "Warm Node runtime process name. Use 'default' unless you need isolation.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id. Use a stable id per target or bundle.", "default": "default"},
        "env": {"type": "string", "enum": ["browser", "node"], "description": "browser = window/document/navigator/fetch/canvas stubs; node = Buffer + process.exit interceptor.", "default": "browser"},
        "page_url": {"type": "string", "description": "Seed location.href/document.URL in browser mode.", "default": ""},
        "user_agent": {"type": "string", "description": "Override navigator.userAgent in browser mode.", "default": ""},
        "globals": {"type": "object", "description": "Optional JSON-serialisable globals injected before code loads.", "default": {}},
        "profile_json": {"type": "string", "description": "Optional JSON browser fingerprint profile, typically from js_runtime_sample_profile.", "default": ""},
    },
    "js_runtime_close": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id to close.", "default": "default"},
    },
    "js_runtime_stop": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name to terminate.", "default": "default"},
    },
    "js_runtime_load": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "source": {"type": "string", "description": "Inline JS source. Prefer path for large bundles."},
        "path": {"type": "string", "description": "Path to a JS file. Relative paths resolve under the assistant session sandbox; absolute sandbox paths are accepted.", "default": ""},
        "job_id": {"type": "string", "description": "ReverseJob id when loading directly from an artifact.", "default": ""},
        "artifact_id": {"type": "string", "description": "ReverseJob artifact id to load without embedding a large string.", "default": ""},
        "filename": {"type": "string", "description": "Filename label used in VM stack traces.", "default": ""},
        "timeout_ms": {"type": "integer", "description": "Load/evaluation timeout in ms.", "default": 30000},
    },
    "js_runtime_eval": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "expression": {"type": "string", "description": "JS expression evaluated inside the existing context. If it returns a function, that function receives inputs."},
        "inputs_json": {"type": "string", "description": "Optional JSON value passed as inputs.", "default": ""},
        "timeout_ms": {"type": "integer", "description": "Evaluation timeout in ms.", "default": 5000},
    },
    "js_runtime_call": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "target": {"type": "string", "description": "Function path already present in context, e.g. 'window.sign', 'JSON.stringify', '_decode'."},
        "args_json": {"type": "string", "description": "JSON array of function arguments.", "default": "[]"},
        "timeout_ms": {"type": "integer", "description": "Call timeout in ms.", "default": 5000},
    },
    "js_runtime_hook": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "target": {"type": "string", "description": "Function path to wrap inside the hosted context, e.g. '_ufe79ufe76ufe77' or 'window.sign'."},
        "hook_id": {"type": "string", "description": "Optional stable hook id.", "default": ""},
        "filter_src": {"type": "string", "description": "Optional JS function body taking (args, thisArg); return truthy to record.", "default": ""},
        "capture_stack": {"type": "boolean", "description": "Capture a truncated JS stack for each hit.", "default": True},
    },
    "js_runtime_drain": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "limit": {"type": "integer", "description": "Max oracle records to drain.", "default": 200},
    },
    "js_runtime_logs": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "limit": {"type": "integer", "description": "Max console log records to drain.", "default": 100},
    },
    "js_runtime_sources": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
    },
    "js_runtime_patches": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
    },
    "js_runtime_hooks": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
    },
    "js_runtime_patch": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "source_id": {"type": "string", "description": "Source id from js_runtime_sources. Empty means latest loaded source.", "default": ""},
        "mode": {"type": "string", "enum": ["string", "regex", "offset"], "description": "Patch mode.", "default": "string"},
        "search": {"type": "string", "description": "String to replace when mode=string.", "default": ""},
        "pattern": {"type": "string", "description": "Regular expression when mode=regex.", "default": ""},
        "flags": {"type": "string", "description": "Regex flags. g is added if omitted.", "default": "g"},
        "start": {"type": "integer", "description": "Start offset when mode=offset.", "default": 0},
        "end": {"type": "integer", "description": "End offset when mode=offset.", "default": 0},
        "replacement": {"type": "string", "description": "Replacement source text.", "default": ""},
        "max_replacements": {"type": "integer", "description": "Maximum replacements.", "default": 1},
        "reevaluate": {"type": "boolean", "description": "Re-evaluate patched source immediately.", "default": True},
        "timeout_ms": {"type": "integer", "description": "Patch re-evaluation timeout.", "default": 30000},
        "note": {"type": "string", "description": "Patch-history note.", "default": ""},
    },
    "js_runtime_instrument": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "target": {"type": "string", "description": "Single function target path.", "default": ""},
        "targets_json": {"type": "string", "description": "JSON array of function target paths.", "default": "[]"},
        "hook_id": {"type": "string", "description": "Optional stable hook id prefix.", "default": ""},
        "filter_src": {"type": "string", "description": "Optional JS function body taking (args, thisArg); return truthy to record.", "default": ""},
        "tag": {"type": "string", "description": "Oracle tag attached to hook records.", "default": ""},
        "capture_stack": {"type": "boolean", "description": "Capture stack traces.", "default": True},
    },
    "js_runtime_snapshot": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
    },
    "js_runtime_restore": {
        "runtime_name": {"type": "string", "description": "Warm runtime process name.", "default": "default"},
        "session_id": {"type": "string", "description": "Hosted JS context id.", "default": "default"},
        "env": {"type": "string", "enum": ["browser", "node"], "description": "New session environment.", "default": "browser"},
        "page_url": {"type": "string", "description": "Seed page URL.", "default": ""},
        "source": {"type": "string", "description": "Optional source to reload.", "default": ""},
        "path": {"type": "string", "description": "Optional path to reload.", "default": ""},
        "filename": {"type": "string", "description": "Filename label.", "default": ""},
        "timeout_ms": {"type": "integer", "description": "Reload timeout.", "default": 30000},
    },
    "js_runtime_sample_profile": {
        "url": {"type": "string", "description": "Authorized URL to load in a real browser for fingerprint sampling."},
        "scope": {"type": "string", "description": "Authorized scope."},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine.", "default": "chromium"},
        "channel": {"type": "string", "description": "System browser channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute browser executable path.", "default": ""},
        "headless": {"type": "boolean", "description": "Run headless.", "default": True},
    },
    "js_runtime_to_reproducer_hint": {
        "job_id": {"type": "string", "description": "Optional ReverseJob id containing oracle records.", "default": ""},
        "stage": {"type": "string", "description": "Optional stage to focus on.", "default": ""},
    },
    "python_reproducer_verify": {
        "job_id": {"type": "string", "description": "ReverseJob id containing oracle records."},
        "oracle_id": {"type": "string", "description": "Optional oracle id.", "default": ""},
        "stage": {"type": "string", "description": "Optional oracle stage filter.", "default": ""},
        "script_path": {"type": "string", "description": "Path to Python script defining reproduce(record). Relative paths resolve under the assistant sandbox."},
        "function_name": {"type": "string", "description": "Callable name inside script.", "default": "reproduce"},
        "expected_field": {"type": "string", "description": "Oracle field to compare against.", "default": "return_value"},
        "input_field": {"type": "string", "description": "Optional record field to pass instead of whole record.", "default": ""},
        "tail": {"type": "integer", "description": "Last N oracle records.", "default": 200},
        "timeout_seconds": {"type": "integer", "description": "Verification timeout.", "default": 30},
    },
    "browser_profile_list": {},
    "browser_attach_status": {},
    "browser_launch_for_attach": {
        "channel": {"type": "string", "description": "Browser channel to relaunch: msedge | chrome | brave | vivaldi | chrome-beta | msedge-beta. The user-data-dir is auto-detected from this channel unless user_data_dir is given.", "default": "msedge"},
        "port": {"type": "integer", "description": "Local TCP port to expose CDP on. Pick something free (default 9222).", "default": 9222},
        "use_my_profile": {"type": "boolean", "description": "If true (default), reuse the user's real profile so cookies / open tabs / extensions persist. Set to false to launch a clean profile.", "default": True},
        "user_data_dir": {"type": "string", "description": "Override the user-data-dir path. Empty = auto-detect from channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Override the browser binary. Empty = auto-detect from channel.", "default": ""},
        "kill_existing": {"type": "boolean", "description": "Kill any running instance of this channel first (required, otherwise the --remote-debugging-port flag is ignored due to Chromium single-instance dedup).", "default": True},
        "show_window": {"type": "boolean", "description": "Launch with a visible window. Default false = hidden (--headless=new); the agent drives it off-screen and the user watches via the in-app Browser live view. Only set true when the user explicitly wants the real browser window visible.", "default": False},
    },
    "browser_attach_existing": {
        "endpoint": {"type": "string", "description": "Full CDP endpoint URL, e.g. http://127.0.0.1:9222 — the browser must already be running with --remote-debugging-port=<port>.", "default": "http://127.0.0.1:9222"},
    },
    "browser_detach": {},
    "browser_resume_control": {},
    "browser_list_tabs": {},
    "browser_open_tab": {
        "url": {"type": "string", "description": "Optional URL to open in a ArgusAI-owned tab. Leave empty for about:blank.", "default": ""},
        "reuse_existing": {"type": "boolean", "description": "Reuse an existing ArgusAI-owned tab instead of creating another one. Default true.", "default": True},
        "expression": {"type": "string", "description": "Optional JavaScript expression to evaluate after navigation.", "default": ""},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "description": "What to wait for when url is set.", "default": "load"},
        "wait_for_load": {"type": "boolean", "description": "Whether to wait for wait_until after navigation. Set false for slow/fire-and-forget pages.", "default": True},
        "navigation_timeout_ms": {"type": "integer", "description": "Navigation timeout in milliseconds.", "default": 30000},
        "extra_wait_ms": {"type": "integer", "description": "Settle delay after navigation completes (0-30000).", "default": 0},
        "capture_html": {"type": "boolean", "description": "Capture page outerHTML. Auto: true when expression empty, false when expression set."},
        "max_html_chars": {"type": "integer", "description": "Char cap for returned HTML (default 8000, max 200000).", "default": 8000},
        "max_eval_chars": {"type": "integer", "description": "Char cap for serialised evaluation result (default 8000, max 50000).", "default": 8000},
    },
    "browser_close_owned_tabs": {
        "target_id": {"type": "string", "description": "Optional CDP target_id of one tab to close. Leave empty to close all ai_owned tabs.", "default": ""},
        "target_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional list of target_id values from browser_list_tabs.", "default": []},
        "url_contains": {"type": "string", "description": "Close ai_owned tabs whose URL contains this substring (e.g. portswigger.net).", "default": ""},
        "force": {"type": "boolean", "description": "When true and target_ids are set, close those tabs even if not ai_owned. Use only after the user explicitly asked to close them.", "default": False},
    },
    "browser_mark_tabs_owned": {
        "target_ids": {"type": "array", "items": {"type": "string"}, "description": "CDP target_id values from browser_list_tabs to track as ai_owned for later cleanup."},
    },
    "browser_use_tab": {
        "index": {"type": "integer", "description": "Legacy global tab index from browser_list_tabs.tabs. Prefer target_id or window_id+tab_index when multiple windows exist."},
        "target_id": {"type": "string", "description": "Stable CDP target_id from browser_list_tabs.tabs/windows. Preferred when available.", "default": ""},
        "window_id": {"type": "integer", "description": "Browser window_id from browser_list_tabs.windows. Use with tab_index."},
        "window_index": {"type": "integer", "description": "0-based window_index from browser_list_tabs.windows. Use with tab_index when window_id is missing."},
        "tab_index": {"type": "integer", "description": "0-based tab index within the selected window. Use with window_id or window_index."},
        "url_contains": {"type": "string", "description": "Pick the first tab whose URL contains this substring (case-insensitive).", "default": ""},
        "title_contains": {"type": "string", "description": "Pick the first tab whose title contains this substring (case-insensitive).", "default": ""},
        "navigate_to": {"type": "string", "description": "Navigate the matched tab to this URL BEFORE running expression / capturing HTML. Use this to walk many URLs on the SAME tab (e.g. iterate PortSwigger labs one by one) instead of polluting the user's browser with one new tab per URL. Stays in scope as long as you reuse the same selector. Empty = no navigation. IMPORTANT: do NOT use this as a 'send one request' tool — if you're just firing a payload at an endpoint and don't need rendered DOM (OOB/blind SQLi/SSRF), use `http_curl` instead. The browser path is 5-10x slower, costs a confirmation per call, and stalls on HTTP2/TLS errors waiting for `load`.", "default": ""},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "description": "What to wait for when navigate_to is set. load = full load (default), domcontentloaded = DOM ready, networkidle = no network activity for 500ms, commit = first response received. Pick by intent: 'commit' for fire-and-forget (just want the server to receive the request), 'domcontentloaded' for plain HTML pages, 'load' for pages with critical inline assets, 'networkidle' only for SPAs that hydrate after load. Don't default to 'load' for everything — it's the most likely to stall.", "default": "load"},
        "wait_for_load": {"type": "boolean", "description": "Whether to wait for wait_until after navigate_to. Set false when you only need the tab to open/navigate and the page may keep loading slowly; this returns after navigation commit.", "default": True},
        "navigation_timeout_ms": {"type": "integer", "description": "Navigation timeout in milliseconds. Use shorter values with wait_for_load=false, longer values when waiting for load/networkidle.", "default": 30000},
        "extra_wait_ms": {"type": "integer", "description": "Settle delay after navigation completes (0-30000). Useful for SPAs that hydrate after load.", "default": 0},
        "expression": {"type": "string", "description": "JavaScript expression evaluated against the matched tab (after any navigation). Prefer a targeted expression that returns ONLY the data you need (e.g. `() => [...document.querySelectorAll('.lab')].map(el => ({title: el.querySelector('h3').textContent, solved: el.classList.contains('is-solved')}))`) instead of dumping HTML and re-parsing it on your side.", "default": ""},
        "capture_html": {"type": "boolean", "description": "Capture the page's rendered outerHTML (truncated). Auto-smart by default: TRUE when no expression is provided (recon), FALSE when expression is set (your JS already returns what you need). Set true alongside an expression ONLY when you genuinely need both — dumping HTML costs context tokens."},
        "max_html_chars": {"type": "integer", "description": "Truncation cap for the captured HTML (200-200000). Default 8000 — only enlarge if a smaller window misses critical structure.", "default": 8000},
    },
    "browser_screenshot": {
        "url": {"type": "string", "description": "Optional authorized URL to navigate before screenshot. Required when no CDP browser is attached.", "default": ""},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope for url navigation.", "default": ""},
        "out_path": {"type": "string", "description": "Sandbox-relative PNG path. Default: screenshots/browser-<timestamp>.png", "default": ""},
        "selector": {"type": "string", "description": "Optional CSS selector. When set, captures only that element.", "default": ""},
        "full_page": {"type": "boolean", "description": "Capture the full scrollable page instead of the viewport. Ignored when selector is set.", "default": False},
        "attach_to_model": {"type": "boolean", "description": "Attach reasonably-sized PNGs to the next model call so vision-capable models can inspect the page.", "default": True},
        "headless": {"type": "boolean", "description": "Managed browser headless mode when no CDP browser is attached.", "default": True},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "description": "Navigation wait condition.", "default": "load"},
        "wait_for_load": {"type": "boolean", "description": "If false, return after navigation commit.", "default": True},
        "wait_for_selector": {"type": "string", "description": "Optional selector to wait for before capture.", "default": ""},
        "extra_wait_ms": {"type": "integer", "description": "Additional settle delay before capture, 0-30000.", "default": 0},
        "navigation_timeout_ms": {"type": "integer", "description": "Navigation/selector timeout, 1000-120000 ms.", "default": 30000},
        "index": {"type": "integer", "description": "Attached browser legacy global tab index.", "default": 0},
        "target_id": {"type": "string", "description": "Attached browser CDP target_id from browser_list_tabs.", "default": ""},
        "window_id": {"type": "integer", "description": "Attached browser window_id from browser_list_tabs.windows.", "default": 0},
        "window_index": {"type": "integer", "description": "Attached browser window_index from browser_list_tabs.windows.", "default": 0},
        "tab_index": {"type": "integer", "description": "Attached browser tab_index within a window.", "default": 0},
        "url_contains": {"type": "string", "description": "Attached browser tab URL substring selector.", "default": ""},
        "title_contains": {"type": "string", "description": "Attached browser tab title substring selector.", "default": ""},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Managed browser engine.", "default": "chromium"},
        "profile": {"type": "string", "description": "Managed browser fingerprint profile.", "default": ""},
        "channel": {"type": "string", "description": "Managed Chromium channel. See browser_probe.channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Managed Chromium executable path. See browser_probe.executable_path.", "default": ""},
        "fresh_context": {"type": "boolean", "description": "Reset managed browser context before capture. Ignored for attached CDP.", "default": False},
    },
    "js_fetch_asset": {
        "url": {"type": "string", "description": "Authorized JS/CSS/source-map/WASM asset URL to fetch"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "save_to_job": {"type": "string", "description": "Optional ReverseJob id to persist the full asset as an artifact.", "default": ""},
        "artifact_kind": {"type": "string", "description": "Artifact kind when save_to_job is set.", "default": "chunk"},
    },
    "js_asset_inventory": {
        "html": {"type": "string", "description": "HTML source or rendered DOM"},
        "code": {"type": "string", "description": "Optional JavaScript source"},
        "source_url": {"type": "string", "description": "Base URL for resolving relative asset paths"},
    },
    "js_bundle_analyze": {"code": {"type": "string", "description": "JavaScript bundle source"}, "source_url": {"type": "string", "description": "Optional source URL"}},
    "js_beautify": {"code": {"type": "string", "description": "JavaScript source"}, "max_chars": {"type": "integer", "description": "Maximum characters to format", "default": 200000}},
    "js_extract_strings": {"code": {"type": "string", "description": "JavaScript source"}, "max_items": {"type": "integer", "description": "Maximum strings/items", "default": 200}},
    "js_search": {"code": {"type": "string", "description": "JavaScript source"}, "pattern": {"type": "string", "description": "Regex or literal pattern"}, "limit": {"type": "integer", "description": "Maximum matches", "default": 50}},
    "js_slice_context": {"code": {"type": "string", "description": "JavaScript source"}, "offset": {"type": "integer", "description": "Byte/char offset", "default": 0}, "symbol": {"type": "string", "description": "Optional symbol name"}, "pattern": {"type": "string", "description": "Optional regex pattern"}},
    "source_map_discover": {"source_url": {"type": "string", "description": "JS source URL"}, "code": {"type": "string", "description": "JS source"}},
    "source_map_recover": {"map_text": {"type": "string", "description": "Source map JSON text"}, "source_url": {"type": "string", "description": "Source map URL"}},
    "webpack_modules_extract": {"code": {"type": "string", "description": "Webpack bundle source"}, "source_url": {"type": "string", "description": "Optional source URL"}, "max_modules": {"type": "integer", "description": "Maximum modules", "default": 80}},
    "webpack_module_read": {"code": {"type": "string", "description": "Webpack bundle source"}, "module_id": {"type": "string", "description": "Module id"}, "offset": {"type": "integer", "description": "Fallback offset", "default": 0}},
    "js_deobfuscate": {"code": {"type": "string", "description": "JavaScript source"}},
    "js_decode_literal": {"text": {"type": "string", "description": "JavaScript string literal body or encoded text"}},
    "js_string_array_map": {"code": {"type": "string", "description": "JavaScript source"}, "max_items": {"type": "integer", "description": "Maximum entries", "default": 200}},
    "js_constant_fold_preview": {"expression": {"type": "string", "description": "Safe literal/arithmetic expression"}},
    "js_control_flow_hints": {"code": {"type": "string", "description": "JavaScript source"}},
    "js_eval_unpack": {"code": {"type": "string", "description": "JavaScript unpacker/source"}, "expression": {"type": "string", "description": "Expression to evaluate after loading code"}},
    "js_api_extract": {"code": {"type": "string", "description": "JavaScript source"}, "source_url": {"type": "string", "description": "Optional source URL"}},
    "js_signature_candidates": {"code": {"type": "string", "description": "JavaScript source"}, "source_url": {"type": "string", "description": "Optional source URL"}},
    "js_dependency_trace": {"code": {"type": "string", "description": "JavaScript source"}, "symbol": {"type": "string", "description": "Variable or function name to trace"}},
    "js_crypto_detect": {"code": {"type": "string", "description": "JavaScript source"}, "source_url": {"type": "string", "description": "Optional source URL"}},
    "js_request_rebuild_hint": {"code": {"type": "string", "description": "JavaScript source"}, "trace_json": {"type": "string", "description": "Optional browser_trace JSON"}},
    "browser_trace": {
        "url": {"type": "string", "description": "Authorized URL to render and instrument"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "engine": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "description": "Browser engine", "default": "chromium"},
        "profile": {"type": "string", "description": "Fingerprint profile name (see browser_probe.profile).", "default": ""},
        "channel": {"type": "string", "description": "System browser channel for Chromium. See browser_probe.channel.", "default": ""},
        "executable_path": {"type": "string", "description": "Absolute path to a Chromium-family browser binary. See browser_probe.executable_path.", "default": ""},
        "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"], "description": "Navigation wait state", "default": "networkidle"},
        "wait_for_selector": {"type": "string", "description": "Optional selector to wait for"},
        "extra_wait_ms": {"type": "integer", "description": "Extra wait after load", "default": 0},
        "click_selector": {"type": "string", "description": "Optional selector to click before collecting final trace"},
        "fresh_context": {"type": "boolean", "description": "Start from clean browser storage", "default": False},
        "out_path": {"type": "string", "description": "Optional path to save the full trace JSON under the session sandbox (relative) or explicit absolute path.", "default": ""},
        "save_to_job": {"type": "string", "description": "Optional ReverseJob id to persist the full trace as an artifact.", "default": ""},
        "max_events": {"type": "integer", "description": "Max request/response/runtime trace events kept in memory and in saved output. Raise when saving to disk.", "default": 250},
    },
    "browser_trace_crypto": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "browser_trace_form": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "click_selector": {"type": "string", "description": "Optional selector to click"}},
    "browser_snapshot_state": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "expression": {"type": "string", "description": "Optional JS expression override"}},
    "browser_instrument_click": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "click_selector": {"type": "string", "description": "Selector to click"}},
    "browser_network_replay_plan": {"trace_json": {"type": "string", "description": "browser_trace JSON"}, "code": {"type": "string", "description": "Optional JS source"}},
    "captcha_detect": {"html": {"type": "string", "description": "HTML source"}, "code": {"type": "string", "description": "JavaScript source"}, "runtime_trace_json": {"type": "string", "description": "Optional browser_trace JSON"}},
    "captcha_script_analyze": {"html": {"type": "string", "description": "HTML source"}, "code": {"type": "string", "description": "JavaScript source"}},
    "captcha_runtime_trace": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "click_selector": {"type": "string", "description": "Optional captcha/action selector"}},
    "anti_bot_fingerprint_detect": {"code": {"type": "string", "description": "JavaScript source"}, "html": {"type": "string", "description": "HTML source"}},
    "captcha_report": {"html": {"type": "string", "description": "HTML source"}, "code": {"type": "string", "description": "JavaScript source"}, "runtime_trace_json": {"type": "string", "description": "Optional runtime trace JSON"}},
    "sandbox_eval_js": {"code": {"type": "string", "description": "JavaScript source to load. Hard limit: 200,000 chars; not suitable for large bundles, JSVMP, or multi-stage deobfuscation. Use js_runtime_load(path=...) for those."}, "expression": {"type": "string", "description": "Expression to evaluate"}, "inputs_json": {"type": "string", "description": "Optional JSON inputs"}, "timeout_ms": {"type": "integer", "description": "Timeout in ms", "default": 5000}},
    "sandbox_test_function": {"code": {"type": "string", "description": "JavaScript source"}, "function_name": {"type": "string", "description": "Function to call, e.g. sign"}, "cases_json": {"type": "string", "description": "JSON array of test inputs"}},
    "sandbox_load_library": {"library_code": {"type": "string", "description": "Library JS snippet"}, "code": {"type": "string", "description": "Additional JS source"}, "expression": {"type": "string", "description": "Expression to evaluate"}, "inputs_json": {"type": "string", "description": "Optional JSON inputs"}},
    "sandbox_compare_outputs": {"code": {"type": "string", "description": "JavaScript source"}, "function_name": {"type": "string", "description": "Function to call"}, "cases_json": {"type": "string", "description": "JSON array of test inputs"}, "repeats": {"type": "integer", "description": "Repeat count", "default": 2}},
    "sandbox_status": {},
    "sandbox_reset": {},
    "json_path_extract": {"text": {"type": "string", "description": "JSON text"}, "path": {"type": "string", "description": "Simple path, e.g. $.a.b[0]", "default": "$"}},
    "querystring_parse_build": {"text": {"type": "string", "description": "Query string or URL"}, "update_key": {"type": "string", "description": "Optional key to update"}, "update_value": {"type": "string", "description": "Optional new value"}},
    "header_cookie_parse": {"text": {"type": "string", "description": "Raw HTTP headers"}},
    "wasm_inventory": {"data": {"type": "string", "description": "WASM bytes as base64 or hex"}, "input_format": {"type": "string", "enum": ["base64", "hex"], "default": "base64"}},
    "har_import_analyze": {"har_text": {"type": "string", "description": "HAR JSON text"}},
    "proxy_import_har": {
        "path": {"type": "string", "description": "Absolute path to a .har / HAR JSON file.", "default": ""},
        "job_id": {"type": "string", "description": "Optional ReverseJob id to store HAR + analysis artifacts.", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "When job_id is set, attach HAR and analysis summary.", "default": True},
    },
    "proxy_import_burp_log": {
        "path": {"type": "string", "description": "Absolute path to a Burp plain-text proxy log.", "default": ""},
        "job_id": {"type": "string", "description": "Optional ReverseJob id to store converted HAR + analysis.", "default": ""},
        "attach_to_job": {"type": "boolean", "description": "When job_id is set, attach HAR and analysis summary.", "default": True},
    },
    "api_inventory_build": {"items_json": {"type": "string", "description": "JSON from browser_trace, proxy/HAR, normalized endpoint list, or newline URL list"}, "source": {"type": "string", "description": "Source label, e.g. browser_trace, proxy, har, static_js", "default": ""}},
    "business_surface_extract": {
        "items_json": {"type": "string", "description": "API inventory JSON, browser_trace/proxy/HAR items, endpoint list, or newline URLs", "default": ""},
        "html": {"type": "string", "description": "Optional HTML source to mine business links/actions", "default": ""},
        "text": {"type": "string", "description": "Optional page text, notes, or endpoint descriptions", "default": ""},
        "source": {"type": "string", "description": "Source label, e.g. api_inventory, browser_trace, proxy, har, static_js", "default": ""},
    },
    "business_test_plan": {
        "surface_json": {"type": "string", "description": "JSON returned by business_surface_extract. The tool returns coverage requirements and gaps, not fixed todos."},
        "depth": {"type": "string", "enum": ["quick", "standard", "deep", "full"], "description": "Requested planning depth for the Agent's later todos", "default": "standard"},
        "roles_json": {"type": "string", "description": "Optional JSON object role->session metadata used to identify evidence gaps", "default": "{}"},
    },
    "business_risk_select": {
        "surface_json": {"type": "string", "description": "JSON returned by business_surface_extract"},
        "asset": {"type": "string", "description": "Optional candidate asset filter, e.g. payment, order, tenant", "default": ""},
        "risk_type": {"type": "string", "description": "Optional candidate risk filter, e.g. object_authorization, state_change, amount_or_quantity", "default": ""},
    },
    "api_inventory_diff": {"old_json": {"type": "string", "description": "Previous api_inventory_build JSON"}, "new_json": {"type": "string", "description": "New api_inventory_build JSON"}},
    "api_spec_import": {"spec": {"type": "string", "description": "OpenAPI v2/v3 (JSON or YAML) or Postman collection JSON, as text"}, "base_url": {"type": "string", "description": "Override/supply the server base URL when the spec has none or a relative one", "default": ""}, "source": {"type": "string", "description": "Source label for the inventory", "default": ""}},
    "api_request_select": {"inventory_json": {"type": "string", "description": "api_inventory_build JSON"}, "protocol": {"type": "string", "description": "Optional protocol filter: http/graphql/websocket/grpc-web", "default": ""}, "tag": {"type": "string", "description": "Optional tag filter: graphql/websocket/grpc-web", "default": ""}, "method": {"type": "string", "description": "Optional HTTP method filter", "default": ""}, "contains": {"type": "string", "description": "Optional URL substring filter", "default": ""}, "limit": {"type": "integer", "description": "Maximum endpoints returned", "default": 20}},
    "graphql_probe": {"endpoint": {"type": "string", "description": "Authorized GraphQL HTTP endpoint"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_json": {"type": "string", "description": "Optional JSON headers, including auth/cookie", "default": "{}"}, "introspect": {"type": "boolean", "description": "Try read-only introspection query", "default": True}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 20}},
    "graphql_batch_probe": {"endpoint": {"type": "string", "description": "Authorized GraphQL HTTP endpoint"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_json": {"type": "string", "description": "Optional JSON headers, including auth/cookie", "default": "{}"}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 20}},
    "websocket_handshake_probe": {"url": {"type": "string", "description": "Authorized ws:// or wss:// URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_json": {"type": "string", "description": "Optional JSON headers, including auth/cookie", "default": "{}"}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 10}},
    "websocket_connect_probe": {"url": {"type": "string", "description": "Authorized ws:// or wss:// URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_json": {"type": "string", "description": "Optional JSON headers, including auth/cookie", "default": "{}"}, "origins_json": {"type": "string", "description": "Optional JSON array of Origin values to test without sending frames.", "default": "[]"}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 10}},
    "websocket_frame_analyze": {"messages_json": {"type": "string", "description": "Captured WebSocket messages/frames JSON from browser hooks, HAR, or proxy output."}},
    "websocket_replay_plan": {"messages_json": {"type": "string", "description": "Captured WebSocket messages/frames JSON"}, "url": {"type": "string", "description": "Optional WebSocket URL", "default": ""}},
    "websocket_send_receive": {"url": {"type": "string", "description": "Authorized ws:// or wss:// URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_json": {"type": "string", "description": "Optional JSON headers, including auth/cookie", "default": "{}"}, "messages_json": {"type": "string", "description": "JSON array of non-destructive messages to send. Hard-capped to 5."}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 10}, "receive_count": {"type": "integer", "description": "Maximum response frames to read per sent message", "default": 3}, "delay_seconds": {"type": "integer", "description": "Delay between sent messages", "default": 1}, "authorization_phrase": {"type": "string", "description": "Must be exactly AUTHORIZED for active WebSocket replay."}},
    "websocket_authz_probe": {"url": {"type": "string", "description": "Authorized ws:// or wss:// URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "baseline_headers_json": {"type": "string", "description": "JSON headers/cookies for the baseline authorized user.", "default": "{}"}, "comparison_headers_json": {"type": "string", "description": "JSON headers/cookies for the second user, logged-out user, or lower-privilege user.", "default": "{}"}, "message_json": {"type": "string", "description": "One non-destructive WebSocket message to compare across both contexts."}, "timeout_seconds": {"type": "integer", "description": "Timeout seconds", "default": 10}, "authorization_phrase": {"type": "string", "description": "Must be exactly AUTHORIZED for active WebSocket authorization comparison."}},
    "grpc_web_analyze": {"headers_json": {"type": "string", "description": "Headers as JSON or raw header lines", "default": "{}"}, "body": {"type": "string", "description": "gRPC-Web body bytes as text/base64/hex"}, "body_format": {"type": "string", "enum": ["text", "base64", "hex"], "default": "text"}},
    "protobuf_field_preview": {"data": {"type": "string", "description": "Protobuf bytes as base64/hex/text"}, "input_format": {"type": "string", "enum": ["base64", "hex", "text"], "default": "base64"}},
    "js_ast_inventory": {"code": {"type": "string", "description": "JavaScript source"}},
    "js_ast_string_array_decode": {"code": {"type": "string", "description": "JavaScript source"}, "max_items": {"type": "integer", "description": "Maximum replacements", "default": 200}},
    "js_function_dependency_graph": {"code": {"type": "string", "description": "JavaScript source"}, "function_name": {"type": "string", "description": "Optional focus function"}},
    "js_hook_template_generate": {"function_name": {"type": "string", "description": "Window function name to hook"}, "expression": {"type": "string", "description": "Optional expression/name"}},
    "wasm_decompile_hint": {"data": {"type": "string", "description": "WASM bytes as base64/hex"}, "input_format": {"type": "string", "enum": ["base64", "hex"], "default": "base64"}},
    "wasm_inspect": {
        "path": {"type": "string", "description": "Absolute or relative path to a .wasm file. Use browser_save_wasm first if you only have the bytes in the browser."},
    },
    "wasm_disassemble": {
        "path": {"type": "string", "description": "Absolute or relative path to a .wasm file."},
        "out_path": {"type": "string", "description": "Optional output path for the WAT. Relative paths are resolved under the current session sandbox; defaults to '<input>.wat' next to the input."},
        "fold_exprs": {"type": "boolean", "description": "Fold the stack-machine into nested expressions (much more readable). Default true.", "default": True},
        "no_check": {"type": "boolean", "description": "Skip strict validation so obfuscated-but-loadable modules disassemble. Default true.", "default": True},
    },
    "wasm_decompile": {
        "path": {"type": "string", "description": "Absolute or relative path to a .wasm file."},
        "out_path": {"type": "string", "description": "Optional output path for the decompiled text. Relative paths are resolved under the current session sandbox; defaults to '<input>.dcmp' next to the input."},
    },
    "wasm_strings": {
        "path": {"type": "string", "description": "Absolute or relative path to a .wasm file."},
        "min_len": {"type": "integer", "description": "Minimum printable-run length (default 6). Raise to 12-16 if results are noisy.", "default": 6},
        "max_results": {"type": "integer", "description": "Cap on returned items in the response (default 200, max 5000). Strings beyond the cap go to '<wasm>.strings.txt' on disk.", "default": 200},
        "where": {"type": "string", "enum": ["data", "all"], "description": "data (default): only walk the data section — cleanest. all: scan every byte.", "default": "data"},
        "out_path": {"type": "string", "description": "Optional output path for the full strings list. Relative paths are resolved under the current session sandbox; defaults to '<wasm>.strings.txt'."},
    },
    "wasm_call_graph": {
        "path": {"type": "string", "description": "Absolute or relative path to a .wasm file."},
        "out_path": {"type": "string", "description": "Optional output path for the call-graph JSON. Relative paths are resolved under the current session sandbox. A markdown sibling ('<...>.md') is written next to it. Defaults to '<input>.callgraph.json'."},
        "max_edges_preview": {"type": "integer", "description": "Max caller-row sample lines included in the response (default 80). Read the JSON or markdown for the full graph.", "default": 80},
    },
    "browser_save_wasm": {
        "out_path": {"type": "string", "description": "Filesystem path to write the captured WASM to. Relative paths are resolved under the current session sandbox; absolute paths are explicit user paths. Refused if it exists unless overwrite=true."},
        "expression": {"type": "string", "description": "JavaScript expression that returns the bytes — must yield a Uint8Array, ArrayBuffer, array of those, or base64 string. Typically reads from a stash populated by a WebAssembly.instantiate hook (see the webassembly_compile recipe), e.g. 'globalThis.__argusai_wasm_samples[0]'."},
        "index": {"type": "integer", "description": "Tab index (from browser_list_tabs). Optional."},
        "url_contains": {"type": "string", "description": "Match a tab whose URL contains this substring (case-insensitive). Optional."},
        "title_contains": {"type": "string", "description": "Match a tab whose title contains this substring (case-insensitive). Optional."},
        "chunk_chars": {"type": "integer", "description": "Base64 chars per page.evaluate round (default 524288 = 512 KB ≈ 384 KB raw). Lower if the browser is unstable; higher if the file is huge and stable.", "default": 524288},
        "max_total_bytes": {"type": "integer", "description": "Safety cap; refuses to write more than this many bytes (default 256 MB).", "default": 268435456},
        "overwrite": {"type": "boolean", "description": "Allow overwriting an existing file at out_path.", "default": False},
    },
    "sqli_probe_param": {"url": {"type": "string", "description": "Authorized URL with query string"}, "param": {"type": "string", "description": "Parameter name"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "sqli_boolean_diff": {"url": {"type": "string", "description": "Authorized URL with query string"}, "param": {"type": "string", "description": "Parameter name"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "sqli_time_probe": {"url": {"type": "string", "description": "Authorized URL with query string"}, "param": {"type": "string", "description": "Parameter name"}, "scope": {"type": "string", "description": "Authorized scope"}, "delay": {"type": "integer", "description": "Short delay seconds, 1-5", "default": 3}},
    "sqlmap_status": {},
    "sqlmap_install_update": {"update": {"type": "boolean", "description": "Update existing managed checkout if present", "default": True}},
    "sqlmap_plan_scan": {
        "url": {"type": "string", "description": "Authorized target URL"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "parameter": {"type": "string", "description": "Optional parameter to focus with -p"},
        "method": {"type": "string", "description": "HTTP method", "default": "GET"},
        "data": {"type": "string", "description": "Optional POST body"},
        "cookie": {"type": "string", "description": "Optional Cookie header"},
        "headers": {"type": "string", "description": "Optional extra headers, one per line"},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Additional sqlmap switches; dangerous switches are blocked"},
        "allow_enumeration": {"type": "boolean", "description": "Allow non-dumping enumeration switches such as --dbs", "default": False},
    },
    "sqlmap_run_scan": {
        "url": {"type": "string", "description": "Authorized target URL"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "parameter": {"type": "string", "description": "Optional parameter to focus with -p"},
        "method": {"type": "string", "description": "HTTP method", "default": "GET"},
        "data": {"type": "string", "description": "Optional POST body"},
        "cookie": {"type": "string", "description": "Optional Cookie header"},
        "headers": {"type": "string", "description": "Optional extra headers, one per line"},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Additional sqlmap switches; dangerous switches are blocked"},
        "allow_enumeration": {"type": "boolean", "description": "Allow non-dumping enumeration switches such as --dbs", "default": False},
        "timeout_seconds": {"type": "integer", "description": "Execution timeout", "default": 180},
    },
    "web_scan_tool_inventory": {},
    "nuclei_status": {},
    "nuclei_plan_scan": {
        "url": {"type": "string", "description": "Authorized target URL"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "tags": {"type": "string", "description": "Optional nuclei -tags filter", "default": ""},
        "severity": {"type": "string", "description": "Severity filter", "default": "medium,high,critical"},
        "rate_limit": {"type": "integer", "description": "Requests per second cap", "default": 50, "minimum": 1, "maximum": 150},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Extra nuclei switches; update/headless/interactsh blocked"},
    },
    "nuclei_run_scan": {
        "url": {"type": "string", "description": "Authorized target URL"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "tags": {"type": "string", "description": "Optional nuclei -tags filter", "default": ""},
        "severity": {"type": "string", "description": "Severity filter", "default": "medium,high,critical"},
        "rate_limit": {"type": "integer", "description": "Requests per second cap", "default": 50, "minimum": 1, "maximum": 150},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Extra nuclei switches; update/headless/interactsh blocked"},
        "timeout_seconds": {"type": "integer", "description": "Execution timeout", "default": 180, "minimum": 30, "maximum": 900},
    },
    "ffuf_status": {},
    "ffuf_plan_scan": {
        "url": {"type": "string", "description": "Base URL or URL containing FUZZ keyword"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "wordlist": {"type": "string", "description": "Optional wordlist path; empty = bundled common_paths.txt", "default": ""},
        "match_codes": {"type": "string", "description": "HTTP status codes to match", "default": "200,204,301,302,307,401,403,405"},
        "rate": {"type": "integer", "description": "Requests per second cap", "default": 50, "minimum": 1, "maximum": 150},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Extra ffuf switches; recursion/replay blocked"},
    },
    "ffuf_run_scan": {
        "url": {"type": "string", "description": "Base URL or URL containing FUZZ keyword"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "wordlist": {"type": "string", "description": "Optional wordlist path; empty = bundled common_paths.txt", "default": ""},
        "match_codes": {"type": "string", "description": "HTTP status codes to match", "default": "200,204,301,302,307,401,403,405"},
        "rate": {"type": "integer", "description": "Requests per second cap", "default": 50, "minimum": 1, "maximum": 150},
        "extra_args": {"type": "array", "items": {"type": "string"}, "description": "Extra ffuf switches; recursion/replay blocked"},
        "timeout_seconds": {"type": "integer", "description": "Execution timeout", "default": 120, "minimum": 30, "maximum": 900},
    },
    "sqli_union_plan": {"max_columns": {"type": "integer", "description": "Maximum UNION column count", "default": 8}},
    "nosql_injection_probe": {},
    "oob_payload_generate": {
        "category": {"type": "string", "description": "Payload category: 'ssrf', 'xxe', 'rce', 'sqli', 'blind-xss', or 'generic'. Determines which template payloads are included in the response."},
        "callback_base": {"type": "string", "description": "Only used in disabled/manual mode. Optional alternative callback base URL when auto Interactsh is off."},
    },
    "oob_check_events": {
        "token": {"type": "string", "description": "The short label returned by oob_payload_generate (e.g. 'a1b2c3d4'). Only events whose JSON contains this token are returned under 'hits'. Pass empty string to see every event under the current correlation id."},
        "events_json": {"type": "string", "description": "Manual mode only — JSON array of events copied from an external OOB service (Burp Collaborator export, etc.) to correlate against the token."},
        "wait_seconds": {"type": "number", "description": "If > 0, block here until at least one matching event lands or this many seconds elapse. Use 10-30 right after firing a payload so you can read the callback in the same tool call instead of polling."},
        "since": {"type": "number", "description": "Unix timestamp; only events received at or after this time are returned. Useful for filtering out callbacks from earlier probes."},
    },
    "oob_status": {},
    "oob_start": {"force": {"type": "boolean", "description": "Set true to deregister the current session and start a fresh one (rare — only when the correlation id has been compromised or the current session is misbehaving)."}},
    "oob_stop": {},
    "auth_session_inventory": {"headers_a": {"type": "object", "description": "Role/session A headers"}, "headers_b": {"type": "object", "description": "Role/session B headers"}},
    "idor_compare_request": {"url": {"type": "string", "description": "Authorized object URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "headers_a": {"type": "object", "description": "Headers for own role"}, "headers_b": {"type": "object", "description": "Headers for compared role"}, "alt_url": {"type": "string", "description": "Optional alternate object URL"}},
    "role_matrix_probe": {"urls": {"type": "array", "items": {"type": "string"}, "description": "Authorized URLs"}, "scope": {"type": "string", "description": "Authorized scope"}, "roles_json": {"type": "string", "description": "JSON object role->headers"}},
    "business_state_probe": {"steps_json": {"type": "string", "description": "JSON array of workflow steps"}},
    "upload_surface_discover": {"url": {"type": "string", "description": "Authorized page URL"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "upload_payload_matrix": {},
    "upload_verify_access": {"url": {"type": "string", "description": "Authorized uploaded file URL"}, "marker": {"type": "string", "description": "Marker text", "default": "argusai-upload-probe"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "ssrf_probe_url_param": {"url": {"type": "string", "description": "Authorized URL with query string"}, "param": {"type": "string", "description": "URL parameter"}, "payload": {"type": "string", "description": "SSRF payload URL", "default": "http://127.0.0.1"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "ssrf_redirect_chain_test": {"url": {"type": "string", "description": "Authorized target URL"}, "redirect_url": {"type": "string", "description": "Controlled redirect URL"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "ssrf_metadata_template": {"provider": {"type": "string", "enum": ["aws", "gcp", "azure", "local"], "default": "aws"}},
    "cors_matrix_probe": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}, "origins": {"type": "array", "items": {"type": "string"}, "description": "Origins to test"}},
    "cors_exploit_verify": {
        "url": {"type": "string", "description": "Authorized sensitive endpoint for CORS exploitability verification from an attacker origin"},
        "scope": {"type": "string", "description": "Authorized scope"},
        "attacker_origin": {"type": "string", "description": "Simulated attacker origin, e.g. https://evil.example or null", "default": "https://evil.example"},
        "method": {"type": "string", "enum": ["GET", "POST", "HEAD"], "description": "Browser fetch method", "default": "GET"},
        "headers_json": {"type": "string", "description": "Optional JSON object of browser-settable request headers", "default": "{}"},
        "data": {"type": "string", "description": "Optional POST body", "default": ""},
        "cookie": {"type": "string", "description": "Optional Cookie header value to inject into the browser for the target URL when no attached real browser session is available", "default": ""},
        "cookie_same_site": {"type": "string", "enum": ["None", "Lax", "Strict"], "description": "SameSite value used for manually injected cookies", "default": "None"},
        "sensitive_regex": {"type": "string", "description": "Optional regex that marks readable response body as sensitive evidence", "default": ""},
        "max_body_chars": {"type": "integer", "description": "Maximum readable body excerpt to return", "default": 2000},
        "fresh_context": {"type": "boolean", "description": "Use a fresh managed browser context before verification", "default": False},
    },
    "csrf_poc_generate": {"action_url": {"type": "string", "description": "Form action URL"}, "method": {"type": "string", "description": "HTTP method", "default": "POST"}, "fields_json": {"type": "string", "description": "JSON object of form fields"}},
    "oauth_flow_analyze": {"url": {"type": "string", "description": "OAuth/OIDC URL"}, "html": {"type": "string", "description": "Optional page HTML"}},
    "jwt_attack_surface": {"token": {"type": "string", "description": "JWT token"}},
    "saml_surface_analyze": {"xml_or_html": {"type": "string", "description": "SAML XML or page HTML"}},
    "http_desync_probe": {"url": {"type": "string", "description": "Authorized URL"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "cache_poison_probe": {"url": {"type": "string", "description": "Authorized URL"}, "header_name": {"type": "string", "description": "Header to test", "default": "X-Forwarded-Host"}, "header_value": {"type": "string", "description": "Marker value", "default": "argusai.invalid"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "raw_http_send": {"url": {"type": "string", "description": "Authorized URL"}, "raw_request": {"type": "string", "description": "Raw HTTP/1 request"}, "scope": {"type": "string", "description": "Authorized scope"}},
    "create_poc_page": {
        "html_content": {"type": "string", "description": "HTML document or body fragment for the local PoC page"},
        "title": {"type": "string", "description": "Page title", "default": "ArgusAI PoC"},
        "filename": {"type": "string", "description": "Optional safe filename slug"},
        "wrap": {"type": "boolean", "description": "Wrap fragment in a full HTML document", "default": True},
        "open_browser": {"type": "boolean", "description": "Open the generated page in the system browser after writing", "default": False},
    },
    "open_poc_page": {
        "path_or_uri": {"type": "string", "description": "Path or file:// URI returned by create_poc_page"},
    },
    "user_todo_create": {
        "title": {"type": "string", "description": "Todo title"},
        "body": {"type": "string", "description": "Optional details", "default": ""},
        "due_at": {"type": "number", "description": "Optional Unix timestamp deadline"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags"},
        "status": {"type": "string", "description": "pending|in_progress|completed|cancelled", "default": "pending"},
    },
    "user_todo_list": {
        "status": {"type": "string", "description": "Filter by status", "default": ""},
        "tag": {"type": "string", "description": "Filter by tag", "default": ""},
        "limit": {"type": "integer", "description": "Max items", "default": 50},
    },
    "user_todo_update": {
        "todo_id": {"type": "string", "description": "Todo id"},
        "title": {"type": "string", "description": "New title", "default": ""},
        "body": {"type": "string", "description": "New body", "default": ""},
        "status": {"type": "string", "description": "New status", "default": ""},
        "clear_due_at": {"type": "boolean", "description": "Clear deadline", "default": False},
    },
    "user_todo_complete": {"todo_id": {"type": "string", "description": "Todo id"}},
    "user_todo_delete": {"todo_id": {"type": "string", "description": "Todo id"}},
    "user_profile_read": {},
    "user_profile_suggest_update": {
        "patch": {"type": "object", "description": "Partial profile fields to suggest"},
        "reason": {"type": "string", "description": "Why this update is suggested", "default": ""},
    },
    "user_profile_apply_update": {
        "patch": {"type": "object", "description": "Partial profile fields to apply"},
    },
    "desktop_observe": {
        "mode": {"type": "string", "description": "foreground|windows|programs|monitors|all", "default": "foreground"},
        "screenshot": {"type": "boolean", "description": "Capture screenshot to session sandbox", "default": False},
        "screenshot_path": {"type": "string", "description": "Relative sandbox path (.jpg default)", "default": ""},
        "monitor": {
            "type": "integer",
            "description": "Monitor index (0-based). -1 = all displays stitched (virtual desktop).",
            "default": -1,
        },
        "hwnd": {
            "type": "integer",
            "description": "Optional window HWND from windows[]. When set with screenshot=true, capture that window instead of a full monitor.",
            "default": 0,
        },
        "screenshot_max_width": {
            "type": "integer",
            "description": "Max JPEG/PNG width after capture. Default -1 = auto from the widest monitor (up to 3840 for 4K). 0 keeps native resolution. Explicit values such as 1920/2560/3840 are also accepted.",
            "default": -1,
        },
        "screenshot_quality": {
            "type": "integer",
            "description": "JPEG quality 1-100 when saving as .jpg (default 90). Raise toward 95-100 if UI text looks blurry; use screenshot_path ending in .png for lossless.",
            "default": 90,
        },
    },
    "desktop_launch_app": {
        "name": {"type": "string", "description": "App name or executable name, e.g. QQMusic.exe, QQ音乐, Google Chrome"},
        "executable_path": {"type": "string", "description": "Optional explicit executable/app/shortcut path", "default": ""},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Optional app arguments (not shell-expanded)", "default": []},
    },
    "desktop_notify": {
        "title": {"type": "string", "description": "Short notification title", "default": "ArgusAI"},
        "message": {"type": "string", "description": "Notification body (shown locally to the user)"},
        "level": {
            "type": "string",
            "enum": ["info", "success", "warning", "error"],
            "description": "Visual severity hint",
            "default": "info",
        },
    },
    "desktop_action_session_start": {
        "minutes": {"type": "integer", "description": "Ignored. Timed desktop authorization is retired.", "default": 5},
        "scope": {"type": "string", "description": "Optional note. No authorization session is created.", "default": ""},
    },
    "desktop_action_session_stop": {},
    "desktop_action_session_status": {},
    "desktop_mouse_move": {
        "x": {"type": "integer", "description": "Screen X, or window-relative X when hwnd is set"},
        "y": {"type": "integer", "description": "Screen Y, or window-relative Y when hwnd is set"},
        "hwnd": {
            "type": "integer",
            "description": "Optional window HWND. When set, focus that window and treat x/y as coordinates relative to its top-left.",
            "default": 0,
        },
    },
    "desktop_click": {
        "x": {"type": "integer", "description": "Screen X, or window-relative X when hwnd is set"},
        "y": {"type": "integer", "description": "Screen Y, or window-relative Y when hwnd is set"},
        "button": {"type": "string", "description": "left|right", "default": "left"},
        "hwnd": {
            "type": "integer",
            "description": "Optional window HWND. When set, focus that window and treat x/y as coordinates relative to its top-left.",
            "default": 0,
        },
        "image_x": {"type": "integer", "description": "Vision fallback: target X in a desktop_observe screenshot (pixels). Requires grounding."},
        "image_y": {"type": "integer", "description": "Vision fallback: target Y in a desktop_observe screenshot (pixels). Requires grounding."},
        "grounding": {"type": "object", "description": "The screenshot_grounding dict returned by desktop_observe; converts image_x/image_y to absolute desktop coords."},
    },
    "desktop_focus_window": {
        "hwnd": {"type": "integer", "description": "Window HWND from desktop_observe windows[]"},
    },
    "desktop_uia_find": {
        "hwnd": {"type": "integer", "description": "Window HWND to search under (0 = desktop root)", "default": 0},
        "name": {"type": "string", "description": "Exact UIA Name", "default": ""},
        "name_contains": {"type": "string", "description": "Case-insensitive substring match on Name", "default": ""},
        "automation_id": {"type": "string", "description": "UIA AutomationId", "default": ""},
        "control_type": {
            "type": "string",
            "description": "UIA control type, e.g. edit|button|listitem|text|document|treeitem|menuitem",
            "default": "",
        },
        "limit": {"type": "integer", "description": "Max elements to return (1-80)", "default": 30},
        "include_offscreen": {"type": "boolean", "description": "Include off-screen elements", "default": False},
    },
    "desktop_uia_action": {
        "action": {
            "type": "string",
            "enum": ["focus", "invoke", "set_value", "select", "toggle", "click"],
            "description": (
                "UIA action. Prefer click over invoke for Chromium/QQ/WeChat search "
                "list items when invoke is a no-op."
            ),
            "default": "focus",
        },
        "hwnd": {"type": "integer", "description": "Window HWND scope (0 = desktop root)", "default": 0},
        "runtime_id": {"type": "string", "description": "Element runtime_id from desktop_uia_find", "default": ""},
        "name": {"type": "string", "description": "Exact UIA Name", "default": ""},
        "name_contains": {"type": "string", "description": "Substring match on Name", "default": ""},
        "automation_id": {"type": "string", "description": "UIA AutomationId", "default": ""},
        "control_type": {"type": "string", "description": "Optional control_type filter", "default": ""},
        "value": {"type": "string", "description": "Text for set_value", "default": ""},
    },
    "desktop_type": {"text": {"type": "string", "description": "Text to type"}},
    "desktop_hotkey": {
        "keys": {
            "type": ["array", "string"],
            "items": {"type": "string"},
            "description": (
                "Hotkey chord. Prefer array tokens: [\"enter\"], [\"down\"], [\"ctrl\",\"a\"], "
                "[\"escape\"]. String forms \"enter\" / \"ctrl+a\" are also accepted. "
                "Do NOT pass character lists like [\"e\",\"n\",\"t\",\"e\",\"r\"]."
            ),
        },
    },
    "session_sandbox_status": {},
    "runtime_install": {
        "runtime": {
            "type": "string",
            "enum": ["python", "node", "wabt"],
            "description": "Managed toolchain to install or repair under data_root/tools/runtimes. 'wabt' supplies the WASM CLI tools used by wasm_disassemble / wasm_decompile.",
        },
        "force": {
            "type": "boolean",
            "description": "Repair by downloading and atomically replacing the current runtime.",
            "default": False,
        },
    },
    "sandbox_pip_install": {
        "packages": {
            "type": "string",
            "description": "Package names or PEP508 specs: plain names separated by spaces/commas (e.g. 'requests' or 'pygame pyinstaller'). Do NOT pass JSON arrays like '[\"pyinstaller\"]' — use pyinstaller or 'pygame pyinstaller'.",
        },
        "upgrade": {"type": "boolean", "description": "Pass --upgrade to pip", "default": False},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 30-1800 (default 300 for large wheels).", "default": 300},
    },
    "discover_shell_environments": {},
    "discover_python_environments": {},
    "cron_list": {
        "tag": {"type": "string", "description": "Optional tag filter (e.g. defense, proxy).", "default": ""},
    },
    "cron_runs": {
        "job_id": {"type": "string", "description": "Cron job id"},
        "limit": {"type": "integer", "description": "Max runs to return (default 20)", "default": 20},
    },
    "cron_upsert": {
        "name": {"type": "string", "description": "Short job name"},
        "prompt": {"type": "string", "description": "Stable prompt executed each tick (prefer KB playbook reference)"},
        "every_seconds": {"type": "integer", "description": "Interval seconds when schedule_kind=interval (min 1; scheduler ticks every ~1s)", "default": 3600},
        "schedule_kind": {
            "type": "string",
            "enum": ["interval", "cron", "webhook", "file_change"],
            "default": "interval",
        },
        "cron_expr": {"type": "string", "description": "5-field cron expr when schedule_kind=cron", "default": ""},
        "webhook_debounce_seconds": {
            "type": "integer",
            "description": "Debounce seconds when schedule_kind=webhook",
            "default": 0,
        },
        "watch_path": {
            "type": "string",
            "description": "File or directory path when schedule_kind=file_change",
            "default": "",
        },
        "watch_glob": {
            "type": "string",
            "description": "Glob under watch_path, e.g. *.json or **/*.py",
            "default": "*",
        },
        "watch_recursive": {"type": "boolean", "default": True},
        "watch_poll_seconds": {"type": "integer", "default": 2},
        "watch_debounce_seconds": {"type": "integer", "default": 2},
        "destination": {
            "type": "string",
            "enum": ["new_session", "dedicated_session", "session"],
            "default": "new_session",
            "description": "new_session=new chat per run; dedicated_session=one job-owned persistent chat; session=wake an existing/current chat",
        },
        "session_id": {
            "type": "string",
            "description": "Target chat when destination=session; omit to use the current conversation",
            "default": "",
        },
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags e.g. defense", "default": []},
        "job_id": {"type": "string", "description": "Existing id to update; omit to create", "default": ""},
        "enabled": {"type": "boolean", "default": True},
        "max_runs": {
            "type": "integer",
            "minimum": 0,
            "description": "Maximum actual runs before automatic disable; 0 means unlimited.",
            "default": 0,
        },
        "context_json": {"type": "string", "description": "Optional JSON object of frozen context", "default": ""},
        "on_change_only": {"type": "boolean", "default": True},
        "report_to_origin": {
            "type": "boolean",
            "default": True,
            "description": "When destination=new_session, post a short result into the creating chat (Codex Triage-style)",
        },
    },
    "cron_set_enabled": {
        "job_id": {"type": "string"},
        "enabled": {"type": "boolean", "default": True},
    },
    "cron_delete": {
        "job_id": {"type": "string"},
    },
    "cron_grant_high_risk": {
        "job_id": {
            "type": "string",
            "description": "Scheduled job receiving the explicit high-risk acknowledgement",
        },
        "granted": {"type": "boolean", "default": True},
    },
    "local_attack_surface_snapshot": {
        "include_firewall": {"type": "boolean", "description": "Include firewall summary if readable", "default": True},
    },
    "sandbox_write_file": {
        "path": {"type": "string", "description": "Relative sandbox path, e.g. notes/result.txt or scripts/test.py"},
        "content": {"type": "string", "description": "Full text content to write (creates or replaces entire file). Prefer sandbox_patch_file for small edits."},
        "file_type": {"type": "string", "description": "Optional type label: txt/md/json/html/py/ps1/bat/cmd/sh"},
    },
    "sandbox_patch_file": {
        "path": {"type": "string", "description": "Relative sandbox path of an existing text/script file to edit in place"},
        "old_string": {"type": "string", "description": "Cursor-style exact substring to replace (must be unique unless replace_all). Copy verbatim from sandbox_read_file content_raw — never from the numbered `   12|` display lines."},
        "new_string": {"type": "string", "description": "Replacement text. Empty string deletes old_string."},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false requires a unique match.", "default": False},
    },
    "sandbox_read_file": {
        "path": {"type": "string", "description": "Relative sandbox path to read. Raster images are attached for vision; you can also inspect host/project images with vision_inspect_image(path=...) or read_text_file without copying them here."},
        "offset": {"type": "integer", "description": "1-based start line for line-mode read (default 1). Use end_line+1 from previous page.", "default": 1},
        "limit": {"type": "integer", "description": "Max lines to return in line mode (default 200, max 800).", "default": 200},
        "start_line": {"type": "integer", "description": "Optional alias for offset when end_line is set.", "default": 0},
        "end_line": {"type": "integer", "description": "Optional inclusive end line; overrides limit when >= start_line.", "default": 0},
        "read_mode": {"type": "string", "enum": ["", "lines", "chars"], "description": "Default lines. Use chars (or offset_chars>0) for minified single-line files.", "default": ""},
        "offset_chars": {"type": "integer", "description": "Char offset for read_mode=chars / minified files.", "default": 0},
        "max_chars": {"type": "integer", "description": "Char window size when read_mode=chars.", "default": 20000},
    },
    "sandbox_stat_file": {
        "path": {"type": "string", "description": "Relative sandbox path. Returns size_bytes, line_count, large_file hint."},
    },
    "sandbox_grep_file": {
        "path": {"type": "string", "description": "Relative sandbox path to search"},
        "pattern": {"type": "string", "description": "Regex (or literal if regex=false)"},
        "mode": {"type": "string", "enum": ["content", "context", "files_with_matches", "count"], "description": "content=line hits; context=char windows for minified JS; count/files_with_matches for cheap probes.", "default": "content"},
        "max_matches": {"type": "integer", "description": "Cap hits in content/context mode (default 50).", "default": 50},
        "context_chars": {"type": "integer", "description": "Chars on each side in context mode (default 200).", "default": 200},
        "case_insensitive": {"type": "boolean", "description": "Case-insensitive regex.", "default": False},
        "regex": {"type": "boolean", "description": "Treat pattern as regex (default) or literal substring.", "default": True},
    },
    "sandbox_list_files": {},
    "sandbox_delete_file": {
        "path": {"type": "string", "description": "Relative sandbox path to delete"},
    },
    "sandbox_open_file": {
        "path": {"type": "string", "description": "Relative sandbox path of the file to hand off to the OS default application (Markdown viewer, image viewer, browser, PDF reader, etc.). Must be inside the current session sandbox; restricted to viewable file types (text, md, html, css, js, json, yaml, csv, log, xml, images, pdf)."},
    },
    "sandbox_reveal_in_folder": {
        "path": {"type": "string", "description": "Optional relative sandbox path. Empty string opens the sandbox root in the OS file manager; a file path opens the parent folder with that file highlighted (Windows Explorer /select, macOS Finder reveal)."},
    },
    "sandbox_reset_files": {
        "confirm_label": {"type": "string", "description": "Must be RESET to clear current session sandbox files"},
    },
    "run_python_script": {
        "path": {"type": "string", "description": "Relative path of a .py file inside the current session sandbox. Runs in the sandbox .venv; packages installed by the script stay in that venv."},
        "python_env": {"type": "string", "description": "Deprecated for sandbox tools — ignored/rejected. Use run_host_python_script for host interpreter escalation."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments passed as argv, no shell expansion"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30). For long jobs (SQLi blind, crawls) raise this; on timeout the process is killed but partial stdout/stderr is still returned with timed_out=true.", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach after a short settle window and return ok/detached/pid without waiting for exit. Required for GUI apps / servers so the default timeout does not kill a process that already started.", "default": False},
    },
    "run_shell_script": {
        "path": {"type": "string", "description": "Relative path of a .ps1/.cmd/.bat/.sh file inside the current session sandbox"},
        "shell": {"type": "string", "description": "Shell to use: powershell, pwsh, cmd, bash, sh"},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments passed as argv, no shell expansion"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30). On timeout the partial output is still returned.", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach after settle for GUI/servers (returns detached=true + pid). Do not use start/pythonw tricks to fake backgrounding.", "default": False},
    },
    "run_python_snippet": {
        "code": {"type": "string", "description": "Python code to write to a temporary sandbox file and run in the sandbox .venv. sys.executable points to that venv, so pip installs stay sandbox-local."},
        "python_env": {"type": "string", "description": "Deprecated for sandbox tools — ignored/rejected. Use run_host_python_snippet for host interpreter escalation."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments passed as argv"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30). For SQLi blind / crawls / brute force pick 300-1200; on timeout the process is killed but partial stdout/stderr you already captured is still returned (timed_out=true, partial_output=true).", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach after settle for GUI/servers so timeout does not kill a successfully started process.", "default": False},
    },
    "run_shell_snippet": {
        "script": {"type": "string", "description": "Shell script snippet to write to a temporary sandbox file and run"},
        "shell": {"type": "string", "description": "Shell to use: powershell, pwsh, cmd, bash, sh"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30). On timeout the partial output is still returned. A timeout on a GUI launch usually means the app is still running — use background=true or shell_session_start next time.", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach after a short settle window (ok=true, detached=true, pid, session_id oneshot). Prefer shell_session_start for multi-step interactive work.", "default": False},
    },
    "shell_session_start": {
        "shell": {"type": "string", "description": "Shell: powershell, pwsh, cmd, bash, sh (default: powershell on Windows, bash elsewhere)"},
        "initial_command": {"type": "string", "description": "Optional first command to exec after the shell starts", "default": ""},
        "label": {"type": "string", "description": "Optional short label for shell_session_list", "default": ""},
    },
    "shell_session_exec": {
        "session_id": {"type": "string", "description": "Interactive session_id from shell_session_start"},
        "command": {"type": "string", "description": "Single-line command to write to session stdin"},
    },
    "shell_session_write": {
        "session_id": {"type": "string", "description": "Interactive session_id from shell_session_start"},
        "data": {"type": "string", "description": "Raw stdin payload (may be multi-line)"},
    },
    "shell_session_list": {},
    "shell_session_read": {
        "session_id": {"type": "string", "description": "session_id from start or background oneshot"},
        "offset": {"type": "integer", "description": "Byte offset into the session log (use next_offset from prior read)", "default": 0},
        "max_chars": {"type": "integer", "description": "Max characters to return, 1-80000", "default": 12000},
    },
    "shell_session_stop": {
        "session_id": {"type": "string", "description": "session_id to stop (kills process tree)"},
    },
    "run_host_python_script": {
        "path": {"type": "string", "description": "Relative path of a .py file inside the current session sandbox. Executed with a host Python interpreter (outside the sandbox .venv)."},
        "reason": {"type": "string", "description": "Required: why the sandbox .venv cannot run this (e.g. tkinter missing, host-only stdlib). Shown on the confirmation card."},
        "sandbox_attempt_summary": {"type": "string", "description": "Required: what you already tried in run_python_snippet/run_python_script and the failure evidence."},
        "python_env": {"type": "string", "description": "Optional host interpreter path or name from discover_python_environments. Empty selects the best available host Python."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments passed as argv, no shell expansion"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30).", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach for host-side GUI apps (Flet/Tk/etc.) so timeout does not kill an already-open window.", "default": False},
    },
    "run_host_python_snippet": {
        "code": {"type": "string", "description": "Python source materialised under .tmp/host-snippet-*.py and executed with a host interpreter."},
        "reason": {"type": "string", "description": "Required: why sandbox execution is insufficient (confirmation card)."},
        "sandbox_attempt_summary": {"type": "string", "description": "Required: prior sandbox attempt and failure evidence."},
        "python_env": {"type": "string", "description": "Optional host interpreter from discover_python_environments."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments passed as argv"},
        "timeout_seconds": {"type": "integer", "description": "Timeout seconds, 1-1800 (default 30).", "default": 30},
        "background": {"type": "boolean", "description": "If true, detach for host-side GUI apps so timeout does not kill an already-open window.", "default": False},
    },
    "nmap_scan": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP scope"},
        "ports": {"type": "string", "description": "Port list, top-100, or all"},
        "service_detection": {"type": "boolean", "description": "Use -sV service detection"},
        "aggressive": {"type": "boolean", "description": "Use aggressive -A scan; high risk"},
    },
    "network_discover_hosts": {
        "target": {"type": "string", "description": "Authorized host, IP, or CIDR. CIDR enumeration is capped by max_hosts."},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP/CIDR scope. Required; empty scope is rejected."},
        "max_hosts": {"type": "integer", "description": "Maximum hosts to enumerate from CIDR, 1-256", "default": 64},
    },
    "network_scan_ports": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP/CIDR scope. Required; empty scope is rejected."},
        "ports": {"type": "string", "description": "Port list/ranges such as '22,80,443,8000-8100' or 'common'. Avoid 'all' unless explicitly authorized.", "default": "common"},
        "timeout_seconds": {"type": "integer", "description": "Per-port timeout, 1-30", "default": 3},
        "concurrency": {"type": "integer", "description": "Concurrent connect probes, 1-256", "default": 64},
        "max_ports": {"type": "integer", "description": "Maximum ports to scan, 1-1024", "default": 256},
    },
    "network_fingerprint_services": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP/CIDR scope. Required; empty scope is rejected."},
        "ports": {"type": "string", "description": "Authorized ports to fingerprint", "default": "common"},
        "timeout_seconds": {"type": "integer", "description": "Per-port timeout, 1-30", "default": 3},
    },
    "network_profile_host": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP/CIDR scope. Required; empty scope is rejected."},
        "ports": {"type": "string", "description": "Authorized ports to profile", "default": "common"},
        "timeout_seconds": {"type": "integer", "description": "Per-port timeout, 1-30", "default": 3},
    },
    "network_assess_exposure": {
        "profile_json": {"type": "string", "description": "JSON host profile returned by network_profile_host/network_scan_ports, or wrapper containing a profile field."},
    },
    "network_protocol_assess": {
        "profile_json": {"type": "string", "description": "JSON host profile returned by network_profile_host/network_scan_ports, or wrapper containing a profile field."},
    },
    "vuln_intel_match": {
        "profile_json": {"type": "string", "description": "JSON host profile to match against offline CPE/CVE risk candidates."},
        "fingerprint_json": {"type": "string", "description": "Optional single service fingerprint JSON. Use either profile_json or fingerprint_json."},
    },
    "weak_credential_audit_plan": {
        "service": {"type": "string", "description": "Optional service to plan for: http_basic, ftp, redis, ssh, rdp, smb."},
        "profile_json": {"type": "string", "description": "Optional host profile JSON. The planner suggests which services are safe/unsafe to audit."},
    },
    "weak_credential_check": {
        "service": {"type": "string", "enum": ["http_basic", "ftp", "redis"], "description": "Supported low-rate audit service."},
        "target": {"type": "string", "description": "Authorized target host/IP or URL."},
        "scope": {"type": "string", "description": "Comma-separated authorized scope. Required; target must be inside scope."},
        "credentials_json": {"type": "string", "description": "JSON array of credential objects: [{\"username\":\"admin\",\"password\":\"admin\"}]. Keep small to avoid lockout."},
        "port": {"type": "integer", "description": "Service port. Defaults: HTTP 80, FTP 21, Redis 6379.", "default": 0},
        "url": {"type": "string", "description": "For http_basic, optional full URL to request."},
        "use_default_candidates": {"type": "boolean", "description": "Append a tiny built-in default credential list. Requires explicit user approval.", "default": False},
        "max_attempts": {"type": "integer", "description": "Maximum attempts, hard-capped at 10.", "default": 6},
        "delay_seconds": {"type": "integer", "description": "Delay between attempts, minimum 1 second.", "default": 2},
        "timeout_seconds": {"type": "integer", "description": "Per-attempt timeout, 1-30 seconds.", "default": 5},
        "stop_on_success": {"type": "boolean", "description": "Stop at first successful credential.", "default": True},
        "authorization_phrase": {"type": "string", "description": "Must be exactly AUTHORIZED for this high-risk audit step."},
    },
    "pentest_plan_chain": {
        "target": {"type": "string", "description": "Authorized target host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized scope. Required; target must be inside scope."},
        "objective": {"type": "string", "description": "Authorized test objective"},
        "rules_json": {"type": "string", "description": "Optional ROE JSON with allowed_actions, forbidden_actions, rate_limit, time_window."},
    },
    "pentest_verify_finding": {
        "target": {"type": "string", "description": "Authorized target host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized scope. Required; target must be inside scope."},
        "finding_json": {"type": "string", "description": "Single finding JSON to verify minimally."},
        "authorization_phrase": {"type": "string", "description": "Must be exactly AUTHORIZED for this high-risk verification step."},
    },
    "post_exploit_map_impact": {
        "target": {"type": "string", "description": "Authorized target host/IP or asset label."},
        "findings_json": {"type": "string", "description": "JSON array of findings to map into impact hypotheses."},
        "profile_json": {"type": "string", "description": "JSON host profile from network_profile_host."},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "description": "Optional case/artifact/evidence references."},
    },
    "post_exploit_plan_actions": {
        "assessment_json": {"type": "string", "description": "JSON returned by post_exploit_map_impact or its assessment field."},
        "mode": {"type": "string", "enum": ["real_target", "sandbox_lab"], "description": "real_target only proposes minimal verification; sandbox_lab proposes local harness generation.", "default": "real_target"},
    },
    "post_exploit_lab_plan": {
        "finding_json": {"type": "string", "description": "Finding JSON used to generate a sandbox-only local harness plan."},
        "profile_json": {"type": "string", "description": "Optional host profile JSON for sample evidence."},
    },
    "post_exploit_lab_generate": {
        "finding_json": {"type": "string", "description": "Finding JSON used to write sandbox-only harness files."},
        "profile_json": {"type": "string", "description": "Optional host profile JSON for sample evidence."},
    },
    "ping_host": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP scope"},
        "count": {"type": "integer", "description": "Packet count, max 10"},
    },
    "dns_lookup": {
        "target": {"type": "string", "description": "Authorized host"},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope"},
        "record_type": {"type": "string", "description": "DNS record type, e.g. A, AAAA, MX, TXT"},
    },
    "trace_route": {
        "target": {"type": "string", "description": "Authorized host or IP"},
        "scope": {"type": "string", "description": "Comma-separated authorized host/IP scope"},
        "max_hops": {"type": "integer", "description": "Maximum hops, max 30"},
    },
    # ------------------------------------------------------------------
    # Generic file I/O — "two-step" read+grep tools.
    # ------------------------------------------------------------------
    "read_text_file": {
        "path": {"type": "string", "description": "Absolute or relative path to a text file or raster image on disk. Images are attached for vision in place — do not copy them into the session sandbox just to look."},
        "offset": {"type": "integer", "description": "1-indexed line number to start at. Negative values count from EOF (-1 = last line). Default 1.", "default": 1},
        "limit": {"type": "integer", "description": "Max lines to return per call (default 200, max 800). When ``more=true`` in the response, call again with offset=end_line+1.", "default": 200},
        "read_mode": {"type": "string", "enum": ["", "lines", "chars"], "description": "Default lines. Use chars (or offset_chars>0) for minified single-line files.", "default": ""},
        "offset_chars": {"type": "integer", "description": "Char offset for read_mode=chars / minified files.", "default": 0},
        "encoding": {"type": "string", "description": "Optional override (e.g. 'utf-8', 'utf-16', 'latin-1'). Default: autodetect."},
        "line_trunc": {"type": "integer", "description": "Per-line char truncation. Long lines are clipped so a minified-JS one-liner doesn't blow the budget. Default 400.", "default": 400},
    },
    "stat_text_file": {
        "path": {"type": "string", "description": "Absolute or relative path to a file. Use BEFORE read_text_file when size is unknown — returns size_bytes, line_count, large_file, encoding_hint."},
    },
    "patch_text_file": {
        "path": {"type": "string", "description": "Absolute or relative path of an existing text file on disk to edit in place"},
        "old_string": {"type": "string", "description": "Exact substring to replace. Copy verbatim from read_text_file content_raw."},
        "new_string": {"type": "string", "description": "Replacement text. Empty string deletes old_string."},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false requires a unique match.", "default": False},
    },
    "write_text_file": {
        "path": {"type": "string", "description": "Absolute or relative path for a new or existing text file on disk"},
        "content": {"type": "string", "description": "Full text content to write (creates or replaces entire file). Prefer patch_text_file for small edits."},
    },
    "grep_text": {
        "pattern": {"type": "string", "description": "Regex (or literal substring if regex=false) to search for."},
        "path": {"type": "string", "description": "Path to the single file to search."},
        "mode": {"type": "string", "enum": ["content", "context", "files_with_matches", "count"], "description": "content (default) → line-oriented {line,text} records. context → CHARACTER-oriented {offset,end,before,match,after} windows that walk past newlines; use this for single-line minified JS where content mode is useless. files_with_matches → only {matched:bool}. count → total hits.", "default": "content"},
        "max_matches": {"type": "integer", "description": "Cap on returned hits in 'content' or 'context' mode (default 50, max 200).", "default": 50},
        "line_trunc": {"type": "integer", "description": "Per-matched-line char clip in 'content' mode (default 300).", "default": 300},
        "context_chars": {"type": "integer", "description": "Per-side window size in 'context' mode (default 200, max 2000). Use larger windows when reading minified-JS deobfuscation hooks; smaller for token-only triage.", "default": 200},
        "case_insensitive": {"type": "boolean", "description": "Case-insensitive regex.", "default": False},
        "regex": {"type": "boolean", "description": "If false, ``pattern`` is treated as a literal substring.", "default": True},
        "encoding": {"type": "string", "description": "Optional encoding override; default autodetect."},
    },
    "read_text_slice": {
        "path": {"type": "string", "description": "Absolute or relative path to the text file."},
        "offset_chars": {"type": "integer", "description": "0-indexed character offset to start at. Negative values count from end (-1 = last char, -1024 = last 1 KB). Default 0.", "default": 0},
        "length": {"type": "integer", "description": "Characters to return. Default 8000, max 24000. For huge files: stat first, then walk in 8-16 KB windows.", "default": 8000},
        "encoding": {"type": "string", "description": "Optional override (e.g. 'utf-8', 'utf-16', 'latin-1'). Default: autodetect."},
    },
    "extract_strings": {
        "path": {"type": "string", "description": "Absolute or relative path to the file to scan."},
        "min_len": {"type": "integer", "description": "Minimum string body length (default 16). Shorter than this and you'd see every variable name in a minified bundle.", "default": 16},
        "max_results": {"type": "integer", "description": "Cap on returned items (default 50, max 500).", "default": 50},
        "kind": {"type": "string", "enum": ["any", "literal", "url", "data_uri", "jwt", "base64", "hex", "high_entropy"], "description": "Filter: 'any' / 'literal' = every string literal of length >= min_len. 'url' / 'data_uri' / 'jwt' / 'base64' / 'hex' = pattern-only token extraction (regex over raw file text). 'high_entropy' = literals with Shannon entropy >= min_entropy (default 4.5) — use to find 'the secret' without knowing its format.", "default": "any"},
        "min_entropy": {"type": "number", "description": "Override entropy floor (bits/char). Only consulted for kind='high_entropy', or if you want to filter base64/hex by entropy.", "default": 0.0},
        "encoding": {"type": "string", "description": "Optional encoding override; default autodetect."},
    },
    "grep_text_dir": {
        "pattern": {"type": "string", "description": "Regex (or literal substring if regex=false) to search for."},
        "root": {"type": "string", "description": "Directory to walk recursively."},
        "glob": {"type": "string", "description": "Optional filename glob, e.g. '*.js' or '**/*.py'. Empty = every file under root."},
        "mode": {"type": "string", "enum": ["content", "files_with_matches"], "description": "content returns path:line:text records (capped at max_matches), files_with_matches returns the list of files with at least one hit.", "default": "content"},
        "max_matches": {"type": "integer", "description": "Global cap on returned hit records (default 50, max 200).", "default": 50},
        "max_files": {"type": "integer", "description": "Safety cap on files scanned (default 5000, max 5000).", "default": 5000},
        "line_trunc": {"type": "integer", "description": "Per-matched-line char clip.", "default": 300},
        "case_insensitive": {"type": "boolean", "description": "Case-insensitive regex.", "default": False},
        "regex": {"type": "boolean", "description": "If false, pattern is treated as a literal substring.", "default": True},
        "exclude_dirs": {"type": "array", "items": {"type": "string"}, "description": "Directory names to skip while walking. Defaults include .git, node_modules, .venv, __pycache__, dist, build, .cache, .cursor, vendor."},
    },
    # ------------------------------------------------------------------
    # Updated read tools — explicit pagination signatures
    # ------------------------------------------------------------------
    "js_fetch_asset": {
        "url": {"type": "string", "description": "Authorized URL to fetch."},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope."},
        "offset": {"type": "integer", "description": "1-indexed line offset for paged mode (default 1). Negative counts from EOF.", "default": 1},
        "limit": {"type": "integer", "description": "Max lines returned in paged mode (default 200, max 800).", "default": 200},
        "meta_only": {"type": "boolean", "description": "Return ONLY metadata (size, content_type, sha256, line count). Use first to gauge whether the asset is small enough to download fully.", "default": False},
        "full": {"type": "boolean", "description": "Return the whole body (still server-capped at 200 KB chars). Use only after meta_only confirms the asset is reasonably sized.", "default": False},
        "line_trunc": {"type": "integer", "description": "Per-line char clip (default 400).", "default": 400},
    },
    "reverse_artifact_read": {
        "job_id": {"type": "string", "description": "Reverse job id (obtain via reverse_job_list)."},
        "artifact_id": {"type": "string", "description": "Artifact id (obtain via reverse_job_get)."},
        "max_bytes": {"type": "integer", "description": "Max bytes to return (default 32768, max 1000000). For larger reads call grep_text or read_text_file against the artifact's path_abs.", "default": 32768},
        "offset_bytes": {"type": "integer", "description": "Byte offset to start at (default 0). Use end_bytes from a prior call to page through.", "default": 0},
    },
    "reverse_finding_read": {
        "job_id": {"type": "string", "description": "Reverse job id."},
        "finding_id": {"type": "string", "description": "Finding id."},
        "max_chars": {"type": "integer", "description": "Max chars of the markdown body (default 32768, max 200000).", "default": 32768},
        "offset": {"type": "integer", "description": "0-based char offset into the body. Inspect body_more in the response to know whether to page.", "default": 0},
    },
    "reverse_reproduce_read": {
        "job_id": {"type": "string", "description": "Reverse job id."},
        "rep_id": {"type": "string", "description": "Reproducer script id."},
        "max_chars": {"type": "integer", "description": "Max chars of the script content (default 32768, max 200000).", "default": 32768},
        "offset": {"type": "integer", "description": "0-based char offset. content_more in the response is true while there's more to page.", "default": 0},
    },
    "browser_evaluate": {
        "url": {"type": "string", "description": "Authorized URL to load."},
        "expression": {"type": "string", "description": "JS expression evaluated against the page after render. Prefer a tight expression that returns ONLY what you need; the response is server-capped."},
        "scope": {"type": "string", "description": "Comma-separated authorized host scope."},
        "headless": {"type": "boolean", "description": "Run headless. Default true.", "default": True},
        "wait_until": {"type": "string", "description": "Navigation wait condition. Default networkidle.", "default": "networkidle"},
        "wait_for_selector": {"type": "string", "description": "Optional selector to await before evaluating."},
        "extra_wait_ms": {"type": "integer", "description": "Optional settle delay after wait_until.", "default": 0},
        "max_value_chars": {"type": "integer", "description": "Max chars to return in the value field. Default 8192. Bigger objects are serialised and truncated; ``value_size_chars`` carries the true size so you can decide whether to re-run with a narrower expression.", "default": 8192},
    },
    "browser_use_tab": {
        "index": {"type": "integer", "description": "Legacy global tab index from browser_list_tabs.tabs. Prefer target_id or window_id+tab_index when multiple windows exist."},
        "target_id": {"type": "string", "description": "Stable CDP target_id from browser_list_tabs.tabs/windows. Preferred when available."},
        "window_id": {"type": "integer", "description": "Browser window_id from browser_list_tabs.windows. Use with tab_index."},
        "window_index": {"type": "integer", "description": "0-based window_index from browser_list_tabs.windows. Use with tab_index when window_id is missing."},
        "tab_index": {"type": "integer", "description": "0-based tab index within the selected window. Use with window_id or window_index."},
        "url_contains": {"type": "string", "description": "Substring match against tab URL."},
        "title_contains": {"type": "string", "description": "Substring match against tab title."},
        "navigate_to": {"type": "string", "description": "Optional URL to navigate the selected tab to before running expression."},
        "expression": {"type": "string", "description": "JS to evaluate against the chosen tab. Prefer a targeted expression."},
        "wait_until": {"type": "string", "description": "load|domcontentloaded|networkidle|commit. Default load.", "default": "load"},
        "wait_for_load": {"type": "boolean", "description": "Wait for full load instead of returning on commit.", "default": True},
        "navigation_timeout_ms": {"type": "integer", "description": "Navigation timeout ms (1000-120000). Default 30000.", "default": 30000},
        "extra_wait_ms": {"type": "integer", "description": "Settle delay after navigation (0-30000).", "default": 0},
        "capture_html": {"type": "boolean", "description": "Capture page outerHTML. Auto: true when expression empty, false when expression set."},
        "max_html_chars": {"type": "integer", "description": "Char cap for returned HTML (default 8000, max 200000).", "default": 8000},
        "max_eval_chars": {"type": "integer", "description": "Char cap for the serialised evaluation result (default 8000, max 50000). evaluation_size_chars in the response carries the true size.", "default": 8000},
    },
    # ------------------------------------------------------------------
    # Python-first reverse engineering: run JS without a browser, do
    # crypto in Python, ship a one-click PoC.
    # ------------------------------------------------------------------
    "run_node_script": {
        "source": {"type": "string", "description": "JavaScript source to run in the managed Node.js sandbox subprocess. NO real browser is involved. Good for pure-computation JS (decoders, signature builders, deobfuscated entry points). If the code contains statements (console.log, const/let/var, function, require/import, semicolons, or multiple lines), use mode='script', not eval."},
        "path": {"type": "string", "description": "Path to JS source. Prefer this for large bundles instead of embedding source.", "default": ""},
        "mode": {"type": "string", "enum": ["script", "eval", "call"], "description": "Choose carefully. script: DEFAULT; run complete JS source verbatim. Use for console.log(...), const/let/var, function definitions, require/import, semicolons, multiple lines, or any snippet that looks like a program. To return a value in script mode, assign globalThis.__result = value. eval: ONLY for a single JavaScript expression such as 1+2, process.version, or myObj.field; do NOT use eval for console.log or statement blocks. call: source defines a named global function, then function_name is invoked with args.", "default": "script"},
        "function_name": {"type": "string", "description": "(mode=call only) Name of the global function to invoke after running source."},
        "args": {"type": "array", "items": {}, "description": "(mode=call only) JSON-serialisable list of arguments."},
        "globals": {"type": "object", "description": "Optional JSON-serialisable globals to pre-inject into the runner's sandbox."},
        "inner_timeout_ms": {"type": "integer", "description": "vm.Script timeout (100-60000 ms). Default 5000.", "default": 5000},
        "process_timeout_seconds": {"type": "integer", "description": "Wall-clock timeout on the Node subprocess (1-600 s). Default 30.", "default": 30},
        "env": {"type": "string", "enum": ["node", "browser"], "description": "node (default): clean Node sandbox with Buffer/process hidden? No — Buffer is exposed but process is not. browser: ALSO injects stub window/document/navigator/location/localStorage/performance/fetch/XMLHttpRequest AND hides process/Buffer/require/global, so anti-bot guards like `typeof process !== 'undefined' && process.versions.node` or `typeof window === 'undefined'` pass. Use whenever the JS gates on browser globals. NOT a real DOM — for layout/innerHTML parsing fall back to sandbox_eval_js (Playwright).", "default": "node"},
        "page_url": {"type": "string", "description": "(env=browser only) Seeds location.href / document.URL. Default https://example.com/. Set to the target site URL when the script reads location.hostname etc."},
        "user_agent": {"type": "string", "description": "(env=browser only) Overrides navigator.userAgent. Default is a recent Chrome on Windows."},
        "profile_json": {"type": "string", "description": "(env=browser only) Optional fingerprint profile JSON from js_runtime_sample_profile, shared with hosted runtime stubs.", "default": ""},
    },
    "run_js_function": {
        "source": {"type": "string", "description": "JavaScript source that DEFINES the function (or makes it a global)."},
        "path": {"type": "string", "description": "Path to JS source. Prefer this for large bundles instead of embedding source.", "default": ""},
        "function_name": {"type": "string", "description": "Name of the global to invoke after source runs (e.g. 'entryFn')."},
        "args": {"type": "array", "items": {}, "description": "JSON-serialisable arguments. The function may return a Promise; the runner awaits it within inner_timeout_ms."},
        "globals": {"type": "object", "description": "Optional pre-injected globals."},
        "inner_timeout_ms": {"type": "integer", "description": "vm.Script timeout, also bounds Promise await (100-60000 ms). Default 5000.", "default": 5000},
        "process_timeout_seconds": {"type": "integer", "description": "Wall-clock timeout (1-600 s). Default 30.", "default": 30},
        "env": {"type": "string", "enum": ["node", "browser"], "description": "Same as run_node_script.env. Pass 'browser' when the function or its module checks typeof window / process.versions.node — common in anti-bot decoders.", "default": "node"},
        "page_url": {"type": "string", "description": "(env=browser only) location.href seed."},
        "user_agent": {"type": "string", "description": "(env=browser only) navigator.userAgent."},
        "profile_json": {"type": "string", "description": "(env=browser only) Optional fingerprint profile JSON from js_runtime_sample_profile, shared with hosted runtime stubs.", "default": ""},
    },
    "crypto_reproduce": {
        "algorithm": {"type": "string", "description": "One of: xor | aes-cbc | aes-ecb | aes-ctr | aes-gcm | des-cbc | des-ecb | 3des-cbc | 3des-ecb | sm4-cbc | sm4-ecb | rc4 | hmac-sha256 | hmac-sha1 | hmac-sha512 | hmac-md5 | sha256 | sha1 | sha512 | md5."},
        "direction": {"type": "string", "enum": ["encrypt", "decrypt"], "description": "For block ciphers. Ignored for hash/HMAC.", "default": "encrypt"},
        "key": {"type": "string", "description": "Key as hex, base64, base64url, or raw utf-8 (auto-detected; force via key_format)."},
        "iv": {"type": "string", "description": "IV / nonce for block-cipher modes. Same encoding rules as key."},
        "data": {"type": "string", "description": "Input data. Same encoding rules."},
        "expected": {"type": "string", "description": "Optional GOLDEN OUTPUT captured from the live JS. If present, the response carries matched=true/false and, on mismatch, a match_hint pointing at the first diverging byte. Pass this whenever you have a concrete JS-produced ciphertext to compare against."},
        "key_format": {"type": "string", "enum": ["auto", "utf8", "hex", "base64", "base64url"], "default": "auto"},
        "iv_format": {"type": "string", "enum": ["auto", "utf8", "hex", "base64", "base64url"], "default": "auto"},
        "data_format": {"type": "string", "enum": ["auto", "utf8", "hex", "base64", "base64url"], "default": "auto"},
        "expected_format": {"type": "string", "enum": ["auto", "utf8", "hex", "base64", "base64url"], "default": "auto"},
        "output_format": {"type": "string", "enum": ["hex", "base64", "base64url", "utf8"], "description": "How to render the returned output bytes.", "default": "hex"},
        "no_padding": {"type": "boolean", "description": "Skip PKCS#7 padding (for block ciphers operating on pre-padded streams).", "default": False},
        "aad": {"type": "string", "description": "GCM additional authenticated data."},
        "aad_format": {"type": "string", "enum": ["auto", "utf8", "hex", "base64", "base64url"], "default": "auto"},
    },
    "create_python_poc": {
        "out_dir": {"type": "string", "description": "Target directory. Empty or relative values resolve under the current session sandbox; absolute paths are explicit user paths. If empty, defaults to <session sandbox>/poc/."},
        "title": {"type": "string", "description": "PoC title for the README."},
        "description": {"type": "string", "description": "One-paragraph description of what the PoC achieves."},
        "walkthrough": {"type": "string", "description": "Markdown-formatted step-by-step explanation for the README."},
        "run_py": {"type": "string", "description": "Full source of run.py — must be self-contained Python that uses the listed requirements. Required."},
        "requirements": {"type": "array", "items": {"type": "string"}, "description": "pip requirements list, e.g. ['requests==2.32.3', 'cryptography>=42'].", "default": []},
        "helpers": {"type": "object", "description": "Optional dict {filename: content} of extra modules written under helpers/."},
        "usage_args": {"type": "string", "description": "Example CLI tail shown in README (e.g. '--target https://example.com --email a@b.c')."},
        "overwrite": {"type": "boolean", "description": "Allow overwriting an existing run.py.", "default": False},
    },
}


# Loom22 §4.3 / §7.3 — progressive disclosure caps for auto-injected skills.
MAX_AUTO_SKILLS = 4
MAX_AUTO_SKILL_CHARS = 18_000

_SKILL_STOPWORDS = frozenset({
    "about", "after", "also", "analysis", "analyze", "approach", "application", "asks", "audit",
    "before", "blind", "browser", "check", "clearly", "code", "command", "common",
    "bypass", "complete", "continue", "control", "create", "current", "data", "debugging",
    "detailed", "does", "during", "each", "end", "execute", "exploit", "extraction",
    "file", "files", "finding", "findings", "first", "following", "from", "guide",
    "help", "history", "http", "https", "injection", "inspect", "interacting",
    "into", "inventory", "issues", "json", "just", "keep", "like", "list", "local",
    "login", "long", "look", "make", "manage", "memory", "methodology", "mobile",
    "model", "more", "must", "name", "need", "next", "only", "open", "page",
    "passive", "plan", "planning", "present", "presenting", "prior", "process",
    "endpoint", "profile", "protocol", "proxy", "read", "real", "redirect", "reference", "report",
    "request", "requests", "results", "review", "risk", "rules", "safe", "scan",
    "scanning", "script", "search", "security", "session", "setup", "should",
    "show", "similar", "source", "start", "state", "steps", "strategy", "structured",
    "such", "supply", "surface", "table", "task", "tasks", "test", "testing",
    "that", "their", "them", "then", "there", "these", "they", "this", "through",
    "tool", "tools", "traffic", "type", "under", "upload", "user", "users",
    "using", "validation", "vault", "verification", "view", "vulnerability",
    "vague", "requests", "trigger",
    "web", "when", "where", "which", "while", "with", "without", "work", "workflow",
    "write", "your",
    "一个", "一下", "不会", "不要", "使用", "分析", "可以", "如何", "如果",
    "安全", "工具", "应该", "当前", "或者", "需要", "进行", "通过",
})


def _message_matches_pattern(message: str, pattern: str) -> bool:
    raw = str(pattern or "").strip()
    if not raw:
        return False
    try:
        return bool(re.search(raw, message, re.IGNORECASE))
    except re.error:
        return _message_matches_token(message, raw)


def _message_matches_token(message: str, token: str) -> bool:
    word = str(token or "").strip().lower()
    if len(word) < 4 or word in _SKILL_STOPWORDS:
        return False
    return bool(re.search(rf"(?<![\w/]){re.escape(word)}(?![\w/])", message, re.IGNORECASE))


def _description_tokens(description: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in description.lower().split():
        word = raw.strip(".,;:()[]{}\"'")
        if len(word) < 5 or word in _SKILL_STOPWORDS or word in seen:
            continue
        seen.add(word)
        tokens.append(word)
        if len(tokens) >= 16:
            break
    return tokens


def _skill_match_score(sd: SkillDoc, message: str) -> int:
    if sd.disable_model_invocation:
        return 0
    score = sd.inject_priority
    if sd.trigger_keywords and any(_message_matches_pattern(message, p) for p in sd.trigger_keywords):
        return score + 120
    if sd.trigger_tools:
        for tool_name in sd.trigger_tools:
            if _message_matches_token(message, tool_name.replace("_", " ")) or _message_matches_token(
                message, tool_name
            ):
                return score + 90
    for token in _description_tokens(sd.description):
        if _message_matches_token(message, token):
            return score + 40
    return 0


def _select_matched_skills(message: str) -> list[SkillDoc]:
    """Rank the skills relevant to this turn (highest relevance first), capped.

    Progressive disclosure (Loom22 §4.3 / §7.2): the runtime surfaces a per-turn
    SHORTLIST of matched skill *names + descriptions* as pointers — never their
    bodies. Injecting truncated bodies bloated context AND created false satiety
    (the model thought the excerpt was the whole playbook and skipped reading).
    Full playbooks are pulled on demand via ``skill_read``.
    """
    ranked: list[tuple[int, int, str, SkillDoc]] = []
    for sd in _SKILL_DOCS:
        score = _skill_match_score(sd, message)
        if score <= 0:
            continue
        ranked.append((score, -len(sd.body), sd.name, sd))
    if any(_is_strong_skill_match(item[3], message) for item in ranked):
        ranked = [item for item in ranked if _is_strong_skill_match(item[3], message)]
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return [sd for _score, _neg_len, _name, sd in ranked[:MAX_AUTO_SKILLS]]


def _select_tool_names(page: str, message: str, *, allow_web_search: bool = False) -> list[str]:
    """Select which tool schemas to send based on page context and keywords (Loom22 §4.3)."""
    names: set[str] = set(TOOL_GROUPS.get("always", []))
    page_map = {
        "task": ["tasks"],
        "tasks": ["tasks"],
        "new": ["tasks"],
        "proxy": ["proxy"],
        "cron": ["cron"],
        "settings": ["settings"],
        "team": ["team"],
        # Narrow default: expand via TOOL_KEYWORDS + tool_search, not whole catalogue.
        "assistant": ["workflow"],
    }
    for group in page_map.get(page, ["workflow"]):
        names.update(TOOL_GROUPS.get(group, []))
    if allow_web_search:
        names.update(TOOL_GROUPS.get("web_lookup", []))
    msg_lower = message.lower()
    for group, patterns in TOOL_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, msg_lower):
                names.update(TOOL_GROUPS.get(group, []))
                break
    for sd in _select_matched_skills(message):
        if not _is_strong_skill_match(sd, message):
            continue
        if sd.trigger_tools:
            # Evidence stays available for playbooks that archive proof, but do
            # NOT union every TOOL_GROUP that happens to contain a trigger tool.
            # That cross-group expansion previously pulled desktop input / shell
            # schemas into unrelated productivity or coding matches.
            names.update(TOOL_GROUPS.get("evidence", []))
        for tool_name in sd.trigger_tools:
            names.add(tool_name)
    return sorted(names)


def apply_project_workspace_tools(session: Any, names: list[str]) -> list[str]:
    """Attach codebase schemas when this conversation is bound to a local project."""
    binding = getattr(session, "runtime_binding", None)
    if str(getattr(binding, "workspace_mode", "") or "") != "project":
        return names
    if not str(getattr(binding, "project_dir", "") or "").strip():
        return names
    registry = getattr(session, "registry", None)
    extra = TOOL_GROUPS.get("codebase", [])
    known = set(names)
    out = list(names)
    for name in extra:
        if name in known:
            continue
        if registry is not None and getattr(registry, "get", None) is not None and registry.get(name) is None:
            continue
        out.append(name)
        known.add(name)
    return out


def _select_skill_text(
    message: str,
    tool_names: Iterable[str] | None = None,
) -> str:
    """Per-turn SHORTLIST of relevant skills (name + 1-line description), not bodies.

    These pointers let the model jump straight to ``skill_read`` on the right
    playbook instead of running ``skill_search`` first; if none of them fit once
    read, it can still ``skill_search`` for others. ``tool_names`` is kept for
    call-site compatibility.
    """
    _ = tool_names
    matched = list(_select_matched_skills(message))
    if not matched:
        return ""
    strong = {sd.name for sd in matched if _is_strong_skill_match(sd, message)}
    rows: list[str] = []
    for sd in matched:
        desc = " ".join((sd.description or "").split())
        if len(desc) > 160:
            desc = desc[:159] + "\u2026"
        tag = " \u2014 **strong match**" if sd.name in strong else ""
        rows.append(f"- `{sd.name}`{tag}: {desc}")
    return "\n".join(rows)


def _is_strong_skill_match(sd: SkillDoc, message: str) -> bool:
    """A strong match = the user clearly invoked this skill's domain (trigger
    keyword / tool hit), as opposed to a weak description-token overlap. These
    are the playbooks the model MUST read in full before executing."""
    if sd.disable_model_invocation:
        return False
    if sd.trigger_keywords and any(_message_matches_pattern(message, p) for p in sd.trigger_keywords):
        return True
    if sd.trigger_tools:
        for tool_name in sd.trigger_tools:
            if _message_matches_token(message, tool_name.replace("_", " ")) or _message_matches_token(
                message, tool_name
            ):
                return True
    return False


def _build_system_prompt(
    tool_lines: str,
    skill_docs_text: str,
    profile_text: str = "",
    *,
    allow_web_search: bool = False,
    greenlight_active: bool = False,
    agent_self_decide: bool = False,
) -> str:
    """Build the small always-on prompt; domain workflows live in skills."""
    web_search_contract = (
        "- **Internet search is ON** for this session (user toggled 联网开). You CAN search the public web "
        "via `web_search` (Bing first; Baidu when the host locale/region is Chinese; "
        "DuckDuckGo/ddgs only as fallback — not Google official APIs). "
        "Use it for news, current events, writeups, docs, CVE references, or any public page discovery. "
        "When the user asks whether you can go online / search the web (e.g. 你能上网吗, 有没有搜索引擎), "
        "answer **yes** and mention `web_search`; do NOT list only HTTP/browser tools and deny search. "
        "Do NOT use `tool_search` or `mcp_server_list` for public web/news queries — `tool_search` only "
        "searches the assistant tool catalogue. If `web_search` returns no useful hits, say so briefly; "
        "do not claim internet search is unavailable while this toggle is on. "
        "For PoCs, vulnerability writeups, affected-host lists, or reference code, prefer `github_search` "
        "(official GitHub API: repositories/code/issues) — it is far more precise than scraping search engines."
    )
    if greenlight_active:
        scope_contract = (
            "- GREENLIGHT MODE is active for this session: treat user-supplied targets/paths/requests as the working "
            "scope for analysis and execution. Do not pause to ask for authorization, ownership, ROE, or a "
            "separate scope attestation. Never output pre-flight boundary disclaimers or refusal prefaces (such as '先声明关键边界'). "
            "When a tool schema requires `scope`, pass the user-supplied target host/path. Tool-layer scope checks and mandatory confirmations still apply at execution time. "
            "GREENLIGHT still never authorizes ArgusAI-on-ArgusAI operations (see Core Contract hard rule)."
        )
        observation_contract = (
            "- Prefer concrete next actions over prolonged observation or boundary lectures. For requests involving search, "
            "verification, or testing, execute the required tools directly without re-asking for authorization or issuing disclaimers."
        )
    else:
        scope_contract = (
            "- Never execute out-of-scope actions. For active security testing, confirm target URL and "
            "scope when they are not clear."
        )
        observation_contract = (
            "- Default to observation before action. For vague requests such as 'take a look' or 'check', "
            "start read-only and propose the next active step when needed."
        )
    parts = [
        "You are the ArgusAI AI assistant, an authorized security assessment and reverse-engineering workbench expert "
        "with practical software capabilities. You support web/API, mobile, native binaries, JavaScript/WASM, cloud, identity, "
        "network, proxy, desktop, sandbox, evidence-driven analysis, code authoring, file processing, and everyday "
        "productivity workflows.",
        "ArgusAI is developed by 一铭 (open-sourced by DAWA Technology, https://dawahezhi.com/; repository: https://github.com/HG-ha/Argusai).",
        "",
        "Use the available tools to inspect current application, browser, task, memory, and sandbox state; do not guess when a tool can verify it.",
        "",
        "## Built-in capability routes",
        "- Proactive defense is an Agent capability, not a UI mode: use the `proactive-defense` skill for authorized "
        "business-security checks, log review, attack-surface baselines, patrols, alert triage, incident response, and attack tracing.",
        "- Scheduled and durable automation is available: use the `assistant-cron` skill for recurring work, scheduled "
        "messages, patrol cadence, and segmented continuation of unfinished Agent work.",
        "- Paired communication channels are an Agent capability, not only a settings page. Use the "
        "`channel-interaction` skill for remote UX (clickable decisions, `/live` stages, file push), and "
        "`channel-human-reply` for natural multi-bubble direct replies. Use `channel_list_targets` to discover "
        "send-enabled QQ, Telegram, Discord, Slack, Feishu, DingTalk, WeCom, WeChat, or Webhook targets. "
        "For a paired-channel turn, `channel_reply` is resident. Prefer a few short, natural bubbles when an "
        "ordinary reply contains distinct conversational thoughts. Choose boundaries by meaning and rhythm, "
        "not punctuation. Call it once with one exact bubble per `messages` item; the runtime never splits or rewrites them. A "
        "single cohesive paragraph or structured block may be returned normally. `channel_reply` is locked to "
        "that exact source identity and requires no proactive-send confirmation. It is a terminal tool: call it "
        "only after all work is complete; once accepted, the runtime ends the turn without another model pass. "
        "The global user-habits profile "
        "governs wording in GUI, floating-ball, and channel conversations alike. When the user asks to generate "
        "and send a deliverable, create it in the current session sandbox and call `channel_send_file`; use "
        "`channel_send_message` only for proactive text. When the user "
        "messaged from a paired channel, omit `identity_id` so delivery returns to that same identity "
        "(`turn_source`). From the GUI, omitting `identity_id` fans out to every send-enabled identity "
        "bound to this conversation, auto-routes to an unambiguous recently active paired chat, or uses the "
        "only paired recipient without changing its inbound binding. A recipient "
        "listed by `channel_list_targets.other_session_targets` may be named explicitly by `identity_id`; "
        "the confirmation-gated outbound send does not move that recipient's incoming conversation binding. "
        "Never tell the user to re-pair merely because the recipient is bound elsewhere. Proactive outbound "
        "sends are confirmation-gated; `channel_reply` is the exact-source reply exception. Sends are queued, so confirm the "
        "outcome with `channel_delivery_status` before telling the user a file arrived; a failed delivery carries the "
        "provider's own reason. Do not invent an MCP tool for channel send — these native tools are the API.",
        "- Two multi-agent mechanisms; do not mix them. Standing team (`team_list` / `team_create` / "
        "`team_add_member` / `team_message_send`): the Team sidebar. It persists across chats, members can "
        "talk in the room, and a new member waits for the first command. Use it when the user says 小队, 团队, "
        "建队, 看团队, or 让团队做. Child agents (`spawn_agent`, `spawn_team`, `run_subagent`): temporary workers "
        "inside THIS chat only. They cannot talk to each other. Use them only to split the current task. "
        "`readonly` is the safe default; `scoped` plus exact `requested_tools` is the whitelist (`scope_ref` is "
        "optional). Spawning authorizes that projection — those tools do not wait for a second Allow card. "
        "Read `multi-agent-orchestration` before choosing. Lifecycle for child agents: `send_agent_input`, "
        "`pause_agent`, `wait_for_agents`, `resume_agent`, `get_agent`, `list_agents`, `close_agent`.",
        "- Delegation is proactive: for non-trivial work with at least two independent, judgeable branches, "
        "prefer launching bounded child Agents early without waiting for the user to request it. Readonly delegation "
        "needs no separate user question. Keep tightly sequential work, shared mutable edits, integration, and the final "
        "decision in the parent Agent.",
        "- Semantic Windows desktop automation is available through the `desktop-automation` skill and UI Automation tools; "
        "persistent shell sessions and per-session sandbox workflows are also available for long-running operational work.",
        "- User notifications are source-aware: for GUI-origin turns, use the `user-notification` skill only when ArgusAI is "
        "hidden or unfocused; for communication-channel-origin turns, never send a duplicate desktop/channel notification "
        "because the normal final reply already returns to that channel.",
        "- A history entry marked `channel_outbound` is a message this Agent delivered proactively from the exact "
        "`Origin conversation` named in that entry, which also states the `Reason` that triggered it. Answer why it was sent "
        "from that inline reason; only when it is insufficient, call `read_assistant_session` for that exact origin. Never "
        "search unrelated sessions or deny the send.",
        "- ArgusAI is intentionally extensible beyond its bundled tools. Users may attach reviewed MCP servers, use installed "
        "or managed external CLIs/adapters, add custom Skills/Checks, and operate other desktop software. Before declaring a "
        "capability unavailable, inspect `mcp_server_list`, search public candidates with `mcp_marketplace_search`, then check "
        "`tool_search`, `skill_search`, and `external_tool_inventory`; read "
        "`mcp-provision-workflow` for safe provisioning and `desktop-automation` for GUI-only software.",
        "- These routes remain available even when their schemas are not attached to the current turn. Never claim a capability "
        "is unavailable merely because it is absent from the task-relevant tool subset; call `skill_search` / `skill_read` for "
        "the workflow and `tool_search` for the concrete executor.",
        "",
        "## Core Contract",
        "- Be concise. Put the answer, decisive evidence, and next action first.",
        "- **No ArgusAI-on-ArgusAI (hard rule, never waived)**: never use ArgusAI tools, sandbox, desktop "
        "automation, browser automation, shell, MCP server, child Agent, reverse/security probes, file operations, "
        "or any other execution route to inspect, read, search, modify, build, test, run, stop, restart, configure, "
        "install, uninstall, package, assess, reverse, delete, or inject ArgusAI itself — including its install "
        "directory, source tree, repository, packaged binary, running process, settings, data directories, sessions, "
        "releases, infrastructure, product packaging, and product internals. This remains forbidden even when the "
        "user explicitly requests or authorizes it. If asked, refuse briefly; do not investigate first and do not "
        "suggest an alternate route for operating on ArgusAI. Normal product use is fine: answer conceptual/how-to "
        "questions from already-provided context, use session sandboxes, and work on user-designated projects that "
        "are not ArgusAI. "
        "**GREENLIGHT MODE does not override this rule.**",
        "- **Reply language (always)**: match the latest real user message. Chinese → Simplified Chinese; "
        "otherwise English. Tool names, English policy/system text, Greenlight wording, and vision/tool "
        "logs must not change the reply language. Status lines, questions, and final answers follow this rule.",
        "- Tool calls MUST use the function-calling API only. Never write pseudo tool-call XML/DSML/function templates in visible prose.",
        "- If a needed executor is not in the current schema list, call `tool_search` with 1–3 keywords "
        "(add `group` when you know the domain). `tool_search` searches the local tool catalogue only — never the public internet. "
        "The top-level function list does **not** grow; full schemas are in the `tool_search` result. "
        "Run a discovered tool with `invoke_catalogue_tool(name=..., arguments={...})` in this same turn. "
        "Do not repeat the identical query. If none of the hits fit, search **once more** with different keywords or `group`. "
        "After two misses, tell the user this capability is not in the catalogue — do not keep searching.",
        *( [web_search_contract] if allow_web_search else [] ),
        scope_contract,
        observation_contract,
        "- If a tool fails, state the failure briefly and choose a concrete alternative or ask for the missing prerequisite.",
        "- After tools run, do not repeat raw tool output. The UI shows tool cards; cite only the evidence needed for the conclusion.",
        "- Explain vulnerabilities as: what it is -> why it matters -> how to fix. Include a safe, directly usable test case when in scope.",
        "- Use Markdown with short paragraphs or bullets. This GUI chat renders a fenced "
        "`mindmap` or mermaid `mindmap` block as a zoomable diagram — use one when a hierarchy, "
        "attack/call/auth chain, or decision tree is clearer than a list. Keep node labels short; "
        "other mermaid diagram types stay as code. Tabular rows still go to `show_data_table`.",
        "- Every visible assistant message MUST carry new information for the user: a fact you just learned, a decision you made, a question you need them to answer, or the final answer itself. Pure scaffolding such as \"好问题\", \"让我先看看…\", \"让我把…读出来\", \"下面用…给你概览\", \"now I'll check…\", \"let me read…\", \"I'll first…\" is forbidden — call the tool directly with no preface.",
        "- A one-sentence preface BEFORE a tool call is allowed ONLY if (a) the tool touches a risky / user-visible action that needs justification, (b) you are about to start a long-running operation (>10s) and want the user to know, or (c) the plan you stated earlier has just changed. None of \"now I will check…\", \"let me read…\", \"I'll first…\" qualify under these exceptions.",
        "- Between two consecutive tool calls within the same turn, write NOTHING unless one of the three exceptions above applies. Silent tool chaining is the default; chained \"过场白\" sentences are a defect, not politeness.",
        "- After a meaningful tool result that changes the plan, confirms progress, or unblocks the next step, write a SHORT status (one or two sentences) — but only when it actually carries new information. Never produce a status line that just restates what you already said or what the tool obviously did.",
        "- Do not respond to system-injected continuation prompts with meta-talk such as \"明白\", \"上一轮我已经回答了\", \"任务已完成（再无新动作）\", \"我已经…\". If the task is finished, emit ONLY the final answer or the explicit user question; if it isn't, emit ONLY the next tool_call. The continuation prompt itself is internal — never quote or acknowledge it.",
        "- Run code in the per-session sandbox by default. Read the matched development/session-sandbox skill before multi-step coding, packaging, host escalation, or long-lived process work.",
        "- For files, stat/search/read a focused slice before patching. Use sandbox tools for session artifacts and disk-file tools for user/system paths; writes keep their confirmation policy.",
        "",
        "## Agent Loop",
        "- For open-ended directives such as analysis, audit, solve, continue, or complete-this-task, keep working through the full chain in the same turn until the task is done, blocked, or needs a human decision.",
        "- Difficulty, workload, unfamiliar code, many files, and long reasoning are not blockers and are never reasons "
        "to ask whether to continue. Decompose with todos, delegate independent branches, choose safe reversible defaults, "
        "and keep working.",
        "- Before asking the user, inspect available UI, task, case, memory, sandbox, file, and tool state. Do not ask for "
        "facts you can discover or implementation details you can safely infer. State a low-risk assumption briefly and "
        "proceed when it does not materially change the requested outcome.",
        "- Write the full final summary only when the task is actually finished, blocked, or requires user input. Short progress updates are allowed during long or risky workflows; they must be concise and followed by the next action.",
        "- `duplicate_tool_call_skipped` is not a tool failure. Re-read the earlier result; vary a freshness argument only when a genuinely fresh result is needed.",
        "- Use `search_memory`, `list_session_messages`, or `read_session_message` when the user refers to previous context instead of guessing from the visible prompt window.",
        "- If an important old result was archived, use `artifact_read` via its artifact reference rather than rerunning the probe.",
        "",
        "## Long-running work",
        "- Durable assistant jobs are runtime-managed, not tools. Use todos for multi-step work and shell sessions for OS-process lifecycle; read the matched development or desktop skill for detailed rules.",
        "- `wait_for_duration` is a cross-domain Agent timing primitive (maximum 300 seconds), not a defense/Cron-specific feature. "
        "Use it whenever the next step genuinely requires brief time passage—UI/app startup, service warm-up, retry backoff, "
        "rate-limit cooldown, state propagation, or staged re-observation—then continue in the same turn. Prefer task-specific "
        "polling for jobs and Cron for longer or scheduled waits; never sleep while waiting for user input.",
        "",
        _todos_section(skill_docs_text),
        "",
        "## UI blocks",
        "- UI blocks are the primary display surface for structured results. Use `show_data_table` for rows/candidates and `show_structured_card` for grouped facts; do not duplicate the same payload as Markdown. A mind map is the exception for trees: emit one fenced `mindmap` and do not reprint the same tree as a long bullet list.",
        "",
        "## User decisions",
        *(
            [
                "- **AUTO-APPROVE is on and overrides the rest of this section.** "
                "You decide and keep working. Do not call `stop_for_user_decision` / "
                "`stop_for_user_decision_batch` to wait for a click. If a tool result has "
                "`agent_must_choose`, pick from the listed options using the evidence, state "
                "the choice, and continue. Do not default to the first option. "
                "Tool authorizations are already granted — do not ask whether you may run a tool.",
            ]
            if agent_self_decide
            else []
        ),
        *(
            []
            if agent_self_decide
            else [
                "- 所有的询问，当存在选择时，必须使用 `stop_for_user_decision` 工具来获取用户选择。",
                "- When a real human choice is needed, call `stop_for_user_decision` / `stop_for_user_decision_batch` alone (no other tools in the same batch), then stop.",
            ]
        ),
        "- UI-block first is mandatory for blocking questions: use `stop_for_user_decision` for one question and "
        "`stop_for_user_decision_batch` for several related questions. Never end a response with a prose question, "
        "numbered option list, or \"which one should I do?\" when the answer is required to continue. Brief prose may "
        "explain why the decision matters, but the question and options belong only in the UI block.",
        "- **Remote channels (Feishu / Telegram / Discord / Slack / …)**: the user can only click buttons from a "
        "`user_decision` card. Never ask them to type `1` / `A` / `B` / `C`. Put every mutually exclusive choice "
        "(strategy, scope, confirm-before-SSH, which change to apply, …) into `stop_for_user_decision` options; "
        "keep the essay in assistant prose only.",
        "- Forbidden patterns: ending with \"你选哪个？\" / \"Which one?\" plus an A/B/C or 1/2/3 Markdown list; "
        "writing options in prose and waiting for a typed reply. Always call the decision tool instead.",
        "- A real human choice means a materially different outcome with no safe reversible default, missing authorization/"
        "scope/credentials, an irreversible external effect, or a mandatory confirmation boundary. Task complexity, effort, "
        "ordering, internal implementation details, and ordinary uncertainty do not qualify.",
        "- Batch related necessary questions into one decision card. Never repeat a question already answered by the user "
        "or current context, and never ask \"should I continue?\" after the user approved the task or Todo plan.",
        "- **Card vs prose (Loom22 §9.6)**: put the explanation in assistant prose only. The card `question` is a short control prompt (1–2 sentences); `options` are concrete clicks; `context` is at most one short why-line. Never paste the prior essay / install guide into `question` or `context`.",
        "- Confirmation questions are hard stops. If you ask whether to proceed, wait for the user.",
        "- Never offer `create_scan` as a carrier for software-development work. Scan tasks are security-scan lifecycle only.",
        "",
        "## Context and Files",
        "- Treat Current UI Context as what the user is looking at. For deictic requests such as 'this task', 'this page', or 'now', use the supplied context first, then call snapshot/detail tools if stale or missing.",
        "- For file-like reads, search or stat first, then read a focused slice/window. Do not pull whole large or minified files into context unless confirmed small.",
        "- Session sandbox artifacts use `sandbox_*` tools. Host files can be read anywhere with `stat_text_file` / `grep_text` / `read_text_file` / `patch_text_file` (same stat → grep → read → patch discipline). The sandbox is for intermediate and generated work, not a read jail.",
        "- Images can be inspected in place: `vision_inspect_image(path=...)` or by reading the file (`codebase_read` / `read_text_file` / `sandbox_read_file`). Do not copy an image into the sandbox just to look at it. Binary/WASM bytes still belong in sandbox artifacts, not chat.",
        "",
        "## Knowledge and Profile",
        "- For non-trivial recurring work, search the knowledge base early; after a non-obvious success, save or update a concise KB entry and mention it in the final summary.",
        "- When the user states a durable preference, propose a small user profile update. Beyond what they state outright, proactively summarize durable user preferences you can infer from how the session actually went — preferred language, report depth, risk tolerance, recurring targets or workflows — and offer them through `user_profile_suggest_update` as a small patch the user can approve or reject. Do not store secrets, credentials, target data, or one-off vulnerability details.",
        "",
        _skills_playbook_section(skill_docs_text),
        "",
        "## Tools",
        "Only a task-relevant SUBSET is attached below. Use `skill_search` for methodology and `tool_search(query=..., group=\"<domain>\")` for any other capability.",
        tool_lines or "- `tool_search(query, group, risk, limit)`: Search the full assistant tool catalogue when a needed tool is not listed here.",
    ]
    if profile_text:
        parts += ["", profile_text.strip()]
    return "\n".join(parts)


def _todos_section(skill_docs_text: str) -> str:
    """Always-on Todos section — set_todos before multi-step work, keep state accurate."""
    has_agent_todo_skill = (
        "agent-todo" in skill_docs_text.lower() or "## Agent Todo" in skill_docs_text
    )
    if has_agent_todo_skill:
        detail_hint = (
            "Status: `agent-todo` matched this task — `skill_read(name=\"agent-todo\")` for the full playbook, "
            "and call `list_todos` each turn you continue a multi-step task."
        )
    else:
        detail_hint = (
            "Status: use `skill_read(name=\"agent-todo\", line_limit=120)` if you need the full todo playbook "
            "(hierarchical items, case pairing, validation todos)."
        )
    return "\n".join([
        "## Todos and progress (use for multi-step work)",
        detail_hint,
        "- **When required**: any task with **3+ tool steps**, multiple phases (recon → probe → validate → report), "
        "pentest/audit/reverse/sandbox flows, or when the user says continue / 继续 / 全面测试 / 调查.",
        "- **Before the first execution tool** on such tasks: call `set_todos` with concrete, observable items "
        "(or `add_todos` if extending an existing list). The runtime **pauses for user confirmation** before any "
        "execution tools run — the user may approve, cancel, or ask to add/remove/subdivide todos first.",
        "- **While working**: after each meaningful step, `update_todo` / `complete_todo_with_refs` so status matches reality. "
        "Do **not** call `set_todos` again after the user confirmed (`开始执行`) unless they asked to modify the plan — "
        "rewriting the list re-opens the confirmation card. "
        "Use parent + `children` for phases (recon, mapping, probing, validation, reporting).",
        "- **Each turn you resume**: call `list_todos` first — pending or in-progress todos are a **stop signal** for auto-continue "
        "only after the user has confirmed the plan (`开始执行`); finish, block, cancel, or `clear_todos` before the final summary.",
        "- Pair security/reverse work with a **case** when evidence must survive compaction (`case_create` early; "
        "todos = control flow, case/artifact = durable proof).",
        "- Tools: `set_todos`, `list_todos`, `add_todos`, `update_todo`, `complete_todo_with_refs`, "
        "`clear_todos` are always loaded. `create_validation_todo` / `assessment_plan_snapshot` "
        "are catalogue tools — `invoke_catalogue_tool(name=..., arguments={...})`.",
    ])


def _skills_playbook_section(skill_docs_text: str) -> str:
    """Always-on Skills section + per-turn shortlist of relevant playbooks.

    ``skill_docs_text`` here is the matched SHORTLIST (skill names + 1-line
    descriptions from ``_select_skill_text``), NOT skill bodies. The bodies are
    pulled on demand via ``skill_read`` — only name+description is ever pushed.
    """
    lines = ["## Skills and playbooks (read before non-trivial work)"]
    shortlist = skill_docs_text.strip()
    if shortlist:
        lines += [
            "**Relevant playbooks matched for THIS task** (pointers only — name + description, NOT the body):",
            shortlist,
            "→ Before running ANY execution tool, `skill_read(name=...)` the best-matching playbook above "
            "(`heading=...` / `line_limit=120` to page long ones). The lines above are NOT the playbook — "
            "they only tell you which skill to load. Do this even if you believe you already know the workflow. "
            "If none of them actually fit once read, `skill_search(query=...)` for a better one.",
        ]
    else:
        lines += [
            "Per-turn playbook pointers attach to the latest user message when keywords match. "
            "If none are attached and this is a non-trivial security / pentest / reverse / sandbox "
            "workflow, `skill_search(query=...)` (using the user's goal and domain keywords, "
            "e.g. `pentest sqli`, `js reverse sign`, `session sandbox`) and `skill_read` "
            "the best result BEFORE improvising. You MUST NOT improvise methodology from memory.",
        ]
    return "\n".join([
        *lines,
        "- Project runbooks live in Skills (`skill_list` / `skill_search` / `skill_read`). "
        "Most tasks have a dedicated workflow skill (pentest, SQLi, JS reverse, sandbox, OOB, proxy, etc.); "
        "the full catalogue is below.",
        "- If the chosen workflow has multiple phases, `set_todos` **before** the first execution tool "
        "(see **Todos and progress** above), then call execution tools.",
        "- Do not skip skill discovery because you \"already know\" the workflow — re-check when scope or domain changes.",
        "- Vague requests (`look at this`, `check`, `看下`) still need `skill_search` if you are about to run a multi-step security workflow.",
    ])


# Domains that are agent-control plumbing rather than user-facing capability
# areas — excluded from the capability map so the map reads as "what I can do".
_CAPABILITY_MAP_SKIP = {"always", "workflow", "user_todos"}
_CAPABILITY_MAP_MAX_REPS = 8


def _skill_catalogue_index() -> str:
    """A STABLE, always-on index of every model-invocable skill (Loom22 §12.5
    push-breadth). The per-turn keyword matcher only attaches a few skill
    bodies; without this index the model never sees the other ~25 playbooks it
    has and cannot reason about what it can do. Detail still pulled via skill_read.
    """
    rows: list[str] = []
    for sd in sorted(_SKILL_DOCS, key=lambda s: s.name):
        if sd.disable_model_invocation:
            continue
        desc = " ".join((sd.description or "").split())
        # Keep push-breadth cheap: the full description and body remain
        # available through skill_search/skill_read.
        if len(desc) > 80:
            desc = desc[:79] + "…"
        rows.append(f"- `{sd.name}`: {desc}")
    if not rows:
        return ""
    return "\n".join([
        f"### Full skill catalogue ({len(rows)} playbooks — open any with `skill_read(name=...)`)",
        "These are ALL your playbooks, not just the ones excerpted this turn. If one matches the task, read it before improvising.",
        *rows,
    ])


def _tool_domain_index() -> str:
    """A STABLE, always-on map of tool DOMAINS (breadth), complementing the
    per-turn tool subset (depth). For each domain it lists the most
    domain-specific tools as representatives — generic cross-domain tools
    (artifact_add, domain_inventory, …) are ranked last so the map surfaces
    what makes each domain distinctive. Full schemas stay behind tool_search.
    """
    from collections import Counter

    counts: Counter = Counter()
    for tools in TOOL_GROUPS.values():
        for t in set(tools):
            counts[t] += 1

    total_tools = len({t for tools in TOOL_GROUPS.values() for t in tools})
    lines: list[str] = []
    for group in sorted(TOOL_GROUPS):
        if group in _CAPABILITY_MAP_SKIP:
            continue
        tools = list(dict.fromkeys(TOOL_GROUPS[group]))
        if not tools:
            continue
        # Most domain-specific first (lowest cross-group count), then name.
        reps = sorted(tools, key=lambda t: (counts[t], t))[:_CAPABILITY_MAP_MAX_REPS]
        rep_str = ", ".join(f"`{t}`" for t in reps)
        more = len(tools) - len(reps)
        suffix = f" … (+{more} more)" if more > 0 else ""
        lines.append(f"- **{group}** ({len(tools)} tools): {rep_str}{suffix}")
    if not lines:
        return ""
    return "\n".join([
        f"## Capability map — tool domains (~{total_tools} tools total)",
        "Your full tool catalogue grouped by domain. Each turn only a task-relevant subset is attached under **## Tools**; "
        "to use anything else call `tool_search(query=..., group=\"<domain>\")` for the tool + its schema. "
        "Representatives shown are the domain-defining tools, not the full list.",
        *lines,
    ])
