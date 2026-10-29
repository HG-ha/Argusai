"""UI-independent routing for native and external assistant turns."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from argusai.core.agent_runtimes.catalog import NATIVE_RUNTIME_ID

TurnHandler = Callable[["AssistantTurnRequest"], dict[str, Any] | Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AssistantTurnRequest:
    session_id: str
    runtime_id: str = NATIVE_RUNTIME_ID
    source: str = "gui"
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "AssistantTurnRequest":
        return cls(
            session_id=str(params.get("session_id") or "default"),
            runtime_id=str(params.get("runtime_id") or NATIVE_RUNTIME_ID),
            source=str(params.get("message_source") or params.get("source") or "gui"),
            params=dict(params),
        )


class AssistantTurnRouter:
    """Route one turn to its bound runtime without silent provider fallback."""

    def __init__(self) -> None:
        self._handlers: dict[str, TurnHandler] = {}

    def register(self, runtime_id: str, handler: TurnHandler, *, replace: bool = False) -> None:
        key = str(runtime_id or "").strip()
        if not key:
            raise ValueError("runtime_id is required")
        if key in self._handlers and not replace:
            raise ValueError(f"turn handler already registered: {key}")
        self._handlers[key] = handler

    def unregister(self, runtime_id: str) -> None:
        self._handlers.pop(str(runtime_id or "").strip(), None)

    def _resolve(
        self,
        request: AssistantTurnRequest,
        native_handler: TurnHandler | None,
        runtime_handler: TurnHandler | None,
    ) -> TurnHandler | None:
        if request.runtime_id == NATIVE_RUNTIME_ID and native_handler is not None:
            return native_handler
        if runtime_handler is not None:
            return runtime_handler
        return self._handlers.get(request.runtime_id)

    def route_sync(
        self,
        request: AssistantTurnRequest,
        *,
        native_handler: TurnHandler | None = None,
        runtime_handler: TurnHandler | None = None,
    ) -> dict[str, Any]:
        handler = self._resolve(request, native_handler, runtime_handler)
        if handler is None:
            return {
                "ok": False,
                "error_code": "assistant_runtime_unavailable",
                "error": f"no turn handler is available for runtime {request.runtime_id}",
                "runtime_id": request.runtime_id,
                "session_id": request.session_id,
            }
        result = handler(request)
        if inspect.isawaitable(result):
            raise RuntimeError("async turn handler used with route_sync")
        return {
            **dict(result),
            "runtime_id": request.runtime_id,
            "session_id": request.session_id,
        }

    async def route(
        self,
        request: AssistantTurnRequest,
        *,
        native_handler: TurnHandler | None = None,
        runtime_handler: TurnHandler | None = None,
    ) -> dict[str, Any]:
        handler = self._resolve(request, native_handler, runtime_handler)
        if handler is None:
            return {
                "ok": False,
                "error_code": "assistant_runtime_unavailable",
                "error": f"no turn handler is available for runtime {request.runtime_id}",
                "runtime_id": request.runtime_id,
                "session_id": request.session_id,
            }
        result = handler(request)
        if inspect.isawaitable(result):
            result = await result
        return {
            **dict(result),
            "runtime_id": request.runtime_id,
            "session_id": request.session_id,
        }
