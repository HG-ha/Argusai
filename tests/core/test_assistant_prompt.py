from argusai.core.assistant.media_auxiliary import ASR_SYSTEM, VISION_SYSTEM
from argusai.core.assistant.skills import _build_system_prompt, _select_tool_names
from argusai.core.assistant.tools import ToolRegistry, build_tool_registry


def test_tool_catalog_can_be_limited_to_active_names() -> None:
    registry = ToolRegistry()
    registry.register("tool_search", "Search tools.", {}, lambda: None)
    registry.register("desktop_launch_app", "Launch desktop app.", {}, lambda: None)
    registry.register("get_app_snapshot", "Read app state.", {}, lambda: None)

    catalog = registry.catalog(["tool_search", "get_app_snapshot"])

    assert "`tool_search" in catalog
    assert "`get_app_snapshot" in catalog
    assert "desktop_launch_app" not in catalog


def test_system_prompt_uses_active_catalog_without_inactive_tools() -> None:
    registry = build_tool_registry(None)
    active_names = _select_tool_names("settings", "帮我看看 AI 端点设置")
    active_catalog = registry.catalog(active_names)
    full_catalog = registry.catalog()

    assert "desktop_launch_app" in full_catalog
    assert "desktop_launch_app" not in active_catalog
    assert "tool_search" in active_catalog

    prompt = _build_system_prompt(active_catalog, "")

    # The active **## Tools** block must only carry the active subset. The
    # always-on capability map above it lists representative tool *names* across
    # all domains by design (breadth), so scope the exclusion to the Tools block.
    tools_block = prompt.split("\n## Tools\n", 1)[1]
    assert "tool_search" in tools_block
    assert "desktop_launch_app" not in tools_block
    assert len(prompt) < len(_build_system_prompt(full_catalog, ""))


def test_base_system_prompt_keeps_workflows_in_skills() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "")

    assert "security assessment and reverse-engineering workbench expert" in prompt
    assert "web/API, mobile, native binaries, JavaScript/WASM, cloud, identity, network, proxy" in prompt
    assert "web security audit expert" not in prompt
    assert "ArgusAI is developed by 一铭 (GitHub: https://github.com/HG-ha)." in prompt
    assert "## Core Contract" in prompt
    assert "No ArgusAI-on-ArgusAI" in prompt
    assert "inspect, read, search, modify, build, test, run, stop, restart" in prompt
    assert "GREENLIGHT MODE does not override this rule" in prompt
    assert "## Skills and playbooks" in prompt
    assert "## Todos and progress" in prompt
    assert "set_todos" in prompt
    assert "skill_search" in prompt
    assert "MUST NOT improvise" in prompt
    assert "## Tools" in prompt
    assert "PortSwigger Web Security Academy uses Oracle" not in prompt
    assert "WebAssembly.compile" not in prompt
    # Workflow-specific tools must not be dumped into the active Tools block;
    # the capability map may still name them as domain representatives.
    tools_block = prompt.split("\n## Tools\n", 1)[1]
    assert "browser_launch_for_attach" not in tools_block


def test_system_prompt_auto_approve_tells_agent_to_decide() -> None:
    off = _build_system_prompt("- `tool_search(query)`: Search tools.", "")
    on = _build_system_prompt(
        "- `tool_search(query)`: Search tools.",
        "",
        agent_self_decide=True,
    )
    assert "必须使用 `stop_for_user_decision`" in off
    assert "AUTO-APPROVE is on" in on
    assert "agent_must_choose" in on
    assert "必须使用 `stop_for_user_decision`" not in on


def test_system_prompt_requires_proactive_delegation_and_question_discipline() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "")

    assert "Delegation is proactive" in prompt
    assert "without waiting for the user to request it" in prompt
    assert "never reasons to ask whether to continue" in prompt
    assert "facts you can discover" in prompt
    assert 'never ask "should I continue?"' in prompt
    assert "mandatory confirmation boundary" in prompt
    assert "fenced `mindmap`" in prompt or "mermaid `mindmap`" in prompt
    assert "UI-block first is mandatory for blocking questions" in prompt
    assert "Never end a response with a prose question" in prompt
    assert "`stop_for_user_decision_batch` for several related questions" in prompt


def test_media_auxiliary_prompts_match_broad_assistant_scope() -> None:
    for prompt in (VISION_SYSTEM, ASR_SYSTEM):
        assert "security assessment and reverse-engineering assistant" in prompt
        assert "security audit assistant" not in prompt


def test_system_prompt_injects_skill_shortlist_as_pointers_not_bodies() -> None:
    shortlist = "- `agent-todo`: Use when tracking multi-step task progress."
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", shortlist)

    # Per-turn shortlist is embedded as a pointer, and the model is told to pull
    # the full body via skill_read — no auto-attached bodies any more.
    assert "## Guidance (auto-attached excerpts)" not in prompt
    assert "Relevant playbooks matched for THIS task" in prompt
    assert shortlist in prompt
    assert "skill_read" in prompt
    assert "list_todos" in prompt
    assert "agent-todo" in prompt.lower()
