"""Backward-compatible normalization for persisted agent runtimes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .catalog import NATIVE_RUNTIME_ID, builtin_agent_runtimes
from .models import AgentRuntimeConfig, SessionRuntimeBinding

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_ARGS = 128
_MAX_ARG_LENGTH = 32_768


def _clean_text(value: Any, *, max_length: int = 32_768) -> str:
    text = str(value or "").strip()
    if "\x00" in text or "\r" in text or "\n" in text:
        return ""
    return text[:max_length]


def _normalize_args(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for raw in value[:_MAX_ARGS]:
        text = str(raw)
        if "\x00" in text or "\r" in text or "\n" in text or len(text) > _MAX_ARG_LENGTH:
            continue
        out.append(text)
    return tuple(out)


def _normalize_env(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        text = str(raw_value or "")
        if not _ENV_NAME_RE.fullmatch(name) or "\x00" in text or len(text) > _MAX_ARG_LENGTH:
            continue
        out[name] = text
    return out


def _custom_id(raw: Mapping[str, Any], index: int) -> str:
    requested = str(raw.get("id") or "").strip().lower()
    if _ID_RE.fullmatch(requested):
        return requested
    seed = json.dumps(
        [
            str(raw.get("name") or ""),
            str(raw.get("command") or ""),
            raw.get("args") if isinstance(raw.get("args"), list) else [],
            index,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"custom-acp-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:10]}"


def normalize_agent_runtimes(value: Any) -> list[dict[str, Any]]:
    """Return JSON-clean runtimes with all built-in presets represented.

    Existing preset entries may override name, command, args, env, cwd, notes,
    and enabled state. The built-in native runtime is always enabled so a
    missing external CLI cannot make new conversations impossible.
    """

    presets = builtin_agent_runtimes()
    preset_map = {item.id: item for item in presets}
    raw_items: Iterable[Any] = value if isinstance(value, list) else ()
    overrides: dict[str, Mapping[str, Any]] = {}
    custom: list[AgentRuntimeConfig] = []
    seen_custom: set[str] = set()

    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            continue
        raw_id = str(item.get("id") or "").strip().lower()
        if raw_id in preset_map:
            overrides[raw_id] = item
            continue
        runtime_id = _custom_id(item, index)
        if runtime_id in preset_map or runtime_id in seen_custom:
            continue
        command = _clean_text(item.get("command"))
        if not command:
            continue
        seen_custom.add(runtime_id)
        name = _clean_text(item.get("name"), max_length=160) or "Custom ACP Agent"
        custom.append(
            AgentRuntimeConfig(
                id=runtime_id,
                name=name,
                backend="acp",
                enabled=bool(item.get("enabled", True)),
                command=command,
                args=_normalize_args(item.get("args")),
                env=_normalize_env(item.get("env")),
                cwd=_clean_text(item.get("cwd")),
                preset=False,
                notes=str(item.get("notes") or "").strip()[:2000],
            )
        )

    normalized: list[AgentRuntimeConfig] = []
    for preset in presets:
        override = overrides.get(preset.id, {})
        enabled = True if preset.id == NATIVE_RUNTIME_ID else bool(override.get("enabled", preset.enabled))
        normalized.append(
            AgentRuntimeConfig(
                id=preset.id,
                name=_clean_text(override.get("name"), max_length=160) or preset.name,
                backend=preset.backend,
                enabled=enabled,
                command=(
                    ""
                    if preset.backend == "native"
                    else _clean_text(override.get("command")) or preset.command
                ),
                args=(
                    ()
                    if preset.backend == "native"
                    else _normalize_args(override.get("args"))
                    if "args" in override
                    else preset.args
                ),
                env=_normalize_env(override.get("env")),
                cwd=_clean_text(override.get("cwd")),
                preset=True,
                notes=str(override.get("notes") or preset.notes).strip()[:2000],
            )
        )
    return [item.to_dict() for item in (*normalized, *custom)]


def runtime_configs(value: Any) -> list[AgentRuntimeConfig]:
    """Normalize settings data and return typed runtime objects."""
    return [
        AgentRuntimeConfig(
            id=str(item["id"]),
            name=str(item["name"]),
            backend=str(item["backend"]),  # type: ignore[arg-type]
            enabled=bool(item["enabled"]),
            command=str(item["command"]),
            args=tuple(str(arg) for arg in item["args"]),
            env={str(k): str(v) for k, v in dict(item["env"]).items()},
            cwd=str(item["cwd"]),
            preset=bool(item["preset"]),
            notes=str(item["notes"]),
        )
        for item in normalize_agent_runtimes(value)
    ]


def normalize_session_runtime_binding(value: Any) -> SessionRuntimeBinding:
    """Normalize session runtime metadata without touching the filesystem."""
    raw = value if isinstance(value, Mapping) else {}
    runtime_id = str(raw.get("runtime_id") or raw.get("id") or NATIVE_RUNTIME_ID).strip().lower()
    if not _ID_RE.fullmatch(runtime_id):
        runtime_id = NATIVE_RUNTIME_ID
    workspace_mode = str(raw.get("workspace_mode") or "sandbox").strip().lower()
    if workspace_mode not in {"sandbox", "project"}:
        workspace_mode = "sandbox"
    project_dir = str(raw.get("project_dir") or "").strip()
    if workspace_mode != "project" or not project_dir:
        workspace_mode = "sandbox"
        project_dir = ""
    acp_config: dict[str, str | bool] = {}
    raw_config = raw.get("acp_config")
    if isinstance(raw_config, Mapping):
        for key, value in raw_config.items():
            config_id = str(key or "").strip()[:120]
            if not config_id:
                continue
            if isinstance(value, bool):
                acp_config[config_id] = value
            else:
                text = str(value or "").strip()[:200]
                if text:
                    acp_config[config_id] = text

    from .acp_config import normalize_config_options

    acp_config_options = tuple(normalize_config_options(raw.get("acp_config_options")))
    raw_prompt_capabilities = raw.get("acp_prompt_capabilities")
    acp_prompt_capabilities: dict[str, bool] = {}
    if isinstance(raw_prompt_capabilities, Mapping):
        for key in ("image", "audio", "embedded_context"):
            if key in raw_prompt_capabilities:
                acp_prompt_capabilities[key] = bool(raw_prompt_capabilities.get(key))
    return SessionRuntimeBinding(
        runtime_id=runtime_id,
        workspace_mode=workspace_mode,  # type: ignore[arg-type]
        project_dir=project_dir,
        acp_session_id=str(raw.get("acp_session_id") or "").strip()[:512],
        locked=bool(raw.get("locked", False)),
        acp_config=acp_config,
        acp_config_options=acp_config_options,
        acp_prompt_capabilities=acp_prompt_capabilities,
    )


def preserve_session_project_dir(
    current: SessionRuntimeBinding,
    requested: SessionRuntimeBinding,
) -> SessionRuntimeBinding:
    """Keep a bound project folder when a later snapshot dropped the path.

    Empty ``project_dir`` normalizes to sandbox. Session-list refresh used to
    send that empty snapshot and bounce a local Agent out of the project.
    """
    if requested.workspace_mode == "project" and requested.project_dir:
        return requested
    if current.workspace_mode == "project" and current.project_dir and not requested.project_dir:
        return replace(
            requested,
            workspace_mode="project",
            project_dir=current.project_dir,
        )
    return requested
