"""Authenticated loopback broker exposing ArgusAI tools to ACP MCP proxies."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from acp import schema

from argusai.core.assistant.confirmation_broker import ConfirmationBroker
from argusai.core.assistant.streaming import EXTERNAL_AGENT_TOOL

PendingCallback = Callable[[dict[str, Any]], None]
ToolStartCallback = Callable[[str, dict[str, Any]], None]
ToolTraceCallback = Callable[[dict[str, Any]], None]
_PERMISSION_DETAIL_LIMIT = 800
# Advertised to the agent as the MCP server's name, and the fingerprint we look
# for when deciding whether a permission request is really about our own tools.
MCP_SERVER_NAME = "ArgusAI Tools"
# MCP-only gateway. Not a ToolRegistry name — local ACP agents search the
# catalogue with tool_search, then call this instead of receiving 200 schemas.
INVOKE_TOOL_NAME = "argusai_invoke_tool"
# Small enough for local models that reject a full MCP dump. Local Agents
# already have their own filesystem/shell; ArgusAI only needs discovery,
# host snapshot, and a way to reach the rest of the catalogue.
ACP_CORE_TOOL_NAMES: tuple[str, ...] = (
    "tool_search",
    "skill_list",
    "skill_search",
    "skill_read",
    "get_app_snapshot",
    "stop_for_user_decision",
    "mcp_server_list",
    "mcp_server_list_tools",
    "mcp_server_call_tool",
)
_INVOKE_TOOL = {
    "name": INVOKE_TOOL_NAME,
    "description": (
        "Call an ArgusAI catalogue tool that is not in this MCP server's "
        "top-level list. Use tool_search first to get the exact name and "
        "parameter schema, then pass that name and its arguments here."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact ArgusAI tool name from tool_search.",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments for that tool.",
            },
        },
        "required": ["name"],
    },
}


def _acp_workspace_mcp_hint(session: Any) -> str:
    binding = getattr(session, "runtime_binding", None)
    project = str(getattr(binding, "project_dir", "") or "").strip()
    if str(getattr(binding, "workspace_mode", "") or "") != "project" or not project:
        return ""
    return (
        f" This conversation's working directory is {project}. "
        "Use your own filesystem and shell tools there; ArgusAI sandbox_* "
        "is session scratch, not project source."
    )


def advertised_mcp_tool_names(registry: Any) -> list[str]:
    """Minimal MCP list for a local Agent.

    A full ArgusAI catalogue is too large for local models. Advertise the
    small core (search/snapshot/MCP wrappers) plus any custom names that are
    not part of the production catalogue. Everything else is reached with
    ``argusai_invoke_tool``. A scoped registry that contains none of those
    names is already a capability projection — list it as-is.
    """
    tools = getattr(registry, "_tools", {}) or {}
    from argusai.core.assistant.skills import TOOL_GROUPS
    from argusai.core.tools import EXECUTORS

    production = set(EXECUTORS) | {name for names in TOOL_GROUPS.values() for name in names}
    advertised: list[str] = []
    seen: set[str] = set()
    for name in ACP_CORE_TOOL_NAMES:
        if name in tools and name not in seen:
            advertised.append(name)
            seen.add(name)
    for name in sorted(tools):
        if name not in production and name not in seen:
            advertised.append(name)
            seen.add(name)
    if not advertised:
        return sorted(tools)
    return advertised


def needs_invoke_gateway(registry: Any) -> bool:
    advertised = set(advertised_mcp_tool_names(registry))
    tools = getattr(registry, "_tools", {}) or {}
    return any(name not in advertised for name in tools)


def _unwrap_invoke_payload(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | dict[str, Any]:
    """Return (inner_name, inner_args) or an error payload."""
    if name != INVOKE_TOOL_NAME:
        return name, arguments
    inner = str(arguments.get("name") or "").strip()
    inner_args = arguments.get("arguments")
    if not isinstance(inner_args, dict):
        inner_args = {}
    if not inner or inner == INVOKE_TOOL_NAME:
        return {
            "ok": False,
            "error_code": "unknown_tool",
            "error": "argusai_invoke_tool requires a catalogue tool name from tool_search",
        }
    return inner, inner_args


def _normalized(text: str) -> str:
    """Fold a label down to its letters and digits.

    Agents rewrite MCP names to suit their own syntax — ``ArgusAI Tools`` shows
    up as ``ArgusAI Tools-nmap_scan``, ``mcp__argusai_tools__nmap_scan`` and
    other spellings — so the separators are the part worth discarding.
    """
    return "".join(char for char in str(text or "").lower() if char.isalnum())


_MCP_SERVER_FINGERPRINT = _normalized(MCP_SERVER_NAME)


def _is_call_back_into_argusai(session: Any, tool_call: dict[str, Any]) -> bool:
    """True when the agent is asking to reach an ArgusAI tool through our proxy.

    The proxy confirms that call itself moments later, with the arguments and
    the risk tier attached. Asking here too puts two cards on screen for one
    action, and this is the one that carries none of the detail the decision
    needs. Letting it through grants nothing: the real gate is still ahead.
    """
    haystack = _normalized(
        f"{tool_call.get('title') or ''} "
        f"{json.dumps(tool_call.get('rawInput') or '', ensure_ascii=False)}"
    )
    if _MCP_SERVER_FINGERPRINT not in haystack:
        return False
    if _normalized(INVOKE_TOOL_NAME) in haystack:
        return True
    # Require one of our own tool names as well, so a project that merely
    # mentions ArgusAI cannot talk its way past the card.
    try:
        names = list(session.registry._tools)
    except Exception:
        return False
    return any(_normalized(name) in haystack for name in names)


def _permission_detail(tool_call: dict[str, Any]) -> str:
    """Summarize what the agent is about to run, for the confirmation card."""
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        command = raw_input.get("command") or raw_input.get("cmd")
        if isinstance(command, str) and command.strip():
            return command.strip()[:_PERMISSION_DETAIL_LIMIT]
    locations = tool_call.get("locations")
    if isinstance(locations, list):
        paths = [
            str(item.get("path") or "").strip()
            for item in locations
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ]
        if paths:
            return "\n".join(paths)[:_PERMISSION_DETAIL_LIMIT]
    if isinstance(raw_input, (dict, list)):
        return json.dumps(raw_input, ensure_ascii=False)[:_PERMISSION_DETAIL_LIMIT]
    return ""


@dataclass
class _SessionBinding:
    session_id: str
    session: Any
    confirmation_broker: ConfirmationBroker
    on_pending: PendingCallback | None = None
    on_tool_start: ToolStartCallback | None = None
    on_tool_trace: ToolTraceCallback | None = None
    on_checkpoint: Callable[[], None] | None = None
    confirmation_timeout_seconds: float = 604800
    execution_lock: threading.RLock = field(default_factory=threading.RLock)


class _BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class ArgusAIToolBroker:
    """Short-lived token broker bound only to 127.0.0.1."""

    def __init__(self) -> None:
        self._bindings: dict[str, _SessionBinding] = {}
        self._lock = threading.RLock()
        self._server: _BrokerHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        server = self._server
        if server is None:
            return ""
        return f"http://127.0.0.1:{server.server_port}"

    def start(self) -> None:
        if self._server is not None:
            return
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > 2_000_000:
                    self._write(413, {"ok": False, "error": "invalid request size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    self._write(400, {"ok": False, "error": "invalid JSON"})
                    return
                token = str(self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                binding = broker._binding_for_token(token)
                if binding is None:
                    self._write(401, {"ok": False, "error": "unauthorized"})
                    return
                try:
                    if self.path == "/v1/tools/list":
                        result = broker._list_tools(binding)
                    elif self.path == "/v1/tools/call":
                        result = broker._call_tool(binding, payload)
                    else:
                        self._write(404, {"ok": False, "error": "not found"})
                        return
                    self._write(200, result)
                except Exception as exc:
                    self._write(500, {
                        "ok": False,
                        "error_code": "tool_broker_error",
                        "error": f"{type(exc).__name__}: {exc}",
                    })

            def _write(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        server = _BrokerHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="argusai-acp-tool-broker",
        )
        self._server = server
        self._thread = thread
        thread.start()

    def register_session(
        self,
        session_id: str,
        session: Any,
        confirmation_broker: ConfirmationBroker,
        *,
        on_pending: PendingCallback | None = None,
        on_tool_start: ToolStartCallback | None = None,
        on_tool_trace: ToolTraceCallback | None = None,
        on_checkpoint: Callable[[], None] | None = None,
        confirmation_timeout_seconds: float = 604800,
    ) -> str:
        self.start()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._bindings[token] = _SessionBinding(
                session_id=session_id,
                session=session,
                confirmation_broker=confirmation_broker,
                on_pending=on_pending,
                on_tool_start=on_tool_start,
                on_tool_trace=on_tool_trace,
                on_checkpoint=on_checkpoint,
                confirmation_timeout_seconds=max(1.0, float(confirmation_timeout_seconds)),
            )
        return token

    def unregister(self, token: str) -> None:
        binding = None
        with self._lock:
            binding = self._bindings.pop(str(token or ""), None)
        if binding is not None:
            binding.confirmation_broker.cancel_external(
                binding.session_id,
                binding.session,
            )

    def shutdown(self) -> None:
        server = self._server
        self._server = None
        with self._lock:
            bindings = list(self._bindings.values())
            self._bindings.clear()
        for binding in bindings:
            binding.confirmation_broker.cancel_external(
                binding.session_id,
                binding.session,
            )
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.3)

    def permission_handler(self, token: str) -> Callable[[dict[str, Any]], str]:
        """Return a blocking callable that confirms an outside agent's own tool.

        ACP agents run their own tool set — Claude Code's ``Bash``, ``Edit`` and
        friends — and ask the client for permission over ``session/request_permission``.
        Those names mean nothing to ArgusAI's confirmation policy, so they are
        relayed under a single synthetic name that is confirmable by
        construction. The card, the pending projection and the session-level
        grant are the ones every other tool already uses.
        """

        def _handle(request: dict[str, Any]) -> str:
            binding = self._binding_for_token(str(token or ""))
            if binding is None:
                return "deny"
            with binding.execution_lock:
                return self._confirm_external_permission(binding, request)

        return _handle

    def _confirm_external_permission(
        self,
        binding: _SessionBinding,
        request: dict[str, Any],
    ) -> str:
        tool_call = request.get("tool_call")
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        if _is_call_back_into_argusai(binding.session, tool_call):
            # Session-wide, so the agent stops asking a question that has no
            # bearing on what actually runs.
            return "allow_session"
        arguments = {
            "agent": str(request.get("agent") or "").strip() or "external-agent",
            "kind": str(tool_call.get("kind") or "other").strip() or "other",
            "title": str(tool_call.get("title") or "").strip(),
            "detail": _permission_detail(tool_call),
        }
        call_id = str(tool_call.get("toolCallId") or uuid.uuid4().hex)
        ticket = binding.confirmation_broker.open_external(
            binding.session_id,
            binding.session,
            tool_name=EXTERNAL_AGENT_TOOL,
            arguments=arguments,
            call_id=call_id,
            always_confirm=True,
        )
        if ticket is None:
            return "allow"
        if binding.on_pending is not None:
            binding.on_pending({
                "pending_id": ticket.pending_id,
                "version": 1,
                "call_id": call_id,
                "name": EXTERNAL_AGENT_TOOL,
                "arguments": arguments,
            })
        if binding.on_checkpoint is not None:
            binding.on_checkpoint()
        verdict = binding.confirmation_broker.wait_external(
            ticket,
            timeout_seconds=binding.confirmation_timeout_seconds,
        )
        if verdict == "allow_session" and ticket.permission_key:
            binding.session.remember_permission(ticket.permission_key)
        return verdict or "deny"

    def _binding_for_token(self, token: str) -> _SessionBinding | None:
        if not token:
            return None
        with self._lock:
            for expected, binding in self._bindings.items():
                if hmac.compare_digest(token, expected):
                    return binding
        return None

    def _list_tools(self, binding: _SessionBinding) -> dict[str, Any]:
        registry = binding.session.registry
        by_name = registry._tools
        tools = []
        for name in advertised_mcp_tool_names(registry):
            tool = by_name.get(name)
            if tool is None:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {
                    "type": "object",
                    "properties": dict(tool.parameters),
                    "required": list(tool.required),
                },
            })
        if needs_invoke_gateway(registry):
            invoke = dict(_INVOKE_TOOL)
            hint = _acp_workspace_mcp_hint(binding.session)
            if hint:
                invoke["description"] = str(invoke.get("description") or "") + hint
            tools.append(invoke)
        tools.sort(key=lambda item: item["name"])
        return {"ok": True, "tools": tools}

    def _call_tool(self, binding: _SessionBinding, payload: dict[str, Any]) -> dict[str, Any]:
        # AssistantSession carries mutable pending/history state and is not
        # event-loop or thread safe. MCP clients may issue calls concurrently,
        # so one binding must execute and confirm tools strictly in order.
        with binding.execution_lock:
            return self._call_tool_serialized(binding, payload)

    def _call_tool_serialized(
        self,
        binding: _SessionBinding,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        unwrapped = _unwrap_invoke_payload(name, arguments)
        if isinstance(unwrapped, dict):
            return unwrapped
        name, arguments = unwrapped
        if binding.session.registry.get(name) is None:
            return {
                "ok": False,
                "error_code": "unknown_tool",
                "error": f"unknown ArgusAI tool: {name}",
            }
        call_id = str(payload.get("call_id") or uuid.uuid4().hex)
        ticket = binding.confirmation_broker.open_external(
            binding.session_id,
            binding.session,
            tool_name=name,
            arguments=arguments,
            call_id=call_id,
        )
        if ticket is not None:
            if binding.on_pending is not None:
                binding.on_pending({
                    "pending_id": ticket.pending_id,
                    "version": 1,
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                })
            if binding.on_checkpoint is not None:
                binding.on_checkpoint()
            verdict = binding.confirmation_broker.wait_external(
                ticket,
                timeout_seconds=binding.confirmation_timeout_seconds,
            )
            if verdict == "deny":
                return {
                    "ok": False,
                    "error_code": "tool_permission_denied",
                    "error": f"user denied tool call: {name}",
                }
            if verdict == "allow_session":
                binding.session.set_auto_approve_tool(name, True)
        if binding.on_tool_start is not None:
            try:
                binding.on_tool_start(name, arguments)
            except Exception:
                pass
        result = asyncio.run(
            binding.session._dispatch_tool(
                name,
                arguments,
                user_approved=ticket is not None,
            )
        )
        if hasattr(binding.session, "_auto_archive_tool_result"):
            result = binding.session._auto_archive_tool_result(name, arguments, result)
        trace = (
            binding.session._trace(name, arguments, result)
            if hasattr(binding.session, "_trace")
            else {
                "name": name,
                "arguments": arguments,
                "result": result,
                "ok": not (isinstance(result, dict) and result.get("error")),
            }
        )
        if binding.on_tool_trace is not None:
            try:
                binding.on_tool_trace(trace)
            except Exception:
                # Rendering telemetry must never turn a successful tool call
                # into an MCP failure seen by the external Agent.
                pass
        if binding.on_checkpoint is not None:
            binding.on_checkpoint()
        return {"ok": True, "result": result}

    def mcp_server(self, token: str) -> schema.McpServerStdio:
        if not self.url:
            raise RuntimeError("tool broker is not running")
        from argusai.core.infra.runtime_exec import self_relaunch_command

        command_argv = self_relaunch_command("--mcp-tool-proxy")
        return schema.McpServerStdio(
            name=MCP_SERVER_NAME,
            command=command_argv[0],
            args=list(command_argv[1:]),
            env=[
                schema.EnvVariable(name="ARGUSAI_TOOL_BROKER_URL", value=self.url),
                schema.EnvVariable(name="ARGUSAI_TOOL_BROKER_TOKEN", value=token),
            ],
        )
