"""Read-only local attack-surface snapshot for defensive workflows and baselines."""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import subprocess
from typing import Any


def _run(cmd: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode == 0, out.strip()
    except Exception as exc:
        return False, str(exc)


def _split_endpoint(value: str) -> tuple[str, int] | None:
    endpoint = str(value or "").strip()
    if not endpoint:
        return None
    if endpoint.startswith("["):
        close = endpoint.rfind("]")
        if close < 0 or close + 1 >= len(endpoint) or endpoint[close + 1] != ":":
            return None
        host = endpoint[1:close]
        port_text = endpoint[close + 2:]
    else:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator:
            return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not 0 <= port <= 65535:
        return None
    return host or "*", port


def _parse_windows_netstat(out: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() not in {"TCP", "UDP"}:
            continue
        proto = parts[0].upper()
        if proto == "TCP":
            if len(parts) < 5:
                continue
            state = parts[-2].upper()
            if state not in {"LISTENING", "LISTEN"}:
                continue
        else:
            state = "LISTEN"
        endpoint = _split_endpoint(parts[1])
        if endpoint is None:
            continue
        pid = parts[-1] if parts[-1].isdigit() else ""
        host, port = endpoint
        rows.append({
            "proto": proto,
            "address": host,
            "port": port,
            "state": state,
            "pid": pid,
        })
    return rows


def _listening_windows() -> list[dict[str, Any]]:
    # Query both protocols explicitly: ``netstat -p tcp`` omits UDP.
    rows: list[dict[str, Any]] = []
    for protocol in ("tcp", "udp"):
        ok, out = _run(["netstat", "-ano", "-p", protocol], timeout=25)
        if ok or out:
            rows.extend(_parse_windows_netstat(out))
    return rows


def _parse_ss_output(out: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0].lower()
        if not proto.startswith(("tcp", "udp")):
            continue
        state = parts[1].upper()
        if proto.startswith("tcp") and state != "LISTEN":
            continue
        endpoint = _split_endpoint(parts[4])
        if endpoint is None:
            continue
        host, port = endpoint
        rows.append({
            "proto": "TCP" if proto.startswith("tcp") else "UDP",
            "address": host,
            "port": port,
            "state": "LISTEN",
            "pid": "",
        })
    return rows


def _parse_posix_netstat_output(out: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].lower()
        if not proto.startswith(("tcp", "udp")):
            continue
        if proto.startswith("tcp"):
            if len(parts) < 6 or parts[-1].upper() not in {"LISTEN", "LISTENING"}:
                continue
        endpoint = _split_endpoint(parts[3])
        if endpoint is None:
            continue
        host, port = endpoint
        rows.append({
            "proto": "TCP" if proto.startswith("tcp") else "UDP",
            "address": host,
            "port": port,
            "state": "LISTEN",
            "pid": "",
        })
    return rows


def _listening_posix() -> list[dict[str, Any]]:
    ok, out = _run(["ss", "-tuln"], timeout=20)
    if ok:
        return _parse_ss_output(out)
    ok, out = _run(["netstat", "-tuln"], timeout=20)
    if ok or out:
        return _parse_posix_netstat_output(out)
    return []


_HIGH_RISK_PORTS = {
    21, 23, 135, 139, 445, 3389, 5900, 6379, 9200, 27017, 11211, 2375, 5985, 5986,
}


def collect_local_attack_surface(*, include_firewall: bool = True) -> dict[str, Any]:
    system = platform.system()
    hostname = socket.gethostname()
    listeners = _listening_windows() if system.lower().startswith("win") else _listening_posix()
    # Dedupe by proto/address/port
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in listeners:
        key = (row.get("proto"), row.get("address"), row.get("port"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: (int(r.get("port") or 0), str(r.get("proto"))))

    risky = [r for r in unique if int(r.get("port") or 0) in _HIGH_RISK_PORTS]
    hints: list[str] = []
    for row in risky[:12]:
        hints.append(
            f"High-interest listen {row.get('proto')} {row.get('address')}:{row.get('port')} "
            f"(pid={row.get('pid') or '?'})"
        )
    if any(str(r.get("address") or "") in {"0.0.0.0", "::", "*"} for r in unique):
        hints.append("One or more sockets listen on all interfaces (0.0.0.0 / ::).")

    firewall: dict[str, Any] = {"ok": False, "summary": ""}
    if include_firewall:
        if system.lower().startswith("win"):
            ok, out = _run(
                ["powershell", "-NoProfile", "-Command",
                 "$ErrorActionPreference='Stop'; "
                 "Get-NetFirewallProfile | Select-Object Name,Enabled | ConvertTo-Json -Compress"],
                timeout=25,
            )
            valid_payload = False
            if ok:
                try:
                    payload = json.loads(out)
                    profiles = payload if isinstance(payload, list) else [payload]
                    valid_payload = bool(profiles) and all(
                        isinstance(item, dict)
                        and str(item.get("Name") or "").strip()
                        and "Enabled" in item
                        for item in profiles
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    valid_payload = False
            firewall = {
                "ok": bool(ok and valid_payload),
                "summary": (out or "")[:1500],
            }
            if not firewall["ok"]:
                firewall["error"] = "failed to query Windows firewall profiles"
        else:
            ok, out = _run(["ufw", "status"], timeout=15)
            if not ok:
                ok, out = _run(["systemctl", "is-active", "firewalld"], timeout=10)
            firewall = {"ok": ok, "summary": (out or "")[:1500]}
            if not ok:
                firewall["error"] = "failed to query a supported firewall service"

    port_set = sorted({int(r["port"]) for r in unique if r.get("port") is not None})
    listener_set = sorted(
        (
            str(row.get("proto") or ""),
            str(row.get("address") or ""),
            int(row.get("port") or 0),
        )
        for row in unique
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "listeners": listener_set,
                "risky": sorted({int(r["port"]) for r in risky}),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "ok": True,
        "hostname": hostname,
        "platform": system,
        "platform_release": platform.release(),
        "listener_count": len(unique),
        "listeners": unique[:200],
        "high_interest_listeners": risky[:50],
        "firewall": firewall,
        "risk_hints": hints[:20],
        "baseline": {
            "port_set": port_set,
            "fingerprint": fingerprint,
        },
    }
