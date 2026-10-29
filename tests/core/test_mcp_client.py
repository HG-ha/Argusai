import asyncio
import json
import sys
from pathlib import Path

from argusai import __version__
from argusai.core._tools import (
    mcp_server_add,
    mcp_server_delete,
    mcp_server_health,
    mcp_server_list,
    mcp_stdio_call_tool,
    mcp_stdio_health,
    mcp_stdio_list_resources,
    mcp_stdio_list_tools,
    mcp_stdio_read_resource,
)
from argusai.core.assistant import build_tool_registry
from argusai.core.infra.mcp_client import (
    MCPStdioClient,
    MCPTransportSpec,
    mcp_tool_to_registry_item,
)
from argusai.core.settings import SettingsManager, normalize_mcp_servers


FAKE_SERVER = r"""
import json
import sys

TOOLS = [{
    "name": "echo",
    "description": "Echo input text",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}]

RESOURCES = [{"uri": "argus://note/1", "name": "note.txt", "mimeType": "text/plain"}]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "fake"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        result = {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}
    elif method == "resources/list":
        result = {"resources": RESOURCES}
    elif method == "resources/read":
        result = {"contents": [{"type": "text", "text": "resource body"}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


def _server_path(tmp_path: Path) -> Path:
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


class FakeApi:
    def __init__(self, tmp_path: Path) -> None:
        self.settings_mgr = SettingsManager(tmp_path / "settings.json")
        self.settings_mgr.load()


class _FakeSseResponse:
    def __init__(self, endpoint: str) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        self.content = b""
        self.text = ""
        self.endpoint = endpoint
        self.messages: asyncio.Queue[dict | None] = asyncio.Queue()

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        yield "event: endpoint"
        yield f"data: {self.endpoint}"
        yield ""
        while True:
            message = await self.messages.get()
            if message is None:
                return
            yield "event: message"
            yield f"data: {json.dumps(message)}"
            yield ""


class _FakeSseStreamContext:
    def __init__(self, response: _FakeSseResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeSseResponse:
        return self.response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.response.messages.put(None)


class _FakePostResponse:
    status_code = 202
    headers: dict[str, str] = {}
    content = b""
    text = ""


class _FakeSseAsyncClient:
    endpoint = "/messages/?session_id=fake"
    last_instance = None

    def __init__(self, **kwargs) -> None:
        type(self).last_instance = self
        self.response = _FakeSseResponse(self.endpoint)
        self.closed = False
        self.get_headers: dict[str, str] = {}
        self.post_headers: list[dict[str, str]] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        assert method == "GET"
        self.get_headers = dict(headers)
        return _FakeSseStreamContext(self.response)

    async def post(self, url: str, *, headers: dict[str, str], json: dict, timeout: int):
        self.post_headers.append(dict(headers))
        request_id = json.get("id")
        if request_id is None:
            return _FakePostResponse()
        method = json.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "fake-sse"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "echo",
                    "description": "Echo input text",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                }]
            }
        elif method == "tools/call":
            text = str(json.get("params", {}).get("arguments", {}).get("text", ""))
            result = {"content": [{"type": "text", "text": f"echo:{text}"}]}
        elif method == "resources/list":
            result = {"resources": []}
        else:
            result = {}
        await self.response.messages.put({"jsonrpc": "2.0", "id": request_id, "result": result})
        return _FakePostResponse()

    async def aclose(self) -> None:
        self.closed = True


def test_mcp_tool_metadata_maps_to_registry_shape() -> None:
    item = mcp_tool_to_registry_item(
        "sec tools",
        {
            "name": "lookup",
            "description": "Lookup data",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        },
    )

    assert item["name"] == "mcp_sec_tools.lookup"
    assert item["mcp_name"] == "lookup"
    assert item["parameters"] == {"q": {"type": "string"}}
    assert item["required"] == ["q"]
    # The new default risk is "high" — third-party MCP servers are
    # treated as untrusted until the user explicitly relaxes the
    # per-server policy.
    assert item["risk"] == "high"
    assert item["allowed"] is True
    assert item["denied"] is False
    assert item["source"]["transport"] == "stdio"


def test_mcp_tool_metadata_applies_policy() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    base = {"name": "write_file", "description": "Write a file", "inputSchema": schema}

    denied = mcp_tool_to_registry_item("fs", base, default_risk="medium", deny_tools=["write_file"])
    assert denied["denied"] is True
    assert denied["allowed"] is False

    allowed_only = mcp_tool_to_registry_item("fs", {**base, "name": "read_file"}, allow_tools=["read_file"], tool_risks={"read_file": "medium-low"})
    assert allowed_only["allowed"] is True
    assert allowed_only["risk"] == "medium-low"


def test_mcp_stdio_tools_are_registered_with_confirmation_policy() -> None:
    registry = build_tool_registry(api=object())
    tools = {item["name"]: item for item in registry.list_tools()}

    assert tools["mcp_server_add"]["needs_confirmation"] is True
    assert tools["mcp_server_add"]["requires_double_confirm"] is True
    assert tools["mcp_server_delete"]["needs_confirmation"] is True
    assert tools["mcp_server_delete"]["requires_double_confirm"] is True
    assert tools["mcp_server_health"]["risk"] == "medium"
    assert tools["mcp_server_health"]["needs_confirmation"] is False
    assert tools["mcp_stdio_health"]["risk"] == "medium"
    assert tools["mcp_stdio_health"]["needs_confirmation"] is True
    assert tools["mcp_stdio_list_tools"]["needs_confirmation"] is True
    assert tools["mcp_stdio_call_tool"]["risk"] == "high"
    assert tools["mcp_stdio_call_tool"]["needs_confirmation"] is True
    assert tools["mcp_stdio_call_tool"]["requires_double_confirm"] is True
    assert tools["mcp_stdio_read_resource"]["needs_confirmation"] is True
    # New per-server entrypoints
    assert tools["mcp_server_list_tools"]["risk"] == "medium"
    assert tools["mcp_server_list_tools"]["needs_confirmation"] is False
    assert tools["mcp_server_list_resources"]["needs_confirmation"] is False
    assert tools["mcp_server_call_tool"]["risk"] == "high"
    assert tools["mcp_server_call_tool"]["needs_confirmation"] is True
    assert tools["mcp_server_read_resource"]["risk"] == "high"
    assert tools["mcp_server_read_resource"]["needs_confirmation"] is True


def test_mcp_server_config_normalization_hides_invalid_entries() -> None:
    result = normalize_mcp_servers([
        {"name": "Filesystem", "command": "npx", "args": ["-y", "@mcp/server"], "env": {"TOKEN": "x"}},
        {"name": "Broken"},
    ])

    assert len(result) == 1
    assert result[0]["id"].startswith("filesystem-")
    assert result[0]["command"] == "npx"
    assert result[0]["env"] == {"TOKEN": "x"}
    # Legacy entries are now treated as stdio transport with a default policy.
    assert result[0]["transport"] == "stdio"
    assert result[0]["policy"]["default_risk"] == "high"
    assert result[0]["policy"]["allow_auto_approve"] is False
    assert result[0]["policy"]["require_confirmation_for_all"] is True
    assert result[0]["url"] == ""
    assert result[0]["headers"] == {}


def test_mcp_server_normalization_supports_streamable_http() -> None:
    result = normalize_mcp_servers([
        {
            "name": "Remote MCP",
            "transport": "streamable_http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer abc"},
            "policy": {"default_risk": "medium", "allow_tools": ["search"], "tool_risks": {"search": "low"}},
        }
    ])

    assert len(result) == 1
    entry = result[0]
    assert entry["transport"] == "streamable_http"
    assert entry["url"] == "https://mcp.example.com/mcp"
    assert entry["headers"] == {"Authorization": "Bearer abc"}
    assert entry["command"] == ""
    assert entry["policy"]["default_risk"] == "medium"
    assert entry["policy"]["allow_tools"] == ["search"]
    assert entry["policy"]["tool_risks"] == {"search": "low"}


def test_mcp_server_normalization_supports_sse() -> None:
    result = normalize_mcp_servers([
        {
            "name": "Legacy SSE MCP",
            "transport": "sse",
            "url": "http://127.0.0.1:3000/sse",
            "headers": {"Authorization": "Bearer abc"},
        }
    ])

    assert len(result) == 1
    entry = result[0]
    assert entry["transport"] == "sse"
    assert entry["url"] == "http://127.0.0.1:3000/sse"
    assert entry["headers"] == {"Authorization": "Bearer abc"}
    assert entry["command"] == ""


def test_mcp_server_normalization_clamps_policy_bounds() -> None:
    result = normalize_mcp_servers([
        {
            "name": "Bad",
            "command": "node",
            "policy": {
                "default_risk": "off-the-charts",
                "max_timeout_seconds": 9999,
                "max_result_bytes": 99999999,
            },
        }
    ])

    p = result[0]["policy"]
    assert p["default_risk"] == "high"
    assert p["max_timeout_seconds"] == 600
    assert p["max_result_bytes"] == 2_000_000


def test_mcp_server_add_list_delete_persisted_settings(tmp_path: Path) -> None:
    api = FakeApi(tmp_path)

    added = mcp_server_add(
        api=api,
        name="Fake",
        command=sys.executable,
        args=["fake.py"],
        server_id="fake",
        env_json='{"TOKEN":"secret"}',
    )
    assert added["ok"] is True
    assert added["server"]["id"] == "fake"
    assert added["server"]["env_keys"] == ["TOKEN"]
    assert "env" not in added["server"]

    listed = mcp_server_list(api=api)
    assert listed["count"] == 1
    assert listed["servers"][0]["id"] == "fake"

    deleted = mcp_server_delete(api=api, server_id="fake")
    assert deleted["ok"] is True
    assert mcp_server_list(api=api)["count"] == 0


def test_mcp_server_health_uses_configured_server(tmp_path: Path) -> None:
    api = FakeApi(tmp_path)
    server = _server_path(tmp_path)
    added = mcp_server_add(api=api, name="Fake", command=sys.executable, args=[str(server)], server_id="fake")
    assert added["ok"] is True

    result = asyncio.run(mcp_server_health(api=api, server_id="fake"))

    assert result["ok"] is True
    assert result["status"] == "alive"
    assert result["server_id"] == "fake"


def test_mcp_stdio_health_reports_alive_server(tmp_path: Path) -> None:
    server = _server_path(tmp_path)

    result = asyncio.run(mcp_stdio_health(api=None, command=sys.executable, args=[str(server)], server_name="fake"))

    assert result["ok"] is True
    assert result["status"] == "alive"
    assert result["server_info"] == {"name": "fake"}
    assert result["capabilities"] == {"tools": {}, "resources": {}}
    assert result["source"]["protocol"] == "mcp"
    assert result["source"]["transport"] == "stdio"
    assert result["source"]["server"] == "fake"
    assert "runtime" in result


def test_mcp_stdio_list_and_call_tool(tmp_path: Path) -> None:
    server = _server_path(tmp_path)

    listed = asyncio.run(mcp_stdio_list_tools(api=None, command=sys.executable, args=[str(server)], server_name="fake"))
    assert listed["ok"] is True
    assert listed["tools"][0]["name"] == "mcp_fake.echo"

    called = asyncio.run(
        mcp_stdio_call_tool(
            api=None,
            command=sys.executable,
            args=[str(server)],
            server_name="fake",
            tool_name="echo",
            arguments_json=json.dumps({"text": "hello"}),
        )
    )
    assert called["ok"] is True
    assert called["summary"] == "echo:hello"
    assert called["source"]["protocol"] == "mcp"
    assert called["source"]["transport"] == "stdio"
    assert called["source"]["server"] == "fake"
    assert called["source"]["tool"] == "echo"


def test_mcp_stdio_health_reports_dead_server() -> None:
    result = asyncio.run(mcp_stdio_health(api=None, command=sys.executable, args=["-c", "import sys; sys.exit(2)"], server_name="dead"))

    assert result["ok"] is False
    assert result["status"] == "dead"
    # Structured error fields from the §5.7 payload.
    assert "error_code" in result
    assert "MCP server exited before response" in result["error"]


def test_mcp_stdio_enter_failure_closes_subprocess_transport() -> None:
    client = MCPStdioClient(
        MCPTransportSpec(
            name="dead",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(2)"],
        )
    )

    async def scenario() -> None:
        try:
            await client.__aenter__()
        except RuntimeError:
            pass
        else:
            raise AssertionError("dead MCP server unexpectedly initialized")

    asyncio.run(scenario())

    assert client._proc is None


def test_mcp_stdio_cancelled_enter_terminates_hung_subprocess() -> None:
    client = MCPStdioClient(
        MCPTransportSpec(
            name="hung",
            command=sys.executable,
            args=[
                "-c",
                "import sys,time; sys.stdin.readline(); time.sleep(60)",
            ],
        )
    )

    async def scenario() -> None:
        try:
            await asyncio.wait_for(client.__aenter__(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError("hung MCP server unexpectedly initialized")

    asyncio.run(scenario())

    assert client._proc is None


def test_mcp_stdio_resources_are_two_step(tmp_path: Path) -> None:
    server = _server_path(tmp_path)

    listed = asyncio.run(mcp_stdio_list_resources(api=None, command=sys.executable, args=[str(server)], server_name="fake"))
    assert listed["ok"] is True
    assert listed["resources"][0]["uri"] == "argus://note/1"

    read = asyncio.run(
        mcp_stdio_read_resource(
            api=None,
            command=sys.executable,
            args=[str(server)],
            server_name="fake",
            uri="argus://note/1",
        )
    )
    assert read["ok"] is True
    assert read["summary"] == "resource body"
    assert read["source"]["protocol"] == "mcp"
    assert read["source"]["transport"] == "stdio"
    assert read["source"]["server"] == "fake"
    assert read["source"]["uri"] == "argus://note/1"


def test_mcp_stdio_call_tool_rejects_non_object_arguments(tmp_path: Path) -> None:
    server = _server_path(tmp_path)

    result = asyncio.run(
        mcp_stdio_call_tool(
            api=None,
            command=sys.executable,
            args=[str(server)],
            tool_name="echo",
            arguments_json="[]",
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_arguments"
    assert "JSON object" in result["error"]


# ---------------------------------------------------------------------------
# Schema / arguments sanitization (Loom22 §5.6)
# ---------------------------------------------------------------------------


def test_sanitize_mcp_schema_strips_unknown_keys_and_caps_description() -> None:
    from argusai.core.infra.mcp_client import sanitize_mcp_schema

    raw = {
        "type": "wonky",  # unknown type → fall back to "string"
        "description": "x" * 5000,
        "format": "uuid",  # unsupported key → dropped
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "nested": {
                "type": "object",
                "properties": {"deep": {"type": "integer", "minimum": 0, "maximum": 5}},
            },
        },
        "required": ["name"],
    }

    out = sanitize_mcp_schema(raw)

    assert out["type"] == "string"
    assert len(out["description"]) <= 2000
    assert "format" not in out
    assert out["properties"]["name"] == {"type": "string", "minLength": 1}
    assert out["properties"]["nested"]["properties"]["deep"]["minimum"] == 0
    assert out["required"] == ["name"]


def test_sanitize_mcp_arguments_truncates_oversize_string() -> None:
    from argusai.core.infra.mcp_client import sanitize_mcp_arguments

    huge = "x" * 60_000
    out = sanitize_mcp_arguments({"big": huge, "ok": "short"})

    assert out["ok"] == "short"
    assert len(out["big"]) < len(huge)
    assert out["big"].endswith("[truncated]")


# ---------------------------------------------------------------------------
# URL validation (Streamable HTTP / SSE)
# ---------------------------------------------------------------------------


def test_validate_remote_url_blocks_public_http() -> None:
    from argusai.core.infra.mcp_client import _validate_remote_url

    ok, err = _validate_remote_url("http://example.com/mcp", allow_insecure_http=False)
    assert ok is False
    assert "insecure" in err or "refus" in err

    ok, _ = _validate_remote_url("https://example.com/mcp", allow_insecure_http=False)
    assert ok is True

    ok, _ = _validate_remote_url("http://127.0.0.1:3000/mcp", allow_insecure_http=False)
    assert ok is True

    ok, _ = _validate_remote_url("http://192.168.1.10/mcp", allow_insecure_http=False)
    assert ok is True  # RFC1918 host counted as private


def test_remote_mcp_user_agent_matches_package_version() -> None:
    from argusai.core.infra.mcp_client import _HttpJsonRpcSession, _mcp_user_agent

    assert _mcp_user_agent("streamable_http") == f"ArgusAI/{__version__} (mcp-http)"
    assert _mcp_user_agent("sse") == f"ArgusAI/{__version__} (mcp-sse)"

    http = _HttpJsonRpcSession(
        MCPTransportSpec(name="http", transport="streamable_http", url="https://example.com/mcp")
    )
    assert http._headers()["User-Agent"] == f"ArgusAI/{__version__} (mcp-http)"

    sse = _HttpJsonRpcSession(
        MCPTransportSpec(name="sse", transport="sse", url="http://127.0.0.1:3000/sse")
    )
    assert sse._headers()["User-Agent"] == f"ArgusAI/{__version__} (mcp-sse)"

    custom = _HttpJsonRpcSession(
        MCPTransportSpec(
            name="http",
            transport="streamable_http",
            url="https://example.com/mcp",
            headers={"User-Agent": "custom-mcp/1"},
        )
    )
    assert custom._headers()["User-Agent"] == "custom-mcp/1"


def test_sse_client_initializes_lists_and_calls_tools(monkeypatch) -> None:
    import httpx

    from argusai.core.infra.mcp_client import MCPTransportSpec, make_mcp_client

    monkeypatch.setattr(httpx, "AsyncClient", _FakeSseAsyncClient)

    async def scenario() -> None:
        spec = MCPTransportSpec(
            name="fake-sse",
            transport="sse",
            url="http://127.0.0.1:3000/sse",
            headers={"Authorization": "Bearer secret"},
        )
        async with make_mcp_client(spec) as client:
            health = client.health()
            assert health["server_info"] == {"name": "fake-sse"}
            assert health["runtime"]["transport"] == "sse"
            assert health["runtime"]["message_endpoint_origin"] == "http://127.0.0.1:3000"

            listed = await client.list_tools()
            assert listed["tools"][0]["name"] == "echo"

            called = await client.call_tool("echo", {"text": "hello"})
            assert called["summary"] == "echo:hello"

        fake = _FakeSseAsyncClient.last_instance
        assert fake is not None
        assert fake.closed is True
        assert fake.get_headers["Accept"] == "text/event-stream"
        assert fake.get_headers["Authorization"] == "Bearer secret"
        assert fake.get_headers["User-Agent"].startswith(f"ArgusAI/{__version__} (mcp-sse)")
        assert all(headers["Authorization"] == "Bearer secret" for headers in fake.post_headers)
        assert all(
            headers["User-Agent"].startswith(f"ArgusAI/{__version__} (mcp-sse)")
            for headers in fake.post_headers
        )

    asyncio.run(scenario())


def test_sse_client_rejects_cross_origin_message_endpoint(monkeypatch) -> None:
    import httpx

    from argusai.core.infra.mcp_client import MCPTransportSpec, make_mcp_client

    class CrossOriginClient(_FakeSseAsyncClient):
        endpoint = "https://attacker.example/messages/?session_id=fake"

    monkeypatch.setattr(httpx, "AsyncClient", CrossOriginClient)

    async def scenario() -> str:
        spec = MCPTransportSpec(
            name="fake-sse",
            transport="sse",
            url="https://mcp.example/sse",
        )
        try:
            async with make_mcp_client(spec):
                return ""
        except ValueError as exc:
            return str(exc)

    error = asyncio.run(scenario())
    assert "same origin" in error
    assert CrossOriginClient.last_instance is not None
    assert CrossOriginClient.last_instance.closed is True


# ---------------------------------------------------------------------------
# Vendor Node resolution
# ---------------------------------------------------------------------------


def test_resolve_node_launch_command_passes_through_non_node_commands() -> None:
    from argusai.core.infra.mcp_client import _resolve_node_launch_command

    cmd, source, runtime_dir = _resolve_node_launch_command("custom-binary", {})
    assert cmd == "custom-binary"
    assert source == "passthrough"
    assert runtime_dir == ""


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------


def test_mcp_error_payload_shape() -> None:
    from argusai.core.infra.mcp_client import MCPTransportSpec, mcp_error_payload

    spec = MCPTransportSpec(name="fake", transport="stdio", command="node")
    payload = mcp_error_payload(spec, error_code="timeout", error="too slow", retryable=True, suggested_fix="retry", tool="lookup")

    assert payload["ok"] is False
    assert payload["error_code"] == "timeout"
    assert payload["retryable"] is True
    assert payload["suggested_fix"] == "retry"
    assert payload["source"]["protocol"] == "mcp"
    assert payload["source"]["server"] == "fake"
    assert payload["source"]["tool"] == "lookup"


# ---------------------------------------------------------------------------
# New mcp_server_* tools (saved-config path with policy)
# ---------------------------------------------------------------------------


def test_mcp_server_list_tools_applies_policy(tmp_path: Path) -> None:
    from argusai.core._tools import mcp_server_list_tools

    api = FakeApi(tmp_path)
    server_script = _server_path(tmp_path)

    added = mcp_server_add(
        api=api,
        name="Fake",
        command=sys.executable,
        args=[str(server_script)],
        server_id="fake",
        policy_json=json.dumps({
            "default_risk": "medium",
            "deny_tools": [],
            "tool_risks": {"echo": "low"},
        }),
    )
    assert added["ok"] is True

    result = asyncio.run(mcp_server_list_tools(api=api, server_id="fake"))
    assert result["ok"] is True
    assert result["count"] == 1
    tool = result["tools"][0]
    assert tool["mcp_name"] == "echo"
    assert tool["risk"] == "low"
    assert tool["allowed"] is True
    assert result["policy_summary"]["default_risk"] == "medium"


def test_mcp_server_call_tool_denies_tools(tmp_path: Path) -> None:
    from argusai.core._tools import mcp_server_call_tool

    api = FakeApi(tmp_path)
    server_script = _server_path(tmp_path)

    mcp_server_add(
        api=api,
        name="Fake",
        command=sys.executable,
        args=[str(server_script)],
        server_id="fake",
        policy_json=json.dumps({"deny_tools": ["echo"]}),
    )

    result = asyncio.run(
        mcp_server_call_tool(
            api=api,
            server_id="fake",
            tool_name="echo",
            arguments_json=json.dumps({"text": "blocked"}),
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "tool_denied"
    assert result["policy_decision"]["allowed"] is False
    assert result["policy_decision"]["reason"] == "deny_listed"


def test_mcp_server_call_tool_succeeds(tmp_path: Path) -> None:
    from argusai.core._tools import mcp_server_call_tool

    api = FakeApi(tmp_path)
    server_script = _server_path(tmp_path)

    mcp_server_add(
        api=api,
        name="Fake",
        command=sys.executable,
        args=[str(server_script)],
        server_id="fake",
        policy_json=json.dumps({"allow_tools": ["echo"], "tool_risks": {"echo": "low"}}),
    )

    result = asyncio.run(
        mcp_server_call_tool(
            api=api,
            server_id="fake",
            tool_name="echo",
            arguments_json=json.dumps({"text": "hi"}),
        )
    )
    assert result["ok"] is True
    assert result["summary"] == "echo:hi"
    assert result["policy_decision"]["allowed"] is True


def test_mcp_server_call_tool_truncates_large_results(tmp_path: Path) -> None:
    """A small max_result_bytes triggers the truncation summary path.

    The settings normalizer clamps ``max_result_bytes`` to a floor of
    1024 so payloads at or below 1 KB always fit. Pick the smallest
    legal cap and a payload that comfortably exceeds it.
    """
    from argusai.core._tools import mcp_server_call_tool

    api = FakeApi(tmp_path)
    server_script = _server_path(tmp_path)

    mcp_server_add(
        api=api,
        name="Fake",
        command=sys.executable,
        args=[str(server_script)],
        server_id="fake",
        policy_json=json.dumps({"max_result_bytes": 1024}),
    )

    result = asyncio.run(
        mcp_server_call_tool(
            api=api,
            server_id="fake",
            tool_name="echo",
            arguments_json=json.dumps({"text": "x" * 4000}),
        )
    )
    assert result["ok"] is True
    assert result.get("truncated") is True
    assert result.get("truncate_reason") == "max_result_bytes"
    assert result.get("max_result_bytes") == 1024


# ---------------------------------------------------------------------------
# Session-level permission auto-allow (MCP per-server+tool)
# ---------------------------------------------------------------------------


def test_session_mcp_permission_key_and_auto_approve() -> None:
    """The session-level auto-allow set supports per-server-tool granularity."""
    from argusai.core.assistant import AssistantSession, ToolRegistry

    registry = ToolRegistry()
    session = AssistantSession(registry=registry)

    # Default: no permissions remembered
    assert session._tool_auto_approved("mcp_server_call_tool", {"server_id": "fs", "tool_name": "read_file"}) is False
    assert session._mcp_permission_key("mcp_server_call_tool", {"server_id": "fs", "tool_name": "read_file"}) == "mcp:fs:read_file"

    # Remember THIS permission key only
    session.remember_permission("mcp:fs:read_file")

    assert session._tool_auto_approved("mcp_server_call_tool", {"server_id": "fs", "tool_name": "read_file"}) is True
    # Different tool on same server still needs confirmation
    assert session._tool_auto_approved("mcp_server_call_tool", {"server_id": "fs", "tool_name": "write_file"}) is False
    # Different server too
    assert session._tool_auto_approved("mcp_server_call_tool", {"server_id": "other", "tool_name": "read_file"}) is False

