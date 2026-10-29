"""Pinned install planning backed by the existing verified downloader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argusai.core.managed_runtimes import (
    archive_sources,
    archive_spec,
    download_verified,
    install_runtime,
)

from .models import ExternalToolSpec, HostPlatform, InstallKind, detect_host_platform


class InstallPlanError(RuntimeError):
    """A declared tool has no safe install plan for the selected host."""


@dataclass(frozen=True)
class InstallPlan:
    """Resolved immutable trust anchors for one declared tool install."""

    tool_id: str
    kind: InstallKind
    platform_key: str
    version: str
    filename: str
    sha256: str
    sources: tuple[str, ...]
    runtime: str = ""

    def __post_init__(self) -> None:
        if self.kind is InstallKind.MANUAL:
            raise ValueError("manual installation does not produce an executable plan")
        if not self.version.strip() or "latest" in self.version.lower():
            raise ValueError("install plans require an exact pinned version")
        digest = self.sha256.strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("install plans require a pinned SHA-256 digest")
        if not self.filename.strip() or not self.sources:
            raise ValueError("install plans require a fixed filename and source list")
        if self.kind is InstallKind.MANAGED_RUNTIME and self.runtime not in {
            "python",
            "node",
            "wabt",
        }:
            raise ValueError("managed-runtime plans must select a known runtime")
        if self.kind is InstallKind.VERIFIED_ARCHIVE and self.runtime:
            raise ValueError("archive plans cannot carry a managed runtime")
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "sources", tuple(self.sources))

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "kind": self.kind.value,
            "platform_key": self.platform_key,
            "version": self.version,
            "filename": self.filename,
            "sha256": self.sha256,
            "sources": list(self.sources),
            "runtime": self.runtime,
        }


def build_install_plan(
    spec: ExternalToolSpec,
    *,
    host_platform: HostPlatform | None = None,
) -> InstallPlan:
    """Resolve only URLs and checksums already declared by application code."""
    host = host_platform or detect_host_platform()
    if not spec.platforms.supports(host):
        raise InstallPlanError(f"{spec.tool_id} does not support {host.key}")
    policy = spec.install
    if policy.kind is InstallKind.MANUAL:
        raise InstallPlanError(f"{spec.tool_id} is manual-install only")
    if policy.kind is InstallKind.VERIFIED_ARCHIVE:
        artifact = policy.artifact_for(host)
        if artifact is None:
            raise InstallPlanError(
                f"{spec.tool_id} has no pinned install artifact for {host.key}"
            )
        return InstallPlan(
            tool_id=spec.tool_id,
            kind=policy.kind,
            platform_key=host.key,
            version=policy.version,
            filename=artifact.filename,
            sha256=artifact.sha256,
            sources=artifact.sources,
        )

    try:
        managed_spec = archive_spec(policy.runtime, platform_key=host.key)
    except (RuntimeError, ValueError) as exc:
        raise InstallPlanError(str(exc)) from exc
    return InstallPlan(
        tool_id=spec.tool_id,
        kind=policy.kind,
        platform_key=host.key,
        version=managed_spec.version,
        filename=managed_spec.filename,
        sha256=managed_spec.sha256,
        sources=tuple(archive_sources(managed_spec)),
        runtime=policy.runtime,
    )


def download_install_artifact(
    plan: InstallPlan,
    destination: str | Path,
) -> dict[str, Any]:
    """Materialize a plan through managed_runtimes' checksum-enforcing downloader.

    Extraction and tool-specific layout remain the responsibility of a domain
    installer. This function intentionally accepts no URL, checksum override,
    shell command, or downloader callback.
    """
    path = Path(destination).expanduser()
    return download_verified(plan.sources, path, plan.sha256)


def install_managed_runtime(
    plan: InstallPlan,
    *,
    tools_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Delegate a managed-runtime plan to the existing atomic installer."""
    if plan.kind is not InstallKind.MANAGED_RUNTIME:
        raise InstallPlanError("only managed-runtime plans can be installed by this helper")
    expected = archive_spec(plan.runtime, platform_key=plan.platform_key)
    if (
        expected.version != plan.version
        or expected.filename != plan.filename
        or expected.sha256.lower() != plan.sha256.lower()
    ):
        raise InstallPlanError("managed-runtime plan no longer matches its pinned manifest")
    return install_runtime(plan.runtime, tools_dir=tools_dir, force=force)
