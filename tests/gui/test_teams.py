"""Tests for the standing Agent Teams backend (gui.teams.TeamsMixin).

The child-Agent manager is stubbed so these exercise team CRUD, member
wiring and status aggregation without an AI provider or real threads.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from argusai.gui.teams import TeamsMixin


class _FakeManager:
    """Stand-in for AgentThreadManager: records spawns, returns a roster."""

    def __init__(self) -> None:
        self.agents: list[dict] = []
        self.spawned: list[dict] = []

    def list(self, *, include_closed: bool = False):
        return {"ok": True, "agents": list(self.agents)}

    def spawn(self, **kwargs):
        start = kwargs.get("start_immediately", True)
        agent = {
            "agent_id": f"agent-{len(self.agents)}",
            "role": kwargs.get("role"),
            "team": kwargs.get("team"),
            "status": "running" if start else "paused",
            "pending_confirmation": None,
        }
        self.spawned.append(kwargs)
        self.agents.append(agent)
        return {"ok": True, "agent": agent}

    def spawn_team(self, **kwargs):
        self.spawned_team = kwargs
        roles = {"web_audit": 3, "recon_sweep": 2, "reverse_triage": 2}.get(kwargs.get("preset"), 0)
        for i in range(roles):
            self.spawn(
                role=f"{kwargs.get('preset')}-{i}",
                objective=kwargs.get("objective"),
                team=kwargs.get("team"),
                start_immediately=kwargs.get("start_immediately", True),
            )
        return {"ok": roles > 0, "spawned": roles, "agents": self.agents[-roles:] if roles else [], "errors": []}

    def get(self, agent_id, *, include_detail=True):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                return {
                    "ok": True,
                    "agent": {
                        **a,
                        "history": a.get("history", [{"role": "assistant", "content": "ok"}]),
                    },
                }
        return {"ok": False, "error": "agent not found"}

    def send_input(self, agent_id, message, *, resume=False, attachments=None, room_visibility=None):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                a.setdefault("inputs", []).append(message)
                if attachments:
                    a.setdefault("attachments", []).extend(attachments)
                a["status"] = "running"
                return {"ok": True, "agent": a}
        return {"ok": False, "error": "agent not found"}

    def halt(self, agent_id):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                status = str(a.get("status") or "")
                stopped = status in {"queued", "running", "closing"}
                if stopped:
                    a["status"] = "paused"
                    a["pending_inputs"] = []
                return {"ok": True, "agent": a, "stopped": stopped}
        return {"ok": False, "error": "agent not found"}

    def set_greenlight(self, agent_id, enabled):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                a["greenlight"] = bool(enabled)
                return {"ok": True, "agent": a, "greenlight": bool(enabled)}
        return {"ok": False, "error": "agent not found"}

    def update(self, agent_id, patch):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                a.update(patch)
                return {"ok": True, "agent": a, "applies_on_next_turn": a["status"] == "running"}
        return {"ok": False, "error": "agent not found"}


class _TeamApi(TeamsMixin):
    def __init__(self, sessions_dir) -> None:
        self.settings_mgr = SimpleNamespace(get=lambda: SimpleNamespace(sessions_dir=str(sessions_dir)))
        self._managers: dict[str, _FakeManager] = {}
        self.dropped: list[str] = []
        self.removed: list[str] = []

    @property
    def _sessions_dir(self) -> Path:
        p = Path(self.settings_mgr.get().sessions_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Bypass the real lead-session / AgentThreadManager machinery.
    def _team_manager(self, team_id):
        tid = self._validate_team_id(team_id)
        return self._managers.setdefault(tid, _FakeManager())

    def _drop_session_runtime(self, session_id):
        self.dropped.append(session_id)

    def _remove_session_storage(self, session_id):
        self.removed.append(session_id)
        return {}


def test_create_list_get_rename_delete(tmp_path) -> None:
    api = _TeamApi(tmp_path)

    created = api.create_team({"name": "Recon", "objective": "map surface"})
    assert created["ok"] is True
    tid = created["team"]["team_id"]
    assert (tmp_path / "_teams" / f"{tid}.json").exists()

    listed = api.list_teams()
    assert listed["ok"] is True
    assert len(listed["teams"]) == 1
    assert listed["teams"][0]["name"] == "Recon"
    # No members yet.
    assert listed["teams"][0]["status"]["overall"] == "empty"

    got = api.get_team(tid)
    assert got["ok"] is True
    assert got["team"]["objective"] == "map surface"
    assert got["team"]["members"] == []

    renamed = api.rename_team(tid, "Recon Squad")
    assert renamed["ok"] is True
    assert api.get_team(tid)["team"]["name"] == "Recon Squad"

    deleted = api.delete_team(tid)
    assert deleted["ok"] is True
    assert deleted["removed"] is True
    assert not (tmp_path / "_teams" / f"{tid}.json").exists()
    assert f"team-{tid}" in api.dropped


def test_create_requires_name(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    assert api.create_team({"name": "", "objective": "x"})["ok"] is False


def test_create_with_preset_spawns_even_without_objective(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    # User picked a preset but left the objective blank — members must still spawn.
    res = api.create_team({"name": "逆向分流小队", "objective": "", "preset": "reverse_triage"})
    assert res["ok"] is True
    assert res.get("preset_spawned") == 2
    tid = res["team"]["team_id"]
    mgr = api._team_manager(tid)
    # The team name was used as the fallback mission.
    assert mgr.spawned_team["team"] == "逆向分流小队"
    assert mgr.spawned_team["objective"] == "逆向分流小队"
    assert mgr.spawned_team["start_immediately"] is False
    assert len(api.get_team(tid)["team"]["members"]) == 2
    assert all(item["status"] == "paused" for item in mgr.agents)


def test_add_member_tags_team_and_updates_status(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]

    res = api.add_team_member(tid, {"role": "recon", "objective": "enumerate"})
    assert res["ok"] is True

    mgr = api._team_manager(tid)
    assert mgr.spawned and mgr.spawned[0]["team"] == "Audit"
    assert mgr.spawned[0]["role"] == "recon"

    assert mgr.spawned[0]["start_immediately"] is False
    team = api.get_team(tid)["team"]
    assert team["status"]["member_count"] == 1
    assert team["status"]["overall"] == "idle"

    # ``get_team_member`` must unwrap ``res["agent"]`` to top-level fields.
    detail = api.get_team_member(tid, "agent-0")
    assert detail["ok"] is True
    assert detail["role"] == "recon"
    assert detail["status"] == "paused"
    assert isinstance(detail["history"], list) and detail["history"][0]["content"] == "ok"


def test_team_room_pass_is_not_shown_as_a_reply(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    mgr = api._team_manager(tid)
    mgr.agents[0]["history"] = [{
        "role": "assistant",
        "content": "这条跟我无关，我先不回。",
        "timestamp": 5,
        "tool_calls": [{"function": {"name": "team_room_pass", "arguments": "{}"}}],
    }]
    chat = api.get_team_chat(tid)
    blob = "\n".join(str(row.get("content") or "") for row in chat["messages"])
    assert "这条跟我无关" not in blob
    assert all("team_room_pass" not in (row.get("tool_names") or []) for row in chat["messages"])


def test_shutdown_cancel_notice_is_hidden_from_team_chat(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "dns-record-auditor", "objective": "map"})
    mgr = api._team_manager(tid)
    mgr.agents[0]["history"] = [
        {
            "role": "assistant",
            "content": "[已中止] 已取消当前助手运行。",
            "timestamp": 4,
        },
        {
            "role": "assistant",
            "content": "[已中止] 已找到两条 NS 记录",
            "timestamp": 5,
        },
    ]
    api.publish_team_member_output(
        f"team-{tid}",
        "agent-0",
        "dns-record-auditor",
        "[已中止] 已取消当前助手运行。",
        visibility="group",
    )
    chat = api.get_team_chat(tid)
    blob = "\n".join(str(row.get("content") or "") for row in chat["messages"])
    assert "已取消当前助手运行" not in blob
    assert "已找到两条 NS 记录" in blob


def test_add_member_requires_role_and_objective(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "T", "objective": ""})["team"]["team_id"]
    assert api.add_team_member(tid, {"role": "", "objective": "x"})["ok"] is False
    assert api.add_team_member(tid, {"role": "r", "objective": ""})["ok"] is False


def test_team_chat_merges_members_and_group_commands(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    mgr = api._team_manager(tid)
    mgr.agents[0]["history"] = [
        {"role": "assistant", "content": "recon done", "timestamp": 20},
    ]
    mgr.agents[1]["history"] = [
        {
            "role": "assistant",
            "content": "",
            "timestamp": 10,
            "tool_calls": [{"function": {"name": "http_probe"}}],
        },
        {"role": "assistant", "content": "verified", "timestamp": 30},
    ]

    sent = api.send_team_message(tid, "focus login", ["agent-0"])
    assert sent["ok"] is True
    assert sent["delivered"] == ["agent-0"]
    assert "focus login" in mgr.agents[0]["inputs"][0]
    assert "team_room_pass" in mgr.agents[0]["inputs"][0]

    chat = api.get_team_chat(tid)
    assert chat["ok"] is True
    assert [row["type"] for row in chat["messages"]] == [
        "activity", "message", "message", "message",
    ]
    assert chat["messages"][0]["tool_names"] == ["http_probe"]
    assert chat["messages"][-1]["content"] == "focus login"
    assert chat["messages"][-1]["target_roles"] == ["recon"]


def test_team_group_message_broadcasts_to_all_members(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})

    sent = api.send_team_message(tid, "status update")
    assert sent["ok"] is True
    assert sent["delivered_count"] == 2
    mgr = api._team_manager(tid)
    assert all("status update" in item["inputs"][0] for item in mgr.agents)


def test_team_message_delivers_attachments(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    attachment = {
        "name": "scope.txt",
        "type": "file",
        "mime": "text/plain",
        "size": 5,
        "data_base64": "aGVsbG8=",
    }
    sent = api.send_team_message(tid, "", attachments=[attachment])
    assert sent["ok"] is True
    member = api._team_manager(tid).agents[0]
    assert member["attachments"][0]["name"] == "scope.txt"
    chat = api.get_team_chat(tid)
    leader = next(row for row in chat["messages"] if row.get("sender") == "leader")
    assert leader["attachments"][0]["name"] == "scope.txt"
    assert "data_base64" not in leader["attachments"][0]


def test_member_output_persists_for_all_and_mention_wakes_peer(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    api.add_team_member(tid, {"role": "reporter", "objective": "report"})
    mgr = api._team_manager(tid)

    api.publish_team_member_output(
        f"team-{tid}",
        "agent-0",
        "recon",
        "Found login flow. @analyst please verify.",
    )

    # Explicit @mention wakes analyst with typed Team Room context.
    assert len(mgr.agents[1]["inputs"]) == 1
    assert "@recon" in mgr.agents[1]["inputs"][0]
    assert "Found login flow" in mgr.agents[1]["inputs"][0]
    # Unmentioned reporter is not auto-woken (prevents autonomous loops) but
    # sees the message as unread context on its next turn.
    assert "inputs" not in mgr.agents[2]
    unread = api.team_member_room_context(f"team-{tid}", "agent-2")
    assert "@recon" in unread
    assert "Found login flow" in unread
    assert api.team_member_room_context(f"team-{tid}", "agent-2") == ""


def test_member_output_without_mention_does_not_wake_peers(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    mgr = api._team_manager(tid)

    api.publish_team_member_output(f"team-{tid}", "agent-0", "recon", "Passive update only.")
    assert "inputs" not in mgr.agents[1]
    assert "Passive update only" in api.team_member_room_context(f"team-{tid}", "agent-1")


def test_private_chat_is_isolated_from_other_members(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    mgr = api._team_manager(tid)

    sent = api.send_team_message(
        tid,
        "private instruction",
        ["agent-0"],
        visibility="private",
    )
    assert sent["ok"] is True
    assert "private instruction" in mgr.agents[0]["inputs"][0]
    # Another member cannot consume the private leader message.
    assert "private instruction" not in api.team_member_room_context(f"team-{tid}", "agent-1")

    api.publish_team_member_output(
        f"team-{tid}",
        "agent-0",
        "recon",
        "private result @analyst",
        visibility="private",
    )
    # A mention inside a private reply must not wake/leak to analyst.
    assert "inputs" not in mgr.agents[1]
    chat = api.get_team_chat(tid)
    private_rows = [row for row in chat["messages"] if row.get("visibility") == "private"]
    assert len(private_rows) == 2
    assert all(row.get("participant_agent_ids") == ["agent-0"] for row in private_rows)
    assert "private instruction" not in {
        row.get("content") for row in chat["messages"] if row.get("visibility") != "private"
    }


def test_later_private_send_does_not_retag_group_reply(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.send_team_message(tid, "work in the room", ["agent-0"])
    api.send_team_message(tid, "just between us", ["agent-0"], visibility="private")
    # The group turn was already running; its reply keeps the group tag.
    api.publish_team_member_output(
        f"team-{tid}", "agent-0", "recon", "group findings", visibility="group",
    )
    api.publish_team_member_output(
        f"team-{tid}", "agent-0", "recon", "private findings", visibility="private",
    )
    mgr = api._team_manager(tid)
    mgr.agents[0]["history"] = [
        {"role": "user", "content": "GROUP ONLY leaked into private", "ui_hidden": True, "timestamp": 1},
        {"role": "assistant", "content": "group findings", "timestamp": 2},
        {"role": "assistant", "content": "private findings", "timestamp": 3},
    ]
    chat = api.get_team_chat(tid)
    by_text = {row.get("content"): row for row in chat["messages"]}
    assert by_text["group findings"]["visibility"] == "group"
    assert by_text["private findings"]["visibility"] == "private"
    assert by_text["work in the room"]["visibility"] == "group"
    assert by_text["just between us"]["visibility"] == "private"
    private_text = {
        row.get("content") for row in chat["messages"] if row.get("visibility") == "private"
    }
    assert "group findings" not in private_text
    assert "work in the room" not in private_text
    assert "GROUP ONLY leaked into private" not in {row.get("content") for row in chat["messages"]}


def test_stop_team_members_halts_live_replies_only(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    mgr = api._team_manager(tid)
    mgr.agents[0]["status"] = "running"
    mgr.agents[0]["pending_inputs"] = ["keep going"]
    mgr.agents[1]["status"] = "completed"
    result = api.stop_team_members(tid)
    assert result["ok"] is True
    assert result["stopped"] == ["agent-0"]
    assert mgr.agents[0]["status"] == "paused"
    assert mgr.agents[0]["pending_inputs"] == []
    assert mgr.agents[1]["status"] == "completed"
    assert api.get_team(tid)["team"]["members"]


def test_team_greenlight_matches_session_authorization(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    turned_on = api.set_team_greenlight(tid, True)
    assert turned_on["ok"] is True
    assert turned_on["greenlight"] is True
    assert api._team_manager(tid).agents[0]["greenlight"] is True
    assert api.get_team(tid)["team"]["greenlight"] is True
    # Tool confirmation policy is a different switch and stays untouched.
    assert api._team_manager(tid).agents[0].get("approval_mode", "leader_auto") == "leader_auto"
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    assert api._team_manager(tid).agents[1]["greenlight"] is True
    turned_off = api.set_team_greenlight(tid, False)
    assert turned_off["greenlight"] is False
    assert api.get_team(tid)["team"]["greenlight"] is False


def test_team_member_can_be_edited_with_per_member_model(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    result = api.update_team_member(tid, "agent-0", {
        "role": "recon-lead",
        "objective": "map auth",
        "permission": "readonly",
        "requested_tools": [],
        "model": "fast-model",
    })
    assert result["ok"] is True
    member = api.get_team(tid)["team"]["members"][0]
    assert member["role"] == "recon-lead"
    assert member["objective"] == "map auth"
    assert member["model"] == "fast-model"


def test_status_summary_variants() -> None:
    f = TeamsMixin._team_status_summary
    assert f([])["overall"] == "empty"
    assert f([{"status": "running"}])["overall"] == "running"
    assert f([{"status": "completed"}])["overall"] == "idle"
    assert f([{"status": "failed"}])["overall"] == "failed"
    attn = f([{"status": "paused", "pending_confirmation": {"pending_tools": [{"name": "x"}]}}])
    assert attn["overall"] == "attention"
    assert attn["attention"] == 1


def test_get_unknown_team(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    assert api.get_team("deadbeef")["ok"] is False


def test_invalid_team_id_rejected(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    # Path traversal / bad chars must not resolve to a team file.
    assert api.get_team("../etc")["ok"] is False
