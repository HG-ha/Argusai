"""Agent Team (spawn_team) fan-out + scoped-spawn scope relaxation + wait interrupt."""

from __future__ import annotations

import threading

from argusai.core.assistant.agent_threads import AgentThreadManager


def _bare_manager_with_fake_spawn():
    """A manager whose ``spawn`` is faked so we can test fan-out in isolation.

    ``spawn_team`` only touches ``self.spawn`` and the module-level presets, so a
    bare instance (no persistence/parent) is enough.
    """
    mgr = AgentThreadManager.__new__(AgentThreadManager)
    calls: list[dict] = []

    def fake_spawn(**kw):
        calls.append(kw)
        return {"ok": True, "agent": {"agent_id": f"agent-{len(calls)}", "role": kw["role"], "team": kw.get("team")}}

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    return mgr, calls


def test_spawn_team_preset_expands_to_roles() -> None:
    mgr, calls = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(preset="web_audit", objective="audit https://example.com")
    assert out["ok"] is True
    assert out["spawned"] == 3
    assert out["team"] == "Web 审计小队"
    roles = [c["role"] for c in calls]
    assert roles == ["recon", "vuln-analyst", "reporter"]
    # Every child is tagged with the team and inherits the shared objective + focus.
    assert all(c["team"] == "Web 审计小队" for c in calls)
    assert all("audit https://example.com" in c["objective"] for c in calls)
    assert all("本角色侧重" in c["objective"] for c in calls)


def test_spawn_team_explicit_roles_override_preset() -> None:
    mgr, calls = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(
        team="MyTeam",
        objective="go",
        roles=[
            {"role": "x", "objective": "do x"},
            {"role": "y", "focus": "do y"},
        ],
    )
    assert out["spawned"] == 2
    assert out["team"] == "MyTeam"
    assert calls[0]["objective"] == "do x"  # per-role objective wins
    assert "do y" in calls[1]["objective"]  # focus appended to shared objective


def test_spawn_team_requires_objective() -> None:
    mgr, _ = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(preset="web_audit", objective="")
    assert out["ok"] is False
    assert "objective" in out["error"]


def test_spawn_team_unknown_preset() -> None:
    mgr, _ = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(preset="nope", objective="go")
    assert out["ok"] is False
    assert "available_presets" in out
    assert "web_audit" in out["available_presets"]


def test_spawn_team_no_preset_no_roles() -> None:
    mgr, _ = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(objective="go")
    assert out["ok"] is False


def test_spawn_team_collects_errors() -> None:
    mgr = AgentThreadManager.__new__(AgentThreadManager)
    calls: list[dict] = []

    def flaky_spawn(**kw):
        calls.append(kw)
        if kw["role"] == "reporter":
            return {"ok": False, "error": "concurrency limit reached", "error_code": "subagent_concurrency_limit"}
        return {"ok": True, "agent": {"agent_id": f"a{len(calls)}", "role": kw["role"]}}

    mgr.spawn = flaky_spawn  # type: ignore[method-assign]
    out = mgr.spawn_team(preset="web_audit", objective="audit")
    assert out["spawned"] == 2
    assert len(out["errors"]) == 1
    assert out["errors"][0]["role"] == "reporter"
    assert out["ok"] is True  # partial success still ok when at least one spawned


def test_spawn_team_passes_scoped_without_scope_ref() -> None:
    """Scoped roles no longer require scope_ref; only requested_tools matter."""
    mgr, calls = _bare_manager_with_fake_spawn()
    out = mgr.spawn_team(
        team="Exec",
        objective="run checks",
        roles=[{"role": "exec", "permission": "scoped", "requested_tools": ["http_probe"]}],
    )
    assert out["spawned"] == 1
    assert calls[0]["permission"] == "scoped"
    assert calls[0]["scope_ref"] == ""  # not forced
    assert calls[0]["requested_tools"] == ["http_probe"]


# ---- wait_for_agents yields to the user without killing children ----

def _bare_manager_with_records(records):
    mgr = AgentThreadManager.__new__(AgentThreadManager)
    mgr._condition = threading.Condition()
    mgr._records = records
    return mgr


def test_wait_interrupts_on_user_and_keeps_children_running() -> None:
    records = {"agent-x": {"agent_id": "agent-x", "status": "running"}}
    mgr = _bare_manager_with_records(records)
    out = mgr.wait(["agent-x"], timeout_seconds=30, should_interrupt=lambda: True)
    assert out["ok"] is True
    assert out["interrupted_by_user"] is True
    assert out["completed"] is False
    assert out["running_agent_ids"] == ["agent-x"]
    # The child is untouched — interrupting the wait must not close/cancel it.
    assert mgr._records["agent-x"]["status"] == "running"


def test_wait_completes_when_children_terminal() -> None:
    records = {"agent-x": {"agent_id": "agent-x", "status": "completed"}}
    mgr = _bare_manager_with_records(records)
    out = mgr.wait(["agent-x"], timeout_seconds=30, should_interrupt=lambda: False)
    assert out["completed"] is True
    assert out["interrupted_by_user"] is False
    assert out["running_agent_ids"] == []


def test_wait_without_interrupt_callback_still_works() -> None:
    records = {"agent-x": {"agent_id": "agent-x", "status": "completed"}}
    mgr = _bare_manager_with_records(records)
    out = mgr.wait(["agent-x"], timeout_seconds=1)
    assert out["ok"] is True
    assert out["interrupted_by_user"] is False
