"""tool_search ranking and natural-language query coverage."""

from __future__ import annotations

import asyncio
import json

import pytest

from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import Tool, _score_tool_search, build_tool_registry


def _search(query: str, *, group: str = "", limit: int = 10) -> dict:
    registry = build_tool_registry(None)
    tool = registry.get("tool_search")
    assert tool is not None
    return tool.fn(query=query, group=group, limit=limit)


def test_shell_command_execute_system_finds_run_shell_snippet() -> None:
    """Regression: the motherboard-S/N bug used this query and got count=0."""
    out = _search("shell command execute system")
    assert out["ok"] is True
    assert out["count"] > 0
    names = [t["name"] for t in out["tools"]]
    assert "run_shell_snippet" in names


def test_run_command_terminal_finds_shell_tools() -> None:
    out = _search("run command terminal")
    assert out["ok"] is True
    assert out["count"] > 0
    names = [t["name"] for t in out["tools"]]
    assert any(n in names for n in ("run_shell_snippet", "run_shell_script", "discover_shell_environments"))


def test_desktop_hardware_query_finds_desktop_observe() -> None:
    out = _search("desktop system hardware information")
    assert out["ok"] is True
    assert out["count"] > 0
    names = [t["name"] for t in out["tools"]]
    assert "desktop_observe" in names


def test_single_keyword_shell_ranks_snippet_high() -> None:
    out = _search("shell snippet", limit=5)
    names = [t["name"] for t in out["tools"]]
    assert names[0] == "run_shell_snippet"
    assert "codebase_run_shell_snippet" not in names[:1]


def test_empty_query_lists_tools() -> None:
    out = _search("")
    assert out["ok"] is True
    assert out["count"] > 50
    assert out["activation_tools"] == []
    assert "catalogue listing" in out["note"]


def test_unwrap_catalogue_invoke_uses_inner_name() -> None:
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL, unwrap_catalogue_invoke

    name, args = unwrap_catalogue_invoke(
        INVOKE_CATALOGUE_TOOL,
        {"name": "cron_list", "arguments": {"limit": 3}},
    )
    assert name == "cron_list"
    assert args == {"limit": 3}


def test_invoke_catalogue_tool_confirmation_uses_inner_tool() -> None:
    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL, build_tool_registry

    session = AssistantSession(registry=build_tool_registry(None))
    reqs = session._confirmation_requirements([{
        "id": "1",
        "type": "function",
        "function": {
            "name": INVOKE_CATALOGUE_TOOL,
            "arguments": json.dumps({
                "name": "nmap_scan",
                "arguments": {"target": "1.1.1.1", "scope": "1.1.1.1"},
            }),
        },
    }])
    assert reqs
    assert reqs[0]["name"] == "nmap_scan"
    resumed_name, resumed_args = session._resolved_tool_call(
        reqs[0]["tool_call"],
        requirement=reqs[0],
    )
    assert resumed_name == "nmap_scan"
    assert resumed_args.get("target") == "1.1.1.1"
    raw_name = reqs[0]["tool_call"]["function"]["name"]
    assert raw_name == INVOKE_CATALOGUE_TOOL


def test_tool_search_returns_parameter_schemas_not_just_keys() -> None:
    out = _search("read file", limit=8)
    hit = next(item for item in out["tools"] if item["name"] == "read_text_file")
    assert isinstance(hit["parameters"], dict)
    assert "path" in hit["parameters"]
    assert isinstance(hit["parameters"]["path"], dict)
    assert hit["parameters"]["path"].get("type") == "string"


def test_tool_search_miss_allows_one_different_retry() -> None:
    out = _search("zzzznotatoolcapabilityxyz")
    assert out["ok"] is True
    assert out["count"] == 0
    assert out["activation_tools"] == []
    assert "Retry once" in out["note"]
    assert "two misses" in out["note"]
    assert "different" in out["next_step"]


def test_score_tool_search_prefers_name_token_hits() -> None:
    tool = Tool(
        name="run_shell_snippet",
        description="Write and run an inline shell snippet.",
        parameters={"script": {}, "shell": {}},
        fn=lambda: None,
        search_text="run shell snippet session sandbox powershell bash",
    )
    score = _score_tool_search(tool, ["shell", "command", "execute", "system"])
    assert score >= 12


def test_group_miss_falls_back_and_activates_cron_tools() -> None:
    out = _search("cron 定时任务创建", group="tasks")
    assert out["ok"] is True
    assert out["fallback_from_group"] == "tasks"
    names = [item["name"] for item in out["tools"]]
    assert "cron_upsert" in names
    assert "cron_upsert" in out["activation_tools"]


def test_unknown_group_also_falls_back_to_full_catalogue() -> None:
    out = _search("cron schedule", group="automation-that-does-not-exist")
    assert out["ok"] is True
    assert out["fallback_from_group"] == "automation-that-does-not-exist"
    assert "unknown tool group" in out["warning"]
    assert any(item["name"] == "cron_upsert" for item in out["tools"])


def test_search_discoveries_do_not_mutate_tools_array() -> None:
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL

    registry = build_tool_registry(None)
    session = AssistantSession(registry=registry)
    session._merge_prompt_tool_names([])
    current = registry.to_openai_tools(list(session._prompt_tool_names))
    before = [
        item["function"]["name"]
        for item in current
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    ]
    expanded, cursor = session._activate_discovered_tool_schemas(
        list(session._prompt_tool_names),
        current,
        [{
            "name": "tool_search",
            "result": {
                "ok": True,
                "activation_tools": ["nmap_scan", "missing_tool"],
            },
        }],
        0,
    )
    after = [
        item["function"]["name"]
        for item in expanded
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    ]
    assert after == before
    assert "nmap_scan" not in after
    assert INVOKE_CATALOGUE_TOOL in after
    assert cursor == 1


def test_agent_searches_without_growing_tool_list() -> None:
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL

    class DiscoveryAI:
        def __init__(self) -> None:
            self.tool_names_by_call: list[set[str]] = []

        async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
            names = {
                item["function"]["name"]
                for item in tools or []
                if isinstance(item, dict) and isinstance(item.get("function"), dict)
            }
            self.tool_names_by_call.append(names)
            if len(self.tool_names_by_call) == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": "",
                            "tool_calls": [{
                                "id": "search-1",
                                "type": "function",
                                "function": {
                                    "name": "tool_search",
                                    "arguments": json.dumps({
                                        "query": "nmap port scan",
                                        "group": "tasks",
                                    }, ensure_ascii=False),
                                },
                            }],
                        },
                    }],
                }
            return {"choices": [{"message": {"content": "found it", "tool_calls": []}}]}

    ai = DiscoveryAI()
    session = AssistantSession(registry=build_tool_registry(None), ai_provider=ai)
    response = asyncio.run(session.chat("帮我查找可用的调度能力", thinking_enabled=False))
    assert response.text == "found it"
    assert ai.tool_names_by_call[0] == ai.tool_names_by_call[1]
    assert "nmap_scan" not in ai.tool_names_by_call[0]
    assert INVOKE_CATALOGUE_TOOL in ai.tool_names_by_call[0]


def test_continue_loop_keeps_progressive_tool_list() -> None:
    registry = build_tool_registry(None)
    session = AssistantSession(registry=registry)
    _names, tools = session._continue_tool_state(
        [{"role": "user", "content": "hello"}],
        [],
    )
    names = {
        item["function"]["name"]
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    assert "tool_search" in names
    assert "invoke_catalogue_tool" in names
    assert "web_search" in names
    assert "spawn_agent" in names
    assert "cron_upsert" in names
    assert "sqli_probe_param" not in names
    assert "nmap_scan" not in names
    assert "read_text_file" not in names
    assert len(names) < 36

    _names, expanded = session._continue_tool_state(
        [{"role": "user", "content": "hello"}],
        [{
            "name": "tool_search",
            "result": {
                "ok": True,
                "activation_tools": ["nmap_scan"],
            },
        }],
    )
    expanded_names = {
        item["function"]["name"]
        for item in expanded
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    assert "nmap_scan" not in expanded_names
    assert "cron_upsert" in expanded_names
    assert "invoke_catalogue_tool" in expanded_names
    assert "sqli_probe_param" not in expanded_names


@pytest.mark.parametrize(
    ("query", "target", "k"),
    [
        ("nmap", "nmap_scan", 1),
        ("port scan", "nmap_scan", 3),
        ("扫端口", "nmap_scan", 3),
        ("端口扫描", "nmap_scan", 1),
        ("cron", "cron_upsert", 8),
        ("定时任务", "cron_upsert", 3),
        ("定时任务创建", "cron_upsert", 1),
        ("shell snippet", "run_shell_snippet", 1),
        ("shell command execute system", "run_shell_snippet", 3),
        ("sqli", "sqli_probe_param", 3),
        ("sql注入", "sqli_probe_param", 3),
        ("web search", "web_search", 1),
        ("上网搜索", "web_search", 1),
        ("read file", "read_text_file", 1),
        ("读文件", "read_text_file", 1),
        ("知识库", "kb_search", 3),
        ("mcp list tools", "mcp_server_list_tools", 3),
        ("desktop hardware", "desktop_observe", 3),
        ("nuclei", "nuclei_run_scan", 3),
        ("wasm decompile", "wasm_decompile", 1),
        ("反编译", "wasm_decompile", 8),
    ],
)
def test_tool_search_hits_expected_tool(query: str, target: str, k: int) -> None:
    names = [item["name"] for item in _search(query, limit=8)["tools"]]
    assert target in names[:k], f"{query!r} ranked {names}"


@pytest.mark.parametrize("query", ["看图", "读图", "识图", "inspect image", "vision_inspect_image"])
def test_session_tool_search_finds_vision_inspect(query: str) -> None:
    session = AssistantSession(registry=build_tool_registry(None))
    tool = session.registry.get("tool_search")
    assert tool is not None
    names = [item["name"] for item in tool.fn(query=query, limit=8)["tools"]]
    assert "vision_inspect_image" in names, f"{query!r} ranked {names}"


def test_session_core_tools_stay_tiny() -> None:
    from argusai.core.agent_runtimes import normalize_session_runtime_binding
    from argusai.core.assistant.tools import INVOKE_CATALOGUE_TOOL

    session = AssistantSession(registry=build_tool_registry(None))
    names = session._core_prompt_tool_names()
    assert "tool_search" in names
    assert INVOKE_CATALOGUE_TOOL in names
    assert "vision_inspect_image" in names
    assert "update_todo" in names
    assert "add_todos" in names
    assert "complete_todo_with_refs" in names
    assert "clear_todos" in names
    assert "web_search" in names
    assert "github_search" in names
    assert "spawn_agent" in names
    assert "list_agents" in names
    assert "cron_upsert" in names
    assert "cron_list" in names
    assert "nmap_scan" not in names
    assert "kb_search" not in names
    assert len(names) < 36

    session.allow_web_search = False
    offline = session._core_prompt_tool_names()
    assert "web_search" not in offline
    assert "github_search" not in offline
    assert "spawn_agent" in offline
    assert "cron_upsert" in offline
    assert len(offline) < 34

    session.allow_web_search = True
    session.runtime_binding = normalize_session_runtime_binding({
        "workspace_mode": "project",
        "project_dir": "H:/work/demo",
    })
    project = session._core_prompt_tool_names()
    assert "codebase_tree" in project
    assert "codebase_run_shell_snippet" in project
    assert "codebase_write_file" not in project
    assert "spawn_agent" in project
    assert "cron_upsert" in project
    assert len(project) < 36
