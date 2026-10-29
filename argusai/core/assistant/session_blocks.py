"""AssistantSession method mixin split from session.py."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import re
import time
import uuid
from typing import Any

from .attachments import *
from .messages import *
from .output import *
from .skills import *
from .streaming import *
from .tools import *

log = logging.getLogger("argusai.assistant")


def is_duplicate_tool_skip_result(result: Any) -> bool:
    """True when the runtime skipped an identical tool call in the same loop."""
    return (
        isinstance(result, dict)
        and bool(result.get("skipped"))
        and result.get("reason") == "duplicate_tool_call_skipped"
    )


_REVERSE_RESULT_TOOLS = frozenset({
    "reverse_job_create",
    "reverse_toolchain_scan", "reverse_toolchain_install", "reverse_sample_import",
    "android_apk_analyze", "ios_ipa_analyze", "ios_class_dump", "windows_pe_analyze", "elf_analyze",
    "android_jadx_decompile", "android_apktool_decode",
    "android_apktool_build", "android_apk_sign", "android_apk_validate",
    "binary_capa_analyze", "binary_yara_scan",
    "reverse_artifact_grep", "reverse_artifact_extract_strings",
    "reverse_job_link_proxy_flow",
    "proxy_import_har", "proxy_import_burp_log",
    "adb_devices", "adb_install", "adb_pull", "adb_push", "adb_logcat",
})


_PROXY_RESULT_TOOLS = frozenset({
    "proxy_search_traffic", "proxy_decode_flow",
    "proxy_rewrite_rule_save", "proxy_rewrite_rule_test",
    "proxy_rewrite_rule_delete", "proxy_rewrite_rules_list",
    "proxy_decrypt_rule_save", "proxy_decrypt_script_rule_save",
    "proxy_decrypt_rule_test", "proxy_decrypt_rule_delete", "proxy_decrypt_rules_list",
})


def _proxy_result_block(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Compact card for the passive-proxy tool family (search / decode / rules)."""
    block: dict[str, Any] = {
        "type": "proxy_result",
        "tool": name,
        "ok": not result.get("error"),
        "error": str(result.get("error") or ""),
    }
    if name == "proxy_search_traffic":
        flows = result.get("flows") or []
        block.update({
            "kind": "search",
            "matched": int(result.get("matched") or 0),
            "total": int(result.get("total") or 0),
            "timed_out": bool(result.get("timed_out")),
            "query": str(result.get("query") or ""),
            "flows": [
                {
                    "id": f.get("id"),
                    "method": f.get("method"),
                    "status": f.get("status") or f.get("response_status"),
                    "url": str(f.get("url") or "")[:200],
                }
                for f in flows[:8]
            ],
        })
    elif name == "proxy_decode_flow":
        decoded = result.get("decoded") if isinstance(result.get("decoded"), dict) else {}
        items: list[dict[str, Any]] = []
        for part in ("request", "response"):
            for item in (decoded.get(part) or {}).get("items", [])[:6]:
                items.append({
                    "part": part,
                    "location": str(item.get("location") or ""),
                    "source": item.get("source"),
                    "kind": item.get("kind"),
                    "cipher_hint": item.get("cipher_hint") or "",
                    "preview": str(item.get("text") or "")[:160],
                })
        block.update({"kind": "decode", "flow_id": result.get("flow_id"), "items": items[:10]})
    elif "rewrite" in name or "decrypt" in name:
        block["kind"] = "rewrite" if "rewrite" in name else "decrypt"
        if name.endswith("rules_list"):
            block["action"] = "list"
            block["rules"] = [
                {"name": r.get("name"), "match": r.get("match"), "enabled": r.get("enabled", True)}
                for r in (result.get("rules") or [])[:12]
            ]
            block["active"] = bool(result.get("active"))
        elif name.endswith("_delete"):
            block["action"] = "delete"
            block["removed"] = bool(result.get("removed"))
        elif name.endswith("_test"):
            block["action"] = "test"
            block["matched"] = result.get("matched")
            block["targets"] = result.get("targets")
            block["hint"] = str(result.get("hint") or "")
            req = result.get("request") if isinstance(result.get("request"), dict) else None
            resp = result.get("response") if isinstance(result.get("response"), dict) else None
            if req:
                block["request_preview"] = str(req.get("body_preview") or "")[:200]
                block["request_trace"] = list(req.get("trace") or [])[:8]
            if resp:
                block["response_preview"] = str(resp.get("body_preview") or "")[:200]
                block["response_trace"] = list(resp.get("trace") or [])[:8]
        else:
            block["action"] = "save"
            rule = result.get("rule") if isinstance(result.get("rule"), dict) else {}
            block["rule"] = {"name": rule.get("name"), "match": rule.get("match"),
                             "apply_to": rule.get("apply_to")}
    # Clickable: jump to the passive-proxy panel to inspect the flows/rules.
    block["actions"] = [{"label": "打开被动代理", "type": "open_route", "payload": {"href": "#/proxy"}}]
    return block


_API_INVENTORY_TOOLS = frozenset({
    "api_inventory_build", "api_spec_import", "api_request_select", "api_inventory_diff",
})


def _api_inventory_block(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Card for the API-inventory family (build / spec import / select / diff)."""
    block: dict[str, Any] = {
        "type": "api_inventory",
        "tool": name,
        "ok": not result.get("error"),
        "error": str(result.get("error") or ""),
    }
    if name == "api_inventory_diff":
        block.update({
            "kind": "diff",
            "added": list(result.get("added") or [])[:20],
            "removed": list(result.get("removed") or [])[:20],
            "unchanged": int(result.get("unchanged_count") or 0),
        })
        return block
    endpoints = result.get("endpoints") or []
    block.update({
        "kind": "inventory",
        "count": int(result.get("count") or len(endpoints)),
        "spec_kind": str(result.get("spec_kind") or ""),
        "groups": result.get("by_protocol_or_tag") if isinstance(result.get("by_protocol_or_tag"), dict) else {},
        "endpoints": [
            {
                "method": e.get("method"),
                "url": str(e.get("url") or "")[:200],
                "protocol": e.get("protocol"),
                "tags": [t for t in (e.get("tags") or []) if t][:4],
                "secured": bool(e.get("secured")),
            }
            for e in endpoints[:20]
            if isinstance(e, dict)
        ],
    })
    return block


_NETWORK_CARD_TOOLS = frozenset({
    "nmap_scan", "network_scan_ports", "network_discover_hosts",
    "network_fingerprint_services", "network_profile_host",
})
_SCANNER_CARD_TOOLS = frozenset({"nuclei_run_scan", "ffuf_run_scan", "sqlmap_run_scan"})
_PROTOCOL_CARD_TOOLS = frozenset({
    "graphql_probe", "graphql_batch_probe", "websocket_connect_probe",
    "websocket_handshake_probe", "websocket_frame_analyze", "grpc_web_analyze",
})
_BUSINESS_CARD_TOOLS = frozenset({
    "business_surface_extract", "business_test_plan", "business_risk_select",
})
_SECURITY_CARD_TOOLS = (
    _NETWORK_CARD_TOOLS | _SCANNER_CARD_TOOLS | _PROTOCOL_CARD_TOOLS | _BUSINESS_CARD_TOOLS
)
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4, "unknown": 5}


def _security_card_block(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """One compact card for network / scanner / protocol / business tool output."""
    block: dict[str, Any] = {
        "type": "security_card",
        "tool": name,
        "ok": not result.get("error"),
        "error": str(result.get("error") or ""),
        "kind": "",
        "stat": "",
        "chips": [],
        "rows": [],
    }
    if result.get("error"):
        return block

    if name in _NETWORK_CARD_TOOLS:
        block["kind"] = "network"
        parsed = result.get("parsed") if isinstance(result.get("parsed"), dict) else {}
        hosts = parsed.get("hosts") or result.get("hosts") or []
        rows: list[dict[str, Any]] = []
        open_count = 0
        for host in hosts[:12]:
            if not isinstance(host, dict):
                continue
            addr = ""
            for a in host.get("addresses") or []:
                if isinstance(a, dict) and (a.get("addrtype") == "ipv4" or not addr):
                    addr = a.get("addr") or addr
            addr = addr or str(host.get("host") or host.get("ip") or "")
            for port in (host.get("ports") or [])[:40]:
                if not isinstance(port, dict):
                    continue
                state = port.get("state", {})
                st = state.get("state") if isinstance(state, dict) else state
                if st and st != "open":
                    continue
                open_count += 1
                svc = port.get("service", {}) if isinstance(port.get("service"), dict) else {}
                val = " ".join(x for x in [svc.get("name"), svc.get("product"), svc.get("version")] if x) if isinstance(svc, dict) else str(svc)
                rows.append({"a": f"{addr}:{port.get('port')}", "b": val})
        if not rows:
            for port in (result.get("open_ports") or result.get("ports") or [])[:40]:
                if isinstance(port, dict):
                    rows.append({"a": str(port.get("port")), "b": str(port.get("service") or port.get("state") or "")})
                else:
                    rows.append({"a": str(port), "b": ""})
            open_count = len(rows)
        block["stat"] = f"{len(hosts) or 1} host · {open_count} open"
        block["rows"] = rows[:24]

    elif name == "nuclei_run_scan":
        block["kind"] = "scanner"
        findings = result.get("findings") or []
        counts: dict[str, int] = {}
        for f in findings:
            sev = str(f.get("severity") or "unknown")
            counts[sev] = counts.get(sev, 0) + 1
        block["chips"] = [
            {"label": f"{k}·{v}", "sev": k}
            for k, v in sorted(counts.items(), key=lambda kv: _SEV_ORDER.get(kv[0], 9))
        ]
        block["rows"] = [
            {"a": str(f.get("name") or f.get("template_id") or ""), "b": str(f.get("matched_at") or f.get("host") or ""), "sev": str(f.get("severity") or "")}
            for f in findings[:24]
        ]
        block["stat"] = f"{result.get('finding_count', len(findings))} findings"

    elif name == "ffuf_run_scan":
        block["kind"] = "scanner"
        res = result.get("results") or []
        block["rows"] = [{"a": str(r.get("status")), "b": str(r.get("url") or r.get("input") or "")} for r in res[:24] if isinstance(r, dict)]
        block["stat"] = f"{result.get('result_count', len(res))} hits"

    elif name == "sqlmap_run_scan":
        block["kind"] = "scanner"
        vuln = bool(result.get("vulnerable"))
        block["chips"] = [{"label": "vulnerable" if vuln else "no finding", "sev": "high" if vuln else "info"}]
        data = result.get("parsed") or result.get("data") or {}
        if isinstance(data, dict):
            for k, v in list(data.items())[:10]:
                block["rows"].append({"a": str(k), "b": str(v)[:120]})
        block["stat"] = "SQLi" if vuln else "sqlmap"

    elif name in _PROTOCOL_CARD_TOOLS:
        block["kind"] = "protocol"
        if name.startswith("graphql"):
            schema = result.get("schema") if isinstance(result.get("schema"), dict) else {}
            types = result.get("types") or schema.get("types") or []
            block["stat"] = "GraphQL" + (f" · {len(types)} types" if isinstance(types, list) and types else "")
            if isinstance(types, list) and types:
                block["rows"] = [{"a": str(t.get("name") if isinstance(t, dict) else t), "b": str(t.get("kind") if isinstance(t, dict) else "")} for t in types[:24]]
            else:
                for k in ("introspection", "query_type", "mutation_type", "batching_allowed"):
                    if k in result:
                        block["chips"].append({"label": f"{k}:{result.get(k)}", "sev": "info"})
        elif name.startswith("websocket"):
            frames = result.get("frames") or result.get("messages") or []
            block["stat"] = f"WebSocket {result.get('status') or ''}".strip()
            for k in ("accepted", "subprotocol", "origin_reflected", "authz"):
                if k in result:
                    block["chips"].append({"label": f"{k}:{result.get(k)}", "sev": "info"})
            if isinstance(frames, list):
                block["rows"] = [{"a": str(f.get("opcode") if isinstance(f, dict) else ""), "b": str((f.get("text") if isinstance(f, dict) else f) or "")[:120]} for f in frames[:16]]
        else:  # grpc_web_analyze
            block["stat"] = "gRPC-Web"
            for k in ("services", "methods", "messages"):
                v = result.get(k)
                if isinstance(v, list):
                    block["rows"].extend({"a": str(x)[:80], "b": k} for x in v[:12])

    else:  # business
        block["kind"] = "business"
        assets = result.get("assets") or result.get("candidate_assets") or []
        ops = result.get("operations") or result.get("candidate_operations") or []
        gaps = result.get("gaps") or result.get("evidence_gaps") or result.get("unknowns") or []
        if assets:
            block["chips"].append({"label": f"assets·{len(assets)}", "sev": "info"})
        if ops:
            block["chips"].append({"label": f"ops·{len(ops)}", "sev": "info"})
        if gaps:
            block["chips"].append({"label": f"gaps·{len(gaps)}", "sev": "medium"})
        for a in (assets or [])[:8]:
            block["rows"].append({"a": str((a.get("asset") or a.get("name") or a.get("id")) if isinstance(a, dict) else a or ""), "b": "asset"})
        for o in (ops or [])[:8]:
            block["rows"].append({"a": str((o.get("operation") or o.get("name") or o.get("id")) if isinstance(o, dict) else o or ""), "b": "op"})
        block["stat"] = f"{len(assets)} assets · {len(gaps)} gaps" if (assets or gaps) else "business surface"
    return block


def _reverse_result_block(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Structured assistant card for mobile/native reverse tool results."""
    attached = result.get("attached") if isinstance(result.get("attached"), dict) else {}
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    block: dict[str, Any] = {
        "type": "reverse_result",
        "title": name,
        "ok": result.get("ok"),
        "partial": bool(result.get("partial")),
        "usable_output": bool(result.get("usable_output")),
        "error": str(result.get("error") or ""),
        "blocked": bool(result.get("blocked")),
        "do_not_retry": bool(result.get("do_not_retry")),
    }
    job_id = str(result.get("job_id") or attached.get("job_id") or job.get("id") or "").strip()
    if job_id:
        block["job_id"] = job_id
    index_ref = str(
        attached.get("index_artifact_ref")
        or attached.get("artifact_ref")
        or result.get("index_artifact_ref")
        or ""
    ).strip()
    if index_ref:
        block["index_artifact_ref"] = index_ref
    output_dir = str(result.get("output_dir") or attached.get("output_dir") or "").strip()
    if output_dir:
        block["output_dir"] = output_dir
    tree = result.get("tree") if isinstance(result.get("tree"), dict) else {}
    file_count = tree.get("file_count")
    if file_count is not None:
        block["file_count"] = file_count
    if name == "reverse_job_create":
        job_title = str(job.get("title") or "").strip()
        block["summary"] = (
            f"Created ReverseJob {job_id}"
            + (f" · {job_title}" if job_title else "")
        ).strip()
        block["next_step"] = "Continue in this conversation, or open the Reverse workbench when needed."
    elif name in {"android_jadx_decompile", "android_apktool_decode"}:
        label = "JADX" if "jadx" in name else "APKTool"
        parts = [f"{label} decompile"]
        if block.get("partial"):
            parts.append("partial (usable)")
        if file_count is not None:
            parts.append(f"{file_count} files")
        if output_dir:
            parts.append(output_dir)
        block["summary"] = " · ".join(parts)
        block["next_step"] = (
            "Open output_dir in the job folder, then use reverse_artifact_grep / "
            "reverse_artifact_extract_strings — do not paste the full decompile tree."
        )
    elif name == "android_apktool_build":
        apk_ref = str(attached.get("artifact_ref") or result.get("output_apk") or "").strip()
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        v_ok = validation.get("ok")
        suffix = "" if v_ok is None else (" · validate ok" if v_ok else " · validate failed")
        block["summary"] = f"APK built{f' → {apk_ref}' if apk_ref else ''}{suffix}"
        if validation and not validation.get("ok"):
            block["next_step"] = str(
                validation.get("suggested_fix")
                or "Fix validation errors (often compressed lib/**/*.so) before android_apk_sign."
            )
        else:
            block["next_step"] = "Run android_apk_sign(job_id=...) then adb_install for lab sideload."
    elif name == "android_apk_sign":
        apk_ref = str(attached.get("artifact_ref") or result.get("output_apk") or "").strip()
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        v_ok = validation.get("ok")
        suffix = "" if v_ok is None else (" · validate ok" if v_ok else " · validate failed")
        block["summary"] = f"APK signed{f' → {apk_ref}' if apk_ref else ''}{suffix}"
        if validation and not validation.get("ok"):
            block["next_step"] = str(
                validation.get("suggested_fix")
                or "Fix validation errors before adb_install."
            )
        else:
            block["next_step"] = "adb_install(job_id=..., artifact_id=...) on a connected device."
    elif name == "android_apk_validate":
        hint = str(result.get("installable_hint") or "").strip()
        err_n = len(result.get("errors") or [])
        warn_n = len(result.get("warnings") or [])
        block["summary"] = f"APK validate · {hint or ('ok' if result.get('ok') else 'failed')} · {err_n} err · {warn_n} warn"
        if result.get("suggested_fix"):
            block["next_step"] = str(result.get("suggested_fix"))
        elif result.get("ok"):
            block["next_step"] = "adb_install(job_id=..., artifact_id=...) on a connected device."
    elif name == "reverse_artifact_grep" and result.get("scan_root"):
        block["summary"] = (
            f"grep {result.get('match_count_returned', len(result.get('matches') or []))} hits "
            f"in {result.get('scan_root')}"
        )
    elif name == "reverse_job_link_proxy_flow":
        summary = result.get("flow_summary") if isinstance(result.get("flow_summary"), dict) else {}
        url = str(summary.get("url") or summary.get("host") or "").strip()
        block["summary"] = f"Linked proxy flow {summary.get('id', '')} {url}".strip()
        block["next_step"] = str(result.get("next_step") or "")
    elif name in {"proxy_import_har", "proxy_import_burp_log"}:
        block["summary"] = str(result.get("summary") or f"{name} · {result.get('entry_count', 0)} entries")
        block["next_step"] = str(result.get("next_step") or "")
    elif name == "ios_class_dump":
        block["summary"] = f"class-dump · {result.get('header_count', 0)} headers"
        if output_dir:
            block["next_step"] = "Open output_dir and review Objective-C interfaces."
    elif name == "reverse_toolchain_install":
        tool_key = str(result.get("tool") or "").strip()
        install_root = str(result.get("install_root") or "").strip()
        path = str(result.get("path") or "").strip()
        paths_saved = result.get("paths_saved") if isinstance(result.get("paths_saved"), dict) else {}
        if install_root:
            block["install_root"] = install_root
        if path:
            block["path"] = path
        if paths_saved:
            block["paths_saved"] = {str(k): str(v) for k, v in paths_saved.items()}
        block["already_installed"] = bool(result.get("already_installed"))
        block["verified"] = bool(result.get("verified"))
        if result.get("ok"):
            if result.get("already_installed"):
                block["summary"] = f"{tool_key} already installed · {path or 'PATH'}"
                block["next_step"] = "Call reverse_toolchain_scan to refresh, or proceed with the CLI bridge tool."
            else:
                block["summary"] = (
                    f"Installed {tool_key} under {install_root or 'data_root/tools/reverse'}"
                    + (f" ({result.get('duration_ms', 0)} ms)" if result.get("duration_ms") else "")
                )
                block["next_step"] = (
                    "Run reverse_toolchain_scan to verify, then call the requested CLI bridge "
                    "(android_jadx_decompile / android_apktool_decode / binary_capa_analyze / …)."
                )
        else:
            block["summary"] = f"Install {tool_key} failed: {result.get('error', '')}"
            docs = str(result.get("docs_url") or "").strip()
            if docs:
                block["docs_url"] = docs
                block["next_step"] = f"Ask user to install manually: {docs}"
            else:
                block["next_step"] = "Retry with force=true after fixing the cause, or fall back to manual install."
    elif isinstance(result.get("summary"), str):
        block["summary"] = result["summary"]
    return block


class SessionBlocksMixin:
    async def _emit_status(
        self,
        on_status: Callable[[str], Awaitable[None] | None] | None,
        text: str,
    ) -> None:
        if not on_status:
            return
        maybe = on_status(text)
        if hasattr(maybe, "__await__"):
            await maybe

    def _tool_timeout_seconds(self, fn_name: str, args: dict[str, Any] | None) -> int | None:
        payload = args or {}
        raw = payload.get("timeout_seconds")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
        schema = EXPLICIT_SCHEMAS.get(str(fn_name or "")) or {}
        spec = schema.get("timeout_seconds") or {}
        default = spec.get("default")
        if default is not None:
            try:
                return max(1, int(default))
            except (TypeError, ValueError):
                pass
        return None

    async def _emit_tool_start(
        self,
        on_status: Callable[[str], Awaitable[None] | None] | None,
        fn_name: str,
        args: dict[str, Any] | None,
    ) -> None:
        timeout = self._tool_timeout_seconds(fn_name, args)
        if timeout and timeout >= 30:
            await self._emit_status(on_status, f"正在执行工具：{fn_name}（上限 {timeout}s）")
        else:
            await self._emit_status(on_status, f"正在执行工具：{fn_name}")


    async def _emit_agent_round_start(
        self,
        on_segment: Callable[[int], Awaitable[None] | None] | None,
        round_index: int,
        *,
        force: bool = False,
    ) -> None:
        """Signal the UI to open a new assistant bubble for this agent-loop round.

        Round 0 of a fresh user turn stays in the first bubble. Pass
        ``force=True`` for nested/resume loops so their first iteration also
        opens a new segment instead of appending into the previous mega-bubble.
        """
        if not on_segment:
            return
        ri = int(round_index)
        if ri <= 0 and not force:
            return
        maybe = on_segment(max(ri, 1))
        if hasattr(maybe, "__await__"):
            await maybe


    async def _emit_trace_blocks(
        self,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
        trace: dict[str, Any],
    ) -> None:
        """Stream the rendered UI blocks for a single freshly-executed tool
        trace so the frontend can show the call as soon as it completes,
        instead of waiting for the whole agent loop to finish."""
        if not on_block:
            return
        try:
            blocks = self._blocks_for_tool_trace([trace])
        except Exception:
            blocks = [{"type": "tool_trace", **trace}]
        for blk in blocks:
            # ``stop_for_user_decision`` is committed as the final
            # ChatResponse block by ``_commit_user_decision_stop``. If we also
            # stream that same card here, the frontend can temporarily receive
            # two identical decision cards in two assistant bubbles (streamed
            # tool block + final commit). Keep decision cards final-only; tool
            # result cards can still stream immediately.
            if isinstance(blk, dict) and blk.get("type") == "user_decision":
                continue
            maybe = on_block(blk)
            if hasattr(maybe, "__await__"):
                await maybe


    async def _emit_single_block(
        self,
        on_block: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
        block: dict[str, Any],
    ) -> None:
        if not on_block or not block:
            return
        maybe = on_block(block)
        if hasattr(maybe, "__await__"):
            await maybe


    async def _emit_checkpoint(self) -> None:
        """Tell the host (GUI bridge / CLI) to persist current state NOW.

        Called from inside the agent loop after every meaningful state
        change (tool batch executed, auto-continue injected, budget
        restart, cancellation commit). The host wires this to
        ``_save_session`` so a process crash / network blowup mid-turn
        leaves an up-to-date checkpoint on disk instead of the snapshot
        from when the turn first started.

        Failures here are swallowed — a checkpoint write going wrong
        must NOT abort the turn that just produced real progress.
        """
        cb = getattr(self, "_on_checkpoint", None)
        if not cb:
            return
        try:
            maybe = cb()
            if hasattr(maybe, "__await__"):
                await maybe
        except Exception as e:
            log.warning("checkpoint callback failed: %s", e)


    def _no_ai_response(self) -> str:
        return (
            "尚未配置 AI 端点。\n\n"
            "请到“设置”里添加 OpenAI 兼容或 Anthropic 兼容端点，填写 base URL、API Key 和模型名称后再使用助手。"
        )


    def _detect_user_language(self, messages: list[dict[str, Any]]) -> str:
        """Return 'zh' if the most recent user content contains Chinese, else 'en'."""
        for msg in reversed(messages or []):
            if (msg or {}).get("role") != "user":
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str):
                try:
                    content = str(content)
                except Exception:
                    content = ""
            if any("\u4e00" <= ch <= "\u9fff" for ch in content):
                return "zh"
            if content.strip():
                return "en"
        return "zh"


    def _trace(self, name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
        return {
            "name": name,
            "arguments": arguments,
            "result": result,
            "ok": not (isinstance(result, dict) and result.get("error")),
        }


    def _tool_call_key(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            args = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            args = str(arguments)
        return f"{name}:{args}"


    def _should_dedupe_tool_call(self, name: str, arguments: dict[str, Any]) -> bool:
        """Loom22 §11 exact-dedupe eligibility for ``name`` + ``arguments``."""
        from .streaming import should_dedupe_tool_call

        tool = self.registry.get(name) if getattr(self, "registry", None) else None
        return should_dedupe_tool_call(tool, arguments, name=name)


    def _remember_executed_tool_key(
        self,
        executed_keys: set[str],
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        """Record a call key only when the tool is exact-dedupe eligible."""
        if self._should_dedupe_tool_call(name, arguments):
            executed_keys.add(self._tool_call_key(name, arguments))


    def _duplicate_tool_result(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # NOTE: ``ok: true`` is intentional — a dedup hit is NOT a tool failure
        # from the LLM's perspective. Reporting ``ok: false`` was actively
        # harmful: the model interpreted it as "the tool errored" and would
        # either retry with the same args (producing yet another dedup hit)
        # or pivot to a completely different vector under the false belief
        # that the original tool was broken. ``skipped: true`` with the same
        # name + arguments is enough for it to look back in trace history.
        return {
            "ok": True,
            "skipped": True,
            "reason": "duplicate_tool_call_skipped",
            "message": (
                f"Skipped repeat of {name} with identical arguments inside the same agent loop. "
                "The previous call's result is in your earlier tool trace — re-read it instead of re-issuing. "
                "If you actually need a fresh value (server-state changed, time-dependent poll), call with at least one different argument (e.g. wait_seconds, since, fresh_context, force=true)."
            ),
            "name": name,
            "arguments": arguments,
        }


    def _ensure_pending_identity(self) -> tuple[str, int]:
        """Return the stable identity of the active approval request."""

        pending = getattr(self, "_pending", None)
        if not isinstance(pending, dict):
            return "", 0
        pending_id = str(pending.get("pending_id") or "").strip()
        if not pending_id:
            pending_id = f"pending_{uuid.uuid4().hex}"
            pending["pending_id"] = pending_id
        try:
            version = max(1, int(pending.get("version") or 1))
        except (TypeError, ValueError):
            version = 1
        pending["version"] = version
        events = getattr(self, "events", None)
        if isinstance(events, list) and not any(
            isinstance(event, dict)
            and event.get("kind") == "approval_request"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("pending_id") == pending_id
            for event in events
        ):
            raw_items = pending.get("confirmations")
            if isinstance(raw_items, list):
                items = [item for item in raw_items if isinstance(item, dict)]
            else:
                item = pending.get("tool_call")
                items = [item] if isinstance(item, dict) else []
            requests = []
            for item in items:
                tool_call = item.get("tool_call")
                requests.append({
                    "call_id": str(
                        tool_call.get("id") if isinstance(tool_call, dict) else ""
                    ),
                    "tool_name": str(item.get("name") or ""),
                    "risk": _tool_risk(str(item.get("name") or "")),
                })
            now = time.time()
            events.append({
                "id": f"evt_{int(now * 1000)}_{len(events)}",
                "kind": "approval_request",
                "source": "runtime",
                "render_policy": "card",
                "context_policy": "exclude",
                "turn_id": str(getattr(self, "_current_turn_id", "") or ""),
                "timestamp": now,
                "payload": {
                    "pending_id": pending_id,
                    "version": version,
                    "requests": requests,
                },
            })
        return pending_id, version

    def _pending_tool_projection(
        self,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        """Project one pending call for UI rendering without chat text."""

        pending_id, version = self._ensure_pending_identity()
        tool_call = item.get("tool_call") if isinstance(item.get("tool_call"), dict) else {}
        call_id = str(tool_call.get("id") or item.get("id") or "")
        return {
            "id": call_id,
            "name": str(item.get("name") or ""),
            "arguments": item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            "pending_id": pending_id,
            "version": version,
        }

    def pending_confirmation_projection(self) -> dict[str, Any]:
        """Return renderer-ready cards for the durable active pending state."""

        pending = getattr(self, "_pending", None)
        if not isinstance(pending, dict):
            return {"pending_tool": None, "pending_tools": [], "pending_blocks": []}
        raw_items = pending.get("confirmations")
        if isinstance(raw_items, list):
            items = [item for item in raw_items if isinstance(item, dict)]
        else:
            item = pending.get("tool_call")
            items = [item] if isinstance(item, dict) else []
        decisions = pending.get("decisions") if isinstance(pending.get("decisions"), dict) else {}
        tools = [self._pending_tool_projection(item) for item in items]
        blocks = []
        for item in items:
            tool_call = item.get("tool_call")
            call_id = str(
                tool_call.get("id") or ""
                if isinstance(tool_call, dict)
                else ""
            )
            blocks.append(self._confirm_block(
                str(item.get("name") or ""),
                item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
                confirm_id=call_id,
                decision=(
                    "allow"
                    if decisions.get(call_id) in {"allow", "allow_session"}
                    else ""
                ),
                runtime=str(item.get("runtime") or ""),
                covered_tool_names=item.get("covered_tool_names"),
            ))
        return {
            "pending_tool": tools[0] if tools else None,
            "pending_tools": tools,
            "pending_blocks": blocks,
        }

    def _confirm_block(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_id: str = "",
        decision: str = "",
        runtime: str = "",
        covered_tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        pending_id, pending_version = self._ensure_pending_identity()
        if not confirm_id:
            pending = getattr(self, "_pending", None)
            if isinstance(pending, dict):
                item = pending.get("tool_call")
                if isinstance(item, dict):
                    tool_call = item.get("tool_call")
                    if isinstance(tool_call, dict):
                        confirm_id = str(tool_call.get("id") or "")
        if not runtime and name == "runtime_install":
            requested = str(arguments.get("runtime") or "").strip().lower()
            if requested in {"python", "node", "wabt"}:
                runtime = requested
                covered_tool_names = ["runtime_install"]
        risk = _tool_risk(name)
        block_type = "command_plan" if name in {
            "ping_host", "dns_lookup", "trace_route", "http_curl", "whois_lookup", "nmap_scan",
            "network_discover_hosts", "network_scan_ports", "network_fingerprint_services", "network_profile_host",
            "pentest_verify_finding", "post_exploit_lab_generate", "weak_credential_check",
            "websocket_connect_probe", "websocket_send_receive", "websocket_authz_probe",
            "export_task_report", "export_workspace_bundle",
        } else ("scan_plan" if name in {"create_scan", "plan_pentest"} else "confirm")

        # MCP cards get a finer-grained auto-allow surface: tick this and
        # the user only auto-allows THIS server/tool, not every future
        # mcp_server_call_tool invocation.
        permission_key = ""
        auto_allow_label = "本次对话自动允许此工具"
        try:
            permission_key = self._mcp_permission_key(name, arguments)  # type: ignore[attr-defined]
        except Exception:
            permission_key = ""
        if permission_key:
            auto_allow_label = (
                "本次对话自动允许此 Agent 的同类操作"
                if name == EXTERNAL_AGENT_TOOL
                else "本次对话自动允许此 MCP 工具"
            )

        # Decision matrix for the auto-allow surface:
        #
        #   1. Tools in the hardcoded never-list (mcp_server_add / delete /
        #      stdio_call_tool) — auto-allow ALWAYS hidden. These are
        #      config / bypass operations.
        #   2. MCP call_tool with a permission_key — auto-allow is offered
        #      because it records exactly `mcp:<server_id>:<tool_name>`, not
        #      the whole wrapper. The first call still needs confirmation;
        #      clicking auto-allow means "do not ask again for THIS child
        #      tool in THIS session".
        #      read_resource keeps the stricter per-server policy because
        #      resources can be arbitrary external/user data.
        #   3. Non-MCP tools — original rule: hide on risk=="high".
        actions: list[dict[str, Any]] = []
        hardcoded_no_auto = name in {
            "mcp_server_add", "mcp_server_delete", "mcp_stdio_call_tool",
            "user_profile_apply_update", "runtime_install",
        }
        if runtime or hardcoded_no_auto:
            allow_auto = False
        elif name == "mcp_server_call_tool" and permission_key:
            allow_auto = True
        elif name == EXTERNAL_AGENT_TOOL and permission_key:
            # Same bargain as MCP: the grant records `acp:<agent>:<kind>`, so
            # saying yes to reading files does not also say yes to shell.
            allow_auto = True
        elif permission_key:
            try:
                allow_auto = bool(self._mcp_server_allows_auto_approve(arguments))  # type: ignore[attr-defined]
            except Exception:
                allow_auto = False
        else:
            allow_auto = risk != "high" or name in _REVERSE_DYNAMIC_TOOLS
        if allow_auto:
            action: dict[str, Any] = {"label": auto_allow_label, "value": "allow_session"}
            if permission_key:
                action["permission_key"] = permission_key
            actions.append(action)

        body = ""
        variant = ""
        desktop_tools = {
            "desktop_observe",
            "desktop_launch_app",
            "desktop_focus_window",
            "desktop_uia_find",
            "desktop_uia_action",
            "desktop_mouse_move",
            "desktop_click",
            "desktop_type",
            "desktop_hotkey",
            "desktop_action_session_start",
            "desktop_action_session_stop",
        }
        if name in desktop_tools:
            variant = "desktop_control"
            if name == "desktop_observe":
                body = (
                    "将读取前台窗口/窗口列表，可选截图到当前会话沙盒。"
                    "截图与标题可能包含敏感信息（密码、聊天、令牌等），仅用于当前审计。"
                )
                try:
                    import sys as _sys
                    if _sys.platform == "darwin":
                        from argusai.core.workspace import darwin_permissions as _dp
                        extra = _dp.confirm_hint_for_tool(name)
                        if extra:
                            body += f"\n{extra}"
                except Exception:
                    pass
            elif name == "desktop_launch_app":
                app = str(arguments.get("name") or arguments.get("executable_path") or "").strip()
                body = (
                    f"将启动本地桌面应用{('：' + app) if app else ''}。"
                    "该工具只启动应用，不执行任意 shell 命令；启动后可用 desktop_observe 截图确认。"
                )
            elif name == "desktop_action_session_start":
                body = (
                    "桌面鼠标/键盘已不再需要限时授权。"
                    "此工具是兼容占位，不会开启授权会话；可直接使用 desktop_click / desktop_mouse_move。"
                )
            elif name == "desktop_action_session_stop":
                body = "桌面限时授权已取消，此工具不会改变任何状态。"
            else:
                body = "将在本机桌面执行鼠标或键盘动作。"
                try:
                    import sys as _sys
                    if _sys.platform == "darwin":
                        from argusai.core.workspace import darwin_permissions as _dp
                        extra = _dp.confirm_hint_for_tool(name)
                        if extra:
                            body += f"\n{extra}"
                except Exception:
                    pass
        elif name == EXTERNAL_AGENT_TOOL:
            body = (
                f"外部 Agent「{arguments.get('agent') or '未知'}」请求执行它自己的工具"
                f"（类型 {arguments.get('kind') or 'other'}）。\n"
                "该工具由外部 Agent 实现，ArgusAI 看不到它的代码，也无法限制它的作用范围。"
            )
            detail = str(arguments.get("detail") or "").strip()
            if detail:
                body += f"\n{detail}"
        elif name.startswith("user_todo_"):
            body = "将修改你的个人待办（跨会话持久化，与会话内评估计划 todo 不同）。"
        elif name == "user_profile_apply_update":
            body = "将写入用户习惯 Profile（设置页可见，与知识库分离）。"

        confirm_title = f"确认执行 {name}"
        if name == EXTERNAL_AGENT_TOOL:
            confirm_title = str(arguments.get("title") or "").strip() or "外部 Agent 请求执行工具"
        if runtime:
            from argusai.core.managed_runtimes import (
                archive_sources,
                archive_spec,
                runtime_root,
            )
            from argusai.core.paths import PathManager

            runtime_label = {
                "python": "Python",
                "node": "Node.js",
                "wabt": "WABT",
            }.get(runtime, runtime)
            try:
                spec = archive_spec(runtime)
                download_mb = max(1, round(spec.approx_download_bytes / (1024 * 1024)))
                sources = archive_sources(spec, self._runtime_mirrors())
                source = sources[0] if sources else "未配置可用下载源"
                version = spec.version
            except Exception:
                download_mb = 0
                source = "请在设置中检查下载源"
                version = "未知"
            tools_dir = self._runtime_tools_dir()
            api = getattr(self.registry, "api", None)
            try:
                data_root = api.settings_mgr.get().data_root
            except Exception:
                data_root = None
            session_id = str(getattr(self.registry, "session_id", None) or "default")
            sandbox = PathManager(data_root=data_root).assistant_sandbox(session_id)
            tool_names = list(dict.fromkeys(covered_tool_names or [name]))
            continuation = "、".join(tool_names)
            size_text = f"约 {download_mb} MB" if download_mb else "大小将在下载前确认"
            # WABT is a set of CLI tools, not a sandbox runtime — calling it a
            # "沙盒环境" would misdescribe what the user is approving.
            noun = "WebAssembly 工具链" if runtime == "wabt" else "沙盒环境"
            body = (
                f"将安装 {runtime_label} {noun}（版本 {version}，下载 {size_text}）。\n"
                f"安装位置：{runtime_root(runtime, tools_dir)}\n"
            )
            if runtime != "wabt":
                body += f"当前会话隔离目录：{sandbox}\n"
            body += (
                f"首选下载源：{source}\n"
                f"安装并校验完成后，将在本次操作中继续执行：{continuation}。"
            )
            if runtime == "python":
                body += "\nPython 代码只在当前会话的 .venv 中运行，不会静默使用系统 Python。"
            elif runtime == "node":
                body += "\nNode.js 本体共享复用，HOME、临时目录、npm 缓存和工作目录按会话隔离。"
            else:
                body += "\nWABT 只提供 wasm2wat / wasm-decompile 等命令行工具，不执行被分析的 WASM 模块。"
            confirm_title = f"安装 {runtime_label} {noun}？"
            variant = "runtime_install"
        if name == "desktop_observe":
            confirm_title = "查看桌面窗口？"
        block: dict[str, Any] = {
            "type": block_type,
            "title": confirm_title,
            "name": name,
            "pending_id": pending_id,
            "pending_version": pending_version,
            "confirm_id": confirm_id,
            "decision": decision,
            "arguments": arguments,
            "risk": risk,
            "permission_key": permission_key,
            "confirm_value": "yes",
            "cancel_value": "no",
            "actions": actions,
        }
        if body:
            block["body"] = body
        if variant:
            block["variant"] = variant
        if name == "desktop_observe":
            block["compact"] = True
        return block


    def _blocks_for_text(self, text: str) -> list[dict[str, Any]]:
        if "尚未配置 AI" in text or "No AI provider" in text:
            return [{
                "type": "confirm",
                "title": "配置 AI 端点",
                "body": "助手需要 AI 端点后才能执行自动化分析。",
                "variant": "info",
                "actions": [{"label": "打开设置", "href": "#/settings"}],
            }]
        return []


    def _blocks_for_tool_trace(self, traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        has_model_file_view = any(tr.get("name") == "show_file_resource_view" for tr in traces)
        for tr in traces:
            name = tr.get("name", "")
            result = tr.get("result")
            if is_duplicate_tool_skip_result(result):
                args_preview = tr.get("arguments") if isinstance(tr.get("arguments"), dict) else {}
                blocks.append({
                    "type": "tool_skipped",
                    "title": name or "tool",
                    "tool_name": name or "tool",
                    "reason": str(result.get("reason") or "duplicate_tool_call_skipped"),
                    "message": str(result.get("message") or ""),
                    "arguments": args_preview,
                    "variant": "info",
                })
                continue
            if isinstance(result, dict) and result.get("unknown_tool"):
                blocks.append({
                    "type": "tool_skipped",
                    "title": name or "unknown tool",
                    "tool_name": name or "tool",
                    "reason": "unknown_tool",
                    "message": str(result.get("error") or ""),
                    "suggested_fix": str(result.get("suggested_fix") or ""),
                    "suggestions": list(result.get("suggestions") or []),
                    "next_step": str(result.get("next_step") or ""),
                    "arguments": tr.get("arguments") if isinstance(tr.get("arguments"), dict) else {},
                    "variant": "error",
                })
                continue
            if name in {"create_scan", "rescan_task"} and isinstance(result, dict) and result.get("id"):
                blocks.append({"type": "task_progress", "task_id": result["id"], "snapshot": result.get("meta") or {}})
            elif name == "wait_for_task" and isinstance(result, dict):
                if result.get("id"):
                    blocks.append({"type": "task_progress", "task_id": result["id"], "snapshot": result})
                    blocks.append({
                        "type": "findings_summary",
                        "task_id": result.get("id"),
                        "counts": result.get("severity_counts") or {},
                        "top_findings": result.get("top_findings") or [],
                    })
            elif name == "plan_pentest" and isinstance(result, dict):
                blocks.append({"type": "scan_plan", **result})
            elif name in {"analyze_findings", "get_task_detail"} and isinstance(result, dict):
                blocks.append({
                    "type": "findings_summary",
                    "task_id": result.get("task_id") or result.get("id"),
                    "counts": result.get("severity_counts") or result.get("severity") or {},
                    "top_findings": result.get("top_critical") or result.get("top_findings") or [],
                })
            elif name in {"http_probe", "payload_probe", "browser_probe", "browser_evaluate", "browser_screenshot", "fetch_page_source", "extract_page_text", "http_request_impersonate"} and isinstance(result, dict):
                blocks.append({"type": "probe_result", **result})
            elif name in {"browser_trace", "browser_trace_crypto", "browser_trace_form", "browser_snapshot_state", "browser_instrument_click", "captcha_runtime_trace"} and isinstance(result, dict):
                blocks.append({"type": "runtime_trace", "title": name, **result})
            elif name == "sandbox_list_files" and isinstance(result, dict):
                if has_model_file_view:
                    # Model already called show_file_resource_view this turn — skip entirely.
                    continue
                root_items = result.get("root_items")
                if isinstance(root_items, list):
                    blocks.append({
                        "type": "file_resource_view",
                        "source_tool": "sandbox_list_files",
                        "render_policy": "tool_authored",
                        "title": "沙盒文件",
                        "subtitle": (
                            f"{result.get('file_count', 0)} 个文件"
                            f" · {result.get('dir_count', 0)} 个目录"
                            f" · 总计 {result.get('total_size', 0)} B"
                        ),
                        "root_label": "session sandbox",
                        "items": root_items,
                        "empty_text": "暂无文件。",
                    })
                else:
                    blocks.append({"type": "sandbox_file", "title": name, **result})
            elif name in {
                "sandbox_write_file", "sandbox_patch_file", "sandbox_read_file",
                "sandbox_stat_file", "sandbox_grep_file",
                "read_text_file", "stat_text_file", "patch_text_file", "write_text_file",
                "grep_text",
                "sandbox_delete_file", "sandbox_reset_files", "session_sandbox_status",
                "sandbox_open_file", "sandbox_reveal_in_folder",
            } and isinstance(result, dict):
                blocks.append({"type": "sandbox_file", "title": name, **result})
            elif name in {"discover_shell_environments", "discover_python_environments"} and isinstance(result, dict):
                blocks.append({"type": "sandbox_env", "title": name, **result})
            elif name in {
                "run_python_script", "run_shell_script", "run_python_snippet", "run_shell_snippet",
                "sandbox_pip_install",
                "run_host_python_script", "run_host_python_snippet",
            } and isinstance(result, dict):
                blocks.append({"type": "sandbox_run", "title": name, **result})
            elif name in {"artifact_add", "evidence_add"} and isinstance(result, dict):
                record_type = "evidence" if name == "evidence_add" else "artifact"
                record = result.get(record_type) if isinstance(result.get(record_type), dict) else {}
                blocks.append({
                    "type": "case_evidence",
                    "title": record.get("title") or name,
                    "ok": result.get("ok", False),
                    "error": result.get("error") or "",
                    "record_type": record_type,
                    "case_id": result.get("case_id") or "",
                    "ref_id": result.get(f"{record_type}_ref") or record.get("id") or "",
                    "kind": record.get("kind") or "other",
                    "confidence": record.get("confidence") or "",
                    "summary": record.get("summary") or "",
                    "path": record.get("path") or "",
                    "artifact_refs": record.get("artifact_refs") or [],
                })
            elif name.startswith("sqli_") or name.startswith("sqlmap_") or name.startswith("nuclei_") or name.startswith("ffuf_") or name.startswith("oob_") or name in {"nosql_injection_probe", "idor_compare_request", "role_matrix_probe", "business_state_probe", "upload_surface_discover", "upload_payload_matrix", "upload_verify_access", "ssrf_probe_url_param", "ssrf_redirect_chain_test", "ssrf_metadata_template", "cors_matrix_probe", "cors_exploit_verify", "csrf_poc_generate", "oauth_flow_analyze", "jwt_attack_surface", "saml_surface_analyze"}:
                if isinstance(result, dict):
                    blocks.append({"type": "vulnerability_evidence", "title": name, **result})
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name in {"http_desync_probe", "cache_poison_probe", "raw_http_send"}:
                if isinstance(result, dict):
                    blocks.append({"type": "request_replay", "title": name, **result})
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name.startswith("js_") or name.startswith("source_map") or name.startswith("webpack_") or name.startswith("captcha_") or name.startswith("anti_bot") or name.startswith("sandbox_") or name.startswith("api_") or name.startswith("business_") or name.startswith("graphql_") or name.startswith("websocket_") or name in {"json_path_extract", "querystring_parse_build", "header_cookie_parse", "wasm_inventory", "wasm_decompile_hint", "har_import_analyze", "grpc_web_analyze", "protobuf_field_preview", "create_poc_page", "open_poc_page"}:
                if isinstance(result, dict):
                    blocks.append({"type": "reverse_result", "title": name, **result})
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name in {"web_search", "github_search", "codec_transform", "hex_view", "hash_text", "hmac_text", "jwt_decode"} and isinstance(result, dict):
                blocks.append({"type": "tool_trace", **tr})
            elif name in {
                "ping_host", "dns_lookup", "trace_route", "http_curl", "whois_lookup", "nmap_scan",
                "network_discover_hosts", "network_scan_ports", "network_fingerprint_services", "network_profile_host",
            } and isinstance(result, dict):
                blocks.append({"type": "command_result", **result})
            elif name.startswith((
                "pcap_", "tshark_", "sca_", "syft_", "trivy_", "osv_",
                "wireless_", "wifi_", "wlan_", "firmware_", "iot_",
                "hardware_", "flash_", "cloud_", "prowler_", "kubectl_",
                "iac_", "checkov_", "appium_", "mobile_dast_", "ad_",
            )) and isinstance(result, dict):
                block_type = "command_result" if any(
                    key in result for key in ("command", "args", "argv", "exit_code", "stdout_excerpt")
                ) else "reverse_result"
                blocks.append({"type": block_type, "title": name, **result})
            elif name in {"export_task_report", "export_workspace_bundle"} and isinstance(result, dict):
                blocks.append({"type": "command_result", **result})
            elif name in {"channel_send_message", "channel_send_file"} and isinstance(result, dict):
                blocks.append({"type": "channel_delivery", "title": name, **result})
            elif name in {"set_todos", "add_todos", "update_todo", "complete_todo_with_refs", "create_validation_todo", "list_todos", "clear_todos"} and isinstance(result, dict):
                # list_todos is read-only. Re-emitting a settled snapshot in the
                # same turn as set_todos stacks the old 9/9 plan next to the new
                # pending plan on channels (GUI singleton hides it; channels
                # must not rely on that). Mutations still emit the live card.
                if name == "list_todos":
                    continue
                blocks.append({
                    "type": "todo_list",
                    "mode": "assessment_plan" if result.get("flat") else "default",
                    "todos": result.get("todos") or [],
                    "flat": result.get("flat") or [],
                    "current": result.get("current") or {},
                    "plan_pending": not bool(result.get("plan_approved", True)),
                })
                plan_confirmation = result.get("plan_confirmation")
                if isinstance(plan_confirmation, dict) and plan_confirmation.get("question"):
                    blocks.append({
                        "type": "user_decision",
                        "question": plan_confirmation.get("question") or "",
                        "options": list(plan_confirmation.get("options") or []),
                        "context": plan_confirmation.get("context") or "",
                        "decision_kind": plan_confirmation.get("decision_kind") or "todo_plan",
                    })
            elif name == "show_data_table" and isinstance(result, dict):
                block = result.get("block")
                if isinstance(block, dict) and block.get("type") == "data_table":
                    blocks.append(block)
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name == "show_structured_card" and isinstance(result, dict):
                if result.get("duplicate"):
                    # Idempotent re-call this turn — the card is already shown.
                    continue
                block = result.get("block")
                if isinstance(block, dict) and block.get("type") == "structured_card":
                    blocks.append(block)
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name == "show_file_resource_view" and isinstance(result, dict):
                block = result.get("block")
                if isinstance(block, dict) and block.get("type") == "file_resource_view":
                    blocks.append(block)
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name in {"kb_search", "search_memory", "search_all_memory", "skill_search", "codebase_search"}:
                # Search tools are model-facing data sources. The model decides
                # whether and how to present them via show_data_table.
                continue
            elif name == "stop_for_user_decision" and isinstance(result, dict):
                # Auto-approve / cron / child Agents resolve the tool without
                # waiting. Do not paint a pending card onto the final
                # transcript — that freezes the composer after the runtime
                # already continued.
                if result.get("auto_continued") or result.get("stopped") is False:
                    continue
                block = {
                    "type": "user_decision",
                    "question": result.get("question") or "",
                    "options": list(result.get("options") or []),
                    "context": result.get("context") or "",
                    "input_placeholder": result.get("input_placeholder") or "",
                    "input_required": bool(result.get("input_required")),
                    "recommended": result.get("recommended") or "",
                }
                sanitizer = getattr(self, "_sanitize_user_decision_block", None)
                blocks.append(sanitizer(block) if callable(sanitizer) else block)
            elif name == "stop_for_user_decision_batch" and isinstance(result, dict):
                if result.get("auto_continued") or result.get("stopped") is False:
                    continue
                blocks.append({
                    "type": "user_decision_batch",
                    "title": result.get("title") or "请确认以下选择",
                    "context": result.get("context") or "",
                    "questions": list(result.get("questions") or []),
                })
            elif name.startswith("user_todo_") and isinstance(result, dict) and result.get("ok"):
                hint = str(result.get("ui_hint") or "")
                if hint == "user_todo_list" or name == "user_todo_list":
                    blocks.append({
                        "type": "user_todo_list",
                        "items": result.get("items") or [],
                        "count": int(result.get("count") or 0),
                    })
                elif result.get("todo"):
                    blocks.append({
                        "type": "user_todo_card",
                        "todo": result.get("todo") or {},
                        "message": str(result.get("message") or ""),
                    })
                elif result.get("deleted"):
                    blocks.append({
                        "type": "user_todo_card",
                        "todo": result.get("deleted") or {},
                        "message": "已删除待办",
                    })
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name == "user_profile_apply_update" and isinstance(result, dict) and result.get("ok"):
                prof = result.get("profile") or {}
                blocks.append({
                    "type": "structured_card",
                    "title": "用户习惯已更新",
                    "variant": "success",
                    "sections": [{
                        "type": "kv",
                        "items": [
                            {"label": "说话风格", "value": prof.get("speaking_style") or "—"},
                            {"label": "任务方向", "value": prof.get("task_focus") or "—"},
                            {"label": "风险偏好", "value": prof.get("risk_preference") or "—"},
                        ],
                    }],
                })
            elif name.startswith("desktop_") and isinstance(result, dict):
                if name in {"desktop_action_session_start", "desktop_action_session_stop"}:
                    perms = result.get("macos_permissions") if isinstance(result, dict) else None
                    if isinstance(perms, dict) and perms.get("missing"):
                        blocks.append({
                            "type": "desktop_control",
                            "state": "idle",
                            "active": False,
                            "required": False,
                            "message": str(result.get("message") or "桌面输入不再需要限时授权。"),
                            "macos_permissions": perms,
                        })
                elif name == "desktop_observe":
                    vdesk = result.get("virtual_desktop") or {}
                    ob: dict[str, Any] = {
                        "type": "desktop_observe",
                        "title": "桌面观察",
                        "screenshot_path": result.get("screenshot_path") or "",
                        "sensitive_warning": result.get("sensitive_warning") or "",
                        "coordinate_hint": result.get("coordinate_hint") or "",
                        "monitor_count": vdesk.get("monitor_count") or len(result.get("monitors") or []),
                        "foreground": result.get("foreground"),
                        "window_count": len(result.get("windows") or []),
                    }
                    if result.get("macos_permissions"):
                        ob["macos_permissions"] = result["macos_permissions"]
                    if result.get("permission_notice"):
                        ob["permission_notice"] = result["permission_notice"]
                    if result.get("screenshot") and not (result.get("screenshot") or {}).get("ok"):
                        sc = result["screenshot"]
                        ob["screenshot_error"] = sc.get("error") or ""
                        if sc.get("macos_permission"):
                            ob["macos_permission"] = sc["macos_permission"]
                    blocks.append(ob)
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name in {"kb_save", "kb_update", "kb_supersede", "kb_delete"} and isinstance(result, dict):
                if result.get("ok"):
                    entry = result.get("entry") or {}
                    if name == "kb_save":
                        action = "saved"
                    elif name == "kb_update":
                        action = "updated"
                    elif name == "kb_supersede":
                        action = "superseded"
                    else:
                        action = "deleted"
                    blocks.append({
                        "type": "kb_updated",
                        "action": action,
                        "id": entry.get("id") or result.get("id") or result.get("new_id") or "",
                        "old_id": result.get("old_id") or "",
                        "new_id": result.get("new_id") or "",
                        "title": entry.get("title") or "",
                        "tags": entry.get("tags") or [],
                        "confidence": entry.get("confidence") or "",
                        "version": entry.get("version") or 1,
                        "applies_to": entry.get("applies_to") or [],
                        "trigger_terms": entry.get("trigger_terms") or [],
                        "reason": entry.get("reason") or result.get("reason") or "",
                        "body_preview": (entry.get("body") or "").strip()[:420],
                        "body_chars": len(entry.get("body") or ""),
                        "near_matches": result.get("near_matches") or [],
                        "notice": result.get("notice") or "",
                    })
                else:
                    blocks.append({"type": "tool_trace", **tr})
            elif name in _REVERSE_RESULT_TOOLS and isinstance(result, dict):
                blocks.append(_reverse_result_block(name, result))
            elif name in _PROXY_RESULT_TOOLS and isinstance(result, dict):
                blocks.append(_proxy_result_block(name, result))
            elif name in _API_INVENTORY_TOOLS and isinstance(result, dict):
                blocks.append(_api_inventory_block(name, result))
            elif name in _SECURITY_CARD_TOOLS and isinstance(result, dict):
                blocks.append(_security_card_block(name, result))
            else:
                blocks.append({"type": "tool_trace", **tr})
        # A turn can mutate the same assessment plan many times. Persist only
        # its latest projection; otherwise reloading the session renders one
        # full plan card per mutation and can fill the entire viewport. A
        # session likewise has at most one pending user decision.
        last_todo = max(
            (index for index, block in enumerate(blocks) if block.get("type") == "todo_list"),
            default=-1,
        )
        decision_types = {"user_decision", "user_decision_batch"}
        last_decision = max(
            (
                index
                for index, block in enumerate(blocks)
                if block.get("type") in decision_types
            ),
            default=-1,
        )
        return [
            block
            for index, block in enumerate(blocks)
            if not (
                (block.get("type") == "todo_list" and index != last_todo)
                or (
                    block.get("type") in decision_types
                    and index != last_decision
                )
            )
        ]


