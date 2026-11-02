"""Tests for the standing Agent Teams backend (gui.teams.TeamsMixin).

Most tests stub the child-Agent manager. Peer routing regressions also use
the real manager with deterministic chat callbacks and no model/network I/O.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from argusai.core.assistant.agent_threads import AgentThreadManager, set_team_room_hooks
from argusai.core.assistant.messages import ChatResponse
from argusai.core.assistant.session import AssistantSession
from argusai.core.assistant.tools import ToolRegistry
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

    def send_input(
        self, agent_id, message, *, resume=False, attachments=None,
        room_visibility=None, interrupt=True, input_ref="", coalesce_key="",
    ):
        for a in self.agents:
            if a["agent_id"] == agent_id:
                if input_ref:
                    refs = a.setdefault("input_refs", set())
                    if input_ref in refs:
                        return {"ok": True, "agent": a, "duplicate": True}
                    refs.add(input_ref)
                a.setdefault("inputs", []).append(message)
                a.setdefault("input_options", []).append({
                    "interrupt": interrupt, "input_ref": input_ref, "coalesce_key": coalesce_key,
                })
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


def _live_team(tmp_path, monkeypatch, chat):
    """Real queue/lifecycle, a fake provider, and temporary room persistence."""
    class _Provider:
        async def close(self):
            return None

    api = _TeamApi(tmp_path)
    settings = SimpleNamespace(
        sessions_dir=str(tmp_path), ai_enabled=True,
        ai_endpoints=[SimpleNamespace(enabled=True, name="mock", model="mock-model")],
    )
    api.settings_mgr = SimpleNamespace(get=lambda: settings)
    tid = api.create_team({"name": "Peer routing", "objective": "inspect mock material"})["team"]["team_id"]
    registry = ToolRegistry(api=api, session_id=f"team-{tid}")
    registry.register("write_probe", "Mock controlled write", {}, lambda: {"ok": True}, risk="medium")
    manager = AgentThreadManager(AssistantSession(registry=registry, ai_provider=object()))
    api._managers[tid] = manager
    monkeypatch.setattr(
        "argusai.core.assistant.agent_threads._create_isolated_ai_provider",
        lambda _parent, model="": _Provider(),
    )
    analyst = api.add_team_member(tid, {
        "role": "analyst", "objective": "verify mock evidence", "permission": "scoped",
        "requested_tools": ["write_probe"],
    })["agent"]["agent_id"]
    recon = api.add_team_member(tid, {"role": "recon", "objective": "collect mock evidence"})["agent"]["agent_id"]

    async def routed_chat(self, message="", **kwargs):
        if self.registry.session_id != analyst:
            self.team_room_pass("No relevant evidence for this member.")
            return ChatResponse(text="", status="done")
        return await chat(self, message, **kwargs)

    monkeypatch.setattr(AssistantSession, "chat", routed_chat)
    set_team_room_hooks(
        context_provider=api.team_member_room_context,
        output_publisher=api.publish_team_member_output,
    )
    return api, tid, manager, analyst, recon


def _direct_input_text(message):
    """Separate the queued work item from the room's shared background context."""
    return message.rsplit("[Direct instruction for you]\n", 1)[-1]


@pytest.mark.parametrize("consume_context_before_route", [False, True])
def test_busy_peer_mentions_are_not_interjections_or_lost_by_context_cursor(
    tmp_path, monkeypatch, consume_context_before_route,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    children = []
    consumed = []

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            children.append(self)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock work finished", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    append_message = api._append_team_message

    def append_then_consume(*args, **kwargs):
        entry = append_message(*args, **kwargs)
        if kwargs.get("sender_agent_id") == recon:
            # A normal context read can win the race between the persisted
            # peer output and its delivery route. Its cursor is not an ack.
            consumed.append(api.team_member_room_context(f"team-{tid}", analyst))
        return entry

    if consume_context_before_route:
        monkeypatch.setattr(api, "_append_team_message", append_then_consume)
    try:
        assert api.send_team_message(tid, "finish current mock task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        api.publish_team_member_output(
            f"team-{tid}", recon, "recon", "@analyst verify peer-evidence-one after your current task",
        )
        assert not children[0]._pause_after_current_response, "peer routing must not interrupt the current task"
        queued = manager.get(analyst)["agent"]
        assert queued["pending_input_count"] == 1, "a consumed room cursor must not suppress peer delivery"
        if consume_context_before_route:
            assert "peer-evidence-one" in consumed[0]
        assert len(calls) == 1
        release.set()
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert len(calls) == 2
        assert "peer-evidence-one" in calls[1]
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


@pytest.mark.parametrize("body_length", [24_000, 31_000])
def test_long_peer_message_preserves_tail_request_in_real_queue_and_followup(
    tmp_path, monkeypatch, body_length,
) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    request = "@analyst 请核验尾部具体请求 peer-tail-request-unique，完成后交付证据引用。"
    body = "x" * (body_length - len(request) - 1) + "\n" + request

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock evidence reviewed", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    try:
        assert api.send_team_message(tid, "finish current mock task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        api.publish_team_member_output(f"team-{tid}", recon, "recon", body)
        entry = api._team_meta(tid)["messages"][-1]
        assert entry["sender_agent_id"] == recon
        assert len(entry["content"]) == 24_000
        with manager._lock:
            pending = list(manager._records[analyst]["pending_inputs"])
        assert len(pending) == 1
        queued_text = pending[0]["text"]
        assert request in queued_text, "the actual peer request must survive storage and envelope limits"
        if body_length == 24_000:
            assert body in queued_text
        else:
            assert request not in entry["content"]
            assert any(marker in queued_text.lower() for marker in ("truncated", "omitted", "截断", "省略"))
        release.set()
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert len(calls) == 2
        assert request in calls[1]
        if body_length == 24_000:
            assert body in calls[1]
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


def test_multiple_peer_messages_and_repeated_routes_deliver_each_entry_once(tmp_path, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    children = []

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            children.append(self)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock task finished", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    entries = []
    try:
        assert api.send_team_message(tid, "finish current mock task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        for token in ("peer-evidence-alpha", "peer-evidence-beta", "peer-evidence-alpha"):
            api.publish_team_member_output(f"team-{tid}", recon, "recon", f"@analyst review {token}")
            entry = api._team_meta(tid)["messages"][-1]
            entries.append(entry)
            # Replaying delivery for the same persisted entry is not another
            # message. Identical text in a different entry still is one.
            api._route_team_room_message(tid, entry, manager, [analyst])
            api._route_team_room_message(tid, entry, manager, [analyst])
        assert not children[0]._pause_after_current_response
        assert manager.get(analyst)["agent"]["pending_input_count"] == 3
        assert len(calls) == 1
        assert len({entry["id"] for entry in entries}) == 3
        release.set()
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert len(calls) == 4
        delivered = "\n".join(_direct_input_text(message) for message in calls[1:])
        assert delivered.count("peer-evidence-alpha") == 2
        assert delivered.count("peer-evidence-beta") == 1
        api._route_team_room_message(tid, entries[0], manager, [analyst])
        assert manager.get(analyst)["agent"]["status"] == "completed"
        assert len(calls) == 4
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


def test_peer_mention_preserves_manual_pause_until_explicit_resume(tmp_path, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock checkpoint", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    try:
        assert api.send_team_message(tid, "start controlled task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        assert manager.pause(analyst)["ok"] is True
        release.set()
        assert manager.wait([analyst], timeout_seconds=2)["agents"][0]["status"] == "paused"
        with manager._condition:
            assert manager._condition.wait_for(lambda: analyst not in manager._runtimes, timeout=2)
        api.publish_team_member_output(f"team-{tid}", recon, "recon", "@analyst review peer-evidence-paused")
        paused = manager.get(analyst)["agent"]
        assert paused["status"] == "paused"
        assert paused["pending_input_count"] == 1
        assert len(calls) == 1
        assert api.resume_team_member(tid, analyst, "explicitly resume controlled work")["ok"] is True
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        peer_turns = [
            _direct_input_text(message) for message in calls[1:]
            if "[Team Room · explicit peer @mention]" in _direct_input_text(message)
        ]
        assert len(peer_turns) == 1
        assert peer_turns[0].count("peer-evidence-paused") == 1
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


def test_peer_mention_waiting_for_confirmation_is_queued_and_delivered_after_approval(tmp_path, monkeypatch) -> None:
    calls = []

    async def chat(self, message="", *, confirmation_action=False, **_kwargs):
        if confirmation_action:
            calls.append("confirmation")
            self._pending = None
            return ChatResponse(text="controlled action approved", status="done")
        if not calls:
            calls.append("initial")
            self._pending = {
                "confirmations": [{
                    "name": "write_probe", "arguments": {},
                    "tool_call": {"id": "call-controlled-write"},
                }],
                "tool_calls": [], "messages": [], "assistant_response": {},
                "decisions": {}, "auto_allow_names": [], "auto_allow_permissions": [],
            }
            return ChatResponse(text="approval required", status="awaiting_confirmation")
        calls.append(message)
        return ChatResponse(text="peer evidence reviewed", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    try:
        assert api.send_team_message(tid, "start controlled task", [analyst], visibility="private")["ok"]
        paused = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        pending = paused["pending_confirmation"]["pending_tools"][0]
        api.publish_team_member_output(f"team-{tid}", recon, "recon", "@analyst review peer-evidence-confirmation")
        waiting = manager.get(analyst)["agent"]
        assert waiting["status"] == "paused"
        assert waiting["pending_confirmation"]["pending_tools"][0]["pending_id"] == pending["pending_id"]
        assert waiting["pending_input_count"] == 1
        assert calls == ["initial"]
        assert manager.confirm(analyst, {
            "type": "confirmation_decision", "pending_id": pending["pending_id"],
            "version": pending["version"], "call_id": pending["id"], "verdict": "allow",
        })["ok"] is True
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert len(calls) == 3 and calls[1] == "confirmation"
        assert _direct_input_text(calls[2]).count("peer-evidence-confirmation") == 1
    finally:
        set_team_room_hooks()
        manager.shutdown()


@pytest.mark.parametrize("automatic", [False, True])
def test_private_confirmation_continuation_never_broadcasts_to_other_members(tmp_path, monkeypatch, automatic) -> None:
    secret = "private-after-approval-secret @recon do-not-deliver"
    published = []

    async def chat(self, message="", *, confirmation_action=False, **_kwargs):
        if confirmation_action:
            self._pending = None
            return ChatResponse(text=secret, status="done")
        self._pending = {
            "confirmations": [{
                "name": "write_probe", "arguments": {}, "tool_call": {"id": "private-controlled-write"},
            }],
            "tool_calls": [], "messages": [], "assistant_response": {},
            "decisions": {}, "auto_allow_names": [], "auto_allow_permissions": [],
        }
        return ChatResponse(text="private approval required", status="awaiting_confirmation")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    gui_publish = api.publish_team_member_output

    def capture_and_publish(parent_id, agent_id, role, text, visibility=None):
        published.append((text, visibility))
        gui_publish(parent_id, agent_id, role, text, visibility)

    set_team_room_hooks(context_provider=api.team_member_room_context, output_publisher=capture_and_publish)
    try:
        assert api.send_team_message(tid, "private controlled request", [analyst], visibility="private")["ok"]
        pending = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert pending["status"] == "paused"
        if automatic:
            manager.parent.set_auto_approve_tool("write_probe", True)
            assert analyst in manager.sync_auto_approve_from_parent()["auto_resolved"]
        else:
            tool = pending["pending_confirmation"]["pending_tools"][0]
            assert api.confirm_team_member_action(tid, analyst, {
                "type": "confirmation_decision", "pending_id": tool["pending_id"],
                "version": tool["version"], "call_id": tool["id"], "verdict": "allow",
            })["ok"] is True
        result = manager.wait([analyst, recon], timeout_seconds=2)
        assert result["completed"] is True
        assert published == [("private approval required", "private"), (secret, "private")]
        messages = api._team_meta(tid)["messages"]
        final = next(item for item in messages if item["content"] == secret)
        assert final["visibility"] == "private"
        assert final["participant_agent_ids"] == [analyst]
        assert all(item["visibility"] == "private" for item in messages)
        assert secret not in api.team_member_room_context(f"team-{tid}", recon)
        other = manager.get(recon)["agent"]
        assert not any(event.get("type") in {"input_queued", "input_accepted"} for event in other["events"])
    finally:
        set_team_room_hooks()
        manager.shutdown()


def test_actual_room_context_excludes_current_batch_and_stops_before_future_request(tmp_path, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    contexts = []

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock task finished", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    actual_provider = api.team_member_room_context

    def capture_context(parent_id, agent_id, *, input_refs=None):
        # Capture this provider call's cursor atomically; another member may
        # publish or read the shared room as soon as the call releases its lock.
        with api._team_room_lock:
            context = actual_provider(parent_id, agent_id, input_refs=input_refs)
            if agent_id == analyst and input_refs:
                contexts.append({
                    "refs": set(input_refs), "context": context,
                    "cursor": api._team_meta(tid).get("member_cursors", {}).get(analyst, 0),
                })
        return context

    set_team_room_hooks(context_provider=capture_context, output_publisher=api.publish_team_member_output)
    try:
        assert api.send_team_message(tid, "finish current mock task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        api._append_team_message(
            tid, content="earlier-unread-leader-guidance", target_agent_ids=[], target_roles=[],
        )
        batch = []
        for number in (1, 2):
            api.publish_team_member_output(f"team-{tid}", recon, "recon", f"current-batch-body-{number}")
            batch.append(api._team_meta(tid)["messages"][-1])
        api.publish_team_member_output(f"team-{tid}", recon, "recon", "@analyst future-independent-request")
        future = api._team_meta(tid)["messages"][-1]
        assert manager.get(analyst)["agent"]["pending_input_count"] == 2
        release.set()
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert len(calls) == 3
        current = next(item for item in contexts if item["refs"] == {entry["id"] for entry in batch})
        assert "earlier-unread-leader-guidance" in current["context"]
        assert "current-batch-body" not in current["context"]
        assert "future-independent-request" not in current["context"]
        assert current["cursor"] == max(entry["sequence"] for entry in batch)
        assert current["cursor"] < future["sequence"]
        assert "earlier-unread-leader-guidance" in calls[1]
        for number in (1, 2):
            assert calls[1].count(f"current-batch-body-{number}") == 1
        assert "future-independent-request" not in calls[1]
        later = next(item for item in contexts if item["refs"] == {future["id"]})
        assert later["cursor"] == future["sequence"]
        assert "future-independent-request" not in later["context"]
        assert calls[2].count("future-independent-request") == 1
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


def test_busy_ordinary_group_messages_batch_without_reordering_explicit_requests(tmp_path, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    children = []

    async def chat(self, message="", **_kwargs):
        calls.append(message)
        if len(calls) == 1:
            children.append(self)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
        return ChatResponse(text="mock task complete", status="done")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, chat)
    try:
        assert api.send_team_message(tid, "finish current mock task", [analyst], visibility="private")["ok"]
        assert started.wait(timeout=2)
        for number in (1, 2, 3):
            api.publish_team_member_output(f"team-{tid}", recon, "recon", f"ordinary-update-{number}")
            entry = api._team_meta(tid)["messages"][-1]
            assert entry["target_agent_ids"] == []
            api._route_team_room_message(tid, entry, manager, [analyst])
        assert manager.get(analyst)["agent"]["pending_input_count"] == 1
        assert not children[0]._pause_after_current_response
        assert len(calls) == 1
        api.publish_team_member_output(f"team-{tid}", recon, "recon", "@analyst explicit-middle-request")
        for number in (4, 5):
            api.publish_team_member_output(f"team-{tid}", recon, "recon", f"ordinary-update-{number}")
        assert manager.get(analyst)["agent"]["pending_input_count"] == 3
        release.set()
        result = manager.wait([analyst], timeout_seconds=2)["agents"][0]
        assert result["status"] == "completed"
        assert result["pending_input_count"] == 0
        assert len(calls) == 4
        direct_calls = [_direct_input_text(message) for message in calls]
        for number in (1, 2, 3):
            assert direct_calls[1].count(f"ordinary-update-{number}") == 1
        assert "explicit-middle-request" not in direct_calls[1]
        assert direct_calls[2].count("explicit-middle-request") == 1
        for number in (4, 5):
            assert direct_calls[3].count(f"ordinary-update-{number}") == 1
        assert "ordinary-update-4" not in direct_calls[1]
        assert "ordinary-update-1" not in direct_calls[3]
    finally:
        release.set()
        set_team_room_hooks()
        manager.shutdown()


def test_unrelated_group_recipients_can_pass_without_starting_reply_broadcasts(tmp_path, monkeypatch) -> None:
    passed = []

    async def unused_chat(self, message="", **_kwargs):
        raise AssertionError("a pass-only room should use the model's silent choice")

    api, tid, manager, analyst, recon = _live_team(tmp_path, monkeypatch, unused_chat)
    reporter = api.add_team_member(tid, {"role": "reporter", "objective": "report verified evidence"})["agent"]["agent_id"]

    async def pass_chat(self, message="", **_kwargs):
        passed.append((self.registry.session_id, message))
        self.team_room_pass("No new evidence or concrete handoff for this member.")
        # Even stray text after the terminal silent choice must not create a
        # new room message and trigger another all-member broadcast.
        return ChatResponse(text="must not be posted", status="done")

    monkeypatch.setattr(AssistantSession, "chat", pass_chat)
    try:
        api.publish_team_member_output(f"team-{tid}", recon, "recon", "ordinary-room-context-for-all")
        result = manager.wait([analyst, reporter], timeout_seconds=2)
        assert result["completed"] is True
        assert {item[0] for item in passed} == {analyst, reporter}
        assert len(passed) == 2
        assert all("ordinary-room-context-for-all" in item[1] for item in passed)
        messages = api._team_meta(tid)["messages"]
        assert len(messages) == 1
        assert messages[0]["sender_agent_id"] == recon
        assert messages[0]["content"] == "ordinary-room-context-for-all"
        assert manager.active_agent_count() == 0
    finally:
        set_team_room_hooks()
        manager.shutdown()


def test_team_room_transactions_share_one_reentrant_lock(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    first = api._team_room_lock
    assert api._team_room_lock is first
    with first:
        with api._team_room_lock:
            assert api._team_room_lock is first


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


def test_failed_delivery_returns_persisted_room_receipt(tmp_path, monkeypatch) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    monkeypatch.setattr(
        api._team_manager(tid),
        "send_input",
        lambda *args, **kwargs: {"ok": False, "error": "confirmation required"},
    )

    result = api.send_team_message(tid, "review this request", ["agent-0"])

    assert result["ok"] is False
    assert result["delivered_count"] == 0
    assert result["errors"][0]["error"] == "confirmation required"
    receipt = result["message"]
    assert receipt["id"]
    messages = api.get_team_chat(tid)["messages"]
    assert [row["id"] for row in messages if row.get("content") == "review this request"] == [receipt["id"]]


def test_member_output_persists_and_routes_to_explicit_and_ordinary_peers(tmp_path) -> None:
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
    # Ordinary recipients receive the same group message and let the model
    # choose whether they have useful evidence or should stay silent.
    assert len(mgr.agents[2]["inputs"]) == 1
    assert "Found login flow" in mgr.agents[2]["inputs"][0]
    assert mgr.agents[1]["input_options"][0]["coalesce_key"] == ""
    assert mgr.agents[2]["input_options"][0]["coalesce_key"] == "team-room"
    assert mgr.agents[2]["input_options"][0]["interrupt"] is False
    assert "inputs" not in mgr.agents[0]
    assert "Found login flow" in api.team_member_room_context(f"team-{tid}", "agent-2")
    assert api.team_member_room_context(f"team-{tid}", "agent-2") == ""


def test_member_output_without_mention_is_delivered_for_the_model_to_choose(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    mgr = api._team_manager(tid)

    api.publish_team_member_output(f"team-{tid}", "agent-0", "recon", "Passive update only.")
    assert len(mgr.agents[1]["inputs"]) == 1
    assert "Passive update only" in mgr.agents[1]["inputs"][0]
    assert mgr.agents[1]["input_options"][0]["interrupt"] is False
    assert mgr.agents[1]["input_options"][0]["coalesce_key"] == "team-room"
    assert "inputs" not in mgr.agents[0]
    assert "Passive update only" in api.team_member_room_context(f"team-{tid}", "agent-1")
    assert api.team_member_room_context(f"team-{tid}", "agent-1") == ""


def test_accepting_peer_delivery_preserves_earlier_unread_leader_context(tmp_path) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "obj"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "recon", "objective": "map"})
    api.add_team_member(tid, {"role": "analyst", "objective": "verify"})
    api._append_team_message(
        tid, content="earlier-public-leader-context", target_agent_ids=[], target_roles=[],
    )

    api.publish_team_member_output(f"team-{tid}", "agent-0", "recon", "later-peer-group-context")

    member = api._team_manager(tid).agents[1]
    assert len(member["inputs"]) == 1
    assert "later-peer-group-context" in member["inputs"][0]
    context = api.team_member_room_context(f"team-{tid}", "agent-1")
    assert "earlier-public-leader-context" in context
    assert "later-peer-group-context" in context
    assert context.index("earlier-public-leader-context") < context.index("later-peer-group-context")
    assert api.team_member_room_context(f"team-{tid}", "agent-1") == ""


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("@allocation is a variable, not a room mention", set()),
        ("Contact foo@analyst.com for the report", set()),
        ("@analyst-2 请检查", {"hyphen-role"}),
        ("@analyst expert 请检查", {"spaced-role"}),
        ("@analyst，请核验证据。", {"short-role", "same-role"}),
        ("(@ANALYST): inspect the evidence", {"short-role", "same-role"}),
        ("（@中文·分析员），请核验证据。", {"chinese-role"}),
        ("@all 请检查", "all"),
        ("@everyone inspect the evidence", "all"),
        ("@全员 请检查", "all"),
        ("@全体 请检查", "all"),
    ],
)
def test_explicit_mention_parser_uses_real_boundaries_and_longest_role(message, expected) -> None:
    roster = [
        {"agent_id": "sender", "role": "coordinator"},
        {"agent_id": "short-role", "role": "analyst"},
        {"agent_id": "same-role", "role": "analyst"},
        {"agent_id": "hyphen-role", "role": "analyst-2"},
        {"agent_id": "spaced-role", "role": "analyst expert"},
        {"agent_id": "chinese-role", "role": "中文·分析员"},
    ]
    targets = TeamsMixin._mentioned_member_ids(message, roster, "sender")
    if expected == "all":
        expected = {item["agent_id"] for item in roster if item["agent_id"] != "sender"}
    assert set(targets) == expected
    assert len(targets) == len(set(targets))


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


def test_team_rename_serializes_with_message_append(tmp_path, monkeypatch) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Before", "objective": "scope"})["team"]["team_id"]
    path = api._team_path(tid)
    read, release = threading.Event(), threading.Event()
    append_started, appended = threading.Event(), threading.Event()
    original_read = Path.read_text

    def _read_at_update(file, *args, **kwargs):
        text = original_read(file, *args, **kwargs)
        if file == path and threading.current_thread().name.startswith("metadata-update") and not read.is_set():
            read.set()
            assert release.wait(timeout=2)
        return text

    def _append():
        append_started.set()
        result = api._append_team_message(
            tid, content="parallel evidence", target_agent_ids=[], target_roles=[],
        )
        appended.set()
        return result

    monkeypatch.setattr(Path, "read_text", _read_at_update)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata-update") as updater, ThreadPoolExecutor(max_workers=1) as writer:
        future = updater.submit(api.update_team, tid, {"name": "After"})
        try:
            assert read.wait(timeout=2)
            message = writer.submit(_append)
            assert append_started.wait(timeout=2)
            assert not appended.wait(timeout=0.05)
        finally:
            release.set()
        assert future.result(timeout=2)["ok"] is True
        assert message.result(timeout=2)["content"] == "parallel evidence"
    stored = api._team_meta(tid)
    assert stored["name"] == "After"
    assert [item["content"] for item in stored["messages"]] == ["parallel evidence"]


def test_greenlight_merges_latest_metadata_and_calls_manager_outside_room_lock(tmp_path, monkeypatch) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "scope"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "A", "objective": "inspect"})
    manager = api._team_manager(tid)
    original_list = manager.list
    inserted = False

    def _publish_while_listing():
        entry = api._append_team_message(
            tid, content="arrived during roster projection", target_agent_ids=[], target_roles=[],
        )
        api._set_team_room_cursor(tid, "agent-0", entry["sequence"])

    def _list():
        nonlocal inserted
        if not inserted:
            inserted = True
            # A manager callback may need a room transaction on another
            # thread. Holding the room lock here would deadlock the callback.
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                pool.submit(_publish_while_listing).result(timeout=2)
            finally:
                # A locking regression must fail, rather than wait forever
                # for this callback while its caller still owns the room lock.
                pool.shutdown(wait=False, cancel_futures=True)
        return original_list()

    monkeypatch.setattr(manager, "list", _list)
    result = api.set_team_greenlight(tid, True)
    assert result["ok"] is True
    stored = api._team_meta(tid)
    assert stored["greenlight"] is True
    assert stored["messages"][0]["content"] == "arrived during roster projection"
    assert stored["member_cursors"]["agent-0"] == stored["messages"][0]["sequence"]
    assert manager.agents[0]["greenlight"] is True


def test_failed_send_rollback_does_not_rewind_a_concurrent_room_read(tmp_path, monkeypatch) -> None:
    api = _TeamApi(tmp_path)
    tid = api.create_team({"name": "Audit", "objective": "scope"})["team"]["team_id"]
    api.add_team_member(tid, {"role": "A", "objective": "inspect"})
    api._append_team_message(tid, content="older leader context", target_agent_ids=[], target_roles=[])
    manager = api._team_manager(tid)
    advanced: list[int] = []

    def _refuse_after_reader_progress(agent_id, _message, **_kwargs):
        event = api._append_team_message(tid, content="newer room context", target_agent_ids=[], target_roles=[])
        api._team_room_context(tid, agent_id, through_sequence=event["sequence"])
        advanced.append(event["sequence"])
        return {"ok": False, "error": "pending confirmation"}

    monkeypatch.setattr(manager, "send_input", _refuse_after_reader_progress)
    assert api.send_team_message(tid, "refused command", ["agent-0"])["ok"] is False
    assert api._team_meta(tid)["member_cursors"]["agent-0"] == advanced[0]
    api._set_team_room_cursor(tid, "agent-0", 0)
    assert api._team_meta(tid)["member_cursors"]["agent-0"] == advanced[0]
    # A rollback that still owns the exact provisional cursor remains valid.
    api._set_team_room_cursor(tid, "agent-0", 1, expected_sequence=advanced[0])
    assert api._team_meta(tid)["member_cursors"]["agent-0"] == 1
