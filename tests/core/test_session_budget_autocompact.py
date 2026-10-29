"""Final budgeter auto-compact (Loom22 §12.6).

When the prompt is over budget the budgeter drops the oldest user-turn chunks,
but must first fold their task-critical gist (goals / conclusions / artifact
refs) into a digest prepended to the surviving context — instead of losing it.
The surviving message *structure* (roles, count, tool_use/tool_result pairing)
must stay intact so no provider 400s on the next turn.
"""

from __future__ import annotations

import json

from argusai.core.assistant.session import AssistantSession


def _messages() -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "SYS " + ("x" * 200)}]
    for i in range(6):
        msgs.append({"role": "user", "content": f"目标{i}: " + ("padding描述 " * 30)})
        msgs.append({
            "role": "assistant",
            "content": f"结论{i}: 已分析第 {i} 步 " + ("细节 " * 20),
            "tool_calls": [{"id": f"tc{i}", "type": "function",
                            "function": {"name": "grep_text", "arguments": "{}"}}],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"tc{i}",
            "content": json.dumps({"ok": True, "artifact_ref": f"art-{i}", "data": "y" * 400}),
        })
    return msgs


def _check_tool_pairing(msgs: list[dict]) -> None:
    open_ids: set[str] = set()
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id"):
                    open_ids.add(tc["id"])
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            assert tid in open_ids, f"orphan tool result {tid!r}"


def _run(session: AssistantSession, budget: int) -> list[dict]:
    session._prompt_budget_bytes = lambda: budget  # type: ignore[assignment]
    msgs = _messages()
    session._enforce_prompt_budget_in_place(msgs, [])
    return msgs


def test_autocompact_preserves_oldest_goal_and_refs() -> None:
    session = AssistantSession()
    msgs = _run(session, budget=2500)
    blob = "\n".join(str(m.get("content") or "") for m in msgs)
    # Oldest turns were dropped (count shrank) but their gist survives.
    assert len(msgs) < len(_messages())
    assert "目标0" in blob, "oldest user goal must be carried in the digest"
    assert "art-0" in blob, "oldest artifact_ref recovery path must survive"
    assert session._DIGEST_BEGIN in blob


def test_autocompact_keeps_structure_valid() -> None:
    session = AssistantSession()
    msgs = _run(session, budget=2500)
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user", "first non-system message must remain a user turn"
    _check_tool_pairing(msgs)


def test_digest_is_deterministic_idempotent() -> None:
    session = AssistantSession()
    first = "\n".join(str(m.get("content") or "") for m in _run(session, budget=2500))
    second = "\n".join(str(m.get("content") or "") for m in _run(session, budget=2500))
    assert first == second, "same history must yield byte-identical digest (cache-friendly)"


def test_under_budget_is_left_untouched() -> None:
    session = AssistantSession()
    msgs = _run(session, budget=10_000_000)
    assert msgs == _messages(), "nothing should change when well under budget"
