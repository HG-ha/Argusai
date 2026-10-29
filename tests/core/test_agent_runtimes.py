from __future__ import annotations

import asyncio
import base64
import json
import sys
import warnings
from pathlib import Path

import pytest

from argusai.core.agent_runtimes import (
    AgentRuntimeConfig,
    SessionRuntimeBinding,
    discover_agent_runtimes,
    normalize_agent_runtimes,
    normalize_session_runtime_binding,
    run_acp_turn,
    runtime_configs,
    strip_synthetic_agent_text,
    test_acp_runtime as run_acp_handshake,
)
from argusai.core.agent_runtimes.acp_turn import build_acp_prompt_blocks
from argusai.core.assistant.attachments import (
    _materialize_acp_media_attachments,
    _materialize_binary_attachments,
    _materialize_text_attachments,
    _materialize_video,
)
from argusai.core.settings import AppSettings, _parse
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
from argusai.gui.agent_runtimes import AgentRuntimesMixin


def test_runtime_normalization_keeps_presets_and_safe_custom_entries() -> None:
    rows = normalize_agent_runtimes([
        {
            "id": "argusai-native",
            "enabled": False,
        },
        {
            "id": "opencode-acp",
            "enabled": False,
            "command": "custom-opencode",
            "args": ["acp", "serve"],
        },
        {
            "id": "my-agent",
            "name": "My Agent",
            "command": "my-agent",
            "args": ["serve", "bad\narg"],
            "env": {"SAFE_NAME": "value", "bad-name": "ignored"},
        },
        {
            "id": "broken",
            "command": "",
        },
    ])

    by_id = {row["id"]: row for row in rows}
    assert by_id["argusai-native"]["enabled"] is True
    assert by_id["opencode-acp"]["enabled"] is False
    assert by_id["opencode-acp"]["command"] == "custom-opencode"
    assert by_id["my-agent"]["args"] == ["serve"]
    assert by_id["my-agent"]["env"] == {"SAFE_NAME": "value"}
    assert "broken" not in by_id
    assert {"codebuddy-acp", "trae-acp", "qoder-acp", "kimi-acp"} <= set(by_id)


def test_runtime_settings_round_trip() -> None:
    original = AppSettings(agent_runtimes=normalize_agent_runtimes([
        {
            "id": "local-test",
            "name": "Local Test",
            "command": sys.executable,
            "args": ["agent.py"],
        },
    ]))

    restored = _parse(original.to_dict())

    by_id = {row["id"]: row for row in restored.agent_runtimes}
    assert by_id["local-test"]["command"] == sys.executable
    assert by_id["local-test"]["args"] == ["agent.py"]


def test_runtime_save_generates_internal_id() -> None:
    settings = AppSettings()

    class SettingsStore:
        def get(self):
            return settings

        def save(self, value):
            assert value is settings

    host = AgentRuntimesMixin()
    host.settings_mgr = SettingsStore()

    result = host.save_agent_runtime({
        "name": "My Coding Agent",
        "command": "my-agent",
        "args": ["acp"],
        "enabled": True,
    })

    assert result["ok"] is True
    assert result["runtime"]["id"] == "custom-my-coding-agent"


def test_runtime_discovery_does_not_launch_process(monkeypatch) -> None:
    calls: list[str] = []

    def fake_which(command: str, path: str | None = None) -> str | None:
        del path
        calls.append(command)
        return f"/mock/bin/{command}" if command == "opencode" else None

    monkeypatch.setattr("argusai.core.agent_runtimes.discovery.shutil.which", fake_which)
    inventory = discover_agent_runtimes(normalize_agent_runtimes(None), path_env="/mock/bin")
    rows = {row["id"]: row for row in inventory["runtimes"]}

    assert rows["argusai-native"]["probe"]["source"] == "builtin"
    assert rows["opencode-acp"]["probe"]["available"] is True
    assert calls.count("opencode") == 1


def test_acp_runtime_initialize_with_fake_agent(tmp_path: Path) -> None:
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    if request.get('method') == 'initialize':",
            "        result = {",
            "            'protocolVersion': 1,",
            "            'agentInfo': {'name': 'fake-acp', 'version': '1.0'},",
            "            'agentCapabilities': {},",
            "            'authMethods': [],",
            "        }",
            "        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
            "        break",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-acp",
        name="Fake ACP",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        result = asyncio.run(run_acp_handshake(runtime, timeout_seconds=5))

    assert result["ok"] is True, json.dumps(result, ensure_ascii=False)
    assert result["protocol_version"] == 1
    assert result["agent_info"]["name"] == "fake-acp"


def test_runtime_configs_returns_typed_entries() -> None:
    entries = runtime_configs(None)
    assert entries[0].id == "argusai-native"
    assert entries[0].backend == "native"


def test_runtime_normalization_includes_cursor_codex_claude_presets() -> None:
    by_id = {row["id"]: row for row in normalize_agent_runtimes([])}
    assert by_id["cursor-acp"]["command"] == "agent"
    assert by_id["cursor-acp"]["args"] == ["acp"]
    assert by_id["codex-acp"]["command"] == "npx"
    assert by_id["codex-acp"]["args"] == ["-y", "@agentclientprotocol/codex-acp"]
    assert by_id["claude-code-acp"]["command"] == "npx"
    assert by_id["claude-code-acp"]["args"] == ["-y", "@agentclientprotocol/claude-agent-acp"]


def test_session_runtime_binding_persists_acp_config() -> None:
    binding = SessionRuntimeBinding(
        runtime_id="codex-acp",
        acp_session_id="s1",
        locked=True,
        acp_config={"model": "gpt-5"},
        acp_prompt_capabilities={
            "image": True,
            "audio": False,
            "embedded_context": True,
        },
        acp_config_options=(
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "category": "model",
                "current_value": "gpt-5",
                "options": [
                    {"value": "gpt-5", "name": "GPT-5"},
                    {"value": "o3", "name": "o3"},
                ],
            },
        ),
    )
    restored = normalize_session_runtime_binding(binding.to_dict())
    assert restored.acp_config == {"model": "gpt-5"}
    assert restored.acp_prompt_capabilities == {
        "image": True,
        "audio": False,
        "embedded_context": True,
    }
    assert restored.acp_config_options[0]["current_value"] == "gpt-5"
    assert len(restored.acp_config_options[0]["options"]) == 2

    session = AssistantSession(registry=ToolRegistry())
    session.runtime_binding = SessionRuntimeBinding(
        runtime_id="opencode-acp",
        workspace_mode="project",
        project_dir="C:/work/demo",
        acp_session_id="acp-session-1",
        locked=True,
    )

    restored = AssistantSession.from_dict(session.to_dict(), ToolRegistry())

    assert restored.runtime_binding.runtime_id == "opencode-acp"
    assert restored.runtime_binding.acp_session_id == "acp-session-1"
    assert restored.runtime_binding.locked is True


def test_preserve_session_project_dir_survives_empty_snapshot() -> None:
    from argusai.core.agent_runtimes import (
        acp_workspace_instruction,
        preserve_session_project_dir,
    )

    current = normalize_session_runtime_binding({
        "runtime_id": "codex-acp",
        "workspace_mode": "project",
        "project_dir": "C:/work/demo",
    })
    dropped = normalize_session_runtime_binding({
        "runtime_id": "codex-acp",
        "workspace_mode": "project",
        "project_dir": "",
    })
    assert dropped.workspace_mode == "sandbox"
    kept = preserve_session_project_dir(current, dropped)
    assert kept.workspace_mode == "project"
    assert kept.project_dir == "C:/work/demo"

    note = acp_workspace_instruction("C:/work/demo", project=True)
    assert "C:/work/demo" in note
    assert "sandbox_*" in note
    assert acp_workspace_instruction("/tmp/sandbox", project=False) == ""


def test_acp_turn_streams_text_and_returns_session_id(tmp_path: Path) -> None:
    script = tmp_path / "fake_acp_turn.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True, 'auth': {}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'fake-session'}",
            "    elif method == 'session/prompt':",
            "        update = {",
            "            'jsonrpc': '2.0',",
            "            'method': 'session/update',",
            "            'params': {",
            "                'sessionId': 'fake-session',",
            "                'update': {",
            "                    'sessionUpdate': 'agent_message_chunk',",
            "                    'content': {'type': 'text', 'text': 'hello from ACP'},",
            "                },",
            "            },",
            "        }",
            "        print(json.dumps(update), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-turn",
        name="Fake Turn",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )
    seen: list[dict] = []

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-turn"),
        message="hello",
        cwd=str(tmp_path),
        on_update=seen.append,
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.acp_session_id == "fake-session"
    assert result.text == "hello from ACP"
    assert result.stop_reason == "end_turn"
    assert seen[0]["sessionUpdate"] == "agent_message_chunk"


def test_strip_synthetic_agent_text_drops_placeholder_turns() -> None:
    assert strip_synthetic_agent_text("No response requested.") == ""
    assert strip_synthetic_agent_text(
        "No response requested.No response requested.\n\n真正的回答"
    ) == "真正的回答"
    assert strip_synthetic_agent_text("hello from ACP") == "hello from ACP"


def test_acp_turn_drops_synthetic_no_response_chunks(tmp_path: Path) -> None:
    script = tmp_path / "fake_acp_noise.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "def emit(text):",
            "    print(json.dumps({",
            "        'jsonrpc': '2.0', 'method': 'session/update',",
            "        'params': {'sessionId': 'noisy-session', 'update': {",
            "            'sessionUpdate': 'agent_message_chunk',",
            "            'content': {'type': 'text', 'text': text},",
            "        }},",
            "    }), flush=True)",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'noisy-session'}",
            "    elif method == 'session/prompt':",
            "        emit('No response requested.')",
            "        emit('No response requested.')",
            "        emit('real answer')",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-noise",
        name="Fake Noise",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )
    seen: list[dict] = []

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-noise"),
        message="hello",
        cwd=str(tmp_path),
        on_update=seen.append,
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.text == "real answer"
    chunks = [
        update["content"]["text"]
        for update in seen
        if update.get("sessionUpdate") == "agent_message_chunk"
    ]
    assert chunks == ["real answer"]


def test_acp_turn_pushes_the_stored_model_preference_to_the_agent(tmp_path: Path) -> None:
    script = tmp_path / "fake_acp_config.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "option = {",
            "    'id': 'model', 'name': 'Model', 'type': 'select', 'category': 'model',",
            "    'currentValue': 'm1',",
            "    'options': [",
            "        {'value': 'm1', 'name': 'Model One'},",
            "        {'value': 'm2', 'name': 'Model Two'},",
            "    ],",
            "}",
            "applied = []",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    params = request.get('params') or {}",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'cfg-session', 'configOptions': [option]}",
            "    elif method == 'session/set_config_option':",
            "        config_id = params.get('configId') or params.get('config_id')",
            "        applied.append([config_id, params.get('value')])",
            "        option['currentValue'] = params.get('value')",
            "        result = {'configOptions': [option]}",
            "    elif method == 'session/prompt':",
            "        print(json.dumps({",
            "            'jsonrpc': '2.0', 'method': 'session/update',",
            "            'params': {'sessionId': 'cfg-session', 'update': {",
            "                'sessionUpdate': 'agent_message_chunk',",
            "                'content': {'type': 'text', 'text': json.dumps(applied)},",
            "            }},",
            "        }), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-config",
        name="Fake Config",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-config", acp_config={"model": "m2"}),
        message="hello",
        cwd=str(tmp_path),
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    # The agent echoes what it was actually told to set, so this fails if the
    # preference never left the client.
    assert json.loads(result.text) == [["model", "m2"]]
    assert result.config_options[0]["current_value"] == "m2"


def test_a_replayed_transcript_is_not_counted_as_this_turn(tmp_path: Path) -> None:
    """`session/load` re-emits the whole history before it answers.

    Those chunks are the previous turns, already on screen. Treating them as
    output repeated the last answer above the new one in every reply after the
    first.
    """
    script = tmp_path / "fake_acp_replay.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "def send(obj):",
            "    print(json.dumps(obj), flush=True)",
            "def chunk(text):",
            "    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {",
            "        'sessionId': 'old-session', 'update': {",
            "            'sessionUpdate': 'agent_message_chunk',",
            "            'content': {'type': 'text', 'text': text},",
            "        }}})",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {",
            "            'auth': {}, 'loadSession': True}}",
            "    elif method == 'session/load':",
            "        chunk('PAST ANSWER')",
            "        result = {}",
            "    elif method == 'session/prompt':",
            "        chunk('new answer')",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-replay",
        name="Fake Replay",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )
    seen: list[dict] = []

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(
            runtime_id="fake-replay",
            acp_session_id="old-session",
            locked=True,
        ),
        message="where are you",
        cwd=str(tmp_path),
        on_update=seen.append,
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.text == "new answer"
    assert not any("PAST ANSWER" in json.dumps(update) for update in seen)


def test_the_agent_process_starts_in_the_session_directory(tmp_path: Path) -> None:
    """Agents read AGENTS.md/CLAUDE.md from wherever they were launched.

    Inheriting ArgusAI's own directory handed a sandboxed session the ArgusAI
    checkout to read and obey.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "fake_acp_cwd.py"
    script.write_text(
        "\n".join([
            "import json, os, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'auth': {}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'cwd-session'}",
            "    elif method == 'session/prompt':",
            "        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update',",
            "            'params': {'sessionId': 'cwd-session', 'update': {",
            "                'sessionUpdate': 'agent_message_chunk',",
            "                'content': {'type': 'text', 'text': os.getcwd()},",
            "            }}}), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-cwd",
        name="Fake Cwd",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-cwd"),
        message="where are you",
        cwd=str(workspace),
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert Path(result.text).resolve() == workspace.resolve()


def _permission_agent_script(path: Path) -> Path:
    """A fake agent that asks the client to approve one of its own tools."""
    path.write_text(
        "\n".join([
            "import json, sys",
            "def send(obj):",
            "    print(json.dumps(obj), flush=True)",
            "while True:",
            "    line = sys.stdin.readline()",
            "    if not line:",
            "        break",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method is None:",
            "        continue",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'auth': {}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'perm-session'}",
            "    elif method == 'session/prompt':",
            "        send({'jsonrpc': '2.0', 'id': 9001,",
            "              'method': 'session/request_permission', 'params': {",
            "            'sessionId': 'perm-session',",
            "            'toolCall': {'toolCallId': 'call-1', 'kind': 'execute',",
            "                         'title': 'Run a shell command',",
            "                         'rawInput': {'command': 'rm -rf /tmp/x'}},",
            "            'options': [",
            "                {'optionId': 'once', 'name': 'Allow once', 'kind': 'allow_once'},",
            "                {'optionId': 'always', 'name': 'Always', 'kind': 'allow_always'},",
            "                {'optionId': 'no', 'name': 'Reject', 'kind': 'reject_once'},",
            "            ]}})",
            "        reply = json.loads(sys.stdin.readline())",
            "        send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {",
            "            'sessionId': 'perm-session', 'update': {",
            "                'sessionUpdate': 'agent_message_chunk',",
            "                'content': {'type': 'text',",
            "                            'text': json.dumps(reply.get('result'))},",
            "            }}})",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})",
        ]),
        encoding="utf-8",
    )
    return path


def _run_permission_turn(tmp_path: Path, handler) -> dict:
    script = _permission_agent_script(tmp_path / "fake_acp_permission.py")
    runtime = AgentRuntimeConfig(
        id="fake-permission",
        name="Fake Permission",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )
    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-permission"),
        message="do the thing",
        cwd=str(tmp_path),
        on_permission=handler,
        timeout_seconds=10,
    ))
    assert result.ok is True, result.to_dict()
    return json.loads(result.text)["outcome"]


def test_an_approved_agent_tool_selects_the_one_off_option(tmp_path: Path) -> None:
    """Approving once must not silently become approving forever."""
    seen: list[dict] = []

    def _handler(request: dict) -> str:
        seen.append(request)
        return "allow"

    outcome = _run_permission_turn(tmp_path, _handler)

    assert outcome == {"outcome": "selected", "optionId": "once"}
    assert seen[0]["agent"] == "fake-permission"
    assert seen[0]["tool_call"]["kind"] == "execute"


def test_a_session_grant_selects_the_option_that_stops_the_asking(
    tmp_path: Path,
) -> None:
    outcome = _run_permission_turn(tmp_path, lambda request: "allow_session")

    assert outcome == {"outcome": "selected", "optionId": "always"}


def test_a_denied_agent_tool_is_cancelled(tmp_path: Path) -> None:
    outcome = _run_permission_turn(tmp_path, lambda request: "deny")

    assert outcome == {"outcome": "cancelled"}


def test_without_a_handler_an_agent_tool_is_still_refused(tmp_path: Path) -> None:
    """No handler means nobody is watching, so nobody can approve."""
    outcome = _run_permission_turn(tmp_path, None)

    assert outcome == {"outcome": "cancelled"}


def test_acp_receives_the_users_message_without_client_instructions(
    tmp_path: Path,
) -> None:
    """ACP has no trusted system-prompt channel; do not fake one in user text."""
    script = tmp_path / "fake_acp_prompt.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'auth': {}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'prompt-session'}",
            "    elif method == 'session/prompt':",
            "        text = request['params']['prompt'][0]['text']",
            "        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update',",
            "            'params': {'sessionId': 'prompt-session', 'update': {",
            "                'sessionUpdate': 'agent_message_chunk',",
            "                'content': {'type': 'text', 'text': text},",
            "            }}}), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-prompt",
        name="Fake Prompt",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id="fake-prompt"),
        message="analyze the authorized staging target",
        cwd=str(tmp_path),
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.text == "analyze the authorized staging target"


def test_acp_turn_replays_transcript_when_load_session_is_unsupported(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_acp_no_load.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {'auth': {}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'fresh-session'}",
            "    elif method == 'session/prompt':",
            "        used_fallback = 'TRANSCRIPT FALLBACK' in json.dumps(request)",
            "        update = {",
            "            'jsonrpc': '2.0', 'method': 'session/update',",
            "            'params': {'sessionId': 'fresh-session', 'update': {",
            "                'sessionUpdate': 'agent_message_chunk',",
            "                'content': {'type': 'text', 'text': 'fallback' if used_fallback else 'current'},",
            "            }},",
            "        }",
            "        print(json.dumps(update), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-no-load",
        name="Fake No Load",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(
            runtime_id="fake-no-load",
            acp_session_id="old-session",
            locked=True,
        ),
        message="current turn",
        stateless_message="TRANSCRIPT FALLBACK",
        cwd=str(tmp_path),
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.session_restarted is True
    assert result.acp_session_id == "fresh-session"
    assert result.text == "fallback"


def test_acp_image_prompt_follows_model_config_and_persists_capability(tmp_path: Path) -> None:
    script = tmp_path / "fake_acp_image.py"
    capture = tmp_path / "image-prompt.json"
    script.write_text(
        "\n".join([
            "import json, sys",
            "capture = sys.argv[1]",
            "order = []",
            "option = {'id': 'model', 'name': 'Model', 'type': 'select', 'category': 'model',",
            "          'currentValue': 'm1', 'options': [{'value': 'm1', 'name': 'One'}, {'value': 'm2', 'name': 'Two'}]}",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    params = request.get('params') or {}",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {",
            "            'loadSession': True, 'promptCapabilities': {'image': True}}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'image-session', 'configOptions': [option]}",
            "    elif method == 'session/set_config_option':",
            "        order.append(method)",
            "        option['currentValue'] = params.get('value')",
            "        result = {'configOptions': [option]}",
            "    elif method == 'session/prompt':",
            "        order.append(method)",
            "        with open(capture, 'w', encoding='utf-8') as fh:",
            "            json.dump({'order': order, 'params': params}, fh)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-image",
        name="Fake Image",
        backend="acp",
        command=sys.executable,
        args=(str(script), str(capture)),
    )
    image_data = "iVBORw0KGgo="

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(
            runtime_id="fake-image",
            acp_config={"model": "m2"},
        ),
        message="",
        attachments=[{
            "type": "image",
            "mime": "image/png",
            "data_base64": image_data,
            "name": "sample.png",
        }],
        attachment_resources=[{
            "type": "image",
            "mime": "image/png",
            "name": "sample.png",
            "sandbox_path": str(tmp_path / "sample.png"),
            "size": 8,
        }],
        cwd=str(tmp_path),
        timeout_seconds=5,
    ))

    assert result.ok is True, result.to_dict()
    assert result.prompt_capabilities == {
        "image": True,
        "audio": False,
        "embedded_context": False,
    }
    payload = json.loads(capture.read_text(encoding="utf-8"))
    assert payload["order"] == [
        "session/set_config_option",
        "session/prompt",
    ]
    blocks = payload["params"]["prompt"]
    assert blocks == [{
        "type": "image",
        "data": image_data,
        "mimeType": "image/png",
        "uri": (tmp_path / "sample.png").resolve().as_uri(),
    }]
    assert image_data not in json.dumps(result.to_dict())


@pytest.mark.parametrize("image_capability", [None, False])
def test_acp_rejects_images_before_prompt_when_capability_is_missing_or_false(
    tmp_path: Path,
    image_capability: bool | None,
) -> None:
    script = tmp_path / f"fake_acp_no_image_{image_capability}.py"
    capture = tmp_path / f"methods-{image_capability}.txt"
    capability_line = (
        "'promptCapabilities': {'image': False},"
        if image_capability is False
        else ""
    )
    script.write_text(
        "\n".join([
            "import json, sys",
            "capture = sys.argv[1]",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    with open(capture, 'a', encoding='utf-8') as fh:",
            "        fh.write(method + '\\n')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {",
            f"            {capability_line}",
            "            'loadSession': True}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'no-image-session'}",
            "    elif method == 'session/prompt':",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id=f"fake-no-image-{image_capability}",
        name="Fake No Image",
        backend="acp",
        command=sys.executable,
        args=(str(script), str(capture)),
    )

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id=runtime.id),
        message="inspect",
        attachments=[{
            "type": "image",
            "mime": "image/png",
            "data_base64": "iVBORw0KGgo=",
        }],
        cwd=str(tmp_path),
        timeout_seconds=5,
    ))

    assert result.ok is False
    assert result.error_code == "acp_image_unsupported"
    assert "session/prompt" not in capture.read_text(encoding="utf-8")


def test_acp_attachments_are_sandboxed_and_rendered_as_safe_resources(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "session-sandbox"
    project = tmp_path / "user-project"
    project.mkdir()
    encoded = base64.b64encode(b"attachment-bytes").decode("ascii")
    attachments = [
        {
            "type": "text",
            "mime": "text/plain",
            "name": "../notes.txt",
            "text": "safe preview",
            "size": 12,
        },
        {
            "type": "file",
            "mime": "application/pdf",
            "name": "../report.pdf",
            "data_base64": encoded,
            "size": 16,
        },
        {
            "id": "history-image-1",
            "type": "image",
            "mime": "image/png",
            "name": "../same.png",
            "data_base64": encoded,
            "size": 16,
        },
        {
            "type": "image",
            "mime": "image/png",
            "name": "../same.png",
            "data_base64": encoded,
            "size": 16,
        },
        {
            "type": "audio",
            "mime": "audio/ogg",
            "name": "../voice.ogg",
            "data_base64": encoded,
            "size": 16,
        },
        {
            "type": "video",
            "mime": "video/mp4",
            "name": "../clip.mp4",
            "data_base64": encoded,
            "size": 16,
        },
    ]

    stored, text_addendum = _materialize_text_attachments(attachments, sandbox)
    stored, binary_addendum = _materialize_binary_attachments(stored, sandbox)
    stored = _materialize_video(stored, sandbox)
    stored = _materialize_acp_media_attachments(stored, sandbox)

    assert "_attachments/notes.txt" in text_addendum
    assert "_attachments/report.pdf" in binary_addendum
    sandbox_resolved = sandbox.resolve()
    for item in stored:
        path = Path(str(item["sandbox_path"])).resolve()
        assert path.is_relative_to(sandbox_resolved)
        assert path.is_file()
    assert stored[2]["sandbox_rel"] == "_images/same.png"
    assert stored[2]["id"] == "history-image-1"
    assert stored[3]["sandbox_rel"] == "_images/same-2.png"
    assert stored[4]["sandbox_rel"] == "_audio/voice.ogg"
    assert stored[5]["sandbox_rel"].startswith("_video/")
    assert "data_base64" not in stored[0]
    assert "data_base64" not in stored[1]
    assert "data_base64" not in stored[5]
    assert list(project.iterdir()) == []

    blocks = build_acp_prompt_blocks(
        "inspect these files",
        attachments,
        stored,
        {
            "image": True,
            "audio": False,
            "embedded_context": True,
        },
    )
    payloads = [
        block.model_dump(mode="json", by_alias=True, exclude_none=True)
        for block in blocks
    ]
    assert payloads[0] == {"type": "text", "text": "inspect these files"}
    assert payloads[1]["type"] == "resource"
    assert payloads[1]["resource"]["text"] == "safe preview"
    assert payloads[2]["type"] == "resource_link"
    assert payloads[2]["uri"].startswith("file:")
    assert payloads[3]["type"] == "image"
    assert payloads[4]["type"] == "image"
    assert payloads[5]["type"] == "resource_link"
    assert payloads[6]["type"] == "resource_link"
    assert encoded not in json.dumps(payloads[1:3] + payloads[5:])


def test_acp_session_updates_keep_wire_order_through_prompt_completion(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake_acp_ordered_updates.py"
    script.write_text(
        "\n".join([
            "import json, sys",
            "for line in sys.stdin:",
            "    request = json.loads(line)",
            "    method = request.get('method')",
            "    if method == 'initialize':",
            "        result = {'protocolVersion': 1, 'agentCapabilities': {}}",
            "    elif method == 'session/new':",
            "        result = {'sessionId': 'ordered-session'}",
            "    elif method == 'session/prompt':",
            "        for index in range(80):",
            "            update = {",
            "                'jsonrpc': '2.0', 'method': 'session/update',",
            "                'params': {'sessionId': 'ordered-session', 'update': {",
            "                    'sessionUpdate': 'agent_message_chunk',",
            "                    'content': {'type': 'text', 'text': f'{index:02d}|'},",
            "                }},",
            "            }",
            "            print(json.dumps(update), flush=True)",
            "        result = {'stopReason': 'end_turn'}",
            "    else:",
            "        result = {}",
            "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)",
        ]),
        encoding="utf-8",
    )
    runtime = AgentRuntimeConfig(
        id="fake-ordered-updates",
        name="Fake Ordered Updates",
        backend="acp",
        command=sys.executable,
        args=(str(script),),
    )
    seen: list[str] = []

    result = asyncio.run(run_acp_turn(
        runtime,
        SessionRuntimeBinding(runtime_id=runtime.id),
        message="go",
        cwd=str(tmp_path),
        on_update=lambda update: seen.append(
            str((update.get("content") or {}).get("text") or "")
        ),
        timeout_seconds=5,
    ))

    expected = [f"{index:02d}|" for index in range(80)]
    assert result.ok is True, result.to_dict()
    assert seen == expected
    assert result.text == "".join(expected)
