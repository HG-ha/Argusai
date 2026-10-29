"""Declarative metadata for trusted external command-line tools.

The declarations in this module are application-owned configuration. Runtime
callers can select a registered tool, but cannot supply an install URL or an
installer command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlparse

from argusai.core.managed_runtimes import platform_subdir

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PLATFORM_KEYS = {
    "win-x64",
    "win-arm64",
    "linux-x64",
    "linux-arm64",
    "macos-x64",
    "macos-arm64",
}


class OperatingSystem(StrEnum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


class Architecture(StrEnum):
    X64 = "x64"
    ARM64 = "arm64"


class ProbeSource(StrEnum):
    OVERRIDE = "override"
    MANAGED = "managed"
    PATH = "path"


class InstallKind(StrEnum):
    MANUAL = "manual"
    VERIFIED_ARCHIVE = "verified_archive"
    MANAGED_RUNTIME = "managed_runtime"


@dataclass(frozen=True)
class HostPlatform:
    operating_system: OperatingSystem
    architecture: Architecture

    @property
    def key(self) -> str:
        os_key = "win" if self.operating_system is OperatingSystem.WINDOWS else self.operating_system.value
        return f"{os_key}-{self.architecture.value}"

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "operating_system": self.operating_system.value,
            "architecture": self.architecture.value,
        }


def detect_host_platform(
    system: str | None = None,
    machine: str | None = None,
) -> HostPlatform:
    """Normalize host information using the managed-runtime platform contract."""
    key = platform_subdir(system, machine)
    os_key, arch_key = key.split("-", 1)
    operating_system = {
        "win": OperatingSystem.WINDOWS,
        "linux": OperatingSystem.LINUX,
        "macos": OperatingSystem.MACOS,
    }[os_key]
    return HostPlatform(operating_system, Architecture(arch_key))


@dataclass(frozen=True)
class PlatformMetadata:
    operating_systems: tuple[OperatingSystem, ...] = tuple(OperatingSystem)
    architectures: tuple[Architecture, ...] = tuple(Architecture)
    platform_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        systems = tuple(OperatingSystem(value) for value in self.operating_systems)
        architectures = tuple(Architecture(value) for value in self.architectures)
        keys = tuple(dict.fromkeys(str(value).strip().lower() for value in self.platform_keys))
        if not systems:
            raise ValueError("at least one operating system must be declared")
        if not architectures:
            raise ValueError("at least one architecture must be declared")
        invalid = [key for key in keys if key not in _PLATFORM_KEYS]
        if invalid:
            raise ValueError(f"unsupported platform keys: {invalid}")
        object.__setattr__(self, "operating_systems", tuple(dict.fromkeys(systems)))
        object.__setattr__(self, "architectures", tuple(dict.fromkeys(architectures)))
        object.__setattr__(self, "platform_keys", keys)

    def supports(self, host: HostPlatform) -> bool:
        if self.platform_keys and host.key not in self.platform_keys:
            return False
        return (
            host.operating_system in self.operating_systems
            and host.architecture in self.architectures
        )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "operating_systems": [item.value for item in self.operating_systems],
            "architectures": [item.value for item in self.architectures],
            "platform_keys": list(self.platform_keys),
        }


@dataclass(frozen=True)
class LicenseMetadata:
    spdx_id: str
    name: str
    url: str
    redistributable: bool
    notice: str = ""

    def __post_init__(self) -> None:
        if not self.spdx_id.strip():
            raise ValueError("license spdx_id is required")
        if not self.name.strip():
            raise ValueError("license name is required")
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("license URL must be an absolute HTTPS URL")

    def as_dict(self) -> dict[str, object]:
        return {
            "spdx_id": self.spdx_id,
            "name": self.name,
            "url": self.url,
            "redistributable": self.redistributable,
            "notice": self.notice,
        }


@dataclass(frozen=True)
class ManagedPath:
    """A trusted path relative to a registry-managed root."""

    relative_path: str
    platform_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        value = self.relative_path.strip().replace("\\", "/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("managed paths must be non-empty relative paths without traversal")
        keys = tuple(dict.fromkeys(str(item).strip().lower() for item in self.platform_keys))
        invalid = [key for key in keys if key not in _PLATFORM_KEYS]
        if invalid:
            raise ValueError(f"unsupported managed-path platform keys: {invalid}")
        object.__setattr__(self, "relative_path", value)
        object.__setattr__(self, "platform_keys", keys)

    def supports(self, host: HostPlatform) -> bool:
        return not self.platform_keys or host.key in self.platform_keys

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "platform_keys": list(self.platform_keys),
        }


@dataclass(frozen=True)
class Probe:
    """Fixed executable names and version arguments for one CLI."""

    commands: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    managed_paths: tuple[ManagedPath, ...] = ()
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        commands = tuple(dict.fromkeys(str(value).strip() for value in self.commands))
        if not commands:
            raise ValueError("probe commands cannot be empty")
        if any(
            not command
            or "/" in command
            or "\\" in command
            or "\x00" in command
            or "\r" in command
            or "\n" in command
            for command in commands
        ):
            raise ValueError("probe commands must be bare executable names")
        version_args = tuple(str(value) for value in self.version_args)
        if any("\x00" in value or "\r" in value or "\n" in value for value in version_args):
            raise ValueError("version probe arguments contain an unsafe control character")
        if not 0 < float(self.timeout_seconds) <= 30:
            raise ValueError("probe timeout must be between 0 and 30 seconds")
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "version_args", version_args)
        object.__setattr__(self, "managed_paths", tuple(self.managed_paths))

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": list(self.commands),
            "version_args": list(self.version_args),
            "managed_paths": [path.as_dict() for path in self.managed_paths],
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class InstallArtifact:
    """A pinned archive admitted for exactly one host platform."""

    platform_key: str
    filename: str
    sha256: str
    sources: tuple[str, ...]

    def __post_init__(self) -> None:
        platform_key = self.platform_key.strip().lower()
        if platform_key not in {*_PLATFORM_KEYS, "any"}:
            raise ValueError(f"unsupported artifact platform: {platform_key}")
        filename = self.filename.strip()
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
        ):
            raise ValueError("artifact filename must be a plain filename")
        digest = self.sha256.strip().lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("artifact sha256 must contain exactly 64 hexadecimal characters")
        sources = tuple(dict.fromkeys(str(value).strip() for value in self.sources))
        if not sources:
            raise ValueError("verified archive policy requires at least one fixed source")
        for source in sources:
            parsed = urlparse(source)
            segments = [segment.lower() for segment in parsed.path.split("/") if segment]
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("artifact sources must be fixed, credential-free HTTPS URLs")
            if any(segment == "latest" or segment.startswith("latest-") for segment in segments):
                raise ValueError("artifact sources cannot use a floating 'latest' URL")
        object.__setattr__(self, "platform_key", platform_key)
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "sources", sources)

    def as_dict(self) -> dict[str, object]:
        return {
            "platform_key": self.platform_key,
            "filename": self.filename,
            "sha256": self.sha256,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class InstallPolicy:
    kind: InstallKind
    version: str = ""
    artifacts: tuple[InstallArtifact, ...] = ()
    runtime: str = ""
    docs_url: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        kind = InstallKind(self.kind)
        version = self.version.strip()
        artifacts = tuple(self.artifacts)
        runtime = self.runtime.strip().lower()
        if self.docs_url:
            parsed = urlparse(self.docs_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("install documentation URL must be absolute HTTPS")
        if kind is InstallKind.MANUAL:
            if version or artifacts or runtime:
                raise ValueError("manual policies cannot carry artifacts, versions, or runtimes")
        elif kind is InstallKind.VERIFIED_ARCHIVE:
            if not version or "latest" in version.lower():
                raise ValueError("verified archive policies require an exact pinned version")
            if not artifacts:
                raise ValueError("verified archive policies require pinned artifacts")
            if runtime:
                raise ValueError("verified archive policies cannot name a managed runtime")
            platforms = [artifact.platform_key for artifact in artifacts]
            if len(platforms) != len(set(platforms)):
                raise ValueError("only one install artifact may be declared per platform")
        elif kind is InstallKind.MANAGED_RUNTIME:
            if version or artifacts or runtime not in {"python", "node", "wabt"}:
                raise ValueError(
                    "managed runtime policies must select python, node, or wabt "
                    "and derive pins from managed_runtimes"
                )
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "runtime", runtime)

    @classmethod
    def manual(cls, *, docs_url: str = "", notes: str = "") -> InstallPolicy:
        return cls(InstallKind.MANUAL, docs_url=docs_url, notes=notes)

    @classmethod
    def verified_archive(
        cls,
        version: str,
        artifacts: tuple[InstallArtifact, ...],
        *,
        docs_url: str = "",
        notes: str = "",
    ) -> InstallPolicy:
        return cls(
            InstallKind.VERIFIED_ARCHIVE,
            version=version,
            artifacts=artifacts,
            docs_url=docs_url,
            notes=notes,
        )

    @classmethod
    def managed_runtime(
        cls,
        runtime: str,
        *,
        docs_url: str = "",
        notes: str = "",
    ) -> InstallPolicy:
        return cls(
            InstallKind.MANAGED_RUNTIME,
            runtime=runtime,
            docs_url=docs_url,
            notes=notes,
        )

    @property
    def can_install(self) -> bool:
        return self.kind is not InstallKind.MANUAL

    def artifact_for(self, host: HostPlatform) -> InstallArtifact | None:
        return next(
            (
                artifact
                for artifact in self.artifacts
                if artifact.platform_key in {host.key, "any"}
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "can_install": self.can_install,
            "version": self.version,
            "runtime": self.runtime,
            "docs_url": self.docs_url,
            "notes": self.notes,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class ExternalToolSpec:
    tool_id: str
    display_name: str
    probe: Probe
    platforms: PlatformMetadata
    license: LicenseMetadata
    install: InstallPolicy = field(default_factory=InstallPolicy.manual)
    description: str = ""

    def __post_init__(self) -> None:
        tool_id = self.tool_id.strip().lower()
        if not _TOOL_ID_RE.fullmatch(tool_id):
            raise ValueError(
                "tool_id must start with an alphanumeric character and contain "
                "only lowercase letters, numbers, '.', '_' or '-'"
            )
        if not self.display_name.strip():
            raise ValueError("display_name is required")
        object.__setattr__(self, "tool_id", tool_id)

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_id": self.tool_id,
            "display_name": self.display_name,
            "description": self.description,
            "probe": self.probe.as_dict(),
            "platforms": self.platforms.as_dict(),
            "license": self.license.as_dict(),
            "install": self.install.as_dict(),
        }


# Explicit aliases make the public contract readable at call sites that prefer
# the longer names while keeping the concise declaration names requested by the
# registry API.
ExternalToolProbe = Probe
ExternalToolInstallPolicy = InstallPolicy
