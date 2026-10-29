from __future__ import annotations

from argusai.core.assistant.confirmation_broker import ConfirmationBroker
from argusai.core.assistant.stream_hub import StreamHub
from argusai.core.assistant.turn_router import AssistantTurnRequest, AssistantTurnRouter


class _PendingSession:
    def __init__(self, tool_name: str = "http_probe") -> None:
        self._pending = {
            "pending_id": "pending-1",
            "version": 2,
            "confirmations": [{
                "tool_call": {
                    "id": "call-1",
                    "function": {"name": tool_name, "arguments": "{}"},
                },
            }],
        }

    def _ensure_pending_identity(self) -> tuple[str, int]:
        return "pending-1", 2


def test_stream_hub_normalizes_and_replays_events() -> None:
    hub = StreamHub()
    hub.create("run-1", "session-1", owner="test")
    event = hub.publish("run-1", {"type": "delta", "text": "hello"})
    hub.finish("run-1")

    result = hub.read("run-1", 0)

    assert event is not None
    assert event["kind"] == "text_chunk"
    assert result["ok"] is True
    assert result["done"] is True
    assert result["events"][0]["text"] == "hello"
    assert result["record"]["owner"] == "test"


def test_turn_router_never_falls_back_for_unknown_runtime() -> None:
    router = AssistantTurnRouter()
    request = AssistantTurnRequest(session_id="s1", runtime_id="missing-acp")

    result = router.route_sync(
        request,
        native_handler=lambda _: {"ok": True, "text": "must not run"},
    )

    assert result["ok"] is False
    assert result["error_code"] == "assistant_runtime_unavailable"


def test_turn_router_routes_native_handler() -> None:
    router = AssistantTurnRouter()
    request = AssistantTurnRequest(session_id="s1", params={"message": "hello"})

    result = router.route_sync(
        request,
        native_handler=lambda routed: {"ok": True, "text": routed.params["message"]},
    )

    assert result["ok"] is True
    assert result["text"] == "hello"
    assert result["runtime_id"] == "argusai-native"


def test_confirmation_broker_fills_single_call_identity() -> None:
    result = ConfirmationBroker().prepare(
        _PendingSession(),
        {"type": "confirmation_decision", "verdict": "allow"},
        source="gui",
    )

    assert result["ok"] is True
    assert result["decision"]["call_id"] == "call-1"
    assert result["decision"]["pending_id"] == "pending-1"


def test_confirmation_broker_allows_explicit_sensitive_remote_tool() -> None:
    result = ConfirmationBroker().prepare(
        _PendingSession("run_shell_snippet"),
        {"type": "confirmation_decision", "verdict": "allow"},
        source="telegram",
    )

    assert result["ok"] is True
    assert result["decision"]["verdict"] == "allow"


def test_confirmation_broker_keeps_remote_session_auto_allow_local() -> None:
    result = ConfirmationBroker().prepare(
        _PendingSession("run_shell_snippet"),
        {"type": "confirmation_decision", "verdict": "allow_session"},
        source="telegram",
    )

    assert result["ok"] is False
    assert result["error_code"] == "local_confirmation_required"
