"""Unified, declarative foundation for optional external command-line tools."""

from .argv import (
    ExternalToolUnavailable,
    UnsafeArgvError,
    argv_from_probe,
    build_safe_argv,
)
from .install import (
    InstallPlan,
    InstallPlanError,
    build_install_plan,
    download_install_artifact,
    install_managed_runtime,
)
from .models import (
    Architecture,
    ExternalToolInstallPolicy,
    ExternalToolProbe,
    ExternalToolSpec,
    HostPlatform,
    InstallArtifact,
    InstallKind,
    InstallPolicy,
    LicenseMetadata,
    ManagedPath,
    OperatingSystem,
    PlatformMetadata,
    Probe,
    ProbeSource,
    detect_host_platform,
)
from .registry import ExternalToolRegistry, ProbeResult
from .catalog import EXTERNAL_TOOL_SPECS, build_default_specs, create_default_registry

__all__ = [
    "Architecture",
    "ExternalToolInstallPolicy",
    "EXTERNAL_TOOL_SPECS",
    "ExternalToolProbe",
    "ExternalToolRegistry",
    "ExternalToolSpec",
    "ExternalToolUnavailable",
    "HostPlatform",
    "InstallArtifact",
    "InstallKind",
    "InstallPlan",
    "InstallPlanError",
    "InstallPolicy",
    "LicenseMetadata",
    "ManagedPath",
    "OperatingSystem",
    "PlatformMetadata",
    "Probe",
    "ProbeResult",
    "ProbeSource",
    "UnsafeArgvError",
    "argv_from_probe",
    "build_default_specs",
    "build_install_plan",
    "build_safe_argv",
    "create_default_registry",
    "detect_host_platform",
    "download_install_artifact",
    "install_managed_runtime",
]
