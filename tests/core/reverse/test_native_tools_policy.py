"""New native reverse tools are registered and low-risk by default."""

from __future__ import annotations

from argusai.core.assistant.tools import _tool_risk
from argusai.core.tools import EXECUTORS


def test_native_reverse_executors_registered() -> None:
    for name in (
        "reverse_toolchain_scan",
        "reverse_toolchain_install",
        "reverse_sample_import",
        "android_apk_analyze",
        "ios_ipa_analyze",
        "windows_pe_analyze",
        "elf_analyze",
        "android_jadx_decompile",
        "android_apktool_decode",
        "android_apktool_build",
        "android_apk_sign",
        "android_apk_validate",
        "binary_capa_analyze",
        "binary_yara_scan",
        "binary_list_functions",
        "binary_disassemble",
        "binary_decompile",
        "binary_patch",
        "android_objection_run",
        "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
        "mobile_frida_recipe_list",
        "mobile_frida_recipe_apply",
        "frida_list_devices",
        "frida_list_processes",
        "frida_attach",
        "frida_spawn",
        "frida_run_script",
        "frida_session_list",
        "frida_session_stop",
        "reverse_artifact_extract_strings",
        "reverse_artifact_grep",
        "reverse_job_link_proxy_flow",
    ):
        assert name in EXECUTORS


def test_static_tools_are_low_risk() -> None:
    for name in (
        "reverse_toolchain_scan",
        "android_apk_analyze",
        "ios_ipa_analyze",
        "windows_pe_analyze",
        "elf_analyze",
        "android_jadx_decompile",
        "android_apktool_decode",
        "android_apktool_build",
        "android_apk_sign",
        "android_apk_validate",
        "binary_capa_analyze",
        "binary_yara_scan",
        "android_objection_run",
        "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
        "mobile_frida_recipe_list",
        "mobile_frida_recipe_apply",
        "frida_list_devices",
        "frida_list_processes",
        "reverse_job_open_dir",
        "reverse_artifact_reveal",
        "reverse_artifact_copy_to",
        "reverse_artifact_extract_strings",
        "reverse_artifact_grep",
        "reverse_job_link_proxy_flow",
        "reverse_job_export",
    ):
        assert _tool_risk(name) == "low"


def test_reverse_toolchain_install_is_medium_risk() -> None:
    assert _tool_risk("reverse_toolchain_install") == "medium"
