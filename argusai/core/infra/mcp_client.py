"""MCP transport adapters and schema/result hardening.

This module is the Adapter layer of Loom22 §5 for ArgusAI:

- :class:`MCPTransportSpec` describes one server's launch / connection
  parameters in a transport-agnostic way (``stdio`` / ``streamable_http``
  / ``sse``) plus the per-server trust policy.
- :class:`MCPStdioClient`, :class:`MCPStreamableHttpClient`,
  :class:`MCPSseClient` are concrete adapters. They expose the same four
  methods (``list_tools``, ``call_tool``, ``list_resources``,
  ``read_resource``) so callers don't need protocol-specific branches.
- :func:`make_mcp_client` returns the right adapter for a spec.
- :func:`sanitize_mcp_schema` and :func:`sanitize_mcp_arguments` strip
  unsupported JSON-Schema features and oversized payloads, per §5.6.
- :func:`mcp_error_payload` produces the structured
  ``{ok, error_code, retryable, suggested_fix, source}`` shape from §5.7.
- :func:`mcp_runtime_for_stdio` returns the Node binary / source label
  used for stdio launches so the GUI can show "vendor / system / user"
  attribution.

This module is **only** the protocol/adapter layer. It does NOT touch
the assistant session, ToolRegistry, or settings — :mod:`argusai.core.
tools.case_mcp` is the place where per-server policy decisions and
artifact archival happen.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from argusai import __version__
from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.security.mcp_provision import (
    lint_mcp_launch,
    normalize_provenance,
    redact_launch_args,
)


# ---------------------------------------------------------------------------
# Transport spec + policy plumbing
# ---------------------------------------------------------------------------


def _default_policy() -> dict[str, Any]:
    return {
        "default_risk": "high",
        "allow_tools": [],
        "deny_tools": [],
        "tool_risks": {},
        "allow_auto_approve": False,
        "require_confirmation_for_all": True,
        "max_timeout_seconds": 60,
        "max_result_bytes": 200_000,
        "allow_insecure_http": False,
    }


@dataclass
class MCPTransportSpec:
    """Transport-agnostic launch / connection spec for one MCP server.

    Either ``command`` (stdio) or ``url`` (http/sse) must be populated
    depending on ``transport``. All other fields are optional and have
    safe defaults.
    """

    name: str
    transport: str = "stdio"
    server_id: str = ""
    # stdio fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # http / sse fields
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # shared
    timeout_seconds: int = 20
    policy: dict[str, Any] = field(default_factory=_default_policy)
    provenance: dict[str, Any] = field(default_factory=dict)
    # Ephemeral launch context, never persisted in settings.
    runtime_tools_dir: str = ""
    runtime_cwd: str = ""

    @property
    def safe_name(self) -> str:
        text = (self.name or self.server_id or "external").strip()
        return text or "external"

    def effective_timeout(self) -> int:
        """Per-call timeout clamped by the policy's ``max_timeout_seconds``."""
        raw = max(1, int(self.timeout_seconds or 20))
        limit = max(1, int((self.policy or {}).get("max_timeout_seconds") or 60))
        return min(raw, limit)

    def source_block(self, *, tool: str = "", uri: str = "") -> dict[str, Any]:
        block: dict[str, Any] = {
            "protocol": "mcp",
            "transport": self.transport,
            "server": self.safe_name,
        }
        if self.server_id:
            block["server_id"] = self.server_id
        if tool:
            block["tool"] = tool
        if uri:
            block["uri"] = uri
        if self.provenance:
            block["provenance"] = normalize_provenance(self.provenance)
        return block


# Bare Node-family command names resolve only against the managed runtime.
NODE_LAUNCH_NAMES = {"node", "npm", "npx", "yarn", "pnpm", "pnpx"}


def lint_mcp_stdio_args(command: str, args: list[Any] | None = None) -> dict[str, Any]:
    """Return a secret-free safety verdict for a stdio MCP launch."""
    return lint_mcp_launch(command, args or [])


# Compatibility-friendly short name for callers that think in terms of args.
lint_mcp_args = lint_mcp_stdio_args


# ---------------------------------------------------------------------------
# Spec construction helpers
# ---------------------------------------------------------------------------


def spec_from_server_dict(
    server: dict[str, Any],
    *,
    runtime_tools_dir: str = "",
    runtime_cwd: str = "",
) -> MCPTransportSpec:
    """Build a transport spec from a normalised ``settings.mcp_servers`` entry."""
    raw_policy = server.get("policy") if isinstance(server.get("policy"), dict) else {}
    policy = {**_default_policy(), **raw_policy}
    return MCPTransportSpec(
        name=str(server.get("name") or server.get("id") or "external"),
        transport=str(server.get("transport") or "stdio").lower(),
        server_id=str(server.get("id") or ""),
        command=str(server.get("command") or ""),
        args=[str(x) for x in (server.get("args") or []) if str(x)],
        env={str(k): str(v) for k, v in (server.get("env") or {}).items()},
        cwd=str(server.get("cwd") or ""),
        url=str(server.get("url") or ""),
        headers={str(k): str(v) for k, v in (server.get("headers") or {}).items()},
        timeout_seconds=int(server.get("timeout_seconds") or 20),
        policy=policy,
        provenance=normalize_provenance(server.get("provenance")),
        runtime_tools_dir=str(runtime_tools_dir or ""),
        runtime_cwd=str(runtime_cwd or ""),
    )


def parse_server_spec(
    *,
    server_name: str,
    command: str = "",
    args: list[Any] | None = None,
    env_json: str = "{}",
    cwd: str = "",
    timeout_seconds: int = 20,
    transport: str = "stdio",
    url: str = "",
    headers_json: str = "{}",
    policy: dict[str, Any] | None = None,
    runtime_tools_dir: str = "",
    runtime_cwd: str = "",
) -> MCPTransportSpec:
    """Parse user/model supplied launch fields for the legacy ``mcp_stdio_*`` path."""
    try:
        env_obj = json.loads(env_json or "{}")
        env = {str(k): str(v) for k, v in env_obj.items()} if isinstance(env_obj, dict) else {}
    except Exception:
        env = {}
    try:
        headers_obj = json.loads(headers_json or "{}")
        headers = {str(k): str(v) for k, v in headers_obj.items()} if isinstance(headers_obj, dict) else {}
    except Exception:
        headers = {}
    transport_norm = str(transport or "stdio").lower()
    if transport_norm not in ("stdio", "streamable_http", "sse"):
        transport_norm = "stdio"
    return MCPTransportSpec(
        name=str(server_name or "external").strip() or "external",
        transport=transport_norm,
        command=str(command or "").strip(),
        args=[str(x) for x in (args or []) if str(x)],
        env=env,
        cwd=str(cwd or "").strip(),
        url=str(url or "").strip(),
        headers=headers,
        timeout_seconds=max(1, min(int(timeout_seconds or 20), 120)),
        policy={**_default_policy(), **(policy or {})},
        provenance={},
        runtime_tools_dir=str(runtime_tools_dir or ""),
        runtime_cwd=str(runtime_cwd or ""),
    )


# Back-compat alias for the dataclass name used in the rest of the
# codebase before the transport refactor. Imports should be updated
# gradually, but legacy callers continue to work.
MCPServerSpec = MCPTransportSpec


# ---------------------------------------------------------------------------
# Schema / arguments sanitization (Loom22 §5.6)
# ---------------------------------------------------------------------------

_SUPPORTED_SCHEMA_KEYS = {
    "type",
    "description",
    "enum",
    "default",
    "items",
    "properties",
    "required",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}
_SUPPORTED_SCHEMA_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}
_SCHEMA_DESCRIPTION_CAP = 2000
_SCHEMA_MAX_DEPTH = 8
_SCHEMA_MAX_PROPERTIES = 64
_ARG_STRING_CAP = 50_000
_ARG_ARRAY_CAP = 500
_ARG_MAX_DEPTH = 8


def _truncate_text(value: Any, cap: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= cap:
        return text
    return text[: cap - 16] + "...[truncated]"


def sanitize_mcp_schema(schema: Any, *, _depth: int = 0) -> dict[str, Any]:
    """Reduce a third-party MCP ``inputSchema`` to a model-safe subset.

    - Whitelists JSON-Schema keys we know the model layer can use.
    - Coerces unknown ``type`` values to ``string`` (still tool-callable).
    - Caps descriptions so a malicious / sloppy MCP server can't pump a
      large prompt into the tool-schema channel.
    - Recurses into ``properties`` / ``items`` with a depth cap.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    if _depth >= _SCHEMA_MAX_DEPTH:
        return {"type": "object", "properties": {}}

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SUPPORTED_SCHEMA_KEYS:
            continue
        if key == "type":
            t = str(value or "").strip().lower()
            out["type"] = t if t in _SUPPORTED_SCHEMA_TYPES else "string"
        elif key == "description":
            out["description"] = _truncate_text(value, _SCHEMA_DESCRIPTION_CAP)
        elif key == "enum":
            if isinstance(value, list):
                out["enum"] = value[:64]
        elif key == "items":
            out["items"] = sanitize_mcp_schema(value, _depth=_depth + 1)
        elif key == "properties":
            if isinstance(value, dict):
                cleaned: dict[str, Any] = {}
                for prop_name, prop_schema in list(value.items())[:_SCHEMA_MAX_PROPERTIES]:
                    cleaned[str(prop_name)] = sanitize_mcp_schema(prop_schema, _depth=_depth + 1)
                out["properties"] = cleaned
        elif key == "required":
            if isinstance(value, list):
                out["required"] = [str(x) for x in value if x][:_SCHEMA_MAX_PROPERTIES]
        elif key in {"minimum", "maximum"}:
            try:
                out[key] = float(value) if isinstance(value, float) else int(value)
            except (TypeError, ValueError):
                continue
        elif key in {"minLength", "maxLength", "minItems", "maxItems"}:
            try:
                out[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        elif key == "default":
            out["default"] = value

    if "type" not in out:
        out["type"] = "object" if "properties" in out else "string"
    return out


def sanitize_mcp_arguments(arguments: Any, *, _depth: int = 0) -> Any:
    """Strip oversized values from model-supplied tool arguments.

    Does NOT enforce a schema match — the MCP server is the source of
    truth for that. We only put soft caps to prevent the AI from
    sending a 5 MB string just because the MCP server's schema allows
    it.
    """
    if _depth >= _ARG_MAX_DEPTH:
        return None
    if arguments is None or isinstance(arguments, bool):
        return arguments
    if isinstance(arguments, (int, float)):
        return arguments
    if isinstance(arguments, str):
        return _truncate_text(arguments, _ARG_STRING_CAP)
    if isinstance(arguments, list):
        return [sanitize_mcp_arguments(item, _depth=_depth + 1) for item in arguments[:_ARG_ARRAY_CAP]]
    if isinstance(arguments, dict):
        return {str(k): sanitize_mcp_arguments(v, _depth=_depth + 1) for k, v in arguments.items()}
    # Fallback: stringify and clip so a non-JSON-serialisable wrapper
    # type cannot crash json.dumps later on.
    return _truncate_text(arguments, _ARG_STRING_CAP)


# ---------------------------------------------------------------------------
# Structured error payloads (Loom22 §5.7)
# ---------------------------------------------------------------------------


def mcp_error_payload(
    spec: MCPTransportSpec,
    *,
    error_code: str,
    error: str,
    retryable: bool,
    suggested_fix: str = "",
    tool: str = "",
    uri: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": error_code,
        "error": error,
        "retryable": retryable,
        "suggested_fix": suggested_fix,
        "source": spec.source_block(tool=tool, uri=uri),
    }
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    return payload


def classify_exception(exc: BaseException) -> tuple[str, bool, str]:
    """Map a Python exception into ``(error_code, retryable, suggested_fix)``."""
    if isinstance(exc, asyncio.TimeoutError):
        return ("timeout", True, "Increase timeout_seconds or narrow the query.")
    if isinstance(exc, FileNotFoundError):
        return (
            "runtime_missing",
            False,
            "The MCP launch command was not found on PATH. Install the runtime or use an absolute path.",
        )
    if isinstance(exc, PermissionError):
        return ("permission_denied", False, "Check filesystem / runtime permissions.")
    if isinstance(exc, ConnectionRefusedError):
        return ("connection_refused", True, "MCP endpoint refused the connection — verify URL and that the server is running.")
    name = type(exc).__name__
    return (f"adapter_error:{name}", True, "")


# ---------------------------------------------------------------------------
# MCP wire-level helpers
# ---------------------------------------------------------------------------


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _normalize_content(content: Any) -> dict[str, Any]:
    """Normalize MCP content into a compact, stable tool result payload."""
    items = content if isinstance(content, list) else [content]
    normalized: list[dict[str, Any]] = []
    text_parts: list[str] = []
    resources: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            text = str(item)
            normalized.append({"type": "text", "text": text})
            text_parts.append(text)
            continue
        kind = str(item.get("type") or "unknown")
        clean: dict[str, Any] = {"type": kind}
        if kind == "text":
            text = str(item.get("text") or "")
            clean["text"] = text
            text_parts.append(text)
        elif kind in {"resource", "resource_link"}:
            uri = str(item.get("uri") or item.get("resource", {}).get("uri") or "")
            clean["uri"] = uri
            clean["name"] = str(item.get("name") or item.get("resource", {}).get("name") or "")
            if uri:
                resources.append(clean)
        elif kind == "image":
            clean["mimeType"] = str(item.get("mimeType") or "")
            clean["data_preview"] = str(item.get("data") or "")[:160]
            clean["truncated"] = bool(item.get("data"))
        else:
            clean["data"] = item
        normalized.append(clean)
    summary = "\n".join(part for part in text_parts if part).strip()
    return {
        "content": normalized,
        "summary": summary[:4000],
        "resources": resources,
        "truncated": len(summary) > 4000,
    }


def _serialised_size(result: dict[str, Any]) -> int:
    try:
        return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Runtime resolution for stdio MCP launches
# ---------------------------------------------------------------------------


def _managed_node_dir(tools_dir: str = "") -> str | None:
    """Return the healthy managed Node root, never a system fallback."""
    try:
        from argusai.core.managed_runtimes import runtime_root, runtime_status
        from argusai.core.paths import PathManager
    except Exception:
        return None
    try:
        base = Path(tools_dir).expanduser() if tools_dir else PathManager().tools_dir
        status = runtime_status("node", base)
    except Exception:
        return None
    if not status.get("healthy"):
        return None
    candidate = runtime_root("node", base)
    return str(candidate) if candidate.exists() else None


def _is_bare_command(command: str) -> bool:
    """``True`` when the command has no path separators (just a name)."""
    text = (command or "").strip()
    if not text:
        return False
    if os.sep in text:
        return False
    if "/" in text:
        return False
    # Don't touch absolute paths like ``C:\bin\node.exe``
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    return True


def _resolve_node_launch_command(
    command: str,
    env: dict[str, str],
    tools_dir: str = "",
) -> tuple[str, str, str]:
    """Resolve a bare Node command exclusively against the managed runtime.

    Returns ``(resolved_command, node_source, runtime_dir)``. ``node_source``
    is one of ``managed`` / ``missing`` / ``passthrough``. ``runtime_dir`` is
    the directory we should prepend to ``PATH`` so child ``npx`` invocations
    find the managed ``node`` / ``npm`` siblings.
    """
    name = (command or "").strip()
    if not name:
        return command, "passthrough", ""
    base = name.lower()
    if sys.platform.startswith("win") and base.endswith(".exe"):
        base = base[:-4]
    if base not in NODE_LAUNCH_NAMES:
        return command, "passthrough", ""
    managed_dir = _managed_node_dir(tools_dir)
    if managed_dir:
        candidate = _node_sibling_in_dir(managed_dir, command)
        if candidate:
            command_dir = str(Path(candidate).parent)
            return candidate, "managed", command_dir
    return command, "missing", ""


def _node_sibling_in_dir(directory: str, command: str) -> str | None:
    """Find ``command`` (with platform-appropriate extension) inside ``directory``."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    candidates = [cmd]
    if sys.platform.startswith("win"):
        if not cmd.lower().endswith((".exe", ".cmd", ".bat", ".ps1")):
            candidates = [cmd + ".cmd", cmd + ".exe", cmd]
    for base in (directory, os.path.join(directory, "bin")):
        for entry in candidates:
            candidate = os.path.join(base, entry)
            if os.path.exists(candidate):
                return candidate
    return None


def mcp_runtime_for_stdio(spec: MCPTransportSpec) -> dict[str, Any]:
    """Inspect the launch command without actually starting the server.

    Returns ``{"transport": "stdio", "node_source": ..., "runtime_dir": ...,
    "command_resolved": ..., "node_version": ...}`` for the GUI to show
    "using managed Node v22.x" attribution.
    """
    if spec.transport != "stdio":
        return {"transport": spec.transport}
    resolved, source, runtime_dir = _resolve_node_launch_command(
        spec.command,
        spec.env,
        spec.runtime_tools_dir,
    )
    info: dict[str, Any] = {
        "transport": "stdio",
        "node_source": source,
        "runtime_dir": runtime_dir,
        "command_resolved": resolved if resolved != spec.command else "",
    }
    if source == "managed" and runtime_dir:
        node_bin = _node_sibling_in_dir(runtime_dir, "node")
        if node_bin:
            info["node_path"] = node_bin
    return info


def _stdio_subprocess_env(spec: MCPTransportSpec, runtime_dir: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(spec.env or {})
    if runtime_dir:
        existing = env.get("PATH", "")
        env["PATH"] = f"{runtime_dir}{os.pathsep}{existing}" if existing else runtime_dir
    sandbox = Path(spec.runtime_cwd).expanduser() if spec.runtime_cwd else None
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
        temp = sandbox / ".tmp" / "mcp"
        cache = sandbox / ".cache" / "node"
        temp.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(sandbox)
        env["USERPROFILE"] = str(sandbox)
        env["TMP"] = str(temp)
        env["TEMP"] = str(temp)
        env["npm_config_cache"] = str(cache)
    return env


# ---------------------------------------------------------------------------
# stdio client (JSON-RPC over a child process)
# ---------------------------------------------------------------------------


class MCPStdioClient:
    """Short-lived JSON-RPC client for one stdio MCP server invocation."""

    def __init__(self, spec: MCPTransportSpec) -> None:
        if spec.transport != "stdio":
            raise ValueError(f"MCPStdioClient requires transport=stdio, got {spec.transport!r}")
        self.spec = spec
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self.initialize_result: dict[str, Any] = {}
        self.runtime_info: dict[str, Any] = {}

    async def __aenter__(self) -> "MCPStdioClient":
        launch_lint = lint_mcp_stdio_args(self.spec.command, self.spec.args)
        if not launch_lint.get("ok"):
            codes = ", ".join(
                str(item.get("code") or "unsafe_argument")
                for item in launch_lint.get("errors") or []
                if isinstance(item, dict)
            )
            raise ValueError(f"Unsafe MCP stdio launch rejected ({codes or 'unsafe_argument'}).")
        resolved, source, runtime_dir = _resolve_node_launch_command(
            self.spec.command,
            self.spec.env,
            self.spec.runtime_tools_dir,
        )
        if source == "missing":
            raise FileNotFoundError(
                f"MCP launch command not found: {self.spec.command!r}. "
                "Install the managed Node sandbox environment and retry."
            )
        env = _stdio_subprocess_env(self.spec, runtime_dir)
        self.runtime_info = {
            "transport": "stdio",
            "node_source": source,
            "runtime_dir": runtime_dir,
            "command_resolved": resolved if resolved != self.spec.command else "",
        }
        self._proc = await asyncio.create_subprocess_exec(
            resolved,
            *self.spec.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.spec.cwd or self.spec.runtime_cwd or None,
            env=env,
            **quiet_subprocess_kwargs(),
        )
        try:
            self.initialize_result = await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "argusai", "version": __version__},
            })
            await self._notify("notifications/initialized", {})
            return self
        except BaseException:
            # ``async with`` does not call __aexit__ when __aenter__ fails.
            # Without explicit cleanup, every failed startup probe leaks the
            # Windows Proactor subprocess and its three pipe transports.
            with suppress(BaseException):
                await self._close_process()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close_process()

    async def _close_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        try:
            # Closing stdin gives well-behaved MCP servers a chance to shut
            # down, while communicate() drains stdout/stderr so their pipe
            # transports can finish before asyncio.run() closes its loop.
            communicate_task = asyncio.create_task(proc.communicate())
            try:
                await asyncio.wait_for(
                    asyncio.shield(communicate_task),
                    timeout=0.25,
                )
                return
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

            if proc.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    proc.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.shield(communicate_task),
                    timeout=1.5,
                )
                return
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

            if proc.returncode is None:
                with suppress(ProcessLookupError, OSError):
                    proc.kill()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.shield(communicate_task),
                    timeout=1.0,
                )
        finally:
            if communicate_task is not None and not communicate_task.done():
                communicate_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await communicate_task
            # asyncio.subprocess.Process exposes no public close(). Its
            # transport does, and calling it is idempotent after communicate().
            # This finalizer is needed when Windows rejects terminate/kill or a
            # pipe callback is still queued during loop shutdown.
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with suppress(Exception):
                    transport.close()
            await asyncio.sleep(0)

    async def _write(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP process is not running")
        self._proc.stdin.write((_json_dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_response(self, request_id: int) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("MCP process is not running")
        timeout = self.spec.effective_timeout()
        while True:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            if not line:
                stderr = ""
                if self._proc.stderr is not None:
                    try:
                        err = await asyncio.wait_for(self._proc.stderr.read(), timeout=0.2)
                        stderr = err.decode("utf-8", errors="replace")[-1000:]
                    except Exception:
                        stderr = ""
                raise RuntimeError(f"MCP server exited before response; stderr={stderr}")
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            if msg.get("id") != request_id:
                continue
            if msg.get("error"):
                raise RuntimeError(str(msg.get("error")))
            return msg.get("result") or {}

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return await self._read_response(request_id)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def list_tools(self) -> dict[str, Any]:
        result = await self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else []
        return {"tools": tools if isinstance(tools, list) else []}

    def health(self) -> dict[str, Any]:
        """Return initialization metadata after a successful handshake."""
        result = self.initialize_result if isinstance(self.initialize_result, dict) else {}
        return {
            "protocol_version": result.get("protocolVersion") or result.get("protocol_version") or "",
            "server_info": result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {},
            "capabilities": result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {},
            "runtime": dict(self.runtime_info),
        }

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        normalized = _normalize_content(result.get("content") if isinstance(result, dict) else result)
        return {
            "raw": result,
            **normalized,
            "is_error": bool(isinstance(result, dict) and result.get("isError")),
        }

    async def list_resources(self) -> dict[str, Any]:
        result = await self._request("resources/list", {})
        resources = result.get("resources") if isinstance(result, dict) else []
        return {"resources": resources if isinstance(resources, list) else []}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri})
        return {"raw": result, **_normalize_content(result.get("contents") if isinstance(result, dict) else result)}


# ---------------------------------------------------------------------------
# Streamable HTTP client (single POST per JSON-RPC request)
# ---------------------------------------------------------------------------


def _validate_remote_url(url: str, *, allow_insecure_http: bool) -> tuple[bool, str]:
    """Return ``(ok, error_message)`` for the remote MCP URL.

    By default we allow HTTPS unconditionally, and HTTP only for
    localhost / loopback / private RFC1918 addresses. Setting the
    policy's ``allow_insecure_http`` opens the door to public HTTP.
    """
    text = (url or "").strip()
    if not text:
        return False, "url is required"
    try:
        parsed = urlparse(text)
    except Exception as exc:
        return False, f"invalid url: {exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported url scheme: {parsed.scheme!r} (use https:// or http://)"
    if parsed.username is not None or parsed.password is not None:
        return False, "credentials must be supplied through MCP headers, not embedded in the URL"
    if parsed.scheme == "http" and not allow_insecure_http:
        host = (parsed.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1", "::1"} and not _is_private_host(host):
            return False, (
                "refusing public http:// MCP endpoint; require https:// or set the server policy's "
                "allow_insecure_http to true for an explicit user override."
            )
    if not parsed.netloc:
        return False, "url is missing host:port"
    return True, ""


def _is_private_host(host: str) -> bool:
    if not host:
        return False
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            octets = [int(p) for p in parts]
        except ValueError:
            return False
        if octets[0] == 10:
            return True
        if octets[0] == 172 and 16 <= octets[1] <= 31:
            return True
        if octets[0] == 192 and octets[1] == 168:
            return True
        if octets[0] == 169 and octets[1] == 254:
            return True
    return False


def _mcp_user_agent(transport: str = "http") -> str:
    """HTTP User-Agent for remote MCP transports; version matches ``clientInfo``."""
    version = str(__version__ or "").strip() or "0.0.0"
    tag = "mcp-sse" if str(transport or "").strip().lower() == "sse" else "mcp-http"
    return f"ArgusAI/{version} ({tag})"


class _HttpJsonRpcSession:
    """Minimal JSON-RPC-over-HTTP session shared by Streamable-HTTP/SSE clients."""

    def __init__(self, spec: MCPTransportSpec) -> None:
        self.spec = spec
        self._next_id = 1
        self._session_id: str = ""
        self.initialize_result: dict[str, Any] = {}
        self.runtime_info: dict[str, Any] = {
            "transport": spec.transport,
            "url_origin": _url_origin(spec.url),
            "auth_header_keys": sorted(spec.headers.keys()),
        }

    def _headers(self, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": _mcp_user_agent(self.spec.transport),
        }
        headers.update(self.spec.headers or {})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if extra:
            headers.update(extra)
        return headers

    def _payload(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}

    def _notify_payload(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def _url_origin(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class MCPStreamableHttpClient(_HttpJsonRpcSession):
    """JSON-RPC over Streamable HTTP (single POST per request).

    The "Streamable HTTP" transport is what current MCP server hosters
    (Cloudflare, Anthropic Workers, FastMCP HTTP) expose: each JSON-RPC
    request goes via POST and the server may reply with either a plain
    JSON body or an ``text/event-stream`` body. We accept both.

    Network IO uses :mod:`httpx` because every other ArgusAI runtime
    component already depends on it (no new dependency).
    """

    async def __aenter__(self) -> "MCPStreamableHttpClient":
        ok, err = _validate_remote_url(self.spec.url, allow_insecure_http=bool((self.spec.policy or {}).get("allow_insecure_http")))
        if not ok:
            raise ValueError(err)
        import httpx  # local import; fast on Python 3.13 hot start
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            timeout=self.spec.effective_timeout(),
            follow_redirects=True,
            trust_env=False,
        )
        self.initialize_result = await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "argusai", "version": __version__},
        })
        await self._notify("notifications/initialized", {})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(self.spec.url, headers=self._headers(), json=payload)
        except self._httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(str(exc)) from exc
        if "Mcp-Session-Id" in resp.headers and not self._session_id:
            self._session_id = resp.headers["Mcp-Session-Id"]
        if resp.status_code >= 400:
            text = resp.text[:1000] if resp.text else ""
            raise RuntimeError(f"HTTP {resp.status_code} from MCP endpoint: {text}")
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type == "text/event-stream":
            return _parse_sse_for_id(resp.text, payload.get("id"))
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"MCP endpoint returned non-JSON body: {exc}")
        return data

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._payload(method, params)
        data = await self._post(payload)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(str(data["error"]))
        if isinstance(data, dict):
            return data.get("result") or {}
        return {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        try:
            await self._client.post(self.spec.url, headers=self._headers(), json=self._notify_payload(method, params))
        except Exception:
            # Notifications are fire-and-forget; failing one shouldn't kill the session.
            return

    async def list_tools(self) -> dict[str, Any]:
        result = await self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else []
        return {"tools": tools if isinstance(tools, list) else []}

    def health(self) -> dict[str, Any]:
        result = self.initialize_result if isinstance(self.initialize_result, dict) else {}
        return {
            "protocol_version": result.get("protocolVersion") or result.get("protocol_version") or "",
            "server_info": result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {},
            "capabilities": result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {},
            "runtime": dict(self.runtime_info),
        }

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        normalized = _normalize_content(result.get("content") if isinstance(result, dict) else result)
        return {
            "raw": result,
            **normalized,
            "is_error": bool(isinstance(result, dict) and result.get("isError")),
        }

    async def list_resources(self) -> dict[str, Any]:
        result = await self._request("resources/list", {})
        resources = result.get("resources") if isinstance(result, dict) else []
        return {"resources": resources if isinstance(resources, list) else []}

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._request("resources/read", {"uri": uri})
        return {"raw": result, **_normalize_content(result.get("contents") if isinstance(result, dict) else result)}


def _parse_sse_for_id(text: str, request_id: Any) -> dict[str, Any]:
    """Pick the JSON-RPC frame with the matching ``id`` out of an SSE body."""
    if not text:
        return {}
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        try:
            msg = json.loads("".join(data_lines))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        if request_id is None or msg.get("id") == request_id:
            return msg
    return {}


# ---------------------------------------------------------------------------
# Legacy SSE transport (long-lived event stream + paired POST endpoint)
# ---------------------------------------------------------------------------


class MCPSseClient(MCPStreamableHttpClient):
    """Client for the legacy MCP HTTP+SSE transport.

    The configured URL is opened as a long-lived SSE stream. The server
    announces a paired POST endpoint through an ``endpoint`` event; client
    requests are posted there and their JSON-RPC responses arrive as
    ``message`` events on the original stream.
    """

    def __init__(self, spec: MCPTransportSpec) -> None:
        super().__init__(spec)
        self._message_endpoint = ""
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False
        self._stream_error: BaseException | None = None

    async def __aenter__(self) -> "MCPSseClient":
        ok, err = _validate_remote_url(
            self.spec.url,
            allow_insecure_http=bool((self.spec.policy or {}).get("allow_insecure_http")),
        )
        if not ok:
            raise ValueError(err)

        import httpx  # local import; shared dependency with Streamable HTTP

        self._httpx = httpx
        timeout = self.spec.effective_timeout()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=None),
            follow_redirects=True,
            trust_env=False,
        )
        self._endpoint_ready = asyncio.get_running_loop().create_future()
        self._stream_cm = self._client.stream(
            "GET",
            self.spec.url,
            headers=self._sse_headers(),
        )
        try:
            self._stream_response = await self._stream_cm.__aenter__()
            if self._stream_response.status_code >= 400:
                await self._stream_response.aread()
                text = self._stream_response.text[:1000] if self._stream_response.text else ""
                raise RuntimeError(f"HTTP {self._stream_response.status_code} from MCP SSE endpoint: {text}")
            content_type = (self._stream_response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "text/event-stream":
                raise RuntimeError(f"MCP SSE endpoint returned unexpected Content-Type: {content_type or '<missing>'}")

            self._reader_task = asyncio.create_task(
                self._read_sse_stream(),
                name=f"mcp-sse:{self.spec.name}",
            )
            await asyncio.wait_for(
                asyncio.shield(self._endpoint_ready),
                timeout=self.spec.effective_timeout(),
            )
            self.initialize_result = await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "argusai", "version": __version__},
                },
            )
            await self._notify("notifications/initialized", {})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        reader_task = getattr(self, "_reader_task", None)
        if reader_task is not None:
            reader_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader_task

        stream_cm = getattr(self, "_stream_cm", None)
        if stream_cm is not None:
            with suppress(Exception):
                await stream_cm.__aexit__(exc_type, exc, tb)

        client = getattr(self, "_client", None)
        if client is not None:
            with suppress(Exception):
                await client.aclose()

        close_error = RuntimeError("MCP SSE transport closed")
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(close_error)
        self._pending.clear()

    def _sse_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "User-Agent": _mcp_user_agent(self.spec.transport),
        }
        headers.update(self.spec.headers or {})
        return headers

    def _event_size_limit(self) -> int:
        raw = (self.spec.policy or {}).get("max_result_bytes", 200_000)
        try:
            requested = int(raw)
        except (TypeError, ValueError):
            requested = 200_000
        return max(64_000, min(requested, 2_000_000))

    async def _read_sse_stream(self) -> None:
        event_name = "message"
        data_lines: list[str] = []
        data_size = 0
        stream_error: BaseException | None = None
        try:
            async for line in self._stream_response.aiter_lines():
                if line == "":
                    if data_lines:
                        self._dispatch_sse_event(event_name, "\n".join(data_lines))
                    event_name = "message"
                    data_lines = []
                    data_size = 0
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                    continue
                if line.startswith("data:"):
                    value = line[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    data_size += len(value)
                    if data_size > self._event_size_limit():
                        raise RuntimeError("MCP SSE event exceeded the configured result-size limit")
                    data_lines.append(value)
            if data_lines:
                self._dispatch_sse_event(event_name, "\n".join(data_lines))
            if not self._closed:
                stream_error = ConnectionError("MCP SSE event stream closed unexpectedly")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            stream_error = exc
        finally:
            if stream_error is not None:
                self._stream_error = stream_error
                if not self._endpoint_ready.done():
                    self._endpoint_ready.set_exception(stream_error)
                for future in list(self._pending.values()):
                    if not future.done():
                        future.set_exception(stream_error)

    def _dispatch_sse_event(self, event_name: str, data: str) -> None:
        if event_name == "endpoint":
            endpoint = urljoin(self.spec.url, data.strip())
            ok, err = _validate_remote_url(
                endpoint,
                allow_insecure_http=bool((self.spec.policy or {}).get("allow_insecure_http")),
            )
            if not ok:
                raise ValueError(f"invalid MCP SSE message endpoint: {err}")
            if _url_origin(endpoint).lower() != _url_origin(self.spec.url).lower():
                raise ValueError("MCP SSE message endpoint must use the same origin as the event stream")
            if not self._message_endpoint:
                self._message_endpoint = endpoint
                self.runtime_info["message_endpoint_origin"] = _url_origin(endpoint)
                if not self._endpoint_ready.done():
                    self._endpoint_ready.set_result(endpoint)
            return

        if event_name not in {"message", ""}:
            return
        try:
            message = json.loads(data)
        except Exception:
            return
        if not isinstance(message, dict) or "id" not in message:
            return
        future = self._pending.get(message.get("id"))
        if future is not None and not future.done():
            future.set_result(message)

    async def _post_message(self, payload: dict[str, Any]) -> None:
        if self._stream_error is not None:
            raise ConnectionError("MCP SSE event stream is no longer available") from self._stream_error
        if not self._message_endpoint:
            raise RuntimeError("MCP SSE server did not announce a message endpoint")
        try:
            response = await self._client.post(
                self._message_endpoint,
                headers=self._headers(),
                json=payload,
                timeout=self.spec.effective_timeout(),
            )
        except self._httpx.TimeoutException as exc:
            raise asyncio.TimeoutError(str(exc)) from exc
        if response.status_code >= 400:
            text = response.text[:1000] if response.text else ""
            raise RuntimeError(f"HTTP {response.status_code} from MCP SSE message endpoint: {text}")

        # Most legacy servers return 202 and deliver the response on the
        # event stream. Accept an immediate JSON response as a compatibility
        # fallback for hybrid implementations.
        if response.content:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type == "application/json":
                with suppress(Exception):
                    message = response.json()
                    if isinstance(message, dict):
                        self._dispatch_sse_event("message", _json_dumps(message))

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._payload(method, params)
        request_id = payload["id"]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._post_message(payload)
            data = await asyncio.wait_for(future, timeout=self.spec.effective_timeout())
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with suppress(Exception):
            await self._post_message(self._notify_payload(method, params))


# ---------------------------------------------------------------------------
# Factory + tool registry adapter
# ---------------------------------------------------------------------------


def make_mcp_client(spec: MCPTransportSpec) -> Any:
    """Return the right adapter for ``spec.transport``."""
    if spec.transport == "stdio":
        return MCPStdioClient(spec)
    if spec.transport == "streamable_http":
        return MCPStreamableHttpClient(spec)
    if spec.transport == "sse":
        return MCPSseClient(spec)
    raise ValueError(f"Unsupported MCP transport: {spec.transport!r}")


def mcp_tool_to_registry_item(
    server_name: str,
    tool: dict[str, Any],
    *,
    server_id: str = "",
    transport: str = "stdio",
    default_risk: str = "high",
    tool_risks: dict[str, str] | None = None,
    allow_tools: list[str] | None = None,
    deny_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Convert MCP tool metadata to ArgusAI ToolRegistry-style metadata.

    The schema is sanitized; description is capped; the effective risk
    is the per-tool override, else the server-level default. ``allowed``
    encodes the allow/deny verdict so the UI can grey out blocked tools
    without re-running policy logic.
    """
    name = str(tool.get("name") or "").strip()
    namespace = str(server_name or "mcp").strip().replace(" ", "_") or "mcp"
    raw_schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    clean_schema = sanitize_mcp_schema(raw_schema)
    description = _truncate_text(tool.get("description") or name, _SCHEMA_DESCRIPTION_CAP)
    tool_risks = tool_risks or {}
    allow_tools = allow_tools or []
    deny_tools = deny_tools or []

    risk = tool_risks.get(name) or default_risk
    if risk not in {"low", "medium-low", "medium", "high"}:
        risk = "high"

    denied = name in set(deny_tools)
    allowed = not denied and (not allow_tools or name in set(allow_tools))

    return {
        "name": f"mcp_{namespace}.{name}",
        "mcp_name": name,
        "description": description,
        "parameters": clean_schema.get("properties") if isinstance(clean_schema.get("properties"), dict) else {},
        "required": clean_schema.get("required") if isinstance(clean_schema.get("required"), list) else [],
        "schema": clean_schema,
        "risk": risk,
        "allowed": allowed,
        "denied": denied,
        "source": {
            "protocol": "mcp",
            "transport": transport,
            "server": namespace,
            "server_id": server_id,
            "tool": name,
        },
    }


# ---------------------------------------------------------------------------
# Long-result handling helper (Loom22 §5.7)
# ---------------------------------------------------------------------------


def maybe_truncate_call_result(
    spec: MCPTransportSpec,
    *,
    tool_name: str,
    result: dict[str, Any],
    max_bytes: int,
) -> dict[str, Any]:
    """If ``result`` would blow past ``max_bytes`` once serialised, return a
    summary-only stand-in that points at the omitted content.

    The caller can take this further (e.g. write a session artifact) but
    even just truncating prevents the assistant from getting an unbounded
    blob handed back into its context window.
    """
    if max_bytes <= 0:
        return result
    size = _serialised_size(result)
    if size <= max_bytes:
        return result

    content = result.get("content") if isinstance(result, dict) else []
    text_parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text") or ""))
            if sum(len(p) for p in text_parts) > 8000:
                break
    preview = "\n".join(text_parts)[:8000]

    return {
        "ok": True,
        "truncated": True,
        "truncate_reason": "max_result_bytes",
        "original_bytes": size,
        "max_result_bytes": max_bytes,
        "summary": result.get("summary") or preview[:4000],
        "preview": preview,
        "is_error": bool(result.get("is_error")),
        "resources": result.get("resources") or [],
        "source": spec.source_block(tool=tool_name),
        "read_more": (
            "Result exceeded max_result_bytes. Re-call with narrower arguments, "
            "or relax the server policy's max_result_bytes if you really need the full payload."
        ),
    }


# ---------------------------------------------------------------------------
# Public helpers re-exported for tests
# ---------------------------------------------------------------------------


def public_server_view(server: dict[str, Any]) -> dict[str, Any]:
    """Strip secrets (``env`` values, ``headers`` values) but keep keys.

    Used by ``case_mcp.mcp_server_list`` and the GUI so the AI / UI can
    see *which* env vars and headers a server uses without leaking the
    actual secret values into chat or logs.
    """
    public = dict(server)
    env = public.pop("env", {}) or {}
    headers = public.pop("headers", {}) or {}
    public["args"] = redact_launch_args(public.get("args") or [])
    provenance = normalize_provenance(public.get("provenance"))
    if provenance:
        public["provenance"] = provenance
    else:
        public.pop("provenance", None)
    public["env_keys"] = sorted(str(k) for k in env.keys()) if isinstance(env, dict) else []
    public["header_keys"] = sorted(str(k) for k in headers.keys()) if isinstance(headers, dict) else []
    return public
