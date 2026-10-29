"""Domain capability registry for the AI security platform."""

from __future__ import annotations

from typing import Any


DOMAIN_CAPABILITIES: dict[str, dict[str, Any]] = {
    "common": {
        "target_types": ["repository", "other"],
        "artifact_kinds": ["log", "note", "finding", "source"],
        "workflows": ["case_triage", "finding_report", "evidence_review"],
        "tool_groups": ["always", "evidence", "workflow", "session_sandbox"],
    },
    "web": {
        "target_types": ["url"],
        "artifact_kinds": ["request", "response", "interaction", "trace", "har", "screenshot", "oracle", "oob"],
        "workflows": ["web_replay", "ssrf_oob", "auth_matrix", "upload_verification", "js_signature_replay"],
        "tool_groups": ["probes", "proxy", "reverse", "attack", "web"],
    },
    "network": {
        "target_types": ["host", "cidr", "service", "pcap"],
        "artifact_kinds": ["pcap", "trace", "log", "network"],
        "workflows": [
            "asset_discovery", "service_triage", "protocol_review",
            "configuration_audit", "weak_credential_audit", "authorized_pentest",
            "post_exploit_assessment", "pcap_analysis",
        ],
        "tool_groups": ["network"],
        "implemented_tools": [
            "network_discover_hosts", "network_scan_ports", "network_fingerprint_services",
            "network_profile_host", "network_assess_exposure", "nmap_scan",
            "network_protocol_assess", "vuln_intel_match",
            "weak_credential_audit_plan", "weak_credential_check",
            "pentest_plan_chain", "pentest_verify_finding",
            "post_exploit_map_impact", "post_exploit_plan_actions", "post_exploit_lab_plan",
            "pcap_tool_inventory", "pcap_summary_plan", "pcap_summary",
            "pcap_filter_plan", "pcap_filter", "pcap_http_extract_plan", "pcap_http_extract",
        ],
        "lab_only_tools": ["post_exploit_lab_generate"],
        "forbidden_by_default": [
            "persistence", "stealth", "credential theft", "automatic lateral movement",
            "data exfiltration", "destructive payloads",
        ],
        "planned_tools": ["masscan"],
    },
    "mobile": {
        "target_types": ["apk", "ipa", "device", "package"],
        "artifact_kinds": ["apk", "ipa", "decompiled", "trace", "har", "log"],
        "implemented_workflows": [
            "mobile_evidence_management", "mobile_api_replay_from_artifacts",
            "mobile_static_triage", "frida_runtime_hook", "mobile_dast",
        ],
        "planned_workflows": [],
        "tool_groups": ["mobile"],
        "implemented_tools": [
            "case_create", "artifact_add", "artifact_read", "evidence_add",
            "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import", "android_apk_analyze", "ios_ipa_analyze", "ios_class_dump",
            "android_jadx_decompile", "android_apktool_decode",
            "android_apktool_build", "android_apk_sign", "android_apk_validate",
            "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
            "android_objection_run",
            "mobile_frida_recipe_list", "mobile_frida_recipe_apply",
            "frida_list_devices", "frida_list_processes",
            "frida_attach", "frida_spawn", "frida_run_script", "frida_session_list", "frida_session_stop",
            "reverse_job_create", "reverse_job_get", "reverse_artifact_add", "reverse_finding_add",
            "reverse_job_open_dir", "reverse_artifact_reveal",
            "reverse_artifact_copy_to", "reverse_artifact_extract_strings", "reverse_artifact_grep",
            "reverse_job_link_proxy_flow", "reverse_job_export",
            "mobile_dast_tool_inventory", "appium_status", "mobile_dast_plan",
        ],
        "planned_tools": [],
    },
    "binary": {
        "target_types": ["exe", "dll", "elf", "process", "dump"],
        "artifact_kinds": ["binary", "decompiled", "log", "trace"],
        "workflows": ["binary_triage", "behavior_analysis", "function_reverse", "ioc_generation"],
        "tool_groups": ["binary"],
        "implemented_tools": [
            "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import", "windows_pe_analyze", "elf_analyze",
            "binary_capa_analyze", "binary_yara_scan",
            "binary_list_functions", "binary_disassemble", "binary_decompile", "binary_patch",
            "frida_list_devices", "frida_list_processes",
            "frida_attach", "frida_spawn", "frida_run_script", "frida_session_list", "frida_session_stop",
            "reverse_job_create", "reverse_job_get", "reverse_artifact_add", "reverse_finding_add",
            "reverse_job_open_dir", "reverse_artifact_reveal",
            "reverse_artifact_copy_to", "reverse_artifact_extract_strings", "reverse_artifact_grep",
            "reverse_job_link_proxy_flow", "reverse_job_export",
        ],
        "planned_tools": [],
    },
    "cloud": {
        "target_types": ["cluster", "namespace", "cloud_account", "iac_repo"],
        "artifact_kinds": ["config", "manifest", "policy", "log", "finding"],
        "workflows": ["iac_policy_scan", "kube_config_audit", "cloud_posture_review"],
        "tool_groups": ["cloud"],
        "implemented_tools": [
            "cloud_tool_inventory", "trivy_cloud_plan", "trivy_cloud_scan",
            "prowler_cloud_plan", "prowler_cloud_scan",
            "kubectl_readonly_plan", "kubectl_readonly_run",
            "iac_tool_inventory", "trivy_iac_plan", "trivy_iac_scan",
            "checkov_scan_plan", "checkov_run_scan",
        ],
        "forbidden_by_default": [
            "apply", "delete", "exec", "port-forward", "secret export",
            "cluster-admin mutation",
        ],
        "planned_tools": [],
    },
    "directory": {
        "target_types": ["ad_domain", "dc"],
        "artifact_kinds": ["ldap", "kerberos", "graph", "log", "finding"],
        "workflows": ["ad_enum_plan", "ad_graph_review", "ad_exposure_assess"],
        "tool_groups": ["directory"],
        "implemented_tools": [
            "ad_tool_inventory", "ad_sharphound_import", "ad_pingcastle_import",
            "ad_certipy_find_plan", "ad_certipy_find_run",
        ],
        "forbidden_by_default": [
            "password spray", "relay", "dcsync", "ticket forging",
            "automatic lateral movement",
        ],
        "planned_tools": [],
    },
    "supply_chain": {
        "target_types": ["repository", "sbom", "container_image"],
        "artifact_kinds": ["sbom", "lockfile", "vuln_report", "log", "finding"],
        "workflows": ["sbom_generate", "dependency_audit", "sbom_diff"],
        "tool_groups": ["supply_chain"],
        "implemented_tools": [
            "sca_tool_inventory", "syft_status", "trivy_status", "osv_status",
            "syft_plan_inventory", "syft_run_inventory",
            "trivy_plan_scan", "trivy_run_scan",
            "osv_plan_scan", "osv_run_scan", "sbom_diff",
        ],
        "planned_tools": [],
    },
    "wireless": {
        "target_types": ["wifi_interface", "wifi_pcap"],
        "artifact_kinds": ["wireless", "pcap", "log"],
        "workflows": ["wifi_passive_survey", "wireless_pcap_review", "wlan_config_review"],
        "tool_groups": ["wireless"],
        "implemented_tools": [
            "wireless_preflight", "wireless_wlan_inventory",
            "wireless_parse_netsh_interfaces", "wireless_parse_netsh_networks",
            "wireless_parse_nmcli", "wireless_pcap_plan", "wireless_action_check",
        ],
        "forbidden_by_default": [
            "deauthentication", "evil twin", "credential cracking",
            "packet injection",
        ],
        "planned_tools": [],
    },
    "iot": {
        "target_types": ["firmware", "device", "rootfs"],
        "artifact_kinds": ["firmware", "filesystem", "binary", "log", "finding"],
        "workflows": ["firmware_extract", "iot_service_map", "default_credential_review"],
        "tool_groups": ["iot"],
        "implemented_tools": [
            "iot_firmware_preflight", "firmware_path_validate",
            "firmware_rootfs_grep", "firmware_service_map", "firmware_analysis_plan",
        ],
        "forbidden_by_default": ["destructive emulation", "credential spraying"],
        "planned_tools": [],
    },
    "hardware": {
        "target_types": ["board", "uart_capture", "flash_dump"],
        "artifact_kinds": ["uart", "flash", "photo", "log", "finding"],
        "workflows": ["hardware_lab_plan", "uart_log_review", "flash_diff"],
        "tool_groups": ["hardware"],
        "implemented_tools": [
            "hardware_preflight", "hardware_uart_parse", "hardware_flash_diff",
            "hardware_sigrok_plan", "hardware_readonly_plan", "hardware_action_check",
        ],
        "forbidden_by_default": [
            "flash", "erase", "program", "glitch automation", "gpio bitbang",
        ],
        "planned_tools": [],
    },
}


def domain_inventory(domain: str = "") -> dict[str, Any]:
    key = str(domain or "").strip().lower()
    if key:
        item = DOMAIN_CAPABILITIES.get(key)
        if item is None:
            return {"ok": False, "error": f"unknown domain: {domain}", "domains": sorted(DOMAIN_CAPABILITIES)}
        return {"ok": True, "domain": key, "capability": item}
    return {"ok": True, "domains": DOMAIN_CAPABILITIES}
