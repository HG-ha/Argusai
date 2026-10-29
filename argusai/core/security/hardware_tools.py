"""Read-only and offline helpers for hardware-security evidence.

No function in this module flashes, erases, programs, resets, halts, glitches,
or otherwise changes a board. CHIPSEC/OpenOCD support is intentionally limited
to environment preflight and a small command allowlist.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

_HARDWARE_TOOLS = {
    "sigrok-cli": ("sigrok-cli",),
    "chipsec": ("chipsec_main", "chipsec_util"),
    "openocd": ("openocd",),
}

_FORBIDDEN_ACTION_TOKENS = frozenset({
    "deauth",
    "crack",
    "erase",
    "flash",
    "glitch",
    "halt",
    "inject",
    "load_image",
    "mass_erase",
    "program",
    "reset",
    "resume",
    "svf",
    "write",
})

_READ_ONLY_COMMANDS: dict[str, dict[str, list[str]]] = {
    "chipsec": {
        "version": ["chipsec_main", "-v"],
        "help": ["chipsec_main", "-h"],
        "platform_info": ["chipsec_util", "platform"],
    },
    "openocd": {
        "version": ["openocd", "--version"],
        "help": ["openocd", "--help"],
    },
    "sigrok-cli": {
        "version": ["sigrok-cli", "--version"],
        "help": ["sigrok-cli", "--help"],
        "device_scan": ["sigrok-cli", "--scan"],
    },
}

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UART_PREFIX_RE = re.compile(
    r"^\s*(?:(?P<timestamp>\[[^\]]+\]|\d{4}-\d{2}-\d{2}[T ][^\s]+)\s*)?"
    r"(?:(?P<direction>RX|TX|IN|OUT)\s*[:>|-]\s*)?"
    r"(?P<message>.*)$",
    re.IGNORECASE,
)

_UART_INDICATORS = (
    ("u-boot", "bootloader", "U-Boot bootloader banner"),
    ("barebox", "bootloader", "Barebox bootloader banner"),
    ("booting linux", "kernel", "Linux boot transition"),
    ("linux version", "kernel", "Linux kernel banner"),
    ("kernel panic", "crash", "Kernel panic"),
    ("init started", "userspace", "init userspace transition"),
    ("starting kernel", "kernel", "Kernel start"),
    ("login:", "login", "Interactive login prompt"),
    ("password:", "login", "Password prompt"),
    ("console=", "boot_argument", "Console exposed in boot arguments"),
    ("init=/bin/sh", "security_signal", "Boot arguments request a shell as init"),
    ("single", "security_signal", "Single-user boot hint"),
    ("jtag", "debug", "JTAG/debug reference"),
    ("secure boot", "secure_boot", "Secure Boot status reference"),
    ("verified boot", "secure_boot", "Verified Boot status reference"),
)

_SIGROK_DECODERS = frozenset({
    "uart",
    "spi",
    "i2c",
    "jtag",
    "swd",
    "can",
    "lin",
    "onewire_network",
    "usb_packet",
})


def _admin_status(system: str) -> tuple[bool, str]:
    if system.startswith("win"):
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin()), "windows_admin_token"
        except Exception:
            return False, "windows_admin_unknown"
    if hasattr(os, "geteuid"):
        try:
            return os.geteuid() == 0, "effective_uid"
        except OSError:
            return False, "effective_uid_unknown"
    return False, "privilege_unknown"


def hardware_toolchain_preflight(
    *,
    paths: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform_name: str = "",
    elevated: bool | None = None,
) -> dict[str, Any]:
    """Probe local tools and report unverified hardware/driver prerequisites."""
    overrides = {str(k).lower(): str(v) for k, v in (paths or {}).items()}
    system = (platform_name or platform.system()).strip().lower()
    detected_elevated, privilege_source = _admin_status(system)
    is_elevated = detected_elevated if elevated is None else bool(elevated)
    tools: dict[str, dict[str, Any]] = {}
    for tool, names in _HARDWARE_TOOLS.items():
        path = ""
        override = overrides.get(tool, "")
        if override and Path(override).expanduser().is_file():
            path = str(Path(override).expanduser().resolve())
        else:
            for name in names:
                found = which(name)
                if found:
                    path = str(Path(found).resolve())
                    break
        reasons: list[str] = []
        if not path:
            reasons.append("missing_tool")
        if tool in {"chipsec", "openocd"}:
            reasons.append("hardware_and_driver_not_verified")
        if tool == "chipsec" and not is_elevated:
            reasons.append("insufficient_privilege")
        tools[tool] = {
            "available": bool(path),
            "path": path,
            "ready": bool(path) and tool == "sigrok-cli",
            "blocked": bool(reasons),
            "blocked_reasons": reasons,
            "mode": "preflight/read-only",
        }
    return {
        "ok": True,
        "platform": system,
        "mode": "preflight-only",
        "privilege": {"elevated": is_elevated, "source": privilege_source},
        "tools": tools,
        "available": [name for name, info in tools.items() if info["available"]],
        "constraints": [
            "Tool presence does not prove a board, probe, driver, cable, or target power is available.",
            "CHIPSEC/OpenOCD actions remain blocked until an operator validates hardware and driver prerequisites.",
            "No flash, erase, program, reset, halt, resume, SVF, or glitch operation is allowed.",
        ],
    }


hardware_environment_preflight = hardware_toolchain_preflight


def hardware_action_policy(tool: str, action: str) -> dict[str, Any]:
    """Resolve one exact action against the hardware read-only allowlist."""
    tool_key = str(tool or "").strip().lower()
    action_key = re.sub(r"[\s-]+", "_", str(action or "").strip().lower())
    combined = f"{tool_key} {action_key}"
    forbidden = any(token in combined for token in _FORBIDDEN_ACTION_TOKENS)
    command = (_READ_ONLY_COMMANDS.get(tool_key) or {}).get(action_key)
    allowed = bool(command) and not forbidden
    return {
        "ok": allowed,
        "allowed": allowed,
        "blocked": not allowed,
        "tool": tool_key,
        "action": action_key,
        "mode": "read-only",
        "command": list(command or []),
        "reason": (
            "state-changing or fault-injection action is forbidden"
            if forbidden
            else "action is not in the exact read-only allowlist"
            if not allowed
            else ""
        ),
    }


validate_hardware_action = hardware_action_policy


def build_readonly_hardware_plan(
    tool: str,
    action: str,
    *,
    paths: dict[str, str] | None = None,
    elevated: bool | None = None,
) -> dict[str, Any]:
    """Return an argv plan but never execute it or connect to a target."""
    policy = hardware_action_policy(tool, action)
    if not policy["allowed"]:
        return policy
    preflight = hardware_toolchain_preflight(paths=paths, elevated=elevated)
    tool_info = preflight["tools"].get(policy["tool"]) or {}
    if not tool_info.get("available"):
        return {
            **policy,
            "ok": False,
            "allowed": True,
            "blocked": True,
            "blocked_reason": "missing_tool",
            "error": f"{policy['tool']} not found",
            "preflight": preflight,
        }
    command = list(policy["command"])
    command[0] = str(tool_info["path"])
    needs_hardware = policy["action"] in {"platform_info", "device_scan"}
    reasons = [
        reason
        for reason in tool_info.get("blocked_reasons", [])
        if reason != "missing_tool"
    ]
    blocked = bool(needs_hardware and reasons)
    return {
        **policy,
        "ok": not blocked,
        "blocked": blocked,
        "blocked_reason": "hardware_or_driver_unverified" if blocked else "",
        "command": command,
        "preflight": preflight,
        "execution": "not_performed",
    }


readonly_hardware_plan = build_readonly_hardware_plan


def parse_uart_log(
    text: str,
    *,
    max_lines: int = 5000,
    max_events: int = 500,
) -> dict[str, Any]:
    """Normalize an existing UART transcript and extract bounded boot signals."""
    line_cap = max(1, min(int(max_lines or 5000), 50000))
    event_cap = max(1, min(int(max_events or 500), 5000))
    lines = _ANSI_RE.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n").splitlines()
    events: list[dict[str, Any]] = []
    stages: list[str] = []
    for index, raw_line in enumerate(lines[:line_cap], start=1):
        match = _UART_PREFIX_RE.match(raw_line)
        if not match:
            continue
        message = (match.group("message") or "").rstrip()
        lower = message.lower()
        indicators = []
        for needle, kind, description in _UART_INDICATORS:
            if needle in lower:
                indicators.append({"kind": kind, "description": description, "needle": needle})
                if kind in {"bootloader", "kernel", "userspace", "login"} and kind not in stages:
                    stages.append(kind)
        if indicators or match.group("direction") or match.group("timestamp"):
            events.append({
                "line": index,
                "timestamp": (match.group("timestamp") or "").strip("[]"),
                "direction": (match.group("direction") or "").upper(),
                "message": message[:2000],
                "indicators": indicators,
            })
            if len(events) >= event_cap:
                break
    security_signals = [
        {
            "line": event["line"],
            "message": event["message"],
            **indicator,
        }
        for event in events
        for indicator in event["indicators"]
        if indicator["kind"] in {"security_signal", "boot_argument", "debug", "secure_boot", "login"}
    ]
    return {
        "ok": True,
        "mode": "offline-read-only",
        "line_count": len(lines),
        "lines_processed": min(len(lines), line_cap),
        "events": events,
        "event_count": len(events),
        "stages": stages,
        "security_signals": security_signals,
        "truncated": len(lines) > line_cap or len(events) >= event_cap,
        "confidence_note": "String indicators are triage leads, not proof that a debug or authentication bypass is exploitable.",
    }


parse_uart_capture = parse_uart_log


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def diff_flash_images(
    baseline_path: str | Path,
    candidate_path: str | Path,
    *,
    chunk_size: int = 1024 * 1024,
    max_ranges: int = 200,
) -> dict[str, Any]:
    """Byte-diff two existing flash dumps without writing either image."""
    baseline = Path(baseline_path).expanduser().resolve()
    candidate = Path(candidate_path).expanduser().resolve()
    if not baseline.is_file():
        return {"ok": False, "error": f"baseline flash image not found: {baseline}"}
    if not candidate.is_file():
        return {"ok": False, "error": f"candidate flash image not found: {candidate}"}
    step = max(4096, min(int(chunk_size or 1024 * 1024), 16 * 1024 * 1024))
    range_cap = max(1, min(int(max_ranges or 200), 5000))
    ranges: list[dict[str, int]] = []
    differing_bytes = 0
    offset = 0
    open_start: int | None = None
    last_diff = -2
    with baseline.open("rb") as left, candidate.open("rb") as right:
        while True:
            left_chunk = left.read(step)
            right_chunk = right.read(step)
            if not left_chunk and not right_chunk:
                break
            width = max(len(left_chunk), len(right_chunk))
            for index in range(width):
                left_byte = left_chunk[index] if index < len(left_chunk) else None
                right_byte = right_chunk[index] if index < len(right_chunk) else None
                if left_byte == right_byte:
                    continue
                absolute = offset + index
                differing_bytes += 1
                if open_start is None:
                    open_start = absolute
                elif absolute != last_diff + 1:
                    if len(ranges) < range_cap:
                        ranges.append({
                            "start": open_start,
                            "end": last_diff + 1,
                            "length": last_diff - open_start + 1,
                        })
                    open_start = absolute
                last_diff = absolute
            offset += width
    if open_start is not None and len(ranges) < range_cap:
        ranges.append({
            "start": open_start,
            "end": last_diff + 1,
            "length": last_diff - open_start + 1,
        })
    left_size = baseline.stat().st_size
    right_size = candidate.stat().st_size
    return {
        "ok": True,
        "mode": "offline-read-only",
        "baseline": {
            "path": str(baseline),
            "size": left_size,
            "sha256": _sha256_file(baseline),
        },
        "candidate": {
            "path": str(candidate),
            "size": right_size,
            "sha256": _sha256_file(candidate),
        },
        "identical": differing_bytes == 0,
        "size_changed": left_size != right_size,
        "differing_bytes": differing_bytes,
        "diff_ranges": ranges,
        "range_count": len(ranges),
        "ranges_truncated": len(ranges) >= range_cap and differing_bytes > sum(item["length"] for item in ranges),
    }


flash_image_diff = diff_flash_images


def _normalize_channels(channels: Iterable[str] | str | None) -> list[str]:
    if isinstance(channels, str):
        raw = [item.strip() for item in channels.split(",")]
    else:
        raw = [str(item).strip() for item in (channels or [])]
    return [item for item in raw if item and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", item)][:64]


def build_sigrok_offline_plan(
    capture_path: str | Path,
    *,
    decoder: str = "",
    channels: Iterable[str] | str | None = None,
    decoder_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sigrok-cli argv list that only reads an existing capture."""
    capture = Path(capture_path).expanduser().resolve()
    if not capture.is_file():
        return {"ok": False, "blocked": True, "error": f"capture not found: {capture}"}
    decoder_key = str(decoder or "").strip().lower()
    if decoder_key and decoder_key not in _SIGROK_DECODERS:
        return {
            "ok": False,
            "blocked": True,
            "error": f"decoder is not in the offline allowlist: {decoder_key}",
            "allowed_decoders": sorted(_SIGROK_DECODERS),
        }
    sigrok = shutil.which("sigrok-cli")
    argv = [sigrok or "sigrok-cli", "--input-file", str(capture)]
    channel_list = _normalize_channels(channels)
    if channel_list:
        argv.extend(["--channels", ",".join(channel_list)])
    clean_options: list[str] = []
    for key, value in (decoder_options or {}).items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", key_text):
            return {"ok": False, "blocked": True, "error": f"invalid decoder option: {key_text}"}
        if not re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,128}", value_text):
            return {"ok": False, "blocked": True, "error": f"unsafe decoder option value for {key_text}"}
        clean_options.append(f"{key_text}={value_text}")
    if decoder_key:
        decoder_spec = decoder_key + (":" + ":".join(clean_options) if clean_options else "")
        argv.extend(["--protocol-decoders", decoder_spec, "--protocol-decoder-annotations"])
    else:
        argv.append("--show")
    return {
        "ok": True,
        "blocked": not bool(sigrok),
        "blocked_reason": "" if sigrok else "sigrok_cli_missing",
        "mode": "offline-read-only",
        "capture": str(capture),
        "capture_size": capture.stat().st_size,
        "decoder": decoder_key,
        "channels": channel_list,
        "command": argv,
        "execution": "not_performed",
        "next_step": (
            "Run this argv in the approved offline evidence workspace."
            if sigrok
            else "Install sigrok-cli manually or open the capture in PulseView; no hardware capture is attempted."
        ),
    }


sigrok_offline_plan = build_sigrok_offline_plan


__all__ = [
    "build_readonly_hardware_plan",
    "build_sigrok_offline_plan",
    "diff_flash_images",
    "flash_image_diff",
    "hardware_action_policy",
    "hardware_environment_preflight",
    "hardware_toolchain_preflight",
    "parse_uart_capture",
    "parse_uart_log",
    "readonly_hardware_plan",
    "sigrok_offline_plan",
    "validate_hardware_action",
]
