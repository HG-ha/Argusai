"""Typed configuration and discovery results for assistant runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentRuntimeBackend = Literal["native", "acp"]


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """One persisted runtime entry.

    ``native`` entries use ArgusAI's built-in agent loop and therefore have no
    process command. ``acp`` entries launch one explicit argv without a shell.
    """

    id: str
    name: str
    backend: AgentRuntimeBackend
    enabled: bool = True
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    preset: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "enabled": bool(self.enabled),
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "cwd": self.cwd,
            "preset": bool(self.preset),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class AgentRuntimeProbe:
    """Read-only local discovery for one configured runtime."""

    runtime_id: str
    available: bool
    enabled: bool
    backend: AgentRuntimeBackend
    command: str = ""
    resolved_command: str = ""
    source: str = ""
    error_code: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "available": bool(self.available),
            "enabled": bool(self.enabled),
            "backend": self.backend,
            "command": self.command,
            "resolved_command": self.resolved_command,
            "source": self.source,
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class SessionRuntimeBinding:
    """Stable runtime/workspace identity persisted with an assistant session."""

    runtime_id: str = "argusai-native"
    workspace_mode: Literal["sandbox", "project"] = "sandbox"
    project_dir: str = ""
    acp_session_id: str = ""
    locked: bool = False
    # Preferred ACP session/set_config_option values (configId → value).
    acp_config: dict[str, str | bool] = field(default_factory=dict)
    # Last configOptions snapshot from session/new|load|set_config_option.
    acp_config_options: tuple[dict[str, Any], ...] = ()
    # Last prompt capability snapshot from ACP initialize. This is a UI hint;
    # every real turn still trusts the fresh handshake.
    acp_prompt_capabilities: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "runtime_id": self.runtime_id,
            "workspace_mode": self.workspace_mode,
            "project_dir": self.project_dir,
            "acp_session_id": self.acp_session_id,
            "locked": bool(self.locked),
            "acp_config": dict(self.acp_config),
            "acp_config_options": [dict(item) for item in self.acp_config_options],
        }
        if self.acp_prompt_capabilities:
            payload["acp_prompt_capabilities"] = {
                str(key): bool(value)
                for key, value in self.acp_prompt_capabilities.items()
            }
        return payload
