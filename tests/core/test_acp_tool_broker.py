from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from argusai.core.agent_runtimes import ArgusAIToolBroker
from argusai.core.assistant.confirmation_broker import ConfirmationBroker
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry


def test_stdio_mcp_proxy_lists_and_calls_session_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        "echo_test",
        "Echo one value.",
        {"value": {"type": "string"}},
        lambda value="": {"echo": value},
        required=["value"],
    )
    session = AssistantSession(registry=registry)
    broker = ArgusAIToolBroker()
    token = broker.register_session("session-1", session, ConfirmationBroker())
    descriptor = broker.mcp_server(token)
    env = os.environ.copy()
    env.update({item.name: item.value for item in descriptor.env})
    process = subprocess.Popen(
        [descriptor.command, *descriptor.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )

    def request(request_id: int, method: str, params: dict | None = None) -> dict:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    try:
        initialized = request(1, "initialize")
        listed = request(2, "tools/list")
        called = request(3, "tools/call", {
            "name": "echo_test",
            "arguments": {"value": "hello"},
        })
    finally:
        process.terminate()
        process.wait(timeout=5)
        broker.unregister(token)
        broker.shutdown()

    assert initialized["result"]["serverInfo"]["name"] == "argusai-tools"
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "echo_test" in names
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"] == {"echo": "hello"}


def test_tool_broker_serializes_calls_for_one_session() -> None:
    registry = ToolRegistry()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_echo(value=""):  # noqa: ANN001
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return {"echo": value}

    registry.register(
        "echo_test",
        "Echo one value slowly.",
        {"value": {"type": "string"}},
        slow_echo,
        required=["value"],
    )
    session = AssistantSession(registry=registry)
    broker = ArgusAIToolBroker()
    token = broker.register_session("session-serial", session, ConfirmationBroker())
    binding = broker._binding_for_token(token)
    assert binding is not None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda value: broker._call_tool(
                    binding,
                    {"name": "echo_test", "arguments": {"value": value}},
                ),
                ("one", "two"),
            ))
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert all(result["ok"] is True for result in results)
    assert max_active == 1


def test_tool_broker_emits_native_start_and_result_trace() -> None:
    registry = ToolRegistry()
    registry.register(
        "echo_test",
        "Echo one value.",
        {"value": {"type": "string"}},
        lambda value="": {"echo": value},
        required=["value"],
    )
    session = AssistantSession(registry=registry)
    starts: list[tuple[str, dict]] = []
    traces: list[dict] = []
    broker = ArgusAIToolBroker()
    token = broker.register_session(
        "session-trace",
        session,
        ConfirmationBroker(),
        on_tool_start=lambda name, arguments: starts.append((name, arguments)),
        on_tool_trace=traces.append,
    )
    binding = broker._binding_for_token(token)
    assert binding is not None

    try:
        result = broker._call_tool(
            binding,
            {"name": "echo_test", "arguments": {"value": "hello"}},
        )
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert result == {"ok": True, "result": {"echo": "hello"}}
    assert starts == [("echo_test", {"value": "hello"})]
    assert traces == [{
        "name": "echo_test",
        "arguments": {"value": "hello"},
        "result": {"echo": "hello"},
        "ok": True,
    }]


def test_external_confirmation_can_resolve_while_tool_call_is_waiting() -> None:
    registry = ToolRegistry()
    registry.register(
        "run_shell_snippet",
        "Test confirmation bridge.",
        {"script": {"type": "string"}},
        lambda script="": {"script": script},
        required=["script"],
    )
    session = AssistantSession(registry=registry)
    confirmations = ConfirmationBroker()
    pending_seen = threading.Event()
    broker = ArgusAIToolBroker()
    token = broker.register_session(
        "session-confirm",
        session,
        confirmations,
        on_pending=lambda _pending: pending_seen.set(),
        confirmation_timeout_seconds=5,
    )
    binding = broker._binding_for_token(token)
    assert binding is not None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                broker._call_tool,
                binding,
                {
                    "name": "run_shell_snippet",
                    "arguments": {"script": "echo safe"},
                    "call_id": "call-confirm",
                },
            )
            assert pending_seen.wait(timeout=2)
            projection = session.pending_confirmation_projection()
            pending = projection["pending_tool"]
            assert pending["id"] == "call-confirm"

            resolved = confirmations.resolve_external(
                "session-confirm",
                session,
                {
                    "type": "confirmation_decision",
                    "call_id": pending["id"],
                    "pending_id": pending["pending_id"],
                    "version": pending["version"],
                    "verdict": "allow",
                },
                source="gui",
            )
            result = future.result(timeout=2)
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert resolved == {
        "ok": True,
        "resolved_external": True,
        "pending_id": pending["pending_id"],
        "verdict": "allow",
    }
    assert result["ok"] is True
    assert result["result"] == {"script": "echo safe"}


def _permission_request(kind: str = "execute") -> dict:
    return {
        "agent": "claude-code",
        "session_id": "acp-1",
        "tool_call": {
            "toolCallId": "call-bash",
            "kind": kind,
            "title": "Run a shell command",
            "rawInput": {"command": "rm -rf /tmp/x"},
        },
        "options": [],
    }


def _answer(confirmations, session, verdict: str) -> None:
    pending = session.pending_confirmation_projection()["pending_tool"]
    confirmations.resolve_external(
        "session-perm",
        session,
        {
            "type": "confirmation_decision",
            "call_id": pending["id"],
            "pending_id": pending["pending_id"],
            "version": pending["version"],
            "verdict": verdict,
        },
        source="gui",
    )


def test_an_outside_agents_tool_still_raises_a_confirmation_card() -> None:
    """The confirmation policy lists tools we registered, and `Bash` is not one.

    Without an explicit rule for names it has never seen, the policy produces no
    requirement and the outside agent's tool would run unasked.
    """
    session = AssistantSession(registry=ToolRegistry())
    confirmations = ConfirmationBroker()
    pending_seen = threading.Event()
    broker = ArgusAIToolBroker()
    token = broker.register_session(
        "session-perm",
        session,
        confirmations,
        on_pending=lambda _pending: pending_seen.set(),
        confirmation_timeout_seconds=5,
    )
    handler = broker.permission_handler(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, _permission_request())
            assert pending_seen.wait(timeout=2)
            card = session.pending_confirmation_projection()["pending_blocks"][0]
            _answer(confirmations, session, "allow")
            verdict = future.result(timeout=2)
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert verdict == "allow"
    assert card["title"] == "Run a shell command"
    assert "claude-code" in card["body"]
    assert "rm -rf /tmp/x" in card["body"]
    assert card["permission_key"] == "acp:claude-code:execute"


def test_a_session_grant_covers_one_kind_and_not_the_others() -> None:
    """`allow_session` on a read must not also approve every future shell call."""
    session = AssistantSession(registry=ToolRegistry())
    confirmations = ConfirmationBroker()
    pending_seen = threading.Event()
    broker = ArgusAIToolBroker()
    token = broker.register_session(
        "session-perm",
        session,
        confirmations,
        on_pending=lambda _pending: pending_seen.set(),
        confirmation_timeout_seconds=5,
    )
    handler = broker.permission_handler(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, _permission_request("read"))
            assert pending_seen.wait(timeout=2)
            _answer(confirmations, session, "allow_session")
            granted = future.result(timeout=2)

            pending_seen.clear()
            repeat = pool.submit(handler, _permission_request("read")).result(timeout=2)
            assert not pending_seen.is_set()

            other = pool.submit(handler, _permission_request("execute"))
            assert pending_seen.wait(timeout=2)
            _answer(confirmations, session, "deny")
            refused = other.result(timeout=2)
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert granted == "allow_session"
    assert session.auto_approve_permissions() == ["acp:claude-code:read"]
    assert repeat == "allow"
    assert refused == "deny"


def test_reaching_an_argusai_tool_is_not_confirmed_twice() -> None:
    """Cursor asks to use our MCP server, then the proxy asks about the tool.

    Both cards describe one nmap scan, and only the second one carries the
    target, the port range and the risk tier.
    """
    registry = ToolRegistry()
    registry.register(
        "nmap_scan",
        "Scan ports.",
        {"target": {"type": "string"}},
        lambda target="": {"target": target},
        required=["target"],
    )
    session = AssistantSession(registry=registry)
    broker = ArgusAIToolBroker()
    pending_seen = threading.Event()
    token = broker.register_session(
        "session-perm",
        session,
        ConfirmationBroker(),
        on_pending=lambda _pending: pending_seen.set(),
        confirmation_timeout_seconds=5,
    )

    try:
        verdict = broker.permission_handler(token)({
            "agent": "cursor-acp",
            "tool_call": {
                "toolCallId": "call-mcp",
                "kind": "other",
                "title": "ArgusAI Tools-nmap_scan: nmap_scan",
            },
            "options": [],
        })
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert verdict == "allow_session"
    assert not pending_seen.is_set()


def test_an_agents_own_tool_is_not_waved_through_by_a_stray_mention() -> None:
    """Only our server name AND one of our tool names together mean it is ours."""
    registry = ToolRegistry()
    registry.register(
        "nmap_scan",
        "Scan ports.",
        {"target": {"type": "string"}},
        lambda target="": {"target": target},
        required=["target"],
    )
    session = AssistantSession(registry=registry)
    confirmations = ConfirmationBroker()
    pending_seen = threading.Event()
    broker = ArgusAIToolBroker()
    token = broker.register_session(
        "session-perm",
        session,
        confirmations,
        on_pending=lambda _pending: pending_seen.set(),
        confirmation_timeout_seconds=5,
    )
    handler = broker.permission_handler(token)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(handler, {
                "agent": "cursor-acp",
                "tool_call": {
                    "toolCallId": "call-shell",
                    "kind": "execute",
                    "title": "Bash: grep -r argusai tools/",
                    "rawInput": {"command": "grep -r argusai tools/"},
                },
                "options": [],
            })
            assert pending_seen.wait(timeout=2)
            _answer(confirmations, session, "deny")
            verdict = future.result(timeout=2)
    finally:
        broker.unregister(token)
        broker.shutdown()

    assert verdict == "deny"


def test_a_permission_request_without_a_live_binding_is_refused() -> None:
    broker = ArgusAIToolBroker()
    try:
        assert broker.permission_handler("not-a-token")(_permission_request()) == "deny"
    finally:
        broker.shutdown()


def test_tool_broker_lists_core_tools_not_full_catalogue() -> None:
    from argusai.core.agent_runtimes.tool_broker import INVOKE_TOOL_NAME
    from argusai.core.assistant.tools import build_tool_registry

    registry = build_tool_registry(None)
    session = AssistantSession(registry=registry)
    broker = ArgusAIToolBroker()
    token = broker.register_session("session-core", session, ConfirmationBroker())
    try:
        listed = broker._list_tools(broker._binding_for_token(token))
        names = {tool["name"] for tool in listed["tools"]}
        assert "tool_search" in names
        assert INVOKE_TOOL_NAME in names
        assert "nmap_scan" not in names
        assert "cron_upsert" not in names
        assert len(names) < 20
        assert len(registry._tools) > 80
        invoked = broker._call_tool(broker._binding_for_token(token), {
            "name": INVOKE_TOOL_NAME,
            "arguments": {"name": "list_checks", "arguments": {}},
        })
        assert invoked["ok"] is True
        missing = broker._call_tool(broker._binding_for_token(token), {
            "name": INVOKE_TOOL_NAME,
            "arguments": {"name": "", "arguments": {}},
        })
        assert missing["ok"] is False
    finally:
        broker.unregister(token)
        broker.shutdown()


def test_tool_broker_invoke_describes_bound_project_dir() -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    from argusai.core.agent_runtimes.tool_broker import INVOKE_TOOL_NAME
    from argusai.core.assistant.tools import build_tool_registry

    registry = build_tool_registry(None)
    session = AssistantSession(registry=registry)
    session.runtime_binding = normalize_session_runtime_binding({
        "runtime_id": "codex-acp",
        "workspace_mode": "project",
        "project_dir": "H:/work/demo",
    })
    broker = ArgusAIToolBroker()
    token = broker.register_session("session-project", session, ConfirmationBroker())
    try:
        listed = broker._list_tools(broker._binding_for_token(token))
        invoke = next(tool for tool in listed["tools"] if tool["name"] == INVOKE_TOOL_NAME)
        assert "H:/work/demo" in invoke["description"]
        assert "sandbox_*" in invoke["description"]
    finally:
        broker.unregister(token)
        broker.shutdown()
