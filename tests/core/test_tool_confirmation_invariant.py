"""Guards for the tool confirmation/auto-approve policy invariants.

P0 regression: the confirmation gate (session_chat_loop) keys off
``_CONFIRM_TOOLS`` only. Any tool in ``_NO_AUTO_APPROVE_TOOLS`` but missing
from ``_CONFIRM_TOOLS`` would skip the prompt entirely and execute on first
call -- the opposite of the "never silently auto-approve" intent (AGENTS.md
§6.4). Also guards against TOOL_GROUPS referencing unregistered tool names.
"""

from __future__ import annotations

from argusai.core.assistant.skills import TOOL_GROUPS
from argusai.core.assistant.streaming import _CONFIRM_TOOLS, _NO_AUTO_APPROVE_TOOLS
from argusai.core.assistant.tools import build_tool_registry

# Active attack/injection probes that send real payloads to the target. These
# previously sat in _NO_AUTO_APPROVE_TOOLS but were missing from _CONFIRM_TOOLS,
# so they ran with zero confirmation.
_FORMERLY_UNCONFIRMED_PROBES = {
    "sqli_probe_param",
    "sqli_boolean_diff",
    "sqli_time_probe",
    "idor_compare_request",
    "role_matrix_probe",
    "upload_surface_discover",
    "upload_verify_access",
    "ssrf_probe_url_param",
    "cors_matrix_probe",
    "http_desync_probe",
    "cache_poison_probe",
    "raw_http_send",
}


def test_no_auto_approve_is_subset_of_confirm() -> None:
    missing = _NO_AUTO_APPROVE_TOOLS - _CONFIRM_TOOLS
    assert not missing, f"must-never-auto-approve tools that skip confirmation: {sorted(missing)}"
    assert "cron_grant_high_risk" in _NO_AUTO_APPROVE_TOOLS


def test_attack_probes_now_require_confirmation() -> None:
    for name in _FORMERLY_UNCONFIRMED_PROBES:
        assert name in _CONFIRM_TOOLS, name
        assert name in _NO_AUTO_APPROVE_TOOLS, name


def test_proxy_group_tool_names_are_registered() -> None:
    registry = build_tool_registry(api=object(), allow_web_search=True)
    known = set(registry._tools)
    for name in TOOL_GROUPS["proxy"]:
        # Proxy tools are all EXECUTOR-based (no per-session tools), so every
        # name must resolve to a registered tool.
        assert name in known, f"proxy group references unregistered tool: {name}"
    assert "proxy_import_har" in TOOL_GROUPS["proxy"]
    assert "proxy_import_burp_log" in TOOL_GROUPS["proxy"]
    # The old dangling names must be gone.
    assert "import_proxy_har" not in TOOL_GROUPS["proxy"]
    assert "import_proxy_burp_log" not in TOOL_GROUPS["proxy"]
