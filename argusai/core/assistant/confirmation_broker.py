"""Shared typed confirmation policy for GUI, ACP, and message channels."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .streaming import normalize_confirmation_decision

REMOTE_CONFIRMATION_SOURCES = frozenset({"channel", "qq", "telegram", "discord", "slack", "feishu", "dingtalk", "wecom", "wechat", "webhook"})


@dataclass
class ExternalConfirmationTicket:
    session_id: str
    pending_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    permission_key: str = ""
    created_at: float = field(default_factory=time.time)
    decision: str = ""
    event: threading.Event = field(default_factory=threading.Event)


def _pending_calls(pending: dict[str, Any]) -> list[dict[str, Any]]:
    rows = pending.get("confirmations")
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, dict)]
    single = pending.get("tool_call")
    return [single] if isinstance(single, dict) else []


def _call_identity(item: dict[str, Any]) -> tuple[str, str]:
    tool_call = item.get("tool_call") if isinstance(item.get("tool_call"), dict) else item
    if not isinstance(tool_call, dict):
        return "", ""
    call_id = str(tool_call.get("id") or item.get("confirm_id") or "")
    function = tool_call.get("function")
    name = (
        str(function.get("name") or "")
        if isinstance(function, dict)
        else str(item.get("name") or tool_call.get("name") or "")
    )
    return call_id, name


class ConfirmationBroker:
    """Normalize a decision and enforce source-specific safety boundaries."""

    def __init__(self) -> None:
        self._external: dict[tuple[str, str], ExternalConfirmationTicket] = {}
        self._lock = threading.RLock()

    def prepare(
        self,
        session: Any,
        action: Any,
        *,
        source: str = "gui",
    ) -> dict[str, Any]:
        decision = normalize_confirmation_decision(action)
        if decision is None:
            return {"ok": False, "error_code": "invalid_confirmation", "error": "invalid confirmation action"}
        pending = getattr(session, "_pending", None)
        if not isinstance(pending, dict):
            return {"ok": False, "error_code": "confirmation_missing", "error": "no pending confirmation"}

        pending_id, version = session._ensure_pending_identity()
        decision.setdefault("pending_id", pending_id)
        decision.setdefault("version", version)
        calls = _pending_calls(pending)
        if (
            decision.get("type") == "confirmation_decision"
            and not decision.get("call_id")
            and decision.get("verdict") != "deny"
            and len(calls) == 1
        ):
            decision["call_id"] = _call_identity(calls[0])[0]

        source_key = str(source or "gui").strip().lower()
        if source_key in REMOTE_CONFIRMATION_SOURCES:
            blocked = self._remote_block_reason(decision, calls)
            if blocked:
                return {
                    "ok": False,
                    "error_code": "local_confirmation_required",
                    "error": blocked,
                    "requires_local_confirmation": True,
                    "pending_id": pending_id,
                    "version": version,
                }
        return {
            "ok": True,
            "decision": decision,
            "pending_id": pending_id,
            "version": version,
        }

    def open_external(
        self,
        session_id: str,
        session: Any,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
        always_confirm: bool = False,
    ) -> ExternalConfirmationTicket | None:
        tool_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }
        # Opening a waiter and rechecking the live approval policy share the
        # resolver lock. A GUI toggle can therefore either release this ticket
        # or take effect before it is opened, without leaving a stale waiter.
        with self._lock:
            requirements = session._confirmation_requirements([tool_call])
            permission_key = ""
            if not requirements and always_confirm:
                # Unknown outside-agent tools still require an explicit grant.
                permission_key = str(session._mcp_permission_key(tool_name, arguments) or "")
                if not (permission_key and session._tool_auto_approved(tool_name, arguments)):
                    requirements = [{
                        "name": tool_name,
                        "arguments": dict(arguments),
                        "tool_call": tool_call,
                    }]
            if not requirements:
                return None
            pending_id = f"external-{uuid.uuid4().hex}"
            ticket = ExternalConfirmationTicket(
                session_id=session_id,
                pending_id=pending_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                permission_key=permission_key,
            )
            pending = {
                "external": True,
                "pending_id": pending_id,
                "version": 1,
                "confirmations": requirements,
                "tool_calls": [tool_call],
            }
            self._external[(session_id, pending_id)] = ticket
            session._pending = pending
            return ticket

    def resolve_external(
        self,
        session_id: str,
        session: Any,
        action: Any,
        *,
        source: str = "gui",
    ) -> dict[str, Any] | None:
        with self._lock:
            pending = getattr(session, "_pending", None)
            if not isinstance(pending, dict) or not pending.get("external"):
                return None
            # External waiters must bind to the card the client actually saw.
            # prepare() retains native legacy defaults, so validate before it
            # can supply the identity of a newer ticket to an unbound action.
            decision = normalize_confirmation_decision(action)
            if decision is None:
                return {"ok": False, "error_code": "invalid_confirmation", "error": "invalid confirmation action"}
            error = session._validate_confirmation_binding(
                decision, pending, allow_legacy_unbound=False,
            )
            if error:
                return {"ok": False, "error_code": "confirmation_changed", "error": error}
            prepared = self.prepare(session, decision, source=source)
            if not prepared.get("ok"):
                return prepared
            decision = prepared["decision"]
            pending_id = str(prepared.get("pending_id") or "")
            ticket = self._external.get((session_id, pending_id))
            if ticket is None or ticket.event.is_set():
                return {
                    "ok": False,
                    "error_code": "confirmation_expired",
                    "error": "external confirmation is no longer pending",
                }
            if decision.get("type") == "batch_confirmation_decision":
                verdict = str(dict(decision.get("decisions") or {}).get(ticket.call_id) or "")
                if not verdict:
                    return {
                        "ok": False, "error_code": "confirmation_changed",
                        "error": "the current external call is absent from this confirmation",
                    }
            else:
                verdict = str(decision.get("verdict") or "deny")
            session._record_confirmation_decision(decision, pending, timestamp=time.time())
            ticket.decision = verdict
            session._pending = None
            ticket.event.set()
            return {
                "ok": True,
                "resolved_external": True,
                "pending_id": pending_id,
                "verdict": ticket.decision,
            }

    def resolve_external_if_auto_approved(self, session_id: str, session: Any) -> dict[str, Any] | None:
        """Release a live waiter only under this session's current tool policy."""
        with self._lock:
            pending = getattr(session, "_pending", None)
            if not isinstance(pending, dict) or not pending.get("external"):
                return None
            pending_id = str(pending.get("pending_id") or "")
            ticket = self._external.get((session_id, pending_id))
            if ticket is None or ticket.event.is_set():
                return None
            if not session._tool_auto_approved(ticket.tool_name, ticket.arguments):
                return None
            receipt = {
                "pending_id": pending_id,
                "version": int(pending.get("version") or 1),
                "call_id": ticket.call_id,
                "verdict": "allow",
            }
            resolved = self.resolve_external(session_id, session, {
                "type": "confirmation_decision", **receipt,
            })
            return receipt if resolved and resolved.get("ok") else None

    def wait_external(
        self,
        ticket: ExternalConfirmationTicket,
        *,
        timeout_seconds: float = 600,
    ) -> str:
        ticket.event.wait(max(1.0, float(timeout_seconds)))
        with self._lock:
            self._external.pop((ticket.session_id, ticket.pending_id), None)
        return ticket.decision or "deny"

    def cancel_external(self, session_id: str, session: Any) -> int:
        """Deny and release every external waiter owned by one session."""
        released = 0
        with self._lock:
            for (ticket_session_id, _), ticket in list(self._external.items()):
                if ticket_session_id != session_id or ticket.event.is_set():
                    continue
                ticket.decision = "deny"
                ticket.event.set()
                released += 1
            pending = getattr(session, "_pending", None)
            if isinstance(pending, dict) and pending.get("external"):
                session._pending = None
        return released

    def _remote_block_reason(
        self,
        decision: dict[str, Any],
        calls: list[dict[str, Any]],
    ) -> str:
        verdicts: dict[str, str] = {}
        if decision.get("type") == "batch_confirmation_decision":
            verdicts = {
                str(key): str(value)
                for key, value in dict(decision.get("decisions") or {}).items()
            }
        else:
            verdicts[str(decision.get("call_id") or "")] = str(decision.get("verdict") or "")
        if any(value == "allow_session" for value in verdicts.values()):
            return "session-level automatic approval must be confirmed in the local GUI"
        if decision.get("auto_allow_names") or decision.get("auto_allow_permissions"):
            return "automatic approval changes must be confirmed in the local GUI"
        return ""
