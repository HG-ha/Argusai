"""One ACP assistant turn with normalized update callbacks."""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import acp

from .acp_config import normalize_config_options
from .acp_runtime import ACP_PERMISSION_HANDLER, ACPAgentProcess
from .models import AgentRuntimeConfig, SessionRuntimeBinding

ACP_UPDATE_CALLBACK = Callable[[dict[str, Any]], None]

# Some agents fabricate a placeholder assistant turn when the upstream model
# returns empty content, then forward it as a normal message chunk. Claude Code
# emits "No response requested." this way, often several times in a row.
SYNTHETIC_AGENT_TEXTS = (
    "No response requested.",
    "No response requested",
)


def strip_synthetic_agent_text(text: str) -> str:
    """Drop harness-fabricated placeholder text from a whole ACP agent message.

    Applied to the joined turn rather than to each chunk, so a sentinel split
    across two ``agent_message_chunk`` frames is still caught.
    """
    cleaned = text
    for sentinel in SYNTHETIC_AGENT_TEXTS:
        if sentinel in cleaned:
            cleaned = cleaned.replace(sentinel, "")
    return cleaned.strip() if cleaned != text else text


def is_synthetic_agent_chunk(text: str) -> bool:
    """True when a streamed chunk is nothing but a placeholder turn.

    Deliberately stricter than :func:`strip_synthetic_agent_text`: a chunk that
    merely mentions the phrase — a quoted transcript, say — is real content and
    must reach the user unedited.
    """
    clean = str(text or "").strip()
    if not clean:
        return False
    for sentinel in SYNTHETIC_AGENT_TEXTS:
        while clean.startswith(sentinel):
            clean = clean[len(sentinel):].strip()
    return not clean


@dataclass
class ACPTurnResult:
    ok: bool
    runtime_id: str
    acp_session_id: str = ""
    text: str = ""
    reasoning: str = ""
    stop_reason: str = ""
    updates: list[dict[str, Any]] = field(default_factory=list)
    agent_capabilities: dict[str, Any] = field(default_factory=dict)
    prompt_capabilities: dict[str, bool] = field(default_factory=dict)
    config_options: list[dict[str, Any]] = field(default_factory=list)
    session_restarted: bool = False
    error_code: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "runtime_id": self.runtime_id,
            "acp_session_id": self.acp_session_id,
            "text": self.text,
            "reasoning": self.reasoning,
            "stop_reason": self.stop_reason,
            "updates": list(self.updates),
            "agent_capabilities": dict(self.agent_capabilities),
            "prompt_capabilities": dict(self.prompt_capabilities),
            "config_options": list(self.config_options),
            "session_restarted": self.session_restarted,
            "error_code": self.error_code,
            "error": self.error,
        }


class ActiveACPTurn:
    """Thread-safe cancellation handle owned by the host runtime."""

    def __init__(self, loop: asyncio.AbstractEventLoop, process: ACPAgentProcess) -> None:
        self.loop = loop
        self.process = process
        self.session_id = ""
        self.cancel_requested = False

    def request_cancel(self) -> concurrent.futures.Future[Any] | None:
        self.cancel_requested = True
        if self.loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(self._cancel(), self.loop)

    async def _cancel(self) -> None:
        connection = self.process.connection
        if connection is not None and self.session_id:
            try:
                await connection.cancel(self.session_id)
            except Exception:
                pass
        await self.process.close()


def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dict(value) if isinstance(value, dict) else {}


def _capability_enabled(value: Any) -> bool:
    # ACP v1 uses booleans; v2 uses an empty object to advertise support.
    return value is True or isinstance(value, dict)


def normalize_prompt_capabilities(agent_capabilities: dict[str, Any]) -> dict[str, bool]:
    """Normalize ACP v1/v2 prompt capability shapes to stable booleans."""
    raw = agent_capabilities.get("promptCapabilities")
    if not isinstance(raw, dict):
        session = agent_capabilities.get("session")
        session = session if isinstance(session, dict) else {}
        raw = session.get("prompt")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "image": _capability_enabled(raw.get("image")),
        "audio": _capability_enabled(raw.get("audio")),
        "embedded_context": _capability_enabled(
            raw.get("embeddedContext", raw.get("embedded_context"))
        ),
    }


def _resource_uri(item: dict[str, Any]) -> str:
    from argusai.core.assistant.host_paths import attachment_host_path

    path = attachment_host_path(item) or str(item.get("sandbox_path") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).expanduser().resolve().as_uri()
    except (OSError, ValueError):
        return ""


def _normalized_base64(value: Any) -> str:
    encoded = str(value or "")
    if encoded.startswith("data:"):
        marker = encoded.find(",")
        if marker < 0 or ";base64" not in encoded[:marker].lower():
            raise ValueError("invalid ACP attachment data URI")
        encoded = encoded[marker + 1:]
    encoded = "".join(encoded.split())
    if not encoded:
        raise ValueError("empty ACP attachment data")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid ACP attachment base64") from exc
    return base64.b64encode(payload).decode("ascii")


def _resource_prompt_block(
    item: dict[str, Any],
    raw: dict[str, Any],
    *,
    embedded_context: bool,
) -> Any | None:
    uri = _resource_uri(item)
    if not uri:
        return None
    mime = str(item.get("mime") or raw.get("mime") or "application/octet-stream")
    kind = str(raw.get("type") or item.get("type") or "").lower()
    text = raw.get("text")
    if (
        embedded_context
        and kind == "text"
        and isinstance(text, str)
        and len(text.encode("utf-8", errors="ignore")) <= 8 * 1024
    ):
        return acp.resource_block(acp.embedded_text_resource(uri, text, mime_type=mime))
    return acp.resource_link_block(
        str(item.get("name") or raw.get("name") or "attachment"),
        uri,
        mime_type=mime,
        size=int(item.get("size") or raw.get("size") or 0) or None,
        description="User-supplied file. Read it in place; write generated artifacts to the session sandbox.",
    )


def acp_workspace_instruction(cwd: str, *, project: bool) -> str:
    """Tell a local Agent it already lives in the chosen project directory."""
    path = str(cwd or "").strip()
    if not project or not path:
        return ""
    return (
        "[ArgusAI workspace] Your working directory is already "
        f"`{path}`. Use your own filesystem and shell tools there. "
        "ArgusAI `sandbox_*` / `run_shell_snippet` write to a separate "
        "session scratch folder, not this project.\n\n"
    )


def build_acp_prompt_blocks(
    message: str,
    attachments: list[dict[str, Any]],
    attachment_resources: list[dict[str, Any]],
    prompt_capabilities: dict[str, bool],
) -> list[Any]:
    """Build one capability-safe ACP prompt without guessing model support."""
    blocks: list[Any] = []
    if message:
        blocks.append(acp.text_block(message))
    embedded_context = bool(prompt_capabilities.get("embedded_context"))
    for index, raw in enumerate(attachments):
        if not isinstance(raw, dict):
            continue
        item = (
            attachment_resources[index]
            if index < len(attachment_resources)
            and isinstance(attachment_resources[index], dict)
            else raw
        )
        kind = str(raw.get("type") or "").lower()
        data = raw.get("data_base64") or raw.get("data") or ""
        mime = str(raw.get("mime") or "application/octet-stream")
        if kind == "image" and prompt_capabilities.get("image") and data:
            blocks.append(acp.image_block(
                _normalized_base64(data),
                mime,
                uri=_resource_uri(item) or None,
            ))
            continue
        if kind == "audio" and prompt_capabilities.get("audio") and data:
            blocks.append(acp.audio_block(_normalized_base64(data), mime))
            continue
        resource = _resource_prompt_block(
            item,
            raw,
            embedded_context=embedded_context,
        )
        if resource is not None:
            blocks.append(resource)
    return blocks


async def _apply_preferred_config(
    connection: Any,
    session_id: str,
    preferred: dict[str, str | bool],
    *,
    known_options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply persisted configId→value preferences; return latest options."""
    options = list(known_options)
    if not preferred:
        return options
    known_ids = {str(item.get("id") or "") for item in options}
    for config_id, value in preferred.items():
        key = str(config_id or "").strip()
        if not key:
            continue
        # Skip unknown ids when we already have a snapshot — avoids noisy errors
        # on agents that never advertised this option.
        if known_ids and key not in known_ids:
            continue
        try:
            response = await connection.set_config_option(key, session_id, value)
        except Exception:
            continue
        refreshed = normalize_config_options(
            _model_dict(response).get("configOptions")
        )
        if refreshed:
            options = refreshed
            known_ids = {str(item.get("id") or "") for item in options}
    return options


async def fetch_acp_config_options(
    runtime: AgentRuntimeConfig,
    *,
    cwd: str,
    preferred: dict[str, str | bool] | None = None,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Open a short ACP session solely to read (and optionally apply) configOptions."""
    process = ACPAgentProcess(runtime, cwd=cwd)
    try:
        initialized = await process.initialize(timeout_seconds=min(30.0, timeout_seconds))
        initialized_payload = _model_dict(initialized)
        capabilities = dict(initialized_payload.get("agentCapabilities") or {})
        connection = process.connection
        if connection is None:
            raise RuntimeError("ACP connection is unavailable after initialize")
        created = await asyncio.wait_for(
            connection.new_session(cwd, mcp_servers=[]),
            timeout=max(1.0, float(timeout_seconds)),
        )
        payload = _model_dict(created)
        session_id = str(payload.get("sessionId") or "")
        options = normalize_config_options(payload.get("configOptions"))
        if session_id and preferred:
            options = await _apply_preferred_config(
                connection,
                session_id,
                preferred,
                known_options=options,
            )
        if session_id and hasattr(connection, "close_session"):
            try:
                await connection.close_session(session_id)
            except Exception:
                pass
        return {
            "ok": True,
            "acp_session_id": session_id,
            "config_options": options,
            "agent_capabilities": capabilities,
            "prompt_capabilities": normalize_prompt_capabilities(capabilities),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "acp_config_probe_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "config_options": [],
        }
    finally:
        await process.close()


async def run_acp_turn(
    runtime: AgentRuntimeConfig,
    binding: SessionRuntimeBinding,
    *,
    message: str,
    cwd: str,
    attachments: list[dict[str, Any]] | None = None,
    attachment_resources: list[dict[str, Any]] | None = None,
    on_update: ACP_UPDATE_CALLBACK | None = None,
    on_active: Callable[[ActiveACPTurn], None] | None = None,
    on_permission: ACP_PERMISSION_HANDLER | None = None,
    mcp_servers: list[Any] | None = None,
    stateless_message: str = "",
    timeout_seconds: float = 604800,
) -> ACPTurnResult:
    """Run initialize → session/new|load → optional set_config_option → prompt."""
    updates: list[dict[str, Any]] = []
    text_parts: list[str] = []
    thought_parts: list[str] = []
    config_options: list[dict[str, Any]] = list(binding.acp_config_options)
    capabilities: dict[str, Any] = {}
    prompt_capabilities: dict[str, bool] = dict(binding.acp_prompt_capabilities)
    replaying = False

    def _event_sink(envelope: dict[str, Any]) -> None:
        update = envelope.get("update")
        if not isinstance(update, dict):
            return
        if replaying:
            # `session/load` re-emits the whole transcript as session/update
            # notifications before it answers. That history is already on
            # screen, and counting it here would repeat the previous answer
            # above the new one and prepend it to this turn's text.
            return
        kind = str(update.get("sessionUpdate") or "")
        content = update.get("content")
        if (
            kind in {"agent_message_chunk", "agent_thought_chunk"}
            and isinstance(content, dict)
            and content.get("type") == "text"
        ):
            raw_text = str(content.get("text") or "")
            if is_synthetic_agent_chunk(raw_text):
                return
            if kind == "agent_message_chunk":
                text_parts.append(raw_text)
            else:
                thought_parts.append(raw_text)
        updates.append(update)
        if on_update is not None:
            on_update(update)

    process = ACPAgentProcess(
        runtime,
        event_sink=_event_sink,
        permission_handler=on_permission,
        cwd=cwd,
    )
    active = ActiveACPTurn(asyncio.get_running_loop(), process)
    if on_active is not None:
        on_active(active)
    try:
        initialize = await process.initialize(timeout_seconds=min(30.0, timeout_seconds))
        initialize_payload = _model_dict(initialize)
        capabilities = dict(initialize_payload.get("agentCapabilities") or {})
        prompt_capabilities = normalize_prompt_capabilities(capabilities)
        connection = process.connection
        if connection is None:
            raise RuntimeError("ACP connection is unavailable after initialize")
        session_restarted = False
        prompt_message = message
        if binding.acp_session_id and capabilities.get("loadSession"):
            replaying = True
            try:
                loaded = await connection.load_session(
                    cwd,
                    binding.acp_session_id,
                    mcp_servers=list(mcp_servers or []),
                )
                # Notifications are dispatched on their own tasks, so the tail
                # of the replay can still arrive after the response does.
                await asyncio.sleep(0.05)
            finally:
                replaying = False
            loaded_payload = _model_dict(loaded) if loaded is not None else {}
            refreshed = normalize_config_options(loaded_payload.get("configOptions"))
            if refreshed:
                config_options = refreshed
            session_id = binding.acp_session_id
        else:
            session_restarted = bool(binding.acp_session_id)
            created = await connection.new_session(cwd, mcp_servers=list(mcp_servers or []))
            created_payload = _model_dict(created)
            session_id = str(created_payload.get("sessionId") or "")
            if not session_id:
                raise RuntimeError("ACP agent returned an empty session id")
            refreshed = normalize_config_options(created_payload.get("configOptions"))
            if refreshed:
                config_options = refreshed
            if session_restarted and stateless_message:
                prompt_message = stateless_message
        active.session_id = session_id
        config_options = await _apply_preferred_config(
            connection,
            session_id,
            dict(binding.acp_config or {}),
            known_options=config_options,
        )
        prompt_attachments = [
            dict(item) for item in list(attachments or []) if isinstance(item, dict)
        ]
        if (
            any(str(item.get("type") or "").lower() == "image" for item in prompt_attachments)
            and not prompt_capabilities.get("image")
        ):
            return ACPTurnResult(
                ok=False,
                runtime_id=runtime.id,
                acp_session_id=session_id,
                updates=updates,
                agent_capabilities=capabilities,
                prompt_capabilities=prompt_capabilities,
                config_options=config_options,
                session_restarted=session_restarted,
                error_code="acp_image_unsupported",
                error="This local Agent does not advertise ACP image prompt support.",
            )
        prompt_blocks = build_acp_prompt_blocks(
            prompt_message,
            prompt_attachments,
            [
                dict(item)
                for item in list(attachment_resources or [])
                if isinstance(item, dict)
            ],
            prompt_capabilities,
        )
        if not prompt_blocks:
            return ACPTurnResult(
                ok=False,
                runtime_id=runtime.id,
                acp_session_id=session_id,
                updates=updates,
                agent_capabilities=capabilities,
                prompt_capabilities=prompt_capabilities,
                config_options=config_options,
                session_restarted=session_restarted,
                error_code="acp_prompt_empty",
                error="No ACP-compatible prompt content remained after validation.",
            )
        response = await asyncio.wait_for(
            connection.prompt(session_id, prompt_blocks),
            timeout=max(1.0, float(timeout_seconds)),
        )
        # ACP notifications and the prompt response share one byte stream, but
        # the SDK dispatches them on separate tasks. Yield briefly so updates
        # that preceded the response are committed before we snapshot/close.
        await asyncio.sleep(0.05)
        response_payload = _model_dict(response)
        return ACPTurnResult(
            ok=True,
            runtime_id=runtime.id,
            acp_session_id=session_id,
            text=strip_synthetic_agent_text("".join(text_parts)),
            reasoning=strip_synthetic_agent_text("".join(thought_parts)),
            stop_reason=str(response_payload.get("stopReason") or ""),
            updates=updates,
            agent_capabilities=capabilities,
            prompt_capabilities=prompt_capabilities,
            config_options=config_options,
            session_restarted=session_restarted,
        )
    except asyncio.TimeoutError:
        return ACPTurnResult(
            ok=False,
            runtime_id=runtime.id,
            acp_session_id=active.session_id or binding.acp_session_id,
            text=strip_synthetic_agent_text("".join(text_parts)),
            reasoning=strip_synthetic_agent_text("".join(thought_parts)),
            updates=updates,
            agent_capabilities=capabilities,
            prompt_capabilities=prompt_capabilities,
            config_options=config_options,
            error_code="acp_turn_timeout",
            error=f"ACP turn timed out after {timeout_seconds:g} seconds",
        )
    except Exception as exc:
        cancelled = active.cancel_requested
        return ACPTurnResult(
            ok=False,
            runtime_id=runtime.id,
            acp_session_id=active.session_id or binding.acp_session_id,
            text=strip_synthetic_agent_text("".join(text_parts)),
            reasoning=strip_synthetic_agent_text("".join(thought_parts)),
            updates=updates,
            agent_capabilities=capabilities,
            prompt_capabilities=prompt_capabilities,
            config_options=config_options,
            error_code="acp_turn_cancelled" if cancelled else "acp_turn_failed",
            error="ACP turn cancelled" if cancelled else f"{type(exc).__name__}: {exc}",
        )
    finally:
        await process.close()
