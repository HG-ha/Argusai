"""Sync readonly sub-agent runner (Loom22 §22).

A sub-agent is a one-shot, depth-capped Agent loop with an independent
history. Only the distilled conclusion returns to the parent context —
never the child transcript.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .streaming import _CONFIRM_TOOLS, _WRITE_TOOLS, _assistant_debug_log, tool_kind
from .tools import ToolRegistry

log = logging.getLogger("argusai.assistant.subagent")

_MAX_SUMMARY_CHARS = 4000
_MAX_CONCURRENT_SUBAGENTS_PER_SESSION = 12
_SUBAGENT_TIMEOUT_SECONDS = 1800
_AI_PROVIDER_UNSET = object()
_AGENT_CONTROL_TOOLS = {
    "run_subagent",
    "spawn_agent",
    "spawn_external_agent",
    "send_agent_input",
    "pause_agent",
    "resume_agent",
    "wait_for_agents",
    "list_agents",
    "get_agent",
    "close_agent",
}


def _create_isolated_ai_provider(parent: Any, model: str = "") -> Any:
    """Build an event-loop-local provider for an async child Agent."""
    if getattr(parent, "ai", None) is None:
        return None
    registry = getattr(parent, "registry", None)
    api = getattr(registry, "api", None)
    settings_mgr = getattr(api, "settings_mgr", None)
    if settings_mgr is None:
        raise RuntimeError("async sub-agent requires access to current AI endpoint settings")
    settings = settings_mgr.get()
    endpoints = list(getattr(settings, "ai_endpoints", None) or [])
    if not bool(getattr(settings, "ai_enabled", False)) or not endpoints:
        raise RuntimeError("async sub-agent cannot create an isolated AI provider from current settings")
    requested_raw = str(model or "").strip()
    requested_model = requested_raw.lower()
    if "::" in requested_raw:
        from dataclasses import replace

        endpoint_name, exact_model = requested_raw.split("::", 1)
        endpoint_name = endpoint_name.strip().lower()
        exact_model = exact_model.strip()
        selected = [
            endpoint
            for endpoint in endpoints
            if str(getattr(endpoint, "name", "") or "").strip().lower() == endpoint_name
        ]
        if not selected or not exact_model:
            raise RuntimeError(f"requested sub-agent endpoint/model is not configured: {model}")
        endpoints = [replace(endpoint, model=exact_model) for endpoint in selected]
    elif requested_model:
        endpoints = [
            endpoint
            for endpoint in endpoints
            if requested_model
            in {
                str(getattr(endpoint, "model", "") or "").strip().lower(),
                str(getattr(endpoint, "name", "") or "").strip().lower(),
            }
        ]
        if not endpoints:
            raise RuntimeError(f"requested sub-agent model is not configured: {model}")
    from argusai.core.ai import create_provider

    return create_provider(endpoints=endpoints)


def _readonly_tool_names(registry: ToolRegistry) -> list[str]:
    names: list[str] = []
    for tool in registry._tools.values():
        if tool.name in _AGENT_CONTROL_TOOLS:
            continue
        # A child has no confirmation UI. Excluding every confirmation-gated
        # tool prevents an unattended readonly delegation from getting stuck
        # in awaiting_confirmation.
        if tool.name in _CONFIRM_TOOLS:
            continue
        kind = str(tool.kind or tool_kind(tool.name))
        if kind == "mutating" or tool.name in _WRITE_TOOLS:
            continue
        if tool.risk in {"high", "critical"} and kind != "idempotent":
            # Keep low-risk reads; skip residual high-risk non-reads.
            if kind not in {"idempotent", "polling", "presentation", "control"}:
                continue
        names.append(tool.name)
    return sorted(names)


def _build_child_registry(parent_registry: ToolRegistry, allowed: set[str]) -> ToolRegistry:
    child = ToolRegistry(api=getattr(parent_registry, "api", None), session_id=getattr(parent_registry, "session_id", None))
    for name, tool in parent_registry._tools.items():
        if name in allowed:
            child._tools[name] = tool
    return child


def _distill_summary(text: str, tool_calls: list[dict[str, Any]]) -> str:
    cleaned = (text or "").strip()
    if cleaned:
        return cleaned[:_MAX_SUMMARY_CHARS]
    if tool_calls:
        last = tool_calls[-1]
        result = last.get("result")
        try:
            blob = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            blob = str(result)
        return f"(no final text; last tool {last.get('name')}: {blob[:800]})"
    return "(sub-agent produced no summary)"


async def run_subagent(
    parent: Any,
    *,
    role: str,
    objective: str,
    context: str = "",
    expected_output: str = "",
    permission: str = "readonly",
    mode: str = "sync",
    scope_ref: str = "",
    requested_capabilities: list[str] | None = None,
    _ai_provider: Any = _AI_PROVIDER_UNSET,
    _runtime_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute ``run_subagent`` for a parent AssistantSession."""
    role_s = str(role or "").strip() or "analyst"
    objective_s = str(objective or "").strip()
    if not objective_s:
        return {"ok": False, "error": "objective is required", "stopped_reason": "denied"}

    perm = str(permission or "readonly").strip().lower() or "readonly"
    if perm not in {"readonly", "scoped"}:
        return {"ok": False, "error": f"unsupported permission: {permission}", "stopped_reason": "denied"}
    if perm == "scoped":
        # Phase 5 ships readonly first; scoped needs server-issued scope_ref.
        if not str(scope_ref or "").strip():
            return {
                "ok": False,
                "error": "permission=scoped requires scope_ref (not yet enabled for this build; use readonly)",
                "stopped_reason": "denied",
            }
        return {
            "ok": False,
            "error": "permission=scoped is reserved; use permission=readonly for sync sub-agents",
            "stopped_reason": "denied",
        }

    mode_s = str(mode or "sync").strip().lower() or "sync"
    depth = int(getattr(parent, "_subagent_depth", 0) or 0)
    if depth >= 1:
        return {
            "ok": False,
            "error": "sub-agent nesting denied (depth hard-capped at 1)",
            "stopped_reason": "denied",
            "depth": depth,
        }

    if mode_s == "async":
        from .background_jobs import BackgroundJobStore
        import asyncio

        if getattr(parent, "_background_jobs", None) is None:
            parent._background_jobs = BackgroundJobStore()
        running = parent._background_jobs.running_count(name="run_subagent")
        if running >= _MAX_CONCURRENT_SUBAGENTS_PER_SESSION:
            return {
                "ok": False,
                "error": "sub-agent concurrency limit reached for this session",
                "error_code": "subagent_concurrency_limit",
                "running": running,
                "max_concurrent": _MAX_CONCURRENT_SUBAGENTS_PER_SESSION,
                "stopped_reason": "denied",
            }
        args = {
            "role": role_s,
            "objective": objective_s,
            "context": str(context or ""),
            "expected_output": str(expected_output or ""),
            "permission": "readonly",
            "mode": "sync",
            "scope_ref": str(scope_ref or ""),
            "requested_capabilities": list(requested_capabilities or []),
        }
        runtime_control: dict[str, Any] = {
            "cancel_event": threading.Event(),
            "child": None,
            "loop": None,
            "task": None,
        }

        def _runner() -> Any:
            async def _run_isolated() -> dict[str, Any]:
                try:
                    runtime_control["loop"] = asyncio.get_running_loop()
                    runtime_control["task"] = asyncio.current_task()
                    if runtime_control["cancel_event"].is_set():
                        return {
                            "ok": False,
                            "status": "cancelled",
                            "stopped_reason": "cancelled",
                        }
                    provider = _create_isolated_ai_provider(parent)
                    try:
                        return await run_subagent(
                            parent,
                            **args,
                            _ai_provider=provider,
                            _runtime_control=runtime_control,
                        )
                    finally:
                        if provider is not None:
                            try:
                                await provider.close()
                            except Exception:
                                log.debug("isolated sub-agent provider close failed", exc_info=True)
                except asyncio.CancelledError:
                    return {
                        "ok": False,
                        "status": "cancelled",
                        "stopped_reason": "cancelled",
                    }
                finally:
                    runtime_control["task"] = None
                    runtime_control["loop"] = None

            return asyncio.run(_run_isolated())

        def _cancel() -> None:
            runtime_control["cancel_event"].set()
            child = runtime_control.get("child")
            if child is not None:
                child.cancel()
            loop = runtime_control.get("loop")
            task = runtime_control.get("task")
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

        receipt = parent._background_jobs.start(
            name="run_subagent",
            arguments=args,
            runner=_runner,
            turn_id=str(getattr(parent, "_current_turn_id", "") or ""),
            cancel=_cancel,
        )
        receipt["role"] = role_s
        receipt["mode"] = "async"
        return receipt

    if mode_s != "sync":
        return {"ok": False, "error": f"unsupported mode: {mode}", "stopped_reason": "denied"}

    from .session import AssistantSession

    allowed = set(_readonly_tool_names(parent.registry))
    child_reg = _build_child_registry(parent.registry, allowed)
    child_ai = parent.ai if _ai_provider is _AI_PROVIDER_UNSET else _ai_provider
    child = AssistantSession(registry=child_reg, ai_provider=child_ai, kb=getattr(parent, "kb", None))
    child._subagent_depth = depth + 1
    child._turn_deadline_override_seconds = _SUBAGENT_TIMEOUT_SECONDS
    if _runtime_control is not None:
        _runtime_control["child"] = child
        cancel_event = _runtime_control.get("cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            child.cancel()
    # Child must never re-offer delegation. AssistantSession re-registers
    # control tools on construction, so strip the whole set — not just
    # run_subagent — or a sync child can spawn_agent and nest.
    for name in _AGENT_CONTROL_TOOLS:
        child.registry._tools.pop(name, None)
    child._seed_unattended_child_auto_approve()
    child._auto_approve_tools = {
        name
        for name in (
            set(getattr(child, "_auto_approve_tools", set()) or set())
            | set(getattr(parent, "_auto_approve_tools", set()) or set())
        )
        if name in child.registry._tools
    }
    child._auto_approve_permissions = (
        set(getattr(child, "_auto_approve_permissions", set()) or set())
        | set(getattr(parent, "_auto_approve_permissions", set()) or set())
    )
    child.allow_web_search = bool(getattr(parent, "allow_web_search", True))
    declaration = getattr(parent, "engagement_declaration", None)
    child.engagement_declaration = dict(declaration) if isinstance(declaration, dict) else None
    child.current_case_id = str(getattr(parent, "current_case_id", "") or "")

    prompt = (
        f"[sub-agent role={role_s}]\n"
        f"Objective (single, judgeable): {objective_s}\n"
    )
    if context:
        prompt += f"Minimal context:\n{context}\n"
    if expected_output:
        prompt += f"Expected output shape: {expected_output}\n"
    prompt += (
        "Rules: use only the provided readonly tools; do not ask the user questions; "
        "when the objective is met, reply with a concise final answer and stop. "
        "Do not dump full tool transcripts."
    )

    _assistant_debug_log(
        f"[agent-debug] event=subagent_start role={role_s!r} depth={depth + 1} "
        f"tools={len(allowed)} mode=sync"
    )
    t0 = time.time()
    try:
        # hide_user_message keeps the parent UI clean; child history is local.
        response = await child.chat(
            prompt,
            hide_user_message=True,
            thinking_enabled=False,
        )
    except Exception as exc:
        log.exception("sub-agent failed")
        return {
            "ok": False,
            "role": role_s,
            "summary": "",
            "error": str(exc),
            "stopped_reason": "error",
            "rounds_used": int(getattr(child, "_current_round_index", 0) or 0),
        }

    text = ""
    tool_calls: list[dict[str, Any]] = []
    status = "done"
    if hasattr(response, "text"):
        text = str(response.text or "")
        tool_calls = list(getattr(response, "tool_calls", None) or getattr(response, "tool_trace", None) or [])
        status = str(getattr(response, "status", "done") or "done")
    elif isinstance(response, dict):
        text = str(response.get("text") or response.get("content") or "")
        tool_calls = list(response.get("tool_calls") or response.get("tool_trace") or [])
        status = str(response.get("status") or "done")

    summary = _distill_summary(text, tool_calls)
    artifacts: list[str] = []
    findings: list[dict[str, Any]] = []
    for tc in tool_calls:
        result = tc.get("result") if isinstance(tc, dict) else None
        if not isinstance(result, dict):
            continue
        for key in ("artifact_ref", "artifact_id"):
            ref = result.get(key)
            if ref:
                artifacts.append(str(ref))
        for key in ("artifacts", "artifact_refs"):
            vals = result.get(key)
            if isinstance(vals, list):
                artifacts.extend(str(v) for v in vals if v)
        if isinstance(result.get("findings"), list):
            for item in result["findings"]:
                if isinstance(item, dict):
                    findings.append(item)

    # Deduplicate refs while preserving order.
    seen: set[str] = set()
    uniq_artifacts: list[str] = []
    for a in artifacts:
        if a not in seen:
            seen.add(a)
            uniq_artifacts.append(a)

    stopped = "objective_met" if status == "done" else status
    out = {
        "ok": True,
        "role": role_s,
        "summary": summary,
        "artifacts": uniq_artifacts[:40],
        "findings": findings[:40],
        "rounds_used": int(getattr(child, "_current_round_index", 0) or 0),
        "stopped_reason": stopped,
        "elapsed_ms": int((time.time() - t0) * 1000),
        # Explicitly not including child.history / transcript.
    }
    _assistant_debug_log(
        f"[agent-debug] event=subagent_end role={role_s!r} stopped={stopped} "
        f"rounds={out['rounds_used']} summary_chars={len(summary)}"
    )
    return out
