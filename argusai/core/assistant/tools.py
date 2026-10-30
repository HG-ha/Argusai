"""Assistant runtime helpers split from the former monolithic module."""

from __future__ import annotations

from contextvars import ContextVar

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

__all__ = ['_tool_risk', 'Tool', 'ToolRegistry', 'build_tool_registry', '_build_tool_search_text', '_score_tool_search', 'unknown_tool_result', '_default_tool_metadata', 'INVOKE_CATALOGUE_TOOL', 'unwrap_catalogue_invoke', 'resolve_session_tool_call']

from argusai.core.tools import EXECUTORS

from .streaming import _CONFIRM_TOOLS, _NO_AUTO_APPROVE_TOOLS, parse_tool_call_arguments

# OpenAI-compat analogue of Anthropic defer_loading + MCP tools/call:
# keep tools[] byte-stable, put discovered schemas in the tool result, then
# invoke by name. Mutating tools[] mid-session busts prefix cache (Codex/OpenAI).
INVOKE_CATALOGUE_TOOL = "invoke_catalogue_tool"
from .tool_policy import DEFAULT_TOOL_ALWAYS_ALLOW, tool_policy_tier
from .skills import EXPLICIT_SCHEMAS, TOOL_GROUPS

# Timed desktop authorization is retired. Keep the executors for replay of
# old sessions, but do not surface them in tool_search.
_HIDDEN_FROM_TOOL_SEARCH = frozenset({
    "desktop_action_session_start",
    "desktop_action_session_stop",
    "desktop_action_session_status",
})

# Tokens that rarely discriminate between tools in natural-language queries.
# Keep "tool"/"tools" — "mcp list tools" must still match list_tools.
_TOOL_SEARCH_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "to", "with", "in", "on", "at", "by",
    "of", "is", "are", "be", "this", "that", "my", "me", "user",
})

# Longer phrases first so "上网搜索" wins over "上网".
_TOOL_SEARCH_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(sorted((
    ("端口扫描", ("nmap", "port", "scan")),
    ("扫描端口", ("nmap", "port", "scan")),
    ("扫端口", ("nmap", "port", "scan")),
    ("port scan", ("nmap", "port", "scan")),
    ("定时任务创建", ("cron", "schedule", "upsert")),
    ("创建定时任务", ("cron", "schedule", "upsert")),
    ("定时任务", ("cron", "schedule")),
    ("周期任务", ("cron", "schedule")),
    ("scheduled task", ("cron", "schedule")),
    ("cron job", ("cron", "schedule")),
    ("sql注入", ("sqli", "injection", "probe")),
    ("sql injection", ("sqli", "injection", "probe")),
    ("知识库", ("kb", "knowledge")),
    ("knowledge base", ("kb", "knowledge")),
    ("读取文件", ("read", "file")),
    ("读文件", ("read", "file")),
    ("看文件", ("read", "file")),
    ("read file", ("read", "file")),
    ("看图", ("vision", "inspect", "image")),
    ("读图", ("vision", "inspect", "image")),
    ("识图", ("vision", "inspect", "image")),
    ("inspect image", ("vision", "inspect", "image")),
    ("read image", ("vision", "inspect", "image")),
    ("上网搜索", ("web", "search")),
    ("搜索引擎", ("web", "search")),
    ("web search", ("web", "search")),
    ("硬件信息", ("observe", "inventory", "hardware")),
    ("mcp list tools", ("mcp", "list", "tools")),
    ("反编译", ("decompile",)),
    ("浏览器", ("browser",)),
    ("上网", ("web", "search")),
    ("硬件", ("observe", "inventory", "profile")),
), key=lambda item: len(item[0]), reverse=True))

# Map conversational query words to catalogue vocabulary the model tends to
# use in multi-word searches (e.g. "run command terminal" -> shell tools).
_TOOL_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "command": ("shell", "script", "run", "execute", "cmd", "powershell", "bash"),
    "execute": ("run", "script", "snippet", "shell"),
    "execution": ("run", "script", "shell"),
    "terminal": ("shell", "powershell", "bash", "cmd", "snippet"),
    "console": ("shell", "powershell", "bash", "cmd"),
    "system": ("shell", "runtime", "status", "inventory"),
    "hardware": ("observe", "inventory", "profile"),
    "information": ("observe", "status", "inventory", "snapshot", "list"),
    "info": ("observe", "status", "inventory", "snapshot"),
    "serial": ("shell", "script", "inventory"),
    "motherboard": ("shell", "script", "inventory"),
    "local": ("shell", "sandbox", "desktop", "runtime"),
    "host": ("shell", "network", "ping", "profile"),
    "port": ("ports", "nmap"),
    "create": ("upsert", "schedule"),
    "schedule": ("cron", "upsert"),
    "injection": ("sqli", "probe"),
}


def _tool_doc_excerpt(doc: str | None, *, max_chars: int = 900) -> str:
    text = (doc or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text)[:max_chars]


def _tool_groups_for(name: str) -> list[str]:
    return [g for g, names in TOOL_GROUPS.items() if name in names]


def _build_tool_search_text(name: str, doc: str | None, param_keys: Iterable[str]) -> str:
    parts = [
        name.replace("_", " "),
        name,
        _tool_doc_excerpt(doc),
        " ".join(param_keys),
        " ".join(_tool_groups_for(name)),
    ]
    return " ".join(p for p in parts if p).lower()


def _expand_search_phrases(query: str) -> str:
    text = str(query or "").strip().lower()
    for phrase, replacements in _TOOL_SEARCH_PHRASES:
        if phrase and phrase in text:
            text = text.replace(phrase, " " + " ".join(replacements) + " ")
    return text


def _tool_search_terms(query: str) -> list[str]:
    expanded = _expand_search_phrases(query)
    parts = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", expanded)
    terms = [t for t in parts if t and len(t) >= 2]
    substantive = [t for t in terms if t not in _TOOL_SEARCH_STOPWORDS]
    ordered: list[str] = []
    seen: set[str] = set()
    for term in (substantive if substantive else terms):
        if term in seen:
            continue
        seen.add(term)
        ordered.append(term)
    return ordered


def _term_variants(term: str) -> list[str]:
    out = [term]
    out.extend(_TOOL_SEARCH_ALIASES.get(term, ()))
    return out


def _score_term_against_tool(term: str, *, name: str, params: str, search_text: str) -> int:
    name_tokens = set(name.split("_"))
    best = 0
    for variant in _term_variants(term):
        if variant in name_tokens:
            best = max(best, 12)
        elif variant in name:
            best = max(best, 10)
        elif any(variant in tok for tok in name_tokens):
            best = max(best, 8)
        elif variant in params:
            best = max(best, 6)
        elif variant in search_text:
            best = max(best, 3)
    return best


def _name_token_matched(token: str, variants: set[str]) -> bool:
    for variant in variants:
        if not variant:
            continue
        if variant == token:
            return True
        if len(variant) >= 3 and variant in token:
            return True
    return False


def _score_name_compactness(name: str, terms: list[str]) -> int:
    """Prefer tools whose name is mostly covered by the query.

    Stops ``codebase_run_shell_snippet`` from beating ``run_shell_snippet``
    on a query that never mentioned the extra prefix.
    """
    name_tokens = [tok for tok in name.split("_") if tok]
    if not name_tokens:
        return 0
    variants = {item for term in terms for item in _term_variants(term)}
    matched = sum(1 for tok in name_tokens if _name_token_matched(tok, variants))
    extra = len(name_tokens) - matched
    coverage = matched / len(name_tokens)
    return int(round(coverage * 10)) - extra * 2


def _score_tool_search(tool: Tool, terms: list[str]) -> int:
    if not terms:
        return 0
    name = tool.name.lower()
    params = " ".join(tool.parameters.keys()).lower()
    search_text = (tool.search_text or name).lower()
    per_term = [_score_term_against_tool(t, name=name, params=params, search_text=search_text) for t in terms]
    total = sum(per_term)
    hit_count = sum(1 for s in per_term if s > 0)
    if hit_count >= 2:
        total += hit_count * 2
    if hit_count:
        total += _score_name_compactness(name, terms)
    return total


def _suggest_similar_tools(registry: "ToolRegistry", name: str, *, limit: int = 5) -> list[str]:
    """Rank registered tools whose names/descriptions resemble ``name``."""
    terms = _tool_search_terms(str(name or "").replace("_", " "))
    if not terms:
        terms = [t for t in str(name or "").lower().split("_") if len(t) >= 2]
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for tool in registry._tools.values():
        if tool.name in {name, "tool_search"}:
            continue
        score = _score_tool_search(tool, terms)
        if score > 0:
            scored.append((score, tool.name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tool_name for _, tool_name in scored[: max(1, min(int(limit or 5), 10))]]


def unwrap_catalogue_invoke(
    name: str,
    arguments: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Expand ``invoke_catalogue_tool`` into the inner catalogue call.

    Confirmation, runtime gates, and dispatch must see the inner tool so a
    wrapper cannot bypass ``_CONFIRM_TOOLS``.
    """
    args = dict(arguments or {})
    if str(name or "").strip() != INVOKE_CATALOGUE_TOOL:
        return str(name or ""), args
    inner = str(args.get("name") or "").strip()
    raw = args.get("arguments")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = {}
        raw = parsed
    if not isinstance(raw, dict):
        raw = {}
    if not inner or inner == INVOKE_CATALOGUE_TOOL:
        return INVOKE_CATALOGUE_TOOL, args
    return inner, dict(raw)


def resolve_session_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    fn = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(fn, dict):
        return "", {}
    name = str(fn.get("name") or "")
    args = parse_tool_call_arguments(fn.get("arguments"))
    return unwrap_catalogue_invoke(name, args)


def unknown_tool_result(registry: "ToolRegistry", name: str) -> dict[str, Any]:
    """Structured failure when the model calls a tool name that is not registered."""
    suggestions = _suggest_similar_tools(registry, name)
    return {
        "ok": False,
        "error": f"Unknown tool: {name}",
        "unknown_tool": str(name or ""),
        "do_not_retry": True,
        "suggested_fix": (
            f"Tool {name!r} is not a top-level function. Do not call it again by that name. "
            "Use tool_search(query=...) to find the catalogue name, then "
            f"{INVOKE_CATALOGUE_TOOL}(name=..., arguments={{...}}). "
            "Or pick from suggestions."
        ),
        "suggestions": suggestions,
        "next_step": suggestions[0] if suggestions else "tool_search",
    }

def _tool_risk(name: str) -> str:
    if name in {"delete_task", "start_proxy", "stop_proxy",
                "install_proxy_ca", "uninstall_proxy_ca",
                "nmap_scan", "pentest_verify_finding",
                "post_exploit_lab_generate",
                "codebase_evidence_add",
                "browser_probe", "browser_evaluate",
                "browser_trace", "browser_trace_crypto", "browser_trace_form", "captcha_runtime_trace",
                "browser_instrument_click", "js_fetch_asset", "create_poc_page", "open_poc_page",
                "run_python_script", "run_shell_script", "run_python_snippet", "run_shell_snippet",
                "run_host_python_script", "run_host_python_snippet", "sandbox_pip_install", "sandbox_pip_install",
                "shell_session_start", "shell_session_exec", "shell_session_write", "shell_session_stop",
                "sandbox_reset_files", "http_desync_probe", "raw_http_send", "sqli_time_probe",
                "sqlmap_install_update", "sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan", "browser_install_engine",
                "browser_launch_for_attach", "browser_attach_existing", "browser_open_tab", "browser_use_tab",
                "js_runtime_create", "js_runtime_load", "js_runtime_eval", "js_runtime_call",
                "js_runtime_hook", "js_runtime_patch", "js_runtime_instrument",
                "js_runtime_restore", "js_runtime_sample_profile", "python_reproducer_verify",
                "frida_attach", "frida_spawn", "frida_run_script", "frida_session_stop",
                "weak_credential_check", "websocket_send_receive", "websocket_authz_probe",
                "export_workspace_bundle",
                # MCP: high-risk operations (config changes, calls into untrusted external servers,
                # reading arbitrary resources). server_call_tool / server_read_resource defaults
                # to high because the per-tool override happens at runtime via policy.
                "mcp_server_add",
                "mcp_server_call_tool", "mcp_server_read_resource",
                "mcp_stdio_call_tool",
                "prowler_cloud_scan", "kubectl_readonly_run", "ad_certipy_find_run",
                "desktop_launch_app", "desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey",
                "desktop_focus_window", "desktop_uia_action",
                "user_todo_bulk_delete",
                "binary_patch",
                "spawn_external_agent",
                "user_profile_apply_update",
                # Persists a JS decryptor that runs in managed Node on every matching flow.
                "proxy_decrypt_script_rule_save",
                # Installs a rule that actively edits or blocks live proxied traffic.
                "proxy_rewrite_rule_save",
                # Turns on interactive breakpoints that park live requests.
                "proxy_breakpoint_config"}:
        return "high"
    if name in {"create_scan", "rescan_task", "payload_probe", "http_curl", "http_request_impersonate",
                "export_task_report",
                "network_scan_ports", "network_fingerprint_services", "network_profile_host",
                "pentest_plan_chain", "post_exploit_map_impact", "post_exploit_plan_actions",
                "trace_route", "fetch_page_source", "extract_page_text",
                "graphql_probe", "graphql_batch_probe", "websocket_handshake_probe", "websocket_connect_probe",
                "js_eval_unpack", "sandbox_eval_js", "sandbox_test_function", "sandbox_compare_outputs", "har_import_analyze",
                "proxy_import_har", "proxy_import_burp_log",
                "sandbox_write_file", "sandbox_delete_file", "sqli_probe_param", "sqli_boolean_diff",
                "reverse_toolchain_install",
                "binary_list_functions", "binary_disassemble", "binary_decompile",
                "write_text_file", "patch_text_file",
                "idor_compare_request", "role_matrix_probe", "upload_surface_discover", "upload_verify_access",
                "ssrf_probe_url_param", "cors_matrix_probe", "cors_exploit_verify", "cache_poison_probe", "sqlmap_plan_scan",
                "nuclei_plan_scan", "ffuf_plan_scan", "web_scan_tool_inventory",
                "pcap_summary", "pcap_filter", "pcap_http_extract",
                "syft_run_inventory", "trivy_run_scan", "osv_run_scan",
                "trivy_cloud_scan", "trivy_iac_scan", "checkov_run_scan",
                "appium_status",
                "wireless_wlan_inventory",
                # MCP medium: server-list / health / list-tools (touches external surface but read-only-ish).
                "mcp_server_delete", "mcp_server_health", "mcp_server_list_tools", "mcp_server_list_resources",
                "mcp_github_candidate",
                "mcp_stdio_health", "mcp_stdio_list_tools", "mcp_stdio_list_resources", "mcp_stdio_read_resource",
                "user_todo_create", "user_todo_update", "user_todo_complete", "user_todo_delete",
                "desktop_observe", "desktop_uia_find", "user_profile_suggest_update",
                "channel_send_message", "channel_send_file",
                # Writes key material into user settings.
                "proxy_decrypt_rule_save", "proxy_decrypt_rule_delete",
                # Removes an active-traffic rewrite rule (stops modifying traffic).
                "proxy_rewrite_rule_delete",
                # Forwards (optionally edited) or drops one parked live request.
                "proxy_breakpoint_resolve",
                # Creates a standing team / recruits a child-Agent member into one.
                "team_create", "team_update", "team_delete",
                "team_add_member", "team_message_send",
                "team_member_update", "team_member_send_input", "team_member_control"}:
        return "medium"
    if name in {"cancel_task", "update_settings", "launch_chrome", "http_probe", "network_discover_hosts",
                "network_protocol_assess", "vuln_intel_match", "weak_credential_audit_plan", "post_exploit_lab_plan",
                "browser_detach", "oob_start", "oob_stop", "oob_payload_generate",
                "sandbox_open_file", "sandbox_reveal_in_folder", "browser_screenshot"}:
        # OOB family is medium-low: oob_start contacts Project Discovery's
        # public Interactsh service (DNS + HTTP records pass through their
        # infrastructure), and oob_payload_generate lazily registers if no
        # session exists yet. The actual attack happens via OTHER tools
        # (raw_http_send, sqli_*, ssrf_*) that already require confirmation.
        return "medium-low"
    return "low"


# ---------------------------------------------------------------------------
# Tool model & registry
# ---------------------------------------------------------------------------

def _default_tool_metadata(name: str) -> dict[str, Any]:
    """Seed Loom22 §11.2 Tool fields from the existing fragment sets."""
    from .streaming import tool_kind

    kind = tool_kind(name)
    # Loom22 §11: exact-dedupe is opt-in for idempotent reads only.
    # Mutating / polling / control / presentation repeats are handled by
    # other guards (mutating_repetition, poll_no_progress, …).
    dedupe = kind == "idempotent"
    concurrency = "read_parallel" if kind in {"idempotent", "polling"} else "serial"
    return {
        "kind": kind,
        "dedupe": dedupe,
        "concurrency": concurrency,
        "completion": "inline",
    }


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Any]
    required: list[str] = field(default_factory=list)
    risk: str = "low"
    # Richer index for ``tool_search`` — full doc excerpt + groups, not just
    # the one-line description shown to the model in the function schema.
    search_text: str = ""
    # Loom22 §2.1 / §11.2 first-class behavioural metadata.
    kind: str = "idempotent"
    dedupe: bool = False
    concurrency: str = "serial"  # serial | read_parallel
    completion: str = "inline"  # inline | background | dynamic
    resource_key_resolver: Callable[[dict[str, Any]], list[str]] | None = None
    completion_binding: Callable[..., Any] | None = None

    @property
    def catalog_line(self) -> str:
        params = ", ".join(self.parameters.keys()) if self.parameters else ""
        sig = f"({params})" if params else ""
        return f"- `{self.name}{sig}`: {self.description}"

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }

    def resource_keys(self, arguments: dict[str, Any] | None = None) -> list[str]:
        if self.resource_key_resolver is None:
            return [self.name]
        try:
            keys = self.resource_key_resolver(arguments or {})
            return [str(k) for k in (keys or [])] or [self.name]
        except Exception:
            return [self.name]


# Standing-team members reuse the lead session's tool objects. This override
# makes those calls write into the member sandbox instead of ``team-<id>``.
tool_session_override: ContextVar[str | None] = ContextVar(
    "argusai_tool_session_override",
    default=None,
)


def bind_tool_session_id(kwargs: dict[str, Any], registry_session_id: str | None) -> None:
    """Stamp the sandbox session for one tool call. A team member override wins."""
    override = tool_session_override.get()
    bound = override or registry_session_id
    if bound:
        kwargs["_session_id"] = bound


class ToolRegistry:
    def __init__(self, api: Any | None = None, session_id: str | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # Kept so the assistant loop can attach long-lived case artifacts for
        # selected tool results without threading the GUI API object through
        # every dispatch call.
        self.api = api
        self.session_id = session_id

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: Callable,
        *,
        required: list[str] | None = None,
        risk: str = "low",
        search_text: str = "",
        kind: str | None = None,
        dedupe: bool | None = None,
        concurrency: str | None = None,
        completion: str | None = None,
        resource_key_resolver: Callable[[dict[str, Any]], list[str]] | None = None,
        completion_binding: Callable[..., Any] | None = None,
    ) -> None:
        meta = _default_tool_metadata(name)
        self._tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            fn=fn,
            required=required or [],
            risk=risk,
            search_text=search_text,
            kind=str(kind or meta["kind"]),
            dedupe=bool(meta["dedupe"] if dedupe is None else dedupe),
            concurrency=str(concurrency or meta["concurrency"]),
            completion=str(completion or meta["completion"]),
            resource_key_resolver=resource_key_resolver,
            completion_binding=completion_binding,
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if not tool:
            return unknown_tool_result(self, name)
        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "error_code": "invalid_arguments",
                "error": "tool arguments must be a JSON object",
                "retryable": True,
            }
        missing = [key for key in tool.required if key not in arguments or arguments[key] is None]
        if missing:
            return {
                "ok": False,
                "error_code": "invalid_arguments",
                "error": f"missing required tool arguments: {', '.join(missing)}",
                "missing_arguments": missing,
                "retryable": True,
            }
        non_empty_fields = {
            "run_python_snippet": ("code",),
            "run_shell_snippet": ("script",),
            "run_host_python_snippet": ("code",),
        }.get(name, ())
        empty = [
            key for key in non_empty_fields
            if not isinstance(arguments.get(key), str) or not str(arguments[key]).strip()
        ]
        if empty:
            return {
                "ok": False,
                "error_code": "invalid_arguments",
                "error": f"tool arguments must not be empty: {', '.join(empty)}",
                "empty_arguments": empty,
                "retryable": True,
            }
        try:
            result = tool.fn(**arguments)
            if inspect.isawaitable(result):
                return await result
            return result
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.exception("Tool %s failed", name)
            return {"error": str(e)}

    def catalog(self, names: Iterable[str] | None = None) -> str:
        tools = self._tools.values()
        if names is not None:
            selected = {str(name) for name in names}
            tools = [t for t in tools if t.name in selected]
        return "\n".join(t.catalog_line for t in sorted(tools, key=lambda t: t.name))

    def to_openai_tools(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        if names is None:
            tools = list(self._tools.values())
        else:
            # Keep caller order. An append-only session list then grows at the
            # end of the tools JSON instead of inserting alphabetically in the
            # middle, which would shift every later schema and bust prefix cache.
            tools = [self._tools[name] for name in names if name in self._tools]
        return [t.to_openai_tool() for t in tools]

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "risk": t.risk,
                "kind": t.kind,
                "dedupe": t.dedupe,
                "concurrency": t.concurrency,
                "completion": t.completion,
                "parameters": list(t.parameters.keys()),
                "required": t.required,
                # True when enabling auto-approve for this tool requires
                # explicit double confirmation in the UI (sandbox scripts,
                # SQLi probes, raw HTTP, sqlmap). Once enabled it runs
                # silently like any other auto-approved tool.
                "requires_double_confirm": t.name in _NO_AUTO_APPROVE_TOOLS,
                "needs_confirmation": t.name in _CONFIRM_TOOLS,
                "policy_tier": tool_policy_tier(t.name),
                "always_allow_default": t.name in DEFAULT_TOOL_ALWAYS_ALLOW,
            }
            for t in sorted(self._tools.values(), key=lambda tool: tool.name)
        ]


def build_tool_registry(api: Any, session_id: str | None = None, allow_web_search: bool | None = None) -> ToolRegistry:
    """Wire all tools from _tools.py to the API instance."""
    r = ToolRegistry(api=api, session_id=session_id)
    if allow_web_search is None:
        try:
            web_search_enabled = bool(getattr(api.settings_mgr.get(), "assistant_enable_web_search", True))
        except Exception:
            web_search_enabled = True
    else:
        web_search_enabled = bool(allow_web_search)

    for tool_name, executor in EXECUTORS.items():
        if tool_name in {"web_search", "github_search", "mcp_github_candidate"} and not web_search_enabled:
            continue
        # Introspect executor signature to build parameter schema
        import inspect
        from typing import get_origin, get_type_hints
        sig = inspect.signature(executor)
        try:
            type_hints = get_type_hints(executor)
        except (NameError, TypeError):
            type_hints = {}
        params: dict[str, Any] = {}
        required: list[str] = []
        for pname, p in sig.parameters.items():
            if pname in ("api", "kwargs"):
                continue
            anno = type_hints.get(pname, p.annotation)
            origin = get_origin(anno)
            ptype = "string"
            if anno is int:
                ptype = "integer"
            elif anno is float:
                ptype = "number"
            elif anno is bool:
                ptype = "boolean"
            elif anno is dict or origin is dict:
                ptype = "object"
            elif anno is list or origin is list:
                ptype = "array"
            param_def: dict[str, Any] = {"type": ptype}
            if p.default is not inspect.Parameter.empty and p.default is not None:
                if ptype == "integer":
                    param_def["default"] = p.default
                elif ptype == "string" and p.default:
                    param_def["default"] = p.default
                elif ptype == "boolean":
                    param_def["default"] = bool(p.default)
            elif p.default is inspect.Parameter.empty:
                required.append(pname)
            params[pname] = param_def

        # Make a closure binding the executor to the API
        def _wrap(fn: Any) -> Any:
            async def _wrapped(**kwargs: Any) -> Any:
                bind_tool_session_id(kwargs, session_id)
                result = fn(api=api, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result
            return _wrapped

        # Use executor's docstring as description, fallback to name
        desc = (executor.__doc__ or tool_name).strip().split("\n")[0]
        risk = _tool_risk(tool_name)
        if tool_name in EXPLICIT_SCHEMAS:
            params = EXPLICIT_SCHEMAS[tool_name]
            required = [
                k for k in params
                if k in {"target", "url", "target_url", "payload", "path", "content", "code", "script", "old_string"}
                and params[k].get("default") is None
            ]
            if tool_name == "codebase_scope_set":
                required = ["root"]
            elif tool_name == "codebase_search":
                required = ["pattern"]
            elif tool_name in {"codebase_read", "codebase_evidence_add"}:
                required = ["path"]
            elif tool_name == "read_assistant_session":
                required = ["session_id"]
            elif tool_name == "mcp_server_add":
                required = ["name"]
            elif tool_name == "mcp_marketplace_get":
                required = ["server_id"]
            elif tool_name in {
                "mcp_server_delete", "mcp_server_health",
                "mcp_server_list_tools", "mcp_server_list_resources",
            }:
                required = ["server_id"]
            elif tool_name == "mcp_server_call_tool":
                required = ["server_id", "tool_name"]
            elif tool_name == "mcp_server_read_resource":
                required = ["server_id", "uri"]
            elif tool_name == "mcp_stdio_call_tool":
                required = ["command", "tool_name"]
            elif tool_name == "mcp_stdio_read_resource":
                required = ["command", "uri"]
            elif tool_name in {"mcp_stdio_health", "mcp_stdio_list_tools", "mcp_stdio_list_resources"}:
                required = ["command"]
            elif tool_name == "reverse_toolchain_install":
                required = ["tool"]
            elif tool_name == "runtime_install":
                required = ["runtime"]
            elif tool_name == "invoke_catalogue_tool":
                required = ["name"]
            elif tool_name == "channel_send_message":
                required = ["text"]
            elif tool_name == "channel_reply":
                required = ["messages"]
            elif tool_name == "channel_send_file":
                required = ["path"]

        extra: dict[str, Any] = {}
        if tool_name in {
            "sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan",
            "syft_run_inventory", "trivy_run_scan", "osv_run_scan",
            "trivy_cloud_scan", "prowler_cloud_scan",
            "trivy_iac_scan", "checkov_run_scan", "ad_certipy_find_run",
        }:
            # Loom22 v2.4 durable background completion — Phase 4 wires the
            # job_ref park path; metadata must already declare the mode.
            extra["completion"] = "background"
            extra["concurrency"] = "serial"
        r.register(
            tool_name,
            desc,
            params,
            _wrap(executor),
            required=required,
            risk=risk,
            search_text=_build_tool_search_text(tool_name, executor.__doc__, params.keys()),
            **extra,
        )

    def _tool_search(
        query: str = "",
        group: str = "",
        risk: str = "",
        confirmable: bool | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the full assistant tool catalogue by purpose or name."""
        q = str(query or "").strip().lower()
        group_name = str(group or "").strip()
        risk_filter = str(risk or "").strip().lower()
        max_items = max(1, min(int(limit or 20), 80))

        group_members: set[str] | None = None
        fallback_from_group = ""
        group_warning = ""
        if group_name:
            group_members = set(TOOL_GROUPS.get(group_name, []))
            if not group_members:
                fallback_from_group = group_name
                group_warning = f"unknown tool group: {group_name}; searched the full catalogue instead"
                group_members = None

        terms = _tool_search_terms(q) if q else []
        def _collect(scope: set[str] | None) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            for tool in sorted(r._tools.values(), key=lambda t: t.name):
                if tool.name in {"tool_search", INVOKE_CATALOGUE_TOOL}:
                    continue
                if tool.name in _HIDDEN_FROM_TOOL_SEARCH:
                    continue
                if scope is not None and tool.name not in scope:
                    continue
                if risk_filter and tool.risk.lower() != risk_filter:
                    continue
                needs_confirmation = tool.name in _CONFIRM_TOOLS
                if confirmable is not None and bool(confirmable) != needs_confirmation:
                    continue

                score = _score_tool_search(tool, terms) if terms else 0
                if terms and score < 3:
                    continue
                found.append({
                    "name": tool.name,
                    "description": tool.description,
                    "risk": tool.risk,
                    "needs_confirmation": needs_confirmation,
                    "requires_double_confirm": tool.name in _NO_AUTO_APPROVE_TOOLS,
                    "parameters": dict(tool.parameters),
                    "required": list(tool.required),
                    "groups": [g for g, names in TOOL_GROUPS.items() if tool.name in names],
                    "_score": score,
                })
            return found

        matches = _collect(group_members)
        if q and group_members is not None:
            matches.sort(key=lambda item: (-item["_score"], item["name"]))
            worldwide = _collect(None)
            worldwide.sort(key=lambda item: (-item["_score"], item["name"]))
            group_best = int(matches[0]["_score"]) if matches else 0
            world_best = int(worldwide[0]["_score"]) if worldwide else 0
            world_name = str(worldwide[0]["name"]) if worldwide else ""
            if not matches:
                fallback_from_group = group_name
                group_warning = (
                    f"no matches in group {group_name}; searched the full catalogue instead"
                )
                matches = worldwide
            elif world_best >= group_best + 8 and world_name not in group_members:
                fallback_from_group = group_name
                group_warning = (
                    f"weak matches in group {group_name}; searched the full catalogue instead"
                )
                matches = worldwide

        matches.sort(key=lambda item: (-item["_score"], item["name"]))
        for item in matches:
            item.pop("_score", None)
        visible = matches[:max_items]
        activation_tools = (
            [str(item["name"]) for item in visible[:8]]
            if q
            else []
        )
        if activation_tools:
            next_step = (
                f"Call `{INVOKE_CATALOGUE_TOOL}` with name= and arguments= from one of "
                "activation_tools / tools[]. Do not expect the top-level function list to grow."
            )
            attach_note = (
                "Full parameter schemas are in this tool result (conversation suffix — cache-safe). "
                f"Call `{INVOKE_CATALOGUE_TOOL}(name=<tool>, arguments={{...}})` to run one. "
                "The session tool list stays fixed. If none fit, search once more with different "
                "keywords or group — do not repeat this exact query."
            )
        elif not q:
            next_step = f"Call tool_search with 1-3 keywords, then `{INVOKE_CATALOGUE_TOOL}`."
            attach_note = (
                "This is a catalogue listing only. Pass a short query to get parameter schemas, "
                f"then `{INVOKE_CATALOGUE_TOOL}`."
            )
        else:
            next_step = (
                "Retry tool_search once with shorter or different keywords, or a different group. "
                "If that also misses, tell the user the catalogue has no matching tool."
            )
            attach_note = (
                "No relevant tool was returned. Retry once with a shorter or different query "
                "(or another group). After two misses, tell the user this capability is not "
                "in the assistant catalogue — do not keep searching."
            )
        return {
            "ok": True,
            "query": query,
            "group": group_name,
            "risk": risk_filter,
            "count": len(matches),
            "tools": visible,
            "activation_tools": activation_tools,
            "fallback_from_group": fallback_from_group,
            "warning": group_warning,
            "groups": sorted(TOOL_GROUPS),
            "next_step": next_step,
            "note": (
                "Results are ranked by relevance. Prefer short keyword queries. "
                "This searches assistant tools only — NOT the public internet. "
                + (
                    "For web/news/public docs use `web_search` instead. "
                    if "web_search" in r._tools
                    else ""
                )
                + attach_note
            ),
        }

    r.register(
        "tool_search",
        "Search the assistant tool catalogue by purpose or name — NOT the public internet (use web_search for that).",
        {
            "query": {"type": "string", "description": "Search terms: 1-3 keywords work best (e.g. 'shell snippet', 'browser cookies', 'wasm decompile'). Longer natural-language phrases are also supported."},
            "group": {"type": "string", "description": f"Optional tool group. Known groups: {', '.join(sorted(TOOL_GROUPS))}.", "default": ""},
            "risk": {"type": "string", "description": "Optional exact risk filter, e.g. low / medium / high.", "default": ""},
            "confirmable": {"type": "boolean", "description": "Optional: true for tools that need confirmation, false for tools that do not."},
            "limit": {"type": "integer", "description": "Maximum results, 1-80.", "default": 20},
        },
        _tool_search,
        risk="low",
    )

    def _invoke_catalogue_tool(name: str = "", arguments: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                "invoke_catalogue_tool requires name= from tool_search. "
                "Pass the catalogue tool name and its arguments object."
            ),
            "name": str(name or ""),
        }

    r.register(
        INVOKE_CATALOGUE_TOOL,
        (
            "Run a catalogue tool that is not in the top-level function list. "
            "Use tool_search first, then pass that exact name and its arguments here "
            "(MCP-style tools/call; the session tool list does not grow)."
        ),
        {
            "name": {
                "type": "string",
                "description": "Exact catalogue tool name from tool_search.",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments for that tool (object, not a string).",
            },
        },
        _invoke_catalogue_tool,
        required=["name"],
        risk="low",
    )

    return r
