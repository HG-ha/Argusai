"""Assistant-facing wrappers for read-only cloud, Kubernetes and IaC tools."""

from __future__ import annotations

from typing import Any

from argusai.core.security import cloud_tools as _cloud
from argusai.core.security import iac_tools as _iac


def cloud_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List available cloud/Kubernetes CLI bridges without executing them."""
    return _cloud.cloud_tool_inventory()


def iac_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List available local IaC scanners without executing them."""
    return _iac.iac_tool_inventory()


def trivy_cloud_plan(
    api: Any,
    target: str = "",
    scan_type: str = "image",
    severities: str = "HIGH,CRITICAL",
    ignore_unfixed: bool = False,
    offline: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a bounded Trivy cloud/image/filesystem scan without running it."""
    return _cloud.trivy_plan_scan(
        target,
        scan_type=scan_type,
        severities=severities,
        ignore_unfixed=ignore_unfixed,
        offline=offline,
    )


async def trivy_cloud_scan(
    api: Any,
    target: str = "",
    scan_type: str = "image",
    severities: str = "HIGH,CRITICAL",
    ignore_unfixed: bool = False,
    offline: bool = True,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an approved bounded Trivy scan and normalize its JSON report."""
    return await _cloud.trivy_run_scan(
        target,
        scan_type=scan_type,
        severities=severities,
        ignore_unfixed=ignore_unfixed,
        offline=offline,
        timeout_seconds=timeout_seconds,
    )


def prowler_cloud_plan(
    api: Any,
    provider: str = "",
    profile: str = "",
    regions: str = "",
    checks: str = "",
    output_directory: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a read-only Prowler assessment using host-managed credentials."""
    return _cloud.prowler_plan_scan(
        provider,
        profile=profile,
        regions=regions,
        checks=checks,
        output_directory=output_directory,
    )


async def prowler_cloud_scan(
    api: Any,
    provider: str = "",
    profile: str = "",
    regions: str = "",
    checks: str = "",
    output_directory: str = "",
    timeout_seconds: int = 900,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an approved Prowler assessment without accepting credentials in arguments."""
    return await _cloud.prowler_run_scan(
        provider,
        profile=profile,
        regions=regions,
        checks=checks,
        output_directory=output_directory,
        timeout_seconds=timeout_seconds,
    )


def kubectl_readonly_plan(
    api: Any,
    verb: str = "",
    resource: str = "",
    name: str = "",
    namespace: str = "",
    all_namespaces: bool = False,
    context: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan one allowlisted read-only kubectl query."""
    return _cloud.kubectl_readonly_plan(
        verb,
        resource=resource,
        name=name,
        namespace=namespace,
        all_namespaces=all_namespaces,
        context=context,
    )


async def kubectl_readonly_run(
    api: Any,
    verb: str = "",
    resource: str = "",
    name: str = "",
    namespace: str = "",
    all_namespaces: bool = False,
    context: str = "",
    timeout_seconds: int = 60,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one approved allowlisted read-only kubectl query."""
    return await _cloud.kubectl_readonly_run(
        verb,
        resource=resource,
        name=name,
        namespace=namespace,
        all_namespaces=all_namespaces,
        context=context,
        timeout_seconds=timeout_seconds,
    )


def trivy_iac_plan(
    api: Any,
    path: str = "",
    severities: str = "MEDIUM,HIGH,CRITICAL",
    offline: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a local Trivy IaC configuration scan."""
    return _iac.trivy_iac_plan(path, severities=severities, offline=offline)


async def trivy_iac_scan(
    api: Any,
    path: str = "",
    severities: str = "MEDIUM,HIGH,CRITICAL",
    offline: bool = True,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an approved local Trivy IaC scan."""
    return await _iac.trivy_iac_scan(
        path,
        severities=severities,
        offline=offline,
        timeout_seconds=timeout_seconds,
    )


def checkov_scan_plan(
    api: Any,
    path: str = "",
    framework: str = "",
    compact: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a local Checkov scan with external downloads disabled."""
    return _iac.checkov_plan_scan(path, framework=framework, compact=compact)


async def checkov_run_scan(
    api: Any,
    path: str = "",
    framework: str = "",
    compact: bool = True,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an approved local Checkov scan and normalize its JSON report."""
    return await _iac.checkov_run_scan(
        path,
        framework=framework,
        compact=compact,
        timeout_seconds=timeout_seconds,
    )
