"""Agent-facing tools for paired communication-channel delivery."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from argusai.core.channels.media import detect_kind, detect_mime
from argusai.core.paths import PathManager
from argusai.core.workspace.session_sandbox import resolve_in_sandbox

_CHANNEL_FILE_LIMIT = 32 * 1024 * 1024
# Same conversation used from several chat apps: fan-out to all actively chatting.
_ACTIVE_FANOUT_SEC = 15 * 60
# GUI / unbound session: fall back to a recently seen send target.
_RECENT_FALLBACK_SEC = 48 * 3600


def _session_id(kwargs: dict[str, Any]) -> str:
    session_id = str(kwargs.get("_session_id") or "").strip()
    if not session_id:
        raise ValueError("channel tools require an active assistant conversation")
    return session_id


def _send_capable(api: Any) -> list[Any]:
    return [
        identity
        for identity in api._channels_store().list_identities()
        if "send" in identity.permissions
    ]


def _targets(api: Any, session_id: str) -> list[Any]:
    return [
        identity
        for identity in api._channels_store().list_identities_for_session(session_id)
        if "send" in identity.permissions
    ]


def _other_session_send_targets(api: Any, session_id: str) -> list[dict[str, Any]]:
    """Paired send-capable identities bound to a *different* conversation."""
    safe = str(session_id or "").strip()
    rows: list[dict[str, Any]] = []
    for identity in _send_capable(api):
        bound = str(identity.bound_session_id or "").strip()
        if not bound or bound == safe:
            continue
        rows.append({
            "identity_id": identity.id,
            "provider": identity.provider,
            "display_name": identity.display_name,
            "bound_session_id": bound,
            "last_seen_at": identity.last_seen_at,
        })
    return rows[:20]


def _identity_row(identity: Any) -> dict[str, Any]:
    return {
        "identity_id": identity.id,
        "channel_id": identity.channel_id,
        "provider": identity.provider,
        "display_name": identity.display_name,
        "bound_session_id": str(identity.bound_session_id or ""),
        "last_seen_at": float(identity.last_seen_at or 0),
    }


def _no_target_message(api: Any, session_id: str) -> str:
    others = _other_session_send_targets(api, session_id)
    if others:
        preview = ", ".join(
            f"{item.get('provider')}:{item.get('display_name') or item.get('identity_id')}->"
            f"{item.get('bound_session_id')}"
            for item in others[:3]
        )
        return (
            "no send-enabled channel identity is bound to THIS conversation "
            f"({session_id}), and none were recently active enough to auto-route. "
            f"Paired elsewhere: {preview}. Pass identity_id for a separately confirmed "
            "outbound send; rebind only if future incoming messages should use this conversation."
        )
    unbound = [
        identity
        for identity in _send_capable(api)
        if not str(identity.bound_session_id or "").strip()
    ]
    if unbound:
        return (
            "paired channel identities exist with send permission, but none are bound "
            "to a conversation and none were recently active. Bind one in Channels or "
            "message the bot once from that chat, then retry."
        )
    lacking = [
        identity
        for identity in api._channels_store().list_identities()
        if "send" not in identity.permissions
        and str(identity.bound_session_id or "").strip() == str(session_id or "").strip()
    ]
    if lacking:
        return (
            "a channel identity is bound to this conversation but lacks the send "
            "permission. Enable send on that identity in Channels, then retry."
        )
    return (
        "no paired channel target with send permission is available. "
        "Pair Telegram/Feishu first, grant send, and bind the identity to this session."
    )


def _session_object(api: Any, session_id: str) -> Any | None:
    sessions = getattr(api, "_assistant_sessions", None) or {}
    return sessions.get(session_id) if isinstance(sessions, dict) else None


def _origin_causal_meta(api: Any, session_id: str) -> dict[str, Any]:
    """Freeze why this send happens: the origin session's last user instruction.

    Captured at send time so the receiving Agent reads a self-contained reason
    from its own history instead of reaching into an unrelated conversation.
    """
    session = _session_object(api, session_id)
    meta: dict[str, Any] = {"origin_session_id": str(session_id or "").strip()}
    if session is None:
        return meta
    for message in reversed(list(getattr(session, "history", []) or [])):
        if getattr(message, "role", "") != "user":
            continue
        if getattr(message, "hidden", False) or getattr(message, "ui_hidden", False):
            continue
        reason = str(getattr(message, "content", "") or "").strip()
        if not reason:
            continue
        meta["reason"] = reason[:600]
        trigger_ref = str(getattr(message, "source_ref", "") or "").strip()
        if not trigger_ref:
            trigger_ref = f"ts:{getattr(message, 'timestamp', 0.0)}"
        meta["trigger_ref"] = trigger_ref
        break
    return meta


def _turn_source_identity(api: Any, session_id: str) -> Any | None:
    """Identity that authored the current channel turn, if still send-capable."""
    session = _session_object(api, session_id)
    if session is None:
        return None
    source = str(getattr(session, "_current_turn_source", "") or "").strip().lower()
    if source != "channel":
        return None
    ref = str(getattr(session, "_current_turn_source_ref", "") or "").strip()
    if not ref:
        return None
    for identity in _send_capable(api):
        if identity.id == ref:
            return identity
    # Still paired but missing send — look up raw so the error can be precise.
    try:
        identity = api._channels_store().get_identity(ref)
    except Exception:
        return None
    return identity if "send" in identity.permissions else None


def _resolve_send_targets(
    api: Any,
    session_id: str,
    identity_id: str = "",
) -> tuple[list[Any], str]:
    """Pick one or more outbound identities.

    Route modes:
    - ``explicit``: caller passed identity_id bound here or not yet bound
    - ``explicit_cross_session``: separately confirmed send to a paired recipient
      whose incoming messages remain bound to another conversation
    - ``turn_source``: reply to the channel identity that sent this turn
    - ``session`` / ``session_fanout``: bound to this conversation (GUI / no turn source)
    - ``auto_single`` / ``auto_active_fanout`` / ``auto_recent``: unbound-session fallback
    - ``auto_only_recipient`` / ``auto_recent_recipient``: an unambiguous paired
      recipient, without rebinding inbound
    """
    safe_session = str(session_id or "").strip()
    requested = str(identity_id or "").strip()
    if requested:
        for identity in _send_capable(api):
            if identity.id != requested:
                continue
            bound = str(identity.bound_session_id or "").strip()
            if bound and bound != safe_session:
                return [identity], "explicit_cross_session"
            return [identity], "explicit"
        raise ValueError(
            "channel target is unavailable or lacks send permission. "
            + _no_target_message(api, session_id)
        )

    # Prefer the chat app that authored this turn (Telegram → Telegram, …).
    turn_identity = _turn_source_identity(api, session_id)
    if turn_identity is not None:
        return [turn_identity], "turn_source"

    bound = _targets(api, session_id)
    if len(bound) == 1:
        return bound, "session"
    if len(bound) > 1:
        # GUI / cron asking to "send me the file" with several apps on one session.
        return bound, "session_fanout"

    # Auto-routing may only reach for identities that are free. Delivering for
    # one bound elsewhere means rebinding it (the outbox refuses a mismatched
    # session), which silently repoints that conversation's user at this one.
    capable = [
        identity for identity in _send_capable(api)
        if str(identity.bound_session_id or "").strip() in {"", safe_session}
    ]
    if not capable:
        elsewhere = [
            identity for identity in _send_capable(api)
            if str(identity.bound_session_id or "").strip() not in {"", safe_session}
        ]
        if len(elsewhere) == 1:
            return elsewhere, "auto_only_recipient"
        recent_elsewhere = sorted(
            (
                identity for identity in elsewhere
                if float(identity.last_seen_at or 0) > 0
                and time.time() - float(identity.last_seen_at) <= _RECENT_FALLBACK_SEC
            ),
            key=lambda item: float(item.last_seen_at or 0),
            reverse=True,
        )
        if recent_elsewhere and (
            len(recent_elsewhere) == 1
            or float(recent_elsewhere[0].last_seen_at or 0)
            - float(recent_elsewhere[1].last_seen_at or 0) >= 30 * 60
        ):
            return [recent_elsewhere[0]], "auto_recent_recipient"
        raise ValueError(_no_target_message(api, session_id))
    if len(capable) == 1:
        return capable, "auto_single"

    now = time.time()
    active = [
        identity for identity in capable
        if float(identity.last_seen_at or 0) > 0
        and now - float(identity.last_seen_at) <= _ACTIVE_FANOUT_SEC
    ]
    if active:
        return active, "auto_active_fanout"

    recent = sorted(
        (
            identity for identity in capable
            if float(identity.last_seen_at or 0) > 0
            and now - float(identity.last_seen_at) <= _RECENT_FALLBACK_SEC
        ),
        key=lambda item: float(item.last_seen_at or 0),
        reverse=True,
    )
    if len(recent) == 1:
        return recent, "auto_recent"
    if recent and (
        len(recent) == 1
        or float(recent[0].last_seen_at or 0) - float(recent[1].last_seen_at or 0) >= 30 * 60
    ):
        return [recent[0]], "auto_recent"

    raise ValueError(
        _no_target_message(api, session_id)
        + " Multiple send targets exist; pass identity_id from channel_list_targets."
    )


def channel_list_targets(
    api: Any,
    identity_id: str = "",
    channel_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """List paired outbound channel targets for the current conversation."""
    session_id = _session_id(kwargs)
    requested_identity = str(identity_id or "").strip()
    requested_channel = str(channel_id or "").strip()
    statuses = {
        str(item.get("channel_id") or ""): item
        for item in api._channels_runtime().statuses()
    }
    channels = {
        str(item.get("id") or ""): item
        for item in api._channels_store().list_channels(public=True)
    }
    rows = []
    for identity in _targets(api, session_id):
        if requested_identity and identity.id != requested_identity:
            continue
        if requested_channel and identity.channel_id != requested_channel:
            continue
        channel = channels.get(identity.channel_id) or {}
        status = statuses.get(identity.channel_id) or {}
        rows.append({
            "identity_id": identity.id,
            "channel_id": identity.channel_id,
            "provider": identity.provider,
            "channel_name": str(channel.get("name") or identity.provider),
            "display_name": identity.display_name,
            "chat_id": identity.external_chat_id,
            "thread_id": identity.external_thread_id,
            "connected_here": bool(status.get("connected")),
            "permissions": list(identity.permissions),
            "last_seen_at": identity.last_seen_at,
        })
    auto_targets: list[dict[str, Any]] = []
    route_mode = "session" if rows else "none"
    if not rows:
        try:
            resolved, route_mode = _resolve_send_targets(api, session_id)
            auto_targets = [_identity_row(item) for item in resolved]
        except ValueError:
            route_mode = "none"
    return {
        "ok": True,
        "session_id": session_id,
        "targets": rows,
        "count": len(rows),
        "route_mode": route_mode,
        "auto_targets": auto_targets,
        "other_session_targets": _other_session_send_targets(api, session_id),
        "note": (
            "When this turn came from a channel, channel_send_* defaults to that "
            "identity (route_mode=turn_source). From the GUI with several apps bound, "
            "omit identity_id to fan out to all of them."
            if rows
            else (
                f"No identity is bound to this conversation; sends will auto-route "
                f"({route_mode}) to the current channel turn, a recently active "
                f"unambiguous chat, or the sole paired recipient when possible. A recipient listed in "
                f"other_session_targets can still be selected explicitly; this "
                f"sends outbound without changing where its incoming chat is bound. "
                + (_no_target_message(api, session_id) if not auto_targets else "")
            ).strip()
        ),
    }


def _bind_for_delivery(api: Any, identity: Any, session_id: str) -> Any:
    """Adopt an unbound identity into the conversation that is sending.

    Multi-app use of one Agent session means Telegram/Feishu peers should land
    on this session when the user asks it to deliver. An identity already bound
    elsewhere is left alone: stealing it would redirect that conversation's
    incoming messages here, and its owner would never see why.
    """
    bound = str(identity.bound_session_id or "").strip()
    if bound:
        return identity
    return api._channels_store().bind_identity(identity.id, session_id)


def channel_delivery_status(
    api: Any,
    delivery_id: str = "",
    limit: int = 10,
    **kwargs: Any,
) -> dict[str, Any]:
    """Check whether queued channel deliveries were sent, retried, or failed."""
    session_id = _session_id(kwargs)
    requested = str(delivery_id or "").strip()
    rows = []
    for job in api._channels_store().list_outbound(
        session_id=session_id,
        limit=max(1, min(int(limit or 10), 50)),
    ):
        if requested and str(job.get("id") or "") != requested:
            continue
        attachments = [
            {
                "name": str(item.get("name") or ""),
                "kind": str(item.get("type") or item.get("kind") or "file"),
                "size": int(item.get("size") or 0),
            }
            for item in list(job.get("attachments") or [])
            if isinstance(item, dict)
        ]
        rows.append({
            "delivery_id": str(job.get("id") or ""),
            "state": str(job.get("state") or ""),
            "attempts": int(job.get("attempts") or 0),
            "delivered_parts": int(job.get("delivered_parts") or 0),
            "channel_id": str(job.get("channel_id") or ""),
            "identity_id": str(job.get("identity_id") or ""),
            "text_preview": str(job.get("text") or "")[:120],
            "attachments": attachments,
            "last_error": str(job.get("last_error") or ""),
            "created_at": float(job.get("created_at") or 0),
            "sent_at": float(job.get("sent_at") or 0),
        })
    return {
        "ok": True,
        "session_id": session_id,
        "deliveries": rows,
        "count": len(rows),
        "note": (
            "state is pending (queued or waiting to retry), processing, sent, or "
            "failed. failed carries the provider's own reason in last_error."
        ),
    }


def _delivery_result(job: dict[str, Any], identity: Any, **extra: Any) -> dict[str, Any]:
    return {
        "delivery_id": job["id"],
        "identity_id": identity.id,
        "channel_id": identity.channel_id,
        "provider": identity.provider,
        "state": job["state"],
        **extra,
    }


def channel_send_message(
    api: Any,
    text: str,
    identity_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send proactive text to paired channel target(s). Requires confirmation.

    Omitting identity_id fans out to every send-enabled identity bound to this
    conversation (or auto-routes to recently active chats when none are bound).
    """
    session_id = _session_id(kwargs)
    clean_text = str(text or "").strip()
    if not clean_text:
        return {"ok": False, "error": "text is required"}
    identities, route_mode = _resolve_send_targets(api, session_id, identity_id)
    causal_meta = _origin_causal_meta(api, session_id)
    deliveries = []
    for identity in identities:
        identity = _bind_for_delivery(api, identity, session_id)
        job = api._channels_runtime().enqueue_outbound(
            identity_id=identity.id,
            session_id=session_id,
            text=clean_text,
            allow_cross_session=route_mode in {
                "explicit_cross_session",
                "auto_only_recipient",
                "auto_recent_recipient",
            },
            meta=causal_meta,
        )
        deliveries.append(_delivery_result(job, identity))
    first = deliveries[0]
    return {
        "ok": True,
        "queued": True,
        "route_mode": route_mode,
        "delivery_count": len(deliveries),
        "deliveries": deliveries,
        # Back-compat single-target fields (first delivery).
        "delivery_id": first["delivery_id"],
        "identity_id": first["identity_id"],
        "channel_id": first["channel_id"],
        "provider": first["provider"],
        "state": first["state"],
    }


def channel_reply(
    api: Any,
    messages: list[str],
    pause_ms: int = 350,
    **kwargs: Any,
) -> dict[str, Any]:
    """Reply to the current channel user as ordered, human-paced bubbles.

    Unlike ``channel_send_message`` this is not a proactive cross-recipient
    write: it is valid only while handling a paired channel turn and is pinned
    to that exact source identity. The Agent chooses the wording and bubble
    boundaries after applying the global user-habits profile; the tool never
    rewrites facts or invents conversational filler.
    """
    session_id = _session_id(kwargs)
    if not isinstance(messages, list):
        return {"ok": False, "error": "messages must be an array of strings"}
    bubbles = [str(item or "").strip() for item in messages]
    bubbles = [item for item in bubbles if item]
    if not bubbles:
        return {"ok": False, "error": "at least one non-empty message is required"}
    if len(bubbles) > 8:
        return {"ok": False, "error": "channel_reply accepts at most 8 messages"}
    if any(len(item) > 12000 for item in bubbles):
        return {"ok": False, "error": "each channel_reply message must be at most 12000 characters"}

    identity = _turn_source_identity(api, session_id)
    if identity is None:
        return {
            "ok": False,
            "error": (
                "channel_reply is only available inside a paired channel-authored turn; "
                "use channel_send_message for proactive desktop or cross-recipient delivery"
            ),
        }
    delay = max(0, min(int(pause_ms or 0), 2000))
    causal_meta = _origin_causal_meta(api, session_id)
    session = getattr(api, "_assistant_sessions", {}).get(session_id)
    native_stream_source = ""
    if bool(getattr(session, "channel_live", True)):
        native_stream_source = str(
            getattr(session, "_current_turn_channel_message_id", "") or ""
        ).strip()
    deliveries: list[dict[str, Any]] = []
    for index, bubble in enumerate(bubbles):
        job = api._channels_runtime().enqueue_outbound(
            identity_id=identity.id,
            session_id=session_id,
            text=bubble,
            meta={
                **causal_meta,
                "channel_reply": True,
                "sequence": index + 1,
                "sequence_total": len(bubbles),
                "delay_before_ms": 0 if index == 0 else delay,
                # Tool-call text is unavailable to the normal token stream.
                # Preserve the Agent's bubble boundary and let the reliable
                # outbox replay this completed bubble through the provider's
                # native/edit stream when supported.
                "native_stream_source_message_id": native_stream_source,
            },
        )
        deliveries.append(_delivery_result(
            job,
            identity,
            sequence=index + 1,
            delay_before_ms=0 if index == 0 else delay,
        ))

    return {
        "ok": True,
        "queued": True,
        # The assistant loop treats an accepted channel_reply as the final
        # action of this turn. Rejected/failed calls never set this flag, so
        # the model can still recover or explain the failure.
        "terminal_delivery": True,
        "route_mode": "turn_source",
        "delivery_count": len(deliveries),
        "deliveries": deliveries,
        "delivery_ids": [item["delivery_id"] for item in deliveries],
        "identity_id": identity.id,
        "channel_id": identity.channel_id,
        "provider": identity.provider,
        "pause_ms": delay,
        "note": "Reply accepted; channel_reply terminates the current Agent turn.",
    }


def channel_send_file(
    api: Any,
    path: str,
    identity_id: str = "",
    caption: str = "",
    filename: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send one sandbox file to paired channel target(s). Requires confirmation.

    Omitting identity_id fans out across every send-enabled identity bound to
    this conversation so the same Agent session can serve Telegram + Feishu + …
    """
    session_id = _session_id(kwargs)
    identities, route_mode = _resolve_send_targets(api, session_id, identity_id)
    root = PathManager(
        data_root=api.settings_mgr.get().data_root,
    ).assistant_sandbox(session_id).resolve()
    try:
        target = resolve_in_sandbox(root, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.is_file():
        return {"ok": False, "error": "sandbox file not found"}
    size = target.stat().st_size
    if size > _CHANNEL_FILE_LIMIT:
        return {
            "ok": False,
            "error": "file exceeds the 32 MiB channel transfer limit",
            "size": size,
        }
    digest = hashlib.sha256()
    header = b""
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not header:
                header = chunk[:16]
            digest.update(chunk)
    requested_name = Path(str(filename or "").replace("\\", "/")).name.strip()
    safe_name = requested_name or target.name
    # Providers have separate photo/video/audio endpoints; sending a screenshot
    # as a generic blob loses the inline preview and hits stricter file APIs.
    mime = detect_mime(name=safe_name, data=header)
    kind = detect_kind(mime, safe_name)
    relative_path = target.relative_to(root).as_posix()
    attachment = {
        "type": kind,
        "name": safe_name[:180],
        "mime": mime,
        "size": size,
        "sha256": digest.hexdigest(),
        "path": str(target),
        "relative_path": relative_path,
    }
    caption_text = str(caption or "").strip()
    causal_meta = _origin_causal_meta(api, session_id)
    deliveries = []
    for identity in identities:
        identity = _bind_for_delivery(api, identity, session_id)
        job = api._channels_runtime().enqueue_outbound(
            identity_id=identity.id,
            session_id=session_id,
            text=caption_text,
            attachments=[attachment],
            allow_cross_session=route_mode in {
                "explicit_cross_session",
                "auto_only_recipient",
                "auto_recent_recipient",
            },
            meta=causal_meta,
        )
        deliveries.append(_delivery_result(job, identity))
    first = deliveries[0]
    file_meta = {
        "name": safe_name,
        "relative_path": relative_path,
        "size": size,
        "sha256": digest.hexdigest(),
        "mime": mime,
        "kind": kind,
    }
    return {
        "ok": True,
        "queued": True,
        "route_mode": route_mode,
        "delivery_count": len(deliveries),
        "deliveries": deliveries,
        "delivery_id": first["delivery_id"],
        "identity_id": first["identity_id"],
        "channel_id": first["channel_id"],
        "provider": first["provider"],
        "state": first["state"],
        "file": file_meta,
    }
