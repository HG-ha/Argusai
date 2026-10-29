"""Standing-team room inside the ops REPL."""

from __future__ import annotations

from typing import Any

from argusai.cli.commands.chat import (
    _run_team_turn,
    _team_monitor_jobs,
    _team_monitor_tokens,
    _try_shell,
)
from argusai.cli.locale import set_locale
from argusai.cli.shell import dispatch


class _TeamApi:
    def __init__(self) -> None:
        self._locale = "zh"
        self.stopped: list[str] = []
        self.sent: list[tuple[Any, ...]] = []
        self._chat_calls = 0

    def list_teams(self) -> dict[str, Any]:
        return {
            "ok": True,
            "teams": [{
                "team_id": "abc",
                "name": "red",
                "status": {"overall": "idle", "member_count": 1, "active": 0},
            }],
        }

    def get_team(self, team_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "team": {
                "team_id": team_id,
                "name": "red",
                "status": {"overall": "idle", "member_count": 1, "active": 0},
                "members": [{"agent_id": "a1", "role": "recon"}],
            },
        }

    def get_team_chat(self, _team_id: str) -> dict[str, Any]:
        self._chat_calls += 1
        if self._chat_calls == 1:
            return {"ok": True, "messages": []}
        return {
            "ok": True,
            "messages": [
                {
                    "id": "user-1",
                    "role": "user",
                    "sender": "leader",
                    "content": "看登录口",
                    "visibility": "group",
                },
                {
                    "id": "m1",
                    "role": "assistant",
                    "role_name": "recon",
                    "content": "登录口在 /login",
                    "visibility": "group",
                },
            ],
        }

    def send_team_message(self, *args: Any) -> dict[str, Any]:
        self.sent.append(args)
        return {"ok": True, "delivered": ["a1"]}

    def cancel_assistant_run(self, _session_id: str) -> dict[str, Any]:
        return {"ok": True}

    def set_team_greenlight(self, team_id: str, enabled: bool = False) -> dict[str, Any]:
        self.greenlight = (team_id, bool(enabled))
        return {"ok": True, "greenlight": bool(enabled)}

    def get_team_token_usage(self, team_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "team_id": team_id,
            "total": {"input_tokens": 30, "output_tokens": 12, "calls": 2},
            "cache_hit_rate": 10.0,
            "members": [
                {
                    "agent_id": "a1",
                    "role": "recon",
                    "usage": {"input_tokens": 30, "output_tokens": 12, "calls": 2},
                    "cache_hit_rate": 10.0,
                },
            ],
        }

    def stop_team_members(self, team_id: str) -> dict[str, Any]:
        self.stopped.append(team_id)
        return {"ok": True, "stopped_count": 1}


def test_team_room_enter_leave_and_stop() -> None:
    set_locale("zh")
    api = _TeamApi()
    listed = dispatch(api, "cli-default", "/team")
    assert listed.handled
    assert any("red" in line and "abc" in line for line in listed.lines)

    room = {"id": "", "name": "", "member_id": "", "member_role": ""}
    by_number = dispatch(api, "cli-default", "/team use 1")
    assert by_number.team_id == "abc"
    assert by_number.team_name == "red"

    entered = _try_shell(
        api, ["cli-default"], "/team use red", version="0", json_out=False, team_room=room
    )
    assert entered is True
    assert room["id"] == "abc"
    assert room["name"] == "red"
    assert room["member_id"] == ""

    private = dispatch(api, "cli-default", "/team pm red recon")
    assert private.team_member_id == "a1"
    assert private.team_member_role == "recon"

    left = _try_shell(
        api, ["cli-default"], "/team leave", version="0", json_out=False, team_room=room
    )
    assert left is True
    assert room["id"] == ""

    bare = _try_shell(
        api, ["cli-default"], "/team stop", version="0", json_out=False, team_room=room
    )
    assert bare is True
    assert api.stopped == []

    room["id"] = "abc"
    stopped = _try_shell(
        api, ["cli-default"], "/team stop", version="0", json_out=False, team_room=room
    )
    assert stopped is True
    assert api.stopped == ["abc"]

    cancelled = _try_shell(
        api, ["cli-default"], "/cancel", version="0", json_out=False, team_room=room
    )
    assert cancelled is True
    assert api.stopped == ["abc", "abc"]

    authorized = _try_shell(
        api, ["cli-default"], "/greenlight on", version="0", json_out=False, team_room=room
    )
    assert authorized is True
    assert api.greenlight == ("abc", True)

    outside = {"id": "", "name": "", "member_id": "", "member_role": ""}
    missing = _try_shell(
        api, ["cli-default"], "/team greenlight on", version="0", json_out=False, team_room=outside
    )
    assert missing is True
    assert api.greenlight == ("abc", True)


def test_monitor_follows_the_open_team_room() -> None:
    api = _TeamApi()
    room = {"id": "abc", "name": "red", "member_id": "", "member_role": ""}
    jobs = _team_monitor_jobs(api, room)
    assert jobs == [{"job_id": "a1", "label": "recon", "state": "idle"}]
    tokens = _team_monitor_tokens(api, room)
    assert tokens["token_usage"]["input_tokens"] == 30

    room["member_id"] = "a1"
    room["member_role"] = "recon"
    private_jobs = _team_monitor_jobs(api, room)
    assert private_jobs == [{"job_id": "a1", "label": "recon", "state": "idle"}]
    private_tokens = _team_monitor_tokens(api, room)
    assert private_tokens["token_usage"]["calls"] == 2


def test_plain_line_is_not_swallowed_and_team_turn_prints_replies() -> None:
    set_locale("zh")
    api = _TeamApi()
    assert dispatch(api, "cli-default", "看登录口").handled is False

    class _Ops:
        sidebar_mode = "off"

        def __init__(self) -> None:
            self.panels: list[list[str]] = []
            self.markup: list[str] = []
            self.speakers: list[tuple[str, str]] = []

        def append_panel(self, lines: list[str]) -> None:
            self.panels.append(list(lines))

        def append_markup(self, line: str) -> None:
            self.markup.append(line)

        def append_speaker(self, name: str, body: str) -> None:
            self.speakers.append((name, body))

        def set_rail_lines(self, lines: list[str] | None) -> None:
            return None

        def set_sidebar_mode(self, mode: str) -> str:
            self.sidebar_mode = mode
            return mode

        def set_status(self, text: str) -> None:
            self.status = text

    ops = _Ops()
    _run_team_turn(
        api,
        {"id": "abc", "name": "red", "member_id": "", "member_role": ""},
        "看登录口",
        ops=ops,
        json_out=False,
        poll_seconds=0,
        quiet_seconds=0,
    )
    assert api.sent
    assert api.sent[0][0] == "abc"
    assert api.sent[0][1] == "看登录口"
    assert api.sent[0][3] == "group"
    assert ops.speakers == [("recon", "登录口在 /login")]
    assert ops.panels == []
    assert ops.markup == []
    assert ops.status == ""
