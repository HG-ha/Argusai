"""``argusai team`` against a fake host API."""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

from argusai.cli.commands import team as team_cmd
from argusai.cli.main import _register_commands, app

_register_commands()
runner = CliRunner()


class _Api:
    def __init__(self) -> None:
        self.team = {
            "team_id": "abcd1234abcd1234",
            "name": "Audit",
            "objective": "map",
            "status": {"overall": "idle", "member_count": 1, "active": 0},
            "members": [{
                "agent_id": "agent-0",
                "role": "recon",
                "status": "paused",
                "objective": "map",
                "summary": "",
            }],
        }
        self.sent: list[Any] = []
        self.stopped = 0

    def shutdown_background_services(self) -> None:
        return None

    def list_teams(self) -> dict[str, Any]:
        return {"ok": True, "teams": [self.team]}

    def get_team(self, team_id: str) -> dict[str, Any]:
        return {"ok": True, "team": self.team}

    def get_team_chat(self, team_id: str) -> dict[str, Any]:
        return {"ok": True, "messages": [
            {"role": "user", "sender": "leader", "content": "look at login", "visibility": "group"},
            {
                "role": "assistant",
                "role_name": "recon",
                "agent_id": "agent-0",
                "content": "private note",
                "visibility": "private",
                "participant_agent_ids": ["agent-0"],
            },
        ]}

    def send_team_message(self, team_id, message, agent_ids, visibility, attachments=None, options=None):
        self.sent.append((message, list(agent_ids or []), visibility))
        return {"ok": True, "delivered": ["agent-0"]}

    def stop_team_members(self, team_id: str) -> dict[str, Any]:
        self.stopped += 1
        return {"ok": True, "stopped": ["agent-0"], "stopped_count": 1}

    def set_team_greenlight(self, team_id: str, enabled: bool = False) -> dict[str, Any]:
        self.team["greenlight"] = bool(enabled)
        return {"ok": True, "greenlight": bool(enabled), "team_id": team_id}


def test_team_chat_hides_private_messages_from_the_group(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(team_cmd, "headless_api", lambda: api)
    result = runner.invoke(app, ["team", "chat", "Audit"])
    assert result.exit_code == 0
    assert "look at login" in result.output
    assert "private note" not in result.output

    private = runner.invoke(app, ["team", "chat", "Audit", "--member", "recon"])
    assert private.exit_code == 0
    assert "private note" in private.output
    assert "look at login" not in private.output


def test_team_send_private_targets_one_member(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(team_cmd, "headless_api", lambda: api)
    result = runner.invoke(app, ["team", "send", "Audit", "check dns", "--member", "recon", "--private"])
    assert result.exit_code == 0, result.output
    assert api.sent == [("check dns", ["agent-0"], "private")]


def test_team_stop(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(team_cmd, "headless_api", lambda: api)
    result = runner.invoke(app, ["team", "stop", "abcd1234abcd1234"])
    assert result.exit_code == 0
    assert api.stopped == 1


def test_team_show_by_list_number(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(team_cmd, "headless_api", lambda: api)
    found = runner.invoke(app, ["team", "show", "1"])
    assert found.exit_code == 0, found.output
    assert "Audit" in found.output
    missing = runner.invoke(app, ["team", "show", "2"])
    assert missing.exit_code != 0
    assert "out of range" in missing.output


def test_load_team_number_follows_list_order() -> None:
    second = {
        "team_id": "bbbb2222bbbb2222",
        "name": "Second",
        "status": {"overall": "idle", "member_count": 0, "active": 0},
        "members": [],
    }
    first = {
        "team_id": "aaaa1111aaaa1111",
        "name": "First",
        "status": {"overall": "idle", "member_count": 0, "active": 0},
        "members": [],
    }

    class _Two:
        def list_teams(self) -> dict[str, Any]:
            return {"ok": True, "teams": [first, second]}

        def get_team(self, team_id: str) -> dict[str, Any]:
            row = first if team_id == first["team_id"] else second
            return {"ok": True, "team": row}

    assert team_cmd._load_team(_Two(), "1")["name"] == "First"
    assert team_cmd._load_team(_Two(), "2")["name"] == "Second"


def test_team_greenlight_on(monkeypatch) -> None:
    api = _Api()
    monkeypatch.setattr(team_cmd, "headless_api", lambda: api)
    result = runner.invoke(app, ["team", "greenlight", "on", "Audit"])
    assert result.exit_code == 0, result.output
    assert api.team["greenlight"] is True
