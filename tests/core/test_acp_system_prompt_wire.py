"""Exercise client systemPrompt on real, isolated fake-agent stdio frames."""
from __future__ import annotations

import asyncio
import json
import sys
import warnings
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import acp
import pytest
from acp import schema

from argusai.core.agent_runtimes.acp_runtime import ACPAgentProcess
from argusai.core.agent_runtimes.acp_turn import run_acp_turn
from argusai.core.agent_runtimes.models import AgentRuntimeConfig, SessionRuntimeBinding


FAKE_AGENT = r"""
import json, sys
capture, advertise = sys.argv[1], sys.argv[2] != 'no'
for line in sys.stdin:
    request = json.loads(line)
    with open(capture, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(request) + '\n')
    method, params = request.get('method'), request.get('params') or {}
    if method == 'initialize':
        capabilities = {'loadSession': True, 'sessionCapabilities': {'list': {}}}
        if advertise:
            capabilities['sessionCapabilities']['systemPrompt'] = {'mode': {'override': {}}} if sys.argv[2] == 'override' else {}
        result = {'protocolVersion': 1, 'agentInfo': {'name': 'wire-fixture', 'version': '1'}, 'agentCapabilities': capabilities}
    elif method in ('session/new', 'session/load'):
        blocks = (params.get('systemPrompt') or {}).get('content') or []
        if any(block.get('text') == 'reject fixture instructions' for block in blocks):
            print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32602,'message':'fixture rejected instructions'}}), flush=True)
            continue
        result = {'configOptions':[{'id':'model','name':'Model','type':'select','category':'model','currentValue':'fixture-model','options':[{'value':'fixture-model','name':'Fixture Model'}]}]}
        if method == 'session/new':
            result['sessionId'] = 'fixture-session'
    elif method == 'session/prompt':
        for text in ('first', 'second'):
            print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}}), flush=True)
        result = {'stopReason':'end_turn'}
    else:
        result = {}
    if 'id' in request:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
"""


def fake_process(tmp_path: Path, *, advertise: bool = True, override: bool = False, updates=None):
    script = tmp_path / "fake_system_prompt_agent.py"
    capture = tmp_path / "wire.jsonl"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    runtime = AgentRuntimeConfig(
        id="system-prompt-wire-fixture", name="Wire fixture", backend="acp",
        command=sys.executable, args=(str(script), str(capture), "override" if override else "yes" if advertise else "no"),
    )
    return ACPAgentProcess(runtime, cwd=str(tmp_path), event_sink=updates), capture


def test_initialize_preserves_wire_capability_without_changing_sdk_models(tmp_path: Path) -> None:
    fields_before = dict(schema.SessionCapabilities.model_fields)
    process, _ = fake_process(tmp_path)

    async def run():
        try:
            result = await process.initialize(timeout_seconds=5)
            assert isinstance(result, schema.InitializeResponse)
            with warnings.catch_warnings():
                warnings.simplefilter("error", UserWarning)
                payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert payload["agentCapabilities"]["sessionCapabilities"]["systemPrompt"] == {}
            assert payload["agentInfo"]["name"] == "wire-fixture"
        finally:
            await process.close()

    asyncio.run(run())
    assert schema.SessionCapabilities.model_fields == fields_before


@pytest.mark.parametrize("operation", ["new", "load"])
@pytest.mark.parametrize("mode", ["append", "override"])
def test_session_system_prompt_is_top_level_and_keeps_typed_config(tmp_path: Path, operation: str, mode: str) -> None:
    process, capture = fake_process(tmp_path, override=mode == "override")
    prompt = {"mode": mode, "content": [{"type": "text", "text": "fixture host instructions"}]}
    server = schema.McpServerStdio(
        name="Fixture Tools", command="not-executed", args=["fixture-only"],
        env=[schema.EnvVariable(name="FIXTURE_VALUE", value="demo")],
    )

    async def run():
        try:
            await process.initialize(timeout_seconds=5)
            if operation == "new":
                result = await process.new_session(str(tmp_path), mcp_servers=[server], system_prompt=prompt)
                assert isinstance(result, schema.NewSessionResponse)
                assert result.session_id == "fixture-session"
            else:
                result = await process.load_session(str(tmp_path), "fixture-session", mcp_servers=[server], system_prompt=prompt)
                assert isinstance(result, schema.LoadSessionResponse)
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert payload["configOptions"][0]["currentValue"] == "fixture-model"
        finally:
            await process.close()

    asyncio.run(run())
    requests = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    params = next(row["params"] for row in requests if row["method"] == f"session/{operation}")
    assert params["systemPrompt"] == prompt
    assert "_meta" not in params
    assert params["mcpServers"][0]["command"] == "not-executed"
    assert params["mcpServers"][0]["env"] == [{"name": "FIXTURE_VALUE", "value": "demo"}]
    if operation == "load":
        assert params["sessionId"] == "fixture-session"


def test_unadvertised_runtime_omits_system_prompt_and_keeps_ordered_updates(tmp_path: Path) -> None:
    updates = []
    process, capture = fake_process(tmp_path, advertise=False, updates=updates.append)

    async def run():
        try:
            initialized = await process.initialize(timeout_seconds=5)
            payload = initialized.model_dump(mode="json", by_alias=True, exclude_none=True)
            assert "systemPrompt" not in payload["agentCapabilities"]["sessionCapabilities"]
            created = await process.new_session(str(tmp_path), mcp_servers=[])
            await process.load_session(str(tmp_path), created.session_id, mcp_servers=[])
            await process.connection.prompt(session_id=created.session_id, prompt=[acp.text_block("fixture text")])
            assert [item["update"]["content"]["text"] for item in updates] == ["first", "second"]
        finally:
            await process.close()

    asyncio.run(run())
    requests = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    for row in requests:
        if row["method"] in {"session/new", "session/load"}:
            assert "systemPrompt" not in row["params"]
            assert "system_prompt" not in row["params"]
            assert "_meta" not in row["params"]
    assert process.connection is None
    assert process.process is None


@pytest.mark.parametrize("payload", [
    "instructions", {}, {"mode": "override", "content": []},
    {"mode": "append", "content": []},
    {"mode": "append", "content": "instructions"},
    {"mode": "append", "content": [{"type": "image", "data": "demo"}]},
    {"mode": "append", "content": [{"type": "text", "text": 123}]},
])
def test_invalid_system_prompt_fails_without_starting_a_process(payload) -> None:
    process = ACPAgentProcess(AgentRuntimeConfig(id="fixture", name="fixture", backend="acp", command="not-executed"))

    async def run():
        for operation in (process.new_session, process.load_session):
            with pytest.raises(ValueError, match="system_prompt"):
                if operation == process.new_session:
                    await operation("/fixture", system_prompt=payload)
                else:
                    await operation("/fixture", "fixture-session", system_prompt=payload)

    asyncio.run(run())
    assert process.process is None


@pytest.mark.parametrize("mode", ["append", "override"])
def test_system_prompt_rejection_is_not_silently_retried_without_it(tmp_path: Path, mode: str) -> None:
    process, capture = fake_process(tmp_path, override=mode == "override")

    async def run():
        try:
            await process.initialize(timeout_seconds=5)
            with pytest.raises(acp.RequestError, match="fixture rejected instructions"):
                await process.new_session(str(tmp_path), system_prompt={
                    "mode": mode, "content": [{"type": "text", "text": "reject fixture instructions"}],
                })
        finally:
            await process.close()

    asyncio.run(run())
    methods = [json.loads(line)["method"] for line in capture.read_text(encoding="utf-8").splitlines()]
    assert methods == ["initialize", "session/new"]


def test_system_prompt_requires_raw_wire_support_instead_of_meta_fallback() -> None:
    process = ACPAgentProcess(AgentRuntimeConfig(id="fixture", name="fixture", backend="acp", command="not-executed"))
    typed = AsyncMock()
    process.connection = SimpleNamespace(new_session=typed)
    with pytest.raises(RuntimeError, match="systemPrompt"):
        asyncio.run(process.new_session("/fixture", system_prompt={
            "mode": "append", "content": [{"type": "text", "text": "fixture instructions"}],
        }))
    typed.assert_not_called()


def test_empty_system_prompt_is_rejected_before_any_session_wire_request(tmp_path: Path) -> None:
    process, capture = fake_process(tmp_path)

    async def run():
        try:
            await process.initialize(timeout_seconds=5)
            with pytest.raises(ValueError, match="system_prompt.*empty"):
                await process.new_session(str(tmp_path), system_prompt={"mode": "append", "content": []})
            with pytest.raises(ValueError, match="system_prompt.*empty"):
                await process.load_session(str(tmp_path), "fixture-session", system_prompt={"mode": "append", "content": []})
        finally:
            await process.close()

    asyncio.run(run())
    methods = [json.loads(line)["method"] for line in capture.read_text(encoding="utf-8").splitlines()]
    assert methods == ["initialize"]


@pytest.mark.parametrize("mode", ["append", "override"])
def test_real_turn_new_and_load_keep_host_context_out_of_user_prompt(tmp_path: Path, mode: str) -> None:
    fixture, capture = fake_process(tmp_path, override=mode == "override")
    runtime = fixture.runtime
    binding = SessionRuntimeBinding(runtime_id=runtime.id, acp_system_prompt_mode=mode)
    instruction = "FIXTURE_HOST_POLICY: use only the agreed fixture tools."
    preferences = "FIXTURE_STYLE: answer in concise Chinese."
    messages = ["请核对演示材料。", "继续核对下一项演示材料。"]

    async def run():
        first = await run_acp_turn(
            runtime, binding, message=messages[0], cwd=str(tmp_path),
            host_instruction=instruction, user_habits=preferences, timeout_seconds=5,
        )
        assert first.ok, first.to_dict()
        resumed = replace(binding, acp_session_id=first.acp_session_id, acp_host_context=first.host_context)
        second = await run_acp_turn(
            runtime, resumed, message=messages[1], cwd=str(tmp_path),
            host_instruction=instruction, user_habits=preferences, timeout_seconds=5,
        )
        assert second.ok, second.to_dict()
        assert first.text == second.text == "firstsecond"
        assert first.host_context["delivery"] == second.host_context["delivery"] == "system_prompt"

    asyncio.run(run())
    requests = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    session_requests = [row for row in requests if row["method"] in {"session/new", "session/load"}]
    assert [row["method"] for row in session_requests] == ["session/new", "session/load"]
    for row in session_requests:
        params = row["params"]
        assert params["systemPrompt"]["mode"] == mode
        text = "\n".join(block["text"] for block in params["systemPrompt"]["content"])
        assert instruction in text and preferences in text
        assert "_meta" not in params
    prompts = [row["params"]["prompt"] for row in requests if row["method"] == "session/prompt"]
    assert [[block["text"] for block in prompt] for prompt in prompts] == [[message] for message in messages]
