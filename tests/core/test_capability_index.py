"""Tests for lazily discoverable capability indexes."""

from argusai.core.assistant.skills import (
    TOOL_GROUPS,
    _SKILL_DOCS,
    _build_system_prompt,
    _skill_catalogue_index,
    _tool_domain_index,
)


def test_skill_catalogue_lists_every_model_invocable_skill() -> None:
    idx = _skill_catalogue_index()
    invocable = [s for s in _SKILL_DOCS if not s.disable_model_invocation]
    assert invocable, "fixture sanity: expected model-invocable skills to exist"
    for sd in invocable:
        assert f"`{sd.name}`" in idx, f"{sd.name} missing from the always-on skill index"


def test_skill_catalogue_excludes_non_invocable_skills() -> None:
    idx = _skill_catalogue_index()
    for sd in _SKILL_DOCS:
        if sd.disable_model_invocation:
            assert f"`{sd.name}`:" not in idx


def test_tool_domain_index_surfaces_capability_domains() -> None:
    idx = _tool_domain_index()
    for domain in ("binary", "mobile", "attack", "proxy", "session_sandbox"):
        assert f"**{domain}**" in idx, f"domain {domain} missing from capability map"
    assert "**always**" not in idx
    assert "**workflow**" not in idx


def test_tool_domain_index_picks_domain_specific_representatives() -> None:
    """The map must surface what makes a domain distinctive, not the generic
    cross-domain plumbing tools that appear at the head of many groups."""
    idx = _tool_domain_index()
    assert "binary_decompile" in idx
    assert "binary_patch" in idx
    assert "`domain_inventory`" not in idx


def test_system_prompt_keeps_indexes_lazy_but_exposes_capability_routes() -> None:
    sp = _build_system_prompt("", "")
    assert "Full skill catalogue" not in sp
    assert "Capability map" not in sp
    assert "binary-triage" not in sp
    assert "binary_decompile" not in sp
    assert "`proactive-defense`" in sp
    assert "`assistant-cron`" in sp
    assert "`desktop-automation`" in sp
    assert "`mcp-provision-workflow`" in sp
    assert "`multi-agent-orchestration`" in sp
    assert "`spawn_agent`" in sp
    assert "`run_subagent`" in sp
    assert "external CLIs/adapters" in sp
    assert "persistent shell sessions" in sp
    assert "skill_search" in sp
    assert "tool_search" in sp


def test_capability_map_covers_all_user_facing_groups() -> None:
    idx = _tool_domain_index()
    skipped = {"always", "workflow", "user_todos"}
    for group, tools in TOOL_GROUPS.items():
        if group in skipped or not tools:
            continue
        assert f"**{group}**" in idx, f"{group} should be represented in the capability map"
