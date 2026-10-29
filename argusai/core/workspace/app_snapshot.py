"""Build rich application / screen snapshots for the AI assistant."""

from __future__ import annotations

import time
from typing import Any

ROUTE_LABELS: dict[str, str] = {
    "tasks": "任务列表",
    "new": "新建扫描",
    "task": "任务详情",
    "proxy": "被动代理",
    "mcp": "MCP 服务",
    "assistant": "AI 助手（全屏路由）",
    "user_todos": "个人待办",
    "knowledge": "知识库",
    "knowledge_entry": "知识库条目",
    "reverse": "JS 逆向工作台",
    "reverse_job": "逆向任务详情",
    "settings": "设置",
}


def _ui_from_api(api: Any) -> dict[str, Any]:
    if hasattr(api, "get_ui_context"):
        return api.get_ui_context() or {}
    return {}


def _route_name(ui: dict[str, Any]) -> str:
    return str(ui.get("route") or "").strip()


def _page_label(route: str, ui: dict[str, Any]) -> str:
    return str(ui.get("page_label") or ROUTE_LABELS.get(route) or route or "未知页面")


def _theme_from_ui(ui: dict[str, Any]) -> dict[str, str]:
    client = ui.get("client") if isinstance(ui.get("client"), dict) else {}
    raw = client.get("theme") if isinstance(client.get("theme"), dict) else {}
    preferred = str(raw.get("preferred") or "").strip()
    effective = str(raw.get("effective") or raw.get("color_scheme") or "").strip()
    out: dict[str, str] = {}
    if preferred in {"auto", "light", "dark"}:
        out["preferred"] = preferred
    if effective in {"light", "dark"}:
        out["effective"] = effective
        out["label"] = "深色" if effective == "dark" else "浅色"
    return out


def _summarize_mcp_servers(raw: dict[str, Any]) -> dict[str, Any]:
    servers = raw.get("servers") if isinstance(raw.get("servers"), list) else []
    rows = []
    for s in servers[:20]:
        if not isinstance(s, dict):
            continue
        probe = s.get("last_probe") if isinstance(s.get("last_probe"), dict) else {}
        rows.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "enabled": s.get("enabled"),
            "transport": s.get("transport"),
            "status": probe.get("status") or s.get("status"),
            "error": (probe.get("error") or s.get("error") or "")[:200],
        })
    return {
        "count": len(servers),
        "enabled_count": sum(1 for s in servers if isinstance(s, dict) and s.get("enabled")),
        "servers": rows,
    }


def _summarize_kb_list(raw: dict[str, Any], *, limit: int = 8) -> dict[str, Any]:
    entries = raw.get("entries") if isinstance(raw.get("entries"), list) else []
    return {
        "count": len(entries),
        "entries": [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "tags": (e.get("tags") or [])[:6],
                "updated_at": e.get("updated_at"),
            }
            for e in entries[:limit]
            if isinstance(e, dict)
        ],
    }


def _summarize_reverse_jobs(raw: dict[str, Any], *, limit: int = 10) -> dict[str, Any]:
    jobs = raw.get("jobs") if isinstance(raw.get("jobs"), list) else []
    return {
        "count": len(jobs),
        "jobs": [
            {
                "id": j.get("id"),
                "title": j.get("title"),
                "status": j.get("status"),
                "target_url": j.get("target_url"),
            }
            for j in jobs[:limit]
            if isinstance(j, dict)
        ],
    }


def _enrich_current_view(api: Any, route: str, ui: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {"route": route, "page_label": _page_label(route, ui)}
    theme = _theme_from_ui(ui)
    if theme:
        view["ui_theme"] = theme
    params = ui.get("route_params") if isinstance(ui.get("route_params"), dict) else {}
    task_id = str(ui.get("task_id") or params.get("id") or "").strip()

    if route == "task" and task_id:
        try:
            from argusai.core.tools.kb_tasks import get_task_detail
            detail = get_task_detail(api, task_id=task_id)
            if isinstance(detail, dict) and not detail.get("error"):
                view["current_task"] = {
                    "id": detail.get("id"),
                    "name": detail.get("name"),
                    "target_url": detail.get("target_url"),
                    "status": detail.get("status"),
                    "severity_counts": detail.get("severity_counts"),
                    "top_findings": detail.get("top_findings") or [],
                    "is_running": detail.get("is_running"),
                }
        except Exception as e:
            view["current_task_error"] = str(e)[:200]

    elif route == "mcp":
        try:
            view["mcp"] = _summarize_mcp_servers(api.list_mcp_servers())
        except Exception as e:
            view["mcp_error"] = str(e)[:200]

    elif route == "proxy":
        try:
            view["proxy"] = api.get_proxy_status()
        except Exception as e:
            view["proxy_error"] = str(e)[:200]

    elif route in ("knowledge", "knowledge_entry"):
        try:
            raw = api.kb_list(include_superseded=False, include_trashed=False)
            view["knowledge"] = _summarize_kb_list(raw if isinstance(raw, dict) else {})
            entry_id = str(params.get("id") or ui.get("knowledge_entry_id") or "").strip()
            if route == "knowledge_entry" and entry_id:
                entry = api.kb_get(entry_id)
                if isinstance(entry, dict) and entry.get("ok"):
                    ent = entry.get("entry") or {}
                    view["knowledge_entry"] = {
                        "id": ent.get("id"),
                        "title": ent.get("title"),
                        "tags": ent.get("tags") or [],
                        "body_preview": str(ent.get("body") or "")[:400],
                    }
        except Exception as e:
            view["knowledge_error"] = str(e)[:200]

    elif route in ("reverse", "reverse_job"):
        try:
            view["reverse_jobs"] = _summarize_reverse_jobs(api.reverse_job_list())
            job_id = str(params.get("id") or ui.get("reverse_job_id") or "").strip()
            if route == "reverse_job" and job_id:
                job = api.reverse_job_get(job_id)
                if isinstance(job, dict) and job.get("ok"):
                    view["reverse_job"] = job.get("job") or {}
        except Exception as e:
            view["reverse_error"] = str(e)[:200]

    elif route == "user_todos":
        try:
            from argusai.core.workspace.user_todos import list_todos
            root = api.settings_mgr.get().data_root
            items = list_todos(root, limit=15)
            view["user_todos"] = {
                "pending": sum(1 for x in items if x.get("status") == "pending"),
                "items": items[:10],
            }
        except Exception as e:
            view["user_todos_error"] = str(e)[:200]

    elif route == "settings":
        try:
            settings = api.get_settings()
            view["settings_sections"] = {
                "data_root": settings.get("data_root"),
                "ai_enabled": (settings.get("ai") or {}).get("enabled"),
                "proxy_port": (settings.get("proxy") or {}).get("port"),
                "has_user_habits": bool((settings.get("user_habits") or {})),
            }
        except Exception as e:
            view["settings_error"] = str(e)[:200]

    elif route == "new":
        view["form"] = "new_scan"

    elif route == "tasks":
        view["list"] = "task_list"

    client = ui.get("client") if isinstance(ui.get("client"), dict) else {}
    if client:
        view["client"] = client

    return view


def build_screen_snapshot(api: Any) -> dict[str, Any]:
    """Current screen: route, human label, and page-specific payload."""
    ui = _ui_from_api(api)
    route = _route_name(ui)
    label = _page_label(route, ui)
    overlays = ui.get("overlays") if isinstance(ui.get("overlays"), dict) else {}

    lines = [f"用户当前主界面：{label}（route={route or 'unknown'}）"]
    theme = _theme_from_ui(ui)
    if theme.get("effective"):
        pref = theme.get("preferred")
        suffix = f"，偏好={pref}" if pref and pref != theme.get("effective") else ""
        lines.append(f"界面主题：{theme.get('label') or theme['effective']}模式（effective={theme['effective']}{suffix}）。")
    if overlays.get("assistant_panel_open"):
        lines.append("AI 助手侧栏面板已打开（主内容区仍为上述页面）。")
    if overlays.get("sandbox_panel_open"):
        lines.append("会话沙盒侧栏已打开。")

    current_view = _enrich_current_view(api, route, ui)
    if route == "mcp" and current_view.get("mcp"):
        mcp = current_view["mcp"]
        lines.append(f"MCP：共 {mcp.get('count', 0)} 个服务，{mcp.get('enabled_count', 0)} 个已启用。")
        for s in (mcp.get("servers") or [])[:3]:
            err = s.get("error")
            st = s.get("status") or "unknown"
            lines.append(f"  - {s.get('name')}: {st}" + (f" ({err[:80]})" if err else ""))

    return {
        "route": route,
        "page_label": label,
        "raw_hash": str(ui.get("raw_hash") or ui.get("raw") or ""),
        "route_params": ui.get("route_params") if isinstance(ui.get("route_params"), dict) else {},
        "overlays": overlays,
        "screen_summary": "\n".join(lines),
        "current_view": current_view,
        "ui_theme": theme,
        "ui_received_at": ui.get("received_at"),
        "snapshot_at": time.time(),
    }


def build_app_snapshot(api: Any) -> dict[str, Any]:
    """Full app snapshot: current screen + global task/proxy/AI summary."""
    screen = build_screen_snapshot(api)

    tasks_raw = api.list_tasks()
    tasks_summary = []
    for t in tasks_raw:
        tasks_summary.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "url": t.get("target_url"),
            "status": t.get("status"),
            "severity_counts": t.get("severity", {}),
            "checks_done": t.get("checks_done"),
            "checks_total": t.get("checks_total"),
            "created_at": t.get("created_at"),
        })

    proxy = api.get_proxy_status()
    settings = api.get_settings()
    ui = _ui_from_api(api)
    route = _route_name(ui)
    task_id = str(ui.get("task_id") or (ui.get("route_params") or {}).get("id") or "").strip()

    visible_tasks = tasks_summary[:12]
    if route == "tasks":
        visible_tasks = tasks_summary[:20]

    out: dict[str, Any] = {
        **screen,
        "tasks": tasks_summary,
        "task_count": len(tasks_summary),
        "running_count": sum(1 for t in tasks_summary if t.get("status") == "running"),
        "visible_tasks": visible_tasks,
        "proxy": proxy,
        "ai": {
            "enabled": settings.get("ai", {}).get("enabled", True),
            "endpoint_count": len(settings.get("ai", {}).get("endpoints", [])),
            "endpoint_names": [ep.get("name", "") for ep in settings.get("ai", {}).get("endpoints", [])],
        },
        "checks_available": len(api.list_checks()),
    }

    if route == "task" and task_id and not screen.get("current_view", {}).get("current_task"):
        try:
            from argusai.core.tools.kb_tasks import get_task_detail
            detail = get_task_detail(api, task_id=task_id)
            if isinstance(detail, dict) and not detail.get("error"):
                out["current_task"] = detail
        except Exception:
            pass
    elif screen.get("current_view", {}).get("current_task"):
        out["current_task"] = screen["current_view"]["current_task"]

    return out
