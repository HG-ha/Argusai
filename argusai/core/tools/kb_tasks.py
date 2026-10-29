"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *


def channel_turn_session_allowlist(api: Any, kwargs: dict[str, Any]) -> frozenset[str] | None:
    """Sessions a channel-originated turn may search or read.

    Desktop / cron / confirmation turns stay unrestricted (``None``). A channel
    pairing may only see conversations granted to that identity.
    """
    session_id = str(kwargs.get("_session_id") or "").strip()
    session = kwargs.get("_session")
    if session is None and api is not None:
        session = getattr(api, "_assistant_sessions", {}).get(session_id)
    if str(getattr(session, "_current_turn_source", "") or "").lower() != "channel":
        return None
    allowed = {session_id} if session_id else set()
    identity_id = str(getattr(session, "_current_turn_source_ref", "") or "").strip()
    store_fn = getattr(api, "_channels_store", None)
    if callable(store_fn) and identity_id:
        try:
            identity = store_fn().get_identity(identity_id)
        except Exception:
            identity = None
        if identity is not None:
            allowed.update(identity.accessible_session_ids())
    return frozenset(sid for sid in allowed if sid)


def get_app_snapshot(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Return what the user is looking at plus global app state.

    Uses the live UI context pushed from the Vue shell (route, overlays) and
    enriches per-page data (MCP servers, task detail, knowledge, reverse, …).
    """
    from argusai.core.workspace.app_snapshot import build_app_snapshot

    return build_app_snapshot(api)


def search_all_memory(api: Any, query: str = "", limit: int = 10, **kwargs: Any) -> dict[str, Any]:
    """Search assistant sessions' long-term memory/history."""
    allowlist = channel_turn_session_allowlist(api, kwargs)
    return api.search_all_assistant_memory(
        query,
        _safe_int(limit, 10, 1, 50),
        session_ids=list(allowlist) if allowlist is not None else None,
    )


def read_assistant_session(
    api: Any,
    session_id: str,
    role: str = "assistant",
    order: str = "desc",
    offset: int = 0,
    limit: int = 10,
    max_chars: int = 24000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read visible messages from one assistant session when a summary is insufficient."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required", "messages": []}
    allowlist = channel_turn_session_allowlist(api, kwargs)
    if allowlist is not None and sid not in allowlist:
        return {
            "ok": False,
            "error": "session is not granted to this channel pairing",
            "session_id": sid,
            "messages": [],
        }
    role_filter = str(role or "assistant").strip().lower()
    if role_filter not in {"user", "assistant", "all"}:
        role_filter = "assistant"
    order_norm = str(order or "desc").strip().lower()
    if order_norm not in {"asc", "desc"}:
        order_norm = "desc"
    off = _safe_int(offset, 0, 0, 1_000_000)
    lim = _safe_int(limit, 10, 1, 50)
    char_budget = _safe_int(max_chars, 24000, 1000, 64000)

    payload = api.get_assistant_history(sid)
    if not isinstance(payload, dict) or not payload.get("ok"):
        return {
            "ok": False,
            "error": str((payload or {}).get("error") or "session could not be read"),
            "session_id": sid,
            "messages": [],
        }

    visible: list[tuple[int, dict[str, Any]]] = []
    for message_id, raw in enumerate(payload.get("history") or []):
        if not isinstance(raw, dict):
            continue
        message_role = str(raw.get("role") or "")
        if message_role not in {"user", "assistant"}:
            continue
        if raw.get("hidden") or raw.get("ui_hidden"):
            continue
        if role_filter != "all" and message_role != role_filter:
            continue
        visible.append((message_id, raw))
    if order_norm == "desc":
        visible.reverse()

    total = len(visible)
    selected = visible[off: off + lim]
    rows: list[dict[str, Any]] = []
    used_chars = 0
    content_budget_exhausted = False
    for message_id, raw in selected:
        remaining = char_budget - used_chars
        if remaining <= 0:
            content_budget_exhausted = True
            break
        content = str(raw.get("content") or "")
        truncated = len(content) > remaining
        if truncated:
            content = content[: max(0, remaining - 3)] + "..."
        used_chars += len(content)

        tool_names: list[str] = []
        for tool_call in raw.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            name = str((tool_call.get("function") or {}).get("name") or "").strip()
            if name:
                tool_names.append(name)
        for trace in raw.get("tool_trace") or []:
            if not isinstance(trace, dict):
                continue
            name = str(trace.get("name") or "").strip()
            if name and name not in tool_names:
                tool_names.append(name)

        row = {
            "message_id": message_id,
            "role": str(raw.get("role") or ""),
            "timestamp": float(raw.get("timestamp") or 0),
            "content": content,
            "truncated": truncated,
        }
        if tool_names:
            row["tool_names"] = tool_names
        if raw.get("attachments"):
            row["attachment_count"] = len(raw.get("attachments") or [])
        rows.append(row)
        if truncated:
            content_budget_exhausted = True
            break

    returned = len(rows)
    next_offset = off + returned
    return {
        "ok": True,
        "session_id": sid,
        "session_name": str(payload.get("name") or sid),
        "role": role_filter,
        "order": order_norm,
        "offset": off,
        "limit": lim,
        "max_chars": char_budget,
        "total_matching": total,
        "returned": returned,
        "next_offset": next_offset,
        "has_more": next_offset < total,
        "content_budget_exhausted": content_budget_exhausted,
        "messages": rows,
    }


# ---------------------------------------------------------------------------
# Knowledge base — persistent, cross-session AI memory the model itself
# curates. See ``argusai/core/knowledge.py`` for the data model.
# ---------------------------------------------------------------------------

def _kb(api: Any):
    kb = getattr(api, "kb", None)
    if kb is None:
        raise RuntimeError("Knowledge base is not initialised on this Api instance.")
    return kb


def _kb_session_id(api: Any) -> str:
    # ``Api`` stores active assistant sessions; we don't know which one is
    # calling here (the assistant tool dispatch doesn't pass session id) so
    # we leave it empty. The session id is only used for audit / changelog,
    # so an empty string is safe; ``kb_save_for_session`` covers the cases
    # where we DO know the session.
    return ""


def kb_search(api: Any, query: str = "", tags: list[str] | None = None, limit: int = 8,
              include_superseded: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Keyword search across the per-user knowledge base.

    Returns lightweight summaries (id, title, tags, applies_to, confidence,
    updated_at). Use ``kb_read`` to fetch full body when a hit looks
    relevant. ALWAYS run this before ``kb_save`` to avoid duplicates.
    """
    kb = _kb(api)
    results = kb.search(
        str(query or ""),
        tags=list(tags or []),
        limit=_safe_int(limit, 8, 1, 32),
        include_superseded=bool(include_superseded),
    )
    return {
        "query": str(query or ""),
        "tag_filter": list(tags or []),
        "count": len(results),
        "entries": results,
    }


def _truncate_text_field(
    obj: dict[str, Any] | None,
    field: str,
    max_chars: int = 32_768,
    offset: int = 0,
) -> dict[str, Any] | None:
    """Trim a single text field in-place. Adds ``<field>_offset``,
    ``<field>_truncated``, ``<field>_total_chars`` siblings so the
    model can paginate.
    """
    if not isinstance(obj, dict):
        return obj
    body = obj.get(field)
    if not isinstance(body, str):
        return obj
    total = len(body)
    start = max(0, int(offset or 0))
    if start > total:
        start = total
    end = min(total, start + max(0, int(max_chars)))
    obj[field] = body[start:end]
    obj[f"{field}_offset"] = start
    obj[f"{field}_total_chars"] = total
    obj[f"{field}_truncated"] = end < total
    obj[f"{field}_more"] = end < total
    return obj


def kb_read(
    api: Any,
    id: str = "",
    max_chars: int = 32_768,
    offset: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a knowledge entry. Default returns the first 32 KB of the
    ``body`` field; use ``offset`` + ``max_chars`` to paginate large
    entries, or call ``kb_search`` first to make sure you actually need
    the full body.
    """
    kb = _kb(api)
    entry = kb.get(str(id or "").strip())
    if entry is None:
        return {"ok": False, "error": "not_found", "id": str(id or "")}
    entry = dict(entry)
    _truncate_text_field(
        entry,
        "body",
        max_chars=_safe_int(max_chars, 32_768, 256, 200_000),
        offset=_safe_int(offset, 0, 0, 100_000_000),
    )
    return {"ok": True, "entry": entry}


def kb_save(api: Any, title: str = "", body: str = "", tags: list[str] | None = None,
            trigger_terms: list[str] | None = None, applies_to: list[str] | None = None,
            confidence: str = "probable", related: list[str] | None = None,
            task_id: str = "", supersedes: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
    """Distil a new piece of reusable, cross-session knowledge.

    BEFORE calling this, call ``kb_search`` with relevant tags/keywords to
    avoid creating a duplicate of an entry you (or a past session) already
    wrote. If you find a close match, prefer ``kb_update`` or ``kb_supersede``.

    The frontend renders a notification card immediately after this call
    so the user is aware their knowledge base was updated.
    """
    kb = _kb(api)
    clean_title = str(title or "").strip()
    clean_body = str(body or "").strip()
    if not clean_title:
        return {"ok": False, "error": "title is required"}
    if len(clean_body) < 24:
        # Tiny bodies are usually mistakes / placeholders. Refuse gently.
        return {"ok": False, "error": "body is too short (need >= 24 chars of actual content)"}
    # Soft dup-check: surface near-matches in the response so the model can
    # reconsider on next turn if needed. Does NOT block creation.
    near = kb.search(clean_title, tags=list(tags or []), limit=3)
    entry = kb.save(
        title=clean_title,
        body=clean_body,
        tags=list(tags or []),
        trigger_terms=list(trigger_terms or []),
        applies_to=list(applies_to or []),
        confidence=str(confidence or "probable"),
        related=list(related or []),
        task_id=str(task_id or ""),
        session_id=_kb_session_id(api),
        supersedes=str(supersedes or ""),
        reason=str(reason or "AI 主动沉淀"),
    )
    return {
        "ok": True,
        "entry": entry,
        "near_matches": [{"id": n["id"], "title": n["title"], "tags": n["tags"]} for n in near if n["id"] != entry["id"]],
        "notice": (
            "已写入知识库。若 near_matches 命中了你想表达的内容，下一轮可改用 "
            "kb_update 或 kb_supersede 合并。"
        ),
    }


def kb_update(api: Any, id: str = "", title: str | None = None, body: str | None = None,
              tags: list[str] | None = None, trigger_terms: list[str] | None = None,
              applies_to: list[str] | None = None, confidence: str | None = None,
              related: list[str] | None = None, task_id: str | None = None,
              reason: str = "", **kwargs: Any) -> dict[str, Any]:
    """Revise an existing knowledge entry IN-PLACE.

    Bumps ``version`` and appends a changelog row. Use this when you learn
    a refinement (typo, extra caveat, better payload) for an entry you
    already wrote. For structurally different rewrites, use
    ``kb_supersede`` instead so the old entry is preserved for reference.

    Pass only the fields you want to change. ``reason`` is required for
    auditability ("why are you changing this?").
    """
    if not str(reason or "").strip():
        return {"ok": False, "error": "reason is required (briefly: why are you updating?)"}
    kb = _kb(api)
    entry = kb.update(
        str(id or "").strip(),
        title=title,
        body=body,
        tags=tags,
        trigger_terms=trigger_terms,
        applies_to=applies_to,
        confidence=confidence,
        related=related,
        task_id=task_id,
        session_id=_kb_session_id(api),
        reason=str(reason),
    )
    if entry is None:
        return {"ok": False, "error": "not_found_or_trashed", "id": str(id or "")}
    return {"ok": True, "entry": entry}


def kb_supersede(api: Any, old_id: str = "", new_id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
    """Mark ``old_id`` as replaced by ``new_id``.

    Use when your new entry is a structural rewrite of an existing one
    (different scope, different conclusion). Both entries stay readable
    but ``kb_search`` only returns the new one by default.
    """
    if not old_id or not new_id:
        return {"ok": False, "error": "both old_id and new_id are required"}
    kb = _kb(api)
    ok = kb.supersede(str(old_id), str(new_id), reason=str(reason or ""))
    if not ok:
        return {"ok": False, "error": "one of the entries was not found"}
    return {"ok": True, "old_id": str(old_id), "new_id": str(new_id)}


def kb_delete(api: Any, id: str = "", reason: str = "", **kwargs: Any) -> dict[str, Any]:
    """Soft-delete an entry (moved to ``_trash`` and removed from search)."""
    if not str(reason or "").strip():
        return {"ok": False, "error": "reason is required (briefly: why are you deleting?)"}
    kb = _kb(api)
    ok = kb.delete(str(id or "").strip(), reason=str(reason))
    if not ok:
        return {"ok": False, "error": "not_found", "id": str(id or "")}
    return {"ok": True, "id": str(id), "trashed": True}


def kb_suggest_cleanup(api: Any, min_age_days: int = 30, max_results: int = 20, **kwargs: Any) -> dict[str, Any]:
    """Inspect the trash and propose old entries for permanent removal.

    Strictly **advisory** — this tool only reads from ``_trash/`` and
    returns a ranked list (oldest first) so the AI can ask the user whether
    to call the corresponding UI action. The AI has NO tool capable of
    actually hard-deleting trashed entries; that is intentionally
    reserved for the user via the Knowledge Base UI ("Empty trash" button)
    or by editing the trash directory directly.

    Use this when the user asks "is anything piling up?" or when you
    notice the trash has grown across sessions.
    """
    kb = _kb(api)
    candidates = kb.suggest_cleanup(
        max_results=_safe_int(max_results, 20, 1, 100),
        min_age_days=_safe_int(min_age_days, 30, 0, 3650),
    )
    return {
        "ok": True,
        "count": len(candidates),
        "min_age_days": int(min_age_days),
        "candidates": candidates,
        "notice": (
            "Advisory only — the AI cannot hard-delete trashed entries. "
            "Tell the user how many candidates exist and ask whether to "
            "open the Knowledge Base trash view and click \"Empty trash\"."
        ),
    }


def list_tasks(api: Any, **kwargs: Any) -> list[dict[str, Any]]:
    tasks = api.list_tasks()
    return [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "target_url": t.get("target_url"),
            "status": t.get("status"),
            "severity": t.get("severity", {}),
            "checks_done": t.get("checks_done"),
            "checks_total": t.get("checks_total"),
            "current_check": t.get("current_check"),
            "created_at": t.get("created_at"),
        }
        for t in tasks
    ]


def get_task_detail(api: Any, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
    task = api.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}
    meta = task.get("meta", {})
    findings = task.get("findings", [])
    sev: dict[str, int] = {}
    for f in findings:
        s = f.get("severity", "info")
        sev[s] = sev.get(s, 0) + 1
    reports = task.get("reports", {})
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "target_url": meta.get("target_url"),
        "status": meta.get("status"),
        "checks": meta.get("checks", []),
        "params": meta.get("params", {}),
        "severity_counts": sev,
        "total_findings": len(findings),
        "top_findings": [
            {"uid": f.get("uid"), "id": f.get("id"), "title": f.get("title"),
             "severity": f.get("severity"), "url": f.get("url"),
             "evidence": _summarize_evidence(f.get("evidence") or {})}
            for f in sorted(findings, key=lambda x: order.get(x.get("severity", "info"), 0), reverse=True)[:10]
        ],
        "event_count": task.get("event_count", 0),
        "is_running": task.get("is_running", False),
        "reports": {"html": reports.get("html"), "markdown": reports.get("md"), "json": reports.get("json")},
    }


def get_task_findings(api: Any, task_id: str = "", severity: str = "", limit: int = 50, **kwargs: Any) -> dict[str, Any]:
    task = api.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}
    limit_n = _safe_int(limit, 50, 1, 500)
    findings = task.get("findings", [])
    if severity:
        findings = [f for f in findings if f.get("severity") == severity]
    findings = findings[:limit_n]
    return {
        "task_id": task_id,
        "total": len(task.get("findings", [])),
        "filtered": len(findings),
        "findings": [
            {"uid": f.get("uid"), "id": f.get("id"), "title": f.get("title"),
             "severity": f.get("severity"), "url": f.get("url"),
             "description": f.get("description", "")[:300], "cwe": f.get("cwe"),
             "evidence": _summarize_evidence(f.get("evidence") or {})}
            for f in findings
        ],
    }


def get_finding_detail(api: Any, task_id: str = "", finding_uid: str = "", **kwargs: Any) -> dict[str, Any]:
    task = api.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}
    for f in task.get("findings", []):
        if f.get("uid") == finding_uid:
            return f
    return {"error": f"Finding not found: {finding_uid}"}


def export_task_report(
    api: Any,
    task_id: str = "",
    out_dir: str = "",
    formats: list[str] | str | None = None,
    overwrite: bool = False,
    create_zip: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Export a task's rendered reports to a user-selected directory."""
    from argusai.core.workspace.workspace_export import export_task_report as _export

    task_obj = api.tasks.get(task_id) if getattr(api, "tasks", None) is not None else None
    if not task_obj:
        return {"ok": False, "error": f"task not found: {task_id}"}
    try:
        target, root_kind, root_path = _resolve_session_output_path(api, kwargs, out_dir, default_name=f"reports-{task_id}")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if isinstance(formats, str):
        selected = [x.strip() for x in formats.replace(";", ",").split(",") if x.strip()]
    else:
        selected = [str(x).strip() for x in (formats or []) if str(x).strip()]
    result = _export(task_obj, target, selected or None, overwrite=bool(overwrite), create_zip=bool(create_zip))
    result["root_kind"] = root_kind
    result["root"] = root_path
    return result


def export_workspace_bundle(
    api: Any,
    out_dir: str = "",
    task_id: str = "",
    title: str = "security-deliverable",
    workflow_md: str = "",
    analysis_md: str = "",
    notes_md: str = "",
    include_reports: bool = True,
    include_sandbox: bool = True,
    include_readme: bool | None = None,
    include_manifest: bool = False,
    overwrite: bool = False,
    max_sandbox_files: int = 200,
    selection_mode: str = "auto",
    sandbox_paths: list[str] | str | None = None,
    create_zip: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Export reports, PoC code, reproduction notes and workspace files to a deliverable directory."""
    from pathlib import Path
    from argusai.core.workspace.workspace_export import default_export_dir, export_workspace_bundle as _export_bundle

    task_obj = api.tasks.get(task_id) if task_id and getattr(api, "tasks", None) is not None else None
    try:
        if out_dir and str(out_dir).strip():
            target, root_kind, root_path = _resolve_session_output_path(api, kwargs, out_dir)
        else:
            sandbox_root = Path(_session_sandbox_root(api, kwargs)).resolve()
            target = default_export_dir(sandbox_root / "exports", title)
            root_kind, root_path = "session_sandbox", str(sandbox_root)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    sandbox_root = Path(_session_sandbox_root(api, kwargs)).resolve()
    result = _export_bundle(
        out_dir=target,
        task=task_obj,
        sandbox_root=sandbox_root,
        workflow_md=str(workflow_md or ""),
        analysis_md=str(analysis_md or ""),
        notes_md=str(notes_md or ""),
        include_reports=bool(include_reports),
        include_sandbox=bool(include_sandbox),
        include_readme=(None if include_readme is None else bool(include_readme)),
        include_manifest=bool(include_manifest),
        overwrite=bool(overwrite),
        max_sandbox_files=_safe_int(max_sandbox_files, 200, 1, 1000),
        selection_mode=str(selection_mode or "auto"),
        sandbox_paths=sandbox_paths,
        create_zip=bool(create_zip),
    )
    result["task_id"] = task_id
    result["root_kind"] = root_kind
    result["root"] = root_path
    return result


def _summarize_evidence(evidence: dict[str, Any]) -> dict[str, str]:
    return {
        "payload": str(evidence.get("payload") or "")[:500],
        "signal": str(evidence.get("signal") or "")[:200],
        "template": str(evidence.get("template") or "")[:300],
        "snippet": str(evidence.get("snippet") or "")[:500],
    }


def get_task_events(api: Any, task_id: str = "", limit: int = 20, **kwargs: Any) -> dict[str, Any]:
    resp = api.get_task_events(task_id, 0)
    events = resp.get("events", [])
    limit_n = _safe_int(limit, 20, 1, 500)
    return {"status": resp.get("status"), "total": resp.get("total", 0), "events": events[-limit_n:]}


def wait_for_task(api: Any, task_id: str = "", timeout_seconds: int = 600, poll_interval: float = 1.5, **kwargs: Any) -> dict[str, Any]:
    """Wait for a task to finish, then return its final snapshot."""
    import time

    if not task_id:
        return {"error": "task_id is required"}
    timeout_n = _safe_int(timeout_seconds, 600, 1, 3600)
    deadline = time.monotonic() + timeout_n
    started_at = time.monotonic()
    last: dict[str, Any] | None = None
    completed = False
    while time.monotonic() < deadline:
        task = api.get_task(task_id)
        if not task:
            return {"error": f"Task not found: {task_id}"}
        last = task
        status = (task.get("meta") or {}).get("status")
        is_running = bool(task.get("is_running", False))
        if status in {"done", "cancelled", "error"} and not is_running:
            completed = True
            break
        time.sleep(max(0.25, float(poll_interval or 1.5)))

    if not last:
        return {"error": f"Task not found: {task_id}"}
    meta = last.get("meta") or {}
    findings = last.get("findings") or []
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    return {
        "id": meta.get("id") or task_id,
        "status": meta.get("status"),
        "completed": completed,
        "is_running": bool(last.get("is_running", False)),
        "progress": meta.get("progress", 0),
        "checks_done": meta.get("checks_done", 0),
        "checks_total": meta.get("checks_total", 0),
        "current_check": meta.get("current_check"),
        "severity_counts": meta.get("severity", {}),
        "total_findings": len(findings),
        "top_findings": [
            {
                "uid": f.get("uid"),
                "id": f.get("id"),
                "title": f.get("title"),
                "severity": f.get("severity"),
                "url": f.get("url"),
                "evidence": _summarize_evidence(f.get("evidence") or {}),
            }
            for f in sorted(findings, key=lambda x: order.get(x.get("severity", "info"), 0), reverse=True)[:10]
        ],
        "reports": last.get("reports") or {},
        "event_count": last.get("event_count", 0),
        "waited_seconds": round(time.monotonic() - started_at, 1),
        "timeout_seconds": timeout_n,
        "timed_out": not completed,
    }


def list_checks(api: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return api.list_checks()


def plan_pentest(api: Any, target_url: str = "", scope: str = "", intensity: str = "balanced", **kwargs: Any) -> dict[str, Any]:
    """Build an authorized penetration-test plan before execution."""
    checks_by_intensity = {
        "safe": ["recon", "headers", "tls", "secrets"],
        "balanced": ["recon", "js_reverse", "headers", "tls", "secrets", "injection", "xss"],
        "active": ["recon", "js_reverse", "headers", "tls", "secrets", "injection", "scanner", "xss", "fuzzer"],
    }
    checks = checks_by_intensity.get(intensity, checks_by_intensity["balanced"])
    scope_list = [s.strip() for s in scope.split(",") if s.strip()] if isinstance(scope, str) else list(scope or [])
    return {
        "target_url": target_url,
        "scope": scope_list,
        "intensity": intensity,
        "checks": checks,
        "risk": "high" if intensity == "active" else ("medium" if intensity == "balanced" else "low"),
        "requires_confirmation": True,
        "recommended_next_action": "create_scan",
    }


def get_proxy_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.get_proxy_status()


def get_proxy_traffic(
    api: Any,
    limit: int = 30,
    resource_kind: str = "",
    host: str = "",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return api.get_proxy_traffic(0, _safe_int(limit, 30, 1, 500), resource_kind or "", host or "")


# Enough of a body to reason about without flooding the context window; media
# stays out of the tool result entirely (the agent can save the body instead).
_PROXY_DETAIL_TEXT_LIMIT = 48 * 1024


def get_proxy_flow_detail(api: Any, flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    return api.get_proxy_flow_detail(
        _safe_int(flow_id, 0, 0, 10_000_000),
        _PROXY_DETAIL_TEXT_LIMIT,
        False,
    )


def get_proxy_ca_info(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.get_proxy_ca_info()


def install_proxy_ca(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.install_proxy_ca()


def uninstall_proxy_ca(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.uninstall_proxy_ca()


def get_proxy_findings(api: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return api.get_proxy_findings()


# Per decoded text returned to the model; the GUI keeps the full plaintext.
_PROXY_TOOL_TEXT_LIMIT = 8 * 1024


def proxy_search_traffic(api: Any, query: str = "", limit: int = 30, **kwargs: Any) -> dict[str, Any]:
    """Filter captured proxy traffic with ArgusAI's field query syntax (see skill proxy-traffic-query)."""
    result = api.proxy.query_traffic(str(query or ""), limit=_safe_int(limit, 30, 1, 200))
    if result.get("ok") and len(result.get("ids") or []) > 200:
        result["ids"] = result["ids"][-200:]
        result["ids_truncated"] = True
    return result


def _trim_decode_item(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in item.items() if k not in ("text", "ciphertext_b64", "hex_preview")}
    text = item.get("text")
    if isinstance(text, str):
        out["text"] = text[:_PROXY_TOOL_TEXT_LIMIT]
        out["text_truncated"] = len(text) > _PROXY_TOOL_TEXT_LIMIT
    if item.get("ciphertext_b64"):
        out["ciphertext_b64"] = str(item["ciphertext_b64"])[:2048]
    if item.get("hex_preview"):
        out["hex_preview"] = str(item["hex_preview"])[:512]
    return out


def proxy_decode_flow(api: Any, flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Auto-decoded layers, ciphertext hints and decryption-rule output for one proxy flow."""
    res = api.get_proxy_flow_decoded(_safe_int(flow_id, 0, 0, 10_000_000))
    if not res.get("ok"):
        return res
    decoded = res.get("decoded") or {}
    out: dict[str, Any] = {"ok": True, "flow_id": res.get("flow_id"), "evicted": res.get("evicted", False)}
    for part in ("request", "response"):
        entry = decoded.get(part) or {}
        out[part] = {
            "items": [_trim_decode_item(i) for i in entry.get("items") or []],
            "ciphertext": [_trim_decode_item(c) for c in entry.get("ciphertext") or []],
        }
    return out


def _coerce_decrypt_rule(rule: Any) -> dict[str, Any]:
    if isinstance(rule, str):
        import json

        try:
            rule = json.loads(rule)
        except json.JSONDecodeError:
            return {}
    return dict(rule) if isinstance(rule, dict) else {}


def _rule_has_script(rule: dict[str, Any]) -> bool:
    return any(str((s or {}).get("op") or "").lower() == "script" for s in rule.get("steps") or [] if isinstance(s, dict))


def _trim_rule_test(res: dict[str, Any]) -> dict[str, Any]:
    items = res.get("items")
    if isinstance(items, dict):
        res = dict(res)
        res["items"] = {part: [_trim_decode_item(i) for i in rows] for part, rows in items.items()}
    return res


def proxy_decrypt_rules_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List saved proxy decryption rules (key material masked) and rule load errors."""
    from argusai.core.security.proxy_decrypt import redact_rule

    res = api.get_proxy_decrypt_rules()
    return {"ok": True, "rules": [redact_rule(r) for r in res.get("rules") or []], "errors": res.get("errors") or {}}


def proxy_decrypt_rule_test(api: Any, rule: Any = None, flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Dry-run a decryption rule on one captured flow without saving it (no script steps)."""
    parsed = _coerce_decrypt_rule(rule)
    if _rule_has_script(parsed):
        return {
            "ok": False,
            "error": "script steps cannot run in proxy_decrypt_rule_test; save with "
                     "proxy_decrypt_script_rule_save (user-confirmed) and pass test_flow_id",
        }
    return _trim_rule_test(api.test_proxy_decrypt_rule(parsed, _safe_int(flow_id, 0, 0, 10_000_000)))


def _save_decrypt_rule(api: Any, rule: dict[str, Any], test_flow_id: int) -> dict[str, Any]:
    from argusai.core.security.proxy_decrypt import redact_rule

    res = api.save_proxy_decrypt_rule(rule)
    if not res.get("ok"):
        return res
    if test_flow_id:
        res["test"] = _trim_rule_test(api.test_proxy_decrypt_rule(res["rule"], test_flow_id))
    res["rule"] = redact_rule(res["rule"])
    return res


def proxy_decrypt_rule_save(api: Any, rule: Any = None, test_flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Create or update a proxy decryption rule (decode/sym/rsa/decompress/slice steps; keys stored in user settings)."""
    parsed = _coerce_decrypt_rule(rule)
    if _rule_has_script(parsed):
        return {"ok": False, "error": "rules with script steps must be saved via proxy_decrypt_script_rule_save"}
    return _save_decrypt_rule(api, parsed, _safe_int(test_flow_id, 0, 0, 10_000_000))


def proxy_decrypt_script_rule_save(api: Any, rule: Any = None, test_flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Create or update a proxy decryption rule that runs JS script steps in managed Node (always user-confirmed)."""
    return _save_decrypt_rule(api, _coerce_decrypt_rule(rule), _safe_int(test_flow_id, 0, 0, 10_000_000))


def proxy_decrypt_rule_delete(api: Any, rule_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a saved proxy decryption rule by id."""
    return api.delete_proxy_decrypt_rule(str(rule_id or ""))


def _coerce_rewrite_rule(rule: Any) -> dict[str, Any]:
    import json as _json

    if isinstance(rule, str):
        try:
            rule = _json.loads(rule or "{}")
        except Exception:
            return {}
    return rule if isinstance(rule, dict) else {}


def _trim_rewrite_test(result: dict[str, Any]) -> dict[str, Any]:
    for part in ("request", "response"):
        block = result.get(part)
        if isinstance(block, dict) and isinstance(block.get("body_preview"), str):
            block["body_preview"] = block["body_preview"][:_PROXY_TOOL_TEXT_LIMIT]
    return result


def proxy_rewrite_rules_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List saved active-rewrite rules (secret-looking header values masked) and load errors."""
    from argusai.core.security.proxy_rewrite import redact_rule

    res = api.get_proxy_rewrite_rules()
    return {
        "ok": True,
        "active": res.get("active", False),
        "rules": [redact_rule(r) for r in res.get("rules") or []],
        "errors": res.get("errors") or {},
    }


def proxy_rewrite_rule_test(api: Any, rule: Any = None, flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Dry-run an active-rewrite rule against one captured flow without touching live traffic."""
    return _trim_rewrite_test(api.test_proxy_rewrite_rule(_coerce_rewrite_rule(rule), _safe_int(flow_id, 0, 0, 10_000_000)))


def proxy_rewrite_rule_save(api: Any, rule: Any = None, test_flow_id: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Create or update an active-rewrite rule that MODIFIES LIVE TRAFFIC (always user-confirmed).

    Actions: set_header/remove_header/set_query/remove_query/set_method/replace_body/short_circuit
    on requests; set_header/remove_header/set_status/replace_body on responses. Match uses the
    proxy-traffic-query syntax. See skill proxy-rewrite-workflow.
    """
    from argusai.core.security.proxy_rewrite import redact_rule

    res = api.save_proxy_rewrite_rule(_coerce_rewrite_rule(rule))
    if not res.get("ok"):
        return res
    test_id = _safe_int(test_flow_id, 0, 0, 10_000_000)
    if test_id:
        res["test"] = _trim_rewrite_test(api.test_proxy_rewrite_rule(res["rule"], test_id))
    res["rule"] = redact_rule(res["rule"])
    return res


def proxy_rewrite_rule_delete(api: Any, rule_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a saved active-rewrite rule by id."""
    return api.delete_proxy_rewrite_rule(str(rule_id or ""))


def proxy_breakpoints_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List requests currently parked at an interactive breakpoint (awaiting edit/forward/drop).

    Each entry has id, method, url, host, headers and body_text. Resolve one with
    proxy_breakpoint_resolve. Parked requests auto-forward after ~2 minutes.
    """
    return api.list_proxy_breakpoints()


def proxy_breakpoint_resolve(
    api: Any, pause_id: str = "", action: str = "forward", edits: Any = None, **kwargs: Any
) -> dict[str, Any]:
    """Resolve a parked request: forward it (optionally edited) or drop it.

    ``action`` = "forward" | "drop". ``edits`` (optional, forward only) may set
    ``method``, ``url``, ``headers`` ([[name, value], …]) and ``body_text``; absent
    fields keep the original. This acts on one live, in-flight request.
    """
    payload = edits if isinstance(edits, dict) else None
    return api.resolve_proxy_breakpoint(str(pause_id or ""), str(action or "forward"), payload)


def proxy_breakpoint_config(
    api: Any,
    enabled: bool = True,
    url_contains: str = "",
    host_contains: str = "",
    methods: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Enable/disable interactive request breakpoints and set which requests to catch.

    Matching requests are PARKED until resolved (so turning this on will hold live
    traffic). Narrow it with url_contains / host_contains / methods to avoid parking
    everything. Pass enabled=false to turn it off.
    """
    method_list = methods if isinstance(methods, list) else ([methods] if methods else [])
    return api.set_proxy_breakpoint_config({
        "enabled": bool(enabled),
        "url_contains": str(url_contains or ""),
        "host_contains": str(host_contains or ""),
        "methods": [str(m) for m in method_list if str(m).strip()],
    })


def export_proxy_har(
    api: Any,
    since: int = 0,
    limit: int = 500,
    path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    return api.export_proxy_har(_safe_int(since, 0, 0, 10_000_000), _safe_int(limit, 500, 1, 5000), path or "")


def save_proxy_flow_body(api: Any, flow_id: int = 0, part: str = "response", **kwargs: Any) -> dict[str, Any]:
    return api.save_proxy_flow_body(_safe_int(flow_id, 0, 0, 10_000_000), part or "response")


def open_proxy_exports_dir(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.open_proxy_exports_dir()


def open_proxy_captures_dir(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.open_proxy_captures_dir()


def team_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List standing (global, cross-session) Agent Teams and their live status.

    Each team has team_id, name, objective, status summary, and members_preview.
    Use team_get(team_id) for the full member roster, or team_add_member to grow one.
    """
    return api.list_teams()


def team_get(api: Any, team_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Read one standing Agent Team with its full member roster and statuses."""
    return api.get_team(str(team_id or ""))


def team_chat_get(api: Any, team_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Read the merged chronological group chat for every member in one standing team."""
    return api.get_team_chat(str(team_id or ""))


def team_message_send(
    api: Any,
    team_id: str = "",
    message: str = "",
    agent_ids: list[str] | None = None,
    visibility: str = "group",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send one group-chat command to all team members or selected agent_ids."""
    return api.send_team_message(
        str(team_id or ""),
        str(message or ""),
        agent_ids if isinstance(agent_ids, list) else None,
        str(visibility or "group"),
    )


def team_create(
    api: Any, name: str = "", objective: str = "", preset: str = "", **kwargs: Any
) -> dict[str, Any]:
    """Create a standing Agent Team (persists across chats; shown in the Team menu).

    Optional preset='web_audit'|'recon_sweep'|'reverse_triage' auto-spawns the
    built-in squad when objective is provided. Returns the new team with its team_id.
    """
    return api.create_team(
        {"name": str(name or ""), "objective": str(objective or ""), "preset": str(preset or "")}
    )


def team_update(
    api: Any, team_id: str = "", name: str = "", objective: str = "", **kwargs: Any
) -> dict[str, Any]:
    """Rename a standing Agent Team or update its objective."""
    patch: dict[str, Any] = {}
    if str(name or "").strip():
        patch["name"] = str(name).strip()
    if objective is not None and str(objective) != "":
        patch["objective"] = str(objective)
    return api.update_team(str(team_id or ""), patch)


def team_delete(api: Any, team_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Permanently delete a standing Agent Team and stop all its member Agents."""
    return api.delete_team(str(team_id or ""))


def team_add_member(
    api: Any,
    team_id: str = "",
    role: str = "",
    objective: str = "",
    context: str = "",
    expected_output: str = "",
    permission: str = "",
    requested_tools: list[str] | None = None,
    model: str | None = None,
    approval_mode: str = "leader_auto",
    allow_web_search: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Recruit one member into a standing team. The member waits for the first command.

    permission='readonly' (safe default) or 'scoped' (then requested_tools is the
    exact tool whitelist). Members are leaves — they cannot recruit further Agents.
    """
    return api.add_team_member(
        str(team_id or ""),
        {
            "role": str(role or ""),
            "objective": str(objective or ""),
            "context": str(context or ""),
            "expected_output": str(expected_output or ""),
            "permission": str(permission or "readonly"),
            "requested_tools": requested_tools if isinstance(requested_tools, list) else [],
            "model": str(model or ""),
            "approval_mode": str(approval_mode or "leader_auto"),
            "allow_web_search": bool(allow_web_search),
        },
    )


def team_member_get(
    api: Any, team_id: str = "", agent_id: str = "", **kwargs: Any
) -> dict[str, Any]:
    """Read one standing-team member's status, summary, events, and conversation history."""
    return api.get_team_member(str(team_id or ""), str(agent_id or ""))


def team_member_update(
    api: Any,
    team_id: str = "",
    agent_id: str = "",
    role: str = "",
    objective: str = "",
    permission: str = "",
    requested_tools: list[str] | None = None,
    model: str | None = None,
    approval_mode: str = "",
    allow_web_search: bool | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Edit one standing-team member's assignment, capability projection, and model."""
    patch: dict[str, Any] = {}
    if str(role or "").strip():
        patch["role"] = str(role).strip()
    if str(objective or "").strip():
        patch["objective"] = str(objective).strip()
    if str(permission or "").strip():
        patch["permission"] = str(permission).strip()
        patch["requested_tools"] = requested_tools if isinstance(requested_tools, list) else []
    if model is not None:
        patch["model"] = str(model)
    if str(approval_mode or "").strip():
        patch["approval_mode"] = str(approval_mode)
    if allow_web_search is not None:
        patch["allow_web_search"] = bool(allow_web_search)
    return api.update_team_member(str(team_id or ""), str(agent_id or ""), patch)


def team_member_send_input(
    api: Any,
    team_id: str = "",
    agent_id: str = "",
    message: str = "",
    resume: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Command or steer a standing-team member Agent.

    If the member is running/queued, injects ``message`` at its next safe boundary;
    if paused/completed/failed and ``resume=True``, wakes and resumes the member
    with ``message`` as its new instruction.
    """
    tid = str(team_id or "")
    aid = str(agent_id or "")
    msg = str(message or "").strip()
    if not msg:
        return {"ok": False, "error": "message is required"}
    info = api.get_team_member(tid, aid)
    status = str(info.get("status") or "") if isinstance(info, dict) else ""
    if status in {"running", "queued"}:
        return api.send_team_member_input(tid, aid, msg)
    if resume:
        return api.resume_team_member(tid, aid, msg)
    return api.send_team_member_input(tid, aid, msg)


def team_member_control(
    api: Any,
    team_id: str = "",
    agent_id: str = "",
    action: str = "pause",
    message: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Manage a standing-team member's lifecycle: action='pause'|'resume'|'close'|'delete'."""
    tid = str(team_id or "")
    aid = str(agent_id or "")
    act = str(action or "pause").strip().lower()
    if act == "pause":
        return api.pause_team_member(tid, aid)
    if act == "resume":
        return api.resume_team_member(tid, aid, str(message or "继续"))
    if act in {"close", "stop"}:
        return api.close_team_member(tid, aid)
    if act in {"delete", "remove"}:
        return api.delete_team_member(tid, aid)
    return {"ok": False, "error": f"unknown action: {act} (expected pause|resume|close|delete)"}


def get_settings(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.get_settings()


# === Action executors ===

def create_scan(api: Any, url: str = "", name: str = "", scope: str | list[str] = "",
                paths: str = "", checks: list[str] | None = None,
                cdp_url: str = "", **kwargs: Any) -> dict[str, Any]:
    """Create a scan task. AI-initiated scans always run headless; the
    "observe browser" / visualization options are intentionally not exposed
    to the model and can only be enabled via the New Scan form in the UI.
    """
    if not url:
        return {"error": "url is required"}
    check_list = list(checks or [])
    params: dict[str, Any] = {
        "url": url.strip(),
        "name": name.strip() if name else "",
        "headless": True,
    }
    if scope:
        params["scope"] = [s.strip() for s in scope.split(",") if s.strip()] if isinstance(scope, str) else list(scope)
    if paths:
        params["paths"] = [p.strip() for p in paths.split("\n") if p.strip()]
    if check_list:
        params["checks"] = check_list
    if cdp_url:
        params["cdp_url"] = cdp_url.strip()
    return api.create_task(params)


def cancel_task(api: Any, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
    return api.cancel_task(task_id)


def delete_task(api: Any, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
    return api.delete_task(task_id)


def rescan_task(api: Any, task_id: str = "", **kwargs: Any) -> dict[str, Any]:
    return api.rescan_task(task_id)


def start_proxy(
    api: Any,
    host: str = "127.0.0.1",
    port: int = 8866,
    scope: str = "",
    deny_hosts: str = "",
    intercept_mode: str = "all",
    mime_allow: str = "",
    use_system_proxy: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "host": host,
        "port": port,
        "use_system_proxy": bool(use_system_proxy),
        "intercept_mode": (intercept_mode or "all").strip().lower(),
    }
    if scope:
        params["scope"] = [s.strip() for s in scope.split(",") if s.strip()]
    if deny_hosts:
        params["deny_hosts"] = [s.strip() for s in deny_hosts.split(",") if s.strip()]
    if mime_allow:
        params["mime_allow"] = [s.strip() for s in mime_allow.split(",") if s.strip()]
    return api.start_proxy(params)


def stop_proxy(api: Any, **kwargs: Any) -> dict[str, Any]:
    return api.stop_proxy()


def update_settings(api: Any, ai_enabled: bool | None = None, proxy_host: str | None = None,
                    proxy_port: int | None = None, proxy_scope: str | None = None,
                    proxy_deny_hosts: str | None = None, proxy_intercept_mode: str | None = None,
                    proxy_mime_allow: str | None = None,
                    **kwargs: Any) -> dict[str, Any]:
    current = api.get_settings()
    ai = current.get("ai", {})
    proxy = current.get("proxy", {})
    if ai_enabled is not None:
        ai["enabled"] = ai_enabled
    if proxy_host is not None:
        proxy["host"] = proxy_host
    if proxy_port is not None:
        proxy["port"] = proxy_port
    if proxy_scope is not None:
        proxy["scope"] = [s.strip() for s in proxy_scope.split(",") if s.strip()]
    if proxy_deny_hosts is not None:
        proxy["deny_hosts"] = [s.strip() for s in proxy_deny_hosts.split(",") if s.strip()]
    if proxy_intercept_mode is not None:
        proxy["intercept_mode"] = (proxy_intercept_mode or "all").strip().lower()
    if proxy_mime_allow is not None:
        proxy["mime_allow"] = [s.strip() for s in proxy_mime_allow.split(",") if s.strip()]
    return api.save_settings({"ai": ai, "proxy": proxy})


def test_ai_connection(api: Any, **kwargs: Any) -> dict[str, Any]:
    settings = api.get_settings()
    endpoints = [ep for ep in settings.get("ai", {}).get("endpoints", []) if ep.get("enabled", True)]
    if not endpoints:
        return {"error": "No enabled AI endpoints configured."}
    results = []
    for ep in endpoints:
        result = api.test_ai_endpoint(ep)
        results.append({"name": ep.get("name", ""), "ok": result.get("ok", False), "error": result.get("error", "")})
    return {"results": results, "all_ok": all(r["ok"] for r in results)}


def launch_chrome(api: Any, port: int = 9222, **kwargs: Any) -> dict[str, Any]:
    return api.launch_chrome({"port": port})


# === Temporary probes and network tools ===

