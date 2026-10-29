"""Ephemeral sandbox selection explain — streams without persisting chat history."""

from __future__ import annotations

import json
from typing import Any

from argusai.core.assistant.ui_protocol import (
    normalize_selection_reference,
    normalize_stream_event,
    selection_reference_to_prompt_text,
)

from .assistant_common import *


_EXPLAIN_SYSTEM = """You are the argusai in-app Q&A helper for sandbox file previews.
The user selected a code or text fragment while reviewing security-audit artifacts and asked a focused question about it.
Answer in clear Chinese unless the user wrote in another language.
Be concise (roughly 3–8 short paragraphs or bullet groups). Do not invent file paths or claim actions were executed.
This is a one-shot question: do not ask to run tools or continue in the main assistant thread."""

_DEFAULT_QUESTION = (
    "请解释这段选中内容在做什么、关键机制是什么，以及与安全审计相关的要点。"
)


def build_explain_messages(
    selection_reference: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    surrounding: str = "",
    question: str = "",
) -> list[dict[str, str]]:
    """Build OpenAI-style messages for a single explain turn (testable)."""
    ref = normalize_selection_reference(selection_reference)
    if not ref or ref.get("kind") != "file_selection":
        raise ValueError("file_selection selection_reference is required")
    parts = [
        "## Selected fragment",
        selection_reference_to_prompt_text(ref),
    ]
    if surrounding:
        parts.extend(["## Surrounding file context", "```text", surrounding[:12000], "```"])
    if context:
        parts.extend([
            "## UI context",
            "```json",
            json.dumps(context, ensure_ascii=False, indent=2, default=str)[:8000],
            "```",
        ])
    user_q = str(question or "").strip() or _DEFAULT_QUESTION
    parts.extend(["## User question", user_q])
    return [
        {"role": "system", "content": _EXPLAIN_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


class AssistantExplainMixin:
    def cancel_assistant_stream(self, run_id: str) -> dict[str, Any]:
        """Cancel any in-flight stream (explain or chat) by ``run_id``."""
        rid = str(run_id or "").strip()
        if not rid:
            return {"ok": False, "error": "run_id is required"}
        stream = self._assistant_streams.get(rid)
        if not stream:
            return {"ok": False, "error": "stream not found"}
        lock = stream.get("lock")
        if lock:
            with lock:
                stream["done"] = True
                stream["events"].append(normalize_stream_event({
                    "type": "error",
                    "error": "cancelled",
                    "kind": "run_error",
                    "data": {"message": "cancelled"},
                }))
        return {"ok": True}

    def start_assistant_explain(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start an ephemeral explain stream (does not write session history)."""
        import threading
        import uuid

        selection_reference = params.get("selection_reference")
        if not selection_reference:
            return {"ok": False, "error": "selection_reference is required"}
        try:
            session_id = self._validate_session_id(params.get("session_id") or "default")
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        run_id = uuid.uuid4().hex
        stream_lock = threading.Lock()
        self._assistant_streams[run_id] = {
            "events": [],
            "done": False,
            "lock": stream_lock,
            "session_id": session_id,
            "ephemeral": True,
        }

        def push(event: dict[str, Any]) -> None:
            event = normalize_stream_event(event)
            stream = self._assistant_streams.get(run_id)
            if stream is not None:
                with stream["lock"]:
                    stream["events"].append(event)

        def on_delta(text: str) -> None:
            if text:
                push({
                    "type": "delta",
                    "text": text,
                    "kind": "text_chunk",
                    "data": {"text": text},
                })

        def on_status(text: str) -> None:
            if text:
                push({
                    "type": "status",
                    "text": text,
                    "kind": "stage",
                    "data": {"label": text},
                })

        push({
            "type": "status",
            "text": "正在回答提问...",
            "kind": "stage",
            "data": {"label": "正在回答提问..."},
        })

        def worker() -> None:
            try:
                result = self._run_assistant_explain(
                    {
                        **params,
                        "session_id": session_id,
                        "selection_reference": selection_reference,
                    },
                    on_delta=on_delta,
                    on_status=on_status,
                )
                push({
                    "type": "done",
                    "result": result,
                    "kind": "run_done",
                    "data": {"status": "ok" if result.get("ok") else "error"},
                })
            except Exception as e:
                debug_log(f"assistant_explain_error run_id={run_id} error={e}")
                push({
                    "type": "error",
                    "error": str(e),
                    "kind": "run_error",
                    "data": {"message": str(e)},
                })
            finally:
                stream = self._assistant_streams.get(run_id)
                if stream is not None:
                    stream["done"] = True

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "run_id": run_id}

    def _run_assistant_explain(
        self,
        params: dict[str, Any],
        on_delta: Any = None,
        on_status: Any = None,
    ) -> dict[str, Any]:
        import asyncio
        import concurrent.futures

        selection_reference = params.get("selection_reference")
        try:
            messages = build_explain_messages(
                selection_reference,
                context=params.get("context") or {},
                surrounding=self._explain_surrounding_text(
                    str(params.get("session_id") or ""),
                    selection_reference,
                ),
                question=str(params.get("question") or ""),
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if on_status:
            on_status("正在连接 AI...")

        app_settings = self.settings_mgr.get()
        if not app_settings.ai_enabled or not app_settings.ai_endpoints:
            return {"ok": False, "error": "AI 未配置或未启用"}

        from argusai.core.ai import create_provider

        provider = create_provider(endpoints=app_settings.ai_endpoints)
        thinking = params.get("thinking_enabled")

        async def _stream() -> str:
            parts: list[str] = []

            async def _on_delta(chunk: str) -> None:
                if not chunk:
                    return
                parts.append(chunk)
                if on_delta:
                    maybe = on_delta(chunk)
                    if hasattr(maybe, "__await__"):
                        await maybe

            try:
                await provider.stream_chat(
                    messages,
                    tools=None,
                    temperature=0.25,
                    on_delta=_on_delta,
                    thinking_enabled=thinking,
                )
                return "".join(parts)
            finally:
                try:
                    await provider.close()
                except Exception:
                    pass

        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                text = pool.submit(asyncio.run, _stream()).result()
        except RuntimeError:
            text = asyncio.run(_stream())

        return {"ok": True, "text": text or ""}

    def _explain_surrounding_text(
        self,
        session_id: str,
        selection_reference: dict[str, Any],
    ) -> str:
        """Optional extra file context around the selection (not persisted)."""
        ref = normalize_selection_reference(selection_reference)
        if not ref:
            return ""
        path = str(ref.get("file_path") or ref.get("file_name") or "").strip()
        if not path or not session_id:
            return ""
        try:
            session_id = self._validate_session_id(session_id)
        except ValueError:
            return ""
        try:
            from argusai.core.workspace.session_sandbox import read_file, sandbox_dir_for_session

            root = sandbox_dir_for_session(self._sessions_dir, session_id)
            payload = read_file(root, path, 24000, as_base64=False)
            if not payload.get("ok"):
                return ""
            content = str(payload.get("content") or "")
            if not content:
                return ""
            lines = content.splitlines()
            if not lines:
                return content[:12000]
            start_line = int(ref.get("start_line") or 1)
            end_line = int(ref.get("end_line") or start_line)
            pad = 12
            lo = max(1, start_line - pad)
            hi = min(len(lines), end_line + pad)
            snippet = "\n".join(lines[lo - 1:hi])
            header = f"# File: {path} (lines {lo}-{hi} of {len(lines)})\n"
            return header + snippet
        except Exception:
            return ""
