"""Assistant runtime helpers split from the former monolithic module."""

from __future__ import annotations

import inspect
import itertools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

log = logging.getLogger("argusai.assistant")

__all__ = ['_assistant_debug_log', '_DEFAULT_MAX_AGENT_ROUNDS', '_DEFAULT_MAX_AUTO_CONTINUE', '_DEFAULT_MAX_TOOLLESS_CONTINUE_DECISIONS', '_DEFAULT_MAX_BUDGET_RESTARTS', '_DEFAULT_AUTO_ARCHIVE_TOOL_RESULTS', '_PSEUDO_TOOL_BLOCK_RE', '_PSEUDO_TAG_RE', '_DSML_INVOKE_RE', '_DSML_PARAM_RE', '_recover_dsml_tool_calls', '_strip_pseudo_tool_calls', '_REPEAT_KEY_PUNCT_RE', '_REPEAT_KEY_EMOJI_RE', '_CONFIRM_APPROVE_TERMS', '_CONFIRM_AUTO_ALLOW_TERMS', '_CONFIRM_DENY_TERMS', '_CONFIRMATION_VERDICTS', '_normalise_confirmation_text', '_confirmation_intent', '_normalize_confirmation_verdict', 'normalize_confirmation_decision', 'confirmation_decision_from_legacy_message', '_is_confirmation_protocol_message', '_repeat_key', '_collapse_repeated_response_text', '_collapse_repeated_response_text_verbose', '_collapse_repeated_line_blocks', '_PseudoTagStreamFilter', '_StreamRepetitionGuard', '_WRITE_TOOLS', '_CONFIRM_TOOLS', '_CONFIRM_LABEL_AFTER_APPROVAL', 'EXTERNAL_AGENT_TOOL', '_NO_AUTO_APPROVE_TOOLS', '_REVERSE_DYNAMIC_TOOLS', '_POLLING_TOOLS', '_PRESENTATION_TOOLS', '_CONTROL_TOOLS', 'FRESHNESS_KEYS', 'tool_kind', 'should_dedupe_tool_call', 'prepare_tool_arguments_for_dispatch', 'TOOL_ARGUMENTS_ERROR_KEY', 'parse_tool_call_arguments', 'repair_llm_json', 'tool_arguments_error']

def _assistant_debug_log(message: str) -> None:
    """Emit assistant runtime diagnostics to stdout AND the rotating log file.

    The stdout half is preserved for developers running ``python -m argusai``
    in a console (the previous behaviour). The logging half is what users
    of the packaged onefile see — they have no console, but their
    ``logs/assistant_loop.log`` rotating file will capture every line.
    Records emitted from this helper are tagged at ``DEBUG`` level so they
    only land in the assistant-loop file by default (assistant logger has
    DEBUG level via :mod:`argusai.core.infra.logging_setup`).
    """

    text = str(message)
    try:
        log.debug("%s", text)
    except Exception:
        pass
    if os.environ.get("ARGUSAI_DEBUG_LOG", "0").lower() not in {"1", "true", "yes"}:
        return
    try:
        print(f"[argusai-debug] {text}", flush=True)
    except Exception:
        pass


# Maximum number of consecutive AI-call + tool-execution rounds in a single
# user turn. After this we force a textual summary so the user does not have
# to manually type "继续" every few calls just because the agent is doing real
# work (PortSwigger labs / SQLi enumeration easily walk through 20-80 probes
# per turn — blind byte-by-byte password extraction over a 20-char secret can
# cost 100+ tool calls on its own). The ceiling is still finite so a model
# stuck in a loop cannot burn the user's API quota forever; bumping above
# ~100 should be paired with a stronger nudge in the system prompt toward
# bulk extraction (UNION / cast-error / time-blind range scans) instead of
# one-byte-at-a-time loops.
_DEFAULT_MAX_AGENT_ROUNDS = 200

# Maximum number of times a single user turn may auto-continue when the model
# stops emitting tool calls but its own todo list still has unfinished items.
# Acts as a hard safety cap in case the model refuses to update todos.
_DEFAULT_MAX_AUTO_CONTINUE = 10

# Toolless-continuation probe. DISABLED by default (0).
#
# Standard function-calling agent loops use a single structural completion
# signal: a model turn that emits NO
# tool_calls IS the end of the turn. Continuation is driven by EXPLICIT
# model-side state — pending todos (auto-continue) or `stop_for_user_decision`
# — never by re-prompting the model to second-guess a finished answer.
#
# The old probe re-ran the model after every toolless reply that "didn't look
# like a question", which (a) required brittle natural-language cue lists to
# avoid false positives and (b) produced duplicate-looking summaries when the
# model had, in fact, already finished. Set this >0 only if you run a weak
# model that habitually stops one step early and you accept the duplicate risk.
_DEFAULT_MAX_TOOLLESS_CONTINUE_DECISIONS = 0

# When the round cap above is hit, we ask the model itself (one extra
# non-tool-suppressed call) whether it wants to keep going. If yes, the
# loop is restarted with a fresh round counter. This safety cap bounds how
# many such restarts may happen inside a single user turn.
_DEFAULT_MAX_BUDGET_RESTARTS = 10


_DEFAULT_AUTO_ARCHIVE_TOOL_RESULTS = {
    "http_curl",
    "http_request_impersonate",
    "raw_http_send",
    "browser_use_tab",
    "browser_trace",
    "browser_probe",
    "sqli_probe_param",
}


# Some chat-tuned models (DeepSeek / Doubao / GLM family etc.) sometimes leak
# their internal tool-call template into ``message.content`` as pseudo-XML
# using full-width pipes ``｜`` (U+FF5C) or regular ``|``:
#
#     <｜DSML｜tool_calls>
#       <｜DSML｜invoke name="payload_probe">
#         <｜DSML｜parameter name="url" string="true">https://...</｜DSML｜parameter>
#         ...
#       </｜DSML｜invoke>
#     </｜DSML｜tool_calls>
#
# The model *sometimes* also fills the real ``tool_calls`` field, in which
# case the inline copy is just noise. But other models emit ONLY the DSML
# template and leave ``tool_calls`` empty, which would end the turn early
# and force the user to type "继续". So we (a) strip the template from the
# visible content, and (b) when ``tool_calls`` is empty but DSML is present,
# parse the DSML and synthesise a real tool_calls list so the agent loop
# can continue automatically.

_PSEUDO_TOOL_BLOCK_RE = re.compile(
    r"<\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]tool_calls\s*>.*?</\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]tool_calls\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PSEUDO_TAG_RE = re.compile(
    r"<\s*/?\s*[\uff5c|][^<>\n\r]{1,400}?[\uff5c|][^<>\n\r]{0,200}?>",
)


_DSML_INVOKE_RE = re.compile(
    r"<\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]invoke\s+name\s*=\s*\"([^\"]+)\"\s*>(.*?)</\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]invoke\s*>",
    re.DOTALL | re.IGNORECASE,
)
_DSML_PARAM_RE = re.compile(
    r"<\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]parameter\s+name\s*=\s*\"([^\"]+)\"(?:\s+[a-z_]+\s*=\s*\"[^\"]*\")?\s*>(.*?)</\s*[\uff5c|][^<>\uff5c|]{1,80}[\uff5c|]parameter\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _recover_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse leaked ``<｜...｜invoke>`` blocks into OpenAI-style tool_calls.

    Returns an empty list when nothing recognisable is found. The synthesised
    calls have stable ``call_dsml_N`` ids so duplicate-detection in the agent
    loop still works.
    """
    if not text or not isinstance(text, str):
        return []
    tool_calls: list[dict[str, Any]] = []
    for idx, m in enumerate(_DSML_INVOKE_RE.finditer(text)):
        name = (m.group(1) or "").strip()
        if not name:
            continue
        body = m.group(2) or ""
        args: dict[str, Any] = {}
        for pm in _DSML_PARAM_RE.finditer(body):
            key = (pm.group(1) or "").strip()
            raw_val = pm.group(2) or ""
            val_str = raw_val.strip()
            if not key:
                continue
            # Try JSON first (handles numbers, bools, arrays, nested objects).
            try:
                args[key] = json.loads(val_str)
                continue
            except Exception:
                pass
            args[key] = val_str
        try:
            arguments = json.dumps(args, ensure_ascii=False)
        except Exception:
            arguments = "{}"
        tool_calls.append({
            "id": f"call_dsml_{idx}_{abs(hash((name, arguments))) & 0xFFFFFFFF:08x}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return tool_calls


def _strip_pseudo_tool_calls(text: str) -> str:
    """Remove leaked ``<｜...｜tool_calls>...`` markup from a model reply."""
    if not text or not isinstance(text, str):
        return text or ""
    # 1. Drop whole well-formed ``tool_calls`` envelopes (tags + body).
    cleaned = _PSEUDO_TOOL_BLOCK_RE.sub("", text)
    # 2. Drop any remaining stray pseudo template tags. We do NOT strip the
    #    bodies in between for safety: if only an opening tag was emitted,
    #    losing the body could erase legitimate prose. In practice the
    #    leftover bodies after step 1 are tiny stragglers.
    cleaned = _PSEUDO_TAG_RE.sub("", cleaned)
    # Collapse the blank lines we just created.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# Intentionally does NOT strip digits. Digit-stripping made structured numeric
# output (九九乘法表, CVE ids, enums, hex) collapse to identical skeletons
# like "×=×=×=" and trip the in-stream repetition watchdog as a false positive.
_REPEAT_KEY_PUNCT_RE = re.compile(r"[`*_#>\-•\.\)\(：:，,。；;！？!?、\[\]\{\}（）「」『』《》]+")
_REPEAT_KEY_EMOJI_RE = re.compile(
    r"[\u2600-\u27BF\u2B00-\u2BFF\U0001F300-\U0001FAFF\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\u2700-\u27BF\u2300-\u23FF"
    r"\u2190-\u21FF\u2460-\u24FF]+"
)


_CONFIRM_APPROVE_TERMS = {
    "yes", "ok", "confirm", "allow", "approve", "y",
    "是", "确认", "允许", "同意", "好", "可以", "行", "执行",
}
_CONFIRM_AUTO_ALLOW_TERMS = {
    "allow_session", "auto_allow", "always_allow", "本次对话自动允许", "自动允许",
}
_CONFIRM_DENY_TERMS = {
    "no", "cancel", "deny", "reject", "abort", "stop", "n",
    "否", "取消", "拒绝", "停止",
}


def _normalise_confirmation_text(text: str) -> str:
    return re.sub(r"[\s`*_#>\-•\d\.\)\(：:，,。；;！？!?、\[\]\{\}（）「」『』《》]+", "", (text or "").strip().lower())


def _confirmation_intent(message: str) -> str:
    """Legacy text → verdict helper (migration / old clients only).

    New UI must send a typed ``confirmation`` object (Loom22 §9). Natural
    language like "不搞了" must never be guessed as a confirmation decision.
    """

    raw = (message or "").strip()
    text = raw.lower()
    compact = _normalise_confirmation_text(raw)
    if text in _CONFIRM_AUTO_ALLOW_TERMS or compact in _CONFIRM_AUTO_ALLOW_TERMS:
        return "allow_session"
    if text in _CONFIRM_APPROVE_TERMS or compact in _CONFIRM_APPROVE_TERMS:
        return "allow"
    if text in _CONFIRM_DENY_TERMS or compact in _CONFIRM_DENY_TERMS:
        return "deny"
    return ""


_CONFIRMATION_VERDICTS = frozenset({"allow", "allow_session", "deny"})


def _normalize_confirmation_verdict(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _CONFIRMATION_VERDICTS:
        return text
    aliases = {
        "allow_once": "allow",
        "accept": "allow",
        "acceptforsession": "allow_session",
        "accept_for_session": "allow_session",
        "decline": "deny",
        "cancel": "deny",
    }
    if text in aliases:
        return aliases[text]
    # Map legacy UI tokens (yes / no / allow_session …) into canonical verdicts.
    return _confirmation_intent(text)


def normalize_confirmation_decision(payload: Any) -> dict[str, Any] | None:
    """Normalize a typed confirmation RPC payload (Loom22 §9).

    Preferred shapes::

        {"type": "batch_confirmation_decision", "decisions": {"call_id": "allow"}}
        {"type": "confirmation_decision", "verdict": "deny"}
        {"type": "confirmation_decision", "call_id": "…", "verdict": "allow_session"}
    """

    if not isinstance(payload, dict):
        return None
    raw_type = str(payload.get("type") or "").strip().lower()
    auto_allow_names = [
        str(n) for n in (payload.get("auto_allow_names") or []) if str(n or "").strip()
    ]
    auto_allow_permissions = [
        str(p) for p in (payload.get("auto_allow_permissions") or []) if str(p or "").strip()
    ]
    pending_id = str(payload.get("pending_id") or "").strip()
    version = payload.get("version")

    decisions: dict[str, str] = {}
    raw_decisions = payload.get("decisions")
    if isinstance(raw_decisions, dict):
        for key, value in raw_decisions.items():
            verdict = _normalize_confirmation_verdict(value)
            if verdict:
                decisions[str(key)] = verdict

    if raw_type in {"batch_confirmation_decision", "confirm_batch"} or (
        not raw_type and decisions
    ):
        if not decisions:
            return None
        out: dict[str, Any] = {
            "type": "batch_confirmation_decision",
            "decisions": decisions,
            "auto_allow_names": auto_allow_names,
            "auto_allow_permissions": auto_allow_permissions,
        }
        if pending_id:
            out["pending_id"] = pending_id
        if version is not None:
            out["version"] = version
        return out

    call_id = str(
        payload.get("call_id") or payload.get("confirm_id") or ""
    ).strip()
    verdict = _normalize_confirmation_verdict(
        payload.get("verdict") if "verdict" in payload else payload.get("decision")
    )
    if raw_type in {"confirmation_decision", "tool_decision", "confirm_one"} or (
        not raw_type and verdict and not decisions
    ):
        if not verdict:
            return None
        out = {
            "type": "confirmation_decision",
            "verdict": verdict,
            "auto_allow_names": auto_allow_names,
            "auto_allow_permissions": auto_allow_permissions,
        }
        if call_id:
            out["call_id"] = call_id
        if pending_id:
            out["pending_id"] = pending_id
        if version is not None:
            out["version"] = version
        return out

    if decisions:
        return {
            "type": "batch_confirmation_decision",
            "decisions": decisions,
            "auto_allow_names": auto_allow_names,
            "auto_allow_permissions": auto_allow_permissions,
        }
    return None


def confirmation_decision_from_legacy_message(message: str) -> dict[str, Any] | None:
    """Migrate old ``confirm_batch:`` / ``yes`` chat-channel payloads to typed form."""

    raw = (message or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low.startswith("confirm_batch:"):
        try:
            body = json.loads(raw.split(":", 1)[1])
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        body = dict(body)
        body.setdefault("type", "batch_confirmation_decision")
        return normalize_confirmation_decision(body)
    if low.startswith("confirm_one:"):
        try:
            _, rest = raw.split(":", 1)
            tcid, verdict = rest.split(":", 1)
        except Exception:
            return None
        return normalize_confirmation_decision({
            "type": "confirmation_decision",
            "call_id": tcid.strip(),
            "verdict": verdict.strip(),
        })
    intent = _confirmation_intent(raw)
    if not intent:
        return None
    return normalize_confirmation_decision({
        "type": "confirmation_decision",
        "verdict": intent,
    })


def _is_confirmation_protocol_message(message: str) -> bool:
    """True when ``message`` is a legacy confirmation chat-channel payload."""

    return confirmation_decision_from_legacy_message(message) is not None


def _repeat_key(text: str) -> str:
    """Normalise ``text`` for similarity comparison in dedup / loop watchdog.

    Strips whitespace, common punctuation (both ASCII and CJK), and emoji so
    two lines that differ only in decoration ("让我试试 ✅" vs "让我试试") map
    to the same key. Digits and letters are kept — otherwise a times-table or
    numbered list collapses into a repeated skeleton and false-triggers the
    stream repetition guard.
    """

    key = text or ""
    key = _REPEAT_KEY_EMOJI_RE.sub("", key)
    key = _REPEAT_KEY_PUNCT_RE.sub("", key)
    key = re.sub(r"\s+", "", key)
    return key.lower()


def _collapse_repeated_response_text(text: str, *, max_repeats: int = 2) -> str:
    """Trim obvious model self-repetition in a final text reply.

    Some providers occasionally get stuck repeating the same short Chinese
    status/probe sentence inside one assistant message. This is not an agent
    loop and tool budgets cannot stop it, so collapse consecutive duplicate
    lines before persisting/rendering the final answer.
    """
    collapsed, dropped, _ = _collapse_repeated_response_text_verbose(text, max_repeats=max_repeats)
    if not dropped:
        return collapsed
    return collapsed


def _collapse_repeated_response_text_verbose(
    text: str,
    *,
    max_repeats: int = 2,
) -> tuple[str, int, str]:
    """Like :func:`_collapse_repeated_response_text` but returns stats.

    Returns ``(collapsed_text, dropped_lines, mode)`` where ``mode`` is
    one of ``"none"`` / ``"consecutive"`` / ``"block"`` so the caller can
    log exactly which kind of repetition was detected.
    """

    if not text or len(text) < 80:
        return text or "", 0, "none"
    lines = text.splitlines()
    out: list[str] = []
    prev_key = ""
    repeat_count = 0
    dropped = 0
    mode = "consecutive"
    for line in lines:
        key = _repeat_key(line)
        # Lower the per-line minimum key length so short Chinese
        # sentences like "不，已能确认从未生效。" (which normalise to
        # ~10 chars) are caught. Previously this was clamped to 8,
        # which still worked for ASCII paragraphs but missed CJK
        # status lines.
        if key and key == prev_key and len(key) >= 4:
            repeat_count += 1
        else:
            prev_key = key
            repeat_count = 1
        if key and repeat_count > max_repeats:
            dropped += 1
            continue
        out.append(line)
    if not dropped:
        out, block_dropped = _collapse_repeated_line_blocks(lines, max_repeats=max_repeats)
        if not block_dropped:
            return text, 0, "none"
        dropped = block_dropped
        mode = "block"
    collapsed = "\n".join(out).rstrip()
    note = f"\n\n[已折叠 {dropped} 行重复输出]"
    return collapsed + note, dropped, mode


def _collapse_repeated_line_blocks(lines: list[str], *, max_repeats: int = 2) -> tuple[list[str], int]:
    keyed = [_repeat_key(line) for line in lines]
    out: list[str] = []
    dropped = 0
    i = 0
    while i < len(lines):
        matched = False
        max_block = min(6, (len(lines) - i) // (max_repeats + 1))
        for block_size in range(1, max_block + 1):
            block = keyed[i:i + block_size]
            if any(len(k) < 4 for k in block):
                continue
            repeats = 1
            while (
                i + (repeats + 1) * block_size <= len(lines)
                and keyed[i + repeats * block_size:i + (repeats + 1) * block_size] == block
            ):
                repeats += 1
            if repeats <= max_repeats:
                continue
            keep_count = max_repeats * block_size
            out.extend(lines[i:i + keep_count])
            dropped += (repeats - max_repeats) * block_size
            i += repeats * block_size
            matched = True
            break
        if not matched:
            out.append(lines[i])
            i += 1
    return out, dropped


class _PseudoTagStreamFilter:
    """Token-aware streaming filter for leaked ``<｜...｜tool_calls>`` markup.

    Token-level stream chunks slice the pseudo template into pieces ('<',
    '｜', 'DSML', ...). A plain regex per chunk can't catch them, so we
    buffer from any ``<`` onwards and decide once we have enough lookahead:

    * If the chunk after ``<`` shows ``｜`` or ``|`` within a short window,
      treat it as a pseudo tag and wait for the closing ``>`` before
      discarding the whole tag.
    * If the chunk reveals anything else (e.g. real HTML / a real ``<`` in
      prose), flush the buffer as-is.
    * When we are sitting inside a ``tool_calls`` envelope, we suppress
      every chunk until the matching closing tag arrives.
    """

    _LOOKAHEAD = 16  # bytes we are willing to hold while deciding

    def __init__(self) -> None:
        self._buf = ""
        self._inside_tool_calls = False

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._inside_tool_calls:
                idx = self._buf.find(">")
                if idx == -1:
                    # No closing bracket yet — keep buffering so the
                    # eventual ``</｜...｜tool_calls>`` arrives intact and
                    # can be recognised. Cap the buffer so a malformed
                    # stream can't eat unbounded memory.
                    if len(self._buf) > 16384:
                        self._buf = self._buf[-2048:]
                    break
                tag = self._buf[: idx + 1]
                self._buf = self._buf[idx + 1 :]
                low = tag.lower()
                if "/" in low.split(">")[0] and "tool_calls" in low:
                    self._inside_tool_calls = False
                continue
            lt = self._buf.find("<")
            if lt == -1:
                out.append(self._buf)
                self._buf = ""
                break
            if lt > 0:
                out.append(self._buf[:lt])
                self._buf = self._buf[lt:]
            window = self._buf[: self._LOOKAHEAD]
            # Only treat ``<`` as the start of a pseudo template tag when
            # the very next character (or the one after ``/`` for closing
            # tags) is the template pipe ``｜`` (U+FF5C) or ``|``. A bare
            # ``|`` further down the chunk does NOT count, otherwise real
            # HTML like ``</a>`` would be eaten just because a leaked
            # pseudo tag appears later in the same chunk.
            looks_pseudo = (
                (len(window) > 1 and window[1] in ("|", "\uff5c")) or
                (len(window) > 2 and window[1] == "/" and window[2] in ("|", "\uff5c"))
            )
            if not looks_pseudo and len(self._buf) >= self._LOOKAHEAD:
                out.append(self._buf[:1])
                self._buf = self._buf[1:]
                continue
            if not looks_pseudo:
                break
            idx = self._buf.find(">")
            if idx == -1:
                if len(self._buf) > 2048:
                    self._buf = ""
                break
            tag = self._buf[: idx + 1]
            self._buf = self._buf[idx + 1 :]
            if re.search(r"[\uff5c|]\s*tool_calls\s*>", tag, re.IGNORECASE) and not tag.lstrip().startswith("</"):
                self._inside_tool_calls = True
            continue
        return "".join(out)

    def flush(self) -> str:
        rest = self._buf
        self._buf = ""
        self._inside_tool_calls = False
        return _strip_pseudo_tool_calls(rest)


class _StreamRepetitionGuard:
    """Real-time watchdog for in-stream model self-repetition.

    Diagnoses the failure mode where a single LLM response keeps emitting
    the same short Chinese / English paragraph over and over. This is a
    decoding-layer pathology (the sampler walked into a low-entropy
    loop); none of our agent-loop budgets stop it because the model is
    still inside ONE response. The terminal de-duplicator runs only
    *after* ``stream_chat`` returns, by which time the UI has already
    received thousands of duplicated bytes via ``on_delta`` — that is
    exactly what the user sees as "circling forever".

    Strategy:

    1. Accumulate a sliding tail of the assistant's already-streamed
       text (last ``window_chars`` characters).
    2. Whenever a new delta lands, normalise the recent tail to a list
       of "blocks" (newline-separated, optionally trimmed) and detect
       whether the last K blocks are byte-identical (after normalisation)
       to the K blocks before them.
    3. Once the same block has been emitted ``warn_repeats`` times in a
       row, raise a "warn" verdict — caller logs a warning, still
       forwards bytes (some prose really does repeat once or twice in
       legitimate output).
    4. Once it hits ``truncate_repeats``, raise a "truncate" verdict —
       caller drops the delta, replaces it with a one-shot
       ``[已检测到模型复读，已折叠后续重复输出]`` notice, and stops
       forwarding more bytes.
    5. Once it hits ``hard_stop_repeats``, raise a "stop" verdict — the
       caller treats the round as terminated and the agent loop exits.

    The guard is intentionally cheap (string slicing + dict membership)
    so it can run on every delta without measurable latency cost.
    """

    # Block size we hash on. Smaller blocks catch shorter loops but cost
    # more comparisons; the values below were chosen by inspecting the
    # user-reported screenshots — those loops were 60-200 char paragraphs
    # repeated end-to-end.
    _MIN_BLOCK_CHARS = 24
    _MAX_BLOCK_CHARS = 600

    def __init__(
        self,
        *,
        window_chars: int = 4096,
        # Loom22 §11.1 soft-first: warn once, hard action at ~2× warn.
        # truncate folds UI spam; hard_stop ends the round.
        warn_repeats: int = 3,
        truncate_repeats: int = 6,
        hard_stop_repeats: int = 8,
    ) -> None:
        self._buf = ""
        self._window_chars = int(window_chars)
        self._warn_repeats = int(warn_repeats)
        self._truncate_repeats = int(truncate_repeats)
        self._hard_stop_repeats = int(hard_stop_repeats)
        self._last_state: str = "ok"
        self._last_block_key: str = ""
        self._last_block_text: str = ""
        self._last_block_repeats: int = 0
        self._truncate_emitted = False

    def state(self) -> str:
        return self._last_state

    def last_repeat_block(self) -> tuple[str, int]:
        return self._last_block_text, self._last_block_repeats

    def feed(self, chunk: str) -> dict[str, Any]:
        """Consume a streamed delta and return a verdict dict.

        Verdict keys:
        - ``state``: "ok" / "warn" / "truncate" / "stop"
        - ``forward_text``: bytes the caller should still forward to the
          UI (empty when truncating or stopping).
        - ``notice``: optional one-shot notice text to forward instead
          (only set on the first transition into "truncate").
        - ``repeats``: how many times the offending block has now
          appeared back-to-back.
        - ``block_preview``: short preview of the repeated block for
          logging.
        """

        if not chunk:
            return {"state": "ok", "forward_text": "", "repeats": 0}
        self._buf += chunk
        if len(self._buf) > self._window_chars * 2:
            # Keep the buffer bounded; the trailing window is what we
            # actually compare against.
            self._buf = self._buf[-self._window_chars * 2 :]

        analysis = self._analyse_tail()
        repeats = analysis["repeats"]
        block_text = analysis["block_text"]
        block_key = analysis["block_key"]

        if not block_key or repeats < 2:
            # No repetition. Reset our progressive counters when the
            # current trailing block changes character class so that
            # legitimate prose doesn't slowly drift into a false
            # positive.
            if block_key and block_key != self._last_block_key:
                self._last_block_key = block_key
                self._last_block_text = block_text
                self._last_block_repeats = 1
            self._last_state = "ok"
            return {
                "state": "ok",
                "forward_text": chunk,
                "repeats": repeats,
                "block_preview": block_text[:80],
            }

        self._last_block_key = block_key
        self._last_block_text = block_text
        self._last_block_repeats = repeats

        if repeats >= self._hard_stop_repeats:
            self._last_state = "stop"
            return {
                "state": "stop",
                "forward_text": "",
                "repeats": repeats,
                "block_preview": block_text[:80],
            }
        if repeats >= self._truncate_repeats:
            if not self._truncate_emitted:
                self._truncate_emitted = True
                self._last_state = "truncate"
                notice = (
                    "\n\n[已检测到模型复读，已折叠后续重复输出]"
                    " (block_repeats="
                    f"{repeats})\n"
                )
                return {
                    "state": "truncate",
                    "forward_text": "",
                    "notice": notice,
                    "repeats": repeats,
                    "block_preview": block_text[:80],
                }
            self._last_state = "truncate"
            return {
                "state": "truncate",
                "forward_text": "",
                "repeats": repeats,
                "block_preview": block_text[:80],
            }
        if repeats >= self._warn_repeats:
            self._last_state = "warn"
            return {
                "state": "warn",
                "forward_text": chunk,
                "repeats": repeats,
                "block_preview": block_text[:80],
            }
        self._last_state = "ok"
        return {
            "state": "ok",
            "forward_text": chunk,
            "repeats": repeats,
            "block_preview": block_text[:80],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _digit_ratio(text: str) -> float:
        if not text:
            return 0.0
        digits = sum(1 for ch in text if ch.isdigit())
        return digits / max(1, len(text))

    def _blocks_equivalent(self, a: str, b: str, *, key: str) -> bool:
        """Loom22-aligned match: structured/numeric content needs raw equality.

        Progressive tables (九九表, CVE lists, hex dumps) share punctuation
        skeletons; after soft normalisation they can look identical even when
        each line advances. For high digit density, require whitespace-folded
        raw equality so we only trip on true verbatim loops.
        """
        if _repeat_key(a) != key or _repeat_key(b) != key:
            return False
        if self._digit_ratio(a) >= 0.12 or self._digit_ratio(b) >= 0.12:
            fold = lambda s: re.sub(r"\s+", "", s or "")
            return fold(a) == fold(b)
        return True

    def _analyse_tail(self) -> dict[str, Any]:
        tail = self._buf[-self._window_chars :]
        if len(tail) < self._MIN_BLOCK_CHARS * 2:
            return {"repeats": 0, "block_text": "", "block_key": ""}
        best = {"repeats": 0, "block_text": "", "block_key": ""}
        # Try several block sizes; pick the largest one that still
        # produces a back-to-back run of at least 2. Larger blocks =
        # higher confidence that we are looking at a real loop rather
        # than a coincidence (e.g. "Yes." "Yes." in legitimate code).
        for block_size in self._candidate_block_sizes(tail):
            block_text = tail[-block_size:]
            key = _repeat_key(block_text)
            if not key or len(key) < 6:
                continue
            # How many adjacent blocks of this size end the buffer and
            # share the same normalised key?
            repeats = 1
            pos = len(tail) - block_size
            while pos - block_size >= 0:
                prev = tail[pos - block_size : pos]
                if not self._blocks_equivalent(prev, block_text, key=key):
                    break
                repeats += 1
                pos -= block_size
                if repeats > 32:
                    break
            if repeats > best["repeats"]:
                best = {
                    "repeats": repeats,
                    "block_text": block_text.strip(),
                    "block_key": key,
                }
        return best

    def _candidate_block_sizes(self, tail: str) -> list[int]:
        """Return a small set of block sizes worth probing.

        We start from natural cut points (newlines) so the most common
        case — a paragraph repeated verbatim — is detected in O(K)
        instead of O(window * size). Falls back to fixed sizes for
        streams without newlines.
        """

        sizes: list[int] = []
        seen: set[int] = set()
        # 1) Boundaries derived from the last ~12 newlines.
        newline_positions = []
        idx = len(tail)
        for _ in range(12):
            j = tail.rfind("\n", 0, idx)
            if j < 0:
                break
            newline_positions.append(j)
            idx = j
        for j in newline_positions:
            size = len(tail) - 1 - j
            if size < self._MIN_BLOCK_CHARS or size > self._MAX_BLOCK_CHARS:
                continue
            if size in seen:
                continue
            seen.add(size)
            sizes.append(size)
        # 2) Fixed fallbacks so paragraph-less streams still get caught.
        for size in (40, 80, 120, 200, 320, 480):
            if self._MIN_BLOCK_CHARS <= size <= self._MAX_BLOCK_CHARS and size not in seen:
                sizes.append(size)
                seen.add(size)
        return sizes


# Tools that modify state — require user confirmation
_WRITE_TOOLS = {
    "create_scan", "cancel_task", "delete_task", "rescan_task",
    "cron_grant_high_risk",
    "start_proxy", "stop_proxy", "install_proxy_ca", "uninstall_proxy_ca",
    "update_settings", "launch_chrome", "runtime_install",
    "create_poc_page", "open_poc_page", "sandbox_write_file", "sandbox_patch_file",
    "codebase_write_file", "codebase_patch_file", "codebase_delete_file",
    "write_text_file", "patch_text_file",
    "sandbox_delete_file", "sandbox_reset_files", "sqlmap_install_update",
    "browser_install_engine", "reverse_toolchain_install",
    "mcp_server_add", "mcp_server_delete",
    # call_tool / read_resource can write/delete on the remote server.
    # They go through per-server policy + the existing confirmation flow.
    "mcp_server_call_tool", "mcp_server_read_resource",
    # browser_save_wasm: drives the user's real browser AND writes binary
    # to disk. Treat like sandbox_write_file for the confirm flow.
    "browser_save_wasm",
    # Attach mode: launching the user's real browser kills any running
    # instance and reroutes every later browser_* call through their real
    # session — both writes from a security perspective.
    "browser_launch_for_attach", "browser_attach_existing", "browser_detach",
    # ReverseJob writes: create/update/trash + artifact/finding/reproduce
    # add/remove + recipe_apply (which bulk-adds hooks).
    "reverse_job_create", "reverse_job_update", "reverse_job_trash",
    "reverse_artifact_add", "reverse_artifact_remove",
    "reverse_job_link_proxy_flow",
    # binary_patch writes a patched executable copy to disk (destructive).
    "binary_patch",
    "reverse_finding_add", "reverse_finding_remove",
    "reverse_reproduce_add", "reverse_recipe_apply",
    "reverse_oracle_add",
    # Frida dynamic instrumentation — modifies/spawns target processes.
    "frida_attach", "frida_spawn", "frida_run_script", "frida_session_stop",
    # Repository audit writes source snippets and evidence into Case storage.
    "codebase_evidence_add",
    # create_python_poc materialises files in the session sandbox.
    "create_python_poc",
    "export_task_report", "export_workspace_bundle",
    # User-level todos (persistent cross-session state).
    "user_todo_create", "user_todo_update", "user_todo_complete", "user_todo_delete",
    "user_todo_bulk_delete",
    # User habits profile writes.
    "user_profile_apply_update",
    # Process / shell / runtime side-effects (were CONFIRM-only; Loom §11.2
    # kind=mutate so exact-dedupe and mutating_repetition both see them).
    "run_python_script", "run_shell_script",
    "run_python_snippet", "run_shell_snippet",
    "run_host_python_script", "run_host_python_snippet",
    "shell_session_start", "shell_session_exec", "shell_session_write", "shell_session_stop",
    "sandbox_pip_install",
    "run_node_script", "run_js_function",
    "js_runtime_create", "js_runtime_load", "js_runtime_eval",
    "js_runtime_call", "js_runtime_hook", "js_runtime_patch",
    "js_runtime_instrument", "js_runtime_restore", "js_runtime_stop",
    "desktop_notify", "desktop_launch_app",
    "desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey",
    "desktop_focus_window",
    "desktop_uia_action",
    # External side effects: proactive delivery to paired communication peers.
    "channel_send_message", "channel_send_file",
    "sandbox_open_file", "sandbox_reveal_in_folder",
    "codebase_open_file", "codebase_reveal_in_folder",
    "adb_install", "adb_push", "adb_pull",
    "sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan",
    # Proxy decryption rules persist keys (and possibly JS) in user settings.
    "proxy_decrypt_rule_save", "proxy_decrypt_script_rule_save", "proxy_decrypt_rule_delete",
    # Active rewrite rules edit/block live proxied traffic.
    "proxy_rewrite_rule_save", "proxy_rewrite_rule_delete",
    # Breakpoints park live requests; resolving forwards/edits/drops them.
    "proxy_breakpoint_config", "proxy_breakpoint_resolve",
    # Standing teams: creating one / recruiting or commanding a child-Agent member.
    "team_create", "team_update", "team_delete",
    "team_add_member", "team_message_send",
    "team_member_update", "team_member_send_input", "team_member_control",
}

_CONFIRM_TOOLS = _WRITE_TOOLS | {
    "cron_upsert", "cron_set_enabled", "cron_delete", "local_attack_surface_snapshot",
    "http_probe", "payload_probe", "browser_probe", "browser_evaluate", "ping_host", "dns_lookup",
    "trace_route", "http_curl", "http_request_impersonate", "whois_lookup", "nmap_scan", "browser_trace",
    "network_discover_hosts", "network_scan_ports", "network_fingerprint_services", "network_profile_host",
    "pentest_verify_finding", "post_exploit_lab_generate", "weak_credential_check",
    "browser_trace_crypto", "browser_trace_form", "browser_instrument_click",
    "graphql_probe", "graphql_batch_probe", "websocket_handshake_probe", "websocket_connect_probe",
    "websocket_send_receive", "websocket_authz_probe",
    "browser_hook_function", "browser_hook_property",
    "browser_hook_remove", "browser_hook_drain", "browser_hook_reattach",
    "captcha_runtime_trace", "js_fetch_asset", "run_python_script", "run_shell_script",
    "run_python_snippet", "run_shell_snippet", "sandbox_pip_install",
    "run_host_python_script", "run_host_python_snippet",
    "shell_session_start", "shell_session_exec", "shell_session_write", "shell_session_stop",
    "sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan", "cors_exploit_verify",
    "pcap_summary", "pcap_filter", "pcap_http_extract",
    # Document parse may upload sandbox files to MinerU when enabled.
    "parse_document",
    "syft_run_inventory", "trivy_run_scan", "osv_run_scan",
    "trivy_cloud_scan", "prowler_cloud_scan", "kubectl_readonly_run",
    "trivy_iac_scan", "checkov_run_scan", "appium_status", "ad_certipy_find_run",
    "wireless_wlan_inventory",
    # Heavy JS deobfuscation / unpacking spawns a long-running Node
    # subprocess and can consume seconds of CPU on big bundles. Require
    # confirmation so the user knows it's being launched.
    "js_deobfuscate_heavy", "js_unpack_bundle",
    # Running arbitrary JS in managed Node — the source code is
    # user-supplied (the AI may have just deobfuscated a third-party
    # bundle) so we want explicit confirmation, the same way we treat
    # ``run_python_script`` / ``run_shell_script``.
    "run_node_script", "run_js_function",
    "js_runtime_create", "js_runtime_load", "js_runtime_eval",
    "js_runtime_call", "js_runtime_hook", "js_runtime_patch",
    "js_runtime_instrument", "js_runtime_restore", "js_runtime_stop",
    "js_runtime_sample_profile",
    "python_reproducer_verify",
    "frida_attach", "frida_spawn", "frida_run_script", "frida_session_stop",
    # Reading the user's real tab content is sensitive even though it's
    # technically read-only: it may expose cookies, tokens, drafts.
    "browser_use_tab", "browser_open_tab", "browser_screenshot", "browser_close_owned_tabs",
    # Opening files / folders in the user's default application launches a
    # GUI window on their desktop. Sandbox-scoped, but still a side-effect
    # outside our own UI, so require confirmation (auto-approvable).
    "sandbox_open_file", "sandbox_reveal_in_folder",
    "codebase_open_file", "codebase_reveal_in_folder",
    "adb_install", "adb_push", "adb_pull",
    "reverse_job_link_proxy_flow",
    "export_task_report", "export_workspace_bundle",
    # Desktop automation — sensitive read + input side-effects.
    "desktop_observe", "desktop_notify", "desktop_launch_app",
    "desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey",
    "desktop_focus_window",
    "desktop_uia_find", "desktop_uia_action",
    "user_todo_delete", "user_todo_bulk_delete",
    "user_profile_apply_update",
    "user_profile_suggest_update",
    # Per-server MCP discovery is read-only and bounded by saved server
    # configuration, so it must not block autonomous capability discovery.
    # Keep config mutations and actual remote calls/resources confirmed.
    "mcp_server_add", "mcp_server_delete",
    "mcp_server_call_tool", "mcp_server_read_resource",
    "mcp_stdio_health", "mcp_stdio_list_tools", "mcp_stdio_call_tool", "mcp_stdio_list_resources", "mcp_stdio_read_resource",
}

# Tools that require a typed confirm_label in the executor; the session
# injects it after the user approves (or when the tool is session auto-approved).
_CONFIRM_LABEL_AFTER_APPROVAL: dict[str, str] = {
    "user_profile_apply_update": "APPLY_USER_PROFILE",
    "user_todo_bulk_delete": "DELETE_USER_TODOS",
    "sandbox_reset_files": "RESET",
    "binary_patch": "PATCH_BINARY",
}


def prepare_tool_arguments_for_dispatch(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    user_approved: bool = False,
    session_auto_approved: bool = False,
) -> dict[str, Any]:
    """Merge executor confirm_label tokens after explicit user approval."""
    args = dict(arguments or {})
    token = _CONFIRM_LABEL_AFTER_APPROVAL.get(name)
    if token and (user_approved or session_auto_approved):
        args["confirm_label"] = token
    return args


# Reserved key carrying "this tool call's arguments were unparseable" from the
# parse site down to the single dispatch choke point. Never sent to a tool.
TOOL_ARGUMENTS_ERROR_KEY = "__argus_arguments_error__"

_JSON_VALID_ESCAPES = frozenset('"\\/bfnrtu')
_JSON_CONTROL_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r"}


def repair_llm_json(text: str) -> str | None:
    """Repair the two JSON defects models hit constantly in tool arguments.

    Windows paths and regexes come through as lone backslashes (``C:\\Users``
    → ``Invalid \\escape``) and multi-line script bodies arrive with raw
    newlines inside a string (``Invalid control character``). Both are
    mechanically fixable without guessing intent, so we escape them instead of
    throwing the whole call away.

    Returns ``None`` when the payload is truncated mid-string, which cannot be
    repaired without inventing content.
    """
    if not text:
        return None
    out: list[str] = []
    in_string = False
    pending_escape = False
    for ch in text:
        if pending_escape:
            out.append("\\" if ch in _JSON_VALID_ESCAPES else "\\\\")
            out.append(ch)
            pending_escape = False
            continue
        if in_string:
            if ch == "\\":
                pending_escape = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            code = ord(ch)
            if code < 0x20:
                out.append(_JSON_CONTROL_ESCAPES.get(code, f"\\u{code:04x}"))
                continue
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
        out.append(ch)
    if in_string or pending_escape:
        return None
    return "".join(out)


def tool_arguments_error(args: dict[str, Any] | None) -> str:
    """Return the parse error recorded for a tool call, or an empty string."""
    if not isinstance(args, dict):
        return ""
    return str(args.get(TOOL_ARGUMENTS_ERROR_KEY) or "")


def parse_tool_call_arguments(raw: Any) -> dict[str, Any]:
    """Parse a model-supplied tool ``arguments`` payload.

    On unrecoverable failure the result carries :data:`TOOL_ARGUMENTS_ERROR_KEY`
    instead of being silently emptied — dispatching ``{}`` used to run tools
    with all-default arguments, so a broken ``run_shell_snippet`` call executed
    an empty script and reported success with no output.
    """
    if isinstance(raw, dict):
        return dict(raw)
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return {}
    for candidate in (text, repair_llm_json(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        return {TOOL_ARGUMENTS_ERROR_KEY: "arguments must be a JSON object"}
    return {TOOL_ARGUMENTS_ERROR_KEY: "arguments were not valid JSON"}


_NO_AUTO_APPROVE_TOOLS = {
    "cron_grant_high_risk",
    "run_python_script", "run_shell_script", "run_python_snippet", "run_shell_snippet",
    "codebase_run_python_script", "codebase_run_python_snippet",
    "codebase_run_shell_script", "codebase_run_shell_snippet",
    "sandbox_pip_install", "runtime_install",
    "run_host_python_script", "run_host_python_snippet",
    "shell_session_start", "shell_session_exec", "shell_session_write", "shell_session_stop",
    "sqli_probe_param", "sqli_boolean_diff", "sqli_time_probe", "idor_compare_request",
    "role_matrix_probe", "upload_surface_discover", "upload_verify_access",
    "ssrf_probe_url_param", "cors_matrix_probe", "cors_exploit_verify", "http_desync_probe",
    "cache_poison_probe", "raw_http_send", "sqlmap_run_scan", "nuclei_run_scan", "ffuf_run_scan",
    # Same caution as Python/shell: the JS source is opaque to us and
    # may have been just deobfuscated; user should always see the call
    # before it executes.
    "run_node_script", "run_js_function",
    "js_runtime_create", "js_runtime_load", "js_runtime_eval",
    "js_runtime_call", "js_runtime_hook", "js_runtime_patch",
    "js_runtime_instrument", "js_runtime_restore", "python_reproducer_verify",
    "js_runtime_sample_profile",
    "pentest_verify_finding",
    "post_exploit_lab_generate",
    "weak_credential_check",
    "websocket_send_receive", "websocket_authz_probe",
    "export_workspace_bundle",
    # mcp_server_add: persistently changes how external servers are launched.
    # mcp_server_delete: persistently removes saved server (still confirmable,
    #   but no silent auto-allow even after one click).
    # mcp_stdio_call_tool: bypasses any saved per-server policy.
    "mcp_server_add", "mcp_server_delete", "mcp_stdio_call_tool",
    "prowler_cloud_scan", "kubectl_readonly_run", "ad_certipy_find_run",
    # Desktop input: never bypass confirmation even with global auto-allow.
    # desktop_observe is extension-tier; default always-allow comes from settings.
    "desktop_mouse_move", "desktop_click", "desktop_type", "desktop_hotkey",
    "desktop_focus_window",
    "desktop_uia_action",
    "user_todo_bulk_delete",
    "user_profile_apply_update",
    # OS trust-store mutations — must never bypass confirmation even with
    # session/global auto-allow (Loom22 §21.7 / AGENTS.md §6.4).
    "install_proxy_ca", "uninstall_proxy_ca",
    # Patching an executable is destructive — always double-confirm.
    "binary_patch",
    # Launching an independent local Agent hands a third-party process a
    # workspace plus an MCP capability projection; every spawn is explicit.
    "spawn_external_agent",
    # Installs JS that runs in managed Node on every matching captured flow.
    "proxy_decrypt_script_rule_save",
    # Installs a rule that actively edits or blocks live proxied traffic.
    "proxy_rewrite_rule_save",
    # Enables interactive breakpoints that hold live requests.
    "proxy_breakpoint_config",
}

# Frida attach/spawn remain in ``_NO_AUTO_APPROVE_TOOLS`` (settings permanent
# allow still needs double confirm), but session "allow all" can opt in via
# ``include_reverse_dynamic`` without bundling sandbox scripts / SQLi probes.
_REVERSE_DYNAMIC_TOOLS = frozenset({
    "frida_attach", "frida_spawn", "frida_run_script", "frida_session_stop",
})
_NO_AUTO_APPROVE_TOOLS = _NO_AUTO_APPROVE_TOOLS | _REVERSE_DYNAMIC_TOOLS

# INVARIANT (AGENTS.md §6.4): anything that must NEVER be silently auto-approved
# must, at minimum, be confirmable. The confirmation gate keys off
# ``_CONFIRM_TOOLS`` alone (session_chat_loop._dispatch path), so a tool that is
# in ``_NO_AUTO_APPROVE_TOOLS`` but missing from ``_CONFIRM_TOOLS`` would skip
# the prompt entirely and execute on first call. Several active attack probes
# (sqli_probe_param, ssrf_probe_url_param, raw_http_send, http_desync_probe,
# cache_poison_probe, cors_matrix_probe, idor_compare_request, role_matrix_probe,
# upload_surface_discover, upload_verify_access, sqli_boolean_diff,
# sqli_time_probe) hit exactly that gap. Enforce the subset relation here so the
# two sets can never drift apart again.
_CONFIRM_TOOLS = _CONFIRM_TOOLS | _NO_AUTO_APPROVE_TOOLS

# Synthetic name for a tool that belongs to an external ACP agent instead of to
# ArgusAI. The sets above are positive lists of tools we registered ourselves,
# so an outside agent's ``Bash`` or ``Edit`` matches nothing and would sail
# through unasked. Every such call is routed under this one name, which makes it
# confirmable by construction; the agent and the ACP tool kind ride along in the
# arguments so the per-session auto-allow key can stay narrow.
EXTERNAL_AGENT_TOOL = "external_agent_tool"

# Tools whose return value is INHERENTLY time-dependent or queries live
# server/session state. Dedup must skip these — re-issuing the same call a
# few seconds later is exactly how the AI is supposed to poll for new
# events / task progress / browser readiness. Returning a dedup-skip for
# these makes the AI think the channel is broken and pivot pointlessly.
_POLLING_TOOLS = {
    "oob_check_events", "oob_status",
    "browser_status", "browser_attach_status", "browser_list_tabs",
    "session_sandbox_status", "sandbox_status",
    "desktop_action_session_status",
    "js_runtime_status", "js_runtime_drain", "js_runtime_logs",
    "js_runtime_sources", "js_runtime_patches", "js_runtime_hooks",
    "reverse_oracle_list", "reverse_oracle_read",
    "frida_session_list",
    "shell_session_list", "shell_session_read",
    "list_tasks", "get_task_detail", "get_task_findings",
    "http_impersonate_status",
    "background_job_status", "background_job_list",
}


# Presentation tools render a model-authored UI block (a card / table / file
# view) and have NO information for the model to react to — the model already
# authored the content. Treating them as a first-class category (rather than
# sniffing for a ``block`` key or a shown-card signature ad hoc) lets the two
# isolation rules live in one place:
#
#   * Data isolation: their result is NOT echoed back into the prompt — only a
#     compact ack — so the full block never re-rides the context window
#     (see ``_tool_result_to_prompt``).
#   * Control-flow isolation: a turn whose tool calls are *only* presentation
#     tools is terminal. The model has finished and just rendered the answer;
#     the runtime must not grant a continuation round for it
#     (see ``_should_probe_toolless_continuation``).
#
# This mirrors how mature agent harnesses treat presentation as a property of
# the output channel, not as an action the model has to follow up on.
_PRESENTATION_TOOLS = frozenset({
    "show_structured_card",
    "show_data_table",
    "show_file_resource_view",
})


# Agent control-flow tools (Loom22 §11.2 "control"): todo state + user-decision
# stops. They neither mutate external state nor return information for the model
# to react to — they drive the loop itself, so loop guards must not treat a
# round of them as "work" or as a repetition to break.
_CONTROL_TOOLS = frozenset({
    "set_todos", "add_todos", "update_todo", "complete_todo_with_refs",
    "create_validation_todo", "list_todos", "clear_todos", "assessment_plan_snapshot",
    "stop_for_user_decision", "stop_for_user_decision_batch",
    "run_subagent", "spawn_agent", "spawn_team", "spawn_external_agent",
    "send_agent_input", "pause_agent", "resume_agent", "close_agent",
    "background_job_cancel",
})


# Loom22 §11 — args that mean "I want a fresh result"; presence disables
# exact-dedupe even when the rest of the call is identical.
FRESHNESS_KEYS = frozenset({
    "force",
    "nonce",
    "since",
    "after_id",
    "wait_seconds",
    "fresh_context",
})


def tool_kind(name: str) -> str:
    """Single source of truth for a tool's behavioural category (Loom22 §11.2).

    Returns one of ``presentation | control | polling | mutating | idempotent``.

    The scattered fragment sets (``_PRESENTATION_TOOLS`` / ``_CONTROL_TOOLS`` /
    ``_POLLING_TOOLS`` / ``_WRITE_TOOLS``) are pieces of ONE concept; deriving
    the category here keeps every dedup / loop-guard / continuation decision
    reading the same table. Loom22 §11.2's core lesson: an incomplete table
    silently disables guards, so this must stay the only place kinds are
    decided (``tests/core/test_tool_taxonomy.py`` asserts each fragment maps to
    its expected kind).

    Precedence matters — a tool listed in several fragments is classified by its
    dominant role: presentation/control are pure loop-plumbing; polling is a
    time-dependent read (dedup-exempt, no-progress via result hash); mutating
    changes external state; everything else is treated as read-only/idempotent.
    """
    n = str(name or "")
    if n in _PRESENTATION_TOOLS:
        return "presentation"
    if n in _CONTROL_TOOLS:
        return "control"
    if n in _POLLING_TOOLS:
        return "polling"
    # Exact-source channel replies change remote state, but intentionally skip
    # proactive-send confirmation: the paired user authored this very turn.
    # Keep them mutating so repeated identical bubbles are never deduplicated.
    if n == "channel_reply":
        return "mutating"
    if n in _WRITE_TOOLS:
        return "mutating"
    return "idempotent"


def should_dedupe_tool_call(
    tool: Any | None,
    args: dict[str, Any] | None,
    *,
    name: str | None = None,
) -> bool:
    """Whether this call is eligible for Loom22 §11 exact-dedupe.

    Exact dedupe is short-loop debounce for identical idempotent reads — not a
    result cache. Polling / mutating / control / presentation never exact-skip;
    freshness keys opt out even when name+args otherwise match.
    """
    if tool is None:
        # Unknown tool: do not invent idempotency by skipping execution.
        return False
    kind = str(getattr(tool, "kind", None) or "").strip()
    if not kind:
        kind = tool_kind(str(getattr(tool, "name", None) or name or ""))
    # Loom uses poll/wait/mutate; our taxonomy uses polling/mutating.
    if kind in {"poll", "polling", "wait", "mutate", "mutating", "control", "presentation"}:
        return False
    if kind != "idempotent":
        return False
    if not bool(getattr(tool, "dedupe", False)):
        return False
    payload = args or {}
    if any(k in payload for k in FRESHNESS_KEYS):
        return False
    return True
