"""Agent-facing tools for the domain-agnostic assistant cron store."""

from __future__ import annotations

from typing import Any


def _store(api: Any):
    from argusai.core.assistant.cron import CronStore

    data_root = None
    try:
        data_root = api.settings_mgr.get().data_root
    except Exception:
        pass
    return CronStore(data_root)


def cron_list(api: Any, tag: str = "", **kwargs: Any) -> dict[str, Any]:
    """List scheduled assistant cron jobs (read-only)."""
    jobs = _store(api).list_jobs()
    tag_f = str(tag or "").strip().lower()
    if tag_f:
        jobs = [j for j in jobs if tag_f in [str(t).lower() for t in (j.get("tags") or [])]]
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def cron_runs(api: Any, job_id: str = "", limit: int = 20, **kwargs: Any) -> dict[str, Any]:
    """List recent execution records for one cron job (read-only)."""
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    try:
        runs = _store(api).list_runs(jid, limit=int(limit or 20))
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "job_id": jid, "runs": []}
    return {"ok": True, "job_id": jid, "runs": runs, "count": len(runs)}


def cron_upsert(
    api: Any,
    name: str = "",
    prompt: str = "",
    every_seconds: int = 3600,
    schedule_kind: str = "interval",
    cron_expr: str = "",
    webhook_debounce_seconds: int = 0,
    watch_path: str = "",
    watch_glob: str = "*",
    watch_recursive: bool = True,
    watch_poll_seconds: int = 2,
    watch_debounce_seconds: int = 2,
    destination: str = "new_session",
    session_id: str = "",
    tags: list[str] | str | None = None,
    job_id: str = "",
    enabled: bool = True,
    max_runs: int = 0,
    context_json: str = "",
    on_change_only: bool = True,
    report_to_origin: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create or update a scheduled assistant job. Requires user confirmation.

    Codex-aligned destinations:
    - ``new_session`` (default): each standalone run gets its own cron chat; results
      are recorded in ``cron_runs`` and (by default) reported back into the
      creating conversation via ``origin_session_id``.
    - ``dedicated_session``: create one job-owned chat on first fire and reuse it.
    - ``session``: wake an existing chat (thread automation); uses that chat's
      history as context. Pass ``session_id`` or leave empty to use the current
      conversation.
    """
    import json

    tag_list: list[str]
    if isinstance(tags, str):
        tag_list = [p.strip() for p in tags.replace(",", " ").split() if p.strip()]
    elif isinstance(tags, list):
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tag_list = []
    context: dict[str, Any] = {}
    raw_ctx = str(context_json or "").strip()
    if raw_ctx:
        try:
            parsed = json.loads(raw_ctx)
            if isinstance(parsed, dict):
                context = parsed
        except Exception:
            return {"ok": False, "error": "context_json must be a JSON object"}
    schedule: dict[str, Any] = {"kind": str(schedule_kind or "interval").strip().lower()}
    if schedule["kind"] == "cron":
        schedule["expr"] = str(cron_expr or "").strip()
    elif schedule["kind"] == "interval":
        schedule["every_seconds"] = int(every_seconds or 3600)
    elif schedule["kind"] == "webhook":
        schedule["debounce_seconds"] = max(0, int(webhook_debounce_seconds or 0))
    elif schedule["kind"] == "file_change":
        schedule.update({
            "path": str(watch_path or "").strip(),
            "glob": str(watch_glob or "*").strip() or "*",
            "recursive": bool(watch_recursive),
            "poll_seconds": max(1, int(watch_poll_seconds or 2)),
            "debounce_seconds": max(0, int(watch_debounce_seconds or 0)),
        })

    dest = str(destination or "new_session").strip().lower()
    if dest not in {"session", "new_session", "dedicated_session"}:
        dest = "new_session"
    current_sid = str(kwargs.get("_session_id") or "").strip()
    target_sid = str(session_id or "").strip()
    if dest == "session" and not target_sid:
        target_sid = current_sid
    if dest == "session" and target_sid and target_sid != current_sid:
        reader = getattr(api, "get_assistant_session", None)
        if callable(reader):
            try:
                found = reader(target_sid)
            except Exception as exc:
                return {"ok": False, "error": f"target session lookup failed: {exc}"}
            if isinstance(found, dict) and not found.get("ok"):
                return {"ok": False, "error": "target session not found"}

    payload = {
        "id": str(job_id or "").strip(),
        "name": str(name or "").strip() or "Scheduled task",
        "prompt": str(prompt or "").strip(),
        "destination": dest,
        "session_id": target_sid,
        "origin_session_id": current_sid,
        "tags": tag_list,
        "enabled": bool(enabled),
        "max_runs": max(0, int(max_runs or 0)),
        "schedule": schedule,
        "context": context,
        "notify": {
            "on_success": True,
            "on_change_only": bool(on_change_only),
            "on_failure": True,
            "report_to_origin": bool(report_to_origin),
        },
        "reschedule": True,
    }
    try:
        store = _store(api)
        job = store.upsert_job(payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    webhook: dict[str, Any] | None = None
    file_watch: dict[str, Any] | None = None
    if schedule["kind"] == "webhook":
        from argusai.core.assistant.cron_triggers import (
            ensure_webhook_secret,
            webhook_url,
        )

        ensure_webhook_secret(store.root, str(job.get("id") or ""))
        settings = api.settings_mgr.get()
        host = str(getattr(settings, "assistant_cron_webhook_host", "127.0.0.1"))
        port = int(getattr(settings, "assistant_cron_webhook_port", 8765) or 8765)
        webhook = {
            "url": webhook_url(host, port, str(job.get("id") or "")),
            "secret_available_locally": True,
            "hint": "Open Scheduled tasks or run cron webhook-secret to copy/rotate the signing secret.",
        }
    elif schedule["kind"] == "file_change":
        from argusai.core.assistant.cron_triggers import FileChangePoller

        file_watch = FileChangePoller(store).initialize_job(job)
    hint = (
        "destination=session runs inside the target chat (keeps thread context). "
        "destination=dedicated_session creates one job-owned chat and reuses it. "
        "destination=new_session uses a distinct cron chat per tick; results go to cron_runs "
        "and are reported back to origin_session_id when report_to_origin=true. "
        "Call cron_runs later to inspect history. Use max_runs for a finite schedule."
    )
    every = int((job.get("schedule") or {}).get("every_seconds") or 0)
    if dest == "session" and every and every < 60:
        hint += (
            " WARNING: destination=session with interval < 60s will contend with "
            "interactive chat on that session; prefer new_session for tight loops."
        )
    out = {"ok": True, "job": job, "hint": hint}
    if webhook is not None:
        out["webhook"] = webhook
    if file_watch is not None:
        out["file_watch"] = file_watch
    return out


def cron_set_enabled(api: Any, job_id: str = "", enabled: bool = True, **kwargs: Any) -> dict[str, Any]:
    """Enable or pause a cron job. Requires user confirmation."""
    try:
        job = _store(api).set_enabled(str(job_id or "").strip(), bool(enabled))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not job:
        return {"ok": False, "error": "job not found"}
    return {"ok": True, "job": job}


def cron_delete(api: Any, job_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a cron job and its run history. Requires user confirmation."""
    jid = str(job_id or "").strip()
    try:
        store = _store(api)
        ok = store.delete_job(jid)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not ok:
        return {"ok": False, "error": "job not found"}
    try:
        from argusai.core.assistant.cron_triggers import delete_webhook_secret

        delete_webhook_secret(store.root, jid)
    except Exception:
        pass
    return {"ok": True, "deleted": True, "job_id": jid}


def cron_grant_high_risk(
    api: Any,
    job_id: str = "",
    granted: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Grant/revoke job-scoped unattended high-risk execution.

    This tool is itself in ``_NO_AUTO_APPROVE_TOOLS`` so the AI cannot forge
    the second acknowledgement.
    """
    import time

    jid = str(job_id or "").strip()
    try:
        store = _store(api)
        job = store.get_job(jid)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not job:
        return {"ok": False, "error": "job not found"}
    approval = dict(job.get("approval") or {})
    approval.update({
        "mode": "unattended_full",
        "high_risk_ack": bool(granted),
        "granted_at": time.time() if granted else None,
        "grant_source": "assistant_confirmation",
        "grant_session_id": str(kwargs.get("_session_id") or "")[:128],
    })
    updated = store.update_job_fields(jid, approval=approval)
    return {
        "ok": True,
        "job_id": jid,
        "granted": bool(granted),
        "approval": approval,
        "job": updated,
    }
