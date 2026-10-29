"""Shared constants for the Reverse Agent Runtime."""

from __future__ import annotations

JOB_SCHEMA_VERSION = 2

JOB_STATUS_VALUES = frozenset({"active", "paused", "done", "archived"})

TARGET_TYPES = frozenset({
    "web_js",
    "android",
    "ios",
    "windows",
    "native",
    "mixed",
    "unknown",
})

PLATFORMS = frozenset({
    "web",
    "android",
    "ios",
    "windows",
    "linux",
    "macos",
    "unknown",
})

RUNTIME_SESSION_KINDS = frozenset({
    "frida",
    "debugger",
    "browser_hook",
    "js_runtime",
    "other",
})

RUNTIME_SESSION_STATES = frozenset({
    "starting",
    "running",
    "stopped",
    "failed",
})

ARTIFACT_KINDS = frozenset({
    # Web / JS (existing)
    "original",
    "beautified",
    "deobfuscated",
    "sourcemap",
    "webpack_module",
    "module",
    "wasm",
    "trace",
    "html",
    # Mobile / native (new)
    "apk",
    "aab",
    "ipa",
    "xapk",
    "dex",
    "smali",
    "pe",
    "elf",
    "macho",
    "disasm",
    "patched",
    "strings",
    "yara_match",
    "capa_report",
    "frida_log",
    "frida_script",
    "debug_trace",
    "dump",
    "manifest",
    "plist",
    "decompiled",
    "other",
})

FINDING_KINDS = frozenset({
    "algorithm",
    "secret",
    "endpoint",
    "vm_opcode",
    "note",
    # Mobile / native security
    "hardcoded_secret",
    "weak_crypto",
    "exported_component",
    "insecure_storage",
    "ssl_pinning",
    "anti_debug",
    "api_auth",
    "pe_import",
    "yara_hit",
    "other",
})

EVENT_KINDS = frozenset({
    "created",
    "updated",
    "artifact_added",
    "artifact_removed",
    "hook_installed",
    "hook_removed",
    "hook_hit_drained",
    "oracle_added",
    "finding_added",
    "finding_removed",
    "reproduce_added",
    "status_changed",
    "tag_changed",
    "linked",
    "note",
    # Reverse Agent Runtime
    "target_imported",
    "toolchain_scanned",
    "runtime_session_started",
    "runtime_session_stopped",
    "evidence_linked",
    "replay_linked",
    "migrated",
})

DEFAULT_ANALYSIS_SUMMARY: dict[str, object] = {
    "static": {"done": False, "tools": [], "updated_at": ""},
    "dynamic": {"done": False, "tools": [], "updated_at": ""},
    "findings_by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
}

DEFAULT_RISK_SUMMARY: dict[str, object] = {
    "overall": "unknown",
    "tags": [],
    "notes": "",
}

DEFAULT_TOOLCHAIN_STATUS: dict[str, object] = {
    "scanned_at": "",
    "tools": {},
}
