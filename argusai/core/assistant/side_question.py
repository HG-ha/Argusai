"""One-shot side questions that read a conversation without joining it.

A side question ("/btw") answers from the conversation the user is watching
while the main turn keeps running. Nothing it produces is written back: the
question and the answer never enter ``session.history``, so they cost no
context on any later turn.

Two properties make this safe to run against a live session:

* The transcript is *flattened to text*. A running turn is frequently parked on
  a tool call whose result has not arrived yet, so the raw message list ends
  with an unmatched ``tool_calls`` entry that Anthropic and OpenAI both reject.
  Flattening also means one code path instead of one per provider dialect.
* Nothing here mutates the session. The caller passes a snapshot of the history
  and its own provider; ``session.ai`` belongs to the running turn and is closed
  by it.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: Deliberately close to Claude Code's own side-question contract: no tools, one
#: response, no steering of the main run.
SIDE_SYSTEM_PROMPT = (
    "You are a lightweight side agent spawned to answer ONE question about the "
    "conversation transcript below.\n"
    "- The main agent is still running. You are a separate instance and must not "
    "steer, resume, or comment on what it should do next.\n"
    "- You have NO tools. Answer from the transcript and your own knowledge only.\n"
    "- If the transcript does not contain the answer, say so plainly instead of "
    "guessing or promising to look it up.\n"
    "- Never say \"let me check\" or announce an action: there is no follow-up "
    "turn and nothing you say is remembered.\n"
    "- Reply in the language the question is written in, and keep it short."
)

_DEFAULT_BUDGET_CHARS = 24000
_PER_MESSAGE_CHARS = 2000


def _clip(text: str, limit: int = _PER_MESSAGE_CHARS) -> str:
    """Elide the middle, which is where long tool payloads carry the least."""
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    head = clean[: limit * 2 // 3]
    tail = clean[-(limit // 3):]
    return f"{head}\n…[{len(clean) - len(head) - len(tail)} chars omitted]…\n{tail}"


def _tool_call_line(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or call.get("name") or "tool")
    raw_args = function.get("arguments") if function else call.get("arguments")
    if isinstance(raw_args, (dict, list)):
        args = json.dumps(raw_args, ensure_ascii=False)
    else:
        args = str(raw_args or "")
    return f"[tool call] {name}({_clip(args, 300)})"


def _message_lines(message: Any) -> list[str]:
    role = str(getattr(message, "role", "") or "")
    content = _clip(str(getattr(message, "content", "") or ""))
    if role == "user":
        return [f"User: {content}"] if content else []
    if role == "tool":
        return [f"[tool result] {content}"] if content else []
    if role != "assistant":
        return []
    lines = [f"Assistant: {content}"] if content else []
    lines.extend(
        _tool_call_line(call)
        for call in list(getattr(message, "tool_calls", None) or [])
        if isinstance(call, dict)
    )
    return lines


def transcript_from_history(
    history: Iterable[Any],
    *,
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> str:
    """Flatten the tail of a conversation into a readable transcript.

    Walks backwards so the newest exchanges survive the budget: a side question
    is nearly always about what just happened.
    """
    blocks: list[str] = []
    used = 0
    for message in reversed(list(history)):
        if bool(getattr(message, "hidden", False)):
            continue
        lines = _message_lines(message)
        if not lines:
            continue
        chunk = "\n".join(lines)
        if used + len(chunk) > max(2000, int(budget_chars)):
            break
        blocks.append(chunk)
        used += len(chunk)
    blocks.reverse()
    return "\n\n".join(blocks)


def build_side_messages(
    history: Iterable[Any],
    question: str,
    *,
    memory_summary: str = "",
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
) -> list[dict[str, str]]:
    """Build the two-message prompt for one side question."""
    transcript = transcript_from_history(history, budget_chars=budget_chars)
    summary = _clip(str(memory_summary or ""), 2000)
    parts = []
    if summary:
        parts.append(f"<earlier_summary>\n{summary}\n</earlier_summary>")
    parts.append(f"<transcript>\n{transcript or '(empty conversation)'}\n</transcript>")
    parts.append(
        "The user asked this side question about the conversation above. "
        "Answer it directly, in one response:\n"
        f"{str(question or '').strip()}"
    )
    return [
        {"role": "system", "content": SIDE_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def answer_side_question(
    provider: Any,
    history: Iterable[Any],
    question: str,
    *,
    memory_summary: str = "",
    budget_chars: int = _DEFAULT_BUDGET_CHARS,
    max_tokens: int = 1200,
) -> str:
    """Ask ``provider`` one toolless question and return the plain answer."""
    raw = await provider.chat(
        messages=build_side_messages(
            history,
            question,
            memory_summary=memory_summary,
            budget_chars=budget_chars,
        ),
        tools=None,
        max_tokens=max_tokens,
    )
    for choice in list(raw.get("choices") or []):
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict):
            text = str(message.get("content") or "").strip()
            if text:
                return text
    return ""
