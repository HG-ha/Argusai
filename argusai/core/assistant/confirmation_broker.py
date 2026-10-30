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
        requirements = session._confirmation_requirements([tool_call])
        permission_key = ""
        if not requirements and always_confirm:
            # The confirmation policy is a positive list of tools ArgusAI
            # registered, so a name it has never seen produces no requirement
            # and would run unasked. Callers relaying someone else's tool set
            # ask for this: unknown means confirmable, not approved.
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
        with self._lock:
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
        pending = getattr(session, "_pending", None)
        if not isinstance(pending, dict) or not pending.get("external"):
            return None
        prepared = self.prepare(session, action, source=source)
        if not prepared.get("ok"):
            return prepared
        pending_id = str(prepared.get("pending_id") or "")
        with self._lock:
            ticket = self._external.get((session_id, pending_id))
            if ticket is None:
                return {
                    "ok": False,
                    "error_code": "confirmation_expired",
                    "error": "external confirmation is no longer pending",
                }
            decision = prepared["decision"]
            if decision.get("type") == "batch_confirmation_decision":
                verdict = str(dict(decision.get("decisions") or {}).get(ticket.call_id) or "deny")
            else:
                verdict = str(decision.get("verdict") or "deny")
            ticket.decision = verdict
            ticket.event.set()
            session._pending = None
        return {
            "ok": True,
            "resolved_external": True,
            "pending_id": pending_id,
            "verdict": ticket.decision,
        }

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
