"""Offline-only wrappers and parsers for Syft, Trivy, and OSV-Scanner."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from argusai.core.infra.command import CommandRequest, CommandRunner

_DEFAULT_TIMEOUT = 300
_MAX_TIMEOUT = 1800
_MAX_JSON_CHARS = 20_000_000
_MAX_RESULT_ITEMS = 200
_MAX_COMPONENTS = 100_000
_MAX_TEXT_FIELD = 4096
_SYFT_FORMATS = {"syft-json", "cyclonedx-json", "spdx-json"}
_TRIVY_SEVERITIES = {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}

_TOOL_METADATA = {
    "syft": {
        "purpose": "local dependency inventory and SBOM generation",
        "install_hint": "Install Anchore Syft manually and ensure syft is on PATH.",
    },
    "trivy": {
        "purpose": "local filesystem/SBOM vulnerability scan using a pre-populated database",
        "install_hint": "Install Aqua Trivy manually and pre-populate its vulnerability database.",
    },
    "osv-scanner": {
        "purpose": "local source/SBOM vulnerability scan using a pre-populated offline database",
        "install_hint": "Install Google OSV-Scanner manually and pre-populate its offline database.",
    },
}


def _missing_tool(tool: str) -> dict[str, Any]:
    metadata = _TOOL_METADATA[tool]
    return {
        "ok": False,
        "blocked": True,
        "do_not_retry": True,
        "can_install": False,
        "error_code": "tool_unavailable",
        "tool": tool,
        "error": f"{tool} is not installed or is not available on PATH",
        "install_hint": (
            f"{metadata['install_hint']} ArgusAI does not download CLI tools or databases."
        ),
    }


def sca_tool_inventory() -> dict[str, Any]:
    """Report local CLI availability without updating or downloading anything."""
    tools: dict[str, dict[str, Any]] = {}
    for name, metadata in _TOOL_METADATA.items():
        path = shutil.which(name)
        tools[name] = {
            "installed": bool(path),
            "path": path or "",
            "can_install": False,
            "purpose": metadata["purpose"],
        }
    return {
        "ok": True,
        "offline_only": True,
        "database_updates_allowed": False,
        "tools": tools,
        "syft": tools["syft"],
        "trivy": tools["trivy"],
        "osv_scanner": tools["osv-scanner"],
    }


def _tool_status(tool: str) -> dict[str, Any]:
    inventory = sca_tool_inventory()
    row = dict(inventory["tools"][tool])
    if not row["installed"]:
        row.update({key: value for key, value in _missing_tool(tool).items() if key != "ok"})
    return {"ok": True, "tool": tool, **row, "inventory": inventory}


def syft_status() -> dict[str, Any]:
    return _tool_status("syft")


def trivy_status() -> dict[str, Any]:
    return _tool_status("trivy")


def osv_status() -> dict[str, Any]:
    return _tool_status("osv-scanner")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_local_source(
    path: str | Path,
    *,
    allowed_root: str | Path | None = None,
) -> Path:
    text = str(path or "").strip()
    if not text:
        raise ValueError("local source path is required")
    candidate = Path(text).expanduser().resolve()
    if allowed_root is not None:
        root = Path(allowed_root).expanduser().resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("source path escapes allowed root")
    if not candidate.exists():
        raise FileNotFoundError(f"local source not found: {candidate}")
    if not (candidate.is_file() or candidate.is_dir()):
        raise ValueError(f"source is not a regular file or directory: {candidate}")
    return candidate


def _source_or_error(
    path: str | Path,
    *,
    allowed_root: str | Path | None,
) -> tuple[Path | None, dict[str, Any] | None]:
    try:
        return _resolve_local_source(path, allowed_root=allowed_root), None
    except (ValueError, FileNotFoundError, OSError) as exc:
        return None, {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_local_input",
            "error": str(exc),
        }


def syft_plan_inventory(
    path: str | Path,
    *,
    output_format: str = "cyclonedx-json",
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan local SBOM generation without executing Syft."""
    source, error = _source_or_error(path, allowed_root=allowed_root)
    if error:
        return error
    status = syft_status()
    if not status.get("installed"):
        return {**_missing_tool("syft"), "input": str(source)}
    fmt = str(output_format or "cyclonedx-json").strip().lower()
    if fmt not in _SYFT_FORMATS:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_arguments",
            "error": f"unsupported Syft output format: {fmt}",
            "allowed_formats": sorted(_SYFT_FORMATS),
        }
    source_arg = f"dir:{source}" if source.is_dir() else f"file:{source}"
    return {
        "ok": True,
        "operation": "inventory",
        "tool": "syft",
        "command": "syft",
        "args": [
            "scan",
            source_arg,
            "--output",
            fmt,
            "--check-for-app-update=false",
        ],
        "input": str(source),
        "output_format": fmt,
        "offline": True,
        "risk": "low",
        "notes": [
            "The source is a resolved local file/directory, never a registry or remote image reference.",
            "Application update checks are disabled.",
        ],
    }


def _normalize_severities(value: str | list[str] | None) -> str:
    raw = value if isinstance(value, list) else str(value or "").split(",")
    selected: list[str] = []
    for item in raw:
        severity = str(item or "").strip().upper()
        if severity in _TRIVY_SEVERITIES and severity not in selected:
            selected.append(severity)
    return ",".join(selected or ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"])


def trivy_plan_scan(
    path: str | Path,
    *,
    mode: str = "filesystem",
    severities: str | list[str] | None = None,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan a local Trivy scan with all database/network updates disabled."""
    source, error = _source_or_error(path, allowed_root=allowed_root)
    if error:
        return error
    status = trivy_status()
    if not status.get("installed"):
        return {**_missing_tool("trivy"), "input": str(source)}
    scan_mode = str(mode or "filesystem").strip().lower()
    if scan_mode not in {"filesystem", "sbom"}:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_arguments",
            "error": "Trivy mode must be filesystem or sbom",
        }
    subcommand = "fs" if scan_mode == "filesystem" else "sbom"
    args = [
        subcommand,
        "--quiet",
        "--format",
        "json",
        "--exit-code",
        "0",
        "--offline-scan",
        "--skip-db-update",
        "--skip-java-db-update",
    ]
    if scan_mode == "filesystem":
        args.extend(["--scanners", "vuln"])
    args.extend(["--severity", _normalize_severities(severities), str(source)])
    return {
        "ok": True,
        "operation": "vulnerability_scan",
        "tool": "trivy",
        "command": "trivy",
        "args": args,
        "input": str(source),
        "mode": scan_mode,
        "offline": True,
        "risk": "low",
        "requires_local_database": True,
        "notes": [
            "Offline scan and all vulnerability/Java database updates are disabled.",
            "A missing or stale local database is reported; this wrapper never downloads one.",
        ],
    }


def osv_plan_scan(
    path: str | Path,
    *,
    mode: str = "source",
    recursive: bool = True,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    """Plan an OSV-Scanner run that explicitly uses offline mode."""
    source, error = _source_or_error(path, allowed_root=allowed_root)
    if error:
        return error
    status = osv_status()
    if not status.get("installed"):
        return {**_missing_tool("osv-scanner"), "input": str(source)}
    scan_mode = str(mode or "source").strip().lower()
    if scan_mode not in {"source", "sbom"}:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error_code": "invalid_arguments",
            "error": "OSV-Scanner mode must be source or sbom",
        }
    args = ["scan", scan_mode, "--offline", "--format", "json"]
    if scan_mode == "source" and bool(recursive) and source.is_dir():
        args.append("--recursive")
    args.append(str(source))
    return {
        "ok": True,
        "operation": "vulnerability_scan",
        "tool": "osv-scanner",
        "command": "osv-scanner",
        "args": args,
        "input": str(source),
        "mode": scan_mode,
        "offline": True,
        "risk": "low",
        "requires_local_database": True,
        "notes": [
            "The --offline flag prevents OSV.dev API queries.",
            "A pre-populated OSV offline database compatible with the installed CLI is required.",
        ],
    }


def _load_json(value: str | bytes | dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    if isinstance(value, dict):
        return value, ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    if not text.strip():
        return None, "empty json"
    if len(text) > _MAX_JSON_CHARS:
        return None, f"json exceeds parser limit ({_MAX_JSON_CHARS} characters)"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"
    if not isinstance(payload, dict):
        return None, "json root must be an object"
    return payload, ""


def _license_names(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else [value]
    names: list[str] = []
    for row in rows:
        if isinstance(row, str):
            name = row
        elif isinstance(row, dict):
            license_obj = row.get("license") if isinstance(row.get("license"), dict) else {}
            name = (
                row.get("value")
                or row.get("spdxExpression")
                or license_obj.get("id")
                or license_obj.get("name")
                or ""
            )
        else:
            name = ""
        clean = str(name or "").strip()
        if clean and clean not in names:
            names.append(clean[:256])
    return names[:20]


def _syft_packages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        packages: list[dict[str, Any]] = []
        for row in artifacts[:_MAX_COMPONENTS]:
            if not isinstance(row, dict):
                continue
            locations = row.get("locations") if isinstance(row.get("locations"), list) else []
            packages.append({
                "name": str(row.get("name") or "")[:512],
                "version": str(row.get("version") or "")[:256],
                "type": str(row.get("type") or "")[:128],
                "purl": str(row.get("purl") or "")[:2048],
                "licenses": _license_names(row.get("licenses")),
                "locations": [
                    str(item.get("path") or "")[:1024]
                    for item in locations[:10]
                    if isinstance(item, dict) and item.get("path")
                ],
            })
        return packages

    components = payload.get("components")
    if isinstance(components, list):
        packages = []
        for row in components[:_MAX_COMPONENTS]:
            if not isinstance(row, dict):
                continue
            packages.append({
                "name": str(row.get("name") or "")[:512],
                "version": str(row.get("version") or "")[:256],
                "type": str(row.get("type") or "")[:128],
                "purl": str(row.get("purl") or "")[:2048],
                "bom_ref": str(row.get("bom-ref") or "")[:2048],
                "licenses": _license_names(row.get("licenses")),
            })
        return packages

    spdx_packages = payload.get("packages")
    if isinstance(spdx_packages, list):
        packages = []
        for row in spdx_packages[:_MAX_COMPONENTS]:
            if not isinstance(row, dict):
                continue
            purl = ""
            for ref in row.get("externalRefs") or []:
                if (
                    isinstance(ref, dict)
                    and str(ref.get("referenceType") or "").lower() == "purl"
                ):
                    purl = str(ref.get("referenceLocator") or "")
                    break
            packages.append({
                "name": str(row.get("name") or "")[:512],
                "version": str(row.get("versionInfo") or "")[:256],
                "type": "spdx-package",
                "purl": purl[:2048],
                "spdx_id": str(row.get("SPDXID") or "")[:512],
                "licenses": _license_names(
                    row.get("licenseConcluded") or row.get("licenseDeclared")
                ),
            })
        return packages
    return []


def parse_syft_json(value: str | bytes | dict[str, Any]) -> dict[str, Any]:
    payload, error = _load_json(value)
    if payload is None:
        return {
            "ok": False,
            "parse_error": error,
            "package_count": 0,
            "packages": [],
        }
    packages = _syft_packages(payload)
    types = Counter(str(row.get("type") or "unknown") for row in packages)
    return {
        "ok": True,
        "format": (
            "syft-json"
            if isinstance(payload.get("artifacts"), list)
            else ("cyclonedx-json" if isinstance(payload.get("components"), list) else "spdx-json")
        ),
        "package_count": len(packages),
        "packages": packages[:_MAX_RESULT_ITEMS],
        "packages_truncated": len(packages) > _MAX_RESULT_ITEMS,
        "types": dict(types.most_common(50)),
    }


def parse_trivy_json(value: str | bytes | dict[str, Any]) -> dict[str, Any]:
    payload, error = _load_json(value)
    if payload is None:
        return {
            "ok": False,
            "parse_error": error,
            "finding_count": 0,
            "findings": [],
        }
    results = payload.get("Results") if isinstance(payload.get("Results"), list) else []
    findings: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for result in results[:_MAX_COMPONENTS]:
        if not isinstance(result, dict):
            continue
        vulnerabilities = (
            result.get("Vulnerabilities")
            if isinstance(result.get("Vulnerabilities"), list)
            else []
        )
        target = str(result.get("Target") or "")[:1024]
        targets.append({
            "target": target,
            "class": str(result.get("Class") or "")[:128],
            "type": str(result.get("Type") or "")[:128],
            "vulnerability_count": len(vulnerabilities),
        })
        for row in vulnerabilities:
            if not isinstance(row, dict) or len(findings) >= _MAX_COMPONENTS:
                continue
            findings.append({
                "id": str(row.get("VulnerabilityID") or "")[:256],
                "package": str(row.get("PkgName") or "")[:512],
                "installed_version": str(row.get("InstalledVersion") or "")[:256],
                "fixed_version": str(row.get("FixedVersion") or "")[:256],
                "severity": str(row.get("Severity") or "UNKNOWN").upper()[:32],
                "title": str(row.get("Title") or "")[:_MAX_TEXT_FIELD],
                "target": target,
                "primary_url": str(row.get("PrimaryURL") or "")[:2048],
            })
    severities = Counter(row["severity"] for row in findings)
    return {
        "ok": True,
        "finding_count": len(findings),
        "findings": findings[:_MAX_RESULT_ITEMS],
        "findings_truncated": len(findings) > _MAX_RESULT_ITEMS,
        "severity_counts": dict(severities),
        "target_count": len(targets),
        "targets": targets[:100],
        "targets_truncated": len(targets) > 100,
    }


def parse_osv_json(value: str | bytes | dict[str, Any]) -> dict[str, Any]:
    payload, error = _load_json(value)
    if payload is None:
        return {
            "ok": False,
            "parse_error": error,
            "finding_count": 0,
            "findings": [],
        }
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    findings: list[dict[str, Any]] = []
    packages_seen: set[tuple[str, str, str]] = set()
    for result in results[:_MAX_COMPONENTS]:
        if not isinstance(result, dict):
            continue
        package_rows = result.get("packages")
        if not isinstance(package_rows, list):
            package_rows = [result] if result.get("package") else []
        source = result.get("source") if isinstance(result.get("source"), dict) else {}
        source_path = str(source.get("path") or source.get("target") or "")[:1024]
        for package_row in package_rows:
            if not isinstance(package_row, dict):
                continue
            package = (
                package_row.get("package")
                if isinstance(package_row.get("package"), dict)
                else package_row
            )
            name = str(package.get("name") or "")[:512]
            version = str(package.get("version") or "")[:256]
            ecosystem = str(package.get("ecosystem") or "")[:128]
            packages_seen.add((ecosystem, name, version))
            vulnerabilities = package_row.get("vulnerabilities")
            if not isinstance(vulnerabilities, list):
                vulnerabilities = package_row.get("groups")
            if not isinstance(vulnerabilities, list):
                vulnerabilities = []
            for vulnerability in vulnerabilities:
                if not isinstance(vulnerability, dict) or len(findings) >= _MAX_COMPONENTS:
                    continue
                identifiers = vulnerability.get("ids")
                if isinstance(identifiers, list) and identifiers:
                    vulnerability_id = identifiers[0]
                    aliases = identifiers[1:]
                else:
                    vulnerability_id = vulnerability.get("id") or ""
                    aliases = vulnerability.get("aliases") or []
                findings.append({
                    "id": str(vulnerability_id or "")[:256],
                    "aliases": [str(item)[:256] for item in aliases[:20]],
                    "package": name,
                    "version": version,
                    "ecosystem": ecosystem,
                    "summary": str(vulnerability.get("summary") or "")[:_MAX_TEXT_FIELD],
                    "source": source_path,
                })
    return {
        "ok": True,
        "package_count": len(packages_seen),
        "finding_count": len(findings),
        "findings": findings[:_MAX_RESULT_ITEMS],
        "findings_truncated": len(findings) > _MAX_RESULT_ITEMS,
    }


def _purl_identity(purl: str) -> str:
    value = str(purl or "").strip().lower()
    if not value:
        return ""
    base = value.split("#", 1)[0].split("?", 1)[0]
    at = base.rfind("@")
    slash = base.rfind("/")
    if at > slash:
        base = base[:at]
    return base


def _package_identity(package: dict[str, Any]) -> str:
    purl = _purl_identity(str(package.get("purl") or ""))
    if purl:
        return purl
    package_type = str(
        package.get("ecosystem") or package.get("type") or "unknown"
    ).strip().lower()
    name = str(package.get("name") or package.get("package") or "").strip().lower()
    return f"{package_type}:{name}" if name else ""


def _normalized_sbom_packages(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = _syft_packages(payload)
    if not rows and isinstance(payload.get("packages"), list):
        for row in payload["packages"][:_MAX_COMPONENTS]:
            if isinstance(row, dict):
                rows.append({
                    "name": str(row.get("name") or row.get("package") or "")[:512],
                    "version": str(
                        row.get("version")
                        or row.get("versionInfo")
                        or row.get("installed_version")
                        or ""
                    )[:256],
                    "type": str(row.get("type") or row.get("ecosystem") or "")[:128],
                    "purl": str(row.get("purl") or "")[:2048],
                    "licenses": _license_names(row.get("licenses")),
                })
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = _package_identity(row)
        if identity:
            indexed[identity] = {
                "identity": identity,
                "name": row.get("name") or "",
                "version": row.get("version") or "",
                "type": row.get("type") or "",
                "purl": row.get("purl") or "",
                "licenses": row.get("licenses") or [],
            }
    return indexed


def sbom_diff(
    old_sbom: str | bytes | dict[str, Any],
    new_sbom: str | bytes | dict[str, Any],
    *,
    max_items: int = _MAX_RESULT_ITEMS,
) -> dict[str, Any]:
    """Diff Syft, CycloneDX, or SPDX JSON by stable package identity."""
    old_payload, old_error = _load_json(old_sbom)
    new_payload, new_error = _load_json(new_sbom)
    if old_payload is None or new_payload is None:
        return {
            "ok": False,
            "error_code": "invalid_sbom",
            "error": "; ".join(
                item
                for item in (
                    f"old: {old_error}" if old_error else "",
                    f"new: {new_error}" if new_error else "",
                )
                if item
            ),
        }
    old = _normalized_sbom_packages(old_payload)
    new = _normalized_sbom_packages(new_payload)
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for key in sorted(old.keys() & new.keys()):
        before = old[key]
        after = new[key]
        changes = {}
        for field in ("version", "licenses", "purl"):
            if before.get(field) != after.get(field):
                changes[field] = {
                    "old": before.get(field),
                    "new": after.get(field),
                }
        if changes:
            changed.append({
                "identity": key,
                "name": after.get("name") or before.get("name"),
                "changes": changes,
            })
        else:
            unchanged += 1
    cap = _bounded_int(max_items, _MAX_RESULT_ITEMS, 1, 1000)
    return {
        "ok": True,
        "old_package_count": len(old),
        "new_package_count": len(new),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "unchanged_count": unchanged,
        "added": added[:cap],
        "removed": removed[:cap],
        "changed": changed[:cap],
        "truncated": len(added) > cap or len(removed) > cap or len(changed) > cap,
        "summary": (
            f"SBOM diff: +{len(added)} -{len(removed)} "
            f"~{len(changed)} ={unchanged}"
        ),
    }


def _offline_database_error(result: dict[str, Any], tool: str) -> dict[str, Any]:
    if result.get("ok"):
        return result
    text = " ".join(
        str(result.get(key) or "")
        for key in ("error", "stderr_excerpt", "stdout_excerpt")
    ).lower()
    database_terms = ("database", "db", "offline")
    missing_terms = ("missing", "not found", "no such", "download", "initialize")
    if any(term in text for term in database_terms) and any(
        term in text for term in missing_terms
    ):
        result.update({
            "blocked": True,
            "do_not_retry": True,
            "can_install": False,
            "error_code": "offline_database_unavailable",
            "suggested_fix": (
                f"Pre-populate the {tool} database outside ArgusAI, then retry. "
                "This wrapper will not use the network or download databases."
            ),
        })
    return result


async def _run_plan(
    plan: dict[str, Any],
    *,
    timeout_seconds: int,
    parser: Any,
) -> dict[str, Any]:
    if not plan.get("ok"):
        return plan
    timeout = _bounded_int(timeout_seconds, _DEFAULT_TIMEOUT, 10, _MAX_TIMEOUT)
    result = await CommandRunner().run(
        CommandRequest(
            str(plan["command"]),
            list(plan["args"]),
            "local.offline",
            [],
            str(plan.get("risk") or "low"),
            float(timeout),
        )
    )
    result["input"] = plan["input"]
    result["operation"] = plan.get("operation")
    result["offline"] = True
    tool = str(plan["tool"])
    if not result.get("ok") and "tool not found" in str(result.get("error") or "").lower():
        return {**result, **_missing_tool(tool)}
    result["parsed"] = parser(str(result.get("stdout_excerpt") or ""))
    if tool in {"trivy", "osv-scanner"}:
        _offline_database_error(result, tool)
    return result


async def syft_run_inventory(
    path: str | Path,
    *,
    output_format: str = "cyclonedx-json",
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = syft_plan_inventory(
        path,
        output_format=output_format,
        allowed_root=allowed_root,
    )
    return await _run_plan(
        plan,
        timeout_seconds=timeout_seconds,
        parser=parse_syft_json,
    )


async def trivy_run_scan(
    path: str | Path,
    *,
    mode: str = "filesystem",
    severities: str | list[str] | None = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = trivy_plan_scan(
        path,
        mode=mode,
        severities=severities,
        allowed_root=allowed_root,
    )
    return await _run_plan(
        plan,
        timeout_seconds=timeout_seconds,
        parser=parse_trivy_json,
    )


async def osv_run_scan(
    path: str | Path,
    *,
    mode: str = "source",
    recursive: bool = True,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    plan = osv_plan_scan(
        path,
        mode=mode,
        recursive=recursive,
        allowed_root=allowed_root,
    )
    return await _run_plan(
        plan,
        timeout_seconds=timeout_seconds,
        parser=parse_osv_json,
    )


# Explicit aliases make the intended SBOM workflow discoverable without adding
# another implementation surface.
syft_plan_sbom = syft_plan_inventory
syft_generate_sbom = syft_run_inventory
