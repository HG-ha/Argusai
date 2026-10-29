"""ACP v1 process lifecycle and protocol handshake."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import acp
from acp import schema

from argusai import __version__
from argusai.core.infra.win_stdio import background_subprocess_kwargs

from .discovery import probe_agent_runtime
from .models import AgentRuntimeConfig

ACP_EVENT_SINK = Callable[[dict[str, Any]], Awaitable[None] | None]
# Blocking: it waits on a human. Returns "allow", "allow_session" or "deny".
ACP_PERMISSION_HANDLER = Callable[[dict[str, Any]], str]
_STDERR_LIMIT = 64 * 1024
_ALLOW_KINDS = ("allow_once", "allow_always")


def _pick_permission_option(options: list[Any], verdict: str) -> str:
    """Choose the agent-offered option that matches the user's verdict.

    A session-wide grant prefers ``allow_always`` so the agent stops asking; a
    one-off approval prefers ``allow_once`` so it does not silently become one.
    Either falls back to the other kind, because an agent is free to offer only
    one of them and refusing would strand the turn.
    """
    ranked = reversed(_ALLOW_KINDS) if verdict == "allow_session" else _ALLOW_KINDS
    by_kind: dict[str, str] = {}
    for option in options:
        payload = _json_value(option)
        if not isinstance(payload, dict):
            continue
        kind = str(payload.get("kind") or "")
        option_id = str(payload.get("optionId") or payload.get("option_id") or "")
        if kind and option_id:
            by_kind.setdefault(kind, option_id)
    for kind in ranked:
        if kind in by_kind:
            return by_kind[kind]
    return ""


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _normalize_initialize_response(value: Any) -> Any:
    """Repair unvalidated ACP model defaults before any serialization.

    Some ACP agents omit ``agentCapabilities.auth``. The current SDK default is
    a plain ``{}`` even though the field is typed as ``AgentAuthCapabilities``;
    Pydantic then emits a serializer warning every time the response is dumped.
    """
    capabilities = getattr(value, "agent_capabilities", None)
    auth = getattr(capabilities, "auth", None)
    if not isinstance(auth, dict):
        return value
    if not hasattr(capabilities, "model_copy") or not hasattr(value, "model_copy"):
        return value
    normalized_capabilities = capabilities.model_copy(update={
        "auth": schema.AgentAuthCapabilities.model_validate(auth),
    })
    return value.model_copy(update={
        "agent_capabilities": normalized_capabilities,
    })


class ACPClientCallbacks:
    """Conservative ACP client callbacks.

    The foundation advertises no filesystem or terminal capability. Permission
    requests go to ``permission_handler`` when the host supplied one, and are
    denied outright when it did not — an unattended caller must never be able to
    approve an outside agent's tool on the user's behalf.
    """

    def __init__(
        self,
        event_sink: ACP_EVENT_SINK | None = None,
        permission_handler: ACP_PERMISSION_HANDLER | None = None,
        *,
        agent_id: str = "",
    ) -> None:
        self.connection: Any = None
        self._event_sink = event_sink
        self._permission_handler = permission_handler
        self._agent_id = agent_id
        self._ordered_stream_updates = False

    def on_connect(self, conn: Any) -> None:
        self.connection = conn

    def enable_ordered_stream_updates(self) -> None:
        """Tell the routed callback to ignore duplicate SDK task dispatch."""
        self._ordered_stream_updates = True

    def observe_stream(self, event: Any) -> None:
        """Deliver session/update in raw NDJSON order before response handling.

        ACP's default dispatcher launches every notification in its own task.
        A fast agent can therefore resolve ``session/prompt`` before its final
        chunks run, or let neighboring chunks race. The low-level observer is
        synchronous in the receive loop, so it preserves both text order and
        the guarantee that all preceding updates exist when prompt returns.
        """
        if self._event_sink is None:
            return
        direction = str(getattr(event, "direction", "") or "").casefold()
        if direction and not direction.endswith("incoming"):
            return
        message = getattr(event, "message", None)
        if not isinstance(message, dict) or message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        payload = {
            "type": "acp_session_update",
            "session_id": str(params.get("sessionId") or params.get("session_id") or ""),
            "update": _json_value(params.get("update") or {}),
        }
        result = self._event_sink(payload)
        if isinstance(result, Awaitable):
            asyncio.get_running_loop().create_task(result)

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        if self._event_sink is None or self._ordered_stream_updates:
            return
        payload = {
            "type": "acp_session_update",
            "session_id": str(session_id),
            "update": _json_value(update),
        }
        result = self._event_sink(payload)
        if isinstance(result, Awaitable):
            await result

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **_: Any,
    ) -> acp.RequestPermissionResponse:
        denied = acp.RequestPermissionResponse(
            outcome=schema.DeniedOutcome(outcome="cancelled"),
        )
        handler = self._permission_handler
        if handler is None:
            return denied
        request = {
            "agent": self._agent_id,
            "session_id": str(session_id),
            "tool_call": _json_value(tool_call),
            "options": [_json_value(item) for item in options],
        }
        # The handler blocks on a human answering the confirmation card, so it
        # cannot run on this event loop: the agent's own session/update
        # notifications arrive on it and the card would render empty.
        try:
            verdict = await asyncio.to_thread(handler, request)
        except Exception:
            return denied
        if verdict not in {"allow", "allow_session"}:
            return denied
        option_id = _pick_permission_option(list(options), verdict)
        if not option_id:
            return denied
        return acp.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id=option_id),
        )

    async def create_elicitation(self, message: str, mode: Any, **_: Any) -> Any:
        del message, mode
        return acp.DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **_: Any) -> None:
        del elicitation_id

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise acp.RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    async def read_text_file(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("fs/write_text_file")

    async def create_terminal(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("terminal/create")

    async def terminal_output(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("terminal/output")

    async def release_terminal(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, *_: Any, **__: Any) -> Any:
        raise acp.RequestError.method_not_found("terminal/kill")


class ACPAgentProcess:
    """One shell-free ACP subprocess and its client-side connection."""

    def __init__(
        self,
        runtime: AgentRuntimeConfig,
        *,
        event_sink: ACP_EVENT_SINK | None = None,
        permission_handler: ACP_PERMISSION_HANDLER | None = None,
        path_env: str | None = None,
        cwd: str = "",
    ) -> None:
        self.runtime = runtime
        self.callbacks = ACPClientCallbacks(
            event_sink,
            permission_handler,
            agent_id=runtime.id,
        )
        self.path_env = path_env
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.connection: Any = None
        self.stderr_excerpt = ""
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> Any:
        if self.process is not None:
            return self.connection
        probe = probe_agent_runtime(self.runtime, path_env=self.path_env)
        if not probe.available or not probe.resolved_command:
            raise RuntimeError(probe.error or "ACP runtime is unavailable")
        # The session's directory has to win over the runtime default. Agents
        # discover their workspace rules (AGENTS.md, CLAUDE.md) from wherever
        # they were launched, so inheriting ArgusAI's own directory hands a
        # sandboxed session the ArgusAI checkout to read and obey.
        cwd = str(self.cwd or self.runtime.cwd or "").strip()
        if cwd and not Path(cwd).expanduser().is_dir():
            raise ValueError(f"ACP runtime cwd is not a directory: {cwd}")
        env = os.environ.copy()
        if self.path_env is not None:
            env["PATH"] = self.path_env
        env.update(self.runtime.env)
        process = await asyncio.create_subprocess_exec(
            probe.resolved_command,
            *self.runtime.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
            env=env,
            limit=4 * 1024 * 1024,
            **background_subprocess_kwargs(),
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            await process.wait()
            raise RuntimeError("ACP subprocess did not expose stdio")
        self.process = process
        self._stderr_task = asyncio.create_task(
            self._collect_stderr(),
            name=f"argusai-acp-stderr-{self.runtime.id}",
        )
        self.connection = acp.connect_to_agent(self.callbacks, process.stdin, process.stdout)
        raw_connection = getattr(self.connection, "_conn", None)
        add_observer = getattr(raw_connection, "add_observer", None)
        if callable(add_observer):
            add_observer(self.callbacks.observe_stream)
            self.callbacks.enable_ordered_stream_updates()
        return self.connection

    async def initialize(self, *, timeout_seconds: float = 12.0) -> Any:
        connection = await self.start()
        capabilities = schema.ClientCapabilities(
            fs=schema.FileSystemCapabilities(readTextFile=False, writeTextFile=False),
            terminal=False,
            auth=schema.AuthCapabilities(terminal=False),
        )
        info = schema.Implementation(name="argusai", title="ArgusAI", version=__version__)
        response = await asyncio.wait_for(
            connection.initialize(acp.PROTOCOL_VERSION, capabilities, info),
            timeout=max(1.0, float(timeout_seconds)),
        )
        return _normalize_initialize_response(response)

    async def _collect_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            if size < _STDERR_LIMIT:
                kept = chunk[: _STDERR_LIMIT - size]
                chunks.append(kept)
                size += len(kept)
        self.stderr_excerpt = b"".join(chunks).decode("utf-8", errors="replace").strip()

    async def close(self) -> None:
        connection, process = self.connection, self.process
        self.connection = None
        self.process = None
        # Shut the agent process down first. The ACP SDK closes its message
        # queue before cancelling the receive task; if the agent is still
        # writing session/update frames at that moment the receive loop raises
        # "message queue already closed" and logs a scary ERROR. Ending the
        # subprocess makes the NDJSON reader see EOF so the receive loop exits
        # cleanly, then connection.close() is just bookkeeping.
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                await _terminate_process_tree(process)
            with contextlib.suppress(Exception):
                await asyncio.sleep(0.05)
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()
        if self._stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._stderr_task, timeout=0.5)
            self._stderr_task = None

    async def __aenter__(self) -> "ACPAgentProcess":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform.startswith("win"):
        def _taskkill() -> None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **background_subprocess_kwargs(),
            )

        await asyncio.to_thread(_taskkill)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2.0)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def test_acp_runtime(
    runtime: AgentRuntimeConfig,
    *,
    timeout_seconds: float = 12.0,
    path_env: str | None = None,
) -> dict[str, Any]:
    """Launch one runtime only after an explicit test action and perform ACP initialize."""
    process = ACPAgentProcess(runtime, path_env=path_env)
    try:
        response = await process.initialize(timeout_seconds=timeout_seconds)
        payload = _json_value(response)
        return {
            "ok": True,
            "runtime_id": runtime.id,
            "protocol_version": payload.get("protocolVersion"),
            "agent_info": payload.get("agentInfo"),
            "agent_capabilities": payload.get("agentCapabilities"),
            "auth_methods": payload.get("authMethods") or [],
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "runtime_id": runtime.id,
            "error_code": "acp_handshake_timeout",
            "error": f"ACP initialize timed out after {timeout_seconds:g} seconds",
            "stderr": process.stderr_excerpt[-2000:],
        }
    except Exception as exc:
        return {
            "ok": False,
            "runtime_id": runtime.id,
            "error_code": "acp_handshake_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "stderr": process.stderr_excerpt[-2000:],
        }
    finally:
        await process.close()
