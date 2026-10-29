"""Assistant-facing Active Directory report import and safe query wrappers."""

from __future__ import annotations

from typing import Any

from argusai.core.security import ad_tools as _ad


def ad_tool_inventory(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List supported AD tooling without contacting a directory."""
    return _ad.ad_tool_inventory()


def ad_sharphound_import(
    api: Any,
    path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Summarize an existing SharpHound JSON/ZIP without exposing identities."""
    return _ad.parse_sharphound_report(path)


def ad_pingcastle_import(
    api: Any,
    path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Summarize an existing PingCastle XML report without exposing identities."""
    return _ad.parse_pingcastle_report(path)


def ad_certipy_find_plan(
    api: Any,
    domain: str = "",
    dc_host: str = "",
    username: str = "",
    output_prefix: str = "argusai-adcs",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a Kerberos/no-pass, read-only Certipy AD CS inventory."""
    return _ad.certipy_find_plan(
        domain,
        dc_host=dc_host,
        username=username,
        output_prefix=output_prefix,
    )


async def ad_certipy_find_run(
    api: Any,
    domain: str = "",
    dc_host: str = "",
    username: str = "",
    output_prefix: str = "argusai-adcs",
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an approved Kerberos/no-pass Certipy AD CS inventory."""
    return await _ad.certipy_find_run(
        domain,
        dc_host=dc_host,
        username=username,
        output_prefix=output_prefix,
        timeout_seconds=timeout_seconds,
    )
