"""Safe wrappers and parsers for cloud and Kubernetes assessment tools.

Only report-producing/read-only command shapes are exposed.  The module never
accepts arbitrary CLI arguments and deliberately excludes kubectl mutation,
exec, port-forward, secret, and log access.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from typing import Any

from argusai.core.infra.command import CommandRequest, CommandRunner

_SEVERITIES = frozenset({"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
_TRIVY_SCAN_TYPES = frozenset({"image", "filesystem", "repository"})
_PROWLER_PROVIDERS = frozenset({"aws", "azure", "gcp", "kubernetes", "m365"})
_KUBECTL_VERBS = frozenset({"get", "describe", "version", "cluster-info", "api-resources"})
_KUBECTL_RESOURCES = frozenset({
    "namespaces", "nodes", "pods", "services", "endpoints",
    "deployments", "replicasets", "statefulsets", "daemonsets", "jobs", "cronjobs",
    "ingresses", "networkpolicies", "poddisruptionbudgets",
    "serviceaccounts", "roles", "rolebindings", "clusterroles", "clusterrolebindings",
    "persistentvolumes", "persistentvolumeclaims", "storageclasses",
})
_KUBECTL_ALIASES = {
    "ns": "namespaces",
    "no": "nodes",
    "po": "pods",
    "svc": "services",
    "ep": "endpoints",
    "deploy": "deployments",
    "rs": "replicasets",
    "sts": "statefulsets",
    "ds": "daemonsets",
    "cj": "cronjobs",
    "ing": "ingresses",
    "netpol": "networkpolicies",
    "sa": "serviceaccounts",
    "role": "roles",
    "rolebinding": "rolebindings",
    "clusterrole": "clusterroles",
    "clusterrolebinding": "clusterrolebindings",
    "pv": "persistentvolumes",
    "pvc": "persistentvolumeclaims",
    "sc": "storageclasses",
}
_MUTATING_KUBECTL = frozenset({
    "apply", "create", "delete", "edit", "exec", "expose", "patch", "replace",
    "rollout", "run", "scale", "set", "taint", "cordon", "uncordon", "drain",
    "attach", "cp", "port-forward", "proxy", "debug", "label", "annotate",
})


def _blocked(tool: str, reason: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "tool": tool,
        "reason": reason,
        "error": error,
        "do_not_retry": reason in {"unsafe_operation", "invalid_input", "tool_unavailable"},
        **extra,
    }


def _tool_path(tool: str) -> str:
    return str(shutil.which(tool) or "")


def cloud_tool_inventory() -> dict[str, Any]:
    """Return host availability without invoking any cloud command."""
    tools = {
        name: {"installed": bool(path := _tool_path(name)), "path": path}
        for name in ("trivy", "prowler", "kubectl")
    }
    return {
        "ok": True,
        "tools": tools,
        "safety": {
            "read_only": True,
            "credential_source": "host environment/profile/kubeconfig; never command output",
            "forbidden": sorted(_MUTATING_KUBECTL | {"credential flags", "secret reads"}),
        },
    }


def _require_tool(tool: str) -> dict[str, Any] | None:
    if _tool_path(tool):
        return None
    return _blocked(
        tool,
        "tool_unavailable",
        f"{tool} is not installed on PATH",
        external_tool_required=tool,
        can_install=False,
        suggested_fix=f"Install {tool} on the host and verify it is available on PATH.",
    )


def _normalize_severities(value: str | list[str] | None) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "HIGH,CRITICAL").replace(" ", ",").split(",")
    return [item for item in dict.fromkeys(str(v).strip().upper() for v in raw) if item in _SEVERITIES]


def trivy_plan_scan(
    target: str,
    *,
    scan_type: str = "image",
    severities: str | list[str] | None = "HIGH,CRITICAL",
    ignore_unfixed: bool = False,
    offline: bool = True,
) -> dict[str, Any]:
    """Build a bounded Trivy report command; never executes it."""
    kind = str(scan_type or "image").strip().lower()
    name = str(target or "").strip()
    if kind not in _TRIVY_SCAN_TYPES:
        return _blocked("trivy", "invalid_input", f"unsupported scan_type: {kind}")
    if not name:
        return _blocked("trivy", "invalid_input", "target is required")
    unavailable = _require_tool("trivy")
    if unavailable:
        return unavailable
    severity_list = _normalize_severities(severities)
    if not severity_list:
        return _blocked("trivy", "invalid_input", "at least one valid severity is required")

    subcommand = {"filesystem": "fs", "repository": "repository"}.get(kind, kind)
    scanners = "vuln,secret" if kind == "image" else "vuln,misconfig,secret"
    args = [
        subcommand,
        "--format", "json",
        "--no-progress",
        "--scanners", scanners,
        "--severity", ",".join(severity_list),
    ]
    if ignore_unfixed:
        args.append("--ignore-unfixed")
    if offline:
        args.append("--skip-db-update")
        if kind != "image":
            args.append("--skip-check-update")
    args.append(name)
    return {
        "ok": True,
        "tool": "trivy",
        "command": "trivy",
        "args": args,
        "target": name,
        "scan_type": kind,
        "risk": "low",
        "read_only": True,
        "offline": bool(offline),
        "notes": ["Produces JSON only; it does not modify the image, repository, or filesystem."],
    }


def _json_value(payload: str | bytes | dict[str, Any] | list[Any]) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload or "")
    return json.loads(text)


def parse_trivy_json(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Normalize Trivy vulnerabilities, misconfigurations, and secret hits."""
    try:
        data = _json_value(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid Trivy JSON: {exc}", "findings": [], "finding_count": 0}
    results = data.get("Results") if isinstance(data, dict) else []
    if not isinstance(results, list):
        results = []
    findings: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        location = str(result.get("Target") or "")
        for key, finding_type in (
            ("Vulnerabilities", "vulnerability"),
            ("Misconfigurations", "misconfiguration"),
            ("Secrets", "secret"),
        ):
            rows = result.get(key) or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                finding_id = (
                    row.get("VulnerabilityID")
                    or row.get("ID")
                    or row.get("RuleID")
                    or row.get("Title")
                    or "unknown"
                )
                findings.append({
                    "type": finding_type,
                    "id": str(finding_id),
                    "title": str(row.get("Title") or row.get("PkgName") or finding_id),
                    "severity": str(row.get("Severity") or "UNKNOWN").lower(),
                    "target": location,
                    "package": str(row.get("PkgName") or ""),
                    "installed_version": str(row.get("InstalledVersion") or ""),
                    "fixed_version": str(row.get("FixedVersion") or ""),
                    "status": str(row.get("Status") or ""),
                    "message": str(row.get("Message") or row.get("Description") or "")[:1000],
                })
    counts = Counter(item["severity"] for item in findings)
    return {
        "ok": True,
        "format": "trivy-json",
        "finding_count": len(findings),
        "findings": findings[:500],
        "truncated": len(findings) > 500,
        "severity_counts": dict(counts),
        "artifact_name": str(data.get("ArtifactName") or "") if isinstance(data, dict) else "",
    }


async def trivy_run_scan(
    target: str,
    *,
    scan_type: str = "image",
    severities: str | list[str] | None = "HIGH,CRITICAL",
    ignore_unfixed: bool = False,
    offline: bool = True,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    plan = trivy_plan_scan(
        target,
        scan_type=scan_type,
        severities=severities,
        ignore_unfixed=ignore_unfixed,
        offline=offline,
    )
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        "localhost",
        [],
        "low",
        float(max(30, min(int(timeout_seconds or 300), 1800))),
    ))
    result["plan"] = plan
    result["parsed"] = parse_trivy_json(str(result.get("stdout_excerpt") or ""))
    if result.get("truncated"):
        result["parsed"]["partial"] = True
        result["parsed"]["warning"] = "Command output was truncated before parsing."
    return result


def prowler_plan_scan(
    provider: str,
    *,
    profile: str = "",
    regions: str | list[str] | None = None,
    checks: str | list[str] | None = None,
    output_directory: str = "",
) -> dict[str, Any]:
    """Build a Prowler report-only command using host-managed credentials."""
    cloud = str(provider or "").strip().lower()
    if cloud not in _PROWLER_PROVIDERS:
        return _blocked("prowler", "invalid_input", f"unsupported provider: {cloud}")
    unavailable = _require_tool("prowler")
    if unavailable:
        return unavailable
    args = [cloud, "--output-formats", "json-ocsf"]
    if profile.strip():
        args.extend(["--profile", profile.strip()])
    region_list = [str(item).strip() for item in (regions if isinstance(regions, list) else str(regions or "").split(",")) if str(item).strip()]
    if region_list:
        args.extend(["--region", *region_list[:20]])
    check_list = [str(item).strip() for item in (checks if isinstance(checks, list) else str(checks or "").split(",")) if str(item).strip()]
    if check_list:
        args.extend(["--checks", *check_list[:100]])
    if output_directory.strip():
        args.extend(["--output-directory", output_directory.strip()])
    return {
        "ok": True,
        "tool": "prowler",
        "command": "prowler",
        "args": args,
        "target": cloud,
        "provider": cloud,
        "risk": "low",
        "read_only": True,
        "credential_source": "host profile/environment only",
        "notes": ["No access key, client secret, password, or token is accepted by this wrapper."],
    }


def parse_prowler_json(payload: str | bytes | dict[str, Any] | list[Any]) -> dict[str, Any]:
    """Normalize Prowler native or OCSF JSON/JSONL report rows."""
    try:
        data = _json_value(payload)
        rows = data if isinstance(data, list) else data.get("findings", data.get("Findings", [data]))
    except json.JSONDecodeError:
        rows = []
        for line in str(payload or "").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid Prowler JSON: {exc}", "findings": [], "finding_count": 0}
    if not isinstance(rows, list):
        rows = []
    findings: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        finding_info = row.get("finding_info") if isinstance(row.get("finding_info"), dict) else {}
        resources = row.get("resources") if isinstance(row.get("resources"), list) else []
        resource = resources[0] if resources and isinstance(resources[0], dict) else {}
        status = str(row.get("Status") or row.get("status") or row.get("status_code") or "")
        severity = str(row.get("Severity") or row.get("severity") or row.get("severity_id") or "unknown")
        findings.append({
            "id": str(row.get("CheckID") or row.get("check_id") or finding_info.get("uid") or row.get("uid") or ""),
            "title": str(row.get("CheckTitle") or row.get("check_title") or finding_info.get("title") or row.get("message") or ""),
            "status": status.lower(),
            "severity": severity.lower(),
            "provider": str(row.get("Provider") or row.get("cloud_provider") or row.get("cloud", {}).get("provider", "")),
            "account": str(row.get("AccountId") or row.get("account_uid") or row.get("cloud", {}).get("account", {}).get("uid", "")),
            "region": str(row.get("Region") or row.get("region") or row.get("cloud", {}).get("region", "")),
            "resource": str(row.get("ResourceName") or row.get("resource_name") or resource.get("name") or resource.get("uid") or ""),
            "remediation": str(row.get("Remediation") or row.get("remediation") or "")[:1000],
        })
    failed = [item for item in findings if item["status"] in {"fail", "failed", "new"}]
    return {
        "ok": True,
        "format": "prowler-json",
        "finding_count": len(findings),
        "failed_count": len(failed),
        "findings": findings[:500],
        "severity_counts": dict(Counter(item["severity"] for item in failed)),
        "truncated": len(findings) > 500,
    }


async def prowler_run_scan(
    provider: str,
    *,
    profile: str = "",
    regions: str | list[str] | None = None,
    checks: str | list[str] | None = None,
    output_directory: str = "",
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    plan = prowler_plan_scan(
        provider,
        profile=profile,
        regions=regions,
        checks=checks,
        output_directory=output_directory,
    )
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        plan["provider"],
        [],
        "low",
        float(max(60, min(int(timeout_seconds or 900), 3600))),
    ))
    result["plan"] = plan
    result["parsed"] = parse_prowler_json(str(result.get("stdout_excerpt") or ""))
    return result


def _normalize_resource(resource: str) -> str:
    raw = str(resource or "").strip().lower()
    base = raw.split("/", 1)[0].split(".", 1)[0]
    return _KUBECTL_ALIASES.get(base, base)


def kubectl_readonly_plan(
    verb: str,
    *,
    resource: str = "",
    name: str = "",
    namespace: str = "",
    all_namespaces: bool = False,
    context: str = "",
) -> dict[str, Any]:
    """Build one kubectl command from a strict read-only allowlist."""
    action = str(verb or "").strip().lower()
    if action in _MUTATING_KUBECTL or action in {"logs", "secrets", "secret", "auth"}:
        return _blocked("kubectl", "unsafe_operation", f"kubectl {action} is forbidden by policy")
    if action not in _KUBECTL_VERBS:
        return _blocked("kubectl", "invalid_input", f"kubectl verb is not allowlisted: {action}")
    normalized = _normalize_resource(resource)
    if action in {"get", "describe"}:
        if normalized not in _KUBECTL_RESOURCES:
            return _blocked("kubectl", "unsafe_operation", f"kubectl resource is not allowlisted: {resource}")
    unavailable = _require_tool("kubectl")
    if unavailable:
        return unavailable

    args: list[str] = []
    if context.strip():
        args.extend(["--context", context.strip()])
    args.append(action)
    if action in {"get", "describe"}:
        args.append(normalized)
        if name.strip():
            args.append(name.strip())
        if all_namespaces and normalized not in {"nodes", "namespaces", "clusterroles", "clusterrolebindings", "persistentvolumes", "storageclasses"}:
            args.append("--all-namespaces")
        elif namespace.strip():
            args.extend(["--namespace", namespace.strip()])
        if action == "get":
            args.extend(["--output", "json"])
    return {
        "ok": True,
        "tool": "kubectl",
        "command": "kubectl",
        "args": args,
        "target": context.strip() or "current-context",
        "verb": action,
        "resource": normalized,
        "risk": "low",
        "read_only": True,
        "notes": ["Secrets, logs, exec, apply, delete, patch, and all mutation commands are excluded."],
    }


def parse_kubectl_json(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Summarize Kubernetes JSON without returning object specs or secret data."""
    try:
        data = _json_value(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid kubectl JSON: {exc}", "resources": [], "resource_count": 0}
    if not isinstance(data, dict):
        return {"ok": False, "error": "kubectl JSON root must be an object", "resources": [], "resource_count": 0}
    items = data.get("items") if isinstance(data.get("items"), list) else [data]
    resources: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or data.get("kind") or "").removesuffix("List")
        if kind.lower() == "secret":
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        resources.append({
            "kind": kind,
            "name": str(meta.get("name") or ""),
            "namespace": str(meta.get("namespace") or ""),
            "labels": dict(list((meta.get("labels") or {}).items())[:20]) if isinstance(meta.get("labels"), dict) else {},
            "phase": str(status.get("phase") or ""),
            "ready_replicas": status.get("readyReplicas"),
            "replicas": status.get("replicas"),
            "service_type": str(spec.get("type") or "") if kind.lower() == "service" else "",
            "policy_types": list(spec.get("policyTypes") or []) if kind.lower() == "networkpolicy" else [],
        })
    return {
        "ok": True,
        "format": "kubernetes-json-summary",
        "resource_count": len(resources),
        "resources": resources[:500],
        "truncated": len(resources) > 500,
        "secret_objects_omitted": True,
    }


async def kubectl_readonly_run(
    verb: str,
    *,
    resource: str = "",
    name: str = "",
    namespace: str = "",
    all_namespaces: bool = False,
    context: str = "",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    plan = kubectl_readonly_plan(
        verb,
        resource=resource,
        name=name,
        namespace=namespace,
        all_namespaces=all_namespaces,
        context=context,
    )
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        "kubernetes.local",
        [],
        "low",
        float(max(10, min(int(timeout_seconds or 60), 300))),
    ))
    result["plan"] = plan
    if plan["verb"] == "get":
        result["parsed"] = parse_kubectl_json(str(result.get("stdout_excerpt") or ""))
    return result


# Stable descriptive aliases for callers that prefer domain-specific names.
trivy_cloud_plan = trivy_plan_scan
trivy_cloud_scan = trivy_run_scan
prowler_cloud_plan = prowler_plan_scan
prowler_cloud_scan = prowler_run_scan
