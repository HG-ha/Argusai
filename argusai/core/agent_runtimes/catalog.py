"""Built-in ACP runtime presets.

Presets describe launch commands only. Discovery never starts a process and
never reads a third-party CLI's login or credential files.
"""

from __future__ import annotations

from .models import AgentRuntimeConfig

NATIVE_RUNTIME_ID = "argusai-native"

BUILTIN_AGENT_RUNTIMES: tuple[AgentRuntimeConfig, ...] = (
    AgentRuntimeConfig(
        id=NATIVE_RUNTIME_ID,
        name="ArgusAI Agent",
        backend="native",
        preset=True,
        notes="Built-in ArgusAI agent loop.",
    ),
    AgentRuntimeConfig(
        id="codebuddy-acp",
        name="CodeBuddy",
        backend="acp",
        command="codebuddy",
        args=("--acp",),
        preset=True,
    ),
    AgentRuntimeConfig(
        id="trae-acp",
        name="TRAE CLI",
        backend="acp",
        command="traecli",
        args=("acp", "serve"),
        preset=True,
    ),
    AgentRuntimeConfig(
        id="qoder-acp",
        name="Qoder CLI",
        backend="acp",
        command="qodercli",
        args=("--acp",),
        preset=True,
    ),
    AgentRuntimeConfig(
        id="kimi-acp",
        name="Kimi Code CLI",
        backend="acp",
        command="kimi",
        args=("acp",),
        preset=True,
    ),
    AgentRuntimeConfig(
        id="opencode-acp",
        name="OpenCode",
        backend="acp",
        command="opencode",
        args=("acp",),
        preset=True,
    ),
    AgentRuntimeConfig(
        id="cursor-acp",
        name="Cursor",
        backend="acp",
        command="agent",
        args=("acp",),
        preset=True,
        notes=(
            "Cursor CLI ACP (`agent acp`). If PATH only has cursor-agent, "
            "override the command to cursor-agent in this panel."
        ),
    ),
    AgentRuntimeConfig(
        id="codex-acp",
        name="OpenAI Codex",
        backend="acp",
        command="npx",
        args=("-y", "@agentclientprotocol/codex-acp"),
        preset=True,
        notes="Requires Node.js/npm. Uses the official Codex ACP adapter via npx.",
    ),
    AgentRuntimeConfig(
        id="claude-code-acp",
        name="Claude Code",
        backend="acp",
        command="npx",
        args=("-y", "@agentclientprotocol/claude-agent-acp"),
        preset=True,
        notes="Requires Node.js/npm. Uses the official Claude Code ACP adapter via npx.",
    ),
)


def builtin_agent_runtimes() -> list[AgentRuntimeConfig]:
    """Return independent copies suitable for settings normalization."""
    return [
        AgentRuntimeConfig(
            id=item.id,
            name=item.name,
            backend=item.backend,
            enabled=item.enabled,
            command=item.command,
            args=tuple(item.args),
            env=dict(item.env),
            cwd=item.cwd,
            preset=item.preset,
            notes=item.notes,
        )
        for item in BUILTIN_AGENT_RUNTIMES
    ]
