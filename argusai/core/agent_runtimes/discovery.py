"""Read-only PATH discovery for configured assistant runtimes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from argusai.core.paths import is_explicit_absolute

from .config import runtime_configs
from .models import AgentRuntimeConfig, AgentRuntimeProbe


def probe_agent_runtime(
    runtime: AgentRuntimeConfig,
    *,
    path_env: str | None = None,
) -> AgentRuntimeProbe:
    """Resolve one runtime command without starting it or reading credentials."""
    if runtime.backend == "native":
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=True,
            enabled=runtime.enabled,
            backend=runtime.backend,
            source="builtin",
        )
    if not runtime.enabled:
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=False,
            enabled=False,
            backend=runtime.backend,
            command=runtime.command,
            error_code="agent_runtime_disabled",
            error="runtime is disabled",
        )
    command = str(runtime.command or "").strip()
    if not command:
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=False,
            enabled=True,
            backend=runtime.backend,
            error_code="agent_runtime_command_missing",
            error="runtime command is missing",
        )

    if is_explicit_absolute(command):
        candidate = Path(command).expanduser()
        if candidate.is_file():
            return AgentRuntimeProbe(
                runtime_id=runtime.id,
                available=True,
                enabled=True,
                backend=runtime.backend,
                command=command,
                resolved_command=str(candidate.resolve()),
                source="configured",
            )
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=False,
            enabled=True,
            backend=runtime.backend,
            command=command,
            error_code="agent_runtime_command_not_found",
            error=f"configured runtime command does not exist: {command}",
        )

    if "/" in command or "\\" in command:
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=False,
            enabled=True,
            backend=runtime.backend,
            command=command,
            error_code="agent_runtime_command_unsafe",
            error="runtime command must be a bare executable name or an absolute path",
        )

    resolved = shutil.which(command, path=path_env) if path_env is not None else shutil.which(command)
    if resolved:
        return AgentRuntimeProbe(
            runtime_id=runtime.id,
            available=True,
            enabled=True,
            backend=runtime.backend,
            command=command,
            resolved_command=str(Path(resolved).expanduser().resolve()),
            source="path",
        )
    # Cursor installs historically used either `agent` or `cursor-agent`.
    if runtime.id == "cursor-acp":
        for alias in ("cursor-agent",):
            alias_resolved = (
                shutil.which(alias, path=path_env)
                if path_env is not None
                else shutil.which(alias)
            )
            if alias_resolved:
                return AgentRuntimeProbe(
                    runtime_id=runtime.id,
                    available=True,
                    enabled=True,
                    backend=runtime.backend,
                    command=alias,
                    resolved_command=str(Path(alias_resolved).expanduser().resolve()),
                    source="path-alias",
                )
    return AgentRuntimeProbe(
        runtime_id=runtime.id,
        available=False,
        enabled=True,
        backend=runtime.backend,
        command=command,
        error_code="agent_runtime_not_found",
        error=f"{command} was not found on PATH",
    )


def public_runtime_config(runtime: AgentRuntimeConfig) -> dict[str, Any]:
    """Return runtime metadata safe for the GUI and logs."""
    return {
        "id": runtime.id,
        "name": runtime.name,
        "backend": runtime.backend,
        "enabled": bool(runtime.enabled),
        "command": runtime.command,
        "args": list(runtime.args),
        "env_keys": sorted(runtime.env),
        "has_env": bool(runtime.env),
        "cwd": runtime.cwd,
        "preset": bool(runtime.preset),
        "notes": runtime.notes,
    }


def discover_agent_runtimes(
    value: Any,
    *,
    path_env: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic runtime inventory without launching any CLI."""
    runtimes = runtime_configs(value)
    rows: list[dict[str, Any]] = []
    available: list[str] = []
    missing: list[str] = []
    disabled: list[str] = []
    for runtime in runtimes:
        probe = probe_agent_runtime(runtime, path_env=path_env)
        rows.append({**public_runtime_config(runtime), "probe": probe.to_dict()})
        if not runtime.enabled:
            disabled.append(runtime.id)
        elif probe.available:
            available.append(runtime.id)
        else:
            missing.append(runtime.id)
    return {
        "ok": True,
        "path_env_present": bool(path_env if path_env is not None else os.environ.get("PATH")),
        "available": available,
        "missing": missing,
        "disabled": disabled,
        "runtimes": rows,
    }
