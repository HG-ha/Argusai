"""Offline-first, read-only helpers for authorized wireless assessments.

This module deliberately does not expose association, monitor-mode switching,
packet injection, deauthentication, credential recovery, or key cracking.
Host interaction is limited to inventory commands that the operating system
already provides; packet captures are only inspected or turned into an
offline-analysis plan.
"""

from __future__ import annotations

import platform
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs

_NETSH_INTERFACE_FIELDS = {
    "name": "name",
    "名称": "name",
    "description": "description",
    "说明": "description",
    "guid": "guid",
    "physical address": "mac",
    "物理地址": "mac",
    "state": "state",
    "状态": "state",
    "ssid": "ssid",
    "bssid": "bssid",
    "network type": "network_type",
    "网络类型": "network_type",
    "radio type": "radio_type",
    "无线电类型": "radio_type",
    "authentication": "authentication",
    "身份验证": "authentication",
    "cipher": "cipher",
    "密码": "cipher",
    "connection mode": "connection_mode",
    "连接模式": "connection_mode",
    "channel": "channel",
    "频道": "channel",
    "receive rate (mbps)": "receive_rate_mbps",
    "接收速率(mbps)": "receive_rate_mbps",
    "transmit rate (mbps)": "transmit_rate_mbps",
    "传输速率 (mbps)": "transmit_rate_mbps",
    "signal": "signal",
    "信号": "signal",
    "profile": "profile",
    "配置文件": "profile",
}

_NETSH_NETWORK_FIELDS = {
    "network type": "network_type",
    "网络类型": "network_type",
    "authentication": "authentication",
    "身份验证": "authentication",
    "encryption": "encryption",
    "加密": "encryption",
}

_NETSH_BSSID_FIELDS = {
    "signal": "signal",
    "信号": "signal",
    "radio type": "radio_type",
    "无线电类型": "radio_type",
    "channel": "channel",
    "频道": "channel",
}

_NMCLI_FIELDS = (
    "in_use",
    "ssid",
    "bssid",
    "mode",
    "channel",
    "frequency_mhz",
    "rate",
    "signal",
    "bars",
    "security",
)

_FORBIDDEN_WIRELESS_ACTIONS = frozenset({
    "deauth",
    "deauthentication",
    "disassociate",
    "disconnect",
    "packet_injection",
    "inject",
    "crack",
    "cracking",
    "brute_force",
    "bruteforce",
    "wps_attack",
    "evil_twin",
    "credential_recovery",
})

_READ_ONLY_ACTIONS = frozenset({
    "preflight",
    "inventory",
    "show_interfaces",
    "show_networks",
    "parse_capture",
    "pcap_plan",
})


def _split_netsh_field(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([^:]+?)\s+:\s*(.*)$", line.rstrip())
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _coerce_percent(value: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*%", str(value or ""))
    return max(0, min(100, int(match.group(1)))) if match else None


def _coerce_int(value: str) -> int | None:
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group(0)) if match else None


def _normalize_interface(row: dict[str, Any]) -> dict[str, Any]:
    if "channel" in row:
        row["channel"] = _coerce_int(str(row["channel"]))
    if "signal" in row:
        row["signal_percent"] = _coerce_percent(str(row["signal"]))
    for key in ("receive_rate_mbps", "transmit_rate_mbps"):
        if key in row:
            try:
                row[key] = float(str(row[key]).replace(",", "."))
            except ValueError:
                pass
    return row


def parse_netsh_wlan_interfaces(text: str) -> dict[str, Any]:
    """Parse ``netsh wlan show interfaces`` without assuming one adapter."""
    interfaces: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw_line in str(text or "").splitlines():
        field = _split_netsh_field(raw_line)
        if not field:
            continue
        label, value = field
        key = _NETSH_INTERFACE_FIELDS.get(label.lower()) or _NETSH_INTERFACE_FIELDS.get(label)
        if not key:
            continue
        if key == "name" and current:
            interfaces.append(_normalize_interface(current))
            current = {}
        current[key] = value
    if current:
        interfaces.append(_normalize_interface(current))
    return {
        "ok": True,
        "source": "netsh",
        "interface_count": len(interfaces),
        "interfaces": interfaces,
    }


def parse_netsh_wlan_networks(text: str) -> dict[str, Any]:
    """Parse ``netsh wlan show networks mode=bssid`` output."""
    networks: list[dict[str, Any]] = []
    current_network: dict[str, Any] | None = None
    current_bssid: dict[str, Any] | None = None

    def finish_bssid() -> None:
        nonlocal current_bssid
        if current_network is not None and current_bssid is not None:
            if "channel" in current_bssid:
                current_bssid["channel"] = _coerce_int(str(current_bssid["channel"]))
            if "signal" in current_bssid:
                current_bssid["signal_percent"] = _coerce_percent(str(current_bssid["signal"]))
            current_network.setdefault("bssids", []).append(current_bssid)
        current_bssid = None

    def finish_network() -> None:
        nonlocal current_network
        finish_bssid()
        if current_network is not None:
            networks.append(current_network)
        current_network = None

    interface_name = ""
    for raw_line in str(text or "").splitlines():
        field = _split_netsh_field(raw_line)
        if not field:
            continue
        label, value = field
        label_lower = label.lower()
        if label_lower == "interface name" or label == "接口名称":
            interface_name = value
            continue
        if re.fullmatch(r"ssid\s+\d+", label_lower) or re.fullmatch(r"ssid\s+\d+", label, re.I):
            finish_network()
            current_network = {"ssid": value, "interface": interface_name, "bssids": []}
            continue
        if re.fullmatch(r"bssid\s+\d+", label_lower):
            if current_network is None:
                current_network = {"ssid": "", "interface": interface_name, "bssids": []}
            finish_bssid()
            current_bssid = {"bssid": value}
            continue
        if current_network is None:
            continue
        network_key = _NETSH_NETWORK_FIELDS.get(label_lower) or _NETSH_NETWORK_FIELDS.get(label)
        bssid_key = _NETSH_BSSID_FIELDS.get(label_lower) or _NETSH_BSSID_FIELDS.get(label)
        if current_bssid is not None and bssid_key:
            current_bssid[bssid_key] = value
        elif network_key:
            current_network[network_key] = value
    finish_network()
    return {
        "ok": True,
        "source": "netsh",
        "network_count": len(networks),
        "networks": networks,
    }


def _split_nmcli_terse(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def parse_nmcli_wifi(
    text: str,
    *,
    fields: Iterable[str] = _NMCLI_FIELDS,
) -> dict[str, Any]:
    """Parse escaped, colon-delimited ``nmcli --terse`` Wi-Fi inventory."""
    names = [str(field).strip().lower().replace("-", "_") for field in fields]
    networks: list[dict[str, Any]] = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        values = _split_nmcli_terse(line.rstrip())
        if len(values) < len(names):
            values.extend([""] * (len(names) - len(values)))
        row = dict(zip(names, values, strict=False))
        if "channel" in row:
            row["channel"] = _coerce_int(row["channel"])
        if "frequency_mhz" in row:
            row["frequency_mhz"] = _coerce_int(row["frequency_mhz"])
        if "signal" in row:
            row["signal_percent"] = _coerce_int(row["signal"])
        if "in_use" in row:
            row["in_use"] = row["in_use"].strip() == "*"
        networks.append(row)
    return {
        "ok": True,
        "source": "nmcli",
        "network_count": len(networks),
        "networks": networks,
    }


def wireless_action_policy(action: str) -> dict[str, Any]:
    """Return an explicit deny decision for offensive or state-changing actions."""
    normalized = re.sub(r"[\s-]+", "_", str(action or "").strip().lower())
    forbidden = normalized in _FORBIDDEN_WIRELESS_ACTIONS or any(
        token in normalized for token in ("deauth", "crack", "brute", "inject", "evil_twin")
    )
    allowed = normalized in _READ_ONLY_ACTIONS and not forbidden
    return {
        "ok": allowed,
        "allowed": allowed,
        "blocked": not allowed,
        "action": normalized,
        "mode": "read-only",
        "reason": (
            "wireless action is destructive, disruptive, or credential-focused"
            if forbidden
            else "action is not in the read-only wireless allowlist"
            if not allowed
            else ""
        ),
    }


def wireless_toolchain_preflight(
    *,
    platform_name: str = "",
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    """Probe read-only host inventory and offline PCAP tooling."""
    system = (platform_name or platform.system()).strip().lower()
    windows = system.startswith("win")
    inventory_tool = "netsh" if windows else "nmcli"
    inventory_path = which(inventory_tool)
    tshark_path = which("tshark")
    return {
        "ok": True,
        "platform": system,
        "mode": "read-only",
        "tools": {
            inventory_tool: {
                "available": bool(inventory_path),
                "path": inventory_path or "",
                "purpose": "OS WLAN inventory only",
            },
            "tshark": {
                "available": bool(tshark_path),
                "path": tshark_path or "",
                "purpose": "offline 802.11 PCAP analysis only",
            },
        },
        "inventory": {
            "ready": bool(inventory_path),
            "blocked": not bool(inventory_path),
            "blocked_reason": "" if inventory_path else f"{inventory_tool} not found",
        },
        "pcap": {
            "ready": bool(tshark_path),
            "blocked": not bool(tshark_path),
            "blocked_reason": "" if tshark_path else "tshark not found; planning and header inspection remain available",
        },
        "forbidden": sorted(_FORBIDDEN_WIRELESS_ACTIONS),
    }


wireless_environment_preflight = wireless_toolchain_preflight


def _run_readonly(
    argv: list[str],
    *,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    try:
        proc = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout_seconds or 15), 60)),
            check=False,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "blocked": True, "error": str(exc)}
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = f"{stdout}\n{stderr}".lower()
    blocked_signals = (
        "access is denied",
        "requires elevation",
        "permission denied",
        "wireless autoconfig service is not running",
        "no wi-fi device found",
        "no wireless extensions",
    )
    blocked = proc.returncode != 0 and any(signal in combined for signal in blocked_signals)
    return {
        "ok": proc.returncode == 0,
        "blocked": blocked,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "error": (stderr or stdout).strip()[:2000] if proc.returncode else "",
    }


def wlan_inventory(
    *,
    platform_name: str = "",
    timeout_seconds: int = 15,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run only the OS vendor's non-mutating WLAN inventory commands."""
    preflight = wireless_toolchain_preflight(platform_name=platform_name)
    system = preflight["platform"]
    windows = system.startswith("win")
    tool = "netsh" if windows else "nmcli"
    status = preflight["tools"][tool]
    if not status["available"]:
        return {
            "ok": False,
            "blocked": True,
            "blocked_reason": "missing_tool",
            "error": f"{tool} not found; WLAN inventory cannot run",
            "preflight": preflight,
        }

    if windows:
        commands = [
            [status["path"], "wlan", "show", "interfaces"],
            [status["path"], "wlan", "show", "networks", "mode=bssid"],
        ]
        interface_run = _run_readonly(commands[0], timeout_seconds=timeout_seconds, runner=runner)
        network_run = _run_readonly(commands[1], timeout_seconds=timeout_seconds, runner=runner)
        if not interface_run["ok"] or not network_run["ok"]:
            failed = interface_run if not interface_run["ok"] else network_run
            return {
                "ok": False,
                "blocked": bool(failed.get("blocked")),
                "blocked_reason": "driver_or_permission" if failed.get("blocked") else "command_failed",
                "error": failed.get("error") or "netsh WLAN inventory failed",
                "commands": commands,
                "preflight": preflight,
            }
        interfaces = parse_netsh_wlan_interfaces(interface_run["stdout"])
        networks = parse_netsh_wlan_networks(network_run["stdout"])
        return {
            "ok": True,
            "blocked": False,
            "source": "netsh",
            "mode": "read-only",
            "commands": commands,
            "interfaces": interfaces["interfaces"],
            "networks": networks["networks"],
            "preflight": preflight,
        }

    fields = "IN-USE,SSID,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,BARS,SECURITY"
    command = [status["path"], "--terse", "--escape", "yes", "-f", fields, "device", "wifi", "list", "--rescan", "no"]
    run = _run_readonly(command, timeout_seconds=timeout_seconds, runner=runner)
    if not run["ok"]:
        return {
            "ok": False,
            "blocked": bool(run.get("blocked")),
            "blocked_reason": "driver_or_permission" if run.get("blocked") else "command_failed",
            "error": run.get("error") or "nmcli WLAN inventory failed",
            "commands": [command],
            "preflight": preflight,
        }
    parsed = parse_nmcli_wifi(run["stdout"])
    return {
        "ok": True,
        "blocked": False,
        "source": "nmcli",
        "mode": "read-only",
        "commands": [command],
        "interfaces": [],
        "networks": parsed["networks"],
        "preflight": preflight,
    }


def inspect_pcap_header(path: str | Path) -> dict[str, Any]:
    """Inspect capture magic/link type without invoking a packet parser."""
    capture = Path(path).expanduser().resolve()
    if not capture.is_file():
        return {"ok": False, "error": f"capture not found: {capture}"}
    raw = capture.read_bytes()[:64]
    if len(raw) < 4:
        return {"ok": False, "error": "capture is too small", "path": str(capture)}
    magic = raw[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        return {
            "ok": True,
            "path": str(capture),
            "format": "pcapng",
            "size": capture.stat().st_size,
            "link_type": None,
            "link_type_name": "read interface description blocks with tshark",
            "is_80211": None,
        }
    endian = "<" if magic in {b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1"} else ">" if magic in {b"\xa1\xb2\xc3\xd4", b"\xa1\xb2<M"} else ""
    if not endian or len(raw) < 24:
        return {"ok": False, "error": "unsupported capture magic", "path": str(capture), "magic_hex": magic.hex()}
    link_type = struct.unpack(f"{endian}I", raw[20:24])[0]
    link_names = {
        105: "IEEE802_11",
        119: "PRISM_HEADER",
        127: "IEEE802_11_RADIOTAP",
        163: "IEEE802_11_AVS",
        192: "PPI",
    }
    return {
        "ok": True,
        "path": str(capture),
        "format": "pcap",
        "size": capture.stat().st_size,
        "link_type": link_type,
        "link_type_name": link_names.get(link_type, "other"),
        "is_80211": link_type in link_names,
    }


def build_80211_pcap_plan(path: str | Path) -> dict[str, Any]:
    """Build a bounded, offline-only tshark plan for an existing capture."""
    header = inspect_pcap_header(path)
    if not header.get("ok"):
        return header
    capture = str(Path(path).expanduser().resolve())
    tshark = shutil.which("tshark") or "tshark"
    commands = [
        [tshark, "-r", capture, "-q", "-z", "io,phs"],
        [
            tshark,
            "-r",
            capture,
            "-Y",
            "wlan.fc.type == 0",
            "-T",
            "fields",
            "-e",
            "frame.number",
            "-e",
            "wlan.fc.type_subtype",
            "-e",
            "wlan.sa",
            "-e",
            "wlan.da",
            "-e",
            "wlan.bssid",
            "-e",
            "wlan.ssid",
        ],
        [
            tshark,
            "-r",
            capture,
            "-Y",
            "eapol || wlan_mgt.fixed.auth_alg || wlan_rsna_eapol",
            "-T",
            "fields",
            "-e",
            "frame.number",
            "-e",
            "wlan.bssid",
            "-e",
            "wlan.sa",
            "-e",
            "_ws.col.Protocol",
        ],
    ]
    available = bool(shutil.which("tshark"))
    return {
        "ok": True,
        "blocked": not available,
        "blocked_reason": "" if available else "tshark_missing",
        "mode": "offline-read-only",
        "capture": header,
        "tool": {"name": "tshark", "available": available, "path": shutil.which("tshark") or ""},
        "commands": commands,
        "steps": [
            "Confirm the capture link type is 802.11/radiotap or inspect pcapng interface metadata.",
            "Inventory management, control, and data protocols without transmitting frames.",
            "Review beacon/probe/authentication/EAPOL metadata and redact SSIDs or station MACs before sharing.",
            "Store the capture and derived summaries as Case artifacts; promote only evidence-backed findings.",
        ],
        "forbidden": [
            "deauthentication or disassociation",
            "packet injection",
            "credential/key cracking",
            "active association or evil-twin operation",
        ],
        "next_step": (
            "Run the argv entries in an approved offline environment."
            if available
            else "Install Wireshark/tshark manually or inspect the capture in an existing offline analyzer."
        ),
    }


plan_80211_pcap_analysis = build_80211_pcap_plan


__all__ = [
    "build_80211_pcap_plan",
    "inspect_pcap_header",
    "parse_netsh_wlan_interfaces",
    "parse_netsh_wlan_networks",
    "parse_nmcli_wifi",
    "plan_80211_pcap_analysis",
    "wireless_action_policy",
    "wireless_environment_preflight",
    "wireless_toolchain_preflight",
    "wlan_inventory",
]
