"""Offline-first Active Directory assessment helpers.

The module imports existing reports and builds tightly allowlisted read-only
plans. It never accepts passwords, performs spraying/relay, or exposes
credential/ticket abuse commands.
"""

from __future__ import annotations

import json
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from argusai.core.infra.command import CommandRequest, CommandRunner

_FQDN_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_MAX_REPORT_BYTES = 64 * 1024 * 1024


def _blocked(reason: str, error: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "blocked": True,
        "reason": reason,
        "error": error,
        "do_not_retry": reason in {"invalid_input", "unsafe_operation", "tool_unavailable"},
        **extra,
    }


def ad_tool_inventory() -> dict[str, Any]:
    """Probe supported AD report/query tools without executing them."""
    commands = {
        "certipy": ("certipy",),
        "ldapsearch": ("ldapsearch",),
        "pingcastle": ("PingCastle", "PingCastle.exe"),
        "sharphound": ("SharpHound", "SharpHound.exe"),
    }
    tools: dict[str, dict[str, Any]] = {}
    for name, candidates in commands.items():
        path = next((str(shutil.which(command) or "") for command in candidates if shutil.which(command)), "")
        tools[name] = {"installed": bool(path), "path": path}
    return {
        "ok": True,
        "tools": tools,
        "supported_execution": ["certipy find with Kerberos/no-pass only"],
        "preferred_mode": "import existing PingCastle/SharpHound reports",
        "forbidden": ["password spray", "relay", "dcsync", "ticket forging", "lateral movement"],
    }


def certipy_find_plan(
    domain: str,
    *,
    dc_host: str = "",
    username: str = "",
    output_prefix: str = "argusai-adcs",
) -> dict[str, Any]:
    """Plan Certipy's read-only ``find`` command using Kerberos/no-pass auth."""
    domain_name = str(domain or "").strip().lower()
    if not _FQDN_RE.fullmatch(domain_name):
        return _blocked("invalid_input", "domain must be a DNS domain name")
    dc = str(dc_host or "").strip()
    if dc and not _HOST_RE.fullmatch(dc):
        return _blocked("invalid_input", "dc_host contains unsupported characters")
    user = str(username or "").strip()
    if user and any(ch in user for ch in ("\n", "\r", "\x00")):
        return _blocked("invalid_input", "username contains control characters")
    path = str(shutil.which("certipy") or "")
    if not path:
        return _blocked(
            "tool_unavailable",
            "certipy is not installed on PATH",
            external_tool_required="certipy",
            can_install=False,
            docs_url="https://github.com/ly4k/Certipy",
        )
    args = ["find", "-k", "-no-pass", "-json", "-output", re.sub(r"[^A-Za-z0-9._-]", "_", output_prefix)[:80]]
    if user:
        args.extend(["-u", f"{user}@{domain_name}"])
    if dc:
        args.extend(["-dc-ip", dc])
    return {
        "ok": True,
        "tool": "certipy",
        "command": path,
        "args": args,
        "target": dc or domain_name,
        "domain": domain_name,
        "risk": "medium",
        "read_only": True,
        "credential_source": "existing Kerberos cache; passwords are not accepted",
    }


async def certipy_find_run(
    domain: str,
    *,
    dc_host: str = "",
    username: str = "",
    output_prefix: str = "argusai-adcs",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run an approved Certipy ``find`` plan with no password argument."""
    plan = certipy_find_plan(
        domain,
        dc_host=dc_host,
        username=username,
        output_prefix=output_prefix,
    )
    if not plan.get("ok"):
        return plan
    result = await CommandRunner().run(CommandRequest(
        plan["command"],
        plan["args"],
        plan["target"],
        [plan["domain"], plan["target"]],
        "medium",
        float(max(30, min(int(timeout_seconds or 300), 900))),
    ))
    result["plan"] = plan
    return result


def _safe_report_path(path: str) -> tuple[Path | None, dict[str, Any] | None]:
    target = Path(str(path or "")).expanduser().resolve()
    if not target.is_file():
        return None, _blocked("invalid_input", f"report file not found: {target}")
    if target.stat().st_size > _MAX_REPORT_BYTES:
        return None, _blocked("invalid_input", "report exceeds the 64 MiB import limit")
    return target, None


def parse_sharphound_report(path: str) -> dict[str, Any]:
    """Summarize SharpHound JSON/ZIP without extracting files or returning identities."""
    target, error = _safe_report_path(path)
    if error:
        return error
    assert target is not None
    documents: list[tuple[str, bytes]] = []
    try:
        if target.suffix.lower() == ".zip":
            with zipfile.ZipFile(target) as archive:
                safe_infos = [
                    info for info in archive.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".json")
                    and info.file_size <= _MAX_REPORT_BYTES
                ][:100]
                if sum(info.file_size for info in safe_infos) > _MAX_REPORT_BYTES:
                    return _blocked("invalid_input", "uncompressed SharpHound JSON exceeds the import limit")
                for info in safe_infos:
                    documents.append((Path(info.filename).name, archive.read(info)))
        else:
            documents.append((target.name, target.read_bytes()))
    except (OSError, zipfile.BadZipFile) as exc:
        return _blocked("invalid_input", f"unable to read SharpHound report: {type(exc).__name__}")

    type_counts: Counter[str] = Counter()
    relationship_counts: Counter[str] = Counter()
    object_count = 0
    for filename, raw in documents:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        rows = rows if isinstance(rows, list) else []
        meta = payload.get("meta") if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) else {}
        object_type = str(meta.get("type") or Path(filename).stem.split("_")[-1] or "unknown").lower()
        type_counts[object_type] += len(rows)
        object_count += len(rows)
        for row in rows[:10000]:
            if not isinstance(row, dict):
                continue
            for key, value in row.items():
                if isinstance(value, list) and key.lower() in {
                    "members", "aces", "sessions", "localadmins", "remotedesktopusers",
                    "dcomusers", "psremoteusers", "links",
                }:
                    relationship_counts[key.lower()] += len(value)
    return {
        "ok": True,
        "format": "sharphound",
        "file_count": len(documents),
        "object_count": object_count,
        "object_type_counts": dict(type_counts),
        "relationship_counts": dict(relationship_counts),
        "identities_redacted": True,
    }


def parse_pingcastle_report(path: str) -> dict[str, Any]:
    """Extract bounded risk summaries from an existing PingCastle XML report."""
    target, error = _safe_report_path(path)
    if error:
        return error
    assert target is not None
    try:
        root = ElementTree.fromstring(target.read_bytes())
    except (ElementTree.ParseError, OSError) as exc:
        return _blocked("invalid_input", f"unable to parse PingCastle XML: {type(exc).__name__}")
    scores: dict[str, int] = {}
    for tag in ("GlobalScore", "StaleObjectsScore", "PrivilegedGroupScore", "TrustScore", "AnomalyScore"):
        node = root.find(f".//{tag}")
        try:
            scores[tag] = int((node.text if node is not None else "0") or 0)
        except ValueError:
            scores[tag] = 0
    rules: list[dict[str, Any]] = []
    for node in root.findall(".//HealthcheckRiskRule")[:500]:
        rule_id = str(node.findtext("RiskId") or node.get("RiskId") or "")
        points = str(node.findtext("Points") or node.get("Points") or "0")
        rules.append({"id": rule_id, "points": int(points) if points.isdigit() else 0})
    return {
        "ok": True,
        "format": "pingcastle-xml",
        "scores": scores,
        "rule_count": len(rules),
        "top_rules": sorted(rules, key=lambda row: row["points"], reverse=True)[:50],
        "identities_redacted": True,
    }
