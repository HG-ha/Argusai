"""Application-owned catalogue for optional external security CLIs."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from argusai.core.workspace.tool_adapters import adapter_inventory

from .models import (
    ExternalToolSpec,
    InstallPolicy,
    LicenseMetadata,
    OperatingSystem,
    PlatformMetadata,
    Probe,
)
from .registry import ExternalToolRegistry

_OPERATING_SYSTEMS = {
    "windows": OperatingSystem.WINDOWS,
    "linux": OperatingSystem.LINUX,
    "darwin": OperatingSystem.MACOS,
    "macos": OperatingSystem.MACOS,
}


def _spec_from_adapter(adapter: dict[str, object]) -> ExternalToolSpec | None:
    commands = tuple(str(item) for item in (adapter.get("commands") or []) if str(item).strip())
    spdx_id = str(adapter.get("license_spdx") or "").strip()
    docs_url = str(adapter.get("docs_url") or "").strip()
    if not commands or not spdx_id or not docs_url:
        return None
    systems = tuple(
        dict.fromkeys(
            _OPERATING_SYSTEMS[str(item).strip().lower()]
            for item in (adapter.get("platforms") or [])
            if str(item).strip().lower() in _OPERATING_SYSTEMS
        )
    )
    if not systems:
        systems = tuple(OperatingSystem)
    name = str(adapter.get("name") or "").strip()
    notes = str(adapter.get("notes") or "").strip()
    install_policy = str(adapter.get("install_policy") or "path_only")
    return ExternalToolSpec(
        tool_id=name,
        display_name=name,
        description=(
            f"{str(adapter.get('domain') or 'security')} security adapter. "
            f"Execution risk: {str(adapter.get('risk') or 'medium')}."
        ),
        probe=Probe(commands=commands),
        platforms=PlatformMetadata(operating_systems=systems),
        license=LicenseMetadata(
            spdx_id=spdx_id,
            name=spdx_id,
            url=f"https://spdx.org/licenses/{spdx_id}.html",
            redistributable=True,
            notice=notes,
        ),
        install=InstallPolicy.manual(
            docs_url=docs_url,
            notes=(
                f"Policy '{install_policy}' is intentionally manual until a "
                "version-pinned checksum manifest is declared."
            ),
        ),
    )


def build_default_specs() -> tuple[ExternalToolSpec, ...]:
    """Convert domain adapter metadata into strict discovery declarations."""
    inventory = adapter_inventory()
    specs = [
        spec
        for adapter in inventory.get("adapters", [])
        if isinstance(adapter, dict)
        for spec in [_spec_from_adapter(adapter)]
        if spec is not None
    ]
    return tuple(sorted(specs, key=lambda item: item.tool_id))


EXTERNAL_TOOL_SPECS = build_default_specs()


def create_default_registry(
    *,
    overrides: Mapping[str, str] | None = None,
    managed_roots: Sequence[str | Path] = (),
    path_env: str | None = None,
) -> ExternalToolRegistry:
    """Create a registry populated only from application-owned declarations."""
    return ExternalToolRegistry(
        EXTERNAL_TOOL_SPECS,
        overrides=overrides,
        managed_roots=managed_roots,
        path_env=path_env,
    )
