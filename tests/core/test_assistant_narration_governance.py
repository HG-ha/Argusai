"""Lock down the contract that keeps the assistant from talking to itself.

Two layers are exercised:

1. The ``Core Contract`` section of the system prompt explicitly forbids
   chained scaffolding/narration sentences ("好问题", "让我先看看", "let me…")
   and explicitly forbids replying to the runtime's hidden continuation
   nudges with meta-talk such as "明白" / "任务已完成".

2. The two internal continuation prompts (``_build_toolless_continuation_prompt``
   and ``_build_auto_continue_prompt``) carry the ``INTERNAL`` marker and
   instruct the model not to acknowledge or quote them. This is what stops
   the visible "明白。上一轮我已完整回答了…任务已完成" reply pattern reported
   from the chat UI.
"""

from __future__ import annotations

from argusai.core.assistant import AssistantSession
from argusai.core.assistant.skills import _build_system_prompt


def test_core_contract_bans_scaffolding_narration() -> None:
    prompt = _build_system_prompt("- `tool_search(query)`: Search tools.", "")

    # Hard ban on the most common Chinese scaffolding phrases the weak
    # models love to emit between tool calls.
    for forbidden in ("好问题", "让我先看看", "让我把", "let me read", "I'll first"):
        assert forbidden in prompt, (
            f"Core Contract must explicitly call out '{forbidden}' as forbidden "
            f"scaffolding; otherwise the model keeps emitting it."
        )

    # Hard ban on the meta-talk pattern that surfaced in the bug report:
    # the model replying to the internal continuation nudge instead of the
    # user.
    for forbidden in ("明白", "任务已完成", "上一轮我已"):
        assert forbidden in prompt, (
            f"Core Contract must teach the model not to produce '{forbidden}' "
            f"in response to system-injected continuation prompts."
        )

    # The "silent chaining is fine" rule must be present — otherwise the
    # earlier `do not run a long tool chain in total silence` line tempts
    # the model into chaining narration sentences.
    assert "Silent tool chaining" in prompt


def test_toolless_continuation_prompt_marks_itself_internal() -> None:
    session = AssistantSession()
    msg = session._build_toolless_continuation_prompt()

    assert "INTERNAL" in msg
    assert "do not acknowledge" in msg.lower() or "do not quote" in msg.lower()
    # The prompt must spell out the bad reply pattern so the model knows
    # what NOT to emit. Without this, models cheerfully respond "明白" /
    # "好的，我已完成" and the user sees the runtime nudge bleed through.
    for banned in ("明白", "任务已完成", "上一轮我已"):
        assert banned in msg


def test_auto_continue_prompt_is_internal_in_both_branches() -> None:
    session = AssistantSession()

    # Branch 1: no unfinished todos.
    empty_msg = session._build_auto_continue_prompt()
    assert "INTERNAL" in empty_msg
    assert "过场白" in empty_msg or "narration" in empty_msg.lower()

    # Branch 2: unfinished todos present.
    session.set_todos([
        {"id": "phase.recon", "title": "资产发现", "status": "in_progress"},
    ])
    filled_msg = session._build_auto_continue_prompt()
    assert "INTERNAL" in filled_msg
    assert "phase.recon" in filled_msg
    assert "过场白" in filled_msg or "narration" in filled_msg.lower()
    assert "不要复述" in filled_msg
