"""Session memory: model-driven browse/search (no prompt injection)."""

from __future__ import annotations

from argusai.core.assistant.messages import ChatMessage
from argusai.core.assistant.session import AssistantSession
from argusai.core.tools.kb_tasks import read_assistant_session


def _long_session() -> AssistantSession:
    session = AssistantSession()
    session.history.append(ChatMessage(
        role="user",
        content="https://example.com/login 用 python 实现验证码纯算",
        timestamp=1.0,
    ))
    for i in range(250):
        session.history.append(ChatMessage(
            role="assistant",
            content=f"step {i}",
            tool_calls=[{"id": f"tc{i}", "type": "function", "function": {"name": "grep_text", "arguments": "{}"}}],
            timestamp=float(10 + i),
        ))
        session.history.append(ChatMessage(
            role="tool",
            content='{"ok": true}',
            tool_call_id=f"tc{i}",
            timestamp=float(11 + i),
        ))
    session.history.append(ChatMessage(
        role="user",
        content="你还记得我说的第一句话是什么吗",
        timestamp=9999.0,
    ))
    return session


def test_memory_index_includes_all_user_turns() -> None:
    session = _long_session()
    session.rebuild_memory()
    user_ids = {e["id"] for e in session.memory_index if e.get("role") == "user"}
    assert "0" in user_ids


def test_search_memory_can_find_opening_goal_by_url() -> None:
    session = _long_session()
    hits = session.search_memory("example.com login")
    assert any("example.com/login" in str(h.get("text") or "") for h in hits)


def test_list_session_messages_asc_returns_first_user() -> None:
    session = _long_session()
    out = session.list_session_messages_tool(role="user", order="asc", limit=1)
    assert out["messages"][0]["message_id"] == 0
    assert "example.com/login" in out["messages"][0]["preview"]


def test_read_session_message_fetches_full_row() -> None:
    session = _long_session()
    out = session.read_session_message_tool(0)
    assert out["ok"] is True
    assert "example.com/login" in out["content"]


def test_read_assistant_session_pages_visible_messages_by_exact_id() -> None:
    class _Api:
        def get_assistant_history(self, session_id: str) -> dict:
            assert session_id == "cron-run-123"
            return {
                "ok": True,
                "session_id": session_id,
                "name": "Cron patrol",
                "history": [
                    {"role": "user", "content": "run patrol", "timestamp": 1.0},
                    {"role": "assistant", "content": "first result", "timestamp": 2.0},
                    {"role": "assistant", "content": "final exact result", "timestamp": 3.0},
                    {"role": "assistant", "content": "internal", "hidden": True, "timestamp": 4.0},
                ],
            }

    out = read_assistant_session(
        _Api(),
        session_id="cron-run-123",
        role="assistant",
        order="desc",
        limit=1,
    )

    assert out["ok"] is True
    assert out["session_id"] == "cron-run-123"
    assert out["session_name"] == "Cron patrol"
    assert out["total_matching"] == 2
    assert out["has_more"] is True
    assert out["messages"][0]["content"] == "final exact result"


def test_memory_prompt_does_not_inject_session_origin() -> None:
    session = _long_session()
    prompt = session.memory_prompt("你还记得第一句话")
    assert "Session origin" not in prompt


def test_rebuild_memory_skips_scan_when_history_unchanged() -> None:
    session = _long_session()
    session.rebuild_memory()
    sentinel = object()
    session.memory_index = sentinel  # type: ignore[assignment]
    session.rebuild_memory()  # history unchanged -> must not re-scan
    assert session.memory_index is sentinel


def test_rebuild_memory_reruns_after_append() -> None:
    session = _long_session()
    session.rebuild_memory()
    session.memory_index = []  # type: ignore[assignment]
    session.history.append(ChatMessage(role="user", content="新的一句话", timestamp=10000.0))
    session.rebuild_memory()  # length changed -> fingerprint differs -> rebuild
    assert session.memory_index != []


def test_rebuild_memory_reruns_after_tail_tool_trace_enrichment() -> None:
    session = _long_session()
    session.rebuild_memory()
    session.memory_index = []  # type: ignore[assignment]
    # Enrich the tail message in place without changing its content length.
    session.history[-1].tool_trace = [{"name": "grep_text", "ok": True}]
    session.rebuild_memory()
    assert session.memory_index != []


def test_volatile_context_moves_off_system_onto_last_user_turn() -> None:
    session = AssistantSession()
    msgs = [
        {"role": "system", "content": "STABLE-SYSTEM"},
        {"role": "user", "content": "原始目标"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "继续"},
    ]
    volatile = (
        "\n\n## Long-term Conversation Summary\nMEMORY-XYZ"
        "\n\n## Relevant playbooks for this turn\n- `agent-todo`: track progress."
    )
    session._attach_volatile_context(msgs, volatile)
    assert "MEMORY-XYZ" not in msgs[0]["content"], "must not pollute the stable system prefix"
    assert "agent-todo" not in msgs[0]["content"]
    assert msgs[0]["content"] == "STABLE-SYSTEM"
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"].endswith("track progress.")


def test_volatile_context_falls_back_to_system_without_user_turn() -> None:
    session = AssistantSession()
    msgs = [{"role": "system", "content": "SYS"}]
    session._attach_volatile_context(msgs, "\n\nMEM")
    assert msgs[0]["content"].endswith("MEM")


def test_volatile_context_empty_is_noop() -> None:
    session = AssistantSession()
    msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    snapshot = [dict(m) for m in msgs]
    session._attach_volatile_context(msgs, "")
    assert msgs == snapshot
